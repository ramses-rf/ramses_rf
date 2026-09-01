#!/usr/bin/env python3
"""RAMSES RF - Passive device scan engine.

A read-only observer that listens to RF traffic, classifies unknown devices
by prefix and verb/code pairs, and maintains an in-memory discovery list.

This module is the scan engine only — it does NOT:
  - create devices in the registry (no `get_device()` calls)
  - mutate topology (no `TopologyChangedEvent`s)
  - write to disk (everything in-memory, consumer calls `export_json()`)
  - depend on Home Assistant (plain Python, works in CLI)

The consumer (ramses_cc or the CLI) is responsible for persistence,
notifications, and user-facing accept/discard workflow.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime as dt
from typing import TYPE_CHECKING, Any

from ramses_rf.const import Code, DevType, Verb
from ramses_rf.protocol.ramses import HVAC_KLASS_BY_VC_PAIR
from ramses_tx.const import SZ_ACTIVE_HGI

if TYPE_CHECKING:
    from ramses_rf.gateway import Gateway
    from ramses_tx.dtos import PacketDTO


_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Classification maps
# ---------------------------------------------------------------------------

# Prefix → likely DevType (CH + HVAC domain).
# Not reused from DEV_TYPE_MAP because that maps prefix→human-readable string
# (e.g. "controller"), not prefix→DevType enum (e.g. CTL). Also, DEV_TYPE_MAP
# lacks HVAC prefixes (32:=FAN, 37:=REM). TopologyBuilder has a similar local
# dict but smaller (only eavesdrop-promotable types). Keeping our own is
# the simplest correct approach.
_PREFIX_TO_TYPE: dict[str, DevType] = {
    "00": DevType.TRV,  # radiator_valve (rare, same class as 04:)
    "01": DevType.CTL,
    "02": DevType.UFC,
    "03": DevType.THM,
    "04": DevType.TRV,
    "07": DevType.DHW,
    "08": DevType.JIM,
    "10": DevType.OTB,
    "12": DevType.THM,
    "13": DevType.BDR,
    "17": DevType.OUT,
    "18": DevType.HGI,
    "22": DevType.THM,
    "23": DevType.PRG,
    "30": DevType.RFG,
    "31": DevType.JST,
    "29": DevType.FAN,  # ambiguous: FAN/CO2/HUM/REM — VC pair resolves, FAN is default fallback
    "32": DevType.FAN,
    "34": DevType.RND,
    "37": DevType.REM,
}

# Verb+code → DevType (HVAC domain, from HVAC_KLASS_BY_VC_PAIR).
# Keys are (verb_value, code_value) tuples for fast lookup.
_VC_TO_TYPE: dict[tuple[str, str], DevType] = {
    (v.value, str(c)): dt for (v, c), dt in HVAC_KLASS_BY_VC_PAIR.items()
}

# Codes that only a CTL sends (from CODES_ONLY_FROM_CTL).
# If a device sends one of these, it's definitely a CTL.
# NOTE: 313F (datetime) is only CTL-only when sent as I/RP (broadcasting
# the time).  TRVs send 313F as RQ (requesting the time), so we track
# the verb separately.
_CTL_ONLY_CODES: frozenset[Code | str] = frozenset({Code._1030, Code._1F09})
_CTL_ONLY_CODES_WITH_VERB: dict[Code | str, frozenset[Verb | str]] = {
    Code._313F: frozenset(
        {Verb.I_, Verb.RP}
    ),  # I/RP = CTL broadcasts time; RQ = TRV asks
}

# Codes used in the HVAC ventilation domain (FAN/REM/DIS).
# These codes provide evidence about a device's type even when the
# specific (verb, code) pair is not in _VC_TO_TYPE.  For example,
# RQ 31DA is not in HVAC_KLASS_BY_VC_PAIR (only I/RP 31DA map to FAN),
# and 2411 is not in the table at all (both FANs and REMs/DISs use it).
# But a FAN-classified device sending RQ 31DA or RQ 2411 is evidence
# that it's NOT a FAN (FANs respond with RP, they don't request).
# When sent as src by a device classified as a different type, these
# codes count as evidence-based contradictions.
_HVAC_DOMAIN_CODES: frozenset[Code | str] = frozenset(
    {Code._31D9, Code._31DA, Code._22F1, Code._22F3, Code._2411}
)

# Codes that indicate battery-powered devices.
_BATTERY_CODES: frozenset[Code | str] = frozenset({Code._1060, Code._1FC9})

# Codes that carry zone_index in the payload (binding telemetry).
# Used to extract zone assignment from traffic.
# NOTE: 30C9 (Room Setpoint) is excluded - its payload is 00{setpoint}
# where the first byte is always 00 (a constant), not a zone index.
# 3150 (Actuator State) and 12B0 (Window Open) have the real zone_index.
# 000A (Zone Info) is sent by THMs (22:) with their zone_index as payload —
# e.g. RQ 000A 001 01 means the THM is asking about zone 01 (its zone).
# The CTL and HGI also send 000A, but the HGI is excluded by is_hgi and
# the CTL's zone_index is unused (it gets main_tcs, not a zone placement).
_ZONE_BINDING_CODES: frozenset[Code | str] = frozenset(
    {
        Code._3150,
        Code._000C,
        Code._2309,
        Code._2349,
        Code._10A0,
        Code._1260,
        Code._12B0,
        Code._1F09,
        Code._000A,
    }
)

# HVAC codes sent by REMs/CO2s to their parent FAN (32:).  These are
# NOT binding protocol codes (binding uses 1FC9, done once with FAN off).
# They are operational commands.  A REM sending 22F1 to a FAN doesn't
# prove binding — the REM could be a neighbour's remote broadcasting.
# But a FAN sending a directed packet (I or RP) to a specific 37: device
# IS strong evidence of binding — the FAN is the controller and it's
# communicating with its paired remote.  See schema_architecture.md:
# "How HVAC topology COULD be derived from traffic".
_HVAC_PARENT_INFERENCE_CODES: frozenset[Code | str] = frozenset(
    {
        Code._22F1,  # fan_mode
        Code._31E0,  # vent_demand
        Code._31DA,  # fan_status
        Code._10D0,  # outside_temp
        Code._2411,  # fan_params — FAN RP to REM's RQ, most common directed exchange
    }
)

# Contradiction threshold: after this many consecutive source packets
# where _classify disagrees with likely_type (without a matching packet
# resetting the count), likely_type is updated.  Mirrors the HvacTopologyHandler's
# threshold (3 non-FAN packets).  Generic — works for any device class.
_CONTRADICTION_THRESHOLD: int = 3


def _is_evidence_based(
    device_id: str, code: str, verb: str, is_source: bool
) -> bool:
    """Check if a (verb, code) pair provides evidence-based classification.

    Returns True if the pair maps to a specific DevType via _VC_TO_TYPE
    (a verb+code signature match), if the code is CTL-only, or if the
    code is in the HVAC ventilation domain (_HVAC_DOMAIN_CODES).  These
    are evidence-based classifications that can legitimately contradict
    a declared class.

    Returns False for prefix fallbacks (e.g. 37: → REM is a guess based
    on the device ID prefix, not on what the packet actually says).  A
    prefix fallback should NOT count as a contradiction — otherwise a
    CO2 device sending generic codes like 10E0 would be re-classified as
    REM just because 37: falls to REM by prefix.

    The _HVAC_DOMAIN_CODES check catches codes like RQ 31DA and RQ 2411
    that are not in _VC_TO_TYPE (only I/RP 31DA map to FAN, and 2411 is
    not in the table at all) but are still strong evidence that a
    FAN-classified device is actually a REM/DIS (a FAN responds with RP,
    it doesn't send RQ).
    """
    vc_key = (verb, code)
    if vc_key in _VC_TO_TYPE:
        return True
    if is_source and str(code) in {str(c) for c in _HVAC_DOMAIN_CODES}:
        return True
    return bool(
        is_source
        and (
            code in _CTL_ONLY_CODES
            or (
                code in _CTL_ONLY_CODES_WITH_VERB
                and verb in _CTL_ONLY_CODES_WITH_VERB[code]
            )
        )
    )


# TPI loop codes broadcast by a BDR (13:) or OTB (10:) acting as the
# appliance_control (boiler relay).  These are sent as I broadcasts at
# the TPI cycle rate (usu. 6x/hr).  This is the same signature
# ramses_rf's eavesdrop_appliance_control (tcs.py) watches for to
# identify the system relay.
#
# IMPORTANT (issue 834 comment 5044906835): a DHW valve relay
# (hotwater_valve) ALSO broadcasts 3B00/3EF0 -- both relays participate
# in TPI loops.  Therefore these codes alone are NOT sufficient to
# classify a device as appliance_control.  The 000C binding table is
# the authoritative source:
#   - 000C with role 0F (APP) -> domain FC (appliance_control)
#   - 000C with role 0E (HTG) -> domain FA (hotwater_valve)
# The 3B00/3EF0 heuristic is used only as a fallback hint when no 000C
# binding has been seen yet, and is overridden when a 000C binding
# arrives.
_APPLIANCE_CONTROL_CODES: frozenset[Code | str] = frozenset(
    {Code._3B00, Code._3EF0}
)

# 000C payload roles that map to domain IDs (not zone indices).
# See ramses_tx/const.py DEV_ROLE_MAP and parsers/heating.py
# complex_index.
#   payload[2:4] == "0E" (HTG) -> FA (hotwater_valve) if index == "00",
#                                 F9 if "01"
#   payload[2:4] == "0F" (APP) -> FC (appliance_control)
#   payload[2:4] == "0D" (DHW) -> FA (dhw_sensor) if index == "00",
#                                 F9 if "01"
_000C_ROLE_HTG = "0E"  # hotwater_valve / heating_valve
_000C_ROLE_APP = "0F"  # appliance_control
_000C_ROLE_DHW = "0D"  # dhw_sensor
_000C_DOMAIN_ROLES: frozenset[str] = frozenset(
    {_000C_ROLE_HTG, _000C_ROLE_APP, _000C_ROLE_DHW}
)

# 32: is unambiguous — always a FAN. A FAN sends 22F1 (which maps to REM in
# HVAC_KLASS_BY_VC_PAIR) but is still a FAN, so the prefix must win.
# 18: is unambiguous — always an HGI. The HGI relays packets from all device
# types (22F1, 31DA, etc.) but is still a gateway, not a REM or FAN.
# 37: is ambiguous (REM, CO2, HUM, or DIS all use 37:) — needs the VC pair to
# distinguish, so it's NOT in this set.
_UNAMBIGUOUS_HVAC_PREFIXES: frozenset[str] = frozenset({"18", "32"})

# Valid HVAC types per prefix — constrains VC pair matching so a 37: device
# sending 31D9 I (which maps to FAN) is only classified as FAN if FAN is in
# the allowed set for that prefix.  29: and 37: are both ambiguous: they can
# be FAN, REM, CO2, HUM, or DIS depending on the VC pair.  32: is unambiguous
# (always FAN) and handled in _UNAMBIGUOUS_HVAC_PREFIXES above.
_AMBIGUOUS_HVAC_PREFIX_TYPES: dict[str, frozenset[DevType]] = {
    "29": frozenset(
        {DevType.FAN, DevType.CO2, DevType.HUM, DevType.REM, DevType.DIS}
    ),
    "37": frozenset(
        {DevType.FAN, DevType.REM, DevType.CO2, DevType.HUM, DevType.DIS}
    ),
}


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class DiscoveredDevice:
    """A device seen on RF, classified but not yet created in the registry.

    This dataclass is the scan engine's view of a device. The consumer
    (ramses_cc) extends it with status/enabled/owner/faked fields stored
    in HA's .storage/.
    """

    device_id: str
    first_seen: str  # ISO timestamp
    last_seen: str  # ISO timestamp
    likely_type: str  # DevType value (e.g. "CTL", "TRV")
    codes_seen: list[str] = field(default_factory=list)  # sorted, deduplicated
    bound_to: str | None = None  # parent device ID (CTL for TRV, FAN for REM)
    zone_index: str | None = None  # zone index if known from payload
    domain_id: str | None = None  # domain ID if known (FC=appliance_control)
    # True if domain_id was set from an authoritative 000C binding table
    # entry; False if from a 3B00/3EF0 fallback hint.  Consumers (ramses_cc)
    # use this to distinguish confident classification from hedged hints.
    # Issue 931.
    is_authoritative_domain: bool = False
    rssi: float | None = None  # running average
    confidence: str = "low"  # high, medium, low
    is_battery: bool = False  # seen sending battery info
    source_count: int = 0  # number of packets where this device was src
    destination_count: int = 0  # number of packets where this device was dst
    # Contradiction tracking (issue 1000): for known devices, count
    # source packets where _classify disagrees with the current
    # likely_type.  After _CONTRADICTION_THRESHOLD consecutive
    # disagreements (without a matching packet resetting the count),
    # likely_type is updated to the new classification.  This uses the
    # existing _classify function — no HVAC-specific logic here.  When
    # the strategy pattern arrives, _classify becomes
    # strategy.classify(packet) and this tracking moves with it.
    contradiction_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (for JSON export)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiscoveredDevice:
        """Deserialize from a plain dict (for JSON import/resume)."""
        return cls(
            **{k: data[k] for k in data if k in cls.__dataclass_fields__}
        )


# ---------------------------------------------------------------------------
# Scan engine
# ---------------------------------------------------------------------------


class DiscoveryScan:
    """Passive device scanner. Read-only, no topology mutation.

    Register as a msg_handler on the gateway. Every packet is examined:
    - src, dst, addr3 device IDs are extracted
    - Unknown devices are classified and added to the in-memory dict
    - Known devices are enriched with new codes/binding info

    The scan never calls ``get_device()`` or emits topology events.
    """

    def __init__(self, gateway: Gateway) -> None:
        """Initialise the passive discovery scanner.

        :param gateway: The active gateway instance.
        :type gateway: Gateway
        """
        self._gateway = gateway
        self._devices: dict[str, DiscoveredDevice] = {}
        self._dirty: bool = False
        self._remove_handler: Callable[[], None] | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Register as a raw packet handler on the gateway.

        Uses ``add_raw_packet_handler`` (not ``add_msg_handler``) so the scan
        sees packets from unknown devices even when ``enforce_known_list=True``.
        The raw handler fires before the device ID filter.
        """
        if self._remove_handler is not None:
            _LOGGER.warning("DiscoveryScan.start(): already running")
            return
        self._remove_handler = self._gateway.add_raw_packet_handler(
            self._on_packet
        )
        _LOGGER.info("DiscoveryScan: started (passive observer)")

    def stop(self) -> None:
        """Unregister from gateway."""
        if self._remove_handler:
            self._remove_handler()
            self._remove_handler = None
            _LOGGER.info("DiscoveryScan: stopped")

    @property
    def is_running(self) -> bool:
        """Whether the scan is currently listening to traffic."""
        return self._remove_handler is not None

    @property
    def is_dirty(self) -> bool:
        """Whether the in-memory state has changed since last export/import."""
        return self._dirty

    def clear_dirty(self) -> None:
        """Reset the dirty flag (call after successful persistence)."""
        self._dirty = False

    # -- known device check --------------------------------------------------

    def _is_known(self, device_id: str) -> bool:
        """Check if a device is already known to the gateway.

        A device is "known" if it's the gateway itself, in the known_list,
        or in the schema.  The device_registry is **not** consulted here:
        under the Schema-as-Source-of-Truth architecture (ramses_cc issue
        767, Invariant 1), the schema + derived known_list represent
        declared intent, while the device_registry is derived state that
        is populated from the schema at gateway creation and mutated at
        runtime.  When they disagree, intent wins — a device removed from
        the schema must be re-discoverable even if the running gateway's
        registry still holds a stale entry.
        """
        # The gateway's own HGI is never a "discovered" device.
        # Check the active HGI ID from the transport directly — the device
        # may not be in the device_registry yet when the first packets arrive.
        # For PooledTransport, also check all pool member HGI IDs so that
        # already-accepted pool members are not re-discovered.
        engine = getattr(self._gateway, "_engine", None)
        transport = getattr(engine, "_transport", None) if engine else None
        if transport is not None:
            active_hgi = transport.get_extra_info(SZ_ACTIVE_HGI)
            if active_hgi == device_id:
                return True
            # Check all pool members (PooledTransport returns a list;
            # regular transports return None for this key).
            pool_hgi_ids = transport.get_extra_info("pool_hgi_ids")
            if pool_hgi_ids and device_id in pool_hgi_ids:
                return True
        # Also check via the hgi property (covers the case where the device
        # is in the registry but the transport extra_info is not set)
        if self._gateway.hgi and self._gateway.hgi.id == device_id:
            return True

        # Check known_list (declared intent, derived from schema)
        if device_id in self._gateway._gwy_config.known_list:
            return True

        # Check schema keys (CTL IDs are top-level keys — declared intent)
        return device_id in self._gateway._gwy_config.schema

    def _get_declared_class(self, device_id: str) -> DevType | None:
        """Get the declared class for a known device from the known_list.

        :param device_id: The device ID to look up.
        :return: The declared DevType, or None if the device is not in
            the known_list or has no class.
        """
        entry = self._gateway._gwy_config.known_list.get(device_id)
        if not isinstance(entry, dict):
            return None
        cls = entry.get("class")
        if not isinstance(cls, str) or not cls:
            return None
        try:
            return DevType(cls)
        except ValueError:
            return None

    def _is_declared_hotwater_valve(self, device_id: str) -> bool:
        """Check if a device is declared as hotwater_valve in the schema.

        Under the Schema-as-Source-of-Truth architecture (issue 767),
        the schema is the authoritative declaration of system topology.
        If a BDR (13:) is declared as ``stored_hotwater.hotwater_valve``
        in any CTL's schema entry, it is the DHW valve relay (FA domain),
        NOT the appliance_control (FC domain).

        This prevents the 3B00/3EF0 TPI broadcast heuristic from
        misclassifying a hotwater_valve BDR as appliance_control when
        both relays broadcast the same codes (issue 834 comment
        5044906835).
        """
        schema = self._gateway._gwy_config.schema
        for entry in schema.values():
            if not isinstance(entry, dict):
                continue
            dhw = entry.get("stored_hotwater")
            if (
                isinstance(dhw, dict)
                and dhw.get("hotwater_valve") == device_id
            ):
                return True
        return False

    # -- packet handler ------------------------------------------------------

    async def _on_packet(self, dto: PacketDTO) -> None:
        """Async wrapper for the gateway msg_handler interface.

        Delegates to the sync ``_process_packet`` so tests can call it
        directly without an event loop.
        """
        try:
            self._process_packet(dto)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "DiscoveryScan: error processing packet %s: %s", dto, err
            )

    def _process_packet(self, dto: PacketDTO) -> None:
        """Classify packet, update in-memory dict. No disk I/O.

        Called for every valid packet from the gateway. Must be fast —
        just dict lookups and updates.
        """
        # Extract device IDs from the packet
        source = dto.addr1.strip()
        destination = dto.addr2.strip()
        addr3 = dto.addr3.strip() if dto.addr3 else ""
        code = str(dto.code).strip()
        verb = dto.verb if dto.verb else ""

        # Parse RSSI (stored as string in PacketDTO)
        rssi: float | None = None
        if dto.rssi:
            with contextlib.suppress(ValueError, TypeError):
                rssi = float(dto.rssi)

        # Extract zone_index from payload if this is a binding code
        zone_index = (
            _extract_zone_index_from_payload(dto.payload or dto.raw_payload)
            if code in _ZONE_BINDING_CODES
            else None
        )

        # Extract domain_id (FC/FA/F9) -- the authoritative source is
        # the 000C binding table.  The 3B00/3EF0 TPI broadcast is only
        # a fallback hint (both appliance_control and hotwater_valve
        # relays send these codes).
        # See issue 834 comment 5044906835.
        domain_id: str | None = None
        is_authoritative_domain = False
        if code == Code._000C and dto.payload:
            domain_id = _extract_domain_id_from_000c(dto.payload)
            if domain_id:
                is_authoritative_domain = True
        if not domain_id:
            # Context check (issue 834): before using the 3B00/3EF0 FC
            # heuristic, check if the device is already declared as
            # hotwater_valve in the schema.  If so, it is the DHW valve
            # relay (FA domain), NOT the appliance_control (FC domain).
            # Both relays broadcast 3B00/3EF0, so the heuristic alone is
            # ambiguous — the schema declaration is the disambiguator.
            if not self._is_declared_hotwater_valve(source):
                domain_id = (
                    "FC"
                    if _is_appliance_control_signal(source, code, verb, True)
                    else None
                )

        # Process each address in the packet
        # source: high-confidence (device is actively sending)
        if source and _is_valid_address(source):
            self._process_device(
                source,
                code=code,
                verb=verb,
                rssi=rssi,
                zone_index=zone_index,
                domain_id=domain_id,
                is_authoritative_domain=is_authoritative_domain,
                is_source=True,
                destination=destination,
            )

        # destination: lower-confidence (device is being talked to)
        if destination and _is_valid_address(destination):
            self._process_device(
                destination,
                code=code,
                verb=verb,
                rssi=None,  # RSSI is for the sender, not the receiver
                zone_index=zone_index,
                domain_id=None,
                is_source=False,
                destination=None,
                source=source,  # who sent this packet (for HVAC reply inference)
            )

        # addr3: lowest-confidence (broadcast target or relay)
        if (
            addr3
            and _is_valid_address(addr3)
            and addr3 not in (source, destination)
        ):
            self._process_device(
                addr3,
                code=code,
                verb=verb,
                rssi=None,
                zone_index=None,
                domain_id=None,
                is_source=False,
                destination=None,
            )

    def _process_device(
        self,
        device_id: str,
        *,
        code: str,
        verb: str,
        rssi: float | None,
        zone_index: str | None,
        domain_id: str | None = None,
        is_authoritative_domain: bool = False,
        is_source: bool,
        destination: str | None,
        source: str | None = None,
    ) -> None:
        """Update or create a discovery entry for a single device."""
        # 18: devices are HGI gateways — track them (so we know they're on
        # the network and can include them in the schema as HGI type), but
        # don't process zone bindings or heating topology for them.
        is_hgi = device_id.startswith("18:")

        # For known HGI devices, create a minimal entry if not yet tracked,
        # or just update last_seen if already tracked.  Don't re-classify
        # or mark as dirty — the HGI is already known (in the known_list)
        # and should not trigger discovery notifications.
        if is_hgi and self._is_known(device_id):
            now = dt.now().isoformat(timespec="seconds")
            device = self._devices.get(device_id)
            if device is None:
                # First time seeing this known HGI — create a minimal entry
                # so it appears in scan results, but don't mark dirty (no
                # discovery notification needed for a known device).
                device = DiscoveredDevice(
                    device_id=device_id,
                    first_seen=now,
                    last_seen=now,
                    likely_type=DevType.HGI,
                    codes_seen=[code] if code else [],
                    rssi=rssi,
                    confidence="high",
                    is_battery=False,
                    source_count=1 if is_source else 0,
                    destination_count=0 if is_source else 1,
                )
                self._devices[device_id] = device
                self._dirty = True  # persist the new entry
                _LOGGER.debug(
                    "DiscoveryScan: tracking known HGI %s (not a new discovery)",
                    device_id,
                )
            else:
                device.last_seen = now
                if is_source:
                    device.source_count += 1
                else:
                    device.destination_count += 1
                if code and code not in device.codes_seen:
                    device.codes_seen.append(code)
                    device.codes_seen.sort()
                    self._dirty = True  # persist updated codes
                if rssi is not None and is_source:
                    if device.rssi is None:
                        device.rssi = rssi
                    else:
                        device.rssi = (device.rssi + rssi) / 2
                    self._dirty = True  # persist updated RSSI
            return

        # For known devices, still update zone bindings (they may have been
        # accepted before the scan engine captured zone_index from broadcast
        # traffic).  Skip full processing (classification, confidence, etc.)
        # since the device is already known.
        if self._is_known(device_id) and not is_hgi:
            device = self._devices.get(device_id)
            if device is None:
                # Known device not yet tracked in scan — create a minimal
                # entry so codes_seen is accumulated (needed for DHW valve
                # inference via 1100 code, etc.)
                now = dt.now().isoformat(timespec="seconds")
                # Use the declared class from the known_list as the
                # initial likely_type (issue 1000).  The known_list is
                # derived from the schema, which is authoritative.
                # This ensures the scan engine starts with the declared
                # class and only re-classifies after contradiction
                # threshold is reached.  Without this, a restarted scan
                # engine creates 37:169161 with likely_type=REM (prefix
                # fallback) instead of FAN (declared), so no
                # contradiction is ever detected.
                declared_class = self._get_declared_class(device_id)
                if declared_class is not None:
                    likely_type = declared_class
                    initial_conf = "high"  # declared class is authoritative
                else:
                    likely_type = _classify(
                        device_id, code, verb, is_source=is_source
                    )
                    # Confidence: "high" if evidence-based (VC pair
                    # match, CTL-only code, or zone binding code),
                    # "medium" if prefix fallback (37: → REM is a
                    # guess).  This prevents false-positive class
                    # mismatches where the scan engine confidently says
                    # "REM" based on a prefix fallback for a device
                    # that is actually a CO2.
                    vc_key = (verb, code)
                    is_vc_match = (
                        vc_key in _VC_TO_TYPE
                        and _VC_TO_TYPE[vc_key]
                        in _AMBIGUOUS_HVAC_PREFIX_TYPES.get(
                            device_id[:2], frozenset()
                        )
                    ) or (
                        is_source
                        and (
                            code in _CTL_ONLY_CODES
                            or (
                                code in _CTL_ONLY_CODES_WITH_VERB
                                and verb in _CTL_ONLY_CODES_WITH_VERB[code]
                            )
                        )
                    )
                    is_binding = is_source and code in _ZONE_BINDING_CODES
                    initial_conf = (
                        "high" if (is_vc_match or is_binding) else "medium"
                    )
                device = DiscoveredDevice(
                    device_id=device_id,
                    first_seen=now,
                    last_seen=now,
                    likely_type=likely_type,
                    codes_seen=[code] if code else [],
                    rssi=rssi,
                    confidence=initial_conf,
                    is_battery=code in _BATTERY_CODES,
                    source_count=1 if is_source else 0,
                    destination_count=0 if is_source else 1,
                    domain_id=domain_id if domain_id and is_source else None,
                    is_authoritative_domain=(
                        is_authoritative_domain
                        and bool(domain_id)
                        and is_source
                    ),
                )
                self._devices[device_id] = device
                self._dirty = True
                _LOGGER.debug(
                    "DiscoveryScan: tracking known device %s (not a new discovery)",
                    device_id,
                )
            else:
                device.last_seen = dt.now().isoformat(timespec="seconds")
                if is_source:
                    device.source_count += 1
                else:
                    device.destination_count += 1
                if code and code not in device.codes_seen:
                    device.codes_seen.append(code)
                    device.codes_seen.sort()
                    self._dirty = True
                if rssi is not None and is_source:
                    if device.rssi is None:
                        device.rssi = rssi
                    else:
                        device.rssi = (device.rssi + rssi) / 2
                    self._dirty = True
                if (
                    zone_index
                    and is_source
                    and not device_id.startswith("01:")
                ):
                    # Skip CTL (01:) — it sends 000A with zone config for
                    # multiple zones, not its own zone binding.  Setting
                    # zone_index on the CTL corrupts its comment and schema
                    # entry (issue 813).
                    bound_changed = device.zone_index != zone_index
                    device.zone_index = zone_index
                    if (
                        destination
                        and _is_valid_address(destination)
                        and destination != device_id
                    ):
                        if device.bound_to != destination:
                            device.bound_to = destination
                            bound_changed = True
                    if bound_changed:
                        device.confidence = "high"
                        self._dirty = True
                        _LOGGER.debug(
                            "DiscoveryScan: updated zone binding for known "
                            "device %s (zone=%s, bound_to=%s)",
                            device_id,
                            zone_index,
                            device.bound_to,
                        )
                # Domain_id update (issue 834): an authoritative 000C
                # binding overrides any previous hint; a 3B00/3EF0 hint
                # only applies if no domain_id is set yet.
                if (
                    domain_id
                    and is_source
                    and _should_update_domain_id(
                        device.domain_id, domain_id, is_authoritative_domain
                    )
                ):
                    device.domain_id = domain_id
                    device.is_authoritative_domain = is_authoritative_domain
                    device.confidence = "high"
                    self._dirty = True

                # HVAC topology inference for known devices: a FAN (32:)
                # sending a directed I/RP to this device confirms binding.
                # This is the same check as the unknown-device path below
                # (line ~725), but the known-device path returns early at
                # line ~582, so we need to run it here too.
                if (
                    not is_hgi
                    and not device.bound_to
                    and not is_source
                    and source
                    and _is_valid_address(source)
                    and source.startswith("32:")
                    and verb in (Verb.I_, Verb.RP)
                    and code in _HVAC_PARENT_INFERENCE_CODES
                ):
                    device.bound_to = source
                    self._dirty = True
                    _LOGGER.debug(
                        "DiscoveryScan: HVAC bound_to %s -> %s (known device, "
                        "code=%s, verb=%s)",
                        device_id,
                        source,
                        code,
                        verb.strip(),
                    )
                # REM→FAN inference (known device): a REM/CO2 sending a
                # directed packet (W/RQ/I) to a FAN (32:) confirms
                # binding.  More authoritative than FAN→REM — the REM
                # only sends to the FAN it was 1FC9-paired with (not any
                # FAN in range).  A directed I is proof because the REM
                # addresses its paired FAN specifically; a broadcast I
                # (dst = --:------) is filtered out by _is_valid_address.
                elif (
                    not is_hgi
                    and not device.bound_to
                    and is_source
                    and destination
                    and _is_valid_address(destination)
                    and destination.startswith("32:")
                    and verb in (Verb.W_, Verb.RQ, Verb.I_)
                    and code in _HVAC_PARENT_INFERENCE_CODES
                ):
                    device.bound_to = destination
                    device.confidence = "high"
                    self._dirty = True
                    _LOGGER.debug(
                        "DiscoveryScan: HVAC bound_to %s -> %s (known device, "
                        "REM→FAN, code=%s, verb=%s)",
                        device_id,
                        destination,
                        code,
                        verb.strip(),
                    )
                # DHW topology inference for known devices: directed interaction
                # between a CTL (01:/23:) and a DHW sensor (07:) confirms binding.
                elif (
                    not is_hgi
                    and device_id.startswith("07:")
                    and not is_source
                    and source
                    and _is_valid_address(source)
                    and source.startswith(("01:", "23:"))
                    and code in (Code._10A0, Code._1260, Code._000C)
                ):
                    if device.bound_to != source:
                        device.bound_to = source
                        device.confidence = "high"
                        self._dirty = True
                elif (
                    not is_hgi
                    and device_id.startswith("07:")
                    and is_source
                    and destination
                    and _is_valid_address(destination)
                    and destination.startswith(("01:", "23:"))
                    and code in (Code._10A0, Code._1260)
                ):
                    if device.bound_to != destination:
                        device.bound_to = destination
                        device.confidence = "high"
                        self._dirty = True

                # Re-classify known devices using the same _classify
                # function as for new devices (issue 1000).  Without
                # this, likely_type is frozen at the first packet's
                # classification, so a FAN-classified device that sends
                # DIS-like packets (RQ 31DA, I 22F1) never gets
                # re-classified and check_class_mismatches sees no
                # mismatch.
                #
                # Uses a contradiction counter: consecutive source
                # packets where _classify disagrees with likely_type
                # increment the counter; a matching packet resets it.
                # After _CONTRADICTION_THRESHOLD, likely_type is
                # updated.  This avoids flapping on a single ambiguous
                # packet.
                #
                # When the strategy pattern arrives, _classify becomes
                # strategy.classify(packet) — this tracking naturally
                # moves with it.
                if is_source and code:
                    new_type = _classify(
                        device_id,
                        code,
                        verb,
                        is_source=is_source,
                        device=device,
                    )
                    if (
                        new_type != DevType.DEV
                        and new_type != device.likely_type
                    ):
                        # Only count evidence-based contradictions
                        # (VC pair match or CTL-only code).  A prefix
                        # fallback (e.g. 37: → REM) is a guess, not
                        # evidence — it should NOT contradict a declared
                        # class.  Otherwise a CO2 device sending generic
                        # codes like 10E0 would be re-classified as REM
                        # just because 37: falls to REM by prefix.
                        if not _is_evidence_based(
                            device_id, code, verb, is_source
                        ):
                            # Prefix fallback — skip, don't contradict
                            pass
                        else:
                            device.contradiction_count += 1
                            self._dirty = True
                            if (
                                device.contradiction_count
                                >= _CONTRADICTION_THRESHOLD
                            ):
                                _LOGGER.debug(
                                    "DiscoveryScan: re-classified known "
                                    "device %s: %s -> %s (after %d "
                                    "contradictions, code=%s, verb=%s)",
                                    device_id,
                                    device.likely_type,
                                    new_type,
                                    device.contradiction_count,
                                    code,
                                    verb.strip(),
                                )
                                device.likely_type = new_type
                                device.contradiction_count = 0
                                # Re-classification after threshold is
                                # evidence-based — set confidence to
                                # "high" so check_class_mismatches
                                # trusts it (it skips HVAC devices with
                                # "medium" confidence prefix-fallback
                                # classifications).
                                device.confidence = "high"
                    elif new_type == device.likely_type:
                        # Matching packet — reset contradiction count
                        if device.contradiction_count > 0:
                            device.contradiction_count = 0
                            self._dirty = True
            return

        now = dt.now().isoformat(timespec="seconds")
        device = self._devices.get(device_id)

        if device is None:
            # New device — classify and create entry
            likely_type = _classify(device_id, code, verb, is_source=is_source)
            device = DiscoveredDevice(
                device_id=device_id,
                first_seen=now,
                last_seen=now,
                likely_type=likely_type,
                codes_seen=[code] if code else [],
                rssi=rssi,
                confidence=_initial_confidence(is_source, code, verb),
                is_battery=code in _BATTERY_CODES,
                source_count=1 if is_source else 0,
                destination_count=0 if is_source else 1,
            )
            # Set binding info if available
            # zone_index is extracted from the payload and is valid even for
            # broadcasts (dst == --:------).  bound_to requires a valid dst.
            # Skip for HGI gateways — they don't have zone bindings.
            # Skip for CTL (01:) — it sends 000A with zone config for
            # multiple zones, not its own zone binding (issue 813).
            if (
                zone_index
                and is_source
                and not is_hgi
                and not device_id.startswith("01:")
            ):
                device.zone_index = zone_index
                if (
                    destination
                    and _is_valid_address(destination)
                    and destination != device_id
                ):
                    device.bound_to = destination
                device.confidence = (
                    "high"  # binding telemetry = high confidence
                )
            # Domain_id from 000C binding (authoritative) or 3B00/3EF0
            # (hint).  See issue 834: a 000C FA binding must override a
            # previous 3B00/3EF0 FC hint (the BDR is hotwater_valve, not
            # appliance_control).
            if (
                domain_id
                and is_source
                and not is_hgi
                and _should_update_domain_id(
                    device.domain_id, domain_id, is_authoritative_domain
                )
            ):
                device.domain_id = domain_id
                device.is_authoritative_domain = is_authoritative_domain
                device.confidence = "high"
            # HVAC topology inference: a FAN (32:) sending a directed packet
            # (I or RP) to this device confirms the binding — the FAN is the
            # controller and it's communicating with its paired remote.
            # Skip for HGI gateways — they don't have HVAC parent bindings.
            elif (
                not is_hgi
                and not is_source
                and source
                and _is_valid_address(source)
                and source.startswith("32:")
                and verb in (Verb.I_, Verb.RP)
                and code in _HVAC_PARENT_INFERENCE_CODES
            ):
                device.bound_to = source
            # REM→FAN inference (new device): a REM/CO2 sending a
            # directed packet (W/RQ/I) to a FAN (32:) confirms binding.
            # More authoritative than FAN→REM — the REM only sends to
            # its 1FC9-paired FAN.  A directed I is proof (the REM
            # addresses its paired FAN specifically); a broadcast I
            # (dst = --:------) is filtered out by _is_valid_address.
            elif (
                not is_hgi
                and is_source
                and destination
                and _is_valid_address(destination)
                and destination.startswith("32:")
                and verb in (Verb.W_, Verb.RQ, Verb.I_)
                and code in _HVAC_PARENT_INFERENCE_CODES
            ):
                device.bound_to = destination
                device.confidence = "high"
            # DHW topology inference: directed interaction between CTL and DHW sensor
            elif (
                not is_hgi
                and device_id.startswith("07:")
                and not is_source
                and source
                and _is_valid_address(source)
                and source.startswith(("01:", "23:"))
                and code in (Code._10A0, Code._1260, Code._000C)
            ):
                device.bound_to = source
                device.confidence = "high"
            elif (
                not is_hgi
                and device_id.startswith("07:")
                and is_source
                and destination
                and _is_valid_address(destination)
                and destination.startswith(("01:", "23:"))
                and code in (Code._10A0, Code._1260)
            ):
                device.bound_to = destination
                device.confidence = "high"
            self._devices[device_id] = device
            self._dirty = True
            _LOGGER.info(
                "DiscoveryScan: new device %s (%s, %s)",
                device_id,
                likely_type,
                device.confidence,
            )
            return

        # Existing device — enrich
        changed = False

        device.last_seen = now
        if is_source:
            device.source_count += 1
        else:
            device.destination_count += 1

        # Add code to codes_seen (deduplicated, keep sorted)
        if code and code not in device.codes_seen:
            device.codes_seen.append(code)
            device.codes_seen.sort()
            changed = True

        # Update RSSI as running average (only from src packets)
        if rssi is not None and is_source:
            if device.rssi is None:
                device.rssi = rssi
            else:
                device.rssi = (device.rssi + rssi) / 2
            changed = True

        # Update battery flag
        if code in _BATTERY_CODES and not device.is_battery:
            device.is_battery = True
            changed = True

        # Update zone binding (prefer src packets with zone_index)
        # zone_index is extracted from the payload and is valid even for
        # broadcasts (dst == --:------).  bound_to requires a valid dst
        # that is different from the device itself.
        # Skip for HGI gateways — they don't have zone bindings.
        if zone_index and is_source and not is_hgi:
            bound_changed = device.zone_index != zone_index
            device.zone_index = zone_index
            if (
                destination
                and _is_valid_address(destination)
                and destination != device_id
            ):
                if device.bound_to != destination:
                    device.bound_to = destination
                    bound_changed = True
            if bound_changed:
                device.confidence = "high"
                changed = True

        # Update domain_id (issue 834): an authoritative 000C binding
        # overrides a previous 3B00/3EF0 hint; a hint only applies
        # if no domain_id is set yet.
        if (
            domain_id
            and is_source
            and not is_hgi
            and _should_update_domain_id(
                device.domain_id, domain_id, is_authoritative_domain
            )
        ):
            device.domain_id = domain_id
            device.is_authoritative_domain = is_authoritative_domain
            device.confidence = "high"
            changed = True

        # HVAC topology inference: a FAN (32:) sending a directed packet
        # (I or RP) to this device confirms the binding — the FAN is the
        # controller and it's communicating with its paired remote.
        # Infer bound_to from the packet source if not already set.
        # Skip for HGI gateways — they don't have HVAC parent bindings.
        if (
            not is_hgi
            and not device.bound_to
            and not is_source
            and source
            and _is_valid_address(source)
            and source.startswith("32:")
            and verb in (Verb.I_, Verb.RP)
            and code in _HVAC_PARENT_INFERENCE_CODES
        ):
            device.bound_to = source
            changed = True
        # REM→FAN inference (existing device): a REM/CO2 sending a
        # directed packet (W/RQ/I) to a FAN (32:) confirms binding.
        # More authoritative than FAN→REM — the REM only sends to
        # its 1FC9-paired FAN.  A directed I is proof (the REM
        # addresses its paired FAN specifically); a broadcast I
        # (dst = --:------) is filtered out by _is_valid_address.
        elif (
            not is_hgi
            and not device.bound_to
            and is_source
            and destination
            and _is_valid_address(destination)
            and destination.startswith("32:")
            and verb in (Verb.W_, Verb.RQ, Verb.I_)
            and code in _HVAC_PARENT_INFERENCE_CODES
        ):
            device.bound_to = destination
            device.confidence = "high"
            changed = True
            _LOGGER.debug(
                "DiscoveryScan: HVAC bound_to %s -> %s (REM→FAN, "
                "code=%s, verb=%s)",
                device_id,
                destination,
                code,
                verb.strip(),
            )
        # DHW topology inference for existing devices
        elif (
            not is_hgi
            and device_id.startswith("07:")
            and not is_source
            and source
            and _is_valid_address(source)
            and source.startswith(("01:", "23:"))
            and code in (Code._10A0, Code._1260, Code._000C)
        ):
            if device.bound_to != source:
                device.bound_to = source
                device.confidence = "high"
                changed = True
        elif (
            not is_hgi
            and device_id.startswith("07:")
            and is_source
            and destination
            and _is_valid_address(destination)
            and destination.startswith(("01:", "23:"))
            and code in (Code._10A0, Code._1260)
        ):
            if device.bound_to != destination:
                device.bound_to = destination
                device.confidence = "high"
                changed = True

        # Upgrade confidence based on accumulated evidence
        new_conf = _recompute_confidence(device)
        if new_conf != device.confidence:
            device.confidence = new_conf
            changed = True

        # Re-classify if we have more info now
        new_type = _classify(
            device_id, code, verb, is_source=is_source, device=device
        )
        if new_type != device.likely_type and new_type != DevType.DEV:
            device.likely_type = new_type
            changed = True

        if changed:
            self._dirty = True

    # -- public API ----------------------------------------------------------

    def get_devices(
        self,
        *,
        status: str | None = None,
        likely_type: str | None = None,
        min_confidence: str | None = None,
    ) -> list[DiscoveredDevice]:
        """Return discovered devices, optionally filtered.

        :param status: Not used by the engine (ramses_cc concern). Accepted
            for API compatibility — filtering by status is done by the consumer.
        :param likely_type: Filter by DevType value (e.g. "TRV").
        :param min_confidence: Only return devices with at least this confidence
            level ("low" < "medium" < "high").
        """
        result = list(self._devices.values())

        if likely_type:
            result = [d for d in result if d.likely_type == likely_type]

        if min_confidence:
            order = {"low": 0, "medium": 1, "high": 2}
            min_val = order.get(min_confidence, 0)
            result = [
                d for d in result if order.get(d.confidence, 0) >= min_val
            ]

        return result

    def get_device(self, device_id: str) -> DiscoveredDevice | None:
        """Return a single discovered device by ID, or None."""
        return self._devices.get(device_id)

    def register_known_hgi(self, device_id: str) -> None:
        """Register a known HGI as present in the scan.

        This is used for pool member HGIs that are connected but may not
        have sent any packets (e.g. a USB HGI with ``skip_signature=True``
        that never sends a ``7FFF`` puzzle response).  Without this, the
        HGI would not appear in the scan results and would be flagged as
        "lost" by the discovery manager.

        Only creates a minimal entry if the HGI is not already tracked.
        Does not trigger discovery notifications (the HGI is already known).

        :param device_id: The HGI device ID (must start with ``18:``).
        """
        if not device_id or not device_id.startswith("18:"):
            return
        if device_id in self._devices:
            # Already tracked — update last_seen
            self._devices[device_id].last_seen = dt.now().isoformat(
                timespec="seconds"
            )
            return
        now = dt.now().isoformat(timespec="seconds")
        self._devices[device_id] = DiscoveredDevice(
            device_id=device_id,
            first_seen=now,
            last_seen=now,
            likely_type=DevType.HGI,
            codes_seen=[],
            rssi=None,
            confidence="high",
            is_battery=False,
            source_count=0,
            destination_count=0,
        )
        _LOGGER.debug(
            "DiscoveryScan: registered known HGI %s (pool member, no packets)",
            device_id,
        )

    def remove_device(self, device_id: str) -> bool:
        """Remove a device from the in-memory list.

        Returns True if the device was present and removed.
        """
        if device_id in self._devices:
            del self._devices[device_id]
            self._dirty = True
            return True
        return False

    def export_json(self) -> str:
        """Export the full device list as JSON (for CLI, persistence).

        Returns a JSON string with a ``version`` key and a ``devices`` list.
        """
        data = {
            "version": 1,
            "exported_at": dt.now().isoformat(timespec="seconds"),
            "devices": [
                d.to_dict()
                for d in sorted(
                    self._devices.values(), key=lambda d: d.device_id
                )
            ],
        }
        return json.dumps(data, indent=2, sort_keys=False)

    def import_json(self, data: str) -> None:
        """Load a previously exported list (for resume after restart).

        Replaces the current in-memory dict.

        For known devices (in the known_list), the declared class
        overrides the imported likely_type (issue 1000).  This ensures
        the scan engine always starts from the declared class and only
        re-classifies after contradiction threshold is reached.  Without
        this, a restarted scan engine keeps a stale likely_type from a
        previous VC pair match (e.g. FAN from I 31DA) even though the
        device is declared as REM in the schema.
        """
        parsed = json.loads(data)
        self._devices = {
            d["device_id"]: DiscoveredDevice.from_dict(d)
            for d in parsed.get("devices", [])
        }
        # Override likely_type for known devices with declared class
        had_overrides = False
        for device_id, device in self._devices.items():
            declared = self._get_declared_class(device_id)
            if declared is not None and device.likely_type != declared:
                _LOGGER.debug(
                    "DiscoveryScan: overriding imported likely_type "
                    "for known device %s: %s -> %s (declared class)",
                    device_id,
                    device.likely_type,
                    declared,
                )
                device.likely_type = declared
                device.contradiction_count = 0
                device.confidence = "high"
                had_overrides = True
        self._dirty = had_overrides
        _LOGGER.info("DiscoveryScan: imported %d devices", len(self._devices))

    def device_count(self) -> int:
        """Return the number of discovered devices."""
        return len(self._devices)


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


