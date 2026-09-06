# --- START OF FILE test_discovery_scan.py ---

"""Tests for the passive device scan engine (ramses_rf.discovery_scan).

These tests verify:
- Device classification by prefix and verb/code pairs
- In-memory discovery list management
- JSON export/import round-trip
- Confidence scoring
- Zone binding extraction
- Known device filtering
- No topology mutation (read-only)
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime as dt
from typing import Any
from unittest.mock import MagicMock

import pytest

from ramses_rf.const import Code, DevType, Verb
from ramses_rf.discovery_scan import (
    DiscoveredDevice,
    DiscoveryScan,
    _classify,
    _extract_domain_id_from_000c,
    _extract_zone_index_from_payload,
    _initial_confidence,
    _is_appliance_control_signal,
    _is_valid_address,
    _recompute_confidence,
    _should_update_domain_id,
)
from ramses_tx.dtos import PacketDTO

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_dto(
    src: str = "04:056053",
    dst: str = "01:145038",
    addr3: str = "--:------",
    code: str = Code._3150,
    verb: str = Verb.I_,
    payload: str = "02C8",
    rssi: str = "-72",
) -> PacketDTO:
    """Create a PacketDTO for testing."""
    return PacketDTO(
        timestamp=dt.now(),
        rssi=rssi,
        verb=verb,
        seq="00",
        addr1=src,
        addr2=dst,
        addr3=addr3,
        code=code,
        length="006",
        raw_payload=payload,
    )


def make_mock_gateway(
    known_list: dict[str, Any] | None = None,
    schema: dict[str, Any] | None = None,
    device_by_id: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock Gateway with the minimum interface DiscoveryScan needs."""
    gwy = MagicMock()
    gwy.device_registry = MagicMock()
    gwy.device_registry.device_by_id = device_by_id or {}
    gwy._gwy_config = MagicMock()
    gwy._gwy_config.known_list = known_list or {}
    gwy._gwy_config.schema = schema or {}
    gwy.add_raw_packet_handler = MagicMock(return_value=lambda: None)
    return gwy


# ---------------------------------------------------------------------------
# Classification helper tests
# ---------------------------------------------------------------------------


class TestIsValidAddress:
    """Tests for _is_valid_address."""

    def test_valid_device_id(self) -> None:
        assert _is_valid_address("04:056053") is True

    def test_valid_ctl_id(self) -> None:
        assert _is_valid_address("01:145038") is True

    def test_broadcast_address_rejected(self) -> None:
        assert _is_valid_address("18:73030") is False

    def test_all_device_broadcast_rejected(self) -> None:
        assert _is_valid_address("63:262142") is False

    def test_null_device_type_all_ones_rejected(self) -> None:
        # 63:262143 = 0xFFFFFF, the all-ones sentinel (HGI80 self-disguise)
        assert _is_valid_address("63:262143") is False

    def test_all_null_device_type_rejected(self) -> None:
        # No real device uses type 63 (NUL) — reject the whole prefix
        assert _is_valid_address("63:000000") is False
        assert _is_valid_address("63:999999") is False

    def test_empty_rejected(self) -> None:
        assert _is_valid_address("") is False

    def test_no_colon_rejected(self) -> None:
        assert _is_valid_address("04056053") is False

    def test_too_short_rejected(self) -> None:
        assert _is_valid_address("04:056") is False

    def test_placeholder_rejected(self) -> None:
        assert _is_valid_address("--:------") is False

    def test_all_zeros_rejected(self) -> None:
        assert _is_valid_address("00:------") is False


class TestExtractZoneIdx:
    """Tests for _extract_zone_index_from_payload."""

    def test_valid_zone_index(self) -> None:
        assert _extract_zone_index_from_payload("02C8") == "02"

    def test_hw_zone(self) -> None:
        # "HW" is not valid hex — should return None
        assert _extract_zone_index_from_payload("HW...") is None

    def test_empty_payload(self) -> None:
        assert _extract_zone_index_from_payload("") is None

    def test_short_payload(self) -> None:
        assert _extract_zone_index_from_payload("0") is None

    def test_zone_fc_rejected(self) -> None:
        # FC is the appliance_control domain, not a zone index
        assert _extract_zone_index_from_payload("FC00") is None

    def test_zone_7f_rejected(self) -> None:
        # 7F is broadcast, not a zone index
        assert _extract_zone_index_from_payload("7F00") is None

    def test_zone_0c_rejected(self) -> None:
        # 0C is above the 12-zone max (00-0B)
        assert _extract_zone_index_from_payload("0C00") is None

    def test_zone_0b_accepted(self) -> None:
        # 0B is the highest valid zone index
        assert _extract_zone_index_from_payload("0B00") == "0B"

    def test_zone_00_accepted(self) -> None:
        assert _extract_zone_index_from_payload("0000") == "00"

    def test_zone_lowercase(self) -> None:
        # Should normalise to uppercase
        assert _extract_zone_index_from_payload("0a00") == "0A"


class TestIsApplianceControlSignal:
    """Tests for _is_appliance_control_signal (issue 834)."""

    def test_bdr_3b00_i_is_appliance(self) -> None:
        # BDR broadcasting 3B00 as I — classic TPI loop signature
        assert (
            _is_appliance_control_signal(
                "13:121025", Code._3B00, Verb.I_, True
            )
            is True
        )

    def test_bdr_3ef0_i_is_appliance(self) -> None:
        assert (
            _is_appliance_control_signal(
                "13:121025", Code._3EF0, Verb.I_, True
            )
            is True
        )

    def test_otb_3b00_i_is_appliance(self) -> None:
        # OTB (10:) also broadcasts 3B00 as the boiler relay
        assert (
            _is_appliance_control_signal(
                "10:067219", Code._3B00, Verb.I_, True
            )
            is True
        )

    def test_bdr_3b00_rp_not_appliance(self) -> None:
        # RP is a directed reply, not the broadcast TPI signature
        assert (
            _is_appliance_control_signal(
                "13:121025", Code._3B00, Verb.RP, True
            )
            is False
        )

    def test_bdr_3b00_rq_not_appliance(self) -> None:
        assert (
            _is_appliance_control_signal(
                "13:121025", Code._3B00, Verb.RQ, True
            )
            is False
        )

    def test_bdr_3ef1_not_appliance(self) -> None:
        # 3EF1 is a directed RP from the relay to the HGI, not the TPI broadcast
        assert (
            _is_appliance_control_signal(
                "13:121025", Code._3EF1, Verb.RP, True
            )
            is False
        )

    def test_trv_3b00_not_appliance(self) -> None:
        # Only 13: and 10: can be appliance controls
        assert (
            _is_appliance_control_signal(
                "04:056053", Code._3B00, Verb.I_, True
            )
            is False
        )

    def test_bdr_3b00_as_dst_not_appliance(self) -> None:
        # Must be the src (sender), not dst
        assert (
            _is_appliance_control_signal(
                "13:121025", Code._3B00, Verb.I_, False
            )
            is False
        )

    def test_bdr_other_code_not_appliance(self) -> None:
        assert (
            _is_appliance_control_signal(
                "13:121025", Code._1100, Verb.I_, True
            )
            is False
        )


class TestExtractDomainIdFrom000C:
    """Tests for _extract_domain_id_from_000c (issue 834 regression)."""

    def test_app_role_returns_fc(self) -> None:
        # 000C payload: 00 (index) + 0F (APP) + 00 (pad) + hex_id
        # → domain FC (appliance_control)
        assert _extract_domain_id_from_000c("000F003545C8") == "FC"

    def test_htg_role_index_00_returns_fa(self) -> None:
        # 000C payload: 00 (index) + 0E (HTG) + 00 (pad) + hex_id
        # → domain FA (hotwater_valve)
        assert _extract_domain_id_from_000c("000E003545C8") == "FA"

    def test_htg_role_index_01_returns_f9(self) -> None:
        # 000C payload: 01 (index) + 0E (HTG) + 00 (pad) + hex_id
        # → domain F9 (heating_valve)
        assert _extract_domain_id_from_000c("010E003545C8") == "F9"

    def test_dhw_role_index_00_returns_fa(self) -> None:
        # 000C payload: 00 (index) + 0D (DHW) + 00 (pad) + hex_id
        # → domain FA (dhw_sensor)
        assert _extract_domain_id_from_000c("000D003545C8") == "FA"

    def test_zone_role_returns_none(self) -> None:
        # 000C payload with a zone role (08 = rad_actuator) → no domain_id
        assert _extract_domain_id_from_000c("0008003545C8") is None

    def test_empty_payload_returns_none(self) -> None:
        assert _extract_domain_id_from_000c("") is None

    def test_short_payload_returns_none(self) -> None:
        assert _extract_domain_id_from_000c("00") is None


