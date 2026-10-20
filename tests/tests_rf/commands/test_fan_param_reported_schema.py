"""SET_FAN_PARAM must prefer the device-reported 2411 definition (issue 1332).

A fan states each parameter's data_type/min/max/precision in its RP/2411
responses. When a retained response defines the parameter being written,
that definition wins over the static `_2411_PARAMS_SCHEMA` — the static
table can disagree with the model (e.g. param 52 is a trickle timer on
some devices and sensor sensitivity on others).
"""

from datetime import datetime as dt, timedelta as td
from unittest.mock import AsyncMock, MagicMock

import pytest

from ramses_rf.address import Address
from ramses_rf.commands.builders import build_dto
from ramses_rf.commands.core import Command
from ramses_rf.commands.dispatcher import CommandDispatcher
from ramses_rf.enums import Action
from ramses_rf.messages import Message
from ramses_rf.payloads.hvac import HvacFanParamPayload
from ramses_rf.state import MessageStore
from ramses_tx import Packet

FAN = "32:222222"
OTHER_FAN = "32:333333"
REM = "37:111111"


def _rp_packet(
    src: str,
    param_id: int,
    data_type: int,
    value: float,
    min_val: float,
    max_val: float,
    prec: float,
    dtm: dt | None = None,
) -> Packet:
    """Build a real RP/2411 packet carrying a parameter definition."""
    payload = HvacFanParamPayload(
        parameter_id=param_id,
        data_type=data_type,
        value_scaled=value,
        min_value_scaled=min_val,
        max_value_scaled=max_val,
        precision_scaled=prec,
        trailer_bytes=b"\x00\x32",
    ).to_bytes()
    return Packet.from_port(
        dtm or dt.now(),
        f"000 RP --- {src} {REM} --:------ 2411 023 {payload.hex().upper()}",
    )


def _gateway(*packets: Packet, raw_payloads: list[str] = ()) -> MagicMock:
    gateway = MagicMock()
    gateway.message_store = MessageStore(maintain=False)
    gateway.device_registry.device_by_id = {}
    gateway.conversation_manager = None
    for packet in packets:
        gateway.message_store.add(Message._from_packet(packet))
    for raw in raw_payloads:  # malformed stored payloads must not break send()
        gateway.message_store.add(
            Message._from_packet(
                Packet.from_port(
                    dt.now(),
                    f"000 RP --- {FAN} {REM} --:------ 2411 "
                    f"{len(raw) // 2:03d} {raw}",
                )
            )
        )
    gateway._async_send_dto = AsyncMock(
        return_value=(
            packets[-1]
            if packets
            else _rp_packet(FAN, 0x52, 0x00, 60, 0, 180, 10)
        )
    )
    return gateway


def _intent(param_id: str, value: float) -> Command:
    return Command(
        src=Address(REM),
        dst=Address(FAN),
        action=Action.SET_FAN_PARAM,
        data={"param_id": param_id, "value": value},
    )


async def _sent_payload(gateway: MagicMock, intent: Command) -> str:
    await CommandDispatcher(gateway).send(intent)
    return gateway._async_send_dto.call_args.args[0].payload


@pytest.mark.parametrize("value", [0, 0.5, 1, 1.5, 2, 10, 25])
async def test_param_52_sensitivity_uses_reported_schema(value: float) -> None:
    """Param 52 reported as dtype 01 must encode x10, not as a timer."""
    gateway = _gateway(_rp_packet(FAN, 0x52, 0x01, value, 0, 25.0, 0.1))
    payload = await _sent_payload(gateway, _intent("52", value))
    assert (
        payload
        == f"0000520001{round(value * 10):08X}00000000000000FA000000010032"
    )


async def test_reported_min_max_precision_are_used() -> None:
    """The reported range, not a hardcoded one, must land in the frame."""
    gateway = _gateway(_rp_packet(FAN, 0x52, 0x01, 1.0, 0, 30.0, 0.1))
    payload = await _sent_payload(gateway, _intent("52", 1.0))
    assert payload == "00005200010000000A000000000000012C000000010032"


async def test_reported_range_limits_the_write() -> None:
    """A value inside the static range but outside the reported range fails."""
    gateway = _gateway(_rp_packet(FAN, 0x52, 0x00, 60, 0, 150, 1))
    with pytest.raises(ValueError, match="Invalid value"):
        await _sent_payload(gateway, _intent("52", 160))  # static max is 180


async def test_newest_reported_definition_wins() -> None:
    """An older dtype-01 report must not override a newer dtype-00 one."""
    now = dt.now()
    gateway = _gateway(
        _rp_packet(
            FAN, 0x52, 0x01, 1.0, 0, 25.0, 0.1, dtm=now - td(seconds=5)
        ),
        _rp_packet(FAN, 0x52, 0x00, 60, 0, 180, 10, dtm=now),
    )
    payload = await _sent_payload(gateway, _intent("52", 60))
    # timer encoding: raw value 60, not 600
    assert payload == "00005200000000003C00000000000000B4000000010001"


async def test_fallback_when_no_matching_definition() -> None:
    """No RP for the param, a malformed RP, or a foreign fan -> static schema."""
    for packets, raw in (
        ([], []),
        # a short/N-A reply (not a full definition): must not clamp writes
        ([], ["0000520000"]),
        ([_rp_packet(OTHER_FAN, 0x52, 0x01, 1.0, 0, 25.0, 0.1)], []),
        ([_rp_packet(FAN, 0x31, 0x01, 30, 0, 1800, 30)], []),  # other param
    ):
        gateway = _gateway(*packets, raw_payloads=raw)
        intent = _intent("52", 60)
        payload = await _sent_payload(gateway, intent)
        assert payload == build_dto(intent).payload


async def test_unsupported_reported_dtype_falls_back() -> None:
    """A reported data_type the encoder cannot handle keeps the static schema."""
    gateway = _gateway(_rp_packet(FAN, 0x52, 0x57, 1, 0, 100, 1))
    intent = _intent("52", 60)
    payload = await _sent_payload(gateway, intent)
    assert payload == build_dto(intent).payload


async def test_reported_schema_covers_other_params_and_unknown_ids() -> None:
    """The override is not param-52 specific; a reported def also enables
    writes to parameters missing from the static table."""
    # param 54: static dtype "00" (minutes), device reports dtype "01"
    gateway = _gateway(_rp_packet(FAN, 0x54, 0x01, 3.0, 15.0, 60.0, 0.1))
    payload = await _sent_payload(gateway, _intent("54", 30))
    assert payload == "00005400010000012C0000009600000258000000010032"

    # param 07 is absent from the static FanParamInfo table: the device's
    # own definition is enough to encode the write
    gateway = _gateway(_rp_packet(FAN, 0x07, 0x00, 1, 0, 1, 1))
    payload = await _sent_payload(gateway, _intent("07", 1))
    assert payload == "0000070000000000010000000000000001000000010001"


async def test_no_message_store_falls_back() -> None:
    """Without a message store the static schema is used unchanged."""
    gateway = _gateway()
    gateway.message_store = None
    intent = _intent("52", 60)
    payload = await _sent_payload(gateway, intent)
    assert payload == build_dto(intent).payload
