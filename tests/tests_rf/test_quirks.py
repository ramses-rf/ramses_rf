#!/usr/bin/env python3
"""RAMSES RF - Unittests for HVAC quirks (quirks.py).

Tests cover:
- 12A0 array element splitting (Ventura V1x 3-element list)
- 31DA null-marker prevention (bypass_position, fan_info, exhaust_fan_speed)
- Stateful quirk interactions with existing HvacState

See ramses_cc issue #742: HVAC sensors bounce to None/FF/0 every ~10 min
because 31DA polling snapshots include "no sensor" values.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from ramses_rf.const import (
    SZ_BYPASS_POSITION,
    SZ_FAN_INFO,
    SZ_INDOOR_HUMIDITY,
    SZ_OUTDOOR_HUMIDITY,
    SZ_OUTDOOR_TEMP,
    SZ_SUPPLY_TEMP,
)
from ramses_rf.models import HvacState
from ramses_rf.quirks import apply_hvac_quirks
from ramses_tx.const import SZ_REL_HUMIDITY

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(**kwargs: Any) -> HvacState:
    """Create an HvacState with the given field values."""
    return HvacState(**kwargs)


def _quirk(payload: dict, current_state: HvacState | None, code: str) -> dict:
    """Shorthand wrapper for apply_hvac_quirks."""
    return apply_hvac_quirks(payload, current_state, code)


# ---------------------------------------------------------------------------
# 12A0 array element splitting
# ---------------------------------------------------------------------------


class TestQuirks12A0Idx00:
    """12A0 idx=00: indoor sensor."""

    def test_idx00_remaps_temperature_to_indoor_temp(self) -> None:
        """idx=00 temperature should be remapped to indoor_temp."""
        payload = {
            "hvac_idx": "00",
            SZ_INDOOR_HUMIDITY: 0.55,
            "temperature": 21.5,
        }
        result = _quirk(payload, None, "12A0")
        assert result["indoor_temp"] == 21.5
        assert result[SZ_INDOOR_HUMIDITY] == 0.55

    def test_idx00_without_temperature_passes_through(self) -> None:
        """idx=00 without temperature should pass humidity through."""
        payload = {"hvac_idx": "00", SZ_INDOOR_HUMIDITY: 0.55}
        result = _quirk(payload, None, "12A0")
        assert result[SZ_INDOOR_HUMIDITY] == 0.55
        assert "indoor_temp" not in result

    def test_idx00_preserves_dewpoint(self) -> None:
        """idx=00 should preserve dewpoint_temp if present."""
        payload = {
            "hvac_idx": "00",
            SZ_INDOOR_HUMIDITY: 0.55,
            "temperature": 21.5,
            "dewpoint_temp": 12.3,
        }
        result = _quirk(payload, None, "12A0")
        assert result["dewpoint_temp"] == 12.3


class TestQuirks12A0Idx01:
    """12A0 idx=01: supply sensor.

    parse_humidity_element returns ``rel_humidity`` (not ``indoor_humidity``)
    for idx=01.  The old quirks code checked the wrong key and the supply
    humidity was never remapped.  Since HvacState has no supply_humidity
    field, the rel_humidity key is now explicitly dropped to prevent it
    leaking through as a stray field.
    """

    def test_idx01_pops_rel_humidity(self) -> None:
        """idx=01 should drop rel_humidity (no supply_humidity field)."""
        payload = {
            "hvac_idx": "01",
            SZ_REL_HUMIDITY: 0.60,
            "temperature": 22.0,
        }
        result = _quirk(payload, None, "12A0")
        assert SZ_REL_HUMIDITY not in result
        assert "indoor_humidity" not in result

    def test_idx01_remaps_temperature_to_supply_temp(self) -> None:
        """idx=01 temperature should be remapped to supply_temp."""
        payload = {
            "hvac_idx": "01",
            SZ_REL_HUMIDITY: 0.60,
            "temperature": 22.0,
        }
        result = _quirk(payload, None, "12A0")
        assert result[SZ_SUPPLY_TEMP] == 22.0
        assert "temperature" not in result

    def test_idx01_without_temperature_only_drops_humidity(self) -> None:
        """idx=01 with only rel_humidity should just drop it."""
        payload = {"hvac_idx": "01", SZ_REL_HUMIDITY: 0.60}
        result = _quirk(payload, None, "12A0")
        assert SZ_REL_HUMIDITY not in result
        assert SZ_SUPPLY_TEMP not in result

    def test_idx01_old_key_indoor_humidity_also_dropped(self) -> None:
        """If the parser ever returns 'indoor_humidity' for idx=01,
        it should also be dropped (safety net)."""
        payload = {
            "hvac_idx": "01",
            SZ_INDOOR_HUMIDITY: 0.60,
            "temperature": 22.0,
        }
        result = _quirk(payload, None, "12A0")
        assert SZ_INDOOR_HUMIDITY not in result
        assert result[SZ_SUPPLY_TEMP] == 22.0


class TestQuirks12A0Idx02:
    """12A0 idx=02: outdoor sensor.

    parse_humidity_element already returns ``outdoor_humidity`` for idx=02,
    so no humidity remapping is needed.

    The temperature field is NOT remapped to outdoor_temp — 12A0 comes from
    a separate HUM sensor, and remapping it creates a second outdoor_temp
    source that conflicts with 31DA's outdoor_temp on the FAN's hvac_state.
    See ramses_cc#742.
    """

    def test_idx02_does_not_remap_temperature_to_outdoor_temp(self) -> None:
        """idx=02 temperature should NOT be remapped to outdoor_temp.

        31DA is the authoritative source for outdoor_temp.  Remapping 12A0
        idx=02 temperature causes outdoor_temp to bounce between the HUM
        sensor reading (12A0) and the FAN snapshot (31DA).
        """
        payload = {
            "hvac_idx": "02",
            SZ_OUTDOOR_HUMIDITY: 0.69,
            "temperature": 27.42,
        }
        result = _quirk(payload, None, "12A0")
        assert SZ_OUTDOOR_TEMP not in result
        assert "temperature" in result  # left as-is, not remapped

    def test_idx02_preserves_outdoor_humidity(self) -> None:
        """idx=02 outdoor_humidity should pass through unchanged."""
        payload = {
            "hvac_idx": "02",
            SZ_OUTDOOR_HUMIDITY: 0.69,
            "temperature": 27.42,
        }
        result = _quirk(payload, None, "12A0")
        assert result[SZ_OUTDOOR_HUMIDITY] == 0.69

    def test_idx02_without_temperature_passes_through(self) -> None:
        """idx=02 without temperature should pass humidity through."""
        payload = {"hvac_idx": "02", SZ_OUTDOOR_HUMIDITY: 0.69}
        result = _quirk(payload, None, "12A0")
        assert result[SZ_OUTDOOR_HUMIDITY] == 0.69
        assert SZ_OUTDOOR_TEMP not in result


class TestQuirks12A0FullList:
    """Simulate the full 3-element 12A0 list as the dispatcher iterates it."""

    def test_full_3_element_list_all_fields_mapped(self) -> None:
        """Iterate all 3 elements as the dispatcher does and verify
        the final merged state has correct indoor/outdoor/supply values."""
        elements: list[dict[str, Any]] = [
            {
                "hvac_idx": "00",
                SZ_INDOOR_HUMIDITY: 0.63,
                "temperature": 28.42,
                "dewpoint_temp": 21.0,
            },
            {
                "hvac_idx": "01",
                SZ_REL_HUMIDITY: None,  # EF = not implemented
                "temperature": None,  # 7FFF = not implemented
            },
            {
                "hvac_idx": "02",
                SZ_OUTDOOR_HUMIDITY: 0.69,
                "temperature": 27.42,
                "dewpoint_temp": 21.36,
            },
        ]

        # Process each element through quirks (as dispatcher does)
        results = [_quirk(dict(e), None, "12A0") for e in elements]

        # idx=00
        assert results[0]["indoor_temp"] == 28.42
        assert results[0][SZ_INDOOR_HUMIDITY] == 0.63
        # idx=01
        assert SZ_REL_HUMIDITY not in results[1]
        assert "temperature" not in results[1] or results[1].get("supply_temp") is None
        # idx=02: outdoor_humidity passes through, temperature is NOT remapped
        assert SZ_OUTDOOR_TEMP not in results[2]  # not remapped (31DA is authoritative)
        assert results[2][SZ_OUTDOOR_HUMIDITY] == 0.69

    def test_ventura_real_payload_pattern(self) -> None:
        """Test with the actual Ventura 12A0 payload pattern from issue #742.

        Payload: 003F0B1A7FFF0001EF7FFF7FFF0002450AB6085800
        Elements (14 hex chars each):
          idx=00: humidity=3F, temp=0B1A, dewpoint=7FFF
          idx=01: humidity=EF (None), temp=7FFF (None)
          idx=02: humidity=45, temp=0AB6, dewpoint=0858
        """
        # Simulate what the parser would return for this payload
        elements: list[dict[str, Any]] = [
            {
                "hvac_idx": "00",
                SZ_INDOOR_HUMIDITY: 0.247,  # 3F/255
                "temperature": 28.42,  # 0B1A/100
                "dewpoint_temp": None,  # 7FFF
                "_unknown_12": "FF",
            },
            {
                "hvac_idx": "01",
                SZ_REL_HUMIDITY: None,  # EF = not implemented
                "temperature": None,  # 7FFF
                "_unknown_12": "FF",
            },
            {
                "hvac_idx": "02",
                SZ_OUTDOOR_HUMIDITY: 0.271,  # 45/255
                "temperature": 27.42,  # 0AB6/100
                "dewpoint_temp": 21.36,  # 0858/100
                "_unknown_12": "58",
            },
        ]

        results = [_quirk(dict(e), None, "12A0") for e in elements]

        # idx=00: indoor
        assert results[0]["indoor_temp"] == 28.42
        assert results[0][SZ_INDOOR_HUMIDITY] == pytest.approx(0.247, abs=0.01)

        # idx=01: supply (humidity dropped, temp None → supply_temp=None)
        assert SZ_REL_HUMIDITY not in results[1]
        # temperature=None gets popped into supply_temp
        assert results[1].get(SZ_SUPPLY_TEMP) is None

        # idx=02: outdoor (humidity only, temp NOT remapped to outdoor_temp)
        assert SZ_OUTDOOR_TEMP not in results[2]  # not remapped (31DA is authoritative)
        assert results[2][SZ_OUTDOOR_HUMIDITY] == pytest.approx(0.271, abs=0.01)


class TestQuirks12A0EdgeCases:
    """Edge cases for 12A0 quirks."""

    def test_no_hvac_idx_defaults_to_00(self) -> None:
        """Missing hvac_idx should default to idx=00 behavior."""
        payload = {"temperature": 21.0}
        result = _quirk(payload, None, "12A0")
        assert result["indoor_temp"] == 21.0

    def test_unknown_idx_passes_through(self) -> None:
        """Unknown hvac_idx (e.g. 03) should pass through unchanged."""
        payload = {"hvac_idx": "03", "temperature": 25.0}
        result = _quirk(payload, None, "12A0")
        assert result["temperature"] == 25.0
        assert "indoor_temp" not in result

    def test_short_payload_single_dict(self) -> None:
        """Short 12A0 payload (single dict, no hvac_idx) should
        be treated as idx=00."""
        payload = {SZ_INDOOR_HUMIDITY: 0.50, "temperature": 20.0}
        result = _quirk(payload, None, "12A0")
        assert result["indoor_temp"] == 20.0
        assert result[SZ_INDOOR_HUMIDITY] == 0.50


# ---------------------------------------------------------------------------
# 31DA fan_info quirks
# ---------------------------------------------------------------------------


class TestQuirks31DAFanInfo:
    """31DA fan_info null-marker and unknown-code prevention.

    Devices like the Ventura V1x report fan_info via 22F1/22F4, not 31DA.
    Their 31DA snapshot includes an unknown fan_info code (e.g. 0x1F) that
    parses as '-unknown 0x1F-'.  This must not overwrite a valid fan_info
    from 22F1/22F4/31D9.
    """

    def test_unknown_fan_info_preserves_existing(self) -> None:
        """'-unknown 0x1F-' from 31DA must not overwrite a valid fan_info."""
        state = _make_state(fan_info="auto")
        payload = {SZ_FAN_INFO: "-unknown 0x1F-"}
        result = _quirk(payload, state, "31DA")
        assert result[SZ_FAN_INFO] == "auto"

    def test_off_fan_info_preserves_existing(self) -> None:
        """'off' from 31DA must not overwrite a valid fan_info."""
        state = _make_state(fan_info="auto")
        payload = {SZ_FAN_INFO: "off"}
        result = _quirk(payload, state, "31DA")
        assert result[SZ_FAN_INFO] == "auto"

    def test_empty_fan_info_preserves_existing(self) -> None:
        """'' from 31DA must not overwrite a valid fan_info."""
        state = _make_state(fan_info="low")
        payload = {SZ_FAN_INFO: ""}
        result = _quirk(payload, state, "31DA")
        assert result[SZ_FAN_INFO] == "low"

    def test_valid_fan_info_overwrites(self) -> None:
        """A valid fan_info from 31DA should overwrite the existing one."""
        state = _make_state(fan_info="low")
        payload = {SZ_FAN_INFO: "auto"}
        result = _quirk(payload, state, "31DA")
        assert result[SZ_FAN_INFO] == "auto"

    def test_unknown_fan_info_does_not_overwrite_unknown(self) -> None:
        """If both current and incoming are unknown, keep the incoming."""
        state = _make_state(fan_info="-unknown 0x1E-")
        payload = {SZ_FAN_INFO: "-unknown 0x1F-"}
        result = _quirk(payload, state, "31DA")
        # Current is also unknown, so we keep the incoming value
        assert result[SZ_FAN_INFO] == "-unknown 0x1F-"

    def test_unknown_fan_info_with_no_current_state(self) -> None:
        """If there is no current state, unknown fan_info passes through."""
        payload = {SZ_FAN_INFO: "-unknown 0x1F-"}
        result = _quirk(payload, None, "31DA")
        assert result[SZ_FAN_INFO] == "-unknown 0x1F-"

    def test_off_fan_info_with_no_current_state(self) -> None:
        """If there is no current state, 'off' passes through."""
        payload = {SZ_FAN_INFO: "off"}
        result = _quirk(payload, None, "31DA")
        assert result[SZ_FAN_INFO] == "off"

    def test_off_fan_info_preserves_none_current(self) -> None:
        """If current fan_info is None, 'off' should not be promoted."""
        state = _make_state(fan_info=None)
        payload = {SZ_FAN_INFO: "off"}
        result = _quirk(payload, state, "31DA")
        # current_state.fan_info is None (falsy), so the quirk doesn't fire
        assert result[SZ_FAN_INFO] == "off"

    def test_fan_info_quirk_only_for_31da(self) -> None:
        """The fan_info quirk should only apply to 31DA, not 31D9."""
        state = _make_state(fan_info="auto")
        payload = {SZ_FAN_INFO: "off"}
        result = _quirk(payload, state, "31D9")
        # 31D9 is not 31DA, so the quirk doesn't fire
        assert result[SZ_FAN_INFO] == "off"


# ---------------------------------------------------------------------------
# 31DA bypass_position quirks
# ---------------------------------------------------------------------------


class TestQuirks31DABypassPosition:
    """31DA bypass_position=0.0 null-marker prevention.

    Devices like Orcon report bypass_position via 22F7, not 31DA.
    Their 31DA snapshot includes 0x00 for bypass_position, which parses as
    0.0 (a seemingly valid value).  This must not overwrite a non-zero
    bypass_position from 22F7.
    """

    def test_zero_bypass_overwrites_nonzero_existing(self) -> None:
        """bypass_position=0.0 from 31DA can overwrite any value."""
        state = _make_state(bypass_position=0.5)
        payload = {SZ_BYPASS_POSITION: 0.0}
        result = _quirk(payload, state, "31DA")
        assert result[SZ_BYPASS_POSITION] == 0.0

    def test_zero_bypass_overwrites_nonzero_existing_with_mode(self) -> None:
        """bypass_position=0.0 from 31DA must not overwrite a non-zero value."""
        state = _make_state(bypass_position=0.5, bypass_mode="auto")
        payload = {SZ_BYPASS_POSITION: 0.0}
        result = _quirk(payload, state, "31DA")
        assert result[SZ_BYPASS_POSITION] == 0.5

    def test_zero_bypass_preserves_string_existing_with_mode(self) -> None:
        """bypass_position=0.0 must not overwrite a string value (e.g. 'off')."""
        state = _make_state(bypass_position="off", bypass_mode="auto")
        payload = {SZ_BYPASS_POSITION: 0.0}
        result = _quirk(payload, state, "31DA")
        assert result[SZ_BYPASS_POSITION] == "off"

    def test_nonzero_bypass_overwrites(self) -> None:
        """A non-zero bypass_position from 31DA should overwrite."""
        state = _make_state(bypass_position=0.5)
        payload = {SZ_BYPASS_POSITION: 0.75}
        result = _quirk(payload, state, "31DA")
        assert result[SZ_BYPASS_POSITION] == 0.75

    def test_zero_bypass_with_no_current_state(self) -> None:
        """If there is no current state, 0.0 passes through."""
        payload = {SZ_BYPASS_POSITION: 0.0}
        result = _quirk(payload, None, "31DA")
        assert result[SZ_BYPASS_POSITION] == 0.0

    def test_zero_bypass_with_zero_existing(self) -> None:
        """If current is also 0.0, the quirk doesn't fire (both are 0.0)."""
        state = _make_state(bypass_position=0.0)
        payload = {SZ_BYPASS_POSITION: 0.0}
        result = _quirk(payload, state, "31DA")
        assert result[SZ_BYPASS_POSITION] == 0.0

    def test_zero_bypass_with_none_existing(self) -> None:
        """If current bypass_position is None, 0.0 passes through."""
        state = _make_state(bypass_position=None)
        payload = {SZ_BYPASS_POSITION: 0.0}
        result = _quirk(payload, state, "31DA")
        assert result[SZ_BYPASS_POSITION] == 0.0

    def test_bypass_quirk_only_for_31da(self) -> None:
        """The bypass_position quirk should only apply to 31DA."""
        state = _make_state(bypass_position=0.5)
        payload = {SZ_BYPASS_POSITION: 0.0}
        result = _quirk(payload, state, "22F7")
        assert result[SZ_BYPASS_POSITION] == 0.0


