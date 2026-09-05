#!/usr/bin/env python3
"""RAMSES RF - RAMSES-II compatible packet protocol implementations.

This module provides the concrete Protocol classes (ReadProtocol and
PortProtocol) that bind the transport, command transmission, and base
filters together.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final, TypeAlias

from ..address import HGI_DEVICE_ID
from ..const import (
    DEFAULT_DISABLE_QOS,
    DEFAULT_ECHO_TIMEOUT,
    DEFAULT_GAP_DURATION,
    DEFAULT_NUM_REPEATS,
    MAX_GAP_DURATION,
    MAX_NUM_REPEATS,
    MAX_RETRY_LIMIT,
    MAX_SEND_TIMEOUT,
    RQ,
    SZ_ACTIVE_HGI,
    SZ_IS_EVOFW3,
    Priority,
)
from ..dtos import CommandDTO
from ..exceptions import (
    PacketPayloadInvalid,
    ProtocolError,
    ProtocolSendFailed,
    ProtocolTimeoutError,
    TransportError,
)
from ..packet import Packet
from ..routing import RoutedCommand, RouteRequest, SourcePolicy, WriteOutcome
from ..typing import DeviceIdT, HeaderT, MsgHandlerT, QosParams
from .base import DEFAULT_QOS, _DeviceIdFilterMixin

_DBG_DISABLE_IMPERSONATION_ALERTS: Final[bool] = False
_DBG_DISABLE_QOS: Final[bool] = False

_LOGGER = logging.getLogger(__name__)


class ReadProtocol(_DeviceIdFilterMixin):
    """A protocol that can only receive Packets.

    This protocol operates in read-only mode and rejects any attempts
    to transmit commands or resume writing.
    """

    def __init__(
        self,
        msg_handler: MsgHandlerT,
        /,
        *,
        enforce_include_list: bool = False,
        exclude_list: list[str] | None = None,
        include_list: list[str] | None = None,
        hgi_id: str | None = None,
    ) -> None:
        """Initialize the Read-Only protocol.

        :param msg_handler: The callback invoked when a valid message
            is processed.
        :type msg_handler: MsgHandlerT
        :param enforce_include_list: Flag to enforce include list
            strictly.
        :type enforce_include_list: bool
        :param exclude_list: List of device IDs to block.
        :type exclude_list: list[str] | None
        :param include_list: List of device IDs to allow.
        :type include_list: list[str] | None
        :param hgi_id: Active HGI device ID.
        :type hgi_id: str | None
        """
        super().__init__(
            msg_handler,
            disable_warnings=True,
            enforce_include_list=enforce_include_list,
            exclude_list=exclude_list,
            include_list=include_list,
            hgi_id=hgi_id,
        )
        self._pause_writing = True

    def connection_made(
        self, transport: Any, /, *, ramses: bool = False
    ) -> None:
        """Consume callback if invoked by SerialTransport rather than PortTransport.

        Our PortTransport wraps SerialTransport and will wait for the signature
        echo to be received (c.f. FileTransport) before calling
        connection_made(ramses=True).

        :param transport: The underlying transport instance.
        :type transport: Any
        :param ramses: Flag indicating if invoked by PortTransport after
            handshake.
        :type ramses: bool
        """
        super().connection_made(transport)

    def resume_writing(self) -> None:
        """No-op for read-only protocols.

        ``asyncio`` calls this when the transport's write buffer drains.
        Read-only protocols (eavesdrop/listen mode) never write, but the
        transport may still call this during connection setup. Silently
        ignore rather than raising — the exception broke CLI listen mode
        over MQTT.
        """
        _LOGGER.debug("%s: resume_writing (read-only, ignoring)", self)

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
        """Raise an exception as the Protocol cannot send Commands.

        :param command: Outbound command DTO attempted to send.
        :type command: CommandDTO
        :param gap_duration: Duration between repeat frames in seconds.
        :type gap_duration: float
        :param num_repeats: Number of repeat frames.
        :type num_repeats: int
        :param priority: Transmission priority.
        :type priority: Priority
        :param qos: QoS parameters.
        :type qos: QosParams | None
        :returns: Never returns normally.
        :rtype: Packet
        :raises NotImplementedError: Always raised as ReadProtocol is
            read-only.
        """
        raise NotImplementedError(
            f"{command.verb}|{command.code}: < this Protocol is Read-Only"
        )


class PortProtocol(_DeviceIdFilterMixin):
    """A protocol that can receive Packets and transmit Commands with QoS.

    Implements a priority-queued transceiver that coordinates outbound
    command transmission, link-layer exponential backoff retries, and
    matching hardware echo packet correlation.
    """

    def __init__(
        self,
        msg_handler: MsgHandlerT,
        /,
        *,
        disable_qos: bool | None = DEFAULT_DISABLE_QOS,
        enforce_include_list: bool = False,
        exclude_list: list[str] | None = None,
        include_list: list[str] | None = None,
        hgi_id: str | None = None,
        echo_timeout: float = DEFAULT_ECHO_TIMEOUT,
        max_retry_limit: int = MAX_RETRY_LIMIT,
        send_timeout_limit: float = MAX_SEND_TIMEOUT,
    ) -> None:
        """Initialize the PortProtocol transceiver.

        :param msg_handler: The callback invoked when a valid message
            is processed.
        :type msg_handler: MsgHandlerT
        :param disable_qos: Flag to globally disable QoS capabilities.
        :type disable_qos: bool | None
        :param enforce_include_list: Flag to enforce include list
            strictly.
        :type enforce_include_list: bool
        :param exclude_list: List of device IDs to block.
        :type exclude_list: list[str] | None
        :param include_list: List of device IDs to allow.
        :type include_list: list[str] | None
        :param hgi_id: Active HGI device ID.
        :type hgi_id: str | None
        :param echo_timeout: Default timeout in seconds for echo packet.
        :type echo_timeout: float
        :param max_retry_limit: Maximum number of link-layer retries.
        :type max_retry_limit: int
        :param send_timeout_limit: Maximum global timeout duration in
            seconds.
        :type send_timeout_limit: float
        """
        super().__init__(
            msg_handler,
            disable_warnings=False,
            enforce_include_list=enforce_include_list,
            exclude_list=exclude_list,
            include_list=include_list,
            hgi_id=hgi_id,
        )
        self._disable_qos = disable_qos
        self._echo_timeout = echo_timeout
        self._max_retry_limit = max_retry_limit
        self._send_timeout_limit = send_timeout_limit

        self._queue: asyncio.PriorityQueue[
            tuple[
                int,
                int,
                CommandDTO,
                float,
                int,
                QosParams,
                asyncio.Future[Packet],
                SourcePolicy,
            ]
        ] = asyncio.PriorityQueue()
        self._seq = 0

        self._is_active = False
        self._tx_worker_task: asyncio.Task[None] | None = None
        self._resume_event = asyncio.Event()

        self._pending_cmd: CommandDTO | None = None
        self._pending_fut: asyncio.Future[Packet] | None = None
        self._pending_routed: RoutedCommand | None = None

    def __repr__(self) -> str:
        """Return an unambiguous string representation of this object.

        :returns: String representation of the PortProtocol instance.
        :rtype: str
        """
        status = "Active" if getattr(self, "_is_active", False) else "Inactive"
        q_size = self.qsize if hasattr(self, "_queue") else 0
        return f"PortProtocol({status}, qsize={q_size})"

    @property
    def qsize(self) -> int:
        """Return the current number of queued commands.

        :returns: Number of commands currently queued for transmission.
        :rtype: int
        """
        return self._queue.qsize()

    @property
    def is_sending(self) -> bool:
        """Return True if currently waiting for command echo transmission.

        :returns: True if a command is actively awaiting an echo packet.
        :rtype: bool
        """
        return self._pending_fut is not None and not self._pending_fut.done()

    @property
    def echo_timeout(self) -> float:
        """Return default echo timeout in seconds.

        :returns: Default timeout duration in seconds for echo packets.
        :rtype: float
        """
        return self._echo_timeout

    @echo_timeout.setter
    def echo_timeout(self, value: float) -> None:
        self._echo_timeout = value

    @property
    def max_retry_limit(self) -> int:
        """Return maximum link-layer retries allowed.

        :returns: Maximum number of retry attempts for transmission.
        :rtype: int
        """
        return self._max_retry_limit

    @max_retry_limit.setter
    def max_retry_limit(self, value: int) -> None:
        self._max_retry_limit = value

    @property
    def send_timeout_limit(self) -> float:
        """Return maximum overall send timeout limit.

        :returns: Upper bound for send timeout in seconds.
        :rtype: float
        """
        return self._send_timeout_limit

    def connection_made(
        self, transport: Any, /, *, ramses: bool = False
    ) -> None:
        """Consume callback if invoked by SerialTransport rather than PortTransport.

        Our PortTransport wraps SerialTransport and will wait for the signature
        echo to be received (c.f. FileTransport) before calling
        connection_made(ramses=True).

        :param transport: The underlying transport instance.
        :type transport: Any
        :param ramses: Flag indicating if invoked by PortTransport after
            handshake.
        :type ramses: bool
        """
        if not ramses:
            return None

        super().connection_made(transport)

        # ROBUSTNESS FIX: Ensure self._transport is set even if the wait
        # future was cancelled
        if self._transport is None:
            _LOGGER.warning(
                "%s: Transport bound after wait cancelled (late connection)",
                self,
            )
            self._transport = transport

        # Safe access with check (optional but recommended)
        if self._transport:
            self._set_active_hgi(self._transport.get_extra_info(SZ_ACTIVE_HGI))
            self._is_evofw3 = self._transport.get_extra_info(SZ_IS_EVOFW3)

        self._is_active = True
        self._resume_event.set()

        if self._tx_worker_task is None or self._tx_worker_task.done():
            self._tx_worker_task = self._loop.create_task(
                self._tx_worker(), name="PortProtocol._tx_worker()"
            )

    def connection_lost(self, error: Exception | None) -> None:
        """Handle transport connection lost event and clean up queue.

        :param error: The exception causing connection loss, or None if
            closed cleanly.
        :type error: Exception | None
        """
        super().connection_lost(error)
        self._is_active = False
        self._resume_event.clear()

        if self._tx_worker_task and not self._tx_worker_task.done():
            self._tx_worker_task.cancel()
            self._tx_worker_task = None

        exc_val = TransportError("Connection lost") if error is None else error

        if self._pending_fut and not self._pending_fut.done():
            self._pending_fut.set_exception(exc_val)
            self._pending_fut = None
            self._pending_cmd = None
            self._pending_routed = None

        while not self._queue.empty():
            try:
                *_, fut, _sp = self._queue.get_nowait()
                if not fut.done():
                    fut.set_exception(exc_val)
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

    def pause_writing(self) -> None:
        """Pause writing when transport buffer is full."""
        super().pause_writing()
        self._resume_event.clear()

    def resume_writing(self) -> None:
        """Resume writing when transport buffer drains."""
        super().resume_writing()
        if self._is_active:
            self._resume_event.set()

    def _packet_received(self, packet: Packet) -> None:
        """Handle received packet and resolve pending echo future."""
        super()._packet_received(packet)

        if self._pending_fut is None or self._pending_fut.done():
            return

        if self._pending_cmd is None:
            return

        if getattr(packet, "_is_echo", False) or self._is_matching_echo(
            packet, self._pending_cmd
        ):
            self._pending_fut.set_result(packet)

    def _is_matching_echo(
        self, packet: Packet, expected_cmd: CommandDTO
    ) -> bool:
        """Check if incoming packet matches the pending command echo."""
        try:
            packet_hdr = packet._hdr
        except PacketPayloadInvalid:
            return False

        if HGI_DEVICE_ID in packet_hdr:
            assert packet._hdr_ is not None
            packet__hdr = HeaderT(
                packet._hdr_.replace(HGI_DEVICE_ID, self.hgi_id)
            )
        else:
            packet__hdr = packet_hdr

        return packet__hdr == expected_cmd.tx_header

    async def _tx_worker(self) -> None:
        """Worker loop processing queued commands sequentially."""
        while self._is_active:
            try:
                await self._resume_event.wait()

                (
                    priority_val,
                    seq,
                    cmd,
                    gap_duration,
                    num_repeats,
                    qos,
                    fut,
                    source_policy,
                ) = await self._queue.get()

                if fut.cancelled():
                    self._queue.task_done()
                    continue

                await self._process_tx_item(
                    cmd, gap_duration, num_repeats, qos, fut, source_policy
                )
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as err:
                _LOGGER.exception("Unexpected error in tx worker: %s", err)

    async def _process_tx_item(
        self,
        cmd: CommandDTO,
        gap_duration: float,
        num_repeats: int,
        qos: QosParams,
        fut: asyncio.Future[Packet],
        source_policy: SourcePolicy = SourcePolicy.GATEWAY,
    ) -> None:
        """Transmit a command with link-layer echo backoff retries.

        Uses the pre-serialization routing API (PR 2): the command is
        wrapped in a :class:`RouteRequest`, the transport selects a
        child and resolves the source address, and the final routed
        DTO becomes the pending QoS command.  Each QoS attempt
        prepares a new route (which may select a different child if
        route quality changed) and serializes the final DTO once.
        """
        max_retries = (
            qos.max_retries
            if qos.max_retries is not None
            else self._max_retry_limit
        )
        echo_timeout = (
            qos.timeout if qos.timeout is not None else self._echo_timeout
        )
        backoff_factor = getattr(qos, "backoff", 1.5) or 1.5

        tx_count = 0
        current_timeout = echo_timeout

        try:
            while tx_count <= max_retries:
                if fut.cancelled() or not self._is_active:
                    break

                tx_count += 1

                # Prepare the route for this attempt.  Each QoS retry
                # may select a different child if route quality changed.
                # The source policy is determined in send_cmd() based
                # on whether the command source is the gateway
                # placeholder (GATEWAY) or an intentional non-gateway
                # source (PRESERVE, e.g. faked-device commands).
                request = RouteRequest(
                    command=cmd,
                    source_policy=source_policy,
                )
                try:
                    if self._transport is None:
                        raise ProtocolSendFailed("Transport is not connected")
                    routed = self._transport.prepare_command(request)
                except Exception as prep_err:
                    if not fut.done():
                        fut.set_exception(
                            ProtocolSendFailed(
                                f"Failed to prepare route: {prep_err}"
                            )
                        )
                    return

                # Set pending command from the final routed DTO so QoS
                # echo matching uses the actual source-patched command.
                self._pending_cmd = routed.command
                self._pending_fut = fut
                self._pending_routed = routed

                # Serialize the final DTO once for this attempt.
                frame = str(routed.command)

                try:
                    await self._send_routed_frame(
                        routed,
                        frame,
                        num_repeats=num_repeats,
                        gap_duration=gap_duration,
                    )
                except Exception as send_err:
                    if not fut.done():
                        fut.set_exception(
                            ProtocolSendFailed(
                                f"Failed to transmit frame: {send_err}"
                            )
                        )
                    return

                try:
                    await asyncio.wait_for(
                        asyncio.shield(fut), timeout=current_timeout
                    )
                    return
                except TimeoutError:
                    if tx_count <= max_retries:
                        current_timeout = min(
                            current_timeout * backoff_factor,
                            self._send_timeout_limit,
                        )
                        _LOGGER.info(
                            "Echo timeout for %s (attempt %s/%s), retrying in %s s",
                            cmd,
                            tx_count,
                            max_retries,
                            current_timeout,
                        )
                        continue

                    if not fut.done():
                        fut.set_exception(
                            ProtocolTimeoutError(
                                f"Echo timeout expired after {max_retries} retries for {cmd}"
                            )
                        )
                    return
        finally:
            self._pending_cmd = None
            self._pending_fut = None
            self._pending_routed = None

    async def _send_routed_frame(
        self,
        routed: RoutedCommand,
        frame: str,
        num_repeats: int = 0,
        gap_duration: float = 0.0,
    ) -> None:
        """Send a routed frame via the transport's routing API.

        Applies outbound regex to the serialized frame, then dispatches
        via ``write_routed()``.  Transport-level repeats reuse the same
        routed command and frame.

        :param routed: The routed command from ``prepare_command()``.
        :param frame: The serialized frame.
        :param num_repeats: Number of additional repeat transmissions.
        :param gap_duration: Gap between repeats in seconds.
        """
        if self._transport is None:
            raise ProtocolSendFailed("Transport is not connected")

        # Apply outbound regex to the serialized frame.
        frame = self._apply_regex(frame, self._outbound_regex)

        outcome = await self._transport.write_routed(routed, frame)
        if outcome is WriteOutcome.AMBIGUOUS:
            raise ProtocolSendFailed(
                f"Ambiguous write outcome for frame: {frame}"
            )
        for _ in range(num_repeats - 1):
            await asyncio.sleep(gap_duration)
            await self._transport.write_routed(routed, frame)

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
        """Send a Command with QoS (with retries, until success or ProtocolError).

        Returns the Command's response Packet or the Command echo.

        num_repeats is the number of times to send the Command, in addition to
        the first transmit, with gap_duration seconds between each transmission.

        Commands are queued and sent FIFO, except higher-priority Commands are
        always sent first.

        :param command: Outbound command DTO to transmit.
        :type command: CommandDTO
        :param gap_duration: Duration in seconds between duplicate frame sends.
        :type gap_duration: float
        :param num_repeats: Number of repeat frames to send.
        :type num_repeats: int
        :param priority: Hardware queue priority for transmission.
        :type priority: Priority
        :param qos: Quality of service configuration parameters.
        :type qos: QosParams | None
        :returns: Echo packet returned by the hardware transceiver.
        :rtype: Packet
        :raises ProtocolTimeoutError: If global send timer expires before
            receiving echo packet.
        :raises ProtocolSendFailed: If hardware fails to transmit frame or
            packet is not returned.
        :raises ProtocolError: If command is excluded by device ID filtering.
        """
        assert 0 <= gap_duration <= MAX_GAP_DURATION, (
            "Out of range: gap_duration"
        )
        assert 0 <= num_repeats <= MAX_NUM_REPEATS, "Out of range: num_repeats"

        # Patch command with actual HGI ID if it uses the default placeholder.
        # PortProtocol.send_cmd overrides _BaseProtocol.send_cmd (which does this
        # patching at base.py:309), so we must replicate it here. Without this,
        # all commands go out with the 18:000730 placeholder instead of the real
        # HGI ID. See ramses_cc#757.
        patched_cmd = self._patch_cmd_if_needed(command)

        # Check if impersonation alert is needed when addr1 is not the active HGI
        if patched_cmd.addr1 != self.hgi_id:
            await self._send_impersonation_alert(patched_cmd)

        # Manual filter check to avoid calling super().send_cmd(), which fails
        if not self._is_wanted_addrs(
            DeviceIdT(patched_cmd.addr1),
            DeviceIdT(patched_cmd.addr2),
            sending=True,
        ):
            raise ProtocolError(
                f"Command excluded by device_id filter: {patched_cmd}"
            )

        qos = qos or DEFAULT_QOS
        # RQ commands and commands with QoS retry limits don't use repeat blasts
        if patched_cmd.verb.strip() == RQ or (
            qos is not None and qos.max_retries > 0
        ):
            num_repeats = 0

        # Determine the source policy for pre-serialization routing.
        # If the patched source is the gateway placeholder or the
        # active HGI ID, the transport may substitute the source to
        # match the selected child (GATEWAY).  Otherwise, the source
        # is an intentional non-gateway address (e.g. faked-device
        # commands) and must be preserved as-is (PRESERVE).
        from ..address import HGI_DEV_ADDR

        if patched_cmd.addr1 in (HGI_DEV_ADDR.id, self.hgi_id):
            source_policy = SourcePolicy.GATEWAY
        else:
            source_policy = SourcePolicy.PRESERVE

        if _DBG_DISABLE_QOS:
            # Debug path: skip QoS, send directly via routing API.
            request = RouteRequest(
                command=patched_cmd, source_policy=source_policy
            )
            try:
                if self._transport is None:
                    raise ProtocolSendFailed("Transport is not connected")
                routed = self._transport.prepare_command(request)
                frame = str(routed.command)
                await self._send_routed_frame(
                    routed,
                    frame,
                    num_repeats=num_repeats,
                    gap_duration=gap_duration,
                )
            except Exception as send_err:
                raise ProtocolSendFailed(
                    f"Failed to transmit frame: {send_err}"
                ) from send_err
            return None  # type: ignore[return-value]

        fut: asyncio.Future[Packet] = self._loop.create_future()
        self._seq += 1

        priority_order = {
            Priority.HIGH: 0,
            Priority.DEFAULT: 1,
            Priority.LOW: 2,
        }.get(priority, 1)

        await self._queue.put(
            (
                priority_order,
                self._seq,
                patched_cmd,
                gap_duration,
                num_repeats,
                qos,
                fut,
                source_policy,
            )
        )

        try:
            return await fut
        except ProtocolTimeoutError as err:
            _LOGGER.warning(
                "%s: Send timed out for %s|%s: %s",
                self,
                patched_cmd.verb,
                patched_cmd.code,
                err,
            )
            raise
        except ProtocolError as err:
            _LOGGER.info(
                "%s: Failed to send %s|%s: %s",
                self,
                patched_cmd.verb,
                patched_cmd.code,
                err,
            )
            raise


RamsesProtocolT: TypeAlias = PortProtocol | ReadProtocol
