"""RAMSES RF - Itho HVAC strategy.

Vendor-specific behaviour for Itho Daalderop ventilation systems.
"""

from __future__ import annotations

from typing import Any

from ramses_rf.models import HvacState
from ramses_rf.models.hvac_schemas import _22F1_MODE_ITHO, _22F1_MODE_MAX
from ramses_rf.strategies.base import HvacStrategyBase
from ramses_tx.const import Code


class IthoStrategy(HvacStrategyBase):
    """Strategy for Itho Daalderop ventilation systems.

    Owns the Itho fan mode map and the 31DA exhaust_fan_speed
    overwrite prevention quirk.
    """

    scheme = "itho"
    _mode_map = _22F1_MODE_ITHO
    _mode_max = _22F1_MODE_MAX[scheme]
    _binding_codes = (Code._22F1, Code._22F3)
    alias_language = "nl"
    _aliases = {
        k: v
        for k, v in HvacStrategyBase._DUTCH_ALIASES.items()
        if v in _22F1_MODE_ITHO.values()
    }

    # 22F3 timed boost commands (3-byte Itho format).
    # Payload: 00 00 <mins>
    # Itho boost has no speed selection — it boosts to the
    # device's max speed for the given duration.
    _builtin_commands: dict[str, dict[str, str]] = {
        "boost_10": {
            "verb": "I",
            "code": Code._22F3,
            "payload": "00000A",
            "type": "boost_timer",
        },
        "boost_20": {
            "verb": "I",
            "code": Code._22F3,
            "payload": "000014",
            "type": "boost_timer",
        },
        "boost_30": {
            "verb": "I",
            "code": Code._22F3,
            "payload": "00001E",
            "type": "boost_timer",
        },
    }
    # "boost" is the same in Dutch — no aliases needed

    def apply_quirk(
        self,
        payload: dict[str, Any],
        current_state: HvacState | None,
        msg_code: Code | str,
    ) -> dict[str, Any]:
        """Apply Itho-specific quirks.

        Itho transmits actual fan speed in 31D9, but transmits 31DA
        with a default zero byte [38:40].  We drop the zero if valid
        state exists.

        :param payload: The flattened, canonical telemetry dictionary.
        :type payload: dict[str, Any]
        :param current_state: The existing Read-Model for the device.
        :type current_state: HvacState | None
        :param msg_code: The hex opcode of the incoming message.
        :type msg_code: Code | str
        :returns: The safely mutated telemetry dictionary.
        :rtype: dict[str, Any]
        """
        mutated = super().apply_quirk(payload, current_state, msg_code)

        # QUIRK: Itho 31DA 'exhaust_fan_speed' Overwrite Prevention
        # Itho transmits actual fan speed in 31D9, but transmits 31DA
        # with a default zero byte [38:40]. We drop the zero if valid
        # state exists.
        if (
            msg_code == Code._31DA
            and "exhaust_fan_speed" in mutated
            and current_state
        ):
            if mutated["exhaust_fan_speed"] == 0.0:
                if (
                    current_state.exhaust_fan_speed is not None
                    and current_state.exhaust_fan_speed > 0
                ):
                    mutated["exhaust_fan_speed"] = (
                        current_state.exhaust_fan_speed
                    )

        return mutated
