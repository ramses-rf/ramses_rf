"""RAMSES RF - HVAC vendor strategies.

Vendor-specific behaviour for HVAC ventilation systems.  Each
strategy owns its vendor's fan mode maps, payload quirks, binding
codes, and classification heuristics.

See :doc:`roadmap item 5 <https://github.com/ramses-rf/ramses_rf/issues/1093>`.
"""

from __future__ import annotations

from ramses_rf.strategies.base import (
    HvacStrategy,
    HvacStrategyBase,
    VentilationControlStrategy,
)
from ramses_rf.strategies.climarad import ClimaRadStrategy
from ramses_rf.strategies.itho import IthoStrategy
from ramses_rf.strategies.nuaire import NuaireStrategy
from ramses_rf.strategies.orcon import OrconHrc350Strategy, OrconStrategy
from ramses_rf.strategies.vasco import VascoStrategy

__all__ = [
    "ClimaRadStrategy",
    "HvacStrategy",
    "HvacStrategyBase",
    "IthoStrategy",
    "NuaireStrategy",
    "OrconHrc350Strategy",
    "OrconStrategy",
    "VascoStrategy",
    "VentilationControlStrategy",
    "best_hvac_strategy",
]

#: Strategy classes indexed by scheme name
_STRATEGY_BY_SCHEME: dict[str, type[HvacStrategyBase]] = {
    "climarad": ClimaRadStrategy,
    "itho": IthoStrategy,
    "nuaire": NuaireStrategy,
    "orcon": OrconStrategy,
    "vasco": VascoStrategy,
}


def best_hvac_strategy(
    device_id: str,
    scheme: str | None = None,
    codes_seen: list[str] | None = None,
    model: str | None = None,
) -> HvacStrategy:
    """Select the best HVAC strategy for a device.

    Explicit schemes take precedence. Evidence-based selection using
    ``device_id`` and ``codes_seen`` will be added when validated traffic
    samples are available; the current fallback remains Orcon.

    :param device_id: The HVAC device ID.
    :type device_id: str
    :param scheme: Explicit vendor scheme from the device traits.
    :type scheme: str | None
    :param codes_seen: Protocol codes observed for the device.
    :type codes_seen: list[str] | None
    :param model: Device model reported by 10E0, if available.
    :type model: str | None
    :returns: The selected HVAC strategy.
    :rtype: HvacStrategy
    """
    if scheme == "orcon" and model and model.upper().startswith("VMD-15RMS64"):
        return OrconHrc350Strategy()

    strategy_cls = _STRATEGY_BY_SCHEME.get(scheme or "", OrconStrategy)
    return strategy_cls()