# ---------------------------------------------------------------------------
# 31DA exhaust_fan_speed quirks (existing, regression tests)
# ---------------------------------------------------------------------------


class TestQuirks31DAExhaustFanSpeed:
    """Regression tests for the existing exhaust_fan_speed quirk."""

    def test_zero_exhaust_preserves_nonzero_existing(self) -> None:
        """exhaust_fan_speed=0.0 from 31DA must not overwrite a non-zero value."""
        state = _make_state(exhaust_fan_speed=50.0)
        payload = {"exhaust_fan_speed": 0.0}
        result = _quirk(payload, state, "31DA")
        assert result["exhaust_fan_speed"] == 50.0

    def test_nonzero_exhaust_overwrites(self) -> None:
        """A non-zero exhaust_fan_speed should overwrite."""
        state = _make_state(exhaust_fan_speed=50.0)
        payload = {"exhaust_fan_speed": 75.0}
        result = _quirk(payload, state, "31DA")
        assert result["exhaust_fan_speed"] == 75.0

    def test_zero_exhaust_with_no_current_state(self) -> None:
        """If there is no current state, 0.0 passes through."""
        payload = {"exhaust_fan_speed": 0.0}
        result = _quirk(payload, None, "31DA")
        assert result["exhaust_fan_speed"] == 0.0


