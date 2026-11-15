"""Reproduction test for ramses_rf issue 1384 (UFC 30C9 broadcasts).

HCC100 UFH controllers broadcast ``I 30C9`` arrays carrying a
temperature per circuit (index ``E1``-``E8``).  The UFC entry in
``CODES_BY_DEV_SLUG`` did not list ``30C9``, so every broadcast was
dropped during validation as ``Unexpected code for src (UFC) to Tx``.

The fix allows ``I 30C9`` from UFC devices; the parser already supports
indexed temperature arrays, so the per-circuit temperatures land on the
controller's entity state.

Fixture replays the reporter's traffic: ``I 30C9`` broadcasts from two
HCC100s plus a ``3150`` per-circuit demand broadcast for contrast.

See: https://github.com/ramses-rf/ramses_rf/issues/1384
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from ramses_tx.const import Code

from .helpers import TEST_DIR, load_test_gwy

if TYPE_CHECKING:
    from ramses_rf.gateway import Gateway

UFC_DIR = Path(f"{TEST_DIR}/systems/_ufc_30c9_circuit_temps_issue_1384")


@pytest.fixture
async def gwy() -> Gateway:
    return await load_test_gwy(UFC_DIR)


async def test_ufc_30c9_broadcasts_are_accepted(gwy: Gateway) -> None:
    """Both UFCs expose their per-circuit temperature arrays."""
    ufc_017 = gwy.device_registry.get_device("02:088017")
    ufc_037 = gwy.device_registry.get_device("02:088037")

    temps_017 = await ufc_017.entity_state.get_value(Code._30C9)
    temps_037 = await ufc_037.entity_state.get_value(Code._30C9)

    assert temps_017 is not None and temps_037 is not None
    assert temps_017[0] == {"zone_index": "E1", "temperature": 19.61}
    assert temps_017[1] == {"zone_index": "E2", "temperature": 19.40}
    assert temps_037[0] == {"zone_index": "E1", "temperature": 19.69}
    assert temps_037[1] == {"zone_index": "E2", "temperature": 17.87}


async def test_ufc_3150_demand_broadcasts_still_land(gwy: Gateway) -> None:
    """The already-accepted 3150 per-circuit demand array is unchanged."""
    ufc_017 = gwy.device_registry.get_device("02:088017")

    # The 3150 overload declares an aggregate heat-demand float, but a UFC
    # stores the per-circuit array (E1-E4 demand entries).
    demands = cast(
        "list[dict[str, Any]]",
        await ufc_017.entity_state.get_value(Code._3150),
    )

    assert demands is not None and len(demands) == 4
    assert demands[0]["heat_demand"] == pytest.approx(0.03)
