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
    _decode_000c_items,
    _extract_domain_id_from_000c,
    _extract_zone_index_from_payload,
    _initial_confidence,
    _is_appliance_control_signal,
    _is_evidence_based,
    _is_valid_address,
    _recompute_confidence,
    _should_update_domain_id,
    dev_type_from_prefix,
)
from ramses_rf.payloads.heating import HeatDemand2BPayload
from ramses_tx.const import SZ_ACTIVE_HGI
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
    is_echo: bool = False,
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
        is_echo=is_echo,
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

    def test_decoded_3150_domain_or_zone_index(self) -> None:
        # HeatDemand2B (3150) carries the index as domain_or_zone_index,
        # not zone_index — a decoded payload object must still yield the
        # zone index (ramses-rf/ramses_cc#1345)
        payload = HeatDemand2BPayload.from_bytes(bytes.fromhex("02C8"))
        assert _extract_zone_index_from_payload(payload) == "02"

    def test_decoded_3150_domain_rejected(self) -> None:
        # A domain index (FC appliance_control) is not a zone index
        payload = HeatDemand2BPayload.from_bytes(bytes.fromhex("FCC8"))
        assert _extract_zone_index_from_payload(payload) is None


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


class TestDecode000CItems:
    """Tests for _decode_000c_items (zone-device binding items)."""

    @staticmethod
    def _as_tuples(items: list) -> list[tuple]:
        return [
            (item.zone_index, item.device_role_id, item.device_id_str)
            for item in items
        ]

    def test_5_byte_item(self) -> None:
        # zone_idx=01, role=04 (zone_sensor), no sub-idx, device=8B56E9
        items = _decode_000c_items("0104008B56E9")
        assert len(items) == 1
        assert items[0].zone_index == 1
        assert items[0].device_role_id == 0x04
        assert items[0].device_id_str == "8B56E9"

    def test_6_byte_item(self) -> None:
        # 6B form: index + role + sub-idx + device id
        items = _decode_000c_items("01080012E296")
        assert len(items) == 1
        assert items[0].zone_index == 1
        assert items[0].device_role_id == 0x08
        assert items[0].device_id_str == "12E296"

    def test_multi_item_6_byte(self) -> None:
        items = _decode_000c_items("01080012E2960204008B56E9")
        assert self._as_tuples(items) == [
            (1, 0x08, "12E296"),
            (2, 0x04, "8B56E9"),
        ]

    def test_domain_role_has_no_zone_index(self) -> None:
        # role 0F (APP relay) → domain-level binding, no zone_index
        items = _decode_000c_items("000F003545C8")
        assert len(items) == 1
        assert items[0].zone_index is None
        assert items[0].device_role_id == 0x0F

    def test_invalid_payload_returns_empty(self) -> None:
        assert _decode_000c_items("") == []
        assert _decode_000c_items("01") == []
        assert _decode_000c_items("ZZZZ") == []


class TestDiscoveryScan000CZoneBinding:
    """Zone-sensor attribution from CTL 000C payload items (issue 1344)."""

    def test_ctl_000c_role04_attributes_payload_device(self) -> None:
        """A CTL 000C role-04 item binds the payload device to the zone."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        ctl_id = "01:147133"

        scan._process_packet(
            make_dto(
                src=ctl_id,
                dst="18:001234",
                code=Code._000C,
                verb=Verb.RP,
                payload="0104008B56E9",
            )
        )
        dev = scan.get_device("34:218857")  # 0x8B56E9
        assert dev is not None
        assert dev.zone_index == "01"
        assert dev.bound_to == ctl_id

    def test_ctl_000c_6b_role04_attributes_payload_device(self) -> None:
        """The 6B form (with sub-idx) also attributes the payload device."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        ctl_id = "01:147133"

        # zone_idx=03, role=04, sub-idx=00, device 3029F4 → 50:010996? verify:
        # use a real 34:-ranged hex id: 8B56E9 → 34:218857
        scan._process_packet(
            make_dto(
                src=ctl_id,
                dst="18:001234",
                code=Code._000C,
                verb=Verb.RP,
                payload="0304008B56E9",
            )
        )
        dev = scan.get_device("34:218857")
        assert dev is not None
        assert dev.zone_index == "03"
        assert dev.bound_to == ctl_id

    def test_non_ctl_000c_does_not_attribute_payload_device(self) -> None:
        """A non-CTL source's 000C payload item must not bind the device."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)

        scan._process_packet(
            make_dto(
                src="30:000001",  # UFC — not a controller
                dst="18:001234",
                code=Code._000C,
                verb=Verb.RP,
                payload="0104008B56E9",
            )
        )
        dev = scan.get_device("34:218857")
        # device may be tracked via other paths, but never zone-attributed
        if dev is not None:
            assert dev.zone_index != "01"

    def test_000c_enriches_existing_orphan(self) -> None:
        """A payload-listed device seen earlier (orphan) is enriched."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        ctl_id = "01:147133"

        # THM broadcasts first — tracked without zone/binding
        scan._process_packet(
            make_dto(
                src="34:218857",
                dst="34:218857",
                code=Code._30C9,
                verb=Verb.I_,
                payload="0007BC",
            )
        )
        orphan = scan.get_device("34:218857")
        assert orphan is not None
        assert orphan.zone_index is None

        scan._process_packet(
            make_dto(
                src=ctl_id,
                dst="18:001234",
                code=Code._000C,
                verb=Verb.RP,
                payload="0104008B56E9",
            )
        )
        dev = scan.get_device("34:218857")
        assert dev is not None
        assert dev.zone_index == "01"
        assert dev.bound_to == ctl_id

    def test_000c_new_device_invokes_callback(self) -> None:
        """A never-seen payload device surfaces via the device callback."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        ctl_id = "01:147133"

        callback = MagicMock()
        scan.set_new_device_callback(callback)

        scan._process_packet(
            make_dto(
                src=ctl_id,
                dst="18:001234",
                code=Code._000C,
                verb=Verb.RP,
                payload="0104008B56E9",
            )
        )
        new_ids = [call.args[0].device_id for call in callback.call_args_list]
        assert "34:218857" in new_ids
        assert scan.get_device("34:218857") is not None

    def test_000c_domain_role_does_not_zone_attribute(self) -> None:
        """Domain roles (0F etc.) must not set zone_index on payload dev."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        ctl_id = "01:147133"

        scan._process_packet(
            make_dto(
                src=ctl_id,
                dst="18:001234",
                code=Code._000C,
                verb=Verb.RP,
                payload="000F003545C8",  # APP relay binding → domain FC
            )
        )
        dev = scan.get_device("54:016712")  # 0x3545C8 — if tracked at all
        if dev is not None:
            assert dev.zone_index is None


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


