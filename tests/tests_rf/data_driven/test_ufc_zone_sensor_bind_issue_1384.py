"""Reproduction test for ramses_rf issue 1384 (UFC zone-sensor bind).

On UFH systems the controller's ``000C`` binding table declares the UFC
as each UFH zone's sensor (role ``04``, sub-index = the circuit, e.g.
``0404E10957E5`` = zone 04, role 04, circuit E1, ``02:088037``).  The
zone-binding emit was meant to skip controller-class children, but the
prefix filter compared against ``DevType`` slugs (``"UFC:"``) instead of
device-id prefixes (``"02:"``) and never matched — so the UFC was bound
as the zone's sensor, which ``PARENT_RULES`` rejects (UFCs bind to zones
via their circuits, never as zone devices): LINK/RULES EXCEPTIONs on
every poll cycle.

The fix matches real device-id prefixes: ``02:``/``18:``/``63:``
children are skipped, ``01:`` (a controller can legitimately be a zone
sensor) is allowed.

Fixture replays the reporter's ``000C`` replies binding both HCC100s as
sensors of zones 00 and 04.

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

BIND_DIR = Path(f"{TEST_DIR}/systems/_ufc_000c_zone_sensor_issue_1384")


@pytest.fixture
async def gwy() -> Gateway:
    return await load_test_gwy(BIND_DIR)


async def test_ufc_sensor_bind_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 000C UFC sensor bind must not spam RULES/LINK EXCEPTIONs."""
    # The replay must run in the call phase: pytest clears caplog records
    # between fixture setup and test body.
    await load_test_gwy(BIND_DIR)

    errors = [
        r.message
        for r in caplog.records
        if "RULES EXCEPTION" in r.message or "LINK EXCEPTION" in r.message
    ]
    assert errors == []


async def test_ufc_is_not_the_zone_sensor(gwy: Gateway) -> None:
    """Zones stay sensor-less; the UFC is not bound as a zone device."""
    tcs: Evohome = find_test_tcs(gwy)

    for zone in tcs.zones:
        sensor = zone.sensor
        assert sensor is None or not str(sensor.id).startswith("02:")