class TestShouldUpdateDomainId:
    """Tests for _should_update_domain_id (issue 834 regression)."""

    def test_authoritative_overrides_existing(self) -> None:
        # 000C FA binding must override a previous 3B00/3EF0 FC hint
        assert (
            _should_update_domain_id("FC", "FA", is_authoritative=True) is True
        )

    def test_authoritative_same_value_no_update(self) -> None:
        assert (
            _should_update_domain_id("FC", "FC", is_authoritative=True)
            is False
        )

    def test_authoritative_overrides_none(self) -> None:
        assert (
            _should_update_domain_id(None, "FA", is_authoritative=True) is True
        )

    def test_hint_does_not_override_existing(self) -> None:
        # 3B00/3EF0 hint must NOT override an existing 000C domain_id
        assert (
            _should_update_domain_id("FA", "FC", is_authoritative=False)
            is False
        )

    def test_hint_sets_none(self) -> None:
        # 3B00/3EF0 hint can set domain_id if none exists yet
        assert (
            _should_update_domain_id(None, "FC", is_authoritative=False)
            is True
        )

    def test_hint_same_as_existing_no_update(self) -> None:
        assert (
            _should_update_domain_id("FC", "FC", is_authoritative=False)
            is False
        )

    def test_none_new_returns_false(self) -> None:
        assert (
            _should_update_domain_id("FC", None, is_authoritative=True)
            is False
        )


class TestDiscoveryScanDomainId:
    """Integration tests for domain_id from 000C vs 3B00/3EF0 (issue 834)."""

    def test_000c_fc_binding_sets_domain_on_src(self) -> None:
        """A 000C APP binding (FC) sets domain_id on the CTL (src)."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        ctl_id = "01:216136"

        scan._process_packet(
            make_dto(
                src=ctl_id,
                dst="18:001234",
                code=Code._000C,
                verb=Verb.RP,
                payload="000F003545C8",
            )
        )
        dev = scan.get_device(ctl_id)
        assert dev is not None
        # The CTL is the src of the 000C, so domain_id is set
        assert dev.domain_id == "FC"

    def test_000c_fa_binding_sets_domain_on_src(self) -> None:
        """A 000C HTG binding (FA) sets domain_id on the CTL (src)."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        ctl_id = "01:216136"

        scan._process_packet(
            make_dto(
                src=ctl_id,
                dst="18:001234",
                code=Code._000C,
                verb=Verb.RP,
                payload="000E003545C8",
            )
        )
        dev = scan.get_device(ctl_id)
        assert dev is not None
        assert dev.domain_id == "FA"

    def test_3b00_hint_does_not_override_000c_fa(self) -> None:
        """3B00/3EF0 FC hint must NOT override an existing 000C FA domain_id.

        Issue 834 comment 5044906835: the CTL has a 000C HTG binding (FA),
        then the BDR broadcasts 3B00 (FC hint).  The CTL's domain_id must
        stay FA (authoritative), not be overwritten by the hint.
        """
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        ctl_id = "01:216136"

        # Step 1: 000C HTG binding → FA (authoritative)
        scan._process_packet(
            make_dto(
                src=ctl_id,
                dst="18:001234",
                code=Code._000C,
                verb=Verb.RP,
                payload="000E003545C8",
            )
        )
        dev = scan.get_device(ctl_id)
        assert dev is not None
        assert dev.domain_id == "FA"

        # Step 2: BDR broadcasts 3B00 I → FC hint (should NOT override FA)
        scan._process_packet(
            make_dto(
                src="13:042605",
                dst="--:------",
                code=Code._3B00,
                verb=Verb.I_,
                payload="00C8",
            )
        )
        # CTL's domain_id must still be FA
        dev = scan.get_device(ctl_id)
        assert dev is not None
        assert dev.domain_id == "FA"


class TestClassify:
    """Tests for _classify."""

    def test_prefix_ctl(self) -> None:
        assert (
            _classify("01:145038", Code._2E04, Verb.I_, is_source=True)
            == DevType.CTL
        )

    def test_prefix_trv(self) -> None:
        assert (
            _classify("04:056053", Code._3150, Verb.I_, is_source=True)
            == DevType.TRV
        )

    def test_prefix_dhw(self) -> None:
        assert (
            _classify("07:046947", Code._10A0, Verb.I_, is_source=True)
            == DevType.DHW
        )

    def test_prefix_otb(self) -> None:
        assert (
            _classify("10:067219", Code._0008, Verb.I_, is_source=True)
            == DevType.OTB
        )

    def test_prefix_bdr(self) -> None:
        assert (
            _classify("13:042605", Code._1100, Verb.I_, is_source=True)
            == DevType.BDR
        )

    def test_prefix_fan(self) -> None:
        assert (
            _classify("32:157747", Code._31DA, Verb.I_, is_source=True)
            == DevType.FAN
        )

    def test_prefix_rem(self) -> None:
        assert (
            _classify("37:179540", Code._22F1, Verb.I_, is_source=True)
            == DevType.REM
        )

    def test_vc_pair_fan(self) -> None:
        """I 31DA → FAN (from HVAC_KLASS_BY_VC_PAIR)."""
        assert (
            _classify("32:157747", Code._31DA, Verb.I_, is_source=True)
            == DevType.FAN
        )

    def test_vc_pair_rem(self) -> None:
        """I 22F1 → REM."""
        assert (
            _classify("37:179540", Code._22F1, Verb.I_, is_source=True)
            == DevType.REM
        )

    def test_vc_pair_co2(self) -> None:
        """I 1298 → CO2."""
        assert (
            _classify("37:123456", Code._1298, Verb.I_, is_source=True)
            == DevType.CO2
        )

    def test_hvac_prefix_wins_over_vc_pair(self) -> None:
        """A FAN (32:) sending 22F1 should stay FAN, not become REM."""
        assert (
            _classify("32:157747", Code._22F1, Verb.I_, is_source=True)
            == DevType.FAN
        )

    def test_vc_pair_for_non_hvac_prefix(self) -> None:
        """A non-HVAC prefix sending an HVAC code should use the VC pair.

        Note: 18: (HGI) is now unambiguous — it's always HGI regardless of
        codes sent (it relays packets from all device types).  Use a different
        prefix to test VC pair override on non-HVAC prefixes.
        """
        # 30: is RFG, but if it sends I 31DA the VC pair should win → FAN
        assert (
            _classify("30:123456", Code._31DA, Verb.I_, is_source=True)
            == DevType.FAN
        )

    def test_ctl_only_code(self) -> None:
        """A device sending 1030 (CTL-only code) is classified as CTL."""
        assert (
            _classify("01:145038", Code._1030, Verb.I_, is_source=True)
            == DevType.CTL
        )

    def test_ctl_only_code_not_from_dst(self) -> None:
        """CTL-only code from dst (not src) should not classify as CTL."""
        result = _classify("01:145038", Code._1030, Verb.I_, is_source=False)
        # Falls back to prefix
        assert result == DevType.CTL  # prefix 01 = CTL anyway

    def test_unknown_prefix(self) -> None:
        """Unknown prefix with no VC match returns DEV."""
        assert (
            _classify("99:999999", Code._0001, Verb.I_, is_source=True)
            == DevType.DEV
        )

    def test_reclassify_with_dev(self) -> None:
        """Re-classify using accumulated codes_seen."""
        dev = DiscoveredDevice(
            device_id="01:145038",
            first_seen="2026-07-01T10:00:00",
            last_seen="2026-07-01T10:00:00",
            likely_type="DEV",
            codes_seen=[Code._1030, Code._2E04],
        )
        result = _classify(
            "01:145038", Code._0001, Verb.I_, is_source=True, device=dev
        )
        assert result == DevType.CTL

    def test_313f_i_is_ctl(self) -> None:
        """313F I (datetime broadcast) is CTL-only."""
        assert (
            _classify("01:145038", Code._313F, Verb.I_, is_source=True)
            == DevType.CTL
        )

    def test_313f_rp_is_ctl(self) -> None:
        """313F RP (datetime reply) is CTL-only."""
        assert (
            _classify("01:145038", Code._313F, Verb.RP, is_source=True)
            == DevType.CTL
        )

    def test_313f_rq_is_not_ctl(self) -> None:
        """313F RQ (datetime request) is NOT CTL-only — TRVs send RQ too."""
        # A 04: TRV sending 313F RQ should stay TRV, not become CTL
        result = _classify("04:056053", Code._313F, Verb.RQ, is_source=True)
        assert result == DevType.TRV

    def test_313f_rq_unknown_prefix_is_not_ctl(self) -> None:
        """313F RQ from unknown prefix should not be classified as CTL."""
        result = _classify("99:999999", Code._313F, Verb.RQ, is_source=True)
        assert result == DevType.DEV

    def test_37_313f_rq_is_not_ctl(self) -> None:
        """313F RQ from 37: should not be CTL — falls back to REM (prefix)."""
        result = _classify("37:154519", Code._313F, Verb.RQ, is_source=True)
        assert result == DevType.REM

    def test_37_31d9_is_fan_as_source(self) -> None:
        """37: sending 31D9 I as source should be FAN — some FANs use 37: prefix.

        31D9 I maps to FAN in HVAC_KLASS_BY_VC_PAIR, and 37: is ambiguous
        (FAN/REM/CO2/HUM/DIS) so FAN is a valid type for 37:.
        """
        result = _classify("37:154519", Code._31D9, Verb.I_, is_source=True)
        assert result == DevType.FAN

    def test_37_31d9_as_destination_is_not_fan(self) -> None:
        """37: receiving 31D9 I should NOT be FAN — VC pair classifies sender.

        When a FAN (32:) sends I 31D9 to a REM/DIS (37:), the (I, 31D9)
        pair tells us the sender is a FAN.  The receiver should fall back
        to the prefix default (REM), not inherit the sender's classification.
        """
        result = _classify("37:169161", Code._31D9, Verb.I_, is_source=False)
        assert result == DevType.REM

    def test_37_31da_as_destination_is_not_fan(self) -> None:
        """37: receiving 31DA I/RP should NOT be FAN — same direction logic."""
        result = _classify("37:169161", Code._31DA, Verb.I_, is_source=False)
        assert result == DevType.REM
        result = _classify("37:169161", Code._31DA, Verb.RP, is_source=False)
        assert result == DevType.REM

    def test_32_31d9_is_fan(self) -> None:
        """32: sending 31D9 I should be FAN (unambiguous prefix)."""
        assert (
            _classify("32:153289", Code._31D9, Verb.I_, is_source=True)
            == DevType.FAN
        )

    def test_37_22f1_is_rem(self) -> None:
        """37: sending 22F1 I should be REM (VC pair matches valid type)."""
        assert (
            _classify("37:168270", Code._22F1, Verb.I_, is_source=True)
            == DevType.REM
        )

    def test_29_31d9_is_fan(self) -> None:
        """29: sending 31D9 I should be FAN (VC pair matches valid type)."""
        assert (
            _classify("29:146052", Code._31D9, Verb.I_, is_source=True)
            == DevType.FAN
        )

    def test_29_22f1_is_rem(self) -> None:
        """29: sending 22F1 I should be REM (VC pair matches valid type)."""
        assert (
            _classify("29:181813", Code._22F1, Verb.I_, is_source=True)
            == DevType.REM
        )

    def test_29_1298_is_co2(self) -> None:
        """29: sending 1298 I should be CO2 (VC pair matches valid type)."""
        assert (
            _classify("29:123456", Code._1298, Verb.I_, is_source=True)
            == DevType.CO2
        )

    def test_29_no_vc_falls_back_to_fan(self) -> None:
        """29: with no matching VC pair should default to FAN (prefix fallback)."""
        # 2411 is not in HVAC_KLASS_BY_VC_PAIR
        assert (
            _classify("29:123150", Code._2411, Verb.I_, is_source=True)
            == DevType.FAN
        )

    def test_37_1298_is_co2(self) -> None:
        """37: sending 1298 I should be CO2 (VC pair matches valid type)."""
        assert (
            _classify("37:123456", Code._1298, Verb.I_, is_source=True)
            == DevType.CO2
        )

    def test_18_is_hgi_regardless_of_codes(self) -> None:
        """18: is always HGI — it relays packets from all device types."""
        # 22F1 I maps to REM, but 18: is a gateway, not a REM
        assert (
            _classify("18:130236", Code._22F1, Verb.I_, is_source=True)
            == DevType.HGI
        )
        # 31DA I maps to FAN, but 18: is a gateway, not a FAN
        assert (
            _classify("18:130236", Code._31DA, Verb.I_, is_source=True)
            == DevType.HGI
        )
        # 31D9 I maps to FAN, but 18: is a gateway, not a FAN
        assert (
            _classify("18:130236", Code._31D9, Verb.I_, is_source=True)
            == DevType.HGI
        )

    def test_37_rq_31da_is_not_fan(self) -> None:
        """37: sending RQ 31DA should NOT be FAN — it's a DIS requesting status.

        31DA maps to FAN for I (broadcast) and RP (response), but RQ is a
        request.  A DIS (display remote) sends RQ 31DA to the FAN to ask
        for fan status — it's not a FAN broadcasting its own status.
        """
        # Direct RQ 31DA — not in _VC_TO_TYPE (only I and RP are)
        result = _classify("37:169161", Code._31DA, Verb.RQ, is_source=True)
        assert result != DevType.FAN

    def test_37_rq_31da_with_accumulated_codes_not_fan(self) -> None:
        """37: with 31DA in codes_seen, sending RQ 31DA, should NOT be FAN.

        The accumulated-codes check must not try all verbs — only the
        current verb.  RQ 31DA is not in _VC_TO_TYPE, so it should not
        classify as FAN even though (I, 31DA) maps to FAN.
        """
        dev = DiscoveredDevice(
            device_id="37:169161",
            first_seen="2026-07-01T10:00:00",
            last_seen="2026-07-01T10:00:00",
            likely_type="DEV",
            codes_seen=[Code._31DA, Code._1470, Code._313F],
        )
        result = _classify(
            "37:169161", Code._31DA, Verb.RQ, is_source=True, device=dev
        )
        assert result != DevType.FAN

    def test_37_i_31da_is_fan(self) -> None:
        """37: sending I 31DA (broadcast) IS FAN — it's broadcasting status."""
        result = _classify("37:169161", Code._31DA, Verb.I_, is_source=True)
        assert result == DevType.FAN