# ---------------------------------------------------------------------------
# Integration: full 31DA Ventura payload simulation
# ---------------------------------------------------------------------------


class TestQuirks31DAVenturaIntegration:
    """Simulate the full Ventura 31DA payload from issue #742.

    Payload: 00EF0002F600EF0AB67FFF0B1A0AFEBE09001F0000000000008500850000
    Key null markers:
      indoor_humidity[10:12] = 00 → 0.0  (filtered by dispatcher)
      outdoor_humidity[12:14] = EF → None (filtered by dispatcher)
      supply_temp[18:22] = 7FFF → None (filtered by dispatcher)
      bypass_position[34:36] = 00 → 0.0 (filtered by quirks)
      fan_info[36:38] = 1F → '-unknown 0x1F-' (filtered by quirks)
    """

    def test_ventura_31da_does_not_overwrite_good_state(self) -> None:
        """Full Ventura 31DA payload must not overwrite good values
        from 12A0/22F1/22F4/22F7."""
        # Simulate state established by 12A0 + 22F1 + 22F7
        state = _make_state(
            indoor_humidity=0.63,
            outdoor_humidity=0.69,
            indoor_temp=28.42,
            outdoor_temp=27.42,
            bypass_position=0.5,
            fan_info="auto",
            fan_mode="auto",
            exhaust_fan_speed=60.0,
        )

        # Simulate the 31DA payload (post-parser dict)
        payload = {
            "hvac_id": "00",
            "exhaust_fan_speed": 0.0,  # null marker
            SZ_FAN_INFO: "-unknown 0x1F-",  # unknown code
            "air_quality": None,
            "co2_level": 758,
            SZ_INDOOR_HUMIDITY: 0.0,  # null marker (filtered by dispatcher)
            SZ_OUTDOOR_HUMIDITY: None,  # EF = not implemented
            "exhaust_temp": 27.42,
            SZ_SUPPLY_TEMP: None,  # 7FFF
            "indoor_temp": 28.42,
            "outdoor_temp": 28.14,
            "speed_capabilities": ["off", "timer", "boost"],
            SZ_BYPASS_POSITION: 0.0,  # null marker
            "supply_fan_speed": 0.0,
            "remaining_mins": 0,
            "post_heat": 0.0,
            "pre_heat": 0.0,
            "supply_flow": 133.0,
            "exhaust_flow": 133.0,
        }

        result = _quirk(payload, state, "31DA")

        # Quirks should preserve these values
        assert result[SZ_FAN_INFO] == "auto"  # not '-unknown 0x1F-'
        assert result[SZ_BYPASS_POSITION] == 0.5  # not 0.0
        assert result["exhaust_fan_speed"] == 60.0  # not 0.0

        # These pass through (dispatcher will filter the null markers)
        assert result[SZ_INDOOR_HUMIDITY] is None  # quirks normalise 0.0 → None
        assert result[SZ_OUTDOOR_HUMIDITY] is None  # dispatcher filters this
        assert result[SZ_SUPPLY_TEMP] is None  # dispatcher filters this

    def test_ventura_31da_with_no_current_state_passes_through(self) -> None:
        """When there is no existing state, 31DA values pass through.
        Humidity 0.0 is normalised to None (physically impossible on Earth).
        Other null markers pass through (the stateful quirks cannot know
        they are null markers without comparison)."""
        payload = {
            SZ_FAN_INFO: "-unknown 0x1F-",
            SZ_BYPASS_POSITION: 0.0,
            "exhaust_fan_speed": 0.0,
            SZ_INDOOR_HUMIDITY: 0.0,
        }

        result = _quirk(payload, None, "31DA")

        # Humidity 0.0 is normalised to None (always, regardless of state)
        assert result[SZ_INDOOR_HUMIDITY] is None
        # Other null markers pass through (stateful quirks need existing state)
        assert result[SZ_FAN_INFO] == "-unknown 0x1F-"
        assert result[SZ_BYPASS_POSITION] == 0.0
        assert result["exhaust_fan_speed"] == 0.0


