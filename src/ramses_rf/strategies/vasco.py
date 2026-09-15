"""RAMSES RF - Vasco HVAC strategy.

Vendor-specific behaviour for Vasco D60 and ClimaRad Minibox
ventilation systems.
"""

from __future__ import annotations

from ramses_rf.models.hvac_schemas import _22F1_MODE_MAX, _22F1_MODE_VASCO
from ramses_rf.strategies.base import HvacStrategyBase
from ramses_tx.const import Code

# 22F3 timed boost commands (7-byte Vasco/ClimaRad format).
# Payload: 00 02 <mins> <speed> 06 00 00
# Speed: 02=low, 03=medium, 04=high (Vasco mode map)
# Durations: 0F=15, 1E=30, 3C=60 minutes
_VASCO_BOOST_COMMANDS: dict[str, dict[str, str]] = {
    "low_15": {
        "verb": "I",
        "code": Code._22F3,
        "payload": "00020F02060000",
        "type": "boost_timer",
    },
    "low_30": {
        "verb": "I",
        "code": Code._22F3,
        "payload": "00021E02060000",
        "type": "boost_timer",
    },
    "low_60": {
        "verb": "I",
        "code": Code._22F3,
        "payload": "00023C02060000",
        "type": "boost_timer",
    },
    "medium_15": {
        "verb": "I",
        "code": Code._22F3,
        "payload": "00020F03060000",
        "type": "boost_timer",
    },
    "medium_30": {
        "verb": "I",
        "code": Code._22F3,
        "payload": "00021E03060000",
        "type": "boost_timer",
    },
    "medium_60": {
        "verb": "I",
        "code": Code._22F3,
        "payload": "00023C03060000",
        "type": "boost_timer",
    },
    "high_15": {
        "verb": "I",
        "code": Code._22F3,
        "payload": "00020F04060000",
        "type": "boost_timer",
    },
    "high_30": {
        "verb": "I",
        "code": Code._22F3,
        "payload": "00021E04060000",
        "type": "boost_timer",
    },
    "high_60": {
        "verb": "I",
        "code": Code._22F3,
        "payload": "00023C04060000",
        "type": "boost_timer",
    },
}


class VascoStrategy(HvacStrategyBase):
    """Strategy for Vasco D60 and ClimaRad Minibox remotes."""

    scheme = "vasco"
    _mode_map = _22F1_MODE_VASCO
    _mode_max = _22F1_MODE_MAX[scheme]
    _binding_codes = (Code._22F1, Code._22F3)
    alias_language = "nl"
    _aliases = {
        k: v
        for k, v in HvacStrategyBase._DUTCH_ALIASES.items()
        if v in _22F1_MODE_VASCO.values()
    }
    _builtin_commands: dict[str, dict[str, str]] = dict(_VASCO_BOOST_COMMANDS)
    _boost_aliases = {
        k: v
        for k, v in HvacStrategyBase._DUTCH_BOOST_ALIASES.items()
        if v in _VASCO_BOOST_COMMANDS
    }
