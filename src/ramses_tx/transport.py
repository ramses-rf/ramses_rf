#!/usr/bin/env python3
"""RAMSES RF - RAMSES-II compatible packet transport.

Operates at the pkt layer of: app - msg - pkt - h/w

For ser2net, use the following YAML with: ``ser2net -c misc/ser2net.yaml``

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
import fileinput
import functools
import glob
import json
import logging
import os
import re
import sys
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime as dt, timedelta as td
from functools import partial, wraps
from io import TextIOWrapper
from string import printable
from time import perf_counter
from typing import TYPE_CHECKING, Any, Final, TypeAlias
from urllib.parse import parse_qs, unquote, urlparse

from paho.mqtt import MQTTException, client as mqtt

try:
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
    # Fallback for Paho MQTT < 2.0.0 (Home Assistant compatibility)
    CallbackAPIVersion = None  # type: ignore[assignment, misc]
from serial import (  # type: ignore[import-untyped]
    Serial,
    SerialException,
    serial_for_url,
)

from . import exceptions as exc
from .command import Command
from .const import (
    DUTY_CYCLE_DURATION,
    MAX_DUTY_CYCLE_RATE,
    MAX_TRANSMIT_RATE_TOKENS,
    MIN_INTER_WRITE_GAP,
    SZ_ACTIVE_HGI,
    SZ_IS_EVOFW3,
    SZ_SIGNATURE,
)
from .helpers import dt_now
from .packet import Packet
from .schemas import (
    SCH_SERIAL_PORT_CONFIG,
    SZ_EVOFW_FLAG,
    SZ_INBOUND,
    SZ_OUTBOUND,
    DeviceIdT,
    PortConfigT,
)
from .typing import ExceptionT, SerPortNameT

from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

if TYPE_CHECKING:
    from .protocol import RamsesProtocolT


_DEFAULT_TIMEOUT_PORT: Final[float] = 3
_DEFAULT_TIMEOUT_MQTT: Final[float] = 60  # Updated from 9s to 60s for robustness

_SIGNATURE_GAP_SECS = 0.05
_SIGNATURE_MAX_TRYS = 40  # was: 24
_SIGNATURE_MAX_SECS = 3

SZ_RAMSES_GATEWAY: Final = "RAMSES/GATEWAY"
SZ_READER_TASK: Final = "reader_task"


#
# NOTE: All debug flags should be False for deployment to end-users
_DBG_DISABLE_DUTY_CYCLE_LIMIT: Final[bool] = False
_DBG_DISABLE_REGEX_WARNINGS: Final[bool] = False
_DBG_FORCE_FRAME_LOGGING: Final[bool] = False

_LOGGER = logging.getLogger(__name__)


try:
    import serial_asyncio_fast as serial_asyncio  # type: ignore[import-not-found, import-untyped, unused-ignore]

    _LOGGER.debug("Using pyserial-asyncio-fast in place of pyserial-asyncio")
except ImportError:
    import serial_asyncio  # type: ignore[import-not-found, import-untyped, unused-ignore, no-redef]


# For linux, use a modified version of comports() to include /dev/serial/by-id/* links
if os.name == "nt":  # sys.platform == 'win32':
    from serial.tools.list_ports_windows import comports  # type: ignore[import-untyped]

elif os.name != "posix":  # is unsupported
    raise ImportError(
        f"Sorry: no implementation for your platform ('{os.name}') available"
    )

elif sys.platform.lower()[:5] != "linux":  # e.g. osx
    from serial.tools.list_ports_posix import comports  # type: ignore[import-untyped]

else:  # is linux
    # - see: https://github.com/pyserial/pyserial/pull/700
    # - see: https://github.com/pyserial/pyserial/pull/709

    from serial.tools.list_ports_linux import SysFS  # type: ignore[import-untyped]

    def list_links(devices: set[str]) -> list[str]:
        """Search for symlinks to ports already listed in devices.

        :param devices: A set of real device paths.
        :type devices: set[str]
        :return: A list of symlinks pointing to the devices.
        :rtype: list[str]
        """

        links: list[str] = []
        for device in glob.glob("/dev/*") + glob.glob("/dev/serial/by-id/*"):
            if os.path.islink(device) and os.path.realpath(device) in devices:
                links.append(device)
        return links

    def comports(  # type: ignore[no-any-unimported]
        include_links: bool = False, _hide_subsystems: list[str] | None = None
    ) -> list[SysFS]:
        """Return a list of Serial objects for all known serial ports.

        :param include_links: Whether to include symlinks in the results, defaults to False.
        :type include_links: bool, optional
        :param _hide_subsystems: List of subsystems to hide, defaults to None.
        :type _hide_subsystems: list[str] | None, optional
        :return: A list of SysFS objects representing the ports.
        :rtype: list[SysFS]
        """

        if _hide_subsystems is None:
            _hide_subsystems = ["platform"]

        devices = set()
        with open("/proc/tty/drivers") as file:
            drivers = file.readlines()
            for driver in drivers:
                items = driver.strip().split()
                if items[4] == "serial":
                    devices.update(glob.glob(items[1] + "*"))

        if include_links:
            devices.update(list_links(devices))

        result: list[SysFS] = [  # type: ignore[no-any-unimported]
            d for d in map(SysFS, devices) if d.subsystem not in _hide_subsystems
        ]
        return result


async def is_hgi80(serial_port: SerPortNameT) -> bool | None:
    """Return True if the device attached to the port has the attributes of a Honeywell HGI80.

    Return False if it appears to be an evofw3-compatible device (ATMega etc).
    Return None if the type cannot be determined.

    :param serial_port: The serial port path or URL.
    :type serial_port: SerPortNameT
    :return: True if HGI80, False if not (likely evofw3), None if undetermined.
    :rtype: bool | None
    :raises exc.TransportSerialError: If the serial port cannot be found.
    """

    if serial_port[:7] == "mqtt://":
        return False  # ramses_esp

    # TODO: add tests for different serial ports, incl./excl/ by-id

    # See: https://github.com/pyserial/pyserial-asyncio/issues/46
    if "://" in serial_port:  # e.g. "rfc2217://localhost:5001"
        try:
            serial_for_url(serial_port, do_not_open=True)
        except (SerialException, ValueError) as err:
            raise exc.TransportSerialError(
                f"Unable to find {serial_port}: {err}"
            ) from err
        return None

    if not os.path.exists(serial_port):
        raise exc.TransportSerialError(f"Unable to find {serial_port}")

    # first, try the easy win...
    if "by-id" not in serial_port:
        pass
    elif "TUSB3410" in serial_port:
        return True
    elif "evofw3" in serial_port or "FT232R" in serial_port or "NANO" in serial_port:
        return False

    # otherwise, we can look at device attrs via comports()...
    try:
        loop = asyncio.get_running_loop()
        komports = await loop.run_in_executor(
            None, partial(comports, include_links=True)
        )
    except ImportError as err:
        raise exc.TransportSerialError(f"Unable to find {serial_port}: {err}") from err

    # TODO: remove get(): not monkeypatching comports() correctly for /dev/pts/...
    vid = {x.device: x.vid for x in komports}.get(serial_port)

    # this works, but we may not have all valid VIDs
    if not vid:
        pass
    elif vid == 0x10AC:  # Honeywell
        return True
    elif vid in (0x0403, 0x1B4F):  # FTDI, SparkFun
        return False

    # TODO: remove get(): not monkeypatching comports() correctly for /dev/pts/...
    product = {x.device: getattr(x, "product", None) for x in komports}.get(serial_port)

    if not product:  # is None - VM, or not member of plugdev group?
        pass
    elif "TUSB3410" in product:  # ?needed
        return True
    elif "evofw3" in product or "FT232R" in product or "NANO" in product:
        return False

    # could try sending an "!V", expect "# evofw3 0.7.1", but that needs I/O

    _LOGGER.warning(
        f"{serial_port}: the gateway type is not determinable, will assume evofw3"
        + (
            ", TIP: specify the serial port by-id (i.e. /dev/serial/by-id/usb-...)"
            if "by-id" not in serial_port
            else ""
        )
    )
    return None


def _normalise(pkt_line: str) -> str:
    """Perform any (transparent) frame-level hacks, as required at (near-)RF layer.

    Goals:
      - ensure an evofw3 provides the same output as a HGI80 (none, presently)
      - handle 'strange' packets (e.g. ``I|08:|0008``)

    :param pkt_line: The raw packet string from the hardware.
    :type pkt_line: str
    :return: The normalized packet string.
    :rtype: str
    """

    # TODO: deprecate as only for ramses_esp <0.4.0
    # ramses_esp-specific bugs, see: https://github.com/IndaloTech/ramses_esp/issues/1
    pkt_line = re.sub("\r\r", "\r", pkt_line)
    if pkt_line[:4] == " 000":
        pkt_line = pkt_line[1:]
    elif pkt_line[:2] in (I_, RQ, RP, W_):
        pkt_line = ""

    # pseudo-RAMSES-II packets (encrypted payload?)...
    if pkt_line[10:14] in (" 08:", " 31:") and pkt_line[-16:] == "* Checksum error":
        pkt_line = pkt_line[:-17] + " # Checksum error (ignored)"

    # remove any "/r/n" (leading whitespeace is a problem for commands, but not packets)
    return pkt_line.strip()


def _str(value: bytes) -> str:
    """Decode bytes to a string, ignoring non-printable characters.

    :param value: The bytes to decode.
    :type value: bytes
    :return: The decoded string.
    :rtype: str
    """

    try:
        result = "".join(
            c for c in value.decode("ascii", errors="strict") if c in printable
        )
    except UnicodeDecodeError:
        _LOGGER.warning("%s < Can't decode bytestream (ignoring)", value)
        return ""
    return result


def limit_duty_cycle(
    max_duty_cycle: float, time_window: int = DUTY_CYCLE_DURATION
) -> Callable[..., Any]:
    """Limit the Tx rate to the RF duty cycle regulations (e.g. 1% per hour).

    :param max_duty_cycle: Bandwidth available per observation window (percentage as 0.0-1.0).
    :type max_duty_cycle: float
    :param time_window: Duration of the sliding observation window in seconds, defaults to 60.
    :type time_window: int
    :return: A decorator that enforces the duty cycle limit.
    :rtype: Callable[..., Any]
    """

    TX_RATE_AVAIL: int = 38400  # bits per second (deemed)
    FILL_RATE: float = TX_RATE_AVAIL * max_duty_cycle  # bits per second
    BUCKET_CAPACITY: float = FILL_RATE * time_window

    def decorator(
        fnc: Callable[..., Awaitable[None]],
    ) -> Callable[..., Awaitable[None]]:
        # start with a full bit bucket
        bits_in_bucket: float = BUCKET_CAPACITY
        last_time_bit_added = perf_counter()

        @wraps(fnc)
        async def wrapper(
            self: PortTransport, frame: str, *args: Any, **kwargs: Any
        ) -> None:
            nonlocal bits_in_bucket
            nonlocal last_time_bit_added

            rf_frame_size = 330 + len(frame[46:]) * 10

            # top-up the bit bucket
            elapsed_time = perf_counter() - last_time_bit_added
            bits_in_bucket = min(
                bits_in_bucket + elapsed_time * FILL_RATE, BUCKET_CAPACITY
            )
            last_time_bit_added = perf_counter()

            if _DBG_DISABLE_DUTY_CYCLE_LIMIT:
                bits_in_bucket = BUCKET_CAPACITY

            # if required, wait for the bit bucket to refill (not for SETs/PUTs)
            if bits_in_bucket < rf_frame_size:
                await asyncio.sleep((rf_frame_size - bits_in_bucket) / FILL_RATE)

            # consume the bits from the bit bucket
            try:
                await fnc(self, frame, *args, **kwargs)
            finally:
                bits_in_bucket -= rf_frame_size

        @wraps(fnc)
        async def null_wrapper(
            self: PortTransport, frame: str, *args: Any, **kwargs: Any
        ) -> None:
            await fnc(self, frame, *args, **kwargs)

        if 0 < max_duty_cycle <= 1:
            return wrapper

        return null_wrapper

    return decorator


# used by @track_transmit_rate, current_transmit_rate()
_MAX_TRACKED_TRANSMITS = 99
_MAX_TRACKED_DURATION = 300


# used by @track_system_syncs, @avoid_system_syncs
_MAX_TRACKED_SYNCS = 3
_global_sync_cycles: deque[Packet] = deque(maxlen=_MAX_TRACKED_SYNCS)


# TODO: doesn't look right at all...
def avoid_system_syncs(fnc: Callable[..., Awaitable[None]]) -> Callable[..., Any]:
    """Take measures to avoid Tx when any controller is doing a sync cycle.

    :param fnc: The async function to decorate.
    :type fnc: Callable[..., Awaitable[None]]
    :return: The decorated function.
    :rtype: Callable[..., Any]
    """

    DURATION_PKT_GAP = 0.020  # 0.0200 for evohome, or 0.0127 for DTS92
    DURATION_LONG_PKT = 0.022  # time to tx I|2309|048 (or 30C9, or 000A)
    DURATION_SYNC_PKT = 0.010  # time to tx I|1F09|003

    SYNC_WAIT_LONG = (DURATION_PKT_GAP + DURATION_LONG_PKT) * 2
    SYNC_WAIT_SHORT = DURATION_SYNC_PKT
    SYNC_WINDOW_LOWER = td(seconds=SYNC_WAIT_SHORT * 0.8)  # could be * 0
    SYNC_WINDOW_UPPER = SYNC_WINDOW_LOWER + td(seconds=SYNC_WAIT_LONG * 1.2)  #

    @wraps(fnc)
    async def wrapper(*args: Any, **kwargs: Any) -> None:
        global _global_sync_cycles

        def is_imminent(p: Packet) -> bool:
            """Return True if a sync cycle is imminent."""
            return bool(
                SYNC_WINDOW_LOWER
                < (p.dtm + td(seconds=int(p.payload[2:6], 16) / 10) - dt_now())
                < SYNC_WINDOW_UPPER
            )

        start = perf_counter()  # TODO: remove

        # wait for the start of the sync cycle (I|1F09|003, Tx time ~0.009)
        while any(is_imminent(p) for p in _global_sync_cycles):
            await asyncio.sleep(SYNC_WAIT_SHORT)

        # wait for the remainder of sync cycle (I|2309/30C9) to complete
        if perf_counter() - start > SYNC_WAIT_SHORT:
            await asyncio.sleep(SYNC_WAIT_LONG)

        await fnc(*args, **kwargs)
        return None

    return wrapper


def track_system_syncs(fnc: Callable[..., None]) -> Callable[..., Any]:
    """Track/remember any new/outstanding TCS sync cycle.

    :param fnc: The function to decorate (usually a packet reader).
    :type fnc: Callable[..., None]
    :return: The decorated function.
    :rtype: Callable[..., Any]
    """

    @wraps(fnc)
    def wrapper(self: PortTransport, pkt: Packet) -> None:
        global _global_sync_cycles

        def is_pending(p: Packet) -> bool:
            """Return True if a sync cycle is still pending (ignores drift)."""
            return bool(p.dtm + td(seconds=int(p.payload[2:6], 16) / 10) > dt_now())

        if pkt.code != Code._1F09 or pkt.verb != I_ or pkt._len != 3:
            fnc(self, pkt)
            return None

        _global_sync_cycles = deque(
            p for p in _global_sync_cycles if p.src != pkt.src and is_pending(p)
        )
        _global_sync_cycles.append(pkt)  # TODO: sort

        if (
            len(_global_sync_cycles) > _MAX_TRACKED_SYNCS
        ):  # safety net for corrupted payloads
            _global_sync_cycles.popleft()

        fnc(self, pkt)

    return wrapper


# ### Abstractors #####################################################################
# ### Do the bare minimum to abstract each transport from its underlying class


class _CallbackTransportAbstractor:
    """Do the bare minimum to abstract a transport from its underlying class."""

    def __init__(
        self, loop: asyncio.AbstractEventLoop | None = None, **kwargs: Any
    ) -> None:
        """Initialize the callback transport abstractor.

        :param loop: The asyncio event loop, defaults to None.
        :type loop: asyncio.AbstractEventLoop | None, optional
        """
        self._loop = loop or asyncio.get_event_loop()
        # Consume 'kwargs' here. Do NOT pass them to object.__init__().
        super().__init__()


class _BaseTransport:
    """Base class for all transports."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)


