"""SET_FAN_PARAM must prefer the device-reported 2411 definition (issue 1332).

A fan states each parameter's data_type/min/max/precision in its RP/I 2411
frames. `HvacVentilator` caches that definition; when the parameter is
written, the reported schema wins over the static `_2411_PARAMS_SCHEMA` —
which can disagree with the model (e.g. param 52 is a trickle timer on
some devices and sensor sensitivity on others).
"""

from datetime import datetime as dt
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from ramses_rf.address import Address
from ramses_rf.commands.core import Command
from ramses_rf.commands.dispatcher import CommandDispatcher
from ramses_rf.devices.hvac_ventilators import HvacVentilator
from ramses_rf.enums import Action
from ramses_rf.messages import Message
from ramses_rf.models.hvac_schemas import FanParamInfo
from ramses_rf.payloads.hvac import HvacFanParamPayload
from ramses_tx import Packet
from ramses_tx.typing import DeviceIdT

FAN = "32:222222"
OTHER_FAN = "32:333333"
REM = "37:111111"


def _rp(
    src: str,
    param_id: int,
    data_type: int,
    value: float | None,
    min_val: float,
    max_val: float,
    prec: float,
) -> Message:
    """Build a real RP/2411 message carrying a parameter definition."""
    payload = HvacFanParamPayload(
        parameter_id=param_id,
        data_type=data_type,
        value_scaled=value,
        min_value_scaled=min_val,
        max_value_scaled=max_val,
        precision_scaled=prec,
        trailer_bytes=b"\x00\x32",
    ).to_bytes()
    packet = Packet.from_port(
        dt.now(),
        f"000 RP --- {src} {REM} --:------ 2411 023 {payload.hex().upper()}",
    )
    return Message._from_packet(packet)


def _msg(src: str, raw: str) -> Message:
    """Build a real message from a raw 2411 payload hex string."""
    packet = Packet.from_port(
        dt.now(),
        f"000 RP --- {src} {REM} --:------ 2411 {len(raw) // 2:03d} {raw}",
    )
    return Message._from_packet(packet)


def _gateway() -> MagicMock:
    gateway = MagicMock()
    gateway.conversation_manager = None
    gateway.device_registry.device_by_id = {}
    gateway._async_send_dto = AsyncMock(
        return_value=Packet.from_port(
            dt.now(), f"000 RP --- {FAN} {REM} --:------ 2411 004 00000000"
        )
    )
    return gateway


def _fan(gateway: MagicMock, dev_id: str = FAN) -> HvacVentilator:
    fan = HvacVentilator(gateway, Address(cast(DeviceIdT, dev_id)))
    gateway.device_registry.device_by_id[dev_id] = fan
    return fan


def _intent(param_id: str, value: float, dst: str = FAN) -> Command:
    return Command(
        src=Address(cast(DeviceIdT, REM)),
        dst=Address(cast(DeviceIdT, dst)),
        action=Action.SET_FAN_PARAM,
        data={"param_id": param_id, "value": value},
    )


async def _sent_payload(gateway: MagicMock, intent: Command) -> str:
    await CommandDispatcher(gateway).send(intent)
    return str(gateway._async_send_dto.call_args.args[0].payload)


# --- device-side schema caching ------------------------------------------


def test_rp_definition_is_cached() -> None:
    """A full RP/2411 definition is cached on the fan with all fields."""
    gateway = _gateway()
    fan = _fan(gateway)
    fan._handle_2411_message(_rp(FAN, 0x52, 0x01, 15.0, 0, 25.0, 0.1))

    schema = fan.get_2411_param_schema("52")
    assert schema == FanParamInfo(
        "Sensor sensitivity (%)", 0.0, 25.0, 0.1, "01", "%"
    )


def test_schema_key_normalization() -> None:
    """The getter resolves stripped and padded param ids identically."""
    gateway = _gateway()
    fan = _fan(gateway)
    fan._handle_2411_message(_rp(FAN, 0x07, 0x00, 1, 0, 1, 1))

    assert fan.get_2411_param_schema("07") is not None
    assert fan.get_2411_param_schema("7") is not None
    assert fan.get_2411_param_schema("bogus") is None
    assert fan.get_2411_param_schema("4E") is None


def test_incomplete_definitions_are_not_cached() -> None:
    """Short/N-A payloads and unencodable data types must not be cached."""
    gateway = _gateway()
    fan = _fan(gateway)

    # a short (3-byte) reply: parses into a _raw_3b stub, no definition
    fan._handle_2411_message(_msg(FAN, "0000520000"))
    assert fan.get_2411_param_schema("52") is None

    # a data type the builders cannot encode must not poison the cache
    fan._handle_2411_message(_rp(FAN, 0x4E, 0x22, 5, 0, 10, 1))
    assert fan.get_2411_param_schema("4E") is None


