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

from ramses_rf.const import DevType
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
_CTL_ONLY_CODES: frozenset[str] = frozenset({"1030", "1F09"})
_CTL_ONLY_CODES_WITH_VERB: dict[str, frozenset[str]] = {
    "313F": frozenset({" I", "RP"}),  # I/RP = CTL broadcasts time; RQ = TRV asks
}

# Codes that indicate battery-powered devices.
_BATTERY_CODES: frozenset[str] = frozenset({"1060", "1FC9"})

# Codes that carry zone_idx in the payload (binding telemetry).
# Used to extract zone assignment from traffic.
# NOTE: 30C9 (Room Setpoint) is excluded - its payload is 00{setpoint}
# where the first byte is always 00 (a constant), not a zone index.
# 3150 (Actuator State) and 12B0 (Window Open) have the real zone_idx.
# 000A (Zone Info) is sent by THMs (22:) with their zone_idx as payload —
# e.g. RQ 000A 001 01 means the THM is asking about zone 01 (its zone).
# The CTL and HGI also send 000A, but the HGI is excluded by is_hgi and
# the CTL's zone_idx is unused (it gets main_tcs, not a zone placement).
_ZONE_BINDING_CODES: frozenset[str] = frozenset(
    {"3150", "000C", "2309", "2349", "10A0", "1260", "12B0", "1F09", "000A"}
)

