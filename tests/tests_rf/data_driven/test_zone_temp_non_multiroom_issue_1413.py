"""Reproduction and regression test for ramses_cc issue 1413.

DT4R thermostat shows jumping/erratic zone temperature values.

A zone with a bound thermostat (THM, ``22:``) whose controller marks the
zone locally-sensed (``multiroom_mode`` off in the 000A zone flags) takes
its temperature from the zone's actuators, echoed in the controller's
30C9 zone syncs.  The bound sensor's own 30C9 broadcast is a device-local
reading, not the zone temperature — accepting it makes the zone
temperature oscillate between the sensor and controller values.

Multiroom zones (``multiroom_mode`` on) keep the existing behaviour: the
bound sensor's broadcast hydrates the zone temperature, and the
controller echoes that same reading.

See: https://github.com/ramses-rf/ramses_cc/issues/1413
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .helpers import TEST_DIR, load_test_gwy

MULTIROOM_OFF_DIR = Path(f"{TEST_DIR}/systems/_heat_multiroom_off_sensor")
MULTIROOM_ON_DIR = Path(f"{TEST_DIR}/systems/_heat_multiroom_on_sensor")


@pytest.mark.asyncio
async def test_zone_temp_sensor_ignored_when_multiroom_off() -> None:
    """A locally-sensed (non-multiroom) zone must take its temperature
    only from controller syncs — the bound sensor's own 30C9 broadcast
    must not overwrite it (issue 1413).
    """
    # Arrange & Act
    gwy = await load_test_gwy(MULTIROOM_OFF_DIR)
    try:
        tcs = gwy.tcs
        assert tcs is not None, "no TCS loaded"
        zone = tcs.zone_by_index.get("02")
        assert zone is not None, "no zone 02 loaded"
        assert zone.zone_state.multiroom_mode is False
        assert zone.sensor is not None
        assert zone.sensor.id == "22:033088"

        # Assert: zone temperature is the controller-synced value (11.43),
        # not the bound sensor's own reading (12.30)
        temp = await zone.temperature()
        assert temp == 11.43, (
            f"Zone temperature clobbered by non-multiroom sensor: {temp!r}"
        )

        # Assert: the sensor device still records its own temperature
        assert await zone.sensor.temperature() == 12.30
    finally:
        await gwy.stop()


@pytest.mark.asyncio
async def test_zone_temp_sensor_still_writes_when_multiroom_on() -> None:
    """A multiroom zone keeps the sensor-routed behaviour: the bound
    sensor's own 30C9 broadcast updates the zone temperature (the
    controller echoes that same reading in its syncs).
    """
    # Arrange & Act
    gwy = await load_test_gwy(MULTIROOM_ON_DIR)
    try:
        tcs = gwy.tcs
        assert tcs is not None, "no TCS loaded"
        zone = tcs.zone_by_index.get("00")
        assert zone is not None, "no zone 00 loaded"
        assert zone.zone_state.multiroom_mode is True
        assert zone.sensor is not None
        assert zone.sensor.id == "22:017762"

        # Assert: the last sensor broadcast (12.30) wins over the
        # controller sync (12.40) — multiroom behaviour unchanged
        temp = await zone.temperature()
        assert temp == 12.30, (
            f"Zone temperature not updated by multiroom sensor: {temp!r}"
        )
    finally:
        await gwy.stop()