def _is_valid_address(device_id: str) -> bool:
    """Quick check if a device ID looks valid (N.N:NNNNNN or N:NNNNNN).

    Filters out broadcast addresses (18:73030, 18:14803, 18:000730),
    the null/broadcast device type 63: (NUL — e.g. 63:262142=0xFFFFFE,
    63:262143=0xFFFFFF, the latter also used by the HGI80 to disguise its
    own address), placeholder addresses (--:------), and corrupt IDs.
    """
    if not device_id or len(device_id) < 8:
        return False
    # Skip broadcast/multicast addresses
    if device_id in ("18:73030", "18:14803", "18:000730"):
        return False
    # Skip the null/broadcast device type 63: (NUL) — no real device uses
    # type 63; both 63:262142 (0xFFFFFE) and 63:262143 (0xFFFFFF) are
    # sentinels, and the HGI80 emits 63:262143 as a self-disguise address
    if device_id.startswith("63:"):
        return False
    # Skip placeholder/empty addresses (e.g. "--:------")
    if device_id.startswith("-") or device_id.startswith("00:------"):
        return False
    # Basic format check: should contain a colon
    return ":" in device_id


def _classify(
    device_id: str,
    code: str,
    verb: str,
    *,
    is_source: bool,
    device: DiscoveredDevice | None = None,
) -> DevType:
    """Classify a device based on prefix, verb/code, and accumulated evidence.

    Priority:
    1. Unambiguous HVAC prefix (32:=FAN) — takes precedence over verb/code
       pairs (a FAN sends 22F1, but that doesn't make it a REM)
    2. CTL-only codes — if device sends these, it's a CTL
    3. Verb+code pair (HVAC) — for ambiguous HVAC prefixes (29:, 37:), only
       accept VC pairs that map to a type valid for that prefix
    4. CH prefix — fallback for heating domain devices
    5. Accumulated codes — re-evaluate with full evidence

    TODO: DIS (Orcon RF15 Display) is not distinguishable from REM by
    this function.  A DIS sends RQ 2411 and RQ 31DA as normal behavior,
    but so does a REM (per the protocol table, with "VMI only?" caveats).
    The scan engine falls to the 37: prefix fallback (REM) for both.
    When the strategy pattern arrives (issue 939), a DisStrategy could
    use 2411 frequency or the presence of 1470/042F (DIS-only codes) to
    distinguish.  See also: protocol/ramses.py _HVAC_VC_PAIR_BY_CLASS,
    pipeline/topology_handlers/hvac.py HvacTopologyHandler.
    """
    prefix = device_id[:2]

    # 1. Unambiguous HVAC prefixes (32:=FAN) — check first
    if prefix in _UNAMBIGUOUS_HVAC_PREFIXES:
        return _PREFIX_TO_TYPE[prefix]

    # 2. CTL-only codes (only if this device is the sender)
    #    Some codes are CTL-only depending on verb (e.g. 313F I=CTL, RQ=TRV)
    if is_source:
        if code in _CTL_ONLY_CODES:
            return DevType.CTL
        if code in _CTL_ONLY_CODES_WITH_VERB:
            ctl_verbs = _CTL_ONLY_CODES_WITH_VERB[code]
            if verb in ctl_verbs:
                return DevType.CTL

    # 3. Check verb+code pair (HVAC domain)
    #    For ambiguous HVAC prefixes (e.g. 37:), only accept VC pairs that
    #    map to a type valid for that prefix (e.g. 31D9 I→FAN is rejected
    #    for 37: because FAN is 32: only).
    vc_key = (verb, code)
    if vc_key in _VC_TO_TYPE:
        vc_type = _VC_TO_TYPE[vc_key]
        valid_types = _AMBIGUOUS_HVAC_PREFIX_TYPES.get(prefix)
        if valid_types is None or vc_type in valid_types:
            return vc_type

    # 4. Check accumulated codes if we have a device
    if device and is_source:
        for code_seen in device.codes_seen:
            if code_seen in _CTL_ONLY_CODES:
                return DevType.CTL
        # Check verb-aware CTL codes from accumulated data
        for code_seen in device.codes_seen:
            if code_seen in _CTL_ONLY_CODES_WITH_VERB:
                ctl_verbs = _CTL_ONLY_CODES_WITH_VERB[code_seen]
                # Check if any verb in the accumulated data matches
                # We don't track per-code verbs, so be conservative:
                # only classify as CTL if the current verb matches
                if verb in ctl_verbs:
                    return DevType.CTL
        # NOTE: HVAC VC-pair classification from accumulated codes_seen
        # was removed — it caused misclassification because codes_seen
        # doesn't track per-code verbs.  A DIS sending I 22F1 was
        # classified as FAN because 31DA was in codes_seen (from a
        # previous RQ 31DA) and (I, 31DA) maps to FAN.  Step 3 above
        # already checks the current (verb, code) pair, which is
        # sufficient for correct classification.

    # 5. CH prefix fallback
    if prefix in _PREFIX_TO_TYPE:
        return _PREFIX_TO_TYPE[prefix]

    return DevType.DEV