class TestClassifyDis:
    """Tests for DIS (display) classification via 'VMI only' RQ codes.

    A display polls the FAN for parameters/status — RQ 2411, RQ 31DA,
    RQ 10D0 — while a FAN answers (RP) and a bound REM does not
    routinely send these requests.  For ambiguous prefixes (29:, 37:)
    a source RQ on a 'VMI only' code is the DIS signature, unless the
    device has already sent W/RP (a writer/responder, not a display).
    """

    def test_29_rq_2411_is_dis(self) -> None:
        """29: sending RQ 2411 is a DIS, not a FAN — FANs don't request."""
        result = _classify("29:176861", Code._2411, Verb.RQ, is_source=True)
        assert result == DevType.DIS

    def test_29_rq_31da_is_dis(self) -> None:
        """29: sending RQ 31DA is a DIS polling the FAN for status."""
        result = _classify("29:176861", Code._31DA, Verb.RQ, is_source=True)
        assert result == DevType.DIS

    def test_29_rq_10d0_is_dis(self) -> None:
        """29: sending RQ 10D0 is a DIS polling the FAN for filter info."""
        result = _classify("29:176861", Code._10D0, Verb.RQ, is_source=True)
        assert result == DevType.DIS

    def test_37_rq_2411_is_dis(self) -> None:
        """37: sending RQ 2411 is a DIS — stronger than the REM fallback."""
        result = _classify("37:169161", Code._2411, Verb.RQ, is_source=True)
        assert result == DevType.DIS

    def test_32_rq_2411_is_still_fan(self) -> None:
        """32: is unambiguous — the prefix wins over the DIS signature."""
        result = _classify("32:153289", Code._2411, Verb.RQ, is_source=True)
        assert result == DevType.FAN

    def test_04_rq_2411_is_not_dis(self) -> None:
        """A non-HVAC prefix cannot be a DIS — falls back to TRV."""
        result = _classify("04:056053", Code._2411, Verb.RQ, is_source=True)
        assert result == DevType.TRV

    def test_29_rq_2411_as_dst_is_not_dis(self) -> None:
        """The DIS signature only applies to the sender, not the receiver."""
        result = _classify("29:176861", Code._2411, Verb.RQ, is_source=False)
        assert result != DevType.DIS

    def test_29_rq_2411_with_rp_history_not_dis(self) -> None:
        """A device that has sent RP is a responder, not a pure display."""
        dev = DiscoveredDevice(
            device_id="29:176861",
            first_seen="2026-07-01T10:00:00",
            last_seen="2026-07-01T10:00:00",
            likely_type="FAN",
            codes_seen=[Code._2411, Code._31DA],
            verb_codes_seen=["RP:31DA"],
        )
        result = _classify(
            "29:176861", Code._2411, Verb.RQ, is_source=True, device=dev
        )
        assert result != DevType.DIS

    def test_37_rq_31da_with_rp_10e0_history_is_dis(self) -> None:
        """An RP 10E0 (device-info answer) does not disqualify DIS.

        Every addressable device answers the gateway's RQ 10E0
        enumeration poll — including a display.  A 37: that polls the
        FAN (RQ 1470/31DA) and merely answers housekeeping stays DIS;
        only an RP servicing an HVAC-domain request (e.g. RP 31DA)
        marks it an endpoint.
        """
        dev = DiscoveredDevice(
            device_id="37:169161",
            first_seen="2026-07-01T10:00:00",
            last_seen="2026-07-01T10:00:00",
            likely_type="REM",
            codes_seen=[Code._10E0, Code._1470, Code._31DA],
            verb_codes_seen=["RP:10E0", "RQ:1470", " W:2411"],
        )
        result = _classify(
            "37:169161", Code._31DA, Verb.RQ, is_source=True, device=dev
        )
        assert result == DevType.DIS

    def test_29_rq_2411_with_w_history_not_dis(self) -> None:
        """A device that has sent W is a writer, not a pure display."""
        dev = DiscoveredDevice(
            device_id="29:176861",
            first_seen="2026-07-01T10:00:00",
            last_seen="2026-07-01T10:00:00",
            likely_type="REM",
            codes_seen=[Code._22F1, Code._2411],
            verb_codes_seen=[" W:22F1"],
        )
        result = _classify(
            "29:176861", Code._2411, Verb.RQ, is_source=True, device=dev
        )
        assert result != DevType.DIS

    def test_29_rq_2411_with_vmi_w_history_is_dis(self) -> None:
        """A W on a VMI write code (2411) does NOT disqualify DIS.

        The REM klass table attributes W 2411 'from a VMI' — a display
        user editing a parameter.  Only W on non-VMI codes (e.g.
        W 22F1, a REM writing fan mode) disqualifies the signature.
        """
        dev = DiscoveredDevice(
            device_id="29:176861",
            first_seen="2026-07-01T10:00:00",
            last_seen="2026-07-01T10:00:00",
            likely_type="DIS",
            codes_seen=[Code._2411],
            verb_codes_seen=[" W:2411", "RQ:2411"],
        )
        result = _classify(
            "29:176861", Code._2411, Verb.RQ, is_source=True, device=dev
        )
        assert result == DevType.DIS

    def test_29_rq_2411_with_rq_i_history_is_dis(self) -> None:
        """RQ/I-only history is the pure-display signature — still DIS."""
        dev = DiscoveredDevice(
            device_id="29:176861",
            first_seen="2026-07-01T10:00:00",
            last_seen="2026-07-01T10:00:00",
            likely_type="FAN",
            codes_seen=[Code._22F1, Code._2411, Code._31DA],
            verb_codes_seen=["RQ:2411", " I:22F1"],
        )
        result = _classify(
            "29:176861", Code._2411, Verb.RQ, is_source=True, device=dev
        )
        assert result == DevType.DIS

    def test_29_i_22f1_still_rem(self) -> None:
        """The DIS signature must not break the REM VC pair."""
        result = _classify("29:176861", Code._22F1, Verb.I_, is_source=True)
        assert result == DevType.REM

    def test_29_i_31da_still_fan(self) -> None:
        """The DIS signature must not break the FAN VC pair."""
        result = _classify("29:176861", Code._31DA, Verb.I_, is_source=True)
        assert result == DevType.FAN

    def test_rq_2411_is_evidence_based(self) -> None:
        """A source RQ on a 'VMI only' code counts as evidence."""
        assert _is_evidence_based("29:176861", Code._2411, Verb.RQ, True)

    def test_rq_10d0_is_evidence_based(self) -> None:
        """10D0 is not in _HVAC_DOMAIN_CODES — covered by the VMI rule."""
        assert _is_evidence_based("29:176861", Code._10D0, Verb.RQ, True)

    def test_rq_2411_as_dst_not_evidence(self) -> None:
        """A packet the device only received is not evidence."""
        assert not _is_evidence_based("29:176861", Code._2411, Verb.RQ, False)


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

    def test_stub_uses_prefix_derived_type(self) -> None:
        """stub() fills likely_type from the device ID prefix."""
        dev = DiscoveredDevice.stub("18:000730")
        assert dev.device_id == "18:000730"
        assert dev.likely_type == str(DevType.HGI)
        assert dev.confidence == "low"

    def test_stub_explicit_likely_type(self) -> None:
        """stub() honours an explicit likely_type override."""
        dev = DiscoveredDevice.stub(
            "04:056053", likely_type="TRV", confidence="high"
        )
        assert dev.likely_type == "TRV"
        assert dev.confidence == "high"

    def test_stub_unknown_prefix(self) -> None:
        """stub() falls back to "unknown" for unmapped prefixes."""
        dev = DiscoveredDevice.stub("99:123456")
        assert dev.likely_type == "unknown"


