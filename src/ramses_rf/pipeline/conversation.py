"""RAMSES RF - Conversational State Machine (Request/Reply Tracking).

Tracks L7 Command intents awaiting conversational responses (RP/I) from
network entities, managing timeouts and retries at the application layer.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime as dt
from typing import TYPE_CHECKING, Final

from ramses_rf.commands.builders import build_dto
from ramses_rf.commands.core import Command
from ramses_tx import RP
from ramses_tx.dtos import CommandDTO
from ramses_tx.exceptions import ProtocolSendFailed, ProtocolTimeoutError

if TYPE_CHECKING:
    from ramses_rf.messages import Message

_LOGGER = logging.getLogger(__name__)

DEFAULT_RPLY_TIMEOUT: Final[float] = 1.0
MAX_RETRY_LIMIT: Final[int] = 3


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
    ) -> None:
        """Initialise the ConversationManager.

        :param loop: Optional event loop. Uses running loop if None.
        :type loop: asyncio.AbstractEventLoop | None
        :param default_timeout: Fallback timeout in seconds.
        :type default_timeout: float
        :param max_retries: Maximum number of retries before failing.
        :type max_retries: int
        """
        self._loop = loop or asyncio.get_event_loop()
        self.default_timeout = default_timeout
        self.max_retries = max_retries
        self._pending: dict[str, PendingConversation] = {}

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
        # Key based on target device ID, code, and correlation ID or payload idx
        return f"{intent.dst.id}:{dto.code}:{intent.correlation_id}"

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

        effective_retries = max_retries if max_retries is not None else self.max_retries

        fut: asyncio.Future[Message] = self._loop.create_future()
        key = self._conversation_key(intent, dto)

        pending = PendingConversation(
            intent=intent,
            dto=dto,
            fut=fut,
            timeout=effective_timeout,
            max_retries=effective_retries,
        )

        self._pending[key] = pending
        self._schedule_timeout(key, pending)

        return fut

    def _schedule_timeout(self, key: str, pending: PendingConversation) -> None:
        """Schedule a timeout task for a pending conversation.

        :param key: The tracking key.
        :type key: str
        :param pending: The pending conversation object.
        :type pending: PendingConversation
        """

        async def _timeout_handler() -> None:
            await asyncio.sleep(pending.timeout)
            await self._handle_timeout(key)

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
            # Re-schedule timeout
            self._schedule_timeout(key, pending)
        else:
            _LOGGER.warning(
                "Conversation for %s timed out after %d retries.",
                key,
                pending.max_retries,
            )
            self._pending.pop(key, None)
            if not pending.fut.done():
                action_str = pending.intent.action.value
                pending.fut.set_exception(
                    ProtocolTimeoutError(f"Timeout waiting for reply to {action_str}")
                )

    def process_msg(self, msg: Message) -> bool:
        """Evaluate an incoming Message against pending conversations.

        :param msg: The incoming message to evaluate.
        :type msg: Message
        :returns: True if matched, False otherwise.
        :rtype: bool
        """
        if msg.verb != RP:
            return False

        matched_key: str | None = None
        matched_pending: PendingConversation | None = None

        for key, pending in list(self._pending.items()):
            # Check source device matching
            if msg.src.id != pending.intent.dst.id:
                continue

            # Check code matching
            if str(msg.code) != pending.dto.code:
                continue

            matched_key = key
            matched_pending = pending
            break

        if matched_key is None or matched_pending is None:
            return False

        # Cancel timer task
        if matched_pending.timer_task and not matched_pending.timer_task.done():
            matched_pending.timer_task.cancel()

        # Remove from pending
        self._pending.pop(matched_key, None)

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
        for pending in self._pending.values():
            if pending.timer_task and not pending.timer_task.done():
                pending.timer_task.cancel()
            if not pending.fut.done():
                pending.fut.set_exception(
                    ProtocolSendFailed("Conversation manager cancelled")
                )
        self._pending.clear()
