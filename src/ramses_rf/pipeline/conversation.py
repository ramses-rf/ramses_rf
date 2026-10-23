"""RAMSES RF - Conversational State Machine (Request/Reply Tracking).

Tracks L7 Command intents awaiting conversational responses (RP/I) from
network entities, managing timeouts and retries at the application layer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime as dt
from typing import TYPE_CHECKING, Final
from unittest.mock import Mock

from ramses_rf.commands.builders import build_dto
from ramses_rf.commands.core import Command
from ramses_rf.routing import extract_context_value
from ramses_tx import I_, RP, RQ, W_, Packet
from ramses_tx.dtos import CommandDTO
from ramses_tx.exceptions import ProtocolSendFailed, ProtocolTimeoutError

if TYPE_CHECKING:
    from ramses_rf.messages import Message

_LOGGER = logging.getLogger(__name__)

DEFAULT_RPLY_TIMEOUT: Final[float] = 1.0
MAX_RETRY_LIMIT: Final[int] = 3

SendCallbackT = Callable[[CommandDTO], Awaitable[Packet]]

#: Single-flight signature: the full frame identity of an intent's
#: DTO (verb, addr1, addr2, addr3, code, payload).  Identical
#: signatures produce byte-identical frames (ramses-rf/ramses_cc#1336).
type _ConvSigT = tuple[str, str, str, str, str, str]


@dataclass
class PendingConversation:
    """Tracks an in-flight command intent awaiting a response.

    :param intent: The high-level application intent.
    :type intent: Command
    :param dto: The translated L3 payload.
    :type dto: CommandDTO
    :param fut: The future that will be resolved upon receiving a reply.
    :type fut: asyncio.Future[Message]
    :param timeout: Seconds to wait before timing out.
    :type timeout: float
    :param max_retries: Maximum allowed retry attempts.
    :type max_retries: int
    :param retry_count: Current number of attempts performed.
    :type retry_count: int
    :param created_at: Timestamp when tracking started.
    :type created_at: dt
    """

    intent: Command
    dto: CommandDTO
    fut: asyncio.Future[Message]
    timeout: float = DEFAULT_RPLY_TIMEOUT
    max_retries: int = MAX_RETRY_LIMIT
    retry_count: int = 0
    created_at: dt = field(default_factory=dt.now)
    timer_task: asyncio.Task[None] | None = None
    #: Frame signature for single-flight coalescing (RQ only).
    sig: _ConvSigT | None = None


class ConversationManager:
    """Manages L7 Request/Reply (RQ/RP) conversations and retries.

    Correlates outbound commands with inbound decoded Messages from the L7
    dispatcher queue, replacing legacy L3 regex matching.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        *,
        default_timeout: float = DEFAULT_RPLY_TIMEOUT,
        max_retries: int = MAX_RETRY_LIMIT,
        send_func: SendCallbackT | None = None,
    ) -> None:
        """Initialise the ConversationManager.

        :param loop: Optional event loop. Uses running loop if None.
        :type loop: asyncio.AbstractEventLoop | None
        :param default_timeout: Fallback timeout in seconds.
        :type default_timeout: float
        :param max_retries: Maximum number of retries before failing.
        :type max_retries: int
        :param send_func: Callback for physical RF transmission retries.
        :type send_func: SendCallbackT | None
        """
        self._loop = loop or asyncio.get_event_loop()
        self.default_timeout = default_timeout
        self.max_retries = max_retries
        self._send_func = send_func
        self._pending: dict[str, PendingConversation] = {}
        # Single-flight index: frame signature -> conversation key.
        # Only RQ intents are registered — read requests are safe to
        # coalesce, and identical back-to-back frames are dropped by
        # link-layer dedup anyway (ramses-rf/ramses_cc#1336).
        self._sig_index: dict[_ConvSigT, str] = {}

    @property
    def pending_count(self) -> int:
        """Return the number of currently pending conversations.

        :returns: Count of in-flight conversations.
        :rtype: int
        """
        return len(self._pending)

    def _conversation_key(self, intent: Command, dto: CommandDTO) -> str:
        """Generate a unique tracking key for an intent.

        :param intent: The command intent.
        :type intent: Command
        :param dto: The translated DTO.
        :type dto: CommandDTO
        :returns: A unique lookup key.
        :rtype: str
        """
        # Key based on target device ID, code, and correlation ID or payload index
        return f"{intent.dst.id}:{dto.code}:{intent.correlation_id}"

    @staticmethod
    def _intent_signature(dto: CommandDTO) -> _ConvSigT | None:
        """Return the single-flight signature for an RQ DTO.

        Only RQ intents are coalesced: identical read requests are
        interchangeable, while repeated writes may be intentional.

        :param dto: The compiled command DTO.
        :type dto: CommandDTO
        :returns: The frame signature, or None when not coalescible.
        :rtype: tuple[str, str, str, str, str, str] | None
        """
        if dto.verb != RQ:
            return None
        return (
            dto.verb,
            dto.addr1,
            dto.addr2,
            dto.addr3,
            dto.code,
            dto.payload,
        )

    def find_pending_reply(
        self, dto: CommandDTO
    ) -> asyncio.Future[Message] | None:
        """Return the reply future of an identical in-flight request.

        Concurrent identical RQ intents would transmit byte-identical
        frames; on a multi-HGI pool the second frame's echo and reply
        are dropped by dedup, stalling the send queue
        (ramses-rf/ramses_cc#1336).  Sharing the pending conversation
        avoids the duplicate transmission entirely.

        :param dto: The compiled command DTO about to be sent.
        :type dto: CommandDTO
        :returns: The pending conversation's future, or None.
        :rtype: asyncio.Future[Message] | None
        """
        sig = self._intent_signature(dto)
        if sig is None:
            return None
        key = self._sig_index.get(sig)
        if key is None:
            return None
        pending = self._pending.get(key)
        if pending is None or pending.fut.done():
            return None
        _LOGGER.debug(
            "Coalescing identical %s %s->%s onto pending %s",
            dto.verb,
            dto.addr1,
            dto.addr2,
            key,
        )
        return pending.fut

    def _pop_pending(self, key: str) -> PendingConversation | None:
        """Remove a pending conversation and its signature mapping.

        :param key: The tracking key to remove.
        :type key: str
        :returns: The removed conversation, or None.
        :rtype: PendingConversation | None
        """
        pending = self._pending.pop(key, None)
        if (
            pending is not None
            and pending.sig is not None
            and self._sig_index.get(pending.sig) == key
        ):
            del self._sig_index[pending.sig]
        return pending

    async def track_intent(
        self,
        intent: Command,
        dto: CommandDTO | None = None,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> asyncio.Future[Message]:
        """Start tracking a command intent for a conversational reply.

        :param intent: The application command intent.
        :type intent: Command
        :param dto: Optional pre-built CommandDTO. If None, builds DTO.
        :type dto: CommandDTO | None
        :param timeout: Custom timeout in seconds.
        :type timeout: float | None
        :param max_retries: Custom maximum retries.
        :type max_retries: int | None
        :returns: A future that will resolve with the response Message.
        :rtype: asyncio.Future[Message]
        """
        if dto is None:
            dto = build_dto(intent)

        if timeout is not None:
            effective_timeout = timeout
        elif intent.timeout != DEFAULT_RPLY_TIMEOUT:
            effective_timeout = intent.timeout
        else:
            effective_timeout = self.default_timeout

        effective_retries = (
            max_retries if max_retries is not None else self.max_retries
        )

        fut: asyncio.Future[Message] = self._loop.create_future()
        fut.add_done_callback(
            lambda f: f.exception() if not f.cancelled() else None
        )
        key = self._conversation_key(intent, dto)

        if key in self._pending:
            old_pending = self._pop_pending(key)
            assert old_pending is not None
            if old_pending.timer_task and not old_pending.timer_task.done():
                old_pending.timer_task.cancel()
            if not old_pending.fut.done():
                old_pending.fut.set_exception(
                    ProtocolSendFailed(
                        f"Conversation {key} superseded by new request"
                    )
                )

        pending = PendingConversation(
            intent=intent,
            dto=dto,
            fut=fut,
            timeout=effective_timeout,
            max_retries=effective_retries,
            sig=self._intent_signature(dto),
        )

        self._pending[key] = pending
        if pending.sig is not None:
            self._sig_index[pending.sig] = key
        self._schedule_timeout(key, pending)

        return fut

    def _schedule_timeout(
        self, key: str, pending: PendingConversation
    ) -> None:
        """Schedule a timeout task for a pending conversation.

        :param key: The tracking key.
        :type key: str
        :param pending: The pending conversation object.
        :type pending: PendingConversation
        """

        async def _timeout_handler() -> None:
            try:
                await asyncio.sleep(pending.timeout)
                await self._handle_timeout(key)
            except asyncio.CancelledError:
                pass

        pending.timer_task = self._loop.create_task(_timeout_handler())

    async def _handle_timeout(self, key: str) -> None:
        """Process a timeout for a pending conversation.

        :param key: The tracking key of the timed-out conversation.
        :type key: str
        """
        pending = self._pending.get(key)
        if pending is None:
            return

        if pending.retry_count < pending.max_retries:
            pending.retry_count += 1
            _LOGGER.debug(
                "Conversation timeout for %s (attempt %d/%d), retrying...",
                key,
                pending.retry_count,
                pending.max_retries,
            )
            if self._send_func is not None:
                try:
                    await self._send_func(pending.dto)
                except Exception as err:
                    _LOGGER.warning(
                        "Failed to re-send DTO during retry for %s: %s",
                        key,
                        err,
                    )
            # Re-schedule timeout
            self._schedule_timeout(key, pending)
        else:
            _LOGGER.warning(
                "Conversation for %s timed out after %d retries.",
                key,
                pending.max_retries,
            )
            self._pop_pending(key)
            if not pending.fut.done():
                action_str = pending.intent.action.value
                pending.fut.set_exception(
                    ProtocolTimeoutError(
                        f"Timeout waiting for reply to {action_str}"
                    )
                )

    def process_msg(self, msg: Message) -> bool:
        """Evaluate an incoming Message against pending conversations.

        :param msg: The incoming message to evaluate.
        :type msg: Message
        :returns: True if matched, False otherwise.
        :rtype: bool
        """
        matched_key: str | None = None
        matched_pending: PendingConversation | None = None

        for key, pending in list(self._pending.items()):
            # Check source device matching
            if msg.src.id != pending.intent.dst.id:
                continue

            # Check code matching
            if str(msg.code) != pending.dto.code:
                continue

            # Check verb matching: RP is always valid; I is valid for W commands
            # (e.g. Evohome DHW acknowledges W 1F41 with I 1F41, not RP 1F41).
            # See issue 873.
            if msg.verb != RP and not (
                msg.verb == I_ and pending.dto.verb == W_
            ):
                continue

            # Check sub-payload context (index/zone/domain/msg_id) matching
            message_context = msg.context.value
            if message_context is not None and not isinstance(
                message_context, Mock
            ):
                command_context = extract_context_value(
                    pending.dto.payload,
                    code=pending.dto.code,
                )
                if message_context != command_context:
                    continue

            matched_key = key
            matched_pending = pending
            break

        if matched_key is None or matched_pending is None:
            return False

        # Cancel timer task
        if (
            matched_pending.timer_task
            and not matched_pending.timer_task.done()
        ):
            matched_pending.timer_task.cancel()

        # Remove from pending
        self._pop_pending(matched_key)

        # Resolve future
        if not matched_pending.fut.done():
            matched_pending.fut.set_result(msg)

        _LOGGER.debug(
            "Conversation %s successfully resolved by message %s",
            matched_key,
            msg,
        )
        return True

    def cancel_all(self) -> None:
        """Cancel all pending conversations and clear tracking state."""
        for key, pending in list(self._pending.items()):
            if pending.timer_task and not pending.timer_task.done():
                pending.timer_task.cancel()
            if not pending.fut.done():
                _LOGGER.debug(
                    "Cancelling pending conversation %s due to gateway shutdown",
                    key,
                )
                pending.fut.set_exception(
                    ProtocolSendFailed(
                        f"Conversation {key} cancelled on gateway teardown"
                    )
                )
        self._pending.clear()
        self._sig_index.clear()