class TestConfidence:
    """Tests for confidence scoring."""

    def test_initial_high_for_binding_code(self) -> None:
        assert _initial_confidence(True, Code._3150, Verb.I_) == "high"

    def test_initial_medium_for_src(self) -> None:
        assert _initial_confidence(True, Code._0001, Verb.I_) == "medium"

    def test_initial_low_for_dst(self) -> None:
        assert _initial_confidence(False, Code._0001, Verb.I_) == "low"

    def test_recompute_high_with_binding(self) -> None:
        dev = DiscoveredDevice(
            device_id="04:056053",
            first_seen="",
            last_seen="",
            likely_type="TRV",
            zone_index="02",
            bound_to="01:145038",
        )
        assert _recompute_confidence(dev) == "high"

    def test_recompute_high_with_ctl_code(self) -> None:
        dev = DiscoveredDevice(
            device_id="01:145038",
            first_seen="",
            last_seen="",
            likely_type="CTL",
            codes_seen=[Code._1030],
        )
        assert _recompute_confidence(dev) == "high"

    def test_recompute_medium_multiple_src(self) -> None:
        dev = DiscoveredDevice(
            device_id="04:056053",
            first_seen="",
            last_seen="",
            likely_type="TRV",
            source_count=3,
        )
        assert _recompute_confidence(dev) == "medium"

    def test_recompute_low_dst_only(self) -> None:
        dev = DiscoveredDevice(
            device_id="04:056053",
            first_seen="",
            last_seen="",
            likely_type="TRV",
            destination_count=5,
            source_count=0,
        )
        assert _recompute_confidence(dev) == "low"


# ---------------------------------------------------------------------------
# DiscoveredDevice dataclass tests
# ---------------------------------------------------------------------------


class TestDiscoveredDevice:
    """Tests for the DiscoveredDevice dataclass."""

    def test_to_dict(self) -> None:
        dev = DiscoveredDevice(
            device_id="04:056053",
            first_seen="2026-07-01T10:00:00",
            last_seen="2026-07-01T10:01:00",
            likely_type="TRV",
            codes_seen=[Code._1060, Code._3150],
            bound_to="01:145038",
            zone_index="02",
            rssi=-72.0,
            confidence="high",
        )
        d = dev.to_dict()
        assert d["device_id"] == "04:056053"
        assert d["likely_type"] == "TRV"
        assert d["codes_seen"] == [Code._1060, Code._3150]
        assert d["bound_to"] == "01:145038"
        assert d["zone_index"] == "02"

    def test_from_dict(self) -> None:
        data = {
            "device_id": "04:056053",
            "first_seen": "2026-07-01T10:00:00",
            "last_seen": "2026-07-01T10:01:00",
            "likely_type": "TRV",
            "codes_seen": [Code._1060, Code._3150],
            "bound_to": "01:145038",
            "zone_index": "02",
            "rssi": -72.0,
            "confidence": "high",
            "is_battery": True,
            "src_count": 5,
            "dst_count": 2,
        }
        dev = DiscoveredDevice.from_dict(data)
        assert dev.device_id == "04:056053"
        assert dev.likely_type == "TRV"
        assert dev.is_battery is True

    def test_from_dict_ignores_extra_fields(self) -> None:
        """from_dict should ignore fields not in the dataclass."""
        data = {
            "device_id": "04:056053",
            "first_seen": "",
            "last_seen": "",
            "likely_type": "TRV",
            "status": "accepted",  # ramses_cc concern, not engine
            "enabled": True,
        }
        dev = DiscoveredDevice.from_dict(data)
        assert dev.device_id == "04:056053"
        assert not hasattr(dev, "status")

    def test_round_trip(self) -> None:
        """to_dict → from_dict → should be identical."""
        dev = DiscoveredDevice(
            device_id="04:056053",
            first_seen="2026-07-01T10:00:00",
            last_seen="2026-07-01T10:01:00",
            likely_type="TRV",
            codes_seen=[Code._1060, Code._3150],
            rssi=-72.0,
            confidence="medium",
        )
        dev2 = DiscoveredDevice.from_dict(dev.to_dict())
        assert dev2.device_id == dev.device_id
        assert dev2.codes_seen == dev.codes_seen
        assert dev2.rssi == dev.rssi