class _FileTransportAbstractor:
    """Do the bare minimum to abstract a transport from its underlying class."""

    def __init__(
        self,
        pkt_source: dict[str, str] | str | TextIOWrapper,
        protocol: RamsesProtocolT,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialize the file transport abstractor.

        :param pkt_source: The source of packets (file path, file object, or dict).
        :type pkt_source: dict[str, str] | str | TextIOWrapper
        :param protocol: The protocol instance.
        :type protocol: RamsesProtocolT
        :param loop: The asyncio event loop, defaults to None.
        :type loop: asyncio.AbstractEventLoop | None, optional
        """
        # per().__init__(extra=extra)  # done in _BaseTransport

        self._pkt_source = pkt_source

        self._protocol = protocol
        self._loop = loop or asyncio.get_event_loop()


class _PortTransportAbstractor(serial_asyncio.SerialTransport):
    """Do the bare minimum to abstract a transport from its underlying class."""

    serial: Serial  # type: ignore[no-any-unimported]

    def __init__(  # type: ignore[no-any-unimported]
        self,
        serial_instance: Serial,
        protocol: RamsesProtocolT,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialize the port transport abstractor.

        :param serial_instance: The serial object instance.
        :type serial_instance: Serial
        :param protocol: The protocol instance.
        :type protocol: RamsesProtocolT
        :param loop: The asyncio event loop, defaults to None.
        :type loop: asyncio.AbstractEventLoop | None, optional
        """

        super().__init__(loop or asyncio.get_event_loop(), protocol, serial_instance)

        # lf._serial = serial_instance  # ._serial, not .serial

        # lf._protocol = protocol
        # lf._loop = loop or asyncio.get_event_loop()


class _MqttTransportAbstractor:
    """Do the bare minimum to abstract a transport from its underlying class."""

    def __init__(
        self,
        broker_url: str,
        protocol: RamsesProtocolT,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialize the MQTT transport abstractor.

        :param broker_url: The URL of the MQTT broker.
        :type broker_url: str
        :param protocol: The protocol instance.
        :type protocol: RamsesProtocolT
        :param loop: The asyncio event loop, defaults to None.
        :type loop: asyncio.AbstractEventLoop | None, optional
        """
        # per().__init__(extra=extra)  # done in _BaseTransport

        self._broker_url = urlparse(broker_url)

        self._protocol = protocol
        self._loop = loop or asyncio.get_event_loop()


# ### Base classes (common to all Transports) #########################################
# ### Code shared by all R/O, R/W transport types (File/dict, Serial, MQTT)


class _ReadTransport(_BaseTransport):
    """Interface for read-only transports."""

    _protocol: RamsesProtocolT = None  # type: ignore[assignment]
    _loop: asyncio.AbstractEventLoop

    _is_hgi80: bool | None = None  # NOTE: None (unknown) is as False (is_evofw3)

    #  __slots__ = ('_extra',)

    def __init__(
        self, *args: Any, extra: dict[str, Any] | None = None, **kwargs: Any
    ) -> None:
        """Initialize the read-only transport.

        :param extra: Extra info dict, defaults to None.
        :type extra: dict[str, Any] | None, optional
        """
        super().__init__(*args, loop=kwargs.pop("loop", None))

        self._extra: dict[str, Any] = {} if extra is None else extra

        self._evofw_flag = kwargs.pop(SZ_EVOFW_FLAG, None)  # gwy.config.evofw_flag
        # kwargs.pop("comms_params", None)  # FiXME: remove this

        self._closing: bool = False
        self._reading: bool = False

        self._this_pkt: Packet | None = None
        self._prev_pkt: Packet | None = None

        for key in (SZ_ACTIVE_HGI, SZ_SIGNATURE):
            self._extra.setdefault(key, None)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._protocol})"

    def _dt_now(self) -> dt:
        """Return a precise datetime, using last packet's dtm field.

        :return: The timestamp of the current packet or a default.
        :rtype: dt
        """

        try:
            return self._this_pkt.dtm  # type: ignore[union-attr]
        except AttributeError:
            return dt(1970, 1, 1, 1, 0)

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The asyncio event loop as declared by SerialTransport.

        :return: The event loop.
        :rtype: asyncio.AbstractEventLoop
        """
        return self._loop

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        """Get extra information about the transport.

        :param name: The name of the information to retrieve.
        :type name: str
        :param default: Default value if name is not found, defaults to None.
        :type default: Any, optional
        :return: The value associated with name.
        :rtype: Any
        """
        if name == SZ_IS_EVOFW3:
            return not self._is_hgi80
        return self._extra.get(name, default)

    def is_closing(self) -> bool:
        """Return True if the transport is closing or has closed.

        :return: Closing state.
        :rtype: bool
        """
        return self._closing

    def _close(self, exc: exc.RamsesException | None = None) -> None:
        """Inform the protocol that this transport has closed.

        :param exc: The exception that caused the closure, if any.
        :type exc: exc.RamsesException | None, optional
        """

        if self._closing:
            return
        self._closing = True

        self.loop.call_soon_threadsafe(
            functools.partial(self._protocol.connection_lost, exc)  # type: ignore[arg-type]
        )

    def close(self) -> None:
        """Close the transport gracefully."""
        self._close()

    def is_reading(self) -> bool:
        """Return True if the transport is receiving.

        :return: Reading state.
        :rtype: bool
        """
        return self._reading

    def pause_reading(self) -> None:
        """Pause the receiving end (no data to protocol.pkt_received())."""
        self._reading = False

    def resume_reading(self) -> None:
        """Resume the receiving end."""
        self._reading = True

    def _make_connection(self, gwy_id: DeviceIdT | None) -> None:
        """Register the connection with the protocol.

        :param gwy_id: The ID of the gateway device, if known.
        :type gwy_id: DeviceIdT | None
        """
        self._extra[SZ_ACTIVE_HGI] = gwy_id  # or HGI_DEV_ADDR.id

        self.loop.call_soon_threadsafe(  # shouldn't call this until we have HGI-ID
            functools.partial(self._protocol.connection_made, self, ramses=True)  # type: ignore[arg-type]
        )

    # NOTE: all transport should call this method when they receive data
    def _frame_read(self, dtm_str: str, frame: str) -> None:
        """Make a Packet from the Frame and process it (called by each specific Tx).

        :param dtm_str: The timestamp string of the frame.
        :type dtm_str: str
        :param frame: The raw frame string.
        :type frame: str
        """

        if not frame.strip():
            return

        try:
            pkt = Packet.from_file(dtm_str, frame)  # is OK for when src is dict

        except ValueError as err:  # VE from dt.fromisoformat() or falsey packet
            _LOGGER.debug("%s < PacketInvalid(%s)", frame, err)
            return

        except exc.PacketInvalid as err:  # VE from dt.fromisoformat()
            _LOGGER.warning("%s < PacketInvalid(%s)", frame, err)
            return

        self._pkt_read(pkt)

    # NOTE: all protocol callbacks should be invoked from here
    def _pkt_read(self, pkt: Packet) -> None:
        """Pass any valid Packets to the protocol's callback (_prev_pkt, _this_pkt).

        :param pkt: The parsed packet.
        :type pkt: Packet
        :raises exc.TransportError: If called while closing.
        """

        self._this_pkt, self._prev_pkt = pkt, self._this_pkt

        # if self._reading is False:  # raise, or warn & return?
        #     raise exc.TransportError("Reading has been paused")
        if self._closing is True:  # raise, or warn & return?
            raise exc.TransportError("Transport is closing or has closed")

        # TODO: can we switch to call_soon now that QoS has been refactored?
        # NOTE: No need to use call_soon() here, and they may break Qos/Callbacks
        # NOTE: Thus, excepts need checking
        try:  # below could be a call_soon?
            self.loop.call_soon_threadsafe(self._protocol.pkt_received, pkt)
        except AssertionError as err:  # protect from upper layers
            _LOGGER.exception("%s < exception from msg layer: %s", pkt, err)
        except exc.ProtocolError as err:  # protect from upper layers
            _LOGGER.error("%s < exception from msg layer: %s", pkt, err)

    async def write_frame(self, frame: str, disable_tx_limits: bool = False) -> None:
        """Transmit a frame via the underlying handler (e.g. serial port, MQTT).

        :param frame: The frame to write.
        :type frame: str
        :param disable_tx_limits: Whether to bypass duty cycle limits, defaults to False.
        :type disable_tx_limits: bool, optional
        :raises exc.TransportSerialError: Because this transport is read-only.
        """
        raise exc.TransportSerialError("This transport is read only")


class _FullTransport(_ReadTransport):  # asyncio.Transport
    """Interface representing a bidirectional transport."""

    def __init__(
        self, *args: Any, disable_sending: bool = False, **kwargs: Any
    ) -> None:
        """Initialize the full transport.

        :param disable_sending: Whether to disable sending capabilities, defaults to False.
        :type disable_sending: bool, optional
        """
        super().__init__(*args, **kwargs)

        self._disable_sending = disable_sending
        self._transmit_times: deque[dt] = deque(maxlen=_MAX_TRACKED_TRANSMITS)

    def _dt_now(self) -> dt:
        """Get a precise datetime, using the current dtm.

        :return: Current datetime.
        :rtype: dt
        """
        # _LOGGER.error("Full._dt_now()")

        return dt_now()

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        """Get extra info, including transmit rate calculations.

        :param name: Name of info.
        :type name: str
        :param default: Default value.
        :type default: Any, optional
        :return: The requested info.
        :rtype: Any
        """
        if name == "tx_rate":
            return self._report_transmit_rate()
        return super().get_extra_info(name, default=default)

    def _report_transmit_rate(self) -> float:
        """Return the transmit rate in transmits per minute.

        :return: Transmits per minute.
        :rtype: float
        """

        dt_now = dt.now()
        dtm = dt_now - td(seconds=_MAX_TRACKED_DURATION)
        transmit_times = tuple(t for t in self._transmit_times if t > dtm)

        if len(transmit_times) <= 1:
            return len(transmit_times)

        duration: float = (transmit_times[-1] - transmit_times[0]) / td(seconds=1)
        return int(len(transmit_times) / duration * 6000) / 100

    def _track_transmit_rate(self) -> None:
        """Track the Tx rate as period of seconds per x transmits."""

        # period: float = (transmit_times[-1] - transmit_times[0]) / td(seconds=1)
        # num_tx: int   = len(transmit_times)

        self._transmit_times.append(dt.now())

        _LOGGER.debug(f"Current Tx rate: {self._report_transmit_rate():.2f} pkts/min")

    # NOTE: Protocols call write_frame(), not write()
    def write(self, data: bytes) -> None:
        """Write the data to the underlying handler.

        :param data: The data to write.
        :type data: bytes
        :raises exc.TransportError: Always raises, use write_frame instead.
        """
        # _LOGGER.error("Full.write(%s)", data)

        raise exc.TransportError("write() not implemented, use write_frame() instead")

    async def write_frame(self, frame: str, disable_tx_limits: bool = False) -> None:
        """Transmit a frame via the underlying handler (e.g. serial port, MQTT).

        Protocols call Transport.write_frame(), not Transport.write().

        :param frame: The frame to transmit.
        :type frame: str
        :param disable_tx_limits: Whether to disable duty cycle limits, defaults to False.
        :type disable_tx_limits: bool, optional
        :raises exc.TransportError: If sending is disabled or transport is closed.
        """

        if self._disable_sending is True:
            raise exc.TransportError("Sending has been disabled")
        if self._closing is True:
            raise exc.TransportError("Transport is closing or has closed")

        self._track_transmit_rate()

        await self._write_frame(frame)

    async def _write_frame(self, frame: str) -> None:
        """Write some data bytes to the underlying transport.

        :param frame: The frame to write.
        :type frame: str
        :raises NotImplementedError: Abstract method.
        """
        # _LOGGER.error("Full._write_frame(%s)", frame)

        raise NotImplementedError("_write_frame() not implemented here")


_RegexRuleT: TypeAlias = dict[str, str]


class _RegHackMixin:
    """Mixin to apply regex rules to inbound and outbound frames."""

    def __init__(
        self, *args: Any, use_regex: dict[str, _RegexRuleT] | None = None, **kwargs: Any
    ) -> None:
        """Initialize the regex mixin.

        :param use_regex: Dictionary containing inbound/outbound regex rules.
        :type use_regex: dict[str, _RegexRuleT] | None, optional
        """
        super().__init__(*args, **kwargs)

        use_regex = use_regex or {}

        self._inbound_rule: _RegexRuleT = use_regex.get(SZ_INBOUND, {})
        self._outbound_rule: _RegexRuleT = use_regex.get(SZ_OUTBOUND, {})

    @staticmethod
    def _regex_hack(pkt_line: str, regex_rules: _RegexRuleT) -> str:
        """Apply regex rules to a packet line.

        :param pkt_line: The packet line to process.
        :type pkt_line: str
        :param regex_rules: The rules to apply.
        :type regex_rules: _RegexRuleT
        :return: The modified packet line.
        :rtype: str
        """
        if not regex_rules:
            return pkt_line

        result = pkt_line
        for k, v in regex_rules.items():
            try:
                result = re.sub(k, v, result)
            except re.error as err:
                _LOGGER.warning(f"{pkt_line} < issue with regex ({k}, {v}): {err}")

        if result != pkt_line and not _DBG_DISABLE_REGEX_WARNINGS:
            _LOGGER.warning(f"{pkt_line} < Changed by use_regex to: {result}")
        return result

    def _frame_read(self, dtm_str: str, frame: str) -> None:
        super()._frame_read(dtm_str, self._regex_hack(frame, self._inbound_rule))  # type: ignore[misc]

    async def write_frame(self, frame: str, disable_tx_limits: bool = False) -> None:
        await super().write_frame(self._regex_hack(frame, self._outbound_rule))  # type: ignore[misc]


# ### Transports ######################################################################
# ### Implement the transports for File/dict (R/O), Serial, MQTT


class FileTransport(_ReadTransport, _FileTransportAbstractor):
    """Receive packets from a read-only source such as packet log or a dict."""

    def __init__(self, *args: Any, disable_sending: bool = True, **kwargs: Any) -> None:
        """Initialize the file transport.

        :param disable_sending: Must be True for FileTransport.
        :type disable_sending: bool
        :raises exc.TransportSourceInvalid: If disable_sending is False.
        """
        super().__init__(*args, **kwargs)

        if bool(disable_sending) is False:
            raise exc.TransportSourceInvalid("This Transport cannot send packets")

        self._evt_reading = asyncio.Event()

        self._extra[SZ_READER_TASK] = self._reader_task = self._loop.create_task(
            self._start_reader(), name="FileTransport._start_reader()"
        )

        self._make_connection(None)

    async def _start_reader(self) -> None:
        """Start the reader task."""
        self._reading = True
        self._evt_reading.set()  # Start in reading state

        try:
            await self._producer_loop()
        except Exception as err:
            self.loop.call_soon_threadsafe(
                functools.partial(self._protocol.connection_lost, err)  # type: ignore[arg-type]
            )
        else:
            self.loop.call_soon_threadsafe(
                functools.partial(self._protocol.connection_lost, None)
            )

    def pause_reading(self) -> None:
        """Pause the receiving end (no data to protocol.pkt_received())."""
        self._reading = False
        self._evt_reading.clear()  # Puts the loop to sleep efficiently

    def resume_reading(self) -> None:
        """Resume the receiving end."""
        self._reading = True
        self._evt_reading.set()  # Wakes the loop immediately

    async def _producer_loop(self) -> None:
        """Loop through the packet source for Frames and process them."""
        # NOTE: fileinput interaction remains synchronous-blocking for simplicity,
        # but the PAUSE mechanism is now async-non-blocking.

        if isinstance(self._pkt_source, dict):
            for dtm_str, pkt_line in self._pkt_source.items():  # assume dtm_str is OK
                await self._process_line(dtm_str, pkt_line)

        elif isinstance(self._pkt_source, str):  # file_name, used in client parse
            # open file file_name before reading
            try:
                with fileinput.input(files=self._pkt_source, encoding="utf-8") as file:
                    for dtm_pkt_line in file:  # self._pkt_source:
                        await self._process_line_from_raw(dtm_pkt_line)
            except FileNotFoundError as err:
                _LOGGER.warning(f"Correct the packet file name; {err}")

        elif isinstance(self._pkt_source, TextIOWrapper):  # used by client monitor
            for dtm_pkt_line in self._pkt_source:  # should check dtm_str is OK
                await self._process_line_from_raw(dtm_pkt_line)

        else:
            raise exc.TransportSourceInvalid(
                f"Packet source is not dict, TextIOWrapper or str: {self._pkt_source:!r}"
            )

    async def _process_line_from_raw(self, line: str) -> None:
        """Helper to process raw lines."""
        # there may be blank lines in annotated log files
        if (line := line.strip()) and line[:1] != "#":
            await self._process_line(line[:26], line[27:])
            # this is where the parsing magic happens!

    async def _process_line(self, dtm_str: str, frame: str) -> None:
        """Push frame to protocol in a thread-safe way."""
        # Efficient wait - 0% CPU usage while paused
        await self._evt_reading.wait()

        self._frame_read(dtm_str, frame)

        # Yield control to the event loop to prevent starvation during large file reads
        await asyncio.sleep(0)

    def _close(self, exc: exc.RamsesException | None = None) -> None:
        """Close the transport (cancel any outstanding tasks).

        :param exc: The exception causing closure.
        :type exc: exc.RamsesException | None, optional
        """

        super()._close(exc)

        if self._reader_task:
            self._reader_task.cancel()


class PortTransport(_RegHackMixin, _FullTransport, _PortTransportAbstractor):  # type: ignore[misc]
    """Send/receive packets async to/from evofw3/HGI80 via a serial port.

    See: https://github.com/ghoti57/evofw3
    """

    _init_fut: asyncio.Future[Packet | None]
    _init_task: asyncio.Task[None]

    _recv_buffer: bytes = b""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the port transport."""
        super().__init__(*args, **kwargs)

        self._leaker_sem = asyncio.BoundedSemaphore()
        self._leaker_task = self._loop.create_task(
            self._leak_sem(), name="PortTransport._leak_sem()"
        )

        self._loop.create_task(
            self._create_connection(), name="PortTransport._create_connection()"
        )

    async def _create_connection(self) -> None:
        """Invoke the Protocols's connection_made() callback after HGI80 discovery."""

        # HGI80s (and also VMs) take longer to send signature packets as they have long
        # initialisation times, so we must wait until they send OK

        # signature also serves to discover the HGI's device_id (& for pkt log, if any)

        self._is_hgi80 = await is_hgi80(self.serial.name)

        async def connect_sans_signature() -> None:
            """Call connection_made() without sending/waiting for a signature."""

            self._init_fut.set_result(None)
            self._make_connection(gwy_id=None)

        async def connect_with_signature() -> None:
            """Poll port with signatures, call connection_made() after first echo."""

            # TODO: send a 2nd signature, but with addr0 set to learned GWY address
            # TODO: a HGI80 will silently drop this cmd, so an echo would tell us
            # TODO: that the GWY is evofw3-compatible

            sig = Command._puzzle()
            self._extra[SZ_SIGNATURE] = sig.payload

            num_sends = 0
            while num_sends < _SIGNATURE_MAX_TRYS:
                num_sends += 1

                await self._write_frame(str(sig))
                await asyncio.sleep(_SIGNATURE_GAP_SECS)

                if self._init_fut.done():
                    pkt = self._init_fut.result()
                    self._make_connection(gwy_id=pkt.src.id if pkt else None)
                    return

            if not self._init_fut.done():
                self._init_fut.set_result(None)

            self._make_connection(gwy_id=None)
            return

        self._init_fut = asyncio.Future()
        if self._disable_sending:
            self._init_task = self._loop.create_task(
                connect_sans_signature(), name="PortTransport.connect_sans_signature()"
            )
        else:
            self._init_task = self._loop.create_task(
                connect_with_signature(), name="PortTransport.connect_with_signature()"
            )

        try:  # wait to get (1st) signature echo from evofw3/HGI80, if any
            await asyncio.wait_for(self._init_fut, timeout=_SIGNATURE_MAX_SECS)
        except TimeoutError as err:
            raise exc.TransportSerialError(
                f"Failed to initialise Transport within {_SIGNATURE_MAX_SECS} secs"
            ) from err

    async def _leak_sem(self) -> None:
        """Used to enforce a minimum time between calls to self.write()."""
        while True:
            await asyncio.sleep(MIN_INTER_WRITE_GAP)
            with contextlib.suppress(ValueError):
                self._leaker_sem.release()

    # NOTE: self._frame_read() invoked from here
    def _read_ready(self) -> None:
        """Make Frames from the read data and process them."""

        def bytes_read(data: bytes) -> Iterable[tuple[dt, bytes]]:
            self._recv_buffer += data
            if b"\r\n" in self._recv_buffer:
                lines = self._recv_buffer.split(b"\r\n")
                self._recv_buffer = lines[-1]
                for line in lines[:-1]:
                    yield self._dt_now(), line + b"\r\n"

        try:
            data: bytes = self.serial.read(self._max_read_size)
        except SerialException as err:
            if not self._closing:
                self._close(exc=err)  # have to use _close() to pass in exception
            return

        if not data:
            return

        for dtm, raw_line in bytes_read(data):
            if _DBG_FORCE_FRAME_LOGGING:
                _LOGGER.warning("Rx: %s", raw_line)
            elif _LOGGER.getEffectiveLevel() == logging.INFO:  # log for INFO not DEBUG
                _LOGGER.info("Rx: %s", raw_line)

            self._frame_read(
                dtm.isoformat(timespec="milliseconds"), _normalise(_str(raw_line))
            )

    @track_system_syncs
    def _pkt_read(self, pkt: Packet) -> None:
        # NOTE: a signature can override an existing active gateway
        if (
            not self._init_fut.done()
            and pkt.code == Code._PUZZ
            and pkt.payload == self._extra[SZ_SIGNATURE]
        ):
            self._extra[SZ_ACTIVE_HGI] = pkt.src.id  # , by_signature=True)
            self._init_fut.set_result(pkt)

        super()._pkt_read(pkt)

    @limit_duty_cycle(MAX_DUTY_CYCLE_RATE)
    @avoid_system_syncs
    async def write_frame(self, frame: str, disable_tx_limits: bool = False) -> None:
        """Transmit a frame via the underlying handler (e.g. serial port, MQTT).

        Protocols call Transport.write_frame(), not Transport.write().

        :param frame: The frame to transmit.
        :type frame: str
        :param disable_tx_limits: Whether to disable duty cycle limits, defaults to False.
        :type disable_tx_limits: bool, optional
        """

        await self._leaker_sem.acquire()  # MIN_INTER_WRITE_GAP
        await super().write_frame(frame)

    # NOTE: The order should be: minimum gap between writes, duty cycle limits, and
    # then the code that avoids the controller sync cycles

    async def _write_frame(self, frame: str) -> None:
        """Write some data bytes to the underlying transport.

        :param frame: The frame to write.
        :type frame: str
        """

        data = bytes(frame, "ascii") + b"\r\n"

        log_msg = f"Serial transport transmitting frame: {frame}"
        if _DBG_FORCE_FRAME_LOGGING:
            _LOGGER.warning(log_msg)
        elif _LOGGER.getEffectiveLevel() > logging.DEBUG:
            _LOGGER.info(log_msg)
        else:
            _LOGGER.debug(log_msg)

        try:
            self._write(data)
        except SerialException as err:
            self._abort(err)
            return

    def _write(self, data: bytes) -> None:
        """Perform the actual write to the serial port.

        :param data: The bytes to write.
        :type data: bytes
        """
        self.serial.write(data)

    def _abort(self, exc: ExceptionT) -> None:  # type: ignore[override]  # used by serial_asyncio.SerialTransport
        """Abort the transport.

        :param exc: The exception causing the abort.
        :type exc: ExceptionT
        """
        super()._abort(exc)  # type: ignore[arg-type]

        if self._init_task:
            self._init_task.cancel()
        if self._leaker_task:
            self._leaker_task.cancel()

    def _close(self, exc: exc.RamsesException | None = None) -> None:  # type: ignore[override]
        """Close the transport (cancel any outstanding tasks).

        :param exc: The exception causing closure.
        :type exc: exc.RamsesException | None, optional
        """

        super()._close(exc)

        if self._init_task:
            self._init_task.cancel()

        if self._leaker_task:
            self._leaker_task.cancel()


class MqttTransport(_FullTransport, _MqttTransportAbstractor):
    """Send/receive packets to/from ramses_esp via MQTT.
    For full RX logging, turn on debug logging.

    See: https://github.com/IndaloTech/ramses_esp
    """

    # used in .write_frame() to rate-limit the number of writes
    _MAX_TOKENS: Final[int] = MAX_TRANSMIT_RATE_TOKENS
    _TIME_WINDOW: Final[int] = DUTY_CYCLE_DURATION
    _TOKEN_RATE: Final[float] = _MAX_TOKENS / _TIME_WINDOW

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # _LOGGER.error("__init__(%s, %s)", args, kwargs)

        super().__init__(*args, **kwargs)

        self._username = unquote(self._broker_url.username or "")
        self._password = unquote(self._broker_url.password or "")

        self._topic_base = validate_topic_path(self._broker_url.path)
        self._topic_pub = ""
        self._topic_sub = ""
        # Track if we've subscribed to a wildcard data topic (e.g. ".../+/rx")
        self._data_wildcard_topic = ""

        self._mqtt_qos = int(parse_qs(self._broker_url.query).get("qos", ["0"])[0])

        self._connected = False
        self._connecting = False
        self._connection_established = False  # Track if initial connection was made
        self._extra[SZ_IS_EVOFW3] = True

        # Reconnection settings
        self._reconnect_interval = 5.0  # seconds
        self._max_reconnect_interval = 300.0  # 5 minutes max
        self._reconnect_backoff = 1.5
        self._current_reconnect_interval = self._reconnect_interval
        self._reconnect_task: asyncio.Task[None] | None = None

        # used in .write_frame() to rate-limit the number of writes
        self._timestamp = perf_counter()
        self._max_tokens: float = self._MAX_TOKENS * 2  # allow for the initial burst
        self._num_tokens: float = self._MAX_TOKENS * 2

        # set log MQTT flag
        self._log_all = kwargs.pop("log_all", False)

        # instantiate a paho mqtt client
        self.client = mqtt.Client(
            protocol=mqtt.MQTTv5, callback_api_version=CallbackAPIVersion.VERSION2
        )
        self.client.on_connect = self._on_connect
        self.client.on_connect_fail = self._on_connect_fail
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.username_pw_set(self._username, self._password)
        # connect to the mqtt server
        self._attempt_connection()

    def _attempt_connection(self) -> None:
        """Attempt to connect to the MQTT broker."""
        if self._connecting or self._connected:
            return

        self._connecting = True
        try:
            self.client.connect_async(
                str(self._broker_url.hostname or "localhost"),
                self._broker_url.port or 1883,
                60,
            )
            self.client.loop_start()
        except Exception as err:
            _LOGGER.error(f"Failed to initiate MQTT connection: {err}")
            self._connecting = False
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Schedule a reconnection attempt with exponential backoff."""
        if self._closing or self._reconnect_task:
            return

        _LOGGER.info(
            f"Scheduling MQTT reconnect in {self._current_reconnect_interval} seconds"
        )
        self._reconnect_task = self._loop.create_task(
            self._reconnect_after_delay(), name="MqttTransport._reconnect_after_delay()"
        )

    async def _reconnect_after_delay(self) -> None:
        """Wait and then attempt to reconnect."""
        try:
            await asyncio.sleep(self._current_reconnect_interval)

            # Increase backoff for next time
            self._current_reconnect_interval = min(
                self._current_reconnect_interval * self._reconnect_backoff,
                self._max_reconnect_interval,
            )

            _LOGGER.info("Attempting MQTT reconnection...")
            self._attempt_connection()
        except asyncio.CancelledError:
            pass
        finally:
            self._reconnect_task = None

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: dict[str, Any],
        reason_code: Any,
        properties: Any | None,
    ) -> None:
        """Handle MQTT connection success.

        :param client: The MQTT client.
        :type client: mqtt.Client
        :param userdata: User data.
        :type userdata: Any
        :param flags: Connection flags.
        :type flags: dict[str, Any]
        :param reason_code: Connection reason code.
        :type reason_code: Any
        :param properties: Connection properties.
        :type properties: Any | None
        """
        # _LOGGER.error("Mqtt._on_connect(%s, %s, %s, %s)", client, userdata, flags, reason_code.getName())

        self._connecting = False

        if reason_code.is_failure:
            _LOGGER.error(f"MQTT connection failed: {reason_code.getName()}")
            self._schedule_reconnect()
            return

        _LOGGER.info(f"MQTT connected: {reason_code.getName()}")

        # Reset reconnect interval on successful connection
        self._current_reconnect_interval = self._reconnect_interval

        # Cancel any pending reconnect task
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None

        # Subscribe to base topic to see 'online' messages
        self.client.subscribe(self._topic_base)  # hope to see 'online' message

        # Also subscribe to data topics with wildcard for reliability, but only
        # until a specific device topic is known. Once _topic_sub is set, avoid
        # overlapping subscriptions that would duplicate messages.
        if self._topic_base.endswith("/+") and not (
            hasattr(self, "_topic_sub") and self._topic_sub
        ):
            data_wildcard = self._topic_base.replace("/+", "/+/rx")
            self.client.subscribe(data_wildcard, qos=self._mqtt_qos)
            self._data_wildcard_topic = data_wildcard
            _LOGGER.debug(f"Subscribed to data wildcard: {data_wildcard}")

        # If we already have specific topics, re-subscribe to them
        if hasattr(self, "_topic_sub") and self._topic_sub:
            self.client.subscribe(self._topic_sub, qos=self._mqtt_qos)
            _LOGGER.debug(f"Re-subscribed to specific topic: {self._topic_sub}")
            # If we had a wildcard subscription, drop it to prevent duplicates
            if getattr(self, "_data_wildcard_topic", ""):
                try:
                    self.client.unsubscribe(self._data_wildcard_topic)
                    _LOGGER.debug(
                        f"Unsubscribed data wildcard after specific subscribe: {self._data_wildcard_topic}"
                    )
                finally:
                    self._data_wildcard_topic = ""

    def _on_connect_fail(
        self,
        client: mqtt.Client,
        userdata: Any,
    ) -> None:
        """Handle MQTT connection failure.

        :param client: The MQTT client.
        :type client: mqtt.Client
        :param userdata: User data.
        :type userdata: Any
        """
        _LOGGER.error("MQTT connection failed")

        self._connecting = False
        self._connected = False

        if not self._closing:
            self._schedule_reconnect()

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Handle MQTT disconnection.

        :param client: The MQTT client.
        :type client: mqtt.Client
        :param userdata: User data.
        :type userdata: Any
        """
        # Handle different paho-mqtt callback signatures
        reason_code = args[0] if len(args) >= 1 else None

        reason_name = (
            reason_code.getName()
            if reason_code is not None and hasattr(reason_code, "getName")
            else str(reason_code)
        )
        _LOGGER.warning(f"MQTT disconnected: {reason_name}")

        was_connected = self._connected
        self._connected = False

        # If we were previously connected and had established communication,
        # notify that the device is now offline
        if was_connected and hasattr(self, "_topic_sub") and self._topic_sub:
            device_topic = self._topic_sub[:-3]  # Remove "/rx" suffix
            _LOGGER.warning(f"{self}: the MQTT device is offline: {device_topic}")

            # Pause writing since device is offline
            if hasattr(self, "_protocol"):
                self._protocol.pause_writing()

        # Only attempt reconnection if we didn't deliberately disconnect

        if not self._closing:
            # Schedule reconnection for any disconnect (unexpected or failure)
            self._schedule_reconnect()

    def _create_connection(self, msg: mqtt.MQTTMessage) -> None:
        """Invoke the Protocols's connection_made() callback MQTT is established.

        :param msg: The online message triggering the connection.
        :type msg: mqtt.MQTTMessage
        """
        # _LOGGER.error("Mqtt._create_connection(%s)", msg)

        assert msg.payload == b"online", "Coding error"

        if self._connected:
            _LOGGER.info("MQTT device came back online - resuming writing")
            self._loop.call_soon_threadsafe(self._protocol.resume_writing)
            return

        _LOGGER.info("MQTT device is online - establishing connection")
        self._connected = True

        self._extra[SZ_ACTIVE_HGI] = msg.topic[-9:]

        self._topic_pub = msg.topic + "/tx"
        self._topic_sub = msg.topic + "/rx"

        self.client.subscribe(self._topic_sub, qos=self._mqtt_qos)

        # If we previously subscribed to a wildcard data topic, unsubscribe now
        # to avoid duplicate delivery (wildcard and specific both matching)
        if getattr(self, "_data_wildcard_topic", ""):
            try:
                self.client.unsubscribe(self._data_wildcard_topic)
                _LOGGER.debug(
                    f"Unsubscribed data wildcard after device online: {self._data_wildcard_topic}"
                )
            finally:
                self._data_wildcard_topic = ""

        # Only call connection_made on first connection, not reconnections
        if not self._connection_established:
            self._connection_established = True
            self._make_connection(gwy_id=msg.topic[-9:])  # type: ignore[arg-type]
        else:
            _LOGGER.info("MQTT reconnected - protocol connection already established")

    # NOTE: self._frame_read() invoked from here
    def _on_message(
        self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage
    ) -> None:
        """Make a Frame from the MQTT message and process it.

        :param client: The MQTT client.
        :type client: mqtt.Client
        :param userdata: User data.
        :type userdata: Any
        :param msg: The received message.
        :type msg: mqtt.MQTTMessage
        """
        # _LOGGER.error(
        #     "Mqtt._on_message(%s, %s, %s)",
        #     client,
        #     userdata,
        #     (msg.timestamp, msg.topic, msg.payload),
        # )

        if _DBG_FORCE_FRAME_LOGGING:
            _LOGGER.warning("Rx: %s", msg.payload)
        elif self._log_all and _LOGGER.getEffectiveLevel() == logging.INFO:
            # log for INFO not DEBUG
            _LOGGER.info("mq Rx: %s", msg.payload)  # TODO remove mq marker?

        if msg.topic[-3:] != "/rx":  # then, e.g. 'RAMSES/GATEWAY/18:017804'
            if msg.payload == b"offline":
                # Check if this offline message is for our current device
                if (
                    hasattr(self, "_topic_sub")
                    and self._topic_sub
                    and msg.topic == self._topic_sub[:-3]
                ) or not hasattr(self, "_topic_sub"):
                    _LOGGER.warning(
                        f"{self}: the ESP device is offline (via LWT): {msg.topic}"
                    )
                    # Don't set _connected = False here - that's for MQTT connection, not ESP device
                    if hasattr(self, "_protocol"):
                        self._protocol.pause_writing()

            # BUG: using create task (self._loop.ct() & asyncio.ct()) causes the
            # BUG: event look to close early
            elif msg.payload == b"online":
                _LOGGER.info(
                    f"{self}: the ESP device is online (via status): {msg.topic}"
                )
                self._create_connection(msg)

            return

        # Handle data messages - if we don't have connection established yet but get data,
        # we can infer the gateway from the topic
        if not self._connection_established and msg.topic.endswith("/rx"):
            # Extract gateway ID from topic like "RAMSES/GATEWAY/18:123456/rx"
            topic_parts = msg.topic.split("/")
            if len(topic_parts) >= 3 and topic_parts[-2] not in ("+", "*"):
                gateway_id = topic_parts[-2]  # Should be something like "18:123456"
                _LOGGER.info(
                    f"Inferring gateway connection from data topic: {gateway_id}"
                )

                # Set up topics and connection
                self._topic_pub = f"{'/'.join(topic_parts[:-1])}/tx"
                self._topic_sub = msg.topic
                self._extra[SZ_ACTIVE_HGI] = gateway_id

                # Mark as connected and establish protocol connection
                self._connected = True
                self._connection_established = True
                self._make_connection(gwy_id=gateway_id)  # type: ignore[arg-type]

                # Ensure we subscribe specifically to the device topic and drop the
                # wildcard subscription to prevent duplicates
                try:
                    self.client.subscribe(self._topic_sub, qos=self._mqtt_qos)
                except Exception as err:  # pragma: no cover - defensive
                    _LOGGER.debug(f"Error subscribing specific topic: {err}")
                if getattr(self, "_data_wildcard_topic", ""):
                    try:
                        self.client.unsubscribe(self._data_wildcard_topic)
                        _LOGGER.debug(
                            f"Unsubscribed data wildcard after inferring device: {self._data_wildcard_topic}"
                        )
                    finally:
                        self._data_wildcard_topic = ""

        try:
            payload = json.loads(msg.payload)
        except json.JSONDecodeError:
            _LOGGER.warning("%s < Can't decode JSON (ignoring)", msg.payload)
            return

        # HACK: hotfix for converting RAMSES_ESP dtm into local/naive dtm
        dtm = dt.fromisoformat(payload["ts"])
        if dtm.tzinfo is not None:
            dtm = dtm.astimezone().replace(tzinfo=None)
        if dtm < dt.now() - td(days=90):
            _LOGGER.warning(
                f"{self}: Have you configured the SNTP settings on the ESP?"
            )
        # FIXME: convert all dt early, and convert to aware, i.e. dt.now().astimezone()

        try:
            self._frame_read(dtm.isoformat(), _normalise(payload["msg"]))
        except exc.TransportError:
            # If the transport is closing, we expect this error and can safely ignore it
            # prevents "Uncaught thread exception" in paho.mqtt client
            if not self._closing:
                raise

    async def write_frame(self, frame: str, disable_tx_limits: bool = False) -> None:
        """Transmit a frame via the underlying handler (e.g. serial port, MQTT).

        Writes are rate-limited to _MAX_TOKENS Packets over the last _TIME_WINDOW
        seconds, except when disable_tx_limits is True (for e.g. user commands).

        Protocols call Transport.write_frame(), not Transport.write().

        :param frame: The frame to transmit.
        :type frame: str
        :param disable_tx_limits: Whether to disable rate limiting, defaults to False.
        :type disable_tx_limits: bool, optional
        """

        # Check if we're connected before attempting to write
        if not self._connected:
            _LOGGER.debug(f"{self}: Dropping write - MQTT not connected")
            return

        # top-up the token bucket
        timestamp = perf_counter()
        elapsed, self._timestamp = timestamp - self._timestamp, timestamp
        self._num_tokens = min(
            self._num_tokens + elapsed * self._TOKEN_RATE, self._max_tokens
        )

        # if would have to sleep >= 1 second, dump the write instead
        if self._num_tokens < 1.0 - self._TOKEN_RATE and not disable_tx_limits:
            _LOGGER.warning(f"{self}: Discarding write (tokens={self._num_tokens:.2f})")
            return

        self._num_tokens -= 1.0
        if self._max_tokens > self._MAX_TOKENS:  # what is the new max number of tokens
            self._max_tokens = min(self._max_tokens, self._num_tokens)
            self._max_tokens = max(self._max_tokens, self._MAX_TOKENS)

        # if in token debt, sleep until the debt is paid
        if self._num_tokens < 0.0 and not disable_tx_limits:
            delay = (0 - self._num_tokens) / self._TOKEN_RATE
            _LOGGER.debug(f"{self}: Sleeping (seconds={delay})")
            await asyncio.sleep(delay)

        await super().write_frame(frame)

    async def _write_frame(self, frame: str) -> None:
        """Write some data bytes to the underlying transport.

        :param frame: The frame to write.
        :type frame: str
        """
        # _LOGGER.error("Mqtt._write_frame(%s)", frame)

        data = json.dumps({"msg": frame})

        if _DBG_FORCE_FRAME_LOGGING:
            _LOGGER.warning("Tx: %s", data)
        elif _LOGGER.getEffectiveLevel() == logging.INFO:  # log for INFO not DEBUG
            _LOGGER.info("Tx: %s", data)

        try:
            self._publish(data)
        except MQTTException as err:
            _LOGGER.error(f"MQTT publish failed: {err}")
            # Don't close the transport, just log the error and continue
            # The broker might come back online
            return

    def _publish(self, payload: str) -> None:
        """Publish the payload to the MQTT broker.

        :param payload: The data payload to publish.
        :type payload: str
        """
        # _LOGGER.error("Mqtt._publish(%s)", message)

        if not self._connected:
            _LOGGER.debug("Cannot publish - MQTT not connected")
            return

        info: mqtt.MQTTMessageInfo = self.client.publish(
            self._topic_pub, payload=payload, qos=self._mqtt_qos
        )

        if not info:
            _LOGGER.warning("MQTT publish returned no info")
        elif info.rc != mqtt.MQTT_ERR_SUCCESS:
            _LOGGER.warning(f"MQTT publish failed with code: {info.rc}")
            # Check if this indicates a connection issue
            if info.rc in (mqtt.MQTT_ERR_NO_CONN, mqtt.MQTT_ERR_CONN_LOST):
                self._connected = False
                if not self._closing:
                    self._schedule_reconnect()

    def _close(self, exc: exc.RamsesException | None = None) -> None:
        """Close the transport (disconnect from the broker and stop its poller).

        :param exc: The exception causing closure.
        :type exc: exc.RamsesException | None, optional
        """
        # _LOGGER.error("Mqtt._close(%s)", exc)

        super()._close(exc)

        # Cancel any pending reconnection attempts
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None

        if not self._connected:
            return
        self._connected = False

        try:
            self.client.unsubscribe(self._topic_sub)
            self.client.disconnect()
            self.client.loop_stop()
        except Exception as err:
            _LOGGER.debug(f"Error during MQTT cleanup: {err}")


class CallbackTransport(_FullTransport, _CallbackTransportAbstractor):
    """A virtual transport that delegates I/O to external callbacks (Inversion of Control).

    This transport allows ramses_rf to be used with external connection managers
    (like Home Assistant's MQTT integration) without direct dependencies.
    """

    def __init__(
        self,
        protocol: RamsesProtocolT,
        io_writer: Callable[[str], Awaitable[None]],
        disable_sending: bool = False,
        **kwargs: Any,
    ) -> None:
        """Initialize the callback transport.

        :param protocol: The protocol instance.
        :type protocol: RamsesProtocolT
        :param io_writer: Async callable to handle outbound frames.
        :type io_writer: Callable[[str], Awaitable[None]]
        :param disable_sending: Whether to disable sending, defaults to False.
        :type disable_sending: bool, optional
        """
        # Pass kwargs up the chain. _ReadTransport will extract 'loop' if present.
        # _BaseTransport will pass 'loop' to _CallbackTransportAbstractor, which consumes it.
        super().__init__(disable_sending=disable_sending, **kwargs)

        self._protocol = protocol
        self._io_writer = io_writer

        # Section 3.1: "Initial State: Default to a PAUSED state"
        self._reading = False

        # Section 6.1: Object Lifecycle Logging
        _LOGGER.info(f"CallbackTransport created with io_writer={io_writer}")

        # NOTE: connection_made is NOT called here. It must be triggered
        # externally (e.g. by the Bridge) via the protocol methods once
        # the external connection is ready.

    async def write_frame(self, frame: str, disable_tx_limits: bool = False) -> None:
        """Process a frame for transmission by passing it to the external writer.

        :param frame: The frame to write.
        :type frame: str
        :param disable_tx_limits: Unused for this transport, kept for API compatibility.
        :type disable_tx_limits: bool, optional
        :raises exc.TransportError: If sending is disabled or the writer fails.
        """
        if self._disable_sending:
            raise exc.TransportError("Sending has been disabled")

        # Section 6.1: Boundary Logging (Outgoing)
        _LOGGER.debug(f"Sending frame via external writer: {frame}")

        try:
            await self._io_writer(frame)
        except Exception as err:
            _LOGGER.error(f"External writer failed to send frame: {err}")
            raise exc.TransportError(f"External writer failed: {err}") from err

    async def _write_frame(self, frame: str) -> None:
        """Wait for the frame to be written by the external writer.

        :param frame: The frame to write.
        :type frame: str
        """
        # Wrapper to satisfy abstract base class, though logic is in write_frame
        await self.write_frame(frame)

    def receive_frame(self, frame: str, dtm: str | None = None) -> None:
        """Ingest a frame from the external source (Read Path).

        This is the public method called by the Bridge to inject data.

        :param frame: The raw frame string to receive.
        :type frame: str
        :param dtm: The timestamp of the frame, defaults to current time.
        :type dtm: str | None, optional
        """
        _LOGGER.debug(
            f"Received frame from external source: frame='{frame}', timestamp={dtm}"
        )

        # Section 4.2: Circuit Breaker implementation (Packet gating)
        if not self._reading:
            _LOGGER.debug(f"Dropping received frame (transport paused): {repr(frame)}")
            return

        dtm = dtm or dt_now().isoformat()

        # Section 6.1: Boundary Logging (Incoming)
        _LOGGER.debug(
            f"Ingesting frame into transport: frame='{frame}', timestamp={dtm}"
        )

        # Pass to the standard processing pipeline
        self._frame_read(dtm, frame.rstrip())


def validate_topic_path(path: str) -> str:
    """Test the topic path and normalize it.

    :param path: The candidate topic path.
    :type path: str
    :return: The valid, normalized path.
    :rtype: str
    :raises ValueError: If the path format is invalid.
    """

    # The user can supply the following paths:
    # - ""
    # - "/RAMSES/GATEWAY"
    # - "/RAMSES/GATEWAY/+" (the previous two are equivalent to this one)
    # - "/RAMSES/GATEWAY/18:123456"

    # "RAMSES/GATEWAY/+"                -> online, online, ...
    # "RAMSES/GATEWAY/18:017804"        -> online
    # "RAMSES/GATEWAY/18:017804/info/+" -> ramses_esp/0.4.0
    # "RAMSES/GATEWAY/+/rx"             -> pkts from all gateways

    new_path = path or SZ_RAMSES_GATEWAY
    if new_path.startswith("/"):
        new_path = new_path[1:]
    if not new_path.startswith(SZ_RAMSES_GATEWAY):
        raise ValueError(f"Invalid topic path: {path}")
    if new_path == SZ_RAMSES_GATEWAY:
        new_path += "/+"
    if len(new_path.split("/")) != 3:
        raise ValueError(f"Invalid topic path: {path}")
    return new_path


RamsesTransportT: TypeAlias = (
    FileTransport | MqttTransport | PortTransport | CallbackTransport
)


async def transport_factory(
    protocol: RamsesProtocolT,
    /,
    *,
    port_name: SerPortNameT | None = None,
    port_config: PortConfigT | None = None,
    packet_log: str | None = None,
    packet_dict: dict[str, str] | None = None,
    transport_constructor: Callable[..., Awaitable[RamsesTransportT]] | None = None,
    disable_sending: bool = False,
    extra: dict[str, Any] | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    log_all: bool = False,
    **kwargs: Any,  # HACK: odd/misc params
) -> RamsesTransportT:
    """Create and return a Ramses-specific async packet Transport.

    :param protocol: The protocol instance that will use this transport.
    :type protocol: RamsesProtocolT
    :param port_name: Serial port name or MQTT URL, defaults to None.
    :type port_name: SerPortNameT | None, optional
    :param port_config: Configuration dictionary for serial port, defaults to None.
    :type port_config: PortConfigT | None, optional
    :param packet_log: Path to a file containing packet logs for playback/parsing, defaults to None.
    :type packet_log: str | None, optional
    :param packet_dict: Dictionary of packets for playback, defaults to None.
    :type packet_dict: dict[str, str] | None, optional
    :param transport_constructor: Custom async callable to create a transport, defaults to None.
    :type transport_constructor: Callable[..., Awaitable[RamsesTransportT]] | None, optional
    :param disable_sending: If True, the transport will not transmit packets, defaults to False.
    :type disable_sending: bool | None, optional
    :param extra: Extra configuration options, defaults to None.
    :type extra: dict[str, Any] | None, optional
    :param loop: Asyncio event loop, defaults to None.
    :type loop: asyncio.AbstractEventLoop | None, optional
    :param log_all: If True, log all MQTT messages including non-protocol ones, defaults to False.
    :type log_all: bool, optional
    :param kwargs: Additional keyword arguments for specific transports.
    :type kwargs: Any
    :return: An instantiated RamsesTransportT object.
    :rtype: RamsesTransportT
    :raises exc.TransportSourceInvalid: If the packet source is invalid or multiple sources are specified.
    """

    # If a constructor is provided, delegate entirely to it.
    if transport_constructor:
        _LOGGER.debug("transport_factory: Delegating to external transport_constructor")
        return await transport_constructor(
            protocol, disable_sending=disable_sending, extra=extra, **kwargs
        )

    # kwargs are specific to a transport. The above transports have:
    # evofw3_flag, use_regex

    def get_serial_instance(  # type: ignore[no-any-unimported]
        ser_name: SerPortNameT, ser_config: PortConfigT | None
    ) -> Serial:
        """Return a Serial instance for the given port name and config.

        May: raise TransportSourceInvalid("Unable to open serial port...")

        :param ser_name: Name of the serial port.
        :type ser_name: SerPortNameT
        :param ser_config: Configuration for the serial port.
        :type ser_config: PortConfigT | None
        :return: Configured Serial object.
        :rtype: Serial
        :raises exc.TransportSourceInvalid: If the serial port cannot be opened.
        """
        # For example:
        # - python client.py monitor 'rfc2217://localhost:5001'
        # - python client.py monitor 'alt:///dev/ttyUSB0?class=PosixPollSerial'

        ser_config = SCH_SERIAL_PORT_CONFIG(ser_config or {})

        try:
            ser_obj = serial_for_url(ser_name, **ser_config)
        except SerialException as err:
            _LOGGER.error(
                "Failed to open %s (config: %s): %s", ser_name, ser_config, err
            )
            raise exc.TransportSourceInvalid(
                f"Unable to open the serial port: {ser_name}"
            ) from err

        # FTDI on Posix/Linux would be a common environment for this library...
        with contextlib.suppress(AttributeError, NotImplementedError, ValueError):
            ser_obj.set_low_latency_mode(True)

        return ser_obj

    def issue_warning() -> None:
        """Warn of the perils of semi-supported configurations."""
        _LOGGER.warning(
            f"{'Windows' if os.name == 'nt' else 'This type of serial interface'} "
            "is not fully supported by this library: "
            "please don't report any Transport/Protocol errors/warnings, "
            "unless they are reproducible with a standard configuration "
            "(e.g. linux with a local serial port)"
        )

    if len([x for x in (packet_dict, packet_log, port_name) if x is not None]) != 1:
        _LOGGER.warning(
            f"Input: packet_dict: {packet_dict}, packet_log: {packet_log}, port_name: {port_name}"
        )
        raise exc.TransportSourceInvalid(
            "Packet source must be exactly one of: packet_dict, packet_log, port_name"
        )

    # File
    if (pkt_source := packet_log or packet_dict) is not None:
        return FileTransport(pkt_source, protocol, extra=extra, loop=loop, **kwargs)

    assert port_name is not None  # mypy check
    assert port_config is not None  # mypy check

    # MQTT
    if port_name[:4] == "mqtt":
        # Check for custom timeout in kwargs, fallback to constant
        mqtt_timeout = kwargs.get("timeout", _DEFAULT_TIMEOUT_MQTT)

        transport = MqttTransport(
            port_name,
            protocol,
            disable_sending=bool(
                disable_sending
            ),  # Feature Added: handled disable_sending
            extra=extra,
            loop=loop,
            log_all=log_all,
            **kwargs,
        )

        try:
            # Robustness Fix: Wait with timeout, handle failure gracefully
            await protocol.wait_for_connection_made(timeout=mqtt_timeout)
        except Exception:
            # CRITICAL FIX: Close the transport if setup fails to prevent "Zombie" callbacks
            # This prevents the "AttributeError: 'NoneType'..." crash later on
            transport.close()
            raise

        return transport

    # Serial
    ser_instance = get_serial_instance(port_name, port_config)

    if os.name == "nt" or ser_instance.portstr[:7] in ("rfc2217", "socket:"):
        issue_warning()  # TODO: add tests for these...

    transport = PortTransport(  # type: ignore[assignment]
        ser_instance,
        protocol,
        disable_sending=bool(disable_sending),
        extra=extra,
        loop=loop,
        **kwargs,
    )

    # TODO: remove this? better to invoke timeout after factory returns?
    await protocol.wait_for_connection_made(timeout=_DEFAULT_TIMEOUT_PORT)
    # pytest-cov times out in virtual_rf.py when set below 30.0 on GitHub Actions
    return transport