def test_definition_without_value_is_still_cached() -> None:
    """A definition with a sentinel (None) value still yields a schema."""
    gateway = _gateway()
    fan = _fan(gateway)
    fan._handle_2411_message(_rp(FAN, 0x52, 0x01, None, 0, 25.0, 0.1))
    assert fan.get_2411_param_schema("52") is not None


def test_foreign_frames_are_not_cached() -> None:
    """Frames that did not originate from this device are ignored."""
    gateway = _gateway()
    fan = _fan(gateway)
    # an RP from a different fan
    fan._handle_2411_message(_rp(OTHER_FAN, 0x52, 0x01, 15.0, 0, 25.0, 0.1))
    assert fan.get_2411_param_schema("52") is None


def test_unknown_param_gets_generic_description() -> None:
    """A reported param absent from the static table is still cached."""
    gateway = _gateway()
    fan = _fan(gateway)
    fan._handle_2411_message(_rp(FAN, 0x60, 0x00, 30, 0, 180, 10))
    schema = fan.get_2411_param_schema("60")
    assert schema is not None
    assert schema.description == "Parameter 60"


# --- dispatcher: reported schema wins over the static table --------------


@pytest.mark.parametrize("value", [0, 0.5, 1, 1.5, 2, 10, 25])
async def test_param_52_sensitivity_uses_reported_schema(value: float) -> None:
    """Param 52 reported as dtype 01 must encode x10."""
    gateway = _gateway()
    fan = _fan(gateway)
    fan._handle_2411_message(_rp(FAN, 0x52, 0x01, value, 0, 25.0, 0.1))

    payload = await _sent_payload(gateway, _intent("52", value))
    assert (
        payload
        == f"0000520001{round(value * 10):08X}00000000000000FA000000010032"
    )


async def test_reported_min_max_precision_are_used() -> None:
    """The reported range, not a hardcoded one, must land in the frame."""
    gateway = _gateway()
    fan = _fan(gateway)
    fan._handle_2411_message(_rp(FAN, 0x52, 0x01, 1.0, 0, 30.0, 0.1))

    payload = await _sent_payload(gateway, _intent("52", 1.0))
    assert payload == "00005200010000000A000000000000012C000000010032"


async def test_reported_range_limits_the_write() -> None:
    """A value inside the static range but outside the reported range fails."""
    gateway = _gateway()
    fan = _fan(gateway)
    # static table: param 54 = 15..60 min; device reports 15..45
    fan._handle_2411_message(_rp(FAN, 0x54, 0x00, 30, 15, 45, 1))
    with pytest.raises(ValueError, match="Invalid value"):
        await _sent_payload(gateway, _intent("54", 50))


async def test_newer_definition_replaces_older() -> None:
    """The latest device-reported definition wins."""
    gateway = _gateway()
    fan = _fan(gateway)
    fan._handle_2411_message(_rp(FAN, 0x52, 0x00, 60, 0, 180, 10))
    fan._handle_2411_message(_rp(FAN, 0x52, 0x01, 15.0, 0, 25.0, 0.1))

    payload = await _sent_payload(gateway, _intent("52", 15.0))
    assert payload == "00005200010000009600000000000000FA000000010032"


async def test_fallback_when_no_schema_is_cached() -> None:
    """No cached schema -> the static table encodes the write.

    Param 54 is a timer in the unified table (dtype 00): value 30 encodes
    raw 30 (0x1E), not 300.
    """
    gateway = _gateway()
    _fan(gateway)  # registered, but has no cached schema
    payload = await _sent_payload(gateway, _intent("54", 30))
    assert payload == "00005400000000001E0000000F0000003C000000010001"

    # and a write to a fan that has never been seen falls back too
    gateway2 = _gateway()
    payload2 = await _sent_payload(
        gateway2, _intent("54", 30, dst="32:999999")
    )
    assert payload2 == "00005400000000001E0000000F0000003C000000010001"


async def test_malformed_frame_does_not_break_send() -> None:
    """A frame that fails to parse cleanly must not prevent a write."""
    gateway = _gateway()
    fan = _fan(gateway)
    # an odd-length/truncated payload that still produces a message
    fan._handle_2411_message(_msg(FAN, "0000520000"))
    payload = await _sent_payload(gateway, _intent("54", 30))
    assert payload == "00005400000000001E0000000F0000003C000000010001"