class TestDevTypeFromPrefix:
    """Tests for dev_type_from_prefix."""

    def test_unambiguous_prefixes(self) -> None:
        assert dev_type_from_prefix("18:000730") == DevType.HGI
        assert dev_type_from_prefix("01:145038") == DevType.CTL
        assert dev_type_from_prefix("32:123456") == DevType.FAN

    def test_unknown_prefix_returns_none(self) -> None:
        assert dev_type_from_prefix("99:123456") is None


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

    def test_pool_member_hgi_is_known(self) -> None:
        """Every pool member HGI counts as our own gateway, not just the
        currently active child (multi-HGI pool, issue 1257 item 4.1).

        Without the pool check, a secondary pool HGI would be flagged as
        a discovered device whenever it is not the active transport.
        """
        gwy = make_mock_gateway()
        gwy._engine._transport.get_extra_info = MagicMock(
            side_effect=lambda key, default=None: {
                SZ_ACTIVE_HGI: "18:001234",
                "pool_hgi_ids": ["18:001234", "18:149488"],
            }.get(key, default)
        )
        scan = DiscoveryScan(gwy)
        assert scan._is_known("18:001234") is True  # active pool child
        assert scan._is_known("18:149488") is True  # secondary pool member
        assert scan._is_known("18:999999") is False  # foreign HGI

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

    def test_zone_binding_extracted_from_addr3_on_i_frame(self) -> None:
        """Directed I frames place the peer CTL in addr3 with addr2=--:------.

        Regression for ramses-rf/ramses_cc#1353: real TRV I packets
        (e.g. ``I --- 04:034039 --:------ 01:256581 12B0 003 060000``)
        carry the controller in addr3; bound_to must be populated for
        both new and known devices so neighbour TRVs stay attributed to
        their own controller.
        """
        gwy = make_mock_gateway(known_list={"04:086351": {"class": "TRV"}})
        scan = DiscoveryScan(gwy)
        # Unknown neighbour TRV addressing neighbour CTL in addr3
        scan._process_packet(
            make_dto(
                src="04:034039",
                dst="--:------",
                addr3="01:256581",
                code=Code._12B0,
                verb=Verb.I_,
                payload="060000",
            )
        )
        foreign_trv = scan.get_device("04:034039")
        assert foreign_trv is not None
        assert foreign_trv.zone_index == "06"
        assert foreign_trv.bound_to == "01:256581"

        # Known TRV enriched on second packet with CTL in addr3
        scan._process_packet(
            make_dto(
                src="04:086351",
                dst="--:------",
                addr3="04:086351",
                code=Code._30C9,
                verb=Verb.I_,
                payload="0007D9",
            )
        )
        scan._process_packet(
            make_dto(
                src="04:086351",
                dst="--:------",
                addr3="01:208973",
                code=Code._3150,
                verb=Verb.I_,
                payload="01C8",
            )
        )
        own_trv = scan.get_device("04:086351")
        assert own_trv is not None
        assert own_trv.zone_index == "01"
        assert own_trv.bound_to == "01:208973"

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

    def test_self_addressed_2309_sets_no_zone_index(self) -> None:
        """A self-addressed 2309 is not zone evidence (ramses_cc#1182).

        Standalone UFH thermostats (22:) broadcast their setpoint to
        themselves (addr3 == src) with an internal stat index of "00" —
        not a CTL zone index.  Stamping "00" made ramses_cc move the THM
        into zone 00's actuators on every reload.
        """
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="22:072769",
                dst="--:------",
                addr3="22:072769",
                code=Code._2309,
                payload="0001F4",
            )
        )
        dev = scan.get_device("22:072769")
        assert dev is not None
        assert dev.zone_index is None
        assert dev.bound_to is None

    def test_directed_2309_sets_zone_index(self) -> None:
        """A 2309 directed at a CTL still carries the bound zone index."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="04:088729",
                dst="--:------",
                addr3="01:068717",
                code=Code._2309,
                payload="0001F4",
            )
        )
        dev = scan.get_device("04:088729")
        assert dev is not None
        assert dev.zone_index == "00"
        assert dev.bound_to == "01:068717"

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

    def test_dst_unknown_prefix_not_registered(self) -> None:
        """A dst-only address with an unknown prefix is not registered.

        Regression: an unbound one-way remote keeps transmitting to its
        stale binding target (``I --- 29:176861 62:008256 --:------
        22F1``).  The ghost dst was registered as a DEV/low device and
        surfaced as a spurious new discovery.
        """
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="29:176861",
                dst="62:008256",
                code=Code._22F1,
                payload="000104",
            )
        )
        # src has a known prefix so it is still registered
        assert scan.get_device("29:176861") is not None
        assert scan.get_device("62:008256") is None

    def test_addr3_unknown_prefix_not_registered(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="01:145038",
                dst="18:006402",
                addr3="62:008256",
                code=Code._000C,
            )
        )
        assert scan.get_device("62:008256") is None

    def test_src_unknown_prefix_registered(self) -> None:
        """A real transmitter is registered even with an unknown prefix."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(src="62:008256", dst="--:------", code=Code._22F1)
        )
        dev = scan.get_device("62:008256")
        assert dev is not None
        assert dev.source_count == 1

    def test_dst_unknown_prefix_tracked_device_still_enriched(self) -> None:
        """A tracked unknown-prefix device still counts dst packets."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        # register 62:008256 as a real transmitter first
        scan._process_packet(
            make_dto(src="62:008256", dst="--:------", code=Code._22F1)
        )
        scan._process_packet(
            make_dto(
                src="29:176861",
                dst="62:008256",
                code=Code._22F1,
                payload="000104",
            )
        )
        dev = scan.get_device("62:008256")
        assert dev is not None
        assert dev.destination_count == 1

    def test_zone_bound_to_unknown_prefix_dst_not_set(self) -> None:
        """bound_to is not pointed at an unregistrable dst address."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="04:056053",
                dst="62:008256",
                code=Code._3150,
                payload="02C8",
            )
        )
        dev = scan.get_device("04:056053")
        assert dev is not None
        assert dev.zone_index == "02"
        assert dev.bound_to is None

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


