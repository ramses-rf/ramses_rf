"""RAMSES RF - Orcon HVAC strategy."""

from __future__ import annotations

from ramses_rf.models.hvac_schemas import _22F1_MODE_MAX, _22F1_MODE_ORCON
from ramses_rf.strategies.base import HvacStrategyBase
from ramses_tx.const import Code, IndexT


class OrconStrategy(HvacStrategyBase):
    """Strategy for Orcon ventilation systems."""

    scheme = "orcon"
    _mode_map = _22F1_MODE_ORCON
    _mode_max = _22F1_MODE_MAX[scheme]
    _binding_codes = (Code._22F1, Code._22F3)
    alias_language = "nl"
    _aliases = {
        k: v
        for k, v in HvacStrategyBase._DUTCH_ALIASES.items()
        if v in _22F1_MODE_ORCON.values()
    }

    def co2_binding_codes(
        self,
    ) -> tuple[Code | tuple[IndexT, Code], ...]:
        """Return the indexed binding offer used by Orcon CO2 sensors."""
        return (
            ("00", Code._31E0),
            ("01", Code._31E0),
            ("00", Code._1298),
        )

    def ventilation_demand_payload(self, value: float) -> str:
        """Encode the second-domain demand used by Orcon VMD units."""
        demand_raw = round(value * 100)
        return f"000000000100{demand_raw:02X}00"


class OrconHrc350Strategy(OrconStrategy):
    """Orcon capability profile for VMD-15RMS64/HRC-350 units."""

    def ventilation_demand_payload(self, value: float) -> str:
        """Encode the first-domain high-resolution HRC-350 demand."""
        demand_raw = round(value * 200)
        return f"0000{demand_raw:02X}0001000000"
