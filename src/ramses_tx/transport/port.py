#!/usr/bin/env python3
"""RAMSES RF - Serial port packet transport.

For ser2net, use the following YAML with:
``ser2net -c examples/ser2net.yaml``

.. code-block::

    connection: &con00
    accepter: telnet(rfc2217),tcp,5001
    timeout: 0
    connector: serialdev,/dev/ttyUSB0,115200n81,local
    options:
        max-connections: 3

For ``socat``, see:

.. code-block::

    socat -dd pty,raw,echo=0 pty,raw,echo=0
    python client.py monitor /dev/pts/0
    cat packet.log | cut -d ' ' -f 2- | unix2dos > /dev/pts/1

For re-flashing evofw3 via Arduino IDE on *my* atmega328p (YMMV):

  - Board:      atmega328p (SW UART)
  - Bootloader: Old Bootloader
  - Processor:  atmega328p (5V, 16 MHz)
  - Host:       57600 (or 115200, YMMV)
  - Pinout:     Nano

For re-flashing evofw3 via Arduino IDE on *my* atmega32u4 (YMMV):

  - Board:      atmega32u4 (HW UART)
  - Processor:  atmega32u4 (5V, 16 MHz)
  - Pinout:     Pro Micro
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine, Iterable
from datetime import datetime as dt
from functools import wraps
from time import perf_counter, time
from typing import Final, ParamSpec, Protocol, TypeVar, runtime_checkable

import serialx
from serialx import BaseSerialTransport, SerialException

from .. import exceptions as exc
from ..address import ALL_DEV_ADDR, HGI_DEV_ADDR, NON_DEV_ADDR
from ..const import (
    DUTY_CYCLE_DURATION,
    I_,
    MAX_DUTY_CYCLE_RATE,
    MIN_INTER_WRITE_GAP,
    SZ_ACTIVE_HGI,
    SZ_NAME,
    SZ_SIGNATURE,
    Code,
)
from ..discovery import is_hgi80
from ..dtos import CommandDTO
from ..helpers import hex_from_str
from ..packet import Packet
from ..schemas import (
    SCH_SERIAL_PORT_CONFIG,
    SZ_BAUDRATE,
    SZ_RTSCTS,
    SZ_XONXOFF,
)
from ..typing import PortConfigT, RamsesProtocolT, SerPortNameT
from ..version import VERSION
from .base import SignaturePolicy, TransportConfig, _FullTransport
from .helpers import _normalise, _str

_LOGGER = logging.getLogger(__name__)

_SIGNATURE_GAP_SECS: Final[float] = 0.05
_SIGNATURE_MAX_TRYS: Final[int] = 40  # was: 24
_SIGNATURE_MAX_SECS: Final[int] = 3

_DBG_DISABLE_DUTY_CYCLE_LIMIT: Final[bool] = False
_DBG_FORCE_FRAME_LOGGING: Final[bool] = False

_P = ParamSpec("_P")
_R = TypeVar("_R")

__all__ = [
    "PortTransport",
    "limit_duty_cycle",
]


@runtime_checkable
class _DutyCycleTarget(Protocol):
    """Protocol for objects supporting duty cycle tracking."""

    _tx_bits_in_bucket: float | None
    _tx_last_time_bit_added: float | None


def limit_duty_cycle(
    max_duty_cycle: float, time_window: int = DUTY_CYCLE_DURATION
) -> Callable[
    [Callable[_P, Coroutine[object, object, _R]]],
    Callable[_P, Coroutine[object, object, _R]],
]:
    """Limit the Tx rate to the RF duty cycle regulations (e.g. 1% per hour).

    :param max_duty_cycle: Maximum duty cycle fraction (0.0 to 1.0).
    :type max_duty_cycle: float
    :param time_window: Time window in seconds.
    :type time_window: int
    :returns: Decorated asynchronous write callable.
    :rtype: Callable[[Callable[_P, Coroutine[object, object, _R]]], Callable[_P, Coroutine[object, object, _R]]]
    """
    TX_RATE_AVAIL: int = 38400  # bits per second (deemed)
    FILL_RATE: float = TX_RATE_AVAIL * max_duty_cycle  # bits per second
    BUCKET_CAPACITY: float = FILL_RATE * time_window

    def decorator(
        fnc: Callable[_P, Coroutine[object, object, _R]],
    ) -> Callable[_P, Coroutine[object, object, _R]]:
        @wraps(fnc)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            self_obj: object = args[0] if args else None
            frame_obj: object = (
                args[1] if len(args) > 1 else kwargs.get("frame", "")
            )
            frame = str(frame_obj)

            if isinstance(self_obj, _DutyCycleTarget):
                if (
                    self_obj._tx_bits_in_bucket is None
                    or self_obj._tx_last_time_bit_added is None
                ):
                    self_obj._tx_bits_in_bucket = BUCKET_CAPACITY
                    self_obj._tx_last_time_bit_added = perf_counter()

                rf_frame_size = 330 + len(frame[46:]) * 10

                elapsed_time = (
                    perf_counter() - self_obj._tx_last_time_bit_added
                )
                self_obj._tx_bits_in_bucket = min(
                    self_obj._tx_bits_in_bucket + elapsed_time * FILL_RATE,
                    BUCKET_CAPACITY,
                )
                self_obj._tx_last_time_bit_added = perf_counter()

                disable_tx_limits = bool(
                    kwargs.get("disable_tx_limits", False)
                    or (len(args) > 2 and args[2] is True)
                )

                if _DBG_DISABLE_DUTY_CYCLE_LIMIT or disable_tx_limits:
                    self_obj._tx_bits_in_bucket = BUCKET_CAPACITY

                if self_obj._tx_bits_in_bucket < rf_frame_size:
                    await asyncio.sleep(
                        (rf_frame_size - self_obj._tx_bits_in_bucket)
                        / FILL_RATE
                    )

                try:
                    return await fnc(*args, **kwargs)
                finally:
                    if self_obj._tx_bits_in_bucket is not None:
                        self_obj._tx_bits_in_bucket -= rf_frame_size

            return await fnc(*args, **kwargs)

        @wraps(fnc)
        async def null_wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            return await fnc(*args, **kwargs)

        if 0 < max_duty_cycle <= 1:
            return wrapper

        return null_wrapper

    return decorator


class _PortBridgeProtocol(asyncio.Protocol):
    """Bridge protocol between serialx transport and PortTransport."""

    def __init__(self, port_transport: PortTransport) -> None:
        """Initialise the bridge protocol.

        :param port_transport: The parent PortTransport instance.
        :type port_transport: PortTransport
        """
        self._port_transport = port_transport

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Handle connection made callback from serialx.

        :param transport: Underlying BaseSerialTransport instance.
        :type transport: asyncio.BaseTransport
        """
        if isinstance(transport, BaseSerialTransport):
            self._port_transport._serial_transport = transport

    def data_received(self, data: bytes) -> None:
        """Handle incoming serial data chunk.

        :param data: Raw byte chunk received from serial port.
        :type data: bytes
        """
        self._port_transport._data_received(data)

    def connection_lost(self, exc: Exception | None) -> None:
        """Handle connection lost event from serialx.

        :param exc: The exception causing connection loss, or None.
        :type exc: Exception | None
        """
        self._port_transport._connection_lost(exc)