# ---------------------------------------------------------------------------
# Non-HVAC codes: quirks should be no-ops
# ---------------------------------------------------------------------------


class TestQuirksNonHvacCodes:
    """Quirks should be no-ops for non-HVAC codes."""

    def test_22f1_passes_through(self) -> None:
        """22F1 payload should pass through unchanged."""
        state = _make_state(fan_info="low")
        payload = {SZ_FAN_INFO: "auto", SZ_BYPASS_POSITION: 0.5}
        result = _quirk(payload, state, "22F1")
        assert result == payload

    def test_22f7_passes_through(self) -> None:
        """22F7 payload should pass through unchanged."""
        state = _make_state(bypass_position=0.5)
        payload = {SZ_BYPASS_POSITION: 0.0}
        result = _quirk(payload, state, "22F7")
        assert result == payload

    def test_10d0_passes_through(self) -> None:
        """10D0 payload should pass through unchanged."""
        state = _make_state(fan_info="auto")
        payload = {"days_remaining": 30}
        result = _quirk(payload, state, "10D0")
        assert result == payload


# ---------------------------------------------------------------------------
# 31DA humidity 0.0 → None normalisation
# ---------------------------------------------------------------------------


class TestQuirks31DAHumidityNormalisation:
    """31DA humidity 0.0 is a null marker (00 = no sensor, 0% is impossible
    on Earth).  The quirks normalise it to None so that BOTH ingestion paths
    (dispatcher and StateProjector) filter it out.

    This is needed because the StateProjector (pipeline/ingestion.py) does not
    have its own null-marker filtering (the dispatcher does, from #737).
    See ramses_cc#742.
    """

    def test_indoor_humidity_0_normalised_to_none(self) -> None:
        """indoor_humidity=0.0 from 31DA should be normalised to None."""
        payload = {SZ_INDOOR_HUMIDITY: 0.0}
        result = _quirk(payload, None, "31DA")
        assert result[SZ_INDOOR_HUMIDITY] is None

    def test_outdoor_humidity_0_normalised_to_none(self) -> None:
        """outdoor_humidity=0.0 from 31DA should be normalised to None."""
        payload = {SZ_OUTDOOR_HUMIDITY: 0.0}
        result = _quirk(payload, None, "31DA")
        assert result[SZ_OUTDOOR_HUMIDITY] is None

    def test_valid_humidity_passes_through(self) -> None:
        """A valid humidity value should pass through unchanged."""
        payload = {SZ_INDOOR_HUMIDITY: 0.55}
        result = _quirk(payload, None, "31DA")
        assert result[SZ_INDOOR_HUMIDITY] == 0.55

    def test_humidity_none_passes_through(self) -> None:
        """None humidity (EF = not implemented) should pass through."""
        payload = {SZ_INDOOR_HUMIDITY: None}
        result = _quirk(payload, None, "31DA")
        assert result[SZ_INDOOR_HUMIDITY] is None

    def test_humidity_normalisation_with_existing_state(self) -> None:
        """Humidity 0.0 should be normalised to None even with existing state."""
        state = _make_state(indoor_humidity=0.55)
        payload = {SZ_INDOOR_HUMIDITY: 0.0}
        result = _quirk(payload, state, "31DA")
        assert result[SZ_INDOOR_HUMIDITY] is None

    def test_humidity_normalisation_only_for_31da(self) -> None:
        """The humidity normalisation should only apply to 31DA, not 12A0."""
        payload = {SZ_INDOOR_HUMIDITY: 0.0, "hvac_idx": "00"}
        result = _quirk(payload, None, "12A0")
        # 12A0 should not be normalised (it has its own idx-based handling)
        assert result[SZ_INDOOR_HUMIDITY] == 0.0


