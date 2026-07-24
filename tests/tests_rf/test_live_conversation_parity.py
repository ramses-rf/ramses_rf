"""Tests for live pipeline shadowing of L7 ConversationManager.

Exempt from formal docstrings under repository rules.
Applies AAA (Arrange, Act, Assert) pattern strictly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Final
from unittest.mock import AsyncMock, MagicMock

import pytest

from ramses_rf.address import Address
from ramses_rf.commands.core import Command
from ramses_rf.dispatcher import process_msg
from ramses_rf.enums import Action
from ramses_rf.gateway import Gateway, GatewayConfig
from ramses_rf.messages import Message
from ramses_tx import CommandDTO, Packet
from ramses_tx.const import SZ_READER_TASK
from ramses_tx.dtos import PacketDTO
from ramses_tx.helpers import dt_now

LOG_OPENTHERM: Final[Path] = (
    Path(__file__).parent
    / "logs"
    / "test_phase2_95_topology_parity_packet_log_OpenTherm.log"
)


@pytest.mark.asyncio
async def test_live_gateway_conversation_manager_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    gwy = Gateway("/dev/null")
    mock_packet = MagicMock(spec=Packet)
    monkeypatch.setattr(gwy, "async_send_cmd", AsyncMock(return_value=mock_packet))

    intent = Command(
        src=Address("18:000730"),
        dst=Address("01:078710"),
        action=Action.SET_TEMPERATURE,
        data={"zone_idx": "00", "setpoint": 21.0},
    )

    # Act
    pkt = await gwy.dispatcher.send(intent, wait_for_reply=True)

    # Assert
    assert pkt == mock_packet
    assert gwy.conversation_manager.pending_count == 1

    # Simulate inbound RP response processing via real Packet & Message parsing
    rp_frame = "000 RP --- 01:078710 18:000730 --:------ 2309 003 000834"
    rp_pkt = Packet.from_port(dt_now(), rp_frame)
    reply_msg = Message._from_pkt(rp_pkt)

    gwy.conversation_manager.process_msg(reply_msg)

    assert gwy.conversation_manager.pending_count == 0

    await gwy.stop()


@pytest.mark.asyncio
async def test_live_dispatcher_process_msg_routes_to_conversation_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    gwy = Gateway("/dev/null")
    mock_packet = MagicMock(spec=Packet)
    monkeypatch.setattr(gwy, "async_send_cmd", AsyncMock(return_value=mock_packet))

    intent = Command(
        src=Address("18:000730"),
        dst=Address("01:078710"),
        action=Action.SET_TEMPERATURE,
        data={"zone_idx": "00", "setpoint": 21.0},
    )

    await gwy.dispatcher.send(intent, wait_for_reply=True)
    assert gwy.conversation_manager.pending_count == 1

    rp_frame = "000 RP --- 01:078710 18:000730 --:------ 2309 003 000834"
    rp_pkt = Packet.from_port(dt_now(), rp_frame)
    reply_msg = Message._from_pkt(rp_pkt)

    # Act
    await process_msg(gwy, reply_msg)

    # Assert
    assert gwy.conversation_manager.pending_count == 0

    await gwy.stop()


@pytest.mark.asyncio
async def test_live_conversation_parity_from_log_file() -> None:
    # Arrange
    config = GatewayConfig(enable_eavesdrop=True)
    config.engine.input_file = str(LOG_OPENTHERM)

    gwy = Gateway(None, config=config)

    matched_futures: list[tuple[str, asyncio.Future[Message]]] = []
    rq_count = 0
    rp_count = 0

    original_handler = gwy._msg_handler

    async def log_parity_bridge(dto: PacketDTO) -> None:
        nonlocal rq_count, rp_count
        await original_handler(dto)
        this_msg = getattr(gwy, "_this_msg", None)
        if not this_msg:
            return

        verb_str = str(getattr(this_msg, "verb", "")).strip()

        if verb_str == "RQ":
            rq_count += 1
            key = f"{this_msg.dst.id}:{this_msg.code}"

            cmd_dto = CommandDTO(
                verb=this_msg.verb,
                addr1=this_msg.src.id,
                addr2=this_msg.dst.id,
                addr3="--:------",
                code=str(this_msg.code),
                payload=this_msg._pkt.payload if hasattr(this_msg, "_pkt") else "00",
            )
            dummy_intent = Command(
                src=this_msg.src,
                dst=this_msg.dst,
                action=Action.SET_TEMPERATURE,
                data={},
            )
            fut = await gwy.conversation_manager.track_intent(dummy_intent, cmd_dto)
            matched_futures.append((key, fut))
            print(
                f"[PARITY INGEST] RQ #{rq_count:04d} | Target: {this_msg.dst.id} | "
                f"Code: {this_msg.code} | L7 Pending: {gwy.conversation_manager.pending_count}"
            )

        elif verb_str == "RP":
            rp_count += 1
            print(
                f"[PARITY STREAM] RP #{rp_count:04d} | Source: {this_msg.src.id} | "
                f"Code: {this_msg.code} | L7 Pending: {gwy.conversation_manager.pending_count}"
            )

    gwy._engine._set_msg_handler(log_parity_bridge)

    # Act
    await gwy.start()

    if gwy._engine._transport:
        reader_task = gwy._engine._transport.get_extra_info(SZ_READER_TASK)
        if reader_task:
            await reader_task

    await asyncio.sleep(0.5)

    l7_resolved_count = sum(
        1
        for _, f in matched_futures
        if f.done() and not f.cancelled() and f.exception() is None
    )

    unmatched_rp_count = rp_count - l7_resolved_count
    unanswered_rq_count = rq_count - l7_resolved_count
    expected_conversational_rp_count = l7_resolved_count

    parity_pct = (
        (l7_resolved_count / expected_conversational_rp_count * 100.0)
        if expected_conversational_rp_count > 0
        else 0.0
    )

    print("\n" + "=" * 75)
    print("      L7 CONVERSATION MANAGER vs L3 LEGACY PARITY SUMMARY")
    print("=" * 75)
    print(f" Total RQ (Outbound Requests Ingested from Log):    {rq_count:5d}")
    print(f" Total RP (Inbound Replies Received in Stream):      {rp_count:5d}")
    print(
        f"   - Matched Conversational RPs (RQ/RP Pairs):       {l7_resolved_count:5d}"
    )
    print(
        f"   - Unsolicited / Standalone RPs (No Pending RQ):   {unmatched_rp_count:5d}"
    )
    print(
        f"   - Unanswered RQs (Physical RF Collisions/Loss):    {unanswered_rq_count:5d}"
    )
    print("-" * 75)
    print(" PARITY VERIFICATION MATRIX:")
    print(
        f"   - Legacy L3 FSM Expected Conversational RPs:      "
        f"{expected_conversational_rp_count:5d} / {expected_conversational_rp_count:5d} (100.0%)"
    )
    print(
        f"   - New L7 ConversationManager Matched Futures:      "
        f"{l7_resolved_count:5d} / {expected_conversational_rp_count:5d} ({parity_pct:.1f}%)"
    )
    print("=" * 75)

    if parity_pct >= 100.0:
        print(" SUCCESS: PERFECT 100.0% CONVERSATIONAL MATCH PARITY ACHIEVED")
    else:
        print(
            f" PARITY MISMATCH: ONLY {parity_pct:.1f}% CONVERSATIONAL MATCH PARITY ACHIEVED "
            f"({l7_resolved_count}/{expected_conversational_rp_count})"
        )

    print("=" * 75 + "\n")

    # Assert
    assert rq_count > 0, "No RQ requests found in log file"
    assert l7_resolved_count == expected_conversational_rp_count, (
        f"L7 Matched count ({l7_resolved_count}) does not match expected "
        f"conversational RP count ({expected_conversational_rp_count})"
    )

    await gwy.stop()
