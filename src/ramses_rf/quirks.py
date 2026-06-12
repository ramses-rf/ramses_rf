"""Hardware-specific quirks, overrides, and quarantine lists.

RAMSES RF - Protocol Quirks and Schema Exceptions.

This file documents hard-won reverse-engineering knowledge where real-world
Honeywell/Resideo hardware violates its own protocol schemas.
These rules were historically hard-coded into the L3 transport dispatcher.

KNOWN EXCEPTIONS:
1. Cross-Domain Routing:
   Devices matching `msg.src.type == msg.dst.type` where both are HEAT_DEVICES
   can sometimes legally communicate in the HVAC domain (e.g., 22F3 codes).

2. Controller Promotions:
   `DEV_TYPE_MAP.PROMOTABLE_SLUGS` is required because devices will occasionally
   transmit packets outside their standard verb schemas, requiring L7 to
   "promote" their device class dynamically.

3. Verb/Code Schema Violations:
   - CTL / RQ / 3EF1: Controllers are known to illegally request 3EF1.
   - BDR / RQ / 3EF0: BDR91 relays are known to illegally request 3EF0.
   - W_  / 0001: General exception to the rule where W_ is transmitted unexpectedly.

"""

from __future__ import annotations

from typing import Any, Final

from ramses_rf.const import DevType
from ramses_rf.models import HvacState
from ramses_tx.const import MsgId

# Map of device types to sets of OpenTherm MsgIds that are known to be unreliable
QUARANTINED_OT_MSG_IDS: Final[dict[str, set[MsgId]]] = {
    DevType.OTB: {MsgId._0E, MsgId._11},
}


def apply_hvac_quirks(
    payload: dict[str, Any], current_state: HvacState | None, msg_code: str
) -> dict[str, Any]:
    """Resolve stateful FSM conflicts and structural anomalies for HVAC packets.

    Stateful quirks cannot be resolved by stateless parsers. They must be
    intercepted by comparing the incoming packet payload to the existing
    CQRS state immediately prior to hydration.

    :param payload: The flattened, canonical telemetry dictionary.
    :type payload: dict[str, Any]
    :param current_state: The existing Read-Model for the device, if any.
    :type current_state: HvacState | None
    :param msg_code: The hex opcode of the incoming message.
    :type msg_code: str
    :return: The safely mutated telemetry dictionary.
    :rtype: dict[str, Any]
    """
    mutated = dict(payload)

    # STRUCTURAL QUIRK: Ventura 12A0 Array Elements
    # The parser returns list elements with an 'hvac_idx'. We must map these
    # generic keys to their specific domain locations.
    if msg_code == "12A0":
        idx = mutated.get("hvac_idx", "00")
        if idx == "00":
            if "temperature" in mutated:
                mutated["indoor_temp"] = mutated["temperature"]
        elif idx == "01":
            if "indoor_humidity" in mutated:
                mutated["outdoor_humidity"] = mutated.pop("indoor_humidity")
            if "temperature" in mutated:
                mutated["supply_temp"] = mutated.pop("temperature")
        return mutated

    if not current_state:
        return mutated

    # QUIRK: Itho 31DA 'exhaust_fan_speed' Overwrite Prevention
    # Itho transmits actual fan speed in 31D9, but transmits 31DA with
    # a default zero byte [38:40]. We drop the zero if valid state exists.
    if msg_code == "31DA" and "exhaust_fan_speed" in mutated:
        if mutated["exhaust_fan_speed"] == 0.0:
            if (
                current_state.exhaust_fan_speed is not None
                and current_state.exhaust_fan_speed > 0
            ):
                mutated["exhaust_fan_speed"] = current_state.exhaust_fan_speed

    # QUIRK: Vasco/ClimaRad 31D9 vs 31DA 'fan_info' precedence
    # Vasco passes string mode details in 31D9. Itho uses 31DA. We must not
    # overwrite a valid, rich string from 31D9 with a blank or 'off' string
    # from a generic 31DA.
    if msg_code == "31DA" and "fan_info" in mutated:
        if mutated["fan_info"] in ("off", ""):
            if current_state.fan_info and current_state.fan_info not in ("off", ""):
                mutated["fan_info"] = current_state.fan_info

    return mutated