# HVAC codes sent by REMs/CO2s to their parent FAN (32:).  These are
# NOT binding protocol codes (binding uses 1FC9, done once with FAN off).
# They are operational commands.  A REM sending 22F1 to a FAN doesn't
# prove binding — the REM could be a neighbour's remote broadcasting.
# But a FAN sending a directed packet (I or RP) to a specific 37: device
# IS strong evidence of binding — the FAN is the controller and it's
# communicating with its paired remote.  See schema_architecture.md:
# "How HVAC topology COULD be derived from traffic".
_HVAC_PARENT_INFERENCE_CODES: frozenset[str] = frozenset(
    {"22F1", "31E0", "31DA", "10D0"}  # fan_mode, vent_demand, fan_status, outside_temp
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
_APPLIANCE_CONTROL_CODES: frozenset[str] = frozenset({"3B00", "3EF0"})

# 000C payload roles that map to domain IDs (not zone indices).
# See ramses_tx/const.py DEV_ROLE_MAP and parsers/heating.py
# complex_idx.
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
    "29": frozenset({DevType.FAN, DevType.CO2, DevType.HUM, DevType.REM, DevType.DIS}),
    "37": frozenset({DevType.FAN, DevType.REM, DevType.CO2, DevType.HUM, DevType.DIS}),
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
    zone_idx: str | None = None  # zone index if known from payload
    domain_id: str | None = None  # domain ID if known (FC=appliance_control)
    rssi: float | None = None  # running average
    confidence: str = "low"  # high, medium, low
    is_battery: bool = False  # seen sending battery info
    src_count: int = 0  # number of packets where this device was src
    dst_count: int = 0  # number of packets where this device was dst

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (for JSON export)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiscoveredDevice:
        """Deserialize from a plain dict (for JSON import/resume)."""
        return cls(**{k: data[k] for k in data if k in cls.__dataclass_fields__})


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

    def __init__(self, gwy: Gateway) -> None:
        self._gwy = gwy
        self._devices: dict[str, DiscoveredDevice] = {}
        self._dirty: bool = False
        self._remove_handler: Callable[[], None] | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Register as a raw packet handler on the gateway.

        Uses ``add_raw_pkt_handler`` (not ``add_msg_handler``) so the scan
        sees packets from unknown devices even when ``enforce_known_list=True``.
        The raw handler fires before the device ID filter.
        """
        if self._remove_handler is not None:
            _LOGGER.warning("DiscoveryScan.start(): already running")
            return
        self._remove_handler = self._gwy.add_raw_pkt_handler(self._on_packet)
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

    def _is_known(self, dev_id: str) -> bool:
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
        # TODO: when multiple HGI gateways are supported, this must check
        # against all gateway IDs, not just the single active one.
        engine = getattr(self._gwy, "_engine", None)
        transport = getattr(engine, "_transport", None) if engine else None
        if transport is not None:
            active_hgi = transport.get_extra_info(SZ_ACTIVE_HGI)
            if active_hgi == dev_id:
                return True
        # Also check via the hgi property (covers the case where the device
        # is in the registry but the transport extra_info is not set)
        if self._gwy.hgi and self._gwy.hgi.id == dev_id:
            return True

        # Check known_list (declared intent, derived from schema)
        if dev_id in self._gwy._gwy_config.known_list:
            return True

        # Check schema keys (CTL IDs are top-level keys — declared intent)
        return dev_id in self._gwy._gwy_config.schema

    # -- packet handler ------------------------------------------------------

    async def _on_packet(self, dto: PacketDTO) -> None:
        """Async wrapper for the gateway msg_handler interface.

        Delegates to the sync ``_process_packet`` so tests can call it
        directly without an event loop.
        """
        try:
            self._process_packet(dto)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("DiscoveryScan: error processing packet %s: %s", dto, err)

    def _process_packet(self, dto: PacketDTO) -> None:
        """Classify packet, update in-memory dict. No disk I/O.

        Called for every valid packet from the gateway. Must be fast —
        just dict lookups and updates.
        """
        # Extract device IDs from the packet
        src = dto.addr1.strip()
        dst = dto.addr2.strip()
        addr3 = dto.addr3.strip() if dto.addr3 else ""
        code = str(dto.code).strip()
        verb = dto.verb if dto.verb else ""

        # Parse RSSI (stored as string in PacketDTO)
        rssi: float | None = None
        if dto.rssi:
            with contextlib.suppress(ValueError, TypeError):
                rssi = float(dto.rssi)

        # Extract zone_idx from payload if this is a binding code
        zone_idx = (
            _extract_zone_idx(dto.payload) if code in _ZONE_BINDING_CODES else None
        )

        # Extract domain_id (FC/FA/F9) -- the authoritative source is
        # the 000C binding table.  The 3B00/3EF0 TPI broadcast is only
        # a fallback hint (both appliance_control and hotwater_valve
        # relays send these codes).
        # See issue 834 comment 5044906835.
        domain_id: str | None = None
        is_authoritative_domain = False
        if code == "000C" and dto.payload:
            domain_id = _extract_domain_id_from_000c(dto.payload)
            if domain_id:
                is_authoritative_domain = True
        if not domain_id:
            domain_id = (
                "FC" if _is_appliance_control_signal(src, code, verb, True) else None
            )

        # Process each address in the packet
        # src: high-confidence (device is actively sending)
        if src and _is_valid_address(src):
            self._process_device(
                src,
                code=code,
                verb=verb,
                rssi=rssi,
                zone_idx=zone_idx,
                domain_id=domain_id,
                is_authoritative_domain=is_authoritative_domain,
                is_src=True,
                dst=dst,
            )

        # dst: lower-confidence (device is being talked to)
        if dst and _is_valid_address(dst):
            self._process_device(
                dst,
                code=code,
                verb=verb,
                rssi=None,  # RSSI is for the sender, not the receiver
                zone_idx=zone_idx,
                domain_id=None,
                is_src=False,
                dst=None,
                src=src,  # who sent this packet (for HVAC reply inference)
            )

        # addr3: lowest-confidence (broadcast target or relay)
        if addr3 and _is_valid_address(addr3) and addr3 not in (src, dst):
            self._process_device(
                addr3,
                code=code,
                verb=verb,
                rssi=None,
                zone_idx=None,
                domain_id=None,
                is_src=False,
                dst=None,
            )

    def _process_device(
        self,
        dev_id: str,
        *,
        code: str,
        verb: str,
        rssi: float | None,
        zone_idx: str | None,
        domain_id: str | None = None,
        is_authoritative_domain: bool = False,
        is_src: bool,
        dst: str | None,
        src: str | None = None,
    ) -> None:
        """Update or create a discovery entry for a single device."""
        # 18: devices are HGI gateways — track them (so we know they're on
        # the network and can include them in the schema as HGI type), but
        # don't process zone bindings or heating topology for them.
        is_hgi = dev_id.startswith("18:")

        # For known HGI devices, create a minimal entry if not yet tracked,
        # or just update last_seen if already tracked.  Don't re-classify
        # or mark as dirty — the HGI is already known (in the known_list)
        # and should not trigger discovery notifications.
        if is_hgi and self._is_known(dev_id):
            now = dt.now().isoformat(timespec="seconds")
            dev = self._devices.get(dev_id)
            if dev is None:
                # First time seeing this known HGI — create a minimal entry
                # so it appears in scan results, but don't mark dirty (no
                # discovery notification needed for a known device).
                dev = DiscoveredDevice(
                    device_id=dev_id,
                    first_seen=now,
                    last_seen=now,
                    likely_type=DevType.HGI,
                    codes_seen=[code] if code else [],
                    rssi=rssi,
                    confidence="high",
                    is_battery=False,
                    src_count=1 if is_src else 0,
                    dst_count=0 if is_src else 1,
                )
                self._devices[dev_id] = dev
                self._dirty = True  # persist the new entry
                _LOGGER.debug(
                    "DiscoveryScan: tracking known HGI %s (not a new discovery)",
                    dev_id,
                )
            else:
                dev.last_seen = now
                if is_src:
                    dev.src_count += 1
                else:
                    dev.dst_count += 1
                if code and code not in dev.codes_seen:
                    dev.codes_seen.append(code)
                    dev.codes_seen.sort()
                    self._dirty = True  # persist updated codes
                if rssi is not None and is_src:
                    if dev.rssi is None:
                        dev.rssi = rssi
                    else:
                        dev.rssi = (dev.rssi + rssi) / 2
                    self._dirty = True  # persist updated RSSI
            return

        # For known devices, still update zone bindings (they may have been
        # accepted before the scan engine captured zone_idx from broadcast
        # traffic).  Skip full processing (classification, confidence, etc.)
        # since the device is already known.
        if self._is_known(dev_id) and not is_hgi:
            dev = self._devices.get(dev_id)
            if dev is None:
                # Known device not yet tracked in scan — create a minimal
                # entry so codes_seen is accumulated (needed for DHW valve
                # inference via 1100 code, etc.)
                now = dt.now().isoformat(timespec="seconds")
                likely_type = _classify(dev_id, code, verb, is_src=is_src)
                dev = DiscoveredDevice(
                    device_id=dev_id,
                    first_seen=now,
                    last_seen=now,
                    likely_type=likely_type,
                    codes_seen=[code] if code else [],
                    rssi=rssi,
                    confidence="high",
                    is_battery=code in _BATTERY_CODES,
                    src_count=1 if is_src else 0,
                    dst_count=0 if is_src else 1,
                    domain_id=domain_id if domain_id and is_src else None,
                )
                self._devices[dev_id] = dev
                self._dirty = True
                _LOGGER.debug(
                    "DiscoveryScan: tracking known device %s (not a new discovery)",
                    dev_id,
                )
            else:
                dev.last_seen = dt.now().isoformat(timespec="seconds")
                if is_src:
                    dev.src_count += 1
                else:
                    dev.dst_count += 1
                if code and code not in dev.codes_seen:
                    dev.codes_seen.append(code)
                    dev.codes_seen.sort()
                    self._dirty = True
                if rssi is not None and is_src:
                    if dev.rssi is None:
                        dev.rssi = rssi
                    else:
                        dev.rssi = (dev.rssi + rssi) / 2
                    self._dirty = True
                if zone_idx and is_src and not dev_id.startswith("01:"):
                    # Skip CTL (01:) — it sends 000A with zone config for
                    # multiple zones, not its own zone binding.  Setting
                    # zone_idx on the CTL corrupts its comment and schema
                    # entry (issue 813).
                    bound_changed = dev.zone_idx != zone_idx
                    dev.zone_idx = zone_idx
                    if dst and _is_valid_address(dst) and dst != dev_id:
                        if dev.bound_to != dst:
                            dev.bound_to = dst
                            bound_changed = True
                    if bound_changed:
                        dev.confidence = "high"
                        self._dirty = True
                        _LOGGER.debug(
                            "DiscoveryScan: updated zone binding for known "
                            "device %s (zone=%s, bound_to=%s)",
                            dev_id,
                            zone_idx,
                            dev.bound_to,
                        )
                # Domain_id update (issue 834): an authoritative 000C
                # binding overrides any previous hint; a 3B00/3EF0 hint
                # only applies if no domain_id is set yet.
                if (
                    domain_id
                    and is_src
                    and _should_update_domain_id(
                        dev.domain_id, domain_id, is_authoritative_domain
                    )
                ):
                    dev.domain_id = domain_id
                    dev.confidence = "high"
                    self._dirty = True
            return

        now = dt.now().isoformat(timespec="seconds")
        dev = self._devices.get(dev_id)

        if dev is None:
            # New device — classify and create entry
            likely_type = _classify(dev_id, code, verb, is_src=is_src)
            dev = DiscoveredDevice(
                device_id=dev_id,
                first_seen=now,
                last_seen=now,
                likely_type=likely_type,
                codes_seen=[code] if code else [],
                rssi=rssi,
                confidence=_initial_confidence(is_src, code, verb),
                is_battery=code in _BATTERY_CODES,
                src_count=1 if is_src else 0,
                dst_count=0 if is_src else 1,
            )
            # Set binding info if available
            # zone_idx is extracted from the payload and is valid even for
            # broadcasts (dst == --:------).  bound_to requires a valid dst.
            # Skip for HGI gateways — they don't have zone bindings.
            # Skip for CTL (01:) — it sends 000A with zone config for
            # multiple zones, not its own zone binding (issue 813).
            if zone_idx and is_src and not is_hgi and not dev_id.startswith("01:"):
                dev.zone_idx = zone_idx
                if dst and _is_valid_address(dst) and dst != dev_id:
                    dev.bound_to = dst
                dev.confidence = "high"  # binding telemetry = high confidence
            # Domain_id from 000C binding (authoritative) or 3B00/3EF0
            # (hint).  See issue 834: a 000C FA binding must override a
            # previous 3B00/3EF0 FC hint (the BDR is hotwater_valve, not
            # appliance_control).
            if (
                domain_id
                and is_src
                and not is_hgi
                and _should_update_domain_id(
                    dev.domain_id, domain_id, is_authoritative_domain
                )
            ):
                dev.domain_id = domain_id
                dev.confidence = "high"
            # HVAC topology inference: a FAN (32:) sending a directed packet
            # (I or RP) to this device confirms the binding — the FAN is the
            # controller and it's communicating with its paired remote.
            # Skip for HGI gateways — they don't have HVAC parent bindings.
            elif (
                not is_hgi
                and not is_src
                and src
                and _is_valid_address(src)
                and src.startswith("32:")
                and verb in (" I", "RP")
                and code in _HVAC_PARENT_INFERENCE_CODES
            ):
                dev.bound_to = src
            self._devices[dev_id] = dev
            self._dirty = True
            _LOGGER.info(
                "DiscoveryScan: new device %s (%s, %s)",
                dev_id,
                likely_type,
                dev.confidence,
            )
            return

        # Existing device — enrich
        changed = False

        dev.last_seen = now
        if is_src:
            dev.src_count += 1
        else:
            dev.dst_count += 1

        # Add code to codes_seen (deduplicated, keep sorted)
        if code and code not in dev.codes_seen:
            dev.codes_seen.append(code)
            dev.codes_seen.sort()
            changed = True

        # Update RSSI as running average (only from src packets)
        if rssi is not None and is_src:
            if dev.rssi is None:
                dev.rssi = rssi
            else:
                dev.rssi = (dev.rssi + rssi) / 2
            changed = True

        # Update battery flag
        if code in _BATTERY_CODES and not dev.is_battery:
            dev.is_battery = True
            changed = True

        # Update zone binding (prefer src packets with zone_idx)
        # zone_idx is extracted from the payload and is valid even for
        # broadcasts (dst == --:------).  bound_to requires a valid dst
        # that is different from the device itself.
        # Skip for HGI gateways — they don't have zone bindings.
        if zone_idx and is_src and not is_hgi:
            bound_changed = dev.zone_idx != zone_idx
            dev.zone_idx = zone_idx
            if dst and _is_valid_address(dst) and dst != dev_id:
                if dev.bound_to != dst:
                    dev.bound_to = dst
                    bound_changed = True
            if bound_changed:
                dev.confidence = "high"
                changed = True

        # Update domain_id (issue 834): an authoritative 000C binding
        # overrides a previous 3B00/3EF0 hint; a hint only applies
        # if no domain_id is set yet.
        if (
            domain_id
            and is_src
            and not is_hgi
            and _should_update_domain_id(
                dev.domain_id, domain_id, is_authoritative_domain
            )
        ):
            dev.domain_id = domain_id
            dev.confidence = "high"
            changed = True

        # HVAC topology inference: a FAN (32:) sending a directed packet
        # (I or RP) to this device confirms the binding — the FAN is the
        # controller and it's communicating with its paired remote.
        # Infer bound_to from the packet source if not already set.
        # Skip for HGI gateways — they don't have HVAC parent bindings.
        if (
            not is_hgi
            and not dev.bound_to
            and not is_src
            and src
            and _is_valid_address(src)
            and src.startswith("32:")
            and verb in (" I", "RP")
            and code in _HVAC_PARENT_INFERENCE_CODES
        ):
            dev.bound_to = src
            changed = True

        # Upgrade confidence based on accumulated evidence
        new_conf = _recompute_confidence(dev)
        if new_conf != dev.confidence:
            dev.confidence = new_conf
            changed = True

        # Re-classify if we have more info now
        new_type = _classify(dev_id, code, verb, is_src=is_src, dev=dev)
        if new_type != dev.likely_type and new_type != DevType.DEV:
            dev.likely_type = new_type
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
            result = [d for d in result if order.get(d.confidence, 0) >= min_val]

        return result

    def get_device(self, dev_id: str) -> DiscoveredDevice | None:
        """Return a single discovered device by ID, or None."""
        return self._devices.get(dev_id)

    def remove_device(self, dev_id: str) -> bool:
        """Remove a device from the in-memory list.

        Returns True if the device was present and removed.
        """
        if dev_id in self._devices:
            del self._devices[dev_id]
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
                for d in sorted(self._devices.values(), key=lambda d: d.device_id)
            ],
        }
        return json.dumps(data, indent=2, sort_keys=False)

    def import_json(self, data: str) -> None:
        """Load a previously exported list (for resume after restart).

        Replaces the current in-memory dict.
        """
        parsed = json.loads(data)
        self._devices = {
            d["device_id"]: DiscoveredDevice.from_dict(d)
            for d in parsed.get("devices", [])
        }
        self._dirty = False
        _LOGGER.info("DiscoveryScan: imported %d devices", len(self._devices))

    def device_count(self) -> int:
        """Return the number of discovered devices."""
        return len(self._devices)


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


def _is_valid_address(dev_id: str) -> bool:
    """Quick check if a device ID looks valid (N.N:NNNNNN or N:NNNNNN).

    Filters out broadcast addresses (18:73030, 18:14803, 18:000730),
    the null/broadcast device type 63: (NUL — e.g. 63:262142=0xFFFFFE,
    63:262143=0xFFFFFF, the latter also used by the HGI80 to disguise its
    own address), placeholder addresses (--:------), and corrupt IDs.
    """
    if not dev_id or len(dev_id) < 8:
        return False
    # Skip broadcast/multicast addresses
    if dev_id in ("18:73030", "18:14803", "18:000730"):
        return False
    # Skip the null/broadcast device type 63: (NUL) — no real device uses
    # type 63; both 63:262142 (0xFFFFFE) and 63:262143 (0xFFFFFF) are
    # sentinels, and the HGI80 emits 63:262143 as a self-disguise address
    if dev_id.startswith("63:"):
        return False
    # Skip placeholder/empty addresses (e.g. "--:------")
    if dev_id.startswith("-") or dev_id.startswith("00:------"):
        return False
    # Basic format check: should contain a colon
    return ":" in dev_id


def _classify(
    dev_id: str,
    code: str,
    verb: str,
    *,
    is_src: bool,
    dev: DiscoveredDevice | None = None,
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
    """
    prefix = dev_id[:2]

    # 1. Unambiguous HVAC prefixes (32:=FAN) — check first
    if prefix in _UNAMBIGUOUS_HVAC_PREFIXES:
        return _PREFIX_TO_TYPE[prefix]

    # 2. CTL-only codes (only if this device is the sender)
    #    Some codes are CTL-only depending on verb (e.g. 313F I=CTL, RQ=TRV)
    if is_src:
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

    # 4. Check accumulated codes if we have a dev
    if dev and is_src:
        for c in dev.codes_seen:
            if c in _CTL_ONLY_CODES:
                return DevType.CTL
        # Check verb-aware CTL codes from accumulated data
        for c in dev.codes_seen:
            if c in _CTL_ONLY_CODES_WITH_VERB:
                ctl_verbs = _CTL_ONLY_CODES_WITH_VERB[c]
                # Check if any verb in the accumulated data matches
                # We don't track per-code verbs, so be conservative:
                # only classify as CTL if the current verb matches
                if verb in ctl_verbs:
                    return DevType.CTL
        # Check HVAC codes from accumulated data — but only with the
        # current verb.  Trying all verbs (I, RP, RQ, W) caused
        # misclassification: a DIS sending RQ 31DA (requesting fan status
        # from a FAN) was classified as FAN because (I, 31DA) maps to FAN
        # in _VC_TO_TYPE.  The device never sent I 31DA — it only sent
        # RQ 31DA — but the loop tried all verbs regardless.
        #
        # By only checking the current verb, we ensure:
        # - RQ 31DA (DIS asking FAN for status) → not in _VC_TO_TYPE → skip
        # - I 31DA (FAN broadcasting status) → FAN
        # - RP 31DA (FAN responding to request) → FAN
        valid_types = _AMBIGUOUS_HVAC_PREFIX_TYPES.get(prefix)
        for c in dev.codes_seen:
            if (verb, c) in _VC_TO_TYPE:
                vc_type = _VC_TO_TYPE[(verb, c)]
                if valid_types is None or vc_type in valid_types:
                    return vc_type

    # 5. CH prefix fallback
    if prefix in _PREFIX_TO_TYPE:
        return _PREFIX_TO_TYPE[prefix]

    return DevType.DEV


def _initial_confidence(is_src: bool, code: str, verb: str) -> str:
    """Determine initial confidence for a newly seen device."""
    if is_src and code in _ZONE_BINDING_CODES:
        return "high"  # binding telemetry from src = high confidence
    if is_src:
        return "medium"  # device is actively sending
    return "low"  # only seen as dst/addr3


def _recompute_confidence(dev: DiscoveredDevice) -> str:
    """Recompute confidence based on accumulated evidence."""
    # High: has zone binding info (zone_idx, with or without bound_to)
    if dev.zone_idx:
        return "high"

    # High: sends CTL-only codes
    if any(c in _CTL_ONLY_CODES for c in dev.codes_seen):
        return "high"
    # High: sends verb-aware CTL-only codes (e.g. 313F I/RP)
    if any(c in _CTL_ONLY_CODES_WITH_VERB for c in dev.codes_seen):
        return "high"

    # Medium: seen as src multiple times
    if dev.src_count >= 2:
        return "medium"

    # Medium: seen as src at least once with known codes
    if dev.src_count >= 1 and len(dev.codes_seen) >= 2:
        return "medium"

    # Low: only seen as dst, or seen once as src
    return "low"


def _extract_zone_idx(payload: str) -> str | None:
    """Extract zone_idx from a payload string.

    Zone index is typically the first 2 hex chars of the payload.
    Returns None if payload is empty, too short, or not a valid zone index.

    Valid zone indices are 00-0B (12 zones max).  Values like FC (appliance
    control domain), 7F (broadcast), or other non-zone domain IDs are rejected.
    """
    if not payload or len(payload) < 2:
        return None
    idx = payload[:2].upper()
    # Validate: must be hex chars
    try:
        val = int(idx, 16)
    except ValueError:
        return None
    # Validate: zone indices are 00-0B (12 zones max)
    if val > 0x0B:
        return None
    return idx


def _should_update_domain_id(
    current: str | None,
    new: str | None,
    is_authoritative: bool,
) -> bool:
    """Decide whether to update a device's domain_id.

    An authoritative 000C binding always wins -- it overrides any
    previous value (including a 3B00/3EF0 FC hint that was set before
    the 000C binding arrived).  A non-authoritative 3B00/3EF0 hint
    only applies if the device has no domain_id yet (None).

    See issue 834 comment 5044906835: a BDR hotwater_valve broadcasting
    3B00/3EF0 gets domain_id=FC from the hint, but when the 000C HTG
    binding arrives (domain FA), it must override the hint.

    :param current: The device's current domain_id (may be None).
    :param new: The new domain_id from the current packet.
    :param is_authoritative: True if from a 000C binding, False if
        from 3B00/3EF0.
    :return: True if the domain_id should be updated.
    """
    if new is None:
        return False
    if is_authoritative:
        return current != new
    # Non-authoritative hint: only set if no domain_id exists yet
    return current is None


def _is_appliance_control_signal(
    dev_id: str, code: str, verb: str, is_src: bool
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

    See issue 834: without this signal, a BDR with no zone_idx is
    misclassified as a hotwater_valve by ramses_cc's
    generate_schema_entry.
    """
    if not is_src:
        return False
    if not dev_id.startswith(("13:", "10:")):
        return False
    if code not in _APPLIANCE_CONTROL_CODES:
        return False
    # 3B00/3EF0 are broadcast as I by the relay; RQ/RP are
    # directed replies (e.g. 3EF1 RP from the relay to the HGI)
    # and are not the TPI signature.
    return verb.strip() == "I"


def _extract_domain_id_from_000c(payload: str) -> str | None:
    """Extract the domain_id (FC/FA/F9) from a 000C binding payload.

    000C payloads encode the domain in the zone_type field
    (payload[2:4]):
      - "0F" (APP) -> FC (appliance_control)
      - "0E" (HTG) -> FA (hotwater_valve, index 00) or
        F9 (heating_valve, index 01)
      - "0D" (DHW) -> FA (dhw_sensor, index 00) or
        F9 (index 01)

    This is the authoritative source for domain_id -- the controller's
    binding table explicitly declares which domain each relay belongs
    to.  The 3B00/3EF0 TPI broadcast heuristic is ambiguous because
    both appliance_control and hotwater_valve relays send these codes.

    See issue 834 comment 5044906835: a BDR hotwater_valve broadcasting
    3B00/3EF0 was misclassified as appliance_control because the scan
    engine only used the TPI heuristic, not the 000C binding.

    :param payload: The raw hex payload string from a 000C packet.
    :return: "FC", "FA", "F9", or None if the payload is not a
        domain binding.
    """
    if not payload or len(payload) < 4:
        return None
    role = payload[2:4].upper()
    if role not in _000C_DOMAIN_ROLES:
        return None
    idx = payload[:2].upper()
    if role == _000C_ROLE_APP:
        return "FC"
    # HTG/DHW: index "00" → FA, index "01" → F9
    return "FA" if idx == "00" else "F9"
