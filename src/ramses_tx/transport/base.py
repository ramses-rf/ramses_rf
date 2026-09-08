#!/usr/bin/env python3
"""RAMSES RF - Base classes for RAMSES-II compatible packet transports."""

from __future__ import annotations

import asyncio
import functools
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime as dt
from enum import Enum, auto
from typing import Any, TypeAlias

from .. import exceptions as exc
from ..address import HGI_DEV_ADDR
from ..const import SZ_ACTIVE_HGI, SZ_IS_EVOFW3, SZ_SIGNATURE
from ..helpers import dt_now
from ..interfaces import TransportInterface
from ..packet import Packet
from ..typing import DeviceIdT, RamsesProtocolT

_LOGGER = logging.getLogger(__name__)

_MAX_TRACKED_TRANSMITS = 99
_MAX_TRACKED_DURATION = 300
_DBG_DISABLE_REGEX_WARNINGS = False

_TxKeyT: TypeAlias = tuple[str, str, str, str, str, str]


class SignaturePolicy(Enum):
    """Startup signature probe policy for serial transports.

    Controls when (and whether) the startup ``7FFF`` signature probe
    is sent to discover the HGI device ID after port open.

    - ``IMMEDIATE``: send probes immediately after open (default,
      backward-compatible with existing single-USB behavior).
    - ``DELAYED``: wait ``startup_grace`` seconds after open before
      sending probes (for ESP32 USB devices that reset on open).
    - ``SKIP``: never send probes; the child is receive-only until the
      HGI ID is learned from inbound traffic or configured explicitly.
    """

    IMMEDIATE = auto()
    DELAYED = auto()
    SKIP = auto()


@dataclass
class TransportConfig:
    """Configuration parameters for Ramses transports.

    :param disable_sending: Disable packet transmission on transport.
    :type disable_sending: bool
    :param disable_qos: Disable QoS retries.
    :type disable_qos: bool
    :param enforce_min_gap: Enforce minimum gap between frame transmissions.
    :type enforce_min_gap: bool
    :param evofw_flag: Optional evofw3 flag.
    :type evofw_flag: str | None
    :param autostart: Autostart transport loop.
    :type autostart: bool
    :param log_all: Log all packets including malformed ones.
    :type log_all: bool
    :param use_regex: Regex filter rules.
    :type use_regex: dict[str, dict[str, str]]
    :param timeout: Optional timeout override.
    :type timeout: float | None
    :param app_context: Optional application context.
    :type app_context: Any | None
    :param signature_policy: Startup signature probe policy for serial
        transports (Phase 2, issue 1119).  ``IMMEDIATE`` sends probes
        right after port open (default, backward-compatible).
        ``DELAYED`` waits ``startup_grace`` seconds before probing
        (for ESP32 USB devices that reset on open).  ``SKIP`` never
        probes — the child is receive-only until identity is learned.
    :type signature_policy: SignaturePolicy
    :param startup_grace: Grace period in seconds before sending
        signature probes when ``signature_policy`` is ``DELAYED``.
        Ignored for ``IMMEDIATE`` and ``SKIP``.  Default 3.0s based on
        the hardware feasibility gate (ESP32 cold boot ~1.9s).
    :type startup_grace: float | None
    :param enable_reconnect: Enable automatic reconnect with exponential
        backoff when the serial port disconnects (Phase 2, issue 1119).
        Default False preserves existing single-USB behavior.
    :type enable_reconnect: bool
    :param max_reconnect_attempts: Maximum number of reconnect attempts
        before giving up.  Default 5.
    :type max_reconnect_attempts: int
    """

    disable_sending: bool = False
    disable_qos: bool = False
    enforce_min_gap: bool = False
    evofw_flag: str | None = None
    autostart: bool = False
    log_all: bool = False
    use_regex: dict[str, dict[str, str]] = field(default_factory=dict)
    timeout: float | None = None
    app_context: Any | None = None
    signature_policy: SignaturePolicy = SignaturePolicy.IMMEDIATE
    startup_grace: float | None = None
    enable_reconnect: bool = False
    max_reconnect_attempts: int = 5


class _BaseTransport:
    """Base class for all transports."""

    def __init__(self) -> None:
        """Initialize the base transport instance."""
        self._recent_tx_queue: deque[tuple[dt, _TxKeyT]] = deque(maxlen=20)
        self._recent_tx_counts: dict[_TxKeyT, int] = {}

    def _dt_now(self) -> dt:
        """Return current datetime using transport clock if available."""
        return dt_now()


