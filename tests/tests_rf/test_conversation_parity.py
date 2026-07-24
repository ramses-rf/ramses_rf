"""Tests for L7 ConversationManager (Request/Reply tracking and timeouts).

Exempt from formal docstrings under repository rules.
Applies AAA (Arrange, Act, Assert) pattern strictly.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from ramses_rf.address import Address
from ramses_rf.commands.builders import build_dto
from ramses_rf.commands.core import Command
from ramses_rf.enums import Action
from ramses_rf.pipeline.conversation import ConversationManager
from ramses_tx import RP
from ramses_tx.exceptions import ProtocolTimeoutError


def _create_mock_message(
    verb: str = "RP",
    code: str = "10A0",
    src_id: str = "01:078710",
) -> MagicMock:
    msg = MagicMock()
    msg.verb = verb
    msg.code = code
    msg.src = Address(src_id)
    return msg


@pytest.mark.asyncio
async def test_conversation_manager_successful_match() -> None:
    # Arrange
    loop = asyncio.get_running_loop()
    manager = ConversationManager(loop=loop, default_timeout=1.0, max_retries=2)
    intent = Command(
        src=Address("18:000730"),
        dst=Address("01:078710"),
        action=Action.SET_TEMPERATURE,
        data={"zone_idx": "00", "setpoint": 21.0},
    )
    dto = build_dto(intent)

    # Act
    fut = await manager.track_intent(intent, dto)
    assert manager.pending_count == 1

    reply_msg = _create_mock_message(verb=RP, code=dto.code, src_id="01:078710")
    matched = manager.process_msg(reply_msg)

    # Assert
    assert matched is True
    assert manager.pending_count == 0
    assert fut.done()
    assert fut.result() == reply_msg


@pytest.mark.asyncio
async def test_conversation_manager_ignores_mismatched_src() -> None:
    # Arrange
    loop = asyncio.get_running_loop()
    manager = ConversationManager(loop=loop, default_timeout=1.0, max_retries=2)
    intent = Command(
        src=Address("18:000730"),
        dst=Address("01:078710"),
        action=Action.SET_TEMPERATURE,
        data={"zone_idx": "00", "setpoint": 21.0},
    )
    dto = build_dto(intent)

    # Act
    fut = await manager.track_intent(intent, dto)
    mismatched_msg = _create_mock_message(verb=RP, code=dto.code, src_id="01:999999")
    matched = manager.process_msg(mismatched_msg)

    # Assert
    assert matched is False
    assert manager.pending_count == 1
    assert not fut.done()

    manager.cancel_all()


@pytest.mark.asyncio
async def test_conversation_manager_timeout_and_retries() -> None:
    # Arrange
    loop = asyncio.get_running_loop()
    manager = ConversationManager(loop=loop, default_timeout=0.05, max_retries=1)
    intent = Command(
        src=Address("18:000730"),
        dst=Address("01:078710"),
        action=Action.SET_TEMPERATURE,
        data={"zone_idx": "00", "setpoint": 21.0},
    )
    dto = build_dto(intent)

    # Act
    fut = await manager.track_intent(intent, dto, timeout=0.05)

    # Allow timeouts to fire
    await asyncio.sleep(0.15)

    # Assert
    assert fut.done()
    assert manager.pending_count == 0
    with pytest.raises(ProtocolTimeoutError):
        fut.result()