def _initial_confidence(is_source: bool, code: str, verb: str) -> str:
    """Determine initial confidence for a newly seen device."""
    if is_source and code in _ZONE_BINDING_CODES:
        return "high"  # binding telemetry from src = high confidence
    if is_source:
        return "medium"  # device is actively sending
    return "low"  # only seen as dst/addr3


def _recompute_confidence(device: DiscoveredDevice) -> str:
    """Recompute confidence based on accumulated evidence."""
    # High: has zone binding info (zone_index, with or without bound_to)
    if device.zone_index:
        return "high"

    # High: sends CTL-only codes
    if any(c in _CTL_ONLY_CODES for c in device.codes_seen):
        return "high"
    # High: sends verb-aware CTL-only codes (e.g. 313F I/RP)
    if any(c in _CTL_ONLY_CODES_WITH_VERB for c in device.codes_seen):
        return "high"

    # Medium: seen as src multiple times
    if device.source_count >= 2:
        return "medium"

    # Medium: seen as src at least once with known codes
    if device.source_count >= 1 and len(device.codes_seen) >= 2:
        return "medium"

    # Low: only seen as dst, or seen once as src
    return "low"


def _extract_zone_index_from_payload(payload: Any | str) -> str | None:
    """Extract and validate the zone_index from a packet payload.

    Zone index is typically the first 2 hex chars of a raw payload string or
    the zone_index property of a typed PayloadBase object. Returns None if
    payload is empty, too short, or not a valid zone index.

    Valid zone indices are 00-0B (12 zones max). Values like FC (appliance
    control domain), 7F (broadcast), or other non-zone domain IDs are
    rejected.

    :param payload: Raw hex string, PayloadBase object, or list of payloads.
    :type payload: Any | str
    :returns: Validated uppercase zone index hex string ("00"-"0B"), or None.
    :rtype: str | None
    """
    if isinstance(payload, list):
        for item in payload:
            result = _extract_zone_index_from_payload(item)
            if result is not None:
                return result
        return None
    if hasattr(payload, "zone_index"):
        zone_index_val = payload.zone_index
        if isinstance(zone_index_val, int):
            if zone_index_val > 0x0B:
                return None
            return f"{zone_index_val:02X}"
        index_str = str(zone_index_val).upper()
    elif isinstance(payload, str) and len(payload) >= 2:
        index_str = payload[:2].upper()
    else:
        return None

    # Validate: must be hex chars
    try:
        zone_num = int(index_str, 16)
    except ValueError:
        return None
    # Validate: zone indices are 00-0B (12 zones max)
    if zone_num > 0x0B:
        return None
    return index_str