# ---------------------------------------------------------------------------
# 31D9 raw-hex fan_mode → None normalisation
# ---------------------------------------------------------------------------


class TestQuirks31D9FanModeNormalisation:
    """31D9 long-payload devices (Orcon, Brofer) send raw hex bytes for
    fan_mode (e.g. "04", "FF", "C8").  These are not semantic names and
    conflict with the semantic fan_mode from 22F4/22F1.  The quirks
    normalise any 2-char hex string to None so that BOTH ingestion paths
    filter it.  The valid semantic fan_mode comes from 22F4 (polled) or
    22F1 (command reply).

    Vasco/ClimaRad short payloads (msg.len == 3) are already converted to
    semantic strings by the parser and are preserved.

    See ramses_cc issue 723 and ramses_cc issue 742.
    """

    def test_fan_mode_ff_normalised_to_none(self) -> None:
        """fan_mode='FF' from 31D9 should be normalised to None."""
        from ramses_tx.const import SZ_FAN_MODE

        payload = {SZ_FAN_MODE: "FF"}
        result = _quirk(payload, None, "31D9")
        assert result[SZ_FAN_MODE] is None

    def test_fan_mode_04_normalised_to_none(self) -> None:
        """fan_mode='04' (raw hex, Orcon) from 31D9 should be normalised."""
        from ramses_tx.const import SZ_FAN_MODE

        payload = {SZ_FAN_MODE: "04"}
        result = _quirk(payload, None, "31D9")
        assert result[SZ_FAN_MODE] is None

    def test_fan_mode_c8_normalised_to_none(self) -> None:
        """fan_mode='C8' (raw hex, Itho boost) from 31D9 should be normalised."""
        from ramses_tx.const import SZ_FAN_MODE

        payload = {SZ_FAN_MODE: "C8"}
        result = _quirk(payload, None, "31D9")
        assert result[SZ_FAN_MODE] is None

    def test_fan_mode_00_normalised_to_none(self) -> None:
        """fan_mode='00' (raw hex, off) from 31D9 should be normalised."""
        from ramses_tx.const import SZ_FAN_MODE

        payload = {SZ_FAN_MODE: "00"}
        result = _quirk(payload, None, "31D9")
        assert result[SZ_FAN_MODE] is None

    def test_fan_mode_semantic_auto_preserved(self) -> None:
        """Semantic fan_mode='auto' (from Vasco lookup) should pass through."""
        from ramses_tx.const import SZ_FAN_MODE

        payload = {SZ_FAN_MODE: "auto"}
        result = _quirk(payload, None, "31D9")
        assert result[SZ_FAN_MODE] == "auto"

    def test_fan_mode_semantic_off_preserved(self) -> None:
        """Semantic fan_mode='off' (from Vasco lookup) should pass through."""
        from ramses_tx.const import SZ_FAN_MODE

        payload = {SZ_FAN_MODE: "off"}
        result = _quirk(payload, None, "31D9")
        assert result[SZ_FAN_MODE] == "off"

    def test_fan_mode_semantic_vasco_speed_preserved(self) -> None:
        """Semantic fan_mode='4 (boost)' (from Vasco lookup) should pass through."""
        from ramses_tx.const import SZ_FAN_MODE

        payload = {SZ_FAN_MODE: "4 (boost)"}
        result = _quirk(payload, None, "31D9")
        assert result[SZ_FAN_MODE] == "4 (boost)"

    def test_fan_mode_raw_hex_with_existing_state(self) -> None:
        """Raw hex fan_mode should be normalised even with existing state."""
        from ramses_tx.const import SZ_FAN_MODE

        state = _make_state(fan_mode="auto")
        payload = {SZ_FAN_MODE: "04"}
        result = _quirk(payload, state, "31D9")
        assert result[SZ_FAN_MODE] is None

    def test_fan_mode_raw_hex_only_for_31d9(self) -> None:
        """The raw-hex normalisation should only apply to 31D9, not 22F4."""
        from ramses_tx.const import SZ_FAN_MODE

        payload = {SZ_FAN_MODE: "04"}
        result = _quirk(payload, None, "22F4")
        assert result[SZ_FAN_MODE] == "04"

    def test_fan_mode_lowercase_hex_normalised(self) -> None:
        """Lowercase raw hex should also be normalised."""
        from ramses_tx.const import SZ_FAN_MODE

        payload = {SZ_FAN_MODE: "0a"}
        result = _quirk(payload, None, "31D9")
        assert result[SZ_FAN_MODE] is None


