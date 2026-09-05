#!/usr/bin/env python3
"""RAMSES RF - RAMSES-II compatible packet protocol base classes.

This module provides the foundational protocol layers, handling transport
binding, basic message dispatching, regex-based payload manipulation,
logging and device ID filtering mechanisms.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from collections import deque
from collections.abc import Callable
from datetime import datetime as dt, timedelta as td
from typing import Any, Final

from ..address import ALL_DEV_ADDR, HGI_DEV_ADDR, NON_DEV_ADDR
from ..const import (
    DEFAULT_GAP_DURATION,
    DEFAULT_NUM_REPEATS,
    I_,
    MAX_GAP_DURATION,
    MAX_NUM_REPEATS,
    SZ_ACTIVE_HGI,
    Code,
    Priority,
)
from ..dtos import CommandDTO, PacketDTO
from ..exceptions import (
    PacketInvalid,
    ProtocolError,
    ProtocolSendFailed,
    TransportError,
)
from ..helpers import dt_now
from ..interfaces import ProtocolInterface, TransportInterface
from ..packet import Packet
from ..schemas import (
    SZ_BLOCK_LIST,
    SZ_INBOUND,
    SZ_KNOWN_LIST,
    SZ_OUTBOUND,
    SZ_SCHEMA,
)
from ..typing import DeviceIdT, MsgFilterT, MsgHandlerT, QosParams

TIP: Final[str] = (
    f", configure the {SZ_SCHEMA}/{SZ_KNOWN_LIST}/{SZ_BLOCK_LIST} as required"
)

_DBG_FORCE_LOG_PACKETS: Final[bool] = False

_LOGGER = logging.getLogger(__name__)

DEFAULT_QOS = QosParams()


class _BaseProtocol(ProtocolInterface, asyncio.Protocol):
    """Base class for RAMSES II protocols."""

    WRITER_TASK: Final[str] = "writer_task"

    def __init__(self, msg_handler: MsgHandlerT, /) -> None:
        """Initialize the base protocol.

        :param msg_handler: The callback invoked when a valid message is
            processed.
        :type msg_handler: MsgHandlerT
        """
        super().__init__()
        self._msg_handler = msg_handler
        self._msg_handlers: list[tuple[MsgHandlerT, MsgFilterT | None]] = []
        self._raw_packet_handlers: list[MsgHandlerT] = []
        self._handler_tasks: set[asyncio.Task[Any]] = set()

        self._transport: TransportInterface | None = None

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.get_event_loop()

        # Start in R/O mode; wait for connection_made() to resume writing
        self._pause_writing: bool = True
        self._wait_connection_lost: asyncio.Future[None] | None = None
        self._wait_connection_made: asyncio.Future[TransportInterface] = (
            self._loop.create_future()
        )

        self._this_msg: PacketDTO | None = None
        self._prev_msg: PacketDTO | None = None

        self._is_evofw3: bool | None = None

        self._active_hgi: DeviceIdT | None = None

        # regex rules and sync trackers
        self._inbound_regex: dict[str, str] = {}
        self._outbound_regex: dict[str, str] = {}
        self._tracked_sync_cycles: deque[Packet] = deque(maxlen=3)

    def _create_handler_task(self, coro: Any) -> None:
        """Create a fire-and-forget task for a message handler coroutine.

        The task is tracked in _handler_tasks so it can be cancelled in
        connection_lost().
        """
        task = self._loop.create_task(coro)
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    @property
    def hgi_id(self) -> DeviceIdT:
        """Get the Hardware Gateway Interface ID."""
        return HGI_DEV_ADDR.id

    def set_regex_rules(self, rules: dict[str, dict[str, str]]) -> None:
        """Set regex rules for inbound/outbound payload manipulation."""
        self._inbound_regex = rules.get(SZ_INBOUND, {})
        self._outbound_regex = rules.get(SZ_OUTBOUND, {})

    def _apply_regex(self, frame: str, rules: dict[str, str]) -> str:
        """Apply regex hacks to a frame string."""
        if not rules:
            return frame
        result = frame
        for pattern, replacement in rules.items():
            try:
                result = re.sub(pattern, replacement, result)
            except re.error as err:
                _LOGGER.warning(
                    "%s < issue with regex (%s, %s): %s",
                    frame,
                    pattern,
                    replacement,
                    err,
                )
        return result

    def add_handler(
        self,
        msg_handler: MsgHandlerT,
        /,
        *,
        msg_filter: MsgFilterT | None = None,
    ) -> Callable[[], None]:
        """Add a Message handler to the list of such callbacks.

        Returns a callback that can be used to subsequently remove the
        Message handler.

        :param msg_handler: The handler function to add.
        :type msg_handler: MsgHandlerT
        :param msg_filter: An optional filter to apply before calling
            the handler.
        :type msg_filter: MsgFilterT | None
        :return: A callable to remove the handler.
        :rtype: Callable[[], None]
        """
        entry = (msg_handler, msg_filter)

        def del_handler() -> None:
            if entry in self._msg_handlers:
                self._msg_handlers.remove(entry)

        if entry not in self._msg_handlers:
            self._msg_handlers.append(entry)

        return del_handler

    def add_raw_packet_handler(
        self,
        msg_handler: MsgHandlerT,
        /,
    ) -> Callable[[], None]:
        """Add a raw packet handler that fires BEFORE the device ID filter.

        Raw handlers receive every valid PacketDTO, including packets from
        unknown devices that would be filtered out by enforce_known_list.
        Used by the passive scan engine to discover unknown devices.

        :param msg_handler: The handler function to add.
        :return: A callable to remove the handler.
        """
        self._raw_packet_handlers.append(msg_handler)

        def del_handler() -> None:
            if msg_handler in self._raw_packet_handlers:
                self._raw_packet_handlers.remove(msg_handler)

        return del_handler

    def connection_made(
        self, transport: Any, /, *, ramses: bool = False
    ) -> None:
        """Establish connection to the Transport.

        The argument is the transport representing the pipe connection.
        To receive data, wait for packet_received() calls. When the
        connection is closed, connection_lost() is called.

        The ``ramses`` keyword is accepted for compatibility with
        ``ProtocolInterface.connection_made`` but ignored here —
        ``PortTransport`` calls this directly after signature echo.

        ``transport`` is typed ``Any`` to satisfy mypy's Liskov check
        against ``asyncio.BaseProtocol.connection_made`` (which expects
        ``BaseTransport``).  At runtime it is always a
        ``TransportInterface``.
        """
        if self._wait_connection_made.done():
            return

        self._wait_connection_lost = self._loop.create_future()
        self._wait_connection_made.set_result(transport)
        self._transport = transport

    async def wait_for_connection_made(
        self, timeout: float = 1.0
    ) -> TransportInterface:
        """Wait until connection_made() has been invoked.

        Will raise TransportError if isn't connected within timeout
        seconds.
        """
        try:
            return await asyncio.wait_for(self._wait_connection_made, timeout)
        except TimeoutError as err:
            raise TransportError(
                f"Transport did not bind to Protocol within {timeout} secs"
            ) from err

    def connection_lost(self, error: Exception | None) -> None:
        """Handle lost or closed Transport connection.

        The argument is an exception object or None (the latter meaning
        a regular EOF is received or the connection was aborted or
        closed).
        """
        if not self._wait_connection_lost:
            _LOGGER.debug(
                "connection_lost called but no connection was established (ignoring)"
            )
            # Reset the connection made future for next attempt
            if self._wait_connection_made.done():
                self._wait_connection_made = self._loop.create_future()
            return

        if self._wait_connection_lost.done():
            return

        # Cancel any pending handler tasks
        for task in self._handler_tasks:
            if not task.done():
                task.cancel()
        self._handler_tasks.clear()

        # Reset connection-scoped state so connection_made() can run
        # cleanly on reconnect (e.g. MQTT broker reconnect, issue 1119)
        self._active_hgi = None
        self._is_evofw3 = None

        self._wait_connection_made = self._loop.create_future()
        if error:
            self._wait_connection_lost.set_exception(error)
        else:
            self._wait_connection_lost.set_result(None)

    async def wait_for_connection_lost(
        self, timeout: float = 1.0
    ) -> Exception | None:
        """Wait until connection_lost() has been invoked.

        Includes scenarios where neither connection_made() nor
        connection_lost() were invoked.

        Will raise TransportError if isn't disconnect within timeout
        seconds.
        """
        if not self._wait_connection_lost:
            return None

        try:
            return await asyncio.wait_for(self._wait_connection_lost, timeout)
        except TimeoutError as err:
            raise TransportError(
                f"Transport did not unbind from Protocol within {timeout} secs"
            ) from err
        except Exception as err:
            # If the transport dropped unexpectedly,
            # connection_lost(err) sets the exception on the future.
            # Awaiting it raises the exception here. We catch and
            # return it to satisfy the ExceptionT | None type hint and
            # prevent teardown crashes in the caller.
            return err

    def pause_writing(self) -> None:
        """Pause writing when transport buffer exceeds high-water mark."""
        self._pause_writing = True

    def resume_writing(self) -> None:
        """Resume writing when transport buffer drains below low-water mark."""
        self._pause_writing = False

    async def _send_impersonation_alert(self, command: CommandDTO) -> None:
        """Allow the Protocol to send an impersonation alert (stub)."""
        return

    def _patch_cmd_if_needed(self, command: CommandDTO) -> CommandDTO:
        """Patch the command source address to match the gateway.

        evofw3: swap placeholder 18:000730 for the real HGI ID.

        HGI80 (TI 3410): reverse — swap the real HGI ID back to
        18:000730, as the HGI80 firmware requires the placeholder as
        source for its own transmissions.  Using the real ID causes a
        silent drop and WantEcho timeout (issue 835).
        """
        if (
            self.hgi_id
            and self._is_evofw3  # Only patch if using evofw3 (not HGI80)
            and command.addr1 == HGI_DEV_ADDR.id
            and self.hgi_id != HGI_DEV_ADDR.id
        ):
            _LOGGER.debug(
                "Patching command with active HGI ID: swapped %s -> %s for %s|%s",
                HGI_DEV_ADDR.id,
                self.hgi_id,
                command.verb,
                command.code,
            )
            return dataclasses.replace(command, addr1=self.hgi_id)

        # HGI80: reverse-patch real HGI ID back to the placeholder.
        # The HGI80 firmware requires 18:000730 as the source for
        # frames it transmits; using the actual gateway ID causes a
        # silent drop and WantEcho timeout (issue 835, cc 864).
        if (
            self.hgi_id
            and not self._is_evofw3  # HGI80
            and command.addr1 == self.hgi_id
            and self.hgi_id != HGI_DEV_ADDR.id
        ):
            _LOGGER.debug(
                "Patching command for HGI80: swapped %s -> %s for %s|%s",
                self.hgi_id,
                HGI_DEV_ADDR.id,
                command.verb,
                command.code,
            )
            return dataclasses.replace(command, addr1=HGI_DEV_ADDR.id)

        return command

    async def send_cmd(
        self,
        command: CommandDTO,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        qos: QosParams | None = None,
    ) -> Packet:
        """Send a Command with QoS until success or ProtocolError.

        Returns the Command's response Packet or the Command echo.

        num_repeats is # of times to send the Command, in addition to
        the first transmit, with gap_duration seconds between each
        transmission.

        Commands are queued and sent FIFO, except higher-priority
        Commands are always sent first.

        Will raise:
            ProtocolSendFailed: tried to Tx Command, but didn't get
                echo/reply
            ProtocolError:      didn't attempt to Tx Command for some
                reason
        """
        assert 0 <= gap_duration <= MAX_GAP_DURATION, (
            "Out of range: gap_duration"
        )
        assert 0 <= num_repeats <= MAX_NUM_REPEATS, "Out of range: num_repeats"

        # Patch command with actual HGI ID if it uses the default placeholder
        patched_cmd = self._patch_cmd_if_needed(command)

        if patched_cmd.addr1 != self.hgi_id:  # Was HGI_DEV_ADDR.id
            await self._send_impersonation_alert(patched_cmd)

        packet = await self._send_cmd(  # may: raise ProtocolError/ProtocolSendFailed
            patched_cmd,
            gap_duration=gap_duration,
            num_repeats=num_repeats,
            priority=priority,
            qos=qos or DEFAULT_QOS,
        )

        if packet is None:
            raise ProtocolSendFailed(
                f"Failed to send command: {patched_cmd} (no packet returned)"
            )
        return packet

    async def _send_cmd(
        self,
        command: CommandDTO,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        qos: QosParams = DEFAULT_QOS,
    ) -> Packet | None:  # only cmd, no args, kwargs
        raise NotImplementedError(f"{self}: Unexpected error")

    async def _send_frame(
        self, frame: str, num_repeats: int = 0, gap_duration: float = 0.0
    ) -> None:
        """Write to the transport."""
        if self._transport is None:
            raise ProtocolSendFailed("Transport is not connected")

        # apply outbound regex
        frame = self._apply_regex(frame, self._outbound_regex)

        await self._transport.write_frame(frame)
        for _ in range(num_repeats - 1):
            await asyncio.sleep(gap_duration)
            await self._transport.write_frame(frame)

    def packet_received(self, packet: Packet) -> None:
        """Wrap self._packet_received(packet).

        Applies inbound regex modifications and tracks synchronization
        cycles before passing the packet to the internal receiver.

        :param packet: The received Packet object to process.
        :type packet: Packet
        """
        # Use packet._frame and prepend RSSI so from_port can correctly parse it
        raw_frame = packet._frame
        hacked_frame = self._apply_regex(raw_frame, self._inbound_regex)

        if hacked_frame != raw_frame:
            try:
                # Packet.from_port strictly expects the 3-character RSSI
                # + space prefix
                packet = Packet.from_port(
                    packet.dtm, f"{packet.rssi} {hacked_frame}"
                )
            except (ValueError, PacketInvalid) as err:
                _LOGGER.debug(
                    "Regex modified frame is invalid, reverting: %s", err
                )
                # Fallback to original packet if regex broke it (packet
                # remains unchanged)

        # Track Sync Cycles
        if (
            packet.code == Code._1F09
            and packet.verb == I_
            and packet._len == 3
        ):

            def is_pending(p: Packet) -> bool:
                """Check if a packet's sync cycle is still pending.

                :param p: The packet to evaluate.
                :return: True if the packet is within the pending
                    window.
                """
                p_dtm = (
                    p.dtm.replace(tzinfo=None)
                    if p.dtm.tzinfo is not None
                    else p.dtm
                )
                now = dt_now()
                now_dtm = (
                    now.replace(tzinfo=None) if now.tzinfo is not None else now
                )
                return bool(
                    p_dtm + td(seconds=int(p.payload[2:6], 16) / 10) > now_dtm
                )

            self._tracked_sync_cycles = deque(
                p
                for p in self._tracked_sync_cycles
                if p.src != packet.src and is_pending(p)
            )
            self._tracked_sync_cycles.append(packet)

        if _DBG_FORCE_LOG_PACKETS:
            _LOGGER.warning("Recv'd: %s %s", packet.rssi, packet)
        elif _LOGGER.getEffectiveLevel() > logging.DEBUG:
            _LOGGER.info("Recv'd: %s %s", packet.rssi, packet)
        else:
            _LOGGER.debug("Recv'd: %s %s", packet.rssi, packet)

        self._packet_received(packet)

    def _packet_received(self, packet: Packet) -> None:
        """Handle received Packet from the Transport."""
        try:
            msg = packet.to_dto()  # should log all invalid msgs appropriately
        except PacketInvalid as err:
            # We explicitly catch specific validation failures. Unhandled
            # internal errors like TypeError or AttributeError will
            # correctly bubble up and fail loudly.
            _LOGGER.debug("Dropped invalid packet during parsing: %s", err)
            return

        self._this_msg, self._prev_msg = msg, self._this_msg
        self._msg_received(msg)

    def _msg_received(self, msg: PacketDTO) -> None:
        """Pass any valid/wanted Messages to the client's callbacks.

        Also maintain _prev_msg, _this_msg attrs.
        """
        if self._msg_handler is not None:
            _LOGGER.debug("Dispatching valid message to handler: %s", msg)
            # Ensure safe dispatch to either coroutine or standard handler
            result = self._msg_handler(msg)
            if asyncio.iscoroutine(result):
                self._create_handler_task(result)

        for callback, msg_filter in self._msg_handlers:
            if msg_filter is None or msg_filter(msg):
                result = callback(msg)
                if asyncio.iscoroutine(result):
                    self._create_handler_task(result)


class _DeviceIdFilterMixin(_BaseProtocol):
    """Filter out unwanted (valid) packets via device IDs."""

    def __init__(
        self,
        msg_handler: MsgHandlerT,
        /,
        *,
        disable_warnings: bool = False,
        enforce_include_list: bool = False,
        exclude_list: list[str] | None = None,
        include_list: list[str] | None = None,
        hgi_id: str | None = None,
    ) -> None:
        super().__init__(msg_handler)

        exclude_list = exclude_list or []
        include_list = include_list or []

        self.enforce_include = enforce_include_list
        self._exclude = list(exclude_list)
        self._include = list(include_list)
        self._include += [ALL_DEV_ADDR.id, NON_DEV_ADDR.id]

        self._active_hgi: DeviceIdT | None = None
        self._known_hgi = hgi_id

        self._foreign_gwys_lst: list[DeviceIdT] = []
        self._foreign_last_run = dt.now().date()

    @property
    def hgi_id(self) -> DeviceIdT:
        """Get the ID of the HGI handling the comms."""
        if not self._transport:
            return DeviceIdT(self._known_hgi or HGI_DEV_ADDR.id)
        hgi = self._transport.get_extra_info(SZ_ACTIVE_HGI)
        return DeviceIdT(hgi or self._known_hgi or HGI_DEV_ADDR.id)

    def _set_active_hgi(
        self, device_id: DeviceIdT | None, by_signature: bool = False
    ) -> None:
        """Set the Active Gateway (HGI) device_id.

        Send a warning if the include list is configured incorrectly.

        If device_id is None (MQTT bridge where the HGI ID isn't known
        yet — the ramses_esp hasn't sent its "online" LWT message), no
        warning is emitted.  The transport will update SZ_ACTIVE_HGI
        when the real ID arrives, and the hgi_id property reads it from
        there.  See issue 1002.
        """
        if self._active_hgi is not None and self._active_hgi != device_id:
            _LOGGER.warning(
                "Active gateway already set to %s, overwriting with %s "
                "(connection_made called twice without connection_lost)",
                self._active_hgi,
                device_id,
            )

        # MQTT bridges: HGI ID not known yet (ramses_esp hasn't sent
        # its "online" LWT message).  Defer the check — the transport
        # will update SZ_ACTIVE_HGI when the real ID arrives.
        if device_id is None:
            _LOGGER.debug(
                "Active gateway ID not yet known (MQTT bridge?), "
                "deferring known_list check until the device sends "
                "its online message"
            )
            return

        msg = f"The active gateway '{device_id}: {{ class: HGI }}' "
        msg += "(by signature)" if by_signature else "(by filter)"

        if device_id not in self._exclude:
            self._active_hgi = device_id

        if device_id in self._exclude:
            _LOGGER.error(
                "%s MUST NOT be in the %s%s", msg, SZ_BLOCK_LIST, TIP
            )

        elif device_id not in self._include:
            _LOGGER.warning(
                "%s SHOULD be in the (enforced) %s", msg, SZ_KNOWN_LIST
            )

        elif not self.enforce_include:
            _LOGGER.info(
                "%s is in the %s, which SHOULD be enforced", msg, SZ_KNOWN_LIST
            )

        else:
            _LOGGER.debug("%s is in the %s", msg, SZ_KNOWN_LIST)

    def _is_wanted_addrs(
        self,
        source_id: DeviceIdT,
        destination_id: DeviceIdT,
        sending: bool = False,
    ) -> bool:
        """Return True if the packet is not to be filtered out.

        In any one packet, an excluded device_id 'trumps' an included
        device_id.
        """
        # Deferred HGI check: if _set_active_hgi was called with None
        # (MQTT bridge, HGI ID not known yet), check now whether the
        # transport has learned the real HGI ID.  This runs the
        # known_list check that was skipped during connection_made.
        # See issue 1002.
        if self._active_hgi is None and self._transport and not sending:
            real_hgi = self._transport.get_extra_info(SZ_ACTIVE_HGI)
            if real_hgi is not None:
                self._set_active_hgi(real_hgi)

        def warn_foreign_hgi(device_id: DeviceIdT) -> None:
            current_date = dt.now().date()

            if self._foreign_last_run != current_date:
                self._foreign_last_run = current_date
                self._foreign_gwys_lst = []  # reset the list every 24h

            if device_id in self._foreign_gwys_lst:
                return

            # INFO, not WARNING: an unknown HGI (not in any list) is
            # allowed through for eavesdropping (issue 822), so this
            # message is purely informational.  With multi-HGI setups
            # now supported by the discovery_scan config schema, an
            # unknown second HGI is common and not a problem.
            # See issue 1020.
            _LOGGER.info(
                f"Device {device_id} is potentially a Foreign gateway, "
                f"the Active gateway is {self._active_hgi}, "
                f"alternatively, is it a HVAC device?{TIP}"
            )
            self._foreign_gwys_lst.append(device_id)

        for dev_id in dict.fromkeys(
            (source_id, destination_id)
        ):  # removes duplicates
            # HGI devices (18:) are gateways, not sensors/actuators.
            # The active gateway and known HGIs (in the known_list) are
            # always allowed through.  A foreign HGI that the caller has
            # explicitly put in the block_list (e.g. ramses_cc marks it
            # _owner: not-me) is blocked — it belongs to a different
            # system and won't communicate with our controller.
            #
            # An unknown HGI (not in any list) might be our own second
            # gateway not yet configured.  Its packets are allowed through
            # so the active gateway can eavesdrop on the controller's
            # responses to it (issue 822), and an INFO-level message is
            # logged so the user can decide whether to configure it.
            # HGI_DEV_ADDR (18:000730, the generic broadcast address) is
            # always subject to the normal block/include checks below.
            if dev_id[:2] == "18" and dev_id != HGI_DEV_ADDR.id:
                if dev_id == self._active_hgi:
                    continue
                # A known/configured HGI (e.g. a second gateway declared
                # in the known_list, which the discovery_scan config schema
                # permits) is not a Foreign gateway — suppress the noise
                # warning for it (issue 1020).
                if dev_id in self._include:
                    continue
                # A foreign HGI explicitly in the block_list is blocked.
                # The issue 822 exemption (let unknown HGIs through for
                # eavesdropping) only applies to HGIs that are not in any
                # list — a foreign HGI marked as such by the caller should
                # be filtered out.  See issue 1020.
                if dev_id in self._exclude:
                    return False
                if self._active_hgi:
                    warn_foreign_hgi(dev_id)
                continue

            if dev_id in self._exclude:
                return False

            if dev_id in self._include:
                continue

            if sending and dev_id == HGI_DEV_ADDR.id:
                continue

            if self.enforce_include:
                return False

        return True

    def _packet_received(self, packet: Packet) -> None:
        # Fast early-exit: check device filter before invoking to_dto() if no raw handlers
        if not self._raw_packet_handlers and not self._is_wanted_addrs(
            packet.src.id, packet.dst.id
        ):
            _LOGGER.debug("%s < Packet excluded by device_id filter", packet)
            return

        # Fire raw handlers before the device ID filter (for the scan engine)
        if self._raw_packet_handlers:
            try:
                dto = packet.to_dto()
            except PacketInvalid as err:
                _LOGGER.debug(
                    "Dropped invalid packet for raw handlers: %s", err
                )
            else:
                for handler in self._raw_packet_handlers:
                    result = handler(dto)
                    if asyncio.iscoroutine(result):
                        self._create_handler_task(result)

            if not self._is_wanted_addrs(packet.src.id, packet.dst.id):
                _LOGGER.debug(
                    "%s < Packet excluded by device_id filter", packet
                )
                return

        super()._packet_received(packet)

    async def send_cmd(
        self,
        command: CommandDTO,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        qos: QosParams | None = None,
    ) -> Packet:
        if not self._is_wanted_addrs(
            DeviceIdT(command.addr1), DeviceIdT(command.addr2), sending=True
        ):
            raise ProtocolError(
                f"Command excluded by device_id filter: {command}"
            )
        return await super().send_cmd(
            command,
            gap_duration=gap_duration,
            num_repeats=num_repeats,
            priority=priority,
            qos=qos,
        )
