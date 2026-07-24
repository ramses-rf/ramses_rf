#!/usr/bin/env python3
"""Test the schedule definitions for DHW entities on PollingManager."""

from ramses_rf.pipeline.polling import DEFAULT_POLLING_SCHEDULES


def test_dhw_battery_zero_polling() -> None:
    """Ensure DHW battery devices have polling explicitly disabled."""

    dhw_schedule = DEFAULT_POLLING_SCHEDULES.get("DHW", {})
    # Per system rules, battery devices (DHW) must set intervals to None or empty
    for interval in dhw_schedule.values():
        assert interval is None
