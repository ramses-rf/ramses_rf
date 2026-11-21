"""Reproduction test for ramses_rf issue 1384 (zone discovery).

On a UFH-only system (Evo Color + HCC100 UFCs, no TRVs) the controller's
zone-indexed broadcasts (``2309`` setpoints, ``30C9`` temperatures,
``000A`` parameters) are the only evidence that zones exist.  The
CQRS topology pipeline only instantiated zones from ``0005``/``000C``
payloads, so with an empty zone schema nothing was ever discovered and
no per-zone poll tasks (``0004 zz00``/``000C zz04``/``2349``) were
created either.

The fix instantiates the zone when the TCS's own controller transmits a
zone-indexed payload.  Domain indices (``F9``/``FA``/``FC``), UFH
circuit indices (``E1``-``E8``) and non-controller sources must not
spawn zones.

Fixture replays the reporter's traffic: ``2309``/``30C9``/``000A``
arrays covering zones 00-05, a ``3150 FC`` domain packet, and two
``000C`` replies that bind UFC devices as zone sensors.

See: https://github.com/ramses-rf/ramses_rf/issues/1384
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from .helpers import TEST_DIR, find_test_tcs, load_test_gwy

if TYPE_CHECKING:
    from ramses_rf.gateway import Gateway
    from ramses_rf.systems.tcs import Evohome

DISCOVERY_DIR = Path(
    f"{TEST_DIR}/systems/_zone_discovery_ctl_broadcast_issue_1384"
)


@pytest.fixture
async def gwy() -> Gateway:
    return await load_test_gwy(DISCOVERY_DIR)


async def test_zones_created_from_ctl_broadcasts(gwy: Gateway) -> None:
    """Zones 00-05 are instantiated from the controller's own arrays."""
    tcs: Evohome = find_test_tcs(gwy)

    assert sorted(tcs.zone_by_index.keys()) == [
        "00",
        "01",
        "02",
        "03",
        "04",
        "05",
    ]


async def test_broadcast_state_populates_new_zones(gwy: Gateway) -> None:
    """The same broadcast that creates a zone also populates its state."""
    tcs: Evohome = find_test_tcs(gwy)

    assert await tcs.zone_by_index["00"].temperature() == pytest.approx(19.69)
    assert await tcs.zone_by_index["05"].temperature() == pytest.approx(19.30)
    assert await tcs.zone_by_index["03"].setpoint() == pytest.approx(20.0)


async def test_no_zone_created_for_domain_or_circuit_indices(
    gwy: Gateway,
) -> None:
    """Domain ids (FC) and UFC circuit ids (E1-E6) never become zones."""
    tcs: Evohome = find_test_tcs(gwy)

    for bad_index in ("FC", "FA", "F9", "E1", "E2", "HW"):
        assert bad_index not in tcs.zone_by_index
