"""RAMSES RF - Vasco HVAC strategy.

Vendor-specific behaviour for Vasco D60 and ClimaRad Minibox
ventilation systems.
"""

from __future__ import annotations

from ramses_rf.models.hvac_schemas import _22F1_MODE_MAX, _22F1_MODE_VASCO
from ramses_rf.strategies.base import HvacStrategyBase
from ramses_tx.const import Code


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