class PortTransport(_FullTransport):
    """Send/receive packets async to/from evofw3/HGI80 via a serial port.

    See: https://github.com/ghoti57/evofw3
    """

    _init_fut: asyncio.Future[Packet | None]
    _init_task: asyncio.Task[None]
    _leaker_task: asyncio.Task[None]
    _conn_task: asyncio.Task[None] | None

    _serial_transport: BaseSerialTransport | None
    _port_name: SerPortNameT
    _port_config: PortConfigT

    _recv_buffer: bytes = b""
    _max_read_size: int = 1024

    _tx_bits_in_bucket: float | None = None
    _tx_last_time_bit_added: float | None = None

    def __init__(
        self,
        port_source: SerPortNameT | BaseSerialTransport | object,
        protocol: RamsesProtocolT,
        /,
        *,
        port_config: PortConfigT | None = None,
        config: TransportConfig,
        extra: dict[str, object] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialize the port transport.

        :param port_source: Serial port name, URL, or BaseSerialTransport.
        :type port_source: SerPortNameT | BaseSerialTransport | object
        :param protocol: RamsesProtocol instance receiving packets.
        :type protocol: RamsesProtocolT
        :param port_config: Serial port configuration dictionary.
        :type port_config: PortConfigT | None
        :param config: Transport configuration parameters.
        :type config: TransportConfig
        :param extra: Extra metadata dictionary.
        :type extra: dict[str, object] | None
        :param loop: Asyncio event loop.
        :type loop: asyncio.AbstractEventLoop | None
        """
        _FullTransport.__init__(self, config=config, extra=extra, loop=loop)
        self._protocol = protocol

        self._port_config = SCH_SERIAL_PORT_CONFIG(port_config or {})
        if isinstance(port_source, str):
            self._port_name = SerPortNameT(port_source)
            self._serial_transport = None
        elif isinstance(port_source, BaseSerialTransport):
            self._serial_transport = port_source
            self._port_name = SerPortNameT(
                getattr(port_source.serial, SZ_NAME, "") or ""
            )
        else:
            self._port_name = SerPortNameT(
                getattr(port_source, SZ_NAME, "")
                or getattr(port_source, "port", "")
                or ""
            )
            raw_transport: object = (
                getattr(port_source, "_serial_transport", None)
                if hasattr(port_source, "_serial_transport")
                else None
            )
            self._serial_transport = (
                raw_transport
                if isinstance(raw_transport, BaseSerialTransport)
                else None
            )

        self._tx_bits_in_bucket = None
        self._tx_last_time_bit_added = None
        self._log_all = config.log_all

        self._init_fut = self._loop.create_future()

        self._leaker_sem = asyncio.BoundedSemaphore()
        self._leaker_task = self._loop.create_task(
            self._leak_sem(), name="PortTransport._leak_sem()"
        )

        self._conn_task = self._loop.create_task(
            self._create_connection(),
            name="PortTransport._create_connection()",
        )

    @property
    def serial(self) -> BaseSerialTransport | object | None:
        """Return the underlying serial or transport instance."""
        if self._serial_transport is not None:
            return getattr(
                self._serial_transport, "serial", self._serial_transport
            )
        return None

    async def _create_connection(self) -> None:
        """Invoke connection_made() callback after HGI80 discovery."""
        if self._serial_transport is None:
            bridge_protocol = _PortBridgeProtocol(self)
            try:
                transport, _ = await serialx.create_serial_connection(
                    loop=self._loop,
                    protocol_factory=lambda: bridge_protocol,
                    url=str(self._port_name),
                    baudrate=int(self._port_config.get(SZ_BAUDRATE, 115200)),
                    rtscts=bool(self._port_config.get(SZ_RTSCTS, False)),
                    xonxoff=bool(self._port_config.get(SZ_XONXOFF, False)),
                )
                self._serial_transport = transport
            except (SerialException, OSError, ValueError) as err:
                self._close(exc=exc.TransportSerialError(err))
                if not self._init_fut.done():
                    self._init_fut.set_exception(
                        exc.TransportSerialError(
                            f"Failed to open {self._port_name}: {err}"
                        )
                    )
                return

        self._is_hgi80 = await is_hgi80(self._port_name)

        async def connect_sans_signature() -> None:
            """Call connection_made() without waiting for signature."""
            self._init_fut.set_result(None)
            self._make_connection(gateway_id=None)

        async def connect_with_signature() -> None:
            """Poll with signatures; connect after first echo."""
            payload = (
                f"0010{int(time() * 1000):012X}{hex_from_str(f'v{VERSION}')}"[
                    :48
                ]
            )
            sig = CommandDTO(
                verb=I_,
                addr1=HGI_DEV_ADDR.id,
                addr2=ALL_DEV_ADDR.id,
                addr3=NON_DEV_ADDR.id,
                code=Code._PUZZ,
                payload=payload,
            )
            self._extra[SZ_SIGNATURE] = sig.payload

            num_sends = 0
            while num_sends < _SIGNATURE_MAX_TRYS:
                num_sends += 1

                await self._write_frame(str(sig))
                await asyncio.sleep(_SIGNATURE_GAP_SECS)

                if self._init_fut.done():
                    packet = self._init_fut.result()
                    self._make_connection(
                        gateway_id=packet.src.id if packet else None
                    )
                    return

            if not self._init_fut.done():
                self._init_fut.set_result(None)

            self._make_connection(gateway_id=None)
            return

        async def connect_with_delayed_signature() -> None:
            """Wait grace period, then poll with signatures.

            For ESP32 USB devices that reset on port open (DTR/RTS
            transition pulses EN).  The grace period allows the ESP32
            to boot before sending probes (Phase 2, issue 1119).
            """
            grace = self._startup_grace
            _LOGGER.info(
                "PortTransport: waiting %.1fs grace before signature "
                "probe (signature_policy=DELAYED)",
                grace,
            )
            await asyncio.sleep(grace)
            await connect_with_signature()

        # Dispatch based on disable_sending and signature_policy.
        # disable_sending=True always skips the probe (permanent
        # receive-only, backward-compatible).  When False, the
        # signature_policy controls startup behavior:
        # - IMMEDIATE: probe right after open (default, backward-compatible)
        # - DELAYED: wait startup_grace seconds, then probe
        # - SKIP: no probe; identity learned from inbound traffic
        if self._disable_sending:
            self._init_task = self._loop.create_task(
                connect_sans_signature(),
                name="PortTransport.connect_sans_signature()",
            )
        elif self._signature_policy is SignaturePolicy.SKIP:
            self._init_task = self._loop.create_task(
                connect_sans_signature(),
                name="PortTransport.connect_sans_signature(skip)",
            )
        elif self._signature_policy is SignaturePolicy.DELAYED:
            self._init_task = self._loop.create_task(
                connect_with_delayed_signature(),
                name="PortTransport.connect_with_delayed_signature()",
            )
        else:
            self._init_task = self._loop.create_task(
                connect_with_signature(),
                name="PortTransport.connect_with_signature()",
            )

        # Extend the init timeout when delayed to account for the
        # grace period on top of the signature probe window.
        init_timeout: float = _SIGNATURE_MAX_SECS
        if (
            self._signature_policy is SignaturePolicy.DELAYED
            and not self._disable_sending
        ):
            init_timeout += self._startup_grace

        try:
            await asyncio.wait_for(self._init_fut, timeout=init_timeout)
        except TimeoutError as err:
            raise exc.TransportSerialError(
                f"Failed to initialise Transport within {_SIGNATURE_MAX_SECS} secs"
            ) from err

    async def _leak_sem(self) -> None:
        """Enforce a minimum time between calls to self.write()."""
        while True:
            await asyncio.sleep(MIN_INTER_WRITE_GAP)
            with contextlib.suppress(ValueError):
                self._leaker_sem.release()

    def _data_received(self, data: bytes) -> None:
        """Make Frames from incoming bytes and process them.

        :param data: Incoming raw bytes from serial connection.
        :type data: bytes
        """

        def bytes_read(chunk: bytes) -> Iterable[tuple[dt, bytes]]:
            self._recv_buffer += chunk
            if b"\r\n" in self._recv_buffer:
                lines = self._recv_buffer.split(b"\r\n")
                self._recv_buffer = lines[-1]
                for line in lines[:-1]:
                    yield self._dt_now(), line + b"\r\n"

        if not data:
            return

        for dtm, raw_line in bytes_read(data):
            if _DBG_FORCE_FRAME_LOGGING:
                _LOGGER.warning("Rx: %s", raw_line)
            elif _LOGGER.getEffectiveLevel() == logging.INFO:
                _LOGGER.info("Rx: %s", raw_line)

            self._frame_read(
                dtm.isoformat(timespec="milliseconds"),
                _normalise(_str(raw_line)),
            )

    def _read_ready(self) -> None:
        """Compatibility read method for testing."""
        if self._serial_transport and hasattr(
            self._serial_transport, "serial"
        ):
            try:
                data = self._serial_transport.serial.read(self._max_read_size)
                self._data_received(data)
            except SerialException as err:
                if not self._closing:
                    self._close(exc=exc.TransportSerialError(err))

    def _connection_lost(self, error: Exception | None) -> None:
        """Handle underlying transport disconnection.

        :param error: The exception that caused connection loss, or None.
        :type error: Exception | None
        """
        if not self._closing:
            self._close(exc=exc.TransportSerialError(error) if error else None)

    def _packet_read(self, packet: Packet) -> None:
        if (
            not self._init_fut.done()
            and packet.code == Code._PUZZ
            and packet.payload == self._extra.get(SZ_SIGNATURE)
        ):
            self._extra[SZ_ACTIVE_HGI] = packet.src.id
            self._init_fut.set_result(packet)

        super()._packet_read(packet)

    @limit_duty_cycle(MAX_DUTY_CYCLE_RATE)
    async def write_frame(
        self, frame: str, disable_tx_limits: bool = False
    ) -> None:
        """Transmit a frame via the underlying transport handler.

        :param frame: Raw ASCII frame to transmit.
        :type frame: str
        :param disable_tx_limits: Flag to bypass duty cycle limits.
        :type disable_tx_limits: bool
        """
        await self._leaker_sem.acquire()
        await super().write_frame(frame)

    async def _write_frame(self, frame: str) -> None:
        """Write some data bytes to the underlying transport.

        :param frame: Raw ASCII packet string to write.
        :type frame: str
        """
        data = bytes(frame, "ascii") + b"\r\n"

        if _DBG_FORCE_FRAME_LOGGING:
            _LOGGER.warning("Serial transport Tx frame: %s", frame)
        elif _LOGGER.getEffectiveLevel() > logging.DEBUG or self._log_all:
            _LOGGER.info("Serial transport Tx frame: %s", frame)
        else:
            _LOGGER.debug("Serial transport Tx frame: %s", frame)

        try:
            self._write(data)
        except SerialException as err:
            self._abort(exc.TransportSerialError(err))

    def _write(self, data: bytes) -> None:
        """Perform the actual write to the serial port.

        :param data: Raw ASCII bytes with line terminator to write.
        :type data: bytes
        :raises SerialException: If write to underlying transport fails.
        """
        if self._serial_transport is None:
            raise SerialException("Serial transport is not connected")
        self._serial_transport.write(data)

    def _abort(self, exc_val: Exception) -> None:
        """Abort the transport immediately.

        :param exc_val: Exception causing the abort.
        :type exc_val: Exception
        """
        if self._serial_transport is not None:
            with contextlib.suppress(Exception):
                self._serial_transport.abort()

        self._close(exc=exc.TransportSerialError(exc_val))

    def _close(self, exc: exc.RamsesException | None = None) -> None:
        """Close the transport (cancel any outstanding tasks).

        :param exc: Optional exception causing the closure.
        :type exc: exc.RamsesException | None
        """
        super()._close(exc)

        if self._serial_transport is not None:
            with contextlib.suppress(Exception):
                self._serial_transport.close()

        if init_task := getattr(self, "_init_task", None):
            init_task.cancel()

        if leaker_task := getattr(self, "_leaker_task", None):
            leaker_task.cancel()

        if conn_task := getattr(self, "_conn_task", None):
            conn_task.cancel()
