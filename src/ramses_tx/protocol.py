#!/usr/bin/env python3
"""RAMSES RF - RAMSES-II compatible packet protocol."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime as dt
from typing import TYPE_CHECKING, Any, Final, TypeAlias

from . import exceptions as exc
from .address import ALL_DEV_ADDR, HGI_DEV_ADDR, NON_DEV_ADDR
from .command import Command
from .const import (
    DEFAULT_DISABLE_QOS,
    DEFAULT_GAP_DURATION,
    DEFAULT_NUM_REPEATS,
    DEV_TYPE_MAP,
    SZ_ACTIVE_HGI,
    SZ_IS_EVOFW3,
    DevType,
    Priority,
)
from .logger import set_logger_timesource
from .message import Message
from .packet import Packet
from .protocol_fsm import ProtocolContext
from .schemas import SZ_BLOCK_LIST, SZ_CLASS, SZ_KNOWN_LIST, SZ_PORT_NAME
from .transport import transport_factory
from .typing import ExceptionT, MsgFilterT, MsgHandlerT, QosParams

from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

if TYPE_CHECKING:
    from .schemas import DeviceIdT, DeviceListT
    from .transport import RamsesTransportT


TIP = f", configure the {SZ_KNOWN_LIST}/{SZ_BLOCK_LIST} as required"

#
# NOTE: All debug flags should be False for deployment to end-users
_DBG_DISABLE_IMPERSONATION_ALERTS: Final[bool] = False
_DBG_DISABLE_QOS: Final[bool] = False
_DBG_FORCE_LOG_PACKETS: Final[bool] = False

_LOGGER = logging.getLogger(__name__)


DEFAULT_QOS = QosParams()


class _BaseProtocol(asyncio.Protocol):
    """Base class for RAMSES II protocols."""

    WRITER_TASK = "writer_task"

    def __init__(self, msg_handler: MsgHandlerT) -> None:
        self._msg_handler = msg_handler
        self._msg_handlers: list[MsgHandlerT] = []

        self._transport: RamsesTransportT | None = None
        self._loop = asyncio.get_running_loop()

        self._pause_writing = False  # FIXME: Start in R/O mode as no connection yet?
        self._wait_connection_lost: asyncio.Future[None] | None = None
        self._wait_connection_made: asyncio.Future[RamsesTransportT] = (
            self._loop.create_future()
        )

        self._this_msg: Message | None = None
        self._prev_msg: Message | None = None

        self._is_evofw3: bool | None = None

        self._active_hgi: DeviceIdT | None = None
        self._context: ProtocolContext | None = None

    @property
    def hgi_id(self) -> DeviceIdT:
        return HGI_DEV_ADDR.id

    def add_handler(
        self,
        msg_handler: MsgHandlerT,
        /,
        *,
        msg_filter: MsgFilterT | None = None,
    ) -> Callable[[], None]:
        """Add a Message handler to the list of such callbacks.

        Returns a callback that can be used to subsequently remove the Message handler.
        """

        def del_handler() -> None:
            if msg_handler in self._msg_handlers:
                self._msg_handlers.remove(msg_handler)

        if msg_handler not in self._msg_handlers:
            self._msg_handlers.append(msg_handler)

        return del_handler

    def connection_made(self, transport: RamsesTransportT) -> None:  # type: ignore[override]
        """Called when the connection to the Transport is established.

        The argument is the transport representing the pipe connection. To receive data,
        wait for pkt_received() calls. When the connection is closed, connection_lost()
        is called.
        """

        if self._wait_connection_made.done():
            return

        self._wait_connection_lost = self._loop.create_future()
        self._wait_connection_made.set_result(transport)
        self._transport = transport

    async def wait_for_connection_made(self, timeout: float = 1) -> RamsesTransportT:
        """A courtesy function to wait until connection_made() has been invoked.

        Will raise TransportError if isn't connected within timeout seconds.
        """

        try:
            return await asyncio.wait_for(self._wait_connection_made, timeout)
        except TimeoutError as err:
            raise exc.TransportError(
                f"Transport did not bind to Protocol within {timeout} secs"
            ) from err

    def connection_lost(self, err: ExceptionT | None) -> None:  # type: ignore[override]
        """Called when the connection to the Transport is lost or closed.

        The argument is an exception object or None (the latter meaning a regular EOF is
        received or the connection was aborted or closed).
        """

        # FIX: Check if _wait_connection_lost exists before asserting
        # This handles cases where connection was never fully established (e.g. timeout)
        if not self._wait_connection_lost:
            _LOGGER.debug(
                "connection_lost called but no connection was established (ignoring)"
            )
            # Reset the connection made future for next attempt
            if self._wait_connection_made.done():
                self._wait_connection_made = self._loop.create_future()
            return

        if self._wait_connection_lost.done():  # BUG: why is callback invoked twice?
            return

        self._wait_connection_made = self._loop.create_future()
        if err:
            self._wait_connection_lost.set_exception(err)
        else:
            self._wait_connection_lost.set_result(None)

    async def wait_for_connection_lost(self, timeout: float = 1) -> ExceptionT | None:
        """A courtesy function to wait until connection_lost() has been invoked.

        Includes scenarios where neither connection_made() nor connection_lost() were
        invoked.

        Will raise TransportError if isn't disconnect within timeout seconds.
        """

        if not self._wait_connection_lost:
            return None

        try:
            return await asyncio.wait_for(self._wait_connection_lost, timeout)
        except TimeoutError as err:
            raise exc.TransportError(
                f"Transport did not unbind from Protocol within {timeout} secs"
            ) from err

    def pause_writing(self) -> None:
        """Called when the transport's buffer goes over the high-water mark.

        Pause and resume calls are paired -- pause_writing() is called once when the
        buffer goes strictly over the high-water mark (even if subsequent writes
        increases the buffer size even more), and eventually resume_writing() is called
        once when the buffer size reaches the low-water mark.

        Note that if the buffer size equals the high-water mark, pause_writing() is not
        called -- it must go strictly over. Conversely, resume_writing() is called when
        the buffer size is equal or lower than the low-water mark.  These end conditions
        are important to ensure that things go as expected when either mark is zero.

        NOTE: This is the only Protocol callback that is not called through
        EventLoop.call_soon() -- if it were, it would have no effect when it's most
        needed (when the app keeps writing without yielding until pause_writing() is
        called).
        """

        self._pause_writing = True

    def resume_writing(self) -> None:
        """Called when the transport's buffer drains below the low-water mark.

        See pause_writing() for details.
        """

        self._pause_writing = False

    async def _send_impersonation_alert(self, cmd: Command) -> None:
        """Allow the Protocol to send an impersonation alert (stub)."""
        return

    async def send_cmd(
        self,
        cmd: Command,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        qos: QosParams = DEFAULT_QOS,  # max_retries, timeout, wait_for_reply
    ) -> Packet:
        """Send a Command with Qos (with retries, until success or ProtocolError).

        Returns the Command's response Packet or the Command echo if a response is not
        expected (e.g. sending an RP).

        If wait_for_reply is True, return the RQ's RP (or W's I), or raise an exception
        if one doesn't arrive. If it is False, return the echo of the Command only. If
        it is None (the default), act as True for RQs, and False for all other Commands.

        num_repeats is # of times to send the Command, in addition to the fist transmit,
        with gap_duration seconds between each transmission. If wait_for_reply is True,
        then num_repeats is ignored.

        Commands are queued and sent FIFO, except higher-priority Commands are always
        sent first.

        Will raise:
            ProtocolSendFailed: tried to Tx Command, but didn't get echo/reply
            ProtocolError:      didn't attempt to Tx Command for some reason
        """

        assert gap_duration == DEFAULT_GAP_DURATION
        assert 0 <= num_repeats <= 3  # if QoS, only Tx x1, with no repeats

        # FIX: Patch command with actual HGI ID if it uses the default placeholder
        # NOTE: HGI80s (TI 3410) require the default ID (18:000730), or they will silent-fail

        if (
            self.hgi_id
            and self._is_evofw3  # Only patch if using evofw3 (not HGI80)
            and cmd._addrs[0].id == HGI_DEV_ADDR.id
            and self.hgi_id != HGI_DEV_ADDR.id
        ):
            # The command uses the default 18:000730, but we know the real ID.
            # Reconstruct the command string with the correct address.

            _LOGGER.debug(
                f"Patching command with active HGI ID: swapped {HGI_DEV_ADDR.id} -> {self.hgi_id} for {cmd._hdr}"
            )

            # Get current addresses as strings
            # The command uses the default 18:000730, but we know the real ID.
            # Reconstruct the command string with the correct address.

            # Get current addresses as strings
            new_addrs = [a.id for a in cmd._addrs]

            # ONLY patch the Source Address (Index 0).
            # Leave Dest (Index 1/2) alone to avoid breaking tests that expect 18:000730 there.
            new_addrs[0] = self.hgi_id

            new_frame = f"{cmd.verb} {cmd.seqn} {new_addrs[0]} {new_addrs[1]} {new_addrs[2]} {cmd.code} {int(cmd.len_):03d} {cmd.payload}"
            cmd = Command(new_frame)

        if qos and not self._context:
            _LOGGER.warning(f"{cmd} < QoS is currently disabled by this Protocol")

        if cmd.src.id != self.hgi_id:  # Was HGI_DEV_ADDR.id
            await self._send_impersonation_alert(cmd)

        if qos.wait_for_reply and num_repeats:
            _LOGGER.warning(f"{cmd} < num_repeats set to 0, as wait_for_reply is True")
            num_repeats = 0  # the lesser crime over wait_for_reply=False

        pkt = await self._send_cmd(  # may: raise ProtocolError/ProtocolSendFailed
            cmd,
            gap_duration=gap_duration,
            num_repeats=num_repeats,
            priority=priority,
            qos=qos,
        )

        if not pkt:  # HACK: temporary workaround for returning None
            raise exc.ProtocolSendFailed(f"Failed to send command: {cmd} (REPORT THIS)")

        return pkt

    async def _send_cmd(
        self,
        cmd: Command,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        qos: QosParams = DEFAULT_QOS,
    ) -> Packet:  # only cmd, no args, kwargs
        # await self._send_frame(
        #     str(cmd), num_repeats=num_repeats, gap_duration=gap_duration
        # )
        raise NotImplementedError(f"{self}: Unexpected error")

    async def _send_frame(
        self, frame: str, num_repeats: int = 0, gap_duration: float = 0.0
    ) -> None:  # _send_frame() -> transport
        """Write to the transport."""

        if self._transport is None:
            raise exc.ProtocolSendFailed("Transport is not connected")

        await self._transport.write_frame(frame)
        for _ in range(num_repeats - 1):
            await asyncio.sleep(gap_duration)
            await self._transport.write_frame(frame)

    def pkt_received(self, pkt: Packet) -> None:
        """A wrapper for self._pkt_received(pkt)."""
        if _DBG_FORCE_LOG_PACKETS:
            _LOGGER.warning(f"Recv'd: {pkt._rssi} {pkt}")
        elif _LOGGER.getEffectiveLevel() > logging.DEBUG:
            _LOGGER.info(f"Recv'd: {pkt._rssi} {pkt}")
        else:
            _LOGGER.debug(f"Recv'd: {pkt._rssi} {pkt}")

        self._pkt_received(pkt)

    def _pkt_received(self, pkt: Packet) -> None:
        """Called by the Transport when a Packet is received."""
        try:
            msg = Message(pkt)  # should log all invalid msgs appropriately
        except exc.PacketInvalid:  # TODO: InvalidMessageError (packet is valid)
            return

        self._this_msg, self._prev_msg = msg, self._this_msg
        self._msg_received(msg)

    def _msg_received(self, msg: Message) -> None:
        """Pass any valid/wanted Messages to the client's callback.

        Also maintain _prev_msg, _this_msg attrs.
        """

        if self._msg_handler:  # type: ignore[truthy-function]
            _LOGGER.debug(f"Dispatching valid message to handler: {msg}")
            self._loop.call_soon_threadsafe(self._msg_handler, msg)
        for callback in self._msg_handlers:
            # TODO: if handler's filter returns True:
            self._loop.call_soon_threadsafe(callback, msg)


class _DeviceIdFilterMixin(_BaseProtocol):
    """Filter out any unwanted (but otherwise valid) packets via device ids."""

    def __init__(
        self,
        msg_handler: MsgHandlerT,
        enforce_include_list: bool = False,
        exclude_list: DeviceListT | None = None,
        include_list: DeviceListT | None = None,
    ) -> None:
        super().__init__(msg_handler)

        exclude_list = exclude_list or {}
        include_list = include_list or {}

        self.enforce_include = enforce_include_list
        self._exclude = list(exclude_list.keys())
        self._include = list(include_list.keys())
        self._include += [ALL_DEV_ADDR.id, NON_DEV_ADDR.id]

        self._active_hgi: DeviceIdT | None = None
        # HACK: to disable_warnings if pkt source is static (e.g. a file/dict)
        # HACK: but a dynamic source (e.g. a port/MQTT) should warn if needed
        self._known_hgi = self._extract_known_hgi_id(
            include_list, disable_warnings=isinstance(self, ReadProtocol)
        )

        self._foreign_gwys_lst: list[DeviceIdT] = []
        self._foreign_last_run = dt.now().date()

    @property
    def hgi_id(self) -> DeviceIdT:
        if not self._transport:
            return self._known_hgi or HGI_DEV_ADDR.id
        # CRITICAL FIX: get_extra_info returns None if key exists but val is None
        # We must ensure we fallback to the known HGI or default if it returns None
        hgi = self._transport.get_extra_info(SZ_ACTIVE_HGI)
        return hgi or self._known_hgi or HGI_DEV_ADDR.id

    @staticmethod
    def _extract_known_hgi_id(
        include_list: DeviceListT,
        /,
        *,
        disable_warnings: bool = False,
        strict_checking: bool = False,
    ) -> DeviceIdT | None:
        """Return the device_id of the gateway specified in the include_list, if any.

        The 'Known' gateway is the predicted Active gateway, given the known_list.
        The 'Active' gateway is the USB device that is actually Tx/Rx-ing frames.

        The Known gateway ID should be the Active gateway ID, but does not have to
        match.

        Will send a warning if the include_list is configured incorrectly.
        """

        logger = _LOGGER.warning if not disable_warnings else _LOGGER.debug

        explicit_hgis = [
            k
            for k, v in include_list.items()
            if v.get(SZ_CLASS) in (DevType.HGI, DEV_TYPE_MAP[DevType.HGI])
        ]
        implicit_hgis = [
            k
            for k, v in include_list.items()
            if not v.get(SZ_CLASS) and k[:2] == DEV_TYPE_MAP._hex(DevType.HGI)
        ]

        if not explicit_hgis and not implicit_hgis:
            logger(
                f"The {SZ_KNOWN_LIST} SHOULD include exactly one gateway (HGI), "
                f"but does not (it should specify 'class: HGI')"
            )
            return None

        known_hgi = (explicit_hgis if explicit_hgis else implicit_hgis)[0]

        if include_list[known_hgi].get(SZ_CLASS) not in (
            DevType.HGI,
            DEV_TYPE_MAP[DevType.HGI],
        ):
            logger(
                f"The {SZ_KNOWN_LIST} SHOULD include exactly one gateway (HGI): "
                f"{known_hgi} should specify 'class: HGI', as 18: is also used for HVAC"
            )

        elif len(explicit_hgis) > 1:
            logger(
                f"The {SZ_KNOWN_LIST} SHOULD include exactly one gateway (HGI): "
                f"{known_hgi} is the chosen device id (why is there >1 HGI?)"
            )

        else:
            _LOGGER.debug(
                f"The {SZ_KNOWN_LIST} includes exactly one gateway (HGI): {known_hgi}"
            )

        if strict_checking:
            return known_hgi if [known_hgi] == explicit_hgis else None
        return known_hgi

    def _set_active_hgi(self, dev_id: DeviceIdT, by_signature: bool = False) -> None:
        """Set the Active Gateway (HGI) device_id.

        Send a warning if the include list is configured incorrectly.
        """

        assert self._active_hgi is None  # should only be called once

        msg = f"The active gateway '{dev_id}: {{ class: HGI }}' "
        msg += "(by signature)" if by_signature else "(by filter)"

        if dev_id not in self._exclude:
            self._active_hgi = dev_id
            # else: setting self._active_hgi will not help

        if dev_id in self._exclude:
            _LOGGER.error(f"{msg} MUST NOT be in the {SZ_BLOCK_LIST}{TIP}")

        elif dev_id not in self._include:
            _LOGGER.warning(f"{msg} SHOULD be in the (enforced) {SZ_KNOWN_LIST}")
            # self._include.append(dev_id)  # a good idea?

        elif not self.enforce_include:
            _LOGGER.info(f"{msg} is in the {SZ_KNOWN_LIST}, which SHOULD be enforced")

        else:
            _LOGGER.debug(f"{msg} is in the {SZ_KNOWN_LIST}")

    def _is_wanted_addrs(
        self, src_id: DeviceIdT, dst_id: DeviceIdT, sending: bool = False
    ) -> bool:
        """Return True if the packet is not to be filtered out.

        In any one packet, an excluded device_id 'trumps' an included device_id.

        There are two ways to set the Active Gateway (HGI80/evofw3):
        - by signature (evofw3 only), when frame -> packet
        - by known_list (HGI80/evofw3), when filtering packets
        """

        def warn_foreign_hgi(dev_id: DeviceIdT) -> None:
            current_date = dt.now().date()

            if self._foreign_last_run != current_date:
                self._foreign_last_run = current_date
                self._foreign_gwys_lst = []  # reset the list every 24h

            if dev_id in self._foreign_gwys_lst:
                return

            _LOGGER.warning(
                f"Device {dev_id} is potentially a Foreign gateway, "
                f"the Active gateway is {self._active_hgi}, "
                f"alternatively, is it a HVAC device?{TIP}"
            )
            self._foreign_gwys_lst.append(dev_id)

        for dev_id in dict.fromkeys((src_id, dst_id)):  # removes duplicates
            if dev_id in self._exclude:  # problems if incl. active gateway
                return False

            if dev_id == self._active_hgi:  # is active gwy
                continue  # consider: return True (but what if corrupted dst.id?)

            if dev_id in self._include:  # incl. 63:262142 & --:------
                continue

            if sending and dev_id == HGI_DEV_ADDR.id:
                continue

            if self.enforce_include:
                return False

            if dev_id[:2] != DEV_TYPE_MAP.HGI:
                continue

            if self._active_hgi:  # this 18: is not in known_list
                warn_foreign_hgi(dev_id)

        return True

    def pkt_received(self, pkt: Packet) -> None:
        if not self._is_wanted_addrs(pkt.src.id, pkt.dst.id):
            _LOGGER.debug("%s < Packet excluded by device_id filter", pkt)
            return
        super().pkt_received(pkt)

    async def send_cmd(self, cmd: Command, *args: Any, **kwargs: Any) -> Packet:
        if not self._is_wanted_addrs(cmd.src.id, cmd.dst.id, sending=True):
            raise exc.ProtocolError(f"Command excluded by device_id filter: {cmd}")
        return await super().send_cmd(cmd, *args, **kwargs)


class ReadProtocol(_DeviceIdFilterMixin, _BaseProtocol):
    """A protocol that can only receive Packets."""

    def __init__(self, msg_handler: MsgHandlerT, **kwargs: Any) -> None:
        super().__init__(msg_handler, **kwargs)

        self._pause_writing = True

    def connection_made(  # type: ignore[override]
        self, transport: RamsesTransportT, /, *, ramses: bool = False
    ) -> None:
        """Consume the callback if invoked by SerialTransport rather than PortTransport.

        Our PortTransport wraps SerialTransport and will wait for the signature echo
        to be received (c.f. FileTransport) before calling connection_made(ramses=True).
        """
        super().connection_made(transport)

    def resume_writing(self) -> None:
        raise NotImplementedError(f"{self}: The chosen Protocol is Read-Only")

    async def send_cmd(
        self,
        cmd: Command,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        qos: QosParams | None = None,
    ) -> Packet:
        """Raise an exception as the Protocol cannot send Commands."""
        raise NotImplementedError(f"{cmd._hdr}: < this Protocol is Read-Only")


class PortProtocol(_DeviceIdFilterMixin, _BaseProtocol):
    """A protocol that can receive Packets and send Commands +/- QoS (using a FSM)."""

    def __init__(
        self,
        msg_handler: MsgHandlerT,
        disable_qos: bool | None = DEFAULT_DISABLE_QOS,
        **kwargs: Any,
    ) -> None:
        """Add a FSM to the Protocol, to provide QoS."""
        super().__init__(msg_handler, **kwargs)

        self._context = ProtocolContext(self)
        self._disable_qos = disable_qos  # no wait_for_reply

    def __repr__(self) -> str:
        if not self._context:
            return super().__repr__()
        cls = self._context.state.__class__.__name__
        return f"QosProtocol({cls}, len(queue)={self._context._que.qsize()})"

    def connection_made(  # type: ignore[override]
        self, transport: RamsesTransportT, /, *, ramses: bool = False
    ) -> None:
        """Consume the callback if invoked by SerialTransport rather than PortTransport.

        Our PortTransport wraps SerialTransport and will wait for the signature echo
        to be received (c.f. FileTransport) before calling connection_made(ramses=True).
        """

        if not ramses:
            return None

        # if isinstance(transport, MqttTransport):  # HACK
        #     self._context.echo_timeout = 0.5  # HACK: need to move FSM to transport?

        super().connection_made(transport)
        # TODO: needed? self.resume_writing()

        # ROBUSTNESS FIX: Ensure self._transport is set even if the wait future was cancelled
        if self._transport is None:
            _LOGGER.warning(
                f"{self}: Transport bound after wait cancelled (late connection)"
            )
            self._transport = transport

        # Safe access with check (optional but recommended)
        if self._transport:
            self._set_active_hgi(self._transport.get_extra_info(SZ_ACTIVE_HGI))
            self._is_evofw3 = self._transport.get_extra_info(SZ_IS_EVOFW3)

        if not self._context:
            return

        self._context.connection_made(transport)

        if self._pause_writing:
            self._context.pause_writing()
        else:
            self._context.resume_writing()

    def connection_lost(self, err: ExceptionT | None) -> None:  # type: ignore[override]
        """Inform the FSM that the connection with the Transport has been lost."""

        super().connection_lost(err)
        if self._context:
            self._context.connection_lost(err)  # is this safe, when KeyboardInterrupt?

    def pause_writing(self) -> None:
        """Inform the FSM that the Protocol has been paused."""

        super().pause_writing()
        if self._context:
            self._context.pause_writing()

    def resume_writing(self) -> None:
        """Inform the FSM that the Protocol has been resumed."""

        super().resume_writing()
        if self._context:
            self._context.resume_writing()

    def pkt_received(self, pkt: Packet) -> None:
        """Pass any valid/wanted packets to the callback."""

        super().pkt_received(pkt)
        if self._context:
            self._context.pkt_received(pkt)

    async def _send_impersonation_alert(self, cmd: Command) -> None:
        """Send a puzzle packet warning that impersonation is occurring."""

        if _DBG_DISABLE_IMPERSONATION_ALERTS:
            return

        msg = f"{self}: Impersonating device: {cmd.src}, for pkt: {cmd.tx_header}"
        if self._is_evofw3 is False:
            _LOGGER.error(f"{msg}, NB: non-evofw3 gateways can't impersonate!")
        else:
            _LOGGER.info(msg)

        await self._send_cmd(Command._puzzle(msg_type="11", message=cmd.tx_header))

    async def _send_cmd(  # NOTE: QoS wrapped here...
        self,
        cmd: Command,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        qos: QosParams = DEFAULT_QOS,
    ) -> Packet:
        """Wrapper to send a Command with QoS (retries, until success or exception)."""

        # TODO: use a sync function, so we don't have a stack of awaits before the write
        async def send_cmd(kmd: Command) -> None:
            """Wrapper to for self._send_frame(cmd)."""

            await self._send_frame(
                str(kmd), gap_duration=gap_duration, num_repeats=num_repeats
            )

        qos = qos or DEFAULT_QOS

        if _DBG_DISABLE_QOS:  # TODO: should allow echo Packet?
            await send_cmd(cmd)
            return None  # type: ignore[return-value]  # used for test/dev

        # if cmd.code == Code._PUZZ:  # NOTE: not as simple as this
        #     priority = Priority.HIGHEST  # FIXME: hack for _7FFF

        _CODES = (Code._0006, Code._0404, Code._0418, Code._1FC9)  # must have QoS
        # 0006|RQ must have wait_for_reply: (TODO: explain why)
        # 0404|RQ must have wait_for_reply: (TODO: explain why)
        # 0418|RQ must have wait_for_reply: if null log entry, reply has no idx
        # 1FC9|xx must have wait_for_reply and priority (timing critical)

        if self._disable_qos is True or _DBG_DISABLE_QOS:
            qos._wait_for_reply = False
        elif self._disable_qos is None and cmd.code not in _CODES:
            qos._wait_for_reply = False

        # Should do this check before, or after previous block (of non-QoS sends)?
        # if not self._transport._is_wanted_addrs(cmd.src.id, cmd.dst.id, sending=True):
        #     raise exc.ProtocolError(
        #         f"{self}: Failed to send {cmd._hdr}: excluded by list"
        #     )

        assert self._context

        try:
            return await self._context.send_cmd(send_cmd, cmd, priority, qos)
        # except InvalidStateError as err:  # TODO: handle InvalidStateError separately
        #     # reset protocol stack
        except exc.ProtocolError as err:
            _LOGGER.info(f"{self}: Failed to send {cmd._hdr}: {err}")
            raise

    async def send_cmd(
        self,
        cmd: Command,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        qos: QosParams = DEFAULT_QOS,  # max_retries, timeout, wait_for_reply
    ) -> Packet:
        """Send a Command with Qos (with retries, until success or ProtocolError).

        Returns the Command's response Packet or the Command echo if a response is not
        expected (e.g. sending an RP).

        If wait_for_reply is True, return the RQ's RP (or W's I), or raise an exception
        if one doesn't arrive. If it is False, return the echo of the Command only. If
        it is None (the default), act as True for RQs, and False for all other Commands.

        num_repeats is # of times to send the Command, in addition to the fist transmit,
        with gap_duration seconds between each transmission. If wait_for_reply is True,
        then num_repeats is ignored.

        Commands are queued and sent FIFO, except higher-priority Commands are always
        sent first.

        Will raise:
            ProtocolSendFailed: tried to Tx Command, but didn't get echo/reply
            ProtocolError:      didn't attempt to Tx Command for some reason
        """

        assert gap_duration == DEFAULT_GAP_DURATION
        assert 0 <= num_repeats <= 3  # if QoS, only Tx x1, with no repeats

        if qos and not self._context:
            _LOGGER.warning(f"{cmd} < QoS is currently disabled by this Protocol")

        if qos.wait_for_reply and num_repeats:
            _LOGGER.warning(f"{cmd} < num_repeats set to 0, as wait_for_reply is True")
            num_repeats = 0  # the lesser crime over wait_for_reply=False

        pkt = await super().send_cmd(  # may: raise ProtocolError/ProtocolSendFailed
            cmd,
            gap_duration=gap_duration,
            num_repeats=num_repeats,
            priority=priority,
            qos=qos,
        )

        if not pkt:  # HACK: temporary workaround for returning None
            raise exc.ProtocolSendFailed(f"Failed to send command: {cmd} (REPORT THIS)")

        return pkt


RamsesProtocolT: TypeAlias = PortProtocol | ReadProtocol


def protocol_factory(
    msg_handler: MsgHandlerT,
    /,
    *,
    disable_qos: bool | None = DEFAULT_DISABLE_QOS,
    disable_sending: bool | None = False,
    enforce_include_list: bool = False,  # True, None, False
    exclude_list: DeviceListT | None = None,
    include_list: DeviceListT | None = None,
) -> RamsesProtocolT:
    """Create and return a Ramses-specific async packet Protocol."""

    if disable_sending:
        _LOGGER.debug("ReadProtocol: Sending has been disabled")
        return ReadProtocol(
            msg_handler,
            enforce_include_list=enforce_include_list,
            exclude_list=exclude_list,
            include_list=include_list,
        )

    if disable_qos:
        _LOGGER.debug("PortProtocol: QoS has been disabled (will wait_for echos)")

    return PortProtocol(
        msg_handler,
        disable_qos=disable_qos,
        enforce_include_list=enforce_include_list,
        exclude_list=exclude_list,
        include_list=include_list,
    )


async def create_stack(
    msg_handler: MsgHandlerT,
    /,
    *,
    protocol_factory_: Callable[..., RamsesProtocolT] | None = None,
    transport_factory_: Awaitable[RamsesTransportT] | None = None,
    disable_qos: bool | None = DEFAULT_DISABLE_QOS,  # True, None, False
    disable_sending: bool | None = False,
    enforce_include_list: bool = False,
    exclude_list: DeviceListT | None = None,
    include_list: DeviceListT | None = None,
    **kwargs: Any,  # TODO: these are for the transport_factory
) -> tuple[RamsesProtocolT, RamsesTransportT]:
    """Utility function to provide a Protocol / Transport pair.

    Architecture: gwy (client) -> msg (Protocol) -> pkt (Transport) -> HGI/log (or dict)
    - send Commands via awaitable Protocol.send_cmd(cmd)
    - receive Messages via Gateway._handle_msg(msg) callback
    """

    read_only = kwargs.get("packet_dict") or kwargs.get("packet_log")
    disable_sending = disable_sending or read_only

    protocol: RamsesProtocolT = (protocol_factory_ or protocol_factory)(
        msg_handler,
        disable_qos=disable_qos,
        disable_sending=disable_sending,
        enforce_include_list=enforce_include_list,
        exclude_list=exclude_list,
        include_list=include_list,
    )

    transport: RamsesTransportT = await (transport_factory_ or transport_factory)(  # type: ignore[operator]
        protocol, disable_sending=bool(disable_sending), **kwargs
    )

    if not kwargs.get(SZ_PORT_NAME):
        set_logger_timesource(transport._dt_now)
        _LOGGER.warning("Logger datetimes maintained as most recent packet timestamp")

    return protocol, transport