def _should_update_domain_id(
    current: str | None,
    new: str | None,
    is_authoritative: bool,
) -> bool:
    """Decide whether to update a device's domain_id.

    An authoritative 000C binding always wins -- it overrides any
    previous value (including a 3B00/3EF0 FC hint that was set before
    the 000C binding arrived). A non-authoritative 3B00/3EF0 hint
    only applies if the device has no domain_id yet (None).

    See issue 834 comment 5044906835: a BDR hotwater_valve broadcasting
    3B00/3EF0 gets domain_id=FC from the hint, but when the 000C HTG
    binding arrives (domain FA), it must override the hint.

    :param current: Current domain_id on the device.
    :type current: str | None
    :param new: New candidate domain_id to evaluate.
    :type new: str | None
    :param is_authoritative: True if from 000C binding, False if from hint.
    :type is_authoritative: bool
    :returns: True if domain_id should be updated, False otherwise.
    :rtype: bool
    """
    if new is None:
        return False
    if is_authoritative:
        return current != new
    # Non-authoritative hint: only set if no domain_id exists yet
    return current is None


def _is_appliance_control_signal(
    device_id: str, code: str, verb: str, is_source: bool
) -> bool:
    """Return True if this packet hints the src is an appliance_control.

    A BDR (13:) or OTB (10:) broadcasting 3B00 or 3EF0 as I *may* be
    acting as the boiler relay (TPI loop).  However, this is NOT
    authoritative -- a DHW valve relay also broadcasts these codes
    (issue 834 comment 5044906835).  The 000C binding table is the
    authoritative source for domain_id.

    This heuristic is used only as a fallback hint when no 000C binding
    has been observed for the device.  See
    ``_extract_domain_id_from_000c`` for the authoritative path.

    See issue 834: without this signal, a BDR with no zone_index is
    misclassified as a hotwater_valve by ramses_cc's
    generate_schema_entry.
    """
    if not is_source:
        return False
    if not device_id.startswith(("13:", "10:")):
        return False
    if code not in _APPLIANCE_CONTROL_CODES:
        return False
    # 3B00/3EF0 are broadcast as I by the relay; RQ/RP are
    # directed replies (e.g. 3EF1 RP from the relay to the HGI)
    # and are not the TPI signature.
    return verb.strip() == "I"