class _ReadTransport(_BaseTransport, TransportInterface):
    """Interface for read-only transports."""

    _protocol: RamsesProtocolT = None  # type: ignore[assignment]  # set by transport_factory
    _loop: asyncio.AbstractEventLoop

    _is_hgi80: bool | None = (
        None  # NOTE: None (unknown) is as False (is_evofw3)
    )

    def __init__(
        self,
        /,
        *,
        config: TransportConfig,
        extra: dict[str, Any] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialize the read-only transport."""
        _BaseTransport.__init__(self)

        self._loop = loop or asyncio.get_event_loop()
        self._extra: dict[str, Any] = {} if extra is None else extra

        self._evofw_flag = config.evofw_flag

        self._closing: bool = False
        self._reading: bool = False

        self._this_packet: Packet | None = None
        self._prev_packet: Packet | None = None

        for key in (SZ_ACTIVE_HGI, SZ_SIGNATURE):
            self._extra.setdefault(key, None)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._protocol})"

    def _dt_now(self) -> dt:
        """Return a precise datetime, using last packet's dtm field."""
        try:
            return self._this_packet.dtm  # type: ignore[union-attr]
        except AttributeError:
            return dt_now()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The asyncio event loop as declared by SerialTransport."""
        return self._loop

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        """Get extra information about the transport."""
        if name == SZ_IS_EVOFW3:
            return not self._is_hgi80
        return self._extra.get(name, default)

    def is_closing(self) -> bool:
        """Return True if the transport is closing or has closed."""
        return self._closing

    def _close(self, exc: exc.RamsesException | None = None) -> None:
        """Inform the protocol that this transport has closed."""
        if self._closing:
            return
        self._closing = True

        if self.loop.is_closed():
            _LOGGER.debug("Event loop already closed, cannot notify protocol")
            return
        try:
            self.loop.call_soon_threadsafe(
                functools.partial(self._protocol.connection_lost, exc)
            )
        except RuntimeError:
            _LOGGER.debug(
                "Event loop closed during _close(), cannot notify protocol"
            )

    def close(self) -> None:
        """Close the transport gracefully."""
        self._close()

    def is_reading(self) -> bool:
        """Return True if the transport is receiving."""
        return self._reading

    def pause_reading(self) -> None:
        """Pause the receiving end (no data to protocol.packet_received())."""
        self._reading = False

    def resume_reading(self) -> None:
        """Resume the receiving end."""
        self._reading = True

    def _make_connection(self, gateway_id: DeviceIdT | None) -> None:
        """Register the connection with the protocol."""
        self._extra[SZ_ACTIVE_HGI] = gateway_id  # or HGI_DEV_ADDR.id

        if self.loop.is_closed():
            _LOGGER.debug("Event loop closed, cannot make connection")
            return
        try:
            self.loop.call_soon_threadsafe(
                functools.partial(
                    self._protocol.connection_made, self, ramses=True
                )
            )
        except RuntimeError:
            _LOGGER.debug("Event loop closed during _make_connection()")

    def _is_recent_tx(self, frame: str) -> bool:
        """Check if frame matches a recent Tx packet within 3.0s.

        Prunes expired entries, then does an O(1) dict lookup for
        the incoming packet key.

        HGI80 echoes arrive with the real HGI ID as addr1, but the
        TX frame used the placeholder 18:000730.  Both variants are
        checked (issue 835).

        :param frame: Raw ASCII frame string from transport.
        :type frame: str
        :returns: True if frame is a hardware echo of recent Tx.
        :rtype: bool
        """
        now = self._dt_now()
        while (
            self._recent_tx_queue
            and (now - self._recent_tx_queue[0][0]).total_seconds() > 3.0
        ):
            _, old_key = self._recent_tx_queue.popleft()
            if old_key in self._recent_tx_counts:
                if self._recent_tx_counts[old_key] <= 1:
                    del self._recent_tx_counts[old_key]
                else:
                    self._recent_tx_counts[old_key] -= 1

        try:
            rx_packet = Packet.from_port(now, frame, is_echo=True)
            rx_dto = rx_packet.to_dto()
        except Exception:
            return False

        # HGI80 echoes arrive with the real HGI ID as addr1,
        # but the TX frame used the placeholder 18:000730.
        addr1_variants = (
            (rx_dto.addr1, HGI_DEV_ADDR.id)
            if rx_dto.addr1 != HGI_DEV_ADDR.id
            else (rx_dto.addr1,)
        )
        for addr1 in addr1_variants:
            rx_key: _TxKeyT = (
                rx_dto.verb,
                rx_dto.code,
                addr1,
                rx_dto.addr2,
                rx_dto.addr3,
                rx_dto.raw_payload,
            )
            if rx_key in self._recent_tx_counts:
                return True
        return False

    def _frame_read(self, dtm_str: str, frame: str) -> None:
        """Make a Packet from the Frame and process it."""
        if not frame.strip():
            return

        is_echo = self._is_recent_tx(frame)

        try:
            packet = Packet.from_file(dtm_str, frame, is_echo=is_echo)
        except ValueError as err:
            _LOGGER.debug("%s < PacketInvalid(%s)", frame, err)
            return
        except exc.PacketInvalid as err:
            _LOGGER.warning("%s < PacketInvalid(%s)", frame, err)
            return

        try:
            self._packet_read(packet)
        except exc.TransportError as err:
            _LOGGER.debug("%s < Transport Error(%s)", packet, err)
            return

    def _packet_read(self, packet: Packet) -> None:
        """Pass any valid Packets to the protocol's callback."""
        self._this_packet, self._prev_packet = packet, self._this_packet

        if self._closing is True:
            raise exc.TransportError("Transport is closing or has closed")

        if self.loop.is_closed():
            raise exc.TransportError("Event loop is closed")

        try:
            self.loop.call_soon_threadsafe(
                self._protocol.packet_received, packet
            )
        except RuntimeError as err:
            # Event loop may close between the is_closed() check and this
            # call when the paho-mqtt thread races asyncio teardown (issue 802)
            raise exc.TransportError(f"Event loop is closed: {err}") from err
        except AssertionError as err:
            _LOGGER.exception("%s < exception from msg layer: %s", packet, err)
        except exc.ProtocolError as err:
            _LOGGER.error("%s < exception from msg layer: %s", packet, err)

    async def send_frame(self, frame: str) -> None:
        """Send a frame (alias for write_frame)."""
        await self.write_frame(frame)

    async def write_frame(
        self, frame: str, disable_tx_limits: bool = False
    ) -> None:
        """Transmit a frame via the underlying handler."""
        raise exc.TransportSerialError("This transport is read only")


class _FullTransport(_ReadTransport):
    """Interface representing a bidirectional transport."""

    def __init__(
        self,
        /,
        *,
        config: TransportConfig,
        extra: dict[str, Any] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialize the bidirectional transport."""
        _ReadTransport.__init__(self, config=config, extra=extra, loop=loop)
        self._transmit_times: deque[dt] = deque(
            maxlen=int(config.timeout)
            if config.timeout
            else _MAX_TRACKED_TRANSMITS
        )
        self._disable_sending: bool = config.disable_sending
        self._signature_policy: SignaturePolicy = config.signature_policy
        self._startup_grace: float = config.startup_grace or 3.0

    def _dt_now(self) -> dt:
        """Get a precise datetime, using the current dtm."""
        return dt_now()

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        """Get extra info, including transmit rate calculations."""
        if name == "tx_rate":
            return self._report_transmit_rate()
        return super().get_extra_info(name, default=default)

    def _report_transmit_rate(self) -> float:
        """Return transmit rate as packets per minute."""
        if not self._transmit_times:
            return 0.0

        now = dt.now()
        transmit_times = [
            t
            for t in self._transmit_times
            if (now - t).total_seconds() <= _MAX_TRACKED_DURATION
        ]
        if not transmit_times:
            return 0.0

        duration = (now - transmit_times[0]).total_seconds()
        if duration == 0:
            return 0.0

        return int(len(transmit_times) / duration * 6000) / 100

    def _track_transmit_rate(self) -> None:
        """Track the Tx rate as period of seconds per x transmits."""
        self._transmit_times.append(dt.now())
        _LOGGER.debug(
            "Current Tx rate: %.2f packets/min", self._report_transmit_rate()
        )

    def write(self, data: bytes) -> None:
        """Write the data to the underlying handler."""
        raise exc.TransportError(
            "write() not implemented, use write_frame() instead"
        )

    async def write_frame(
        self, frame: str, disable_tx_limits: bool = False
    ) -> None:
        """Transmit a frame via the underlying handler."""
        if self._disable_sending is True:
            raise exc.TransportError("Sending has been disabled")
        if self._closing is True:
            raise exc.TransportError("Transport is closing or has closed")

        self._log_tx_packet(frame)
        self._track_transmit_rate()
        await self._write_frame(frame)

    async def _write_frame(self, frame: str) -> None:
        """Write some data bytes to the underlying transport."""
        raise NotImplementedError("_write_frame() not implemented here")

    def _log_tx_packet(self, frame: str) -> None:
        """Emit outbound frames to packet log and record key for echo detection.

        :param frame: Outbound raw ASCII frame string to transmit.
        :type frame: str
        :returns: None
        :rtype: None
        """
        # rstrip only — preserve leading space in verbs
        # like " W", " I" which strip() would remove, making
        # the frame unparsable (issue 835).
        frame_clean = frame.rstrip()
        if not frame_clean:
            return

        if not frame_clean[:3].isdigit():
            frame_clean = f"000 {frame_clean}"

        try:
            now = self._dt_now()
            packet = Packet(now, frame_clean, is_tx=True)
            dto = packet.to_dto()
            tx_key: _TxKeyT = (
                dto.verb,
                dto.code,
                dto.addr1,
                dto.addr2,
                dto.addr3,
                dto.raw_payload,
            )
            self._recent_tx_queue.append((now, tx_key))
            self._recent_tx_counts[tx_key] = (
                self._recent_tx_counts.get(tx_key, 0) + 1
            )

            if self._protocol and hasattr(self._protocol, "_msg_received"):
                try:
                    self._protocol._msg_received(dto)
                except Exception as err:
                    _LOGGER.debug("Failed to dispatch Tx DTO: %s", err)
        except Exception as err:  # pragma: no cover - defensive
            _LOGGER.debug("Failed to log Tx frame: %s", err)


_RegexRuleT: TypeAlias = dict[str, str]
