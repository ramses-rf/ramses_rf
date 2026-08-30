"""RAMSES RF - ClimaRad HVAC strategy."""

from __future__ import annotations

from typing import Any

from ramses_rf.const import SZ_REL_HUMIDITY
from ramses_rf.models import HvacState
from ramses_rf.models.hvac_schemas import _22F1_MODE_MAX, _22F1_MODE_VASCO
from ramses_rf.strategies.base import HvacStrategyBase
from ramses_tx.const import Code


class ClimaRadStrategy(HvacStrategyBase):
    """Strategy for ClimaRad ventilation systems."""

    scheme = "climarad"
    _mode_map = _22F1_MODE_VASCO
    _mode_max = _22F1_MODE_MAX[scheme]
    _binding_codes = (Code._22F1, Code._22F3)
    alias_language = "nl"
    _aliases = {
        k: v
        for k, v in HvacStrategyBase._DUTCH_ALIASES.items()
        if v in _22F1_MODE_VASCO.values()
    }

    def apply_quirk(
        self,
        payload: dict[str, Any],
        current_state: HvacState | None,
        msg_code: Code | str,
    ) -> dict[str, Any]:
        """Apply ClimaRad-specific payload transformations.

        :param payload: The flattened, canonical telemetry dictionary.
        :type payload: dict[str, Any]
        :param current_state: The existing Read-Model for the device.
        :type current_state: HvacState | None
        :param msg_code: The hex opcode of the incoming message.
        :type msg_code: Code | str
        :returns: The safely mutated telemetry dictionary.
        :rtype: dict[str, Any]
        """
        # STRUCTURAL QUIRK: 12A0 Array Elements (ClimaRad Ventura V1x)
        # The parser returns list elements with an 'hvac_index'. We must map
        # these generic keys to their specific domain locations.
        # index=00: indoor sensor  → indoor_humidity, indoor_temp
        # index=01: supply sensor  → rel_humidity (parser key), supply_temp
        # index=02: outdoor sensor → outdoor_humidity, outdoor_temp
        #
        # parse_humidity_element returns different key names per index:
        #   index=00 → SZ_INDOOR_HUMIDITY ("indoor_humidity")
        #   index=01 → SZ_REL_HUMIDITY ("rel_humidity") — NOT "indoor_humidity"!
        #   index=02 → SZ_OUTDOOR_HUMIDITY ("outdoor_humidity")
        #
        # HvacState has no supply_humidity field, so index=01's rel_humidity is
        # dropped (not in dispatcher field list). index=02's outdoor_humidity is
        # already correct and needs no remapping.
        if msg_code == Code._12A0:
            mutated = dict(payload)
            index = mutated.get("hvac_index", "00")
            if index == "00":
                if "temperature" in mutated:
                    mutated["indoor_temp"] = mutated["temperature"]
            elif index == "01":
                # parse_humidity_element returns "rel_humidity", not
                # "indoor_humidity" — the old code checked the wrong key.
                # There is no supply_humidity field in HvacState, so we pop
                # the key to prevent it overwriting indoor_humidity from
                # index=00 via the dispatcher's "temperature" fallback.
                if SZ_REL_HUMIDITY in mutated:
                    mutated.pop(SZ_REL_HUMIDITY)
                if "indoor_humidity" in mutated:
                    mutated.pop("indoor_humidity")
                if "temperature" in mutated:
                    mutated["supply_temp"] = mutated.pop("temperature")
            elif index == "02":
                # parse_humidity_element already returns outdoor_humidity
                # for index=02, so no humidity remapping is needed.
                #
                # NOTE: index=02 also includes a temperature field, but we do
                # NOT remap it to outdoor_temp. The 12A0 array comes from a
                # separate HUM sensor, and the dispatcher routes it to the
                # FAN's hvac_state (as dst target). If we remap temperature →
                # outdoor_temp here, it creates a second outdoor_temp source
                # that conflicts with 31DA's outdoor_temp, causing the sensor
                # to bounce between the two values every polling cycle. 31DA
                # is the authoritative source for outdoor_temp; 12A0 index=02
                # only contributes outdoor_humidity. See ramses_cc issue 742.
                pass
            return mutated

        return super().apply_quirk(payload, current_state, msg_code)