class TestDiscoveryScanNewDeviceCallback:
    """Tests for set_new_device_callback (ramses-rf/ramses_cc issue
    1257 item 2.1 — real-time discovery notification)."""

    def test_fires_for_new_device(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        seen: list[DiscoveredDevice] = []
        scan.set_new_device_callback(seen.append)
        scan._process_packet(
            make_dto(src="04:056053", dst="--:------", code=Code._3150)
        )
        assert len(seen) == 1
        assert seen[0].device_id == "04:056053"

    def test_fires_once_per_device(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        seen: list[DiscoveredDevice] = []
        scan.set_new_device_callback(seen.append)
        scan._process_packet(
            make_dto(src="04:056053", dst="--:------", code=Code._3150)
        )
        scan._process_packet(
            make_dto(src="04:056053", dst="--:------", code=Code._1060)
        )
        assert [d.device_id for d in seen] == ["04:056053"]

    def test_fires_for_each_new_device(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        seen: list[DiscoveredDevice] = []
        scan.set_new_device_callback(seen.append)
        scan._process_packet(
            make_dto(src="04:056053", dst="01:145038", code=Code._3150)
        )
        assert sorted(d.device_id for d in seen) == [
            "01:145038",
            "04:056053",
        ]

    def test_not_fired_for_known_device(self) -> None:
        gwy = make_mock_gateway(known_list={"04:056053": {"class": "TRV"}})
        scan = DiscoveryScan(gwy)
        seen: list[DiscoveredDevice] = []
        scan.set_new_device_callback(seen.append)
        scan._process_packet(
            make_dto(src="04:056053", dst="--:------", code=Code._3150)
        )
        assert seen == []
        # device is still tracked in the scan (minimal entry)
        assert scan.get_device("04:056053") is not None

    def test_not_fired_for_known_hgi(self) -> None:
        gwy = make_mock_gateway(known_list={"18:130236": {"class": "HGI"}})
        scan = DiscoveryScan(gwy)
        seen: list[DiscoveredDevice] = []
        scan.set_new_device_callback(seen.append)
        scan._process_packet(
            make_dto(src="18:130236", dst="--:------", code=Code._22F1)
        )
        assert seen == []
        assert scan.get_device("18:130236") is not None

    def test_fires_for_unknown_hgi(self) -> None:
        # A foreign HGI is a genuine new discovery
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        seen: list[DiscoveredDevice] = []
        scan.set_new_device_callback(seen.append)
        scan._process_packet(
            make_dto(src="18:999999", dst="--:------", code=Code._22F1)
        )
        assert [d.device_id for d in seen] == ["18:999999"]

    def test_callback_exception_does_not_break_scan(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)

        def bad_callback(device: DiscoveredDevice) -> None:
            raise RuntimeError("consumer bug")

        scan.set_new_device_callback(bad_callback)
        scan._process_packet(
            make_dto(src="04:056053", dst="--:------", code=Code._3150)
        )
        assert scan.get_device("04:056053") is not None

    def test_clear_callback(self) -> None:
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        seen: list[DiscoveredDevice] = []
        scan.set_new_device_callback(seen.append)
        scan.set_new_device_callback(None)
        scan._process_packet(
            make_dto(src="04:056053", dst="--:------", code=Code._3150)
        )
        assert seen == []


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

    def test_import_drops_dst_only_unknown_prefix(self) -> None:
        """Persisted dst-only ghosts are dropped on import."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan.import_json(
            json.dumps(
                {
                    "version": 1,
                    "devices": [
                        {
                            "device_id": "62:008256",
                            "first_seen": "2026-10-16T09:57:27",
                            "last_seen": "2026-10-16T10:13:13",
                            "likely_type": "DEV",
                            "codes_seen": [Code._22F1],
                            "source_count": 0,
                            "destination_count": 30,
                        }
                    ],
                }
            )
        )
        assert scan.get_device("62:008256") is None

    def test_import_keeps_unknown_prefix_transmitter(self) -> None:
        """An unknown-prefix device that transmitted is retained."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan.import_json(
            json.dumps(
                {
                    "version": 1,
                    "devices": [
                        {
                            "device_id": "62:008256",
                            "first_seen": "2026-10-16T09:57:27",
                            "last_seen": "2026-10-16T10:13:13",
                            "likely_type": "DEV",
                            "codes_seen": [Code._22F1],
                            "source_count": 3,
                        }
                    ],
                }
            )
        )
        assert scan.get_device("62:008256") is not None

    def test_import_keeps_declared_unknown_prefix(self) -> None:
        """A user-declared device is retained even with an unknown prefix."""
        gwy = make_mock_gateway(known_list={"62:008256": {"class": "DEV"}})
        scan = DiscoveryScan(gwy)
        scan.import_json(
            json.dumps(
                {
                    "version": 1,
                    "devices": [
                        {
                            "device_id": "62:008256",
                            "first_seen": "2026-10-16T09:57:27",
                            "last_seen": "2026-10-16T10:13:13",
                            "likely_type": "DEV",
                            "source_count": 0,
                        }
                    ],
                }
            )
        )
        assert scan.get_device("62:008256") is not None

    def test_import_drops_invalid_address(self) -> None:
        """Sentinel/malformed addresses are dropped on import."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan.import_json(
            json.dumps(
                {
                    "version": 1,
                    "devices": [
                        {
                            "device_id": "63:262142",
                            "first_seen": "2026-10-16T09:57:27",
                            "last_seen": "2026-10-16T10:13:13",
                            "likely_type": "DEV",
                            "destination_count": 5,
                        }
                    ],
                }
            )
        )
        assert scan.get_device("63:262142") is None


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

            with (
                patch("ramses_tx.discovery.comports", rf.comports),
                patch("serialx.async_list_serial_ports", rf.async_comports),
            ):
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
            with (
                patch("ramses_tx.discovery.comports", rf.comports),
                patch("serialx.async_list_serial_ports", rf.async_comports),
            ):
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
            with (
                patch("ramses_tx.discovery.comports", rf.comports),
                patch("serialx.async_list_serial_ports", rf.async_comports),
            ):
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

            with (
                patch("ramses_tx.discovery.comports", rf.comports),
                patch("serialx.async_list_serial_ports", rf.async_comports),
            ):
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

    def test_faked_device_not_reclassified(self) -> None:
        """A faked device is never re-classified by packet evidence.

        37:168270 is declared as DIS with faked=True — the integration
        itself transmits packets with the device's address as the
        source (e.g. spoofed I 22F1 fan-mode commands).  Observed REM
        evidence cannot contradict the declared class.
        """
        gwy = make_mock_gateway(
            known_list={"37:168270": {"class": "DIS", "faked": True}}
        )
        scan = DiscoveryScan(gwy)
        # Send 4x 22F1 packets (REM evidence) — would re-classify a
        # non-faked DIS device to REM after the contradiction threshold
        for _ in range(4):
            scan._process_packet(
                make_dto(src="37:168270", code=Code._22F1, verb=Verb.I_)
            )
        dev = scan.get_device("37:168270")
        assert dev is not None
        assert dev.likely_type == DevType.DIS  # declared class wins
        assert dev.contradiction_count == 0
        scan.stop()

    def test_rem_evidence_not_a_contradiction_for_dis(self) -> None:
        """REM-class packets never re-classify a DIS device.

        A DIS (display) IS a remote plus display requests — I 22F1 /
        I 22F3 are compatible with DIS, not contradicting.  A DIS being
        flagged as REM would be a downgrade: the REM class is a subset
        of what a DIS does.
        """
        gwy = make_mock_gateway(known_list={"37:168270": {"class": "DIS"}})
        scan = DiscoveryScan(gwy)
        # 4x I 22F1 (REM VC-pair evidence) — enough to re-classify a
        # FAN to REM, but must not downgrade a DIS
        for _ in range(4):
            scan._process_packet(
                make_dto(src="37:168270", code=Code._22F1, verb=Verb.I_)
            )
        dev = scan.get_device("37:168270")
        assert dev is not None
        assert dev.likely_type == DevType.DIS  # no downgrade
        assert dev.contradiction_count == 0
        scan.stop()

    def test_rem_upgrade_to_dis_still_works(self) -> None:
        """A declared REM sending display requests IS re-classified to DIS.

        The subset rule only blocks the downgrade direction — a REM
        that sends routine VMI requests (RQ 2411) does something a REM
        cannot do, so upgrading to DIS is legitimate evidence.
        """
        gwy = make_mock_gateway(known_list={"37:222222": {"class": "REM"}})
        scan = DiscoveryScan(gwy)
        for _ in range(4):
            scan._process_packet(
                make_dto(src="37:222222", code=Code._2411, verb=Verb.RQ)
            )
        dev = scan.get_device("37:222222")
        assert dev is not None
        assert dev.likely_type == DevType.DIS  # upgraded
        scan.stop()

    def test_hybrid_co2_rem_reclassified_despite_rem_matches(self) -> None:
        """A hybrid CO2+REM device is re-classified to CO2 despite
        interleaved REM-matching packets.

        A CO2 sensor with integrated remote buttons sends both
        I 1298 (CO2 VC pair) and I 22F1 (REM VC pair).  The device
        was initially classified as REM via prefix fallback
        (confidence="medium").  The REM-matching packets (I 22F1)
        must NOT reset the contradiction count while the current
        classification is from a prefix fallback, otherwise the
        CO2 evidence can never accumulate to the threshold.

        After re-classification to CO2 (confidence="high"), the
        reset is re-enabled, which prevents flapping: the CO2
        matches keep resetting the REM contradiction count.
        """
        gwy = make_mock_gateway(known_list={"37:126776": {"class": "REM"}})
        scan = DiscoveryScan(gwy)
        # Interleave CO2 and REM packets 4 times.
        # Without the fix, each I 22F1 resets the count to 0,
        # so the count never reaches 3.
        for _ in range(4):
            scan._process_packet(
                make_dto(src="37:126776", code=Code._1298, verb=Verb.I_)
            )
            scan._process_packet(
                make_dto(src="37:126776", code=Code._22F1, verb=Verb.I_)
            )
        dev = scan.get_device("37:126776")
        assert dev is not None
        assert dev.likely_type == DevType.CO2  # re-classified
        assert dev.confidence == "high"
        scan.stop()

    def test_hybrid_co2_rem_no_flapping_after_promotion(self) -> None:
        """After promotion to CO2, interleaved REM packets do NOT
        cause flapping back to REM.

        Once re-classified to CO2 with confidence="high", the
        reset is re-enabled.  The I 1298 (CO2) matches reset the
        REM contradiction count before it can reach the threshold,
        so the device stays CO2.
        """
        gwy = make_mock_gateway(known_list={"37:126776": {"class": "REM"}})
        scan = DiscoveryScan(gwy)
        # Promote to CO2 first (4x 1298 without interleaving)
        for _ in range(4):
            scan._process_packet(
                make_dto(src="37:126776", code=Code._1298, verb=Verb.I_)
            )
        dev = scan.get_device("37:126776")
        assert dev is not None
        assert dev.likely_type == DevType.CO2
        assert dev.confidence == "high"

        # Now interleave REM and CO2 packets.
        # The CO2 matches should reset the REM contradiction count,
        # preventing re-classification back to REM.
        for _ in range(5):
            scan._process_packet(
                make_dto(src="37:126776", code=Code._22F1, verb=Verb.I_)
            )
            scan._process_packet(
                make_dto(src="37:126776", code=Code._1298, verb=Verb.I_)
            )
        dev = scan.get_device("37:126776")
        assert dev is not None
        assert dev.likely_type == DevType.CO2  # no flapping
        scan.stop()

    def test_declared_class_change_reseeds_scan_entry(self) -> None:
        """A "declared"-confidence entry re-tracks a changed declaration.

        32:150000 is declared FAN; its scan entry is seeded with
        likely_type=FAN, confidence="declared" (the seed may also be
        restored from persisted scan state after a reload).  The schema
        is then changed to _class=DIS.  The stale FAN seed must be
        re-seeded to DIS on the next source packet so FAN evidence can
        accumulate contradictions — otherwise matching packets never
        increment the count and the non-"high" confidence suppresses
        the class-mismatch flag, hiding a real schema-vs-truth
        mismatch (ramses_extras ha_sim recipe R24).
        """
        kl: dict[str, Any] = {"32:150000": {"class": "FAN"}}
        gwy = make_mock_gateway(known_list=kl)
        scan = DiscoveryScan(gwy)
        # First packet creates the entry seeded with the declared class.
        scan._process_packet(
            make_dto(src="32:150000", code=Code._31D9, verb=Verb.I_)
        )
        dev = scan.get_device("32:150000")
        assert dev is not None
        assert dev.likely_type == DevType.FAN
        assert dev.confidence == "declared"

        # Declaration changes FAN -> DIS (e.g. schema reload).
        kl["32:150000"]["class"] = "DIS"

        # FAN evidence (I 31D9 is a VC pair match for FAN): the first
        # packet re-seeds likely_type to DIS, the next 3 accumulate
        # contradictions and re-classify back to FAN with high
        # confidence.
        for _ in range(4):
            scan._process_packet(
                make_dto(src="32:150000", code=Code._31D9, verb=Verb.I_)
            )
        dev = scan.get_device("32:150000")
        assert dev is not None
        assert dev.likely_type == DevType.FAN  # re-classified by evidence
        assert dev.confidence == "high"  # now flaggable as a mismatch
        scan.stop()

    def test_declared_class_change_respects_dis_compatible_evidence(
        self,
    ) -> None:
        """DIS-compatible evidence does not re-classify a re-seeded DIS.

        After re-seeding to the new DIS declaration, REM-subset traffic
        (I 22F1) stays compatible and must not count as a contradiction.
        """
        kl: dict[str, Any] = {"37:150000": {"class": "REM"}}
        gwy = make_mock_gateway(known_list=kl)
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(src="37:150000", code=Code._22F1, verb=Verb.I_)
        )
        dev = scan.get_device("37:150000")
        assert dev is not None
        assert dev.likely_type == DevType.REM
        assert dev.confidence == "declared"

        kl["37:150000"]["class"] = "DIS"

        for _ in range(4):
            scan._process_packet(
                make_dto(src="37:150000", code=Code._22F1, verb=Verb.I_)
            )
        dev = scan.get_device("37:150000")
        assert dev is not None
        assert dev.likely_type == DevType.DIS  # re-seeded, not re-classified
        assert dev.contradiction_count == 0
        scan.stop()


class TestDisClassification:
    """Packet-level tests for DIS classification and echo filtering.

    A DIS (display, e.g. Orcon RF15/VMI) polls the FAN with RQ 2411,
    RQ 31DA, RQ 10D0 and sends occasional I packets — but never RP/W.
    Echo packets (our own transmissions heard back, e.g. service calls
    sent with a spoofed from_id) must not feed any of this.
    """

    def test_29_rq_traffic_discovers_dis(self) -> None:
        """A 29: device seen only sending RQ 2411 is classified DIS."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="29:176861",
                dst="32:153289",
                code=Code._2411,
                verb=Verb.RQ,
            )
        )
        dev = scan.get_device("29:176861")
        assert dev is not None
        assert dev.likely_type == DevType.DIS
        assert dev.verb_codes_seen == ["RQ:2411"]
        scan.stop()

    def test_known_rem_reclassified_to_dis_by_rq_traffic(self) -> None:
        """A known REM sending only RQ polls is re-classified as DIS.

        29:176861 declared REM in the schema; its real traffic is RQ
        2411/31DA/10D0 + occasional I 22F1 — never RP/W.  The RQ
        packets are evidence-based contradictions (the DIS signature),
        so after the threshold the device re-classifies to DIS.
        """
        gwy = make_mock_gateway(known_list={"29:176861": {"class": "REM"}})
        scan = DiscoveryScan(gwy)
        # First packet creates the entry with the declared class.
        # Three more RQ packets reach the contradiction threshold.
        for code in (Code._2411, Code._31DA, Code._10D0, Code._2411):
            scan._process_packet(
                make_dto(
                    src="29:176861",
                    dst="32:153289",
                    code=code,
                    verb=Verb.RQ,
                )
            )
        dev = scan.get_device("29:176861")
        assert dev is not None
        assert dev.likely_type == DevType.DIS  # re-classified
        assert dev.confidence == "high"
        assert "RQ:2411" in dev.verb_codes_seen
        scan.stop()

    def test_dis_classification_survives_i_22f1(self) -> None:
        """After promotion to DIS, an I 22F1 does not flap it to REM.

        A display also broadcasts I 22F1 (mode display, itho scheme),
        which maps to REM — but the RQ-dominant traffic keeps
        resetting the contradiction count.
        """
        gwy = make_mock_gateway(known_list={"29:176861": {"class": "REM"}})
        scan = DiscoveryScan(gwy)
        for _ in range(4):
            scan._process_packet(
                make_dto(
                    src="29:176861",
                    dst="32:153289",
                    code=Code._2411,
                    verb=Verb.RQ,
                )
            )
        dev = scan.get_device("29:176861")
        assert dev is not None
        assert dev.likely_type == DevType.DIS

        # Interleaved I 22F1 (REM VC pair) between RQ polls — no flap.
        for _ in range(3):
            scan._process_packet(
                make_dto(
                    src="29:176861",
                    dst="32:153289",
                    code=Code._22F1,
                    verb=Verb.I_,
                )
            )
            scan._process_packet(
                make_dto(
                    src="29:176861",
                    dst="32:153289",
                    code=Code._2411,
                    verb=Verb.RQ,
                )
            )
        dev = scan.get_device("29:176861")
        assert dev is not None
        assert dev.likely_type == DevType.DIS  # no flapping
        scan.stop()

    def test_echo_packet_is_ignored(self) -> None:
        """Echo packets carry no evidence — nothing is tracked."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="29:176861",
                dst="32:153289",
                code=Code._2411,
                verb=Verb.RQ,
                is_echo=True,
            )
        )
        assert scan.get_device("29:176861") is None
        assert scan.device_count() == 0
        scan.stop()

    def test_echo_packet_does_not_feed_existing_device(self) -> None:
        """An echo of a spoofed send must not inflate counters/verbs."""
        gwy = make_mock_gateway(known_list={"29:176861": {"class": "REM"}})
        scan = DiscoveryScan(gwy)
        # Real traffic: one I 22F1 broadcast.
        scan._process_packet(
            make_dto(
                src="29:176861",
                dst="--:------",
                code=Code._22F1,
                verb=Verb.I_,
            )
        )
        dev = scan.get_device("29:176861")
        assert dev is not None
        assert dev.source_count == 1
        assert dev.verb_codes_seen == [" I:22F1"]
        # Now our spoofed RQ 2411 echo arrives — must be ignored, so
        # it cannot count as DIS evidence against the declared REM.
        scan._process_packet(
            make_dto(
                src="29:176861",
                dst="32:153289",
                code=Code._2411,
                verb=Verb.RQ,
                is_echo=True,
            )
        )
        dev = scan.get_device("29:176861")
        assert dev is not None
        assert dev.source_count == 1  # unchanged
        assert dev.verb_codes_seen == [" I:22F1"]  # unchanged
        assert dev.contradiction_count == 0
        assert dev.likely_type == DevType.REM
        scan.stop()

    def test_verb_codes_seen_tracked_per_device(self) -> None:
        """verb_codes_seen accumulates deduplicated verb+code pairs."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        for verb, code in (
            (Verb.I_, Code._22F1),
            (Verb.RQ, Code._2411),
            (Verb.RQ, Code._31DA),
        ):
            scan._process_packet(
                make_dto(
                    src="29:176861",
                    dst="32:153289",
                    code=code,
                    verb=verb,
                )
            )
        dev = scan.get_device("29:176861")
        assert dev is not None
        assert sorted(set(dev.verb_codes_seen)) == dev.verb_codes_seen
        assert set(dev.verb_codes_seen) == {
            " I:22F1",
            "RQ:2411",
            "RQ:31DA",
        }
        scan.stop()

    def test_verb_codes_seen_round_trip(self) -> None:
        """verb_codes_seen survives JSON export/import."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="29:176861",
                dst="32:153289",
                code=Code._2411,
                verb=Verb.RQ,
            )
        )
        exported = scan.export_json()

        scan2 = DiscoveryScan(make_mock_gateway())
        scan2.import_json(exported)
        dev = scan2.get_device("29:176861")
        assert dev is not None
        assert dev.verb_codes_seen == ["RQ:2411"]
        assert dev.likely_type == DevType.DIS
        scan.stop()
        scan2.stop()

    def test_import_without_verb_codes_seen(self) -> None:
        """Older exports without verb_codes_seen still import cleanly."""
        data = json.dumps(
            {
                "version": 1,
                "devices": [
                    {
                        "device_id": "29:176861",
                        "first_seen": "2026-07-01T10:00:00",
                        "last_seen": "2026-07-01T10:01:00",
                        "likely_type": "REM",
                        "codes_seen": [Code._22F1, Code._2411],
                    }
                ],
            }
        )
        scan = DiscoveryScan(make_mock_gateway())
        scan.import_json(data)
        dev = scan.get_device("29:176861")
        assert dev is not None
        assert dev.verb_codes_seen == []
        scan.stop()


class TestFanCo2Reclassification:
    """32:-prefix FAN-vs-CO2 disambiguation (issue 1287).

    The 32: prefix defaults to FAN, but standalone CO2 sensors live at
    32: addresses too, and a FAN with an integrated CO2 sensor also
    broadcasts I 1298.  Direction-aware evidence distinguishes them: a
    pure sensor sends I 1298, sends no FAN signatures (I 31D9, I/RP
    31DA, RP 2411), and receives no directed fan-domain requests.
    """

    def test_known_fan32_reclassified_to_co2_by_quiet_1298(self) -> None:
        """A FAN-declared 32: device that only broadcasts I 1298 is
        re-classified to CO2 after the contradiction threshold."""
        gwy = make_mock_gateway(known_list={"32:090174": {"class": "FAN"}})
        scan = DiscoveryScan(gwy)
        for _ in range(4):
            scan._process_packet(
                make_dto(src="32:090174", code=Code._1298, verb=Verb.I_)
            )
        dev = scan.get_device("32:090174")
        assert dev is not None
        assert dev.likely_type == DevType.CO2  # re-classified
        assert dev.confidence == "high"
        scan.stop()

    def test_fan32_with_fan_signature_stays_fan(self) -> None:
        """A 32: device broadcasting both I 1298 and I 31DA is a FAN
        with an integrated CO2 sensor — the FAN signature must win."""
        gwy = make_mock_gateway(known_list={"32:150000": {"class": "FAN"}})
        scan = DiscoveryScan(gwy)
        for _ in range(4):
            # 31DA first so the FAN signature is recorded before the
            # CO2 broadcasts — the device must stay FAN throughout.
            scan._process_packet(
                make_dto(src="32:150000", code=Code._31DA, verb=Verb.I_)
            )
            scan._process_packet(
                make_dto(src="32:150000", code=Code._1298, verb=Verb.I_)
            )
        dev = scan.get_device("32:150000")
        assert dev is not None
        assert dev.likely_type == DevType.FAN
        assert dev.contradiction_count == 0
        scan.stop()

    def test_fan32_co2_first_then_fan_signature_stays_fan(self) -> None:
        """A FAN whose I 1298 arrives before its first 31DA still
        recovers: the late FAN signature restores the prefix class and
        the transient contradiction can never reach the threshold."""
        gwy = make_mock_gateway(known_list={"32:150000": {"class": "FAN"}})
        scan = DiscoveryScan(gwy)
        for _ in range(4):
            scan._process_packet(
                make_dto(src="32:150000", code=Code._1298, verb=Verb.I_)
            )
            scan._process_packet(
                make_dto(src="32:150000", code=Code._31DA, verb=Verb.I_)
            )
        dev = scan.get_device("32:150000")
        assert dev is not None
        assert dev.likely_type == DevType.FAN
        assert dev.contradiction_count < 3
        scan.stop()

    def test_fan32_receiving_fan_requests_stays_fan(self) -> None:
        """A 32: device that is the target of fan-domain requests is a
        real FAN — bound remotes poll it, a CO2 sensor is never polled."""
        gwy = make_mock_gateway(
            known_list={
                "32:150000": {"class": "FAN"},
                "37:169161": {"class": "REM"},
            }
        )
        scan = DiscoveryScan(gwy)
        # A bound REM requests fan parameters from the device
        scan._process_packet(
            make_dto(
                src="37:169161",
                dst="32:150000",
                code=Code._2411,
                verb=Verb.RQ,
            )
        )
        for _ in range(4):
            scan._process_packet(
                make_dto(src="32:150000", code=Code._1298, verb=Verb.I_)
            )
        dev = scan.get_device("32:150000")
        assert dev is not None
        assert dev.fan_requests_received == 1
        assert dev.likely_type == DevType.FAN
        scan.stop()

    def test_fan32_hgi_probe_not_counted_as_fan_request(self) -> None:
        """An HGI's RQ 2411 probe must not count as fan-request evidence —
        gateway-originated probing says nothing about how bound devices
        treat the target."""
        gwy = make_mock_gateway(known_list={"32:090174": {"class": "FAN"}})
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="18:000001",
                dst="32:090174",
                code=Code._2411,
                verb=Verb.RQ,
            )
        )
        for _ in range(4):
            scan._process_packet(
                make_dto(src="32:090174", code=Code._1298, verb=Verb.I_)
            )
        dev = scan.get_device("32:090174")
        assert dev is not None
        assert dev.fan_requests_received == 0
        assert dev.likely_type == DevType.CO2
        scan.stop()

    def test_unknown_32_pure_co2_self_corrects(self) -> None:
        """An unknown 32: device is created as FAN (prefix) but the
        enrich path re-classifies it to CO2 on the next I 1298 — no
        known_list declaration needed."""
        gwy = make_mock_gateway()
        scan = DiscoveryScan(gwy)
        for _ in range(2):
            scan._process_packet(
                make_dto(src="32:092927", code=Code._1298, verb=Verb.I_)
            )
        dev = scan.get_device("32:092927")
        assert dev is not None
        assert dev.likely_type == DevType.CO2
        scan.stop()

    def test_fan_requests_received_survives_export_import(self) -> None:
        """fan_requests_received round-trips through JSON export/import."""
        gwy = make_mock_gateway(known_list={"32:150000": {"class": "FAN"}})
        scan = DiscoveryScan(gwy)
        scan._process_packet(
            make_dto(
                src="37:169161",
                dst="32:150000",
                code=Code._2411,
                verb=Verb.RQ,
            )
        )
        exported = scan.export_json()

        scan2 = DiscoveryScan(make_mock_gateway())
        scan2.import_json(exported)
        dev = scan2.get_device("32:150000")
        assert dev is not None
        assert dev.fan_requests_received == 1
        scan.stop()
        scan2.stop()