def _extract_domain_id_from_000c(payload: Any | str) -> str | None:
    """Extract the domain_id (FC/FA/F9) from a 000C binding payload.

    000C payloads encode the domain in the zone_type field (payload[2:4])
    or on the typed ZoneDevicesPayload.domain_id property:
      - "0F" (APP) -> FC (appliance_control)
      - "0E" (HTG) -> FA (hotwater_valve, index 00) or F9 (heating_valve,
        index 01)
      - "0D" (DHW) -> FA (dhw_sensor, index 00) or F9 (index 01)

    This is the authoritative source for domain_id -- the controller's
    binding table explicitly declares which domain each relay belongs to.
    The 3B00/3EF0 TPI broadcast heuristic is ambiguous because both
    appliance_control and hotwater_valve relays send these codes.

    See issue 834 comment 5044906835: a BDR hotwater_valve broadcasting
    3B00/3EF0 was misclassified as appliance_control because the scan engine
    only used the TPI heuristic, not the 000C binding.

    :param payload: Raw hex string, ZoneDevicesPayload, or list of payloads.
    :type payload: Any | str
    :returns: "FC", "FA", "F9", or None if payload is not a domain
        binding.
    :rtype: str | None
    """
    if isinstance(payload, list):
        for item in payload:
            result = _extract_domain_id_from_000c(item)
            if result is not None:
                return result
        return None
    if hasattr(payload, "domain_id"):
        result_domain = payload.domain_id
        return str(result_domain) if result_domain is not None else None
    if not payload or not isinstance(payload, str) or len(payload) < 4:
        return None
    role = payload[2:4].upper()
    if role not in _000C_DOMAIN_ROLES:
        return None
    index_str = payload[:2].upper()
    if role == _000C_ROLE_APP:
        return "FC"
    # HTG/DHW: index "00" → FA, index "01" → F9
    return "FA" if index_str == "00" else "F9"