# ---------------------------------------------------------------------------
# DiscoveryScan engine tests
# ---------------------------------------------------------------------------


class TestDiscoveryScanLifecycle:
    """Tests for start/stop lifecycle."""

    def test_start_registers_handler(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan.start()
        assert gwy.add_raw_packet_handler.called
        assert scan.is_running is True

    def test_stop_unregisters_handler(self) -> None:
        gwy = make_mock_gateway()
        removed = MagicMock()
        gwy.add_raw_packet_handler = MagicMock(return_value=removed)
        scan = DiscoveryScan(gwy)
        scan.start()
        scan.stop()
        assert removed.called
        assert scan.is_running is False

    def test_start_twice_warns(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan.start()
        scan.start()  # should not double-register
        assert gwy.add_raw_packet_handler.call_count == 1

    def test_stop_without_start_is_noop(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan.stop()  # should not raise


class TestDiscoveryScanPacketHandling:
    """Tests for packet processing logic."""

    def test_new_device_from_src(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(src="04:056053", dst="01:145038", code=Code._3150)
        )
        dev = scan.get_device("04:056053")
        assert dev is not None
        assert dev.likely_type == "TRV"
        assert dev.confidence == "high"  # 3150 is a binding code
        assert dev.source_count == 1
        assert Code._3150 in dev.codes_seen

    def test_new_device_from_dst(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(src="04:056053", dst="01:145038", code=Code._3150)
        )
        # dst (01:145038) should also be recorded
        dev = scan.get_device("01:145038")
        assert dev is not None
        assert dev.destination_count == 1
        assert dev.confidence == "low"  # only seen as dst

    def test_known_device_skipped(self) -> None:
        """Known devices should be tracked for codes_seen but not re-discovered.

        Known devices (in the known_list) are tracked in the scan engine so
        that codes_seen is accumulated (needed for DHW valve inference via
        1100 code, etc.), but they should not trigger discovery notifications.
        """
        gwy = make_mock_gateway(known_list={"04:056053": {}})
        scan = DiscoveryScan(gwy)
        scan._process_packet(make_dto(src="04:056053", code=Code._3150))
        # Should be tracked (codes_seen accumulated)
        dev = scan.get_device("04:056053")
        assert dev is not None
        assert dev.codes_seen == [Code._3150]
        assert dev.confidence == "high"  # known device, high confidence

    def test_known_hgi_not_rediscovered(self) -> None:
        """A known HGI (18:) should be tracked but not re-discovered.

        HGIs are in the known_list but not in ramses_rf's schema (they're
        stripped by _strip_schema_extensions).  The scan engine should
        track them (so they appear in scan results) but not mark them as
        new discoveries (no discovery notification).
        """
        gwy = make_mock_gateway(known_list={"18:130236": {"class": "HGI"}})
        scan = DiscoveryScan(gwy)
        scan._process_packet(make_dto(src="18:130236", code=Code._22F1))
        # Should be tracked (appears in scan results)
        dev = scan.get_device("18:130236")
        assert dev is not None
        assert dev.likely_type == DevType.HGI
        # Second packet — should update, not re-create
        scan.clear_dirty()
        scan._process_packet(make_dto(src="18:130236", code=Code._10E0))
        assert scan.get_device("18:130236") is dev  # same object
        assert dev.source_count == 2
        assert Code._10E0 in dev.codes_seen

    def test_unknown_hgi_tracked(self) -> None:
        """An unknown HGI (e.g. neighbour's) should be tracked but not
        re-discovered after the first sighting."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        # First packet — should create a discovery entry
        scan._process_packet(make_dto(src="18:999999", code=Code._22F1))
        dev = scan.get_device("18:999999")
        assert dev is not None
        assert dev.likely_type == DevType.HGI
        first_seen = dev.first_seen
        # Second packet — should update, not re-create
        scan._process_packet(make_dto(src="18:999999", code=Code._10E0))
        dev2 = scan.get_device("18:999999")
        assert dev2 is not None
        assert dev2.first_seen == first_seen  # same entry, not re-created
        assert dev2.source_count == 2

    def test_known_in_schema_skipped(self) -> None:
        """Known devices in schema should be tracked for codes_seen but
        not re-discovered (no discovery notification)."""
        gwy = make_mock_gateway(schema={"01:145038": {}})
        scan = DiscoveryScan(gwy)
        scan._process_packet(make_dto(src="01:145038", code=Code._2E04))
        # Should be tracked (codes_seen accumulated)
        dev = scan.get_device("01:145038")
        assert dev is not None
        assert dev.codes_seen == [Code._2E04]

    def test_known_in_registry_only_not_skipped(self) -> None:
        """A device in the device_registry but NOT in known_list/schema
        must be re-discoverable (Schema-as-Source-of-Truth, issue 767).
        The registry is derived state; declared intent (schema/known_list)
        wins."""
        gwy = make_mock_gateway(device_by_id={"04:056053": MagicMock()})
        scan = DiscoveryScan(gwy)
        scan._process_packet(make_dto(src="04:056053", code=Code._3150))
        # Registry-only device → NOT known → should be discovered
        assert scan.get_device("04:056053") is not None

    def test_rssi_running_average(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        # First packet
        scan._process_packet(
            make_dto(src="04:056053", code=Code._3150, rssi="-70")
        )
        dev = scan.get_device("04:056053")
        assert dev is not None
        assert dev.rssi == -70.0
        # Second packet — should average
        scan._process_packet(
            make_dto(src="04:056053", code=Code._30C9, rssi="-80")
        )
        assert dev.rssi == -75.0

    def test_rssi_not_updated_from_dst(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="04:056053", dst="01:145038", code=Code._3150, rssi="-70"
            )
        )
        # dst device should not get rssi from this packet
        dst_dev = scan.get_device("01:145038")
        assert dst_dev is not None
        assert dst_dev.rssi is None

    def test_zone_binding_extracted(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="04:056053",
                dst="01:145038",
                code=Code._3150,
                payload="02C8",
            )
        )
        dev = scan.get_device("04:056053")
        assert dev is not None
        assert dev.zone_index == "02"
        assert dev.bound_to == "01:145038"
        assert dev.confidence == "high"

    def test_thm_zone_binding_via_000a(self) -> None:
        """THM (22:) sends RQ 000A with its zone_index as payload (issue 813).

        A room thermostat like 22:012299 sends ``RQ 000A 001 01`` to the CTL,
        where ``01`` is the zone index.  The scan engine should extract this
        and set zone_index on the THM.
        """
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="22:012299", dst="01:216136", code=Code._000A, payload="01"
            )
        )
        dev = scan.get_device("22:012299")
        assert dev is not None
        assert dev.zone_index == "01"
        assert dev.bound_to == "01:216136"
        assert dev.confidence == "high"

    def test_ctl_no_zone_binding_from_000a(self) -> None:
        """CTL (01:) must not get zone_index from its own 000A packets (issue 813).

        The CTL sends 000A with zone config for multiple zones.  The first
        2 hex chars are the zone_index of the zone being configured, NOT the
        CTL's own zone.  Setting zone_index on the CTL corrupts its comment
        and schema entry.
        """
        gwy = make_mock_gateway(known_list={"01:216136": {}})
        scan = DiscoveryScan(gwy)
        # CTL sends 000A to HGI with zone 02 config
        scan._process_packet(
            make_dto(
                src="01:216136", dst="18:072981", code=Code._000A, payload="02"
            )
        )
        dev = scan.get_device("01:216136")
        assert dev is not None
        # CTL must NOT have zone_index set
        assert dev.zone_index is None
        assert dev.bound_to is None

    def test_bdr_3b00_sets_domain_id_fc(self) -> None:
        """BDR broadcasting 3B00 as I sets domain_id=FC (issue 834).

        A BDR acting as the boiler relay broadcasts 3B00 (TPI state) as I.
        The scan engine must capture domain_id=FC so ramses_cc can classify
        it as appliance_control rather than hotwater_valve.
        """
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="13:121025",
                dst="--:------",
                code=Code._3B00,
                payload="00C8",
            )
        )
        dev = scan.get_device("13:121025")
        assert dev is not None
        assert dev.likely_type == "BDR"
        assert dev.domain_id == "FC"
        assert dev.confidence == "high"

    def test_bdr_3ef0_sets_domain_id_fc(self) -> None:
        """BDR broadcasting 3EF0 as I also sets domain_id=FC (issue 834)."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="13:121025",
                dst="--:------",
                code=Code._3EF0,
                payload="0000FF",
            )
        )
        dev = scan.get_device("13:121025")
        assert dev is not None
        assert dev.domain_id == "FC"

    def test_bdr_3ef1_rp_no_domain_id(self) -> None:
        """BDR sending 3EF1 RP (directed reply to HGI) is NOT appliance signal."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="13:121025",
                dst="18:203273",
                code=Code._3EF1,
                verb=Verb.RP,
                payload="00011D011D00FF",
            )
        )
        dev = scan.get_device("13:121025")
        assert dev is not None
        assert dev.domain_id is None

    def test_bdr_1100_no_domain_id(self) -> None:
        """BDR sending 1100 (boiler params) does NOT set domain_id.

        1100 is sent by both appliance controls and DHW valve relays, so it
        is not a reliable appliance_control signal on its own.
        """
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="13:121025",
                dst="--:------",
                code=Code._1100,
                payload="00180400007FFF01",
            )
        )
        dev = scan.get_device("13:121025")
        assert dev is not None
        assert dev.domain_id is None

    def test_known_bdr_3b00_sets_domain_id(self) -> None:
        """A known BDR (in known_list) broadcasting 3B00 gets domain_id=FC.

        This is the issue 834 scenario: the BDR was already in the schema
        but had no domain_id.  When it broadcasts 3B00, the scan engine
        must capture the FC domain so the comment and schema entry can be
        rebuilt correctly.
        """
        gwy = make_mock_gateway(known_list={"13:121025": {}})
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="13:121025",
                dst="--:------",
                code=Code._3B00,
                payload="00C8",
            )
        )
        dev = scan.get_device("13:121025")
        assert dev is not None
        assert dev.domain_id == "FC"

    def test_domain_id_in_to_dict_round_trip(self) -> None:
        """domain_id survives to_dict/from_dict round-trip."""
        dev = DiscoveredDevice(
            device_id="13:121025",
            first_seen="2026-07-18T14:36:23",
            last_seen="2026-07-18T14:36:23",
            likely_type="BDR",
            domain_id="FC",
        )
        d = dev.to_dict()
        assert d["domain_id"] == "FC"
        dev2 = DiscoveredDevice.from_dict(d)
        assert dev2.domain_id == "FC"

    def test_codes_seen_deduplicated_and_sorted(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(make_dto(src="04:056053", code=Code._3150))
        scan._process_packet(make_dto(src="04:056053", code=Code._1060))
        scan._process_packet(
            make_dto(src="04:056053", code=Code._3150)
        )  # duplicate
        dev = scan.get_device("04:056053")
        assert dev is not None
        assert dev.codes_seen == [Code._1060, Code._3150]  # sorted, no dupes

    def test_battery_flag_set(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(make_dto(src="04:056053", code=Code._1060))
        dev = scan.get_device("04:056053")
        assert dev is not None
        assert dev.is_battery is True

    def test_addr3_processed(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="01:145038",
                dst="18:006402",
                addr3="04:056053",
                code=Code._000C,
            )
        )
        dev = scan.get_device("04:056053")
        assert dev is not None
        assert dev.destination_count == 1  # addr3 treated as non-src

    def test_broadcast_address_skipped(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(src="01:145038", dst="18:73030", code=Code._2E04)
        )
        # 18:73030 is broadcast — should not be in discovery list
        assert scan.get_device("18:73030") is None

    def test_hvac_fan_discovered(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="32:157747", dst="18:006402", code=Code._31DA, verb=Verb.I_
            )
        )
        dev = scan.get_device("32:157747")
        assert dev is not None
        assert dev.likely_type == "FAN"

    def test_hvac_rem_discovered(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="37:179540", dst="32:157747", code=Code._22F1, verb=Verb.I_
            )
        )
        dev = scan.get_device("37:179540")
        assert dev is not None
        assert dev.likely_type == "REM"

    def test_dirty_flag_set_on_new_device(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        assert scan.is_dirty is False
        scan._process_packet(make_dto(src="04:056053", code=Code._3150))
        assert scan.is_dirty is True

    def test_clear_dirty(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(make_dto(src="04:056053", code=Code._3150))
        scan.clear_dirty()
        assert scan.is_dirty is False


class TestDiscoveryScanGetDevices:
    """Tests for get_devices with filters."""

    def test_get_all_devices(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(make_dto(src="04:056053", code=Code._3150))
        scan._process_packet(make_dto(src="01:145038", code=Code._2E04))
        assert len(scan.get_devices()) == 2

    def test_filter_by_type(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(make_dto(src="04:056053", code=Code._3150))
        scan._process_packet(make_dto(src="01:145038", code=Code._2E04))
        trvs = scan.get_devices(likely_type="TRV")
        assert len(trvs) == 1
        assert trvs[0].device_id == "04:056053"

    def test_filter_by_min_confidence(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        # 04:056053 sends binding code → high
        scan._process_packet(
            make_dto(src="04:056053", dst="01:145038", code=Code._3150)
        )
        # 01:145038 only seen as dst → low
        high_only = scan.get_devices(min_confidence="high")
        assert len(high_only) == 1
        assert high_only[0].device_id == "04:056053"

    def test_device_count(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        assert scan.device_count() == 0
        scan._process_packet(make_dto(src="04:056053", code=Code._3150))
        assert scan.device_count() == 2  # src + dst both recorded


class TestDiscoveryScanRemoveDevice:
    """Tests for remove_device."""

    def test_remove_existing(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(make_dto(src="04:056053", code=Code._3150))
        assert scan.remove_device("04:056053") is True
        assert scan.get_device("04:056053") is None

    def test_remove_nonexistent(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        assert scan.remove_device("99:999999") is False


class TestDiscoveryScanExportImport:
    """Tests for JSON export/import."""

    def test_export_json_structure(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(src="04:056053", dst="01:145038", code=Code._3150)
        )
        data = json.loads(scan.export_json())
        assert "version" in data
        assert "devices" in data
        assert len(data["devices"]) == 2

    def test_export_import_round_trip(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(src="04:056053", dst="01:145038", code=Code._3150)
        )
        scan._process_packet(make_dto(src="04:056053", code=Code._1060))
        exported = scan.export_json()

        # New scan, import the data
        scan2 = DiscoveryScan(make_mock_gateway())
        scan2.import_json(exported)
        assert scan2.device_count() == 2
        dev = scan2.get_device("04:056053")
        assert dev is not None
        assert dev.likely_type == "TRV"
        assert Code._3150 in dev.codes_seen
        assert Code._1060 in dev.codes_seen

    def test_import_clears_dirty(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(make_dto(src="04:056053", code=Code._3150))
        assert scan.is_dirty is True
        scan.import_json(scan.export_json())
        assert scan.is_dirty is False

    def test_export_sorted_by_device_id(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(make_dto(src="04:056053", code=Code._3150))
        scan._process_packet(make_dto(src="01:145038", code=Code._2E04))
        data = json.loads(scan.export_json())
        ids = [d["device_id"] for d in data["devices"]]
        assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# Integration: no topology mutation
# ---------------------------------------------------------------------------


class TestNoTopologyMutation:
    """Verify the scan never mutates topology."""

    def test_no_get_device_calls(self) -> None:
        """The scan should never call get_device on the registry."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(make_dto(src="04:056053", code=Code._3150))
        scan._process_packet(make_dto(src="01:145038", code=Code._2E04))
        # get_device should never have been called
        gwy.device_registry.get_device.assert_not_called()

    def test_no_schema_modification(self) -> None:
        """The scan should not modify the schema."""
        original_schema: dict[str, Any] = {"01:145038": {}}
        gwy = make_mock_gateway(schema=original_schema)
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(src="04:056053", dst="01:145038", code=Code._3150)
        )
        # Schema should be unchanged
        assert gwy._gwy_config.schema == original_schema


