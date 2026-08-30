"""RAMSES RF - Nuaire HVAC strategy.

Vendor-specific behaviour for Nuaire DRI-ECO ventilation systems.
"""

from __future__ import annotations

from ramses_rf.models.hvac_schemas import _22F1_MODE_MAX, _22F1_MODE_NUAIRE
from ramses_rf.strategies.base import HvacStrategyBase
from ramses_tx.const import Code


class NuaireStrategy(HvacStrategyBase):
    """Strategy for Nuaire DRI-ECO ventilation systems.

    Nuaire uses a single binding code (22F1 only, no 22F3).
    """

    scheme = "nuaire"
    _mode_map = _22F1_MODE_NUAIRE
    _mode_max = _22F1_MODE_MAX[scheme]
    # Nuaire binds with 22F1 only (no 22F3)
    _binding_codes = (Code._22F1,)
    alias_language = "nl"
    _aliases = {
        k: v
        for k, v in HvacStrategyBase._DUTCH_ALIASES.items()
        if v in _22F1_MODE_NUAIRE.values()
    }