# ---------------------------------------------------------------------------
# Parser: parse_fan_info null-marker handling
# ---------------------------------------------------------------------------


class TestParseFanInfoNullMarkers:
    """Test that parse_fan_info handles null markers without crashing.

    Before the fix, FF and EF caused AssertionError crashes in the parser.
    These are null markers for devices that don't report fan_info in 31DA
    (e.g. Nuaire in fan_co2_dcv.yaml sends 0xFF at position [36:38]).
    """

    def test_ff_returns_none(self) -> None:
        """0xFF (no data) should return fan_info=None, not crash."""
        from ramses_tx.helpers import parse_fan_info

        result = parse_fan_info("FF")
        assert result["fan_info"] is None

    def test_ef_returns_none(self) -> None:
        """0xEF (not implemented) should return fan_info=None, not crash."""
        from ramses_tx.helpers import parse_fan_info

        result = parse_fan_info("EF")
        assert result["fan_info"] is None

    def test_unknown_code_returns_unknown_string(self) -> None:
        """Unknown codes (e.g. 0x1F) should return '-unknown 0xNN-',
        not crash with AssertionError."""
        from ramses_tx.helpers import parse_fan_info

        result = parse_fan_info("1F")
        assert result["fan_info"] == "-unknown 0x1F-"
        assert "_unknown_fan_info_flags" in result

    def test_valid_off_still_works(self) -> None:
        """0x00 should still parse as 'off'."""
        from ramses_tx.helpers import parse_fan_info

        result = parse_fan_info("00")
        assert result["fan_info"] == "off"

    def test_valid_speed_still_works(self) -> None:
        """0x08 should still parse as a valid speed."""
        from ramses_tx.helpers import parse_fan_info

        result = parse_fan_info("08")
        assert result["fan_info"] is not None
        assert result["fan_info"] != "-unknown 0x08-"

    def test_fan_co2_dcv_payload_parses_without_crash(self) -> None:
        """The fan_co2_dcv.yaml 31DA payload with fan_info=0xFF should
        parse without crashing (regression test for issue #742)."""
        from ramses_rf.parsers.hvac import parser_31da

        payload = "00EF00C0EFEF7FFF7FFF7FFF7FFF0000EF00FFFF0000EFEF7FFF7FFF00"
        msg = MagicMock()
        msg.code = "31DA"
        msg.verb = "I"

        # Should not raise
        result = parser_31da(payload, msg)
        assert result["fan_info"] is None  # FF = no data