# ---------------------------------------------------------------------------
# Integration: out-of-order discovery (TRV before CTL)
# ---------------------------------------------------------------------------


class TestOutOfOrderDiscovery:
    """Tests for the out-of-order discovery scenario."""

    def test_trv_seen_before_ctl(self) -> None:
        """TRV broadcasts to a CTL address before CTL is seen.

        The CTL should be recorded as a referenced-but-unseen device.
        """
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        # TRV sends to CTL — CTL is dst
        scan._process_packet(
            make_dto(src="04:056053", dst="01:145038", code=Code._3150)
        )
        # TRV should be discovered with binding info
        trv = scan.get_device("04:056053")
        assert trv is not None
        assert trv.bound_to == "01:145038"
        assert trv.zone_index == "02"
        # CTL should also be recorded (as dst)
        ctl = scan.get_device("01:145038")
        assert ctl is not None
        assert ctl.confidence == "low"  # only seen as dst so far

    def test_ctl_appears_later_enriches(self) -> None:
        """When CTL starts sending, its confidence should upgrade."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        # Phase 1: TRV seen, CTL only as dst
        scan._process_packet(
            make_dto(src="04:056053", dst="01:145038", code=Code._3150)
        )
        ctl = scan.get_device("01:145038")
        assert ctl is not None
        assert ctl.confidence == "low"

        # Phase 2: CTL sends its own traffic
        scan._process_packet(
            make_dto(src="01:145038", dst="18:006402", code=Code._2E04)
        )
        assert ctl.source_count == 1
        assert Code._2E04 in ctl.codes_seen
        # Confidence should upgrade to medium (src_count >= 1 + codes >= 2)
        assert ctl.confidence in ("medium", "high")


# ---------------------------------------------------------------------------
# Integration: virtual RF with mixed CH + HVAC traffic
# ---------------------------------------------------------------------------


class TestVirtualRfIntegration:
    """Integration test using the virtual RF to simulate live traffic.

    Sends mixed CH + HVAC packets through a virtual serial port and verifies
    the scan engine discovers and classifies them correctly, even with
    enforce_known_list=True.
    """

    @pytest.mark.asyncio
    async def test_mixed_ch_hvac_discovery(self) -> None:
        """Scan discovers CH + HVAC devices from simulated RF traffic.

        Uses the virtual RF harness to send raw packet frames through a
        virtual serial port, just like real RF traffic.
        """
        from tests_rf.virtual_rf import HgiFwTypes, VirtualRf

        HGI_ID = "18:222222"
        CTL_ID = "01:145038"
        TRV_ID = "04:056053"
        DHW_ID = "07:046947"
        BDR_ID = "13:042605"
        FAN_ID = "32:157747"
        REM_ID = "37:179540"

        # Raw packet frames (evofw3 format: no RSSI, gateway adds it)
        # Must be terminated with \r\n for the virtual RF to process them
        # Payload length must match the declared hex length byte
        raw_packets: list[bytes] = [
            # CTL broadcasts system mode
            b" I --- 01:145038 18:222222 --:------ 2E04 003 000200\r\n",
            # CTL sends zone device map (000C)
            b" I --- 01:145038 18:222222 --:------ 000C 006 000F0035D5B1\r\n",
            # TRV sends heat demand to CTL (zone 02)
            b" I --- 04:056053 01:145038 --:------ 3150 006 02C800000000\r\n",
            # TRV sends battery info
            b" I --- 04:056053 01:145038 --:------ 1060 003 00C800\r\n",
            # DHW sensor sends to CTL
            b" I --- 07:046947 01:145038 --:------ 10A0 006 01C800000000\r\n",
            # BDR sends state
            b" I --- 13:042605 01:145038 --:------ 0008 002 00FF\r\n",
            # FAN broadcasts fan state (31DA, 30 bytes payload)
            b" I --- 32:157747 --:------ 32:157747 31DA 030 00EF007FFF3A2F04C404E204A904BA68000003C8C80000EFEF20A91F0500\r\n",
            # FAN broadcasts fan info (31D9, 17 bytes payload)
            b" I --- 32:157747 --:------ 32:157747 31D9 017 001A020020202020202020202020202008\r\n",
            # REM sends fan mode to FAN
            b" I --- 37:179540 32:157747 --:------ 22F1 003 000107\r\n",
            # REM sends battery
            b" I --- 37:179540 32:157747 --:------ 1060 003 00C800\r\n",
        ]

        rf = VirtualRf(2)
        try:
            rf.set_gateway(rf.ports[0], HGI_ID, fw_type=HgiFwTypes.EVOFW3)

            from unittest.mock import patch

            from ramses_rf.gateway import Gateway, GatewayConfig
            from ramses_tx.config import EngineConfig

            engine_config = EngineConfig(
                disable_qos=True,
                enforce_known_list=True,
                disable_sending=True,
            )
            gwy_config = GatewayConfig(
                disable_discovery=True,
                enable_eavesdrop=False,
                engine=engine_config,
                known_list={HGI_ID: {}},  # only HGI — everything else unknown
                schema={},
            )

            with patch("ramses_tx.discovery.comports", rf.comports):
                gwy = Gateway(rf.ports[0], config=gwy_config)
                await gwy.start()

            scan = DiscoveryScan(gwy)
            scan.start()

            # Dump packets into the virtual RF (one at a time to avoid buffer overflow)
            for packet in raw_packets:
                await rf.dump_frames_to_rf([packet])
                await asyncio.sleep(0.05)
            await asyncio.sleep(1)  # let packets fully process

            scan.stop()

            # Verify registry only has HGI
            registry_ids = set(gwy.device_registry.device_by_id.keys())
            assert registry_ids == {HGI_ID}, (
                f"Registry should only have HGI, got: {registry_ids}"
            )

            # Verify scan discovered all unknown devices
            discovered = {d.device_id: d for d in scan.get_devices()}
            assert CTL_ID in discovered, (
                f"CTL not discovered: {list(discovered.keys())}"
            )
            assert TRV_ID in discovered, (
                f"TRV not discovered: {list(discovered.keys())}"
            )
            assert DHW_ID in discovered, (
                f"DHW not discovered: {list(discovered.keys())}"
            )
            assert BDR_ID in discovered, (
                f"BDR not discovered: {list(discovered.keys())}"
            )
            assert FAN_ID in discovered, (
                f"FAN not discovered: {list(discovered.keys())}"
            )
            assert REM_ID in discovered, (
                f"REM not discovered: {list(discovered.keys())}"
            )

            # Verify classification
            assert discovered[CTL_ID].likely_type == "CTL"
            assert discovered[TRV_ID].likely_type == "TRV"
            assert discovered[DHW_ID].likely_type == "DHW"
            assert discovered[BDR_ID].likely_type == "BDR"
            assert discovered[FAN_ID].likely_type == "FAN"
            assert discovered[REM_ID].likely_type == "REM"

            # Verify TRV has zone binding
            trv = discovered[TRV_ID]
            assert trv.zone_index == "02"
            assert trv.bound_to == CTL_ID
            assert trv.confidence == "high"
            assert trv.is_battery is True

            # Verify FAN is classified as FAN (not REM, despite 22F1)
            fan = discovered[FAN_ID]
            assert fan.likely_type == "FAN"
            assert Code._31DA in fan.codes_seen

            # Verify REM is classified as REM
            rem = discovered[REM_ID]
            assert rem.likely_type == "REM"
            assert Code._22F1 in rem.codes_seen

            await gwy.stop()
        finally:
            await rf.stop()

    @pytest.mark.asyncio
    async def test_resume_after_export_import(self) -> None:
        """Scan can export its state, a new scan can import it and continue.

        Simulates an HA restart: scan1 discovers devices, exports JSON,
        scan2 imports the JSON and continues scanning, discovering new
        devices that appeared after the restart.
        """
        from tests_rf.virtual_rf import HgiFwTypes, VirtualRf

        HGI_ID = "18:333333"
        CTL_ID = "01:145038"
        TRV_ID = "04:056053"
        FAN_ID = "32:157747"

        # Phase 1 packets: CTL + TRV
        phase1_packets: list[bytes] = [
            b" I --- 01:145038 18:333333 --:------ 2E04 003 000200\r\n",
            b" I --- 04:056053 01:145038 --:------ 3150 006 02C800000000\r\n",
        ]

        # Phase 2 packets: FAN (new device after "restart")
        phase2_packets: list[bytes] = [
            b" I --- 32:157747 --:------ 32:157747 31DA 030 00EF007FFF3A2F04C404E204A904BA68000003C8C80000EFEF20A91F0500\r\n",
        ]

        rf = VirtualRf(2)
        try:
            rf.set_gateway(rf.ports[0], HGI_ID, fw_type=HgiFwTypes.EVOFW3)

            from unittest.mock import patch

            from ramses_rf.gateway import Gateway, GatewayConfig
            from ramses_tx.config import EngineConfig

            engine_config = EngineConfig(
                disable_qos=True,
                enforce_known_list=True,
                disable_sending=True,
            )
            gwy_config = GatewayConfig(
                disable_discovery=True,
                enable_eavesdrop=False,
                engine=engine_config,
                known_list={HGI_ID: {}},
                schema={},
            )

            # --- Phase 1: scan and export ---
            with patch("ramses_tx.discovery.comports", rf.comports):
                gwy = Gateway(rf.ports[0], config=gwy_config)
                await gwy.start()

            scan1 = DiscoveryScan(gwy)
            scan1.start()
            for packet in phase1_packets:
                await rf.dump_frames_to_rf([packet])
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.5)
            scan1.stop()

            devices1 = {d.device_id: d for d in scan1.get_devices()}
            assert CTL_ID in devices1
            assert TRV_ID in devices1
            assert FAN_ID not in devices1  # FAN not seen yet

            # Export state
            json_state = scan1.export_json()
            assert CTL_ID in json_state
            assert TRV_ID in json_state

            await gwy.stop()

            # --- Phase 2: new scan imports state and continues ---
            with patch("ramses_tx.discovery.comports", rf.comports):
                gwy2 = Gateway(rf.ports[0], config=gwy_config)
                await gwy2.start()

            scan2 = DiscoveryScan(gwy2)
            scan2.import_json(json_state)  # resume from exported state

            # Verify imported devices are present before scanning
            imported = {d.device_id: d for d in scan2.get_devices()}
            assert CTL_ID in imported
            assert TRV_ID in imported
            assert FAN_ID not in imported  # not yet

            scan2.start()
            for packet in phase2_packets:
                await rf.dump_frames_to_rf([packet])
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.5)
            scan2.stop()

            # Verify all devices now present
            devices2 = {d.device_id: d for d in scan2.get_devices()}
            assert CTL_ID in devices2  # from import
            assert TRV_ID in devices2  # from import
            assert FAN_ID in devices2  # newly discovered
            assert devices2[FAN_ID].likely_type == "FAN"

            await gwy2.stop()
        finally:
            await rf.stop()

    @pytest.mark.asyncio
    async def test_hvac_co2_and_hum_classification(self) -> None:
        """Scan correctly classifies CO2 and HUM devices via VC pairs.

        37: prefix is ambiguous (REM, CO2, HUM all use it), so the VC pair
        must distinguish them:
        - I 1298 → CO2
        - I 22F1 → REM
        - I 31E0 → HUM (if in HVAC_KLASS_BY_VC_PAIR)
        """
        from tests_rf.virtual_rf import HgiFwTypes, VirtualRf

        HGI_ID = "18:444444"
        CO2_ID = "37:111111"
        REM_ID = "37:222222"

        raw_packets: list[bytes] = [
            # CO2 sends air quality (I 1298)
            b" I --- 37:111111 18:444444 --:------ 1298 013 00EF007FFF3A2F04C404E204A9\r\n",
            # REM sends fan mode (I 22F1)
            b" I --- 37:222222 32:157747 --:------ 22F1 003 000107\r\n",
        ]

        rf = VirtualRf(2)
        try:
            rf.set_gateway(rf.ports[0], HGI_ID, fw_type=HgiFwTypes.EVOFW3)

            from unittest.mock import patch

            from ramses_rf.gateway import Gateway, GatewayConfig
            from ramses_tx.config import EngineConfig

            engine_config = EngineConfig(
                disable_qos=True,
                enforce_known_list=True,
                disable_sending=True,
            )
            gwy_config = GatewayConfig(
                disable_discovery=True,
                enable_eavesdrop=False,
                engine=engine_config,
                known_list={HGI_ID: {}},
                schema={},
            )

            with patch("ramses_tx.discovery.comports", rf.comports):
                gwy = Gateway(rf.ports[0], config=gwy_config)
                await gwy.start()

            scan = DiscoveryScan(gwy)
            scan.start()
            for packet in raw_packets:
                await rf.dump_frames_to_rf([packet])
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.5)
            scan.stop()

            discovered = {d.device_id: d for d in scan.get_devices()}

            # CO2 should be classified as CO2 (via I 1298 VC pair)
            assert CO2_ID in discovered, (
                f"CO2 not discovered: {list(discovered.keys())}"
            )
            assert discovered[CO2_ID].likely_type == "CO2", (
                f"Expected CO2, got {discovered[CO2_ID].likely_type}"
            )

            # REM should be classified as REM (via I 22F1 VC pair)
            assert REM_ID in discovered, (
                f"REM not discovered: {list(discovered.keys())}"
            )
            assert discovered[REM_ID].likely_type == "REM", (
                f"Expected REM, got {discovered[REM_ID].likely_type}"
            )

            # Both are 37: prefix but different types — VC pair disambiguates
            assert (
                discovered[CO2_ID].likely_type
                != discovered[REM_ID].likely_type
            )

            await gwy.stop()
        finally:
            await rf.stop()


# ---------------------------------------------------------------------------
# HVAC topology inference — FAN reply (RP) to REM/CO2 confirms binding
# ---------------------------------------------------------------------------

FAN_ID = "32:153289"
REM_29 = "29:176861"  # real REM (29: prefix)
CO2_37 = "37:126776"  # CO2 sensor that also sends 31E0


class TestHvacParentInference:
    """Tests for inferring bound_to from HVAC operational traffic.

    Two inference directions:

    1. **FAN→REM**: a FAN (32:) sending a directed I/RP to a REM/CO2
       confirms binding — the FAN is the controller communicating with
       its paired remote.

    2. **REM→FAN**: a REM/CO2 sending a directed packet (W/RQ/I) to a
       FAN (32:) confirms binding.  This is more authoritative than
       FAN→REM — the REM only sends to the FAN it was 1FC9-paired with
       (not any FAN in range).  A directed I is proof (the REM addresses
       its paired FAN specifically); a broadcast I (dst = --:------)
       is NOT proof (the REM could be a neighbour's remote broadcasting).

    See schema_architecture.md: "How HVAC topology COULD be derived
    from traffic".
    """

    async def test_fan_reply_infers_parent(self) -> None:
        """A FAN (32:) replying RP to a REM should set bound_to on the
        REM.  The REM's RQ to the FAN also sets bound_to (REM→FAN
        inference) — a directed RQ is proof of binding because the REM
        only queries its 1FC9-paired FAN."""
        scan = DiscoveryScan(gateway=make_mock_gateway())
        scan.start()
        try:
            # REM sends RQ to FAN — REM→FAN inference fires immediately
            # (RQ is a directed query, not a broadcast)
            dto1 = make_dto(
                src=REM_29,
                dst=FAN_ID,
                code=Code._31DA,
                verb=Verb.RQ,
                payload="00",
            )
            scan._process_packet(dto1)
            dev = scan.get_device(REM_29)
            assert dev is not None
            assert dev.bound_to == FAN_ID  # REM→FAN inference

            # FAN replies RP to REM — FAN→REM inference would also set
            # bound_to (but it's already set from the RQ above)
            dto2 = make_dto(
                src=FAN_ID,
                dst=REM_29,
                code=Code._31DA,
                verb=Verb.RP,
                payload="00EF007FFF424A084807A8082E075E6800C803C8C80000EFEF208A208A00",
            )
            scan._process_packet(dto2)
            dev = scan.get_device(REM_29)
            assert dev is not None
            assert dev.bound_to == FAN_ID
        finally:
            scan.stop()

    async def test_rem_broadcast_to_fan_does_not_infer(self) -> None:
        """A REM sending broadcast I|22F1 (dst = --:------) should NOT
        set bound_to — a broadcast is not proof of binding (the REM
        could be a neighbour's remote broadcasting)."""
        scan = DiscoveryScan(gateway=make_mock_gateway())
        scan.start()
        try:
            dto = make_dto(
                src=REM_29,
                dst="--:------",
                code=Code._22F1,
                verb=Verb.I_,
                payload="000404",
            )
            scan._process_packet(dto)
            dev = scan.get_device(REM_29)
            assert dev is not None
            assert dev.bound_to is None  # NOT confirmed (broadcast I)
        finally:
            scan.stop()

    async def test_rem_directed_i_to_fan_infers_parent(self) -> None:
        """A REM sending directed I|22F1 to a FAN (dst = 32:...) should
        set bound_to — a directed I is proof because the REM addresses
        its 1FC9-paired FAN specifically (not a broadcast)."""
        scan = DiscoveryScan(gateway=make_mock_gateway())
        scan.start()
        try:
            dto = make_dto(
                src=REM_29,
                dst=FAN_ID,
                code=Code._22F1,
                verb=Verb.I_,
                payload="000404",
            )
            scan._process_packet(dto)
            dev = scan.get_device(REM_29)
            assert dev is not None
            assert dev.bound_to == FAN_ID  # directed I = proof
            assert dev.confidence == "high"
        finally:
            scan.stop()

    async def test_rem_w_to_fan_infers_parent(self) -> None:
        """A REM sending W|22F1 to a FAN should set bound_to — a
        directed W is proof of binding because the REM only sends
        commands to its 1FC9-paired FAN."""
        scan = DiscoveryScan(gateway=make_mock_gateway())
        scan.start()
        try:
            dto = make_dto(
                src=REM_29,
                dst=FAN_ID,
                code=Code._22F1,
                verb=Verb.W_,
                payload="000404",
            )
            scan._process_packet(dto)
            dev = scan.get_device(REM_29)
            assert dev is not None
            assert dev.bound_to == FAN_ID  # REM→FAN inference (directed W)
            assert dev.confidence == "high"
        finally:
            scan.stop()

    async def test_rem_rq_to_fan_infers_parent(self) -> None:
        """A REM sending RQ|2411 to a FAN should set bound_to — a
        directed RQ is proof of binding (same as W)."""
        scan = DiscoveryScan(gateway=make_mock_gateway())
        scan.start()
        try:
            dto = make_dto(
                src=REM_29,
                dst=FAN_ID,
                code=Code._2411,
                verb=Verb.RQ,
                payload="00",
            )
            scan._process_packet(dto)
            dev = scan.get_device(REM_29)
            assert dev is not None
            assert dev.bound_to == FAN_ID  # REM→FAN inference (directed RQ)
            assert dev.confidence == "high"
        finally:
            scan.stop()

    async def test_fan_reply_to_co2_infers_parent(self) -> None:
        """A FAN (32:) replying RP to a CO2 should set bound_to."""
        scan = DiscoveryScan(gateway=make_mock_gateway())
        scan.start()
        try:
            # FAN replies RP to CO2
            dto = make_dto(
                src=FAN_ID,
                dst=CO2_37,
                code=Code._31DA,
                verb=Verb.RP,
                payload="00EF007FFF424A084807A8082E075E6800C803C8C80000EFEF208A208A00",
            )
            scan._process_packet(dto)
            dev = scan.get_device(CO2_37)
            assert dev is not None
            assert dev.bound_to == FAN_ID
        finally:
            scan.stop()

    async def test_i_verb_from_fan_infers_binding(self) -> None:
        """A FAN sending I (directed) to a REM should infer bound_to.

        A directed I packet from the FAN to a specific REM is strong evidence
        of binding — the FAN is the controller and it's communicating with
        its paired remote.  This is different from a REM broadcasting to a
        FAN (which doesn't prove binding).
        """
        scan = DiscoveryScan(gateway=make_mock_gateway())
        scan.start()
        try:
            dto = make_dto(
                src=FAN_ID,
                dst=REM_29,
                code=Code._31DA,
                verb=Verb.I_,  # directed I from FAN
                payload="00EF007FFF424A",
            )
            scan._process_packet(dto)
            dev = scan.get_device(REM_29)
            assert dev is not None
            assert dev.bound_to == FAN_ID
        finally:
            scan.stop()

    async def test_non_32_src_does_not_infer(self) -> None:
        """A non-FAN (not 32:) replying RP should NOT infer bound_to."""
        scan = DiscoveryScan(gateway=make_mock_gateway())
        scan.start()
        try:
            dto = make_dto(
                src="37:168270",  # not a FAN
                dst=REM_29,
                code=Code._31DA,
                verb=Verb.RP,
                payload="00EF007FFF424A",
            )
            scan._process_packet(dto)
            dev = scan.get_device(REM_29)
            assert dev is not None
            assert dev.bound_to is None
        finally:
            scan.stop()

    async def test_zone_binding_takes_precedence(self) -> None:
        """If zone binding sets bound_to first, HVAC inference must not
        overwrite it."""
        scan = DiscoveryScan(gateway=make_mock_gateway())
        scan.start()
        try:
            # First: zone binding to a different FAN
            dto1 = make_dto(
                src=REM_29,
                dst="32:999999",
                code=Code._000C,
                verb=Verb.I_,
                payload="00FFFF02C8",
            )
            scan._process_packet(dto1)
            dev = scan.get_device(REM_29)
            assert dev is not None
            assert dev.bound_to == "32:999999"

            # Then: FAN replies RP to REM — should NOT overwrite
            dto2 = make_dto(
                src=FAN_ID,
                dst=REM_29,
                code=Code._31DA,
                verb=Verb.RP,
                payload="00EF007FFF424A",
            )
            scan._process_packet(dto2)
            dev = scan.get_device(REM_29)
            assert dev is not None
            assert dev.bound_to == "32:999999"  # zone binding wins
        finally:
            scan.stop()

    async def test_fan_reply_enriches_existing_device(self) -> None:
        """If a REM is discovered without bound_to, a later FAN reply
        should enrich it with bound_to."""
        scan = DiscoveryScan(gateway=make_mock_gateway())
        scan.start()
        try:
            # First: REM discovered via a non-HVAC code (no bound_to)
            dto1 = make_dto(
                src=REM_29,
                dst="63:262142",
                code=Code._10E0,
                verb=Verb.I_,
                payload="00",
            )
            scan._process_packet(dto1)
            dev = scan.get_device(REM_29)
            assert dev is not None
            assert dev.bound_to is None

            # Then: FAN replies RP to REM — should set bound_to
            dto2 = make_dto(
                src=FAN_ID,
                dst=REM_29,
                code=Code._31DA,
                verb=Verb.RP,
                payload="00EF007FFF424A",
            )
            scan._process_packet(dto2)
            dev = scan.get_device(REM_29)
            assert dev is not None
            assert dev.bound_to == FAN_ID
        finally:
            scan.stop()


class TestEvidenceBasedContradiction:
    """Tests for evidence-based contradiction re-classification.

    The scan engine re-classifies known devices when source packets
    contradict the declared class.  But only evidence-based
    classifications (VC pair matches, CTL-only codes) should count as
    contradictions — prefix fallbacks (e.g. 37: -> REM) are guesses and
    should NOT contradict a declared class.

    Without this, a CO2 device (37: prefix) sending generic codes like
    10E0 would be re-classified as REM just because 37: falls to REM
    by prefix (false positive on hass).
    """

    def test_co2_not_reclassified_by_generic_code(self) -> None:
        """A CO2 device sending 10E0 (generic) should NOT be re-classified.

        37:126776 is declared as CO2 in the known_list.  It sends 10E0
        (a generic code).  _classify returns REM for 37: prefix fallback,
        but this is a guess, not evidence -- so it should NOT increment
        contradiction_count or re-classify the device.
        """
        gwy = make_mock_gateway(known_list={"37:126776": {"class": "CO2"}})
        scan = DiscoveryScan(gwy)
        # Send 3+ generic 10E0 packets -- would trigger re-classification
        # if prefix fallbacks counted as contradictions
        for _ in range(5):
            scan._process_packet(
                make_dto(src="37:126776", code=Code._10E0, verb=Verb.I_)
            )
        dev = scan.get_device("37:126776")
        assert dev is not None
        assert dev.likely_type == DevType.CO2  # still CO2
        assert dev.contradiction_count == 0  # no contradiction counted
        scan.stop()

    def test_co2_not_reclassified_by_31e0(self) -> None:
        """A CO2 device sending 31E0 (vent_demand) should NOT be re-classified.

        31E0 is not in _VC_TO_TYPE, so _classify falls to prefix (REM).
        But 31E0 is a generic HVAC code -- not evidence of REM.
        """
        gwy = make_mock_gateway(known_list={"37:126776": {"class": "CO2"}})
        scan = DiscoveryScan(gwy)
        for _ in range(5):
            scan._process_packet(
                make_dto(src="37:126776", code=Code._31E0, verb=Verb.I_)
            )
        dev = scan.get_device("37:126776")
        assert dev is not None
        assert dev.likely_type == DevType.CO2
        assert dev.contradiction_count == 0
        scan.stop()

    def test_co2_matching_packet_resets_contradiction(self) -> None:
        """A CO2 device sending 1298 (CO2 VC pair) resets contradiction count.

        (I, 1298) maps to CO2 in _VC_TO_TYPE -- this is evidence-based
        and matches the declared class, so it resets the count.
        """
        gwy = make_mock_gateway(known_list={"37:126776": {"class": "CO2"}})
        scan = DiscoveryScan(gwy)
        # Send a 1298 packet (evidence-based, matches CO2)
        scan._process_packet(
            make_dto(src="37:126776", code=Code._1298, verb=Verb.I_)
        )
        dev = scan.get_device("37:126776")
        assert dev is not None
        assert dev.likely_type == DevType.CO2
        assert dev.contradiction_count == 0
        scan.stop()

    def test_rem_reclassified_by_co2_evidence(self) -> None:
        """A REM device sending 1298 (CO2 evidence) IS re-classified to CO2.

        (I, 1298) is a VC pair match for CO2 -- this IS evidence-based.
        After 3+ such packets, the device should be re-classified.
        """
        gwy = make_mock_gateway(known_list={"37:222222": {"class": "REM"}})
        scan = DiscoveryScan(gwy)
        # Send 4x 1298 packets (evidence-based, contradicts REM)
        # First packet creates the device, subsequent packets increment
        # contradiction_count.  After 3 contradictions (4th packet),
        # likely_type is updated.
        for _ in range(4):
            scan._process_packet(
                make_dto(src="37:222222", code=Code._1298, verb=Verb.I_)
            )
        dev = scan.get_device("37:222222")
        assert dev is not None
        assert dev.likely_type == DevType.CO2  # re-classified
        assert dev.confidence == "high"
        scan.stop()

    def test_fan_reclassified_by_rem_evidence(self) -> None:
        """A FAN device sending 22F1 (REM evidence) IS re-classified to REM.

        (I, 22F1) is a VC pair match for REM -- this IS evidence-based.
        After 3+ such packets, the device should be re-classified.
        """
        gwy = make_mock_gateway(known_list={"37:333333": {"class": "FAN"}})
        scan = DiscoveryScan(gwy)
        for _ in range(4):
            scan._process_packet(
                make_dto(src="37:333333", code=Code._22F1, verb=Verb.I_)
            )
        dev = scan.get_device("37:333333")
        assert dev is not None
        assert dev.likely_type == DevType.REM  # re-classified
        assert dev.confidence == "high"
        scan.stop()
