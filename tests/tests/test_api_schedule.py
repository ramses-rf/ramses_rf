#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Test the Schedule functions.
"""

import json
from copy import deepcopy
from pathlib import Path, PurePath

from ramses_rf import Gateway
from ramses_rf.const import SZ_SCHEDULE, SZ_ZONE_IDX
from ramses_rf.system.schedule import (
    SCH_SCHEDULE_DHW_FULL,
    SCH_SCHEDULE_ZON_FULL,
    SZ_ENABLED,
    SZ_HEAT_SETPOINT,
    SZ_SWITCHPOINTS,
    fragz_to_full_sched,
    full_sched_to_fragz,
)
from tests.helpers import TEST_DIR, load_test_gwy

WORK_DIR = f"{TEST_DIR}/schedules"


def id_fnc(param):
    return PurePath(param).name


def pytest_generate_tests(metafunc):
    folders = [f for f in Path(WORK_DIR).iterdir() if f.is_dir() and f.name[:1] != "_"]
    metafunc.parametrize("dir_name", folders, ids=id_fnc)


async def test_schedule_get(dir_name):
    """Compare the schedule built from a log file with the expected results."""

    with open(f"{dir_name}/schedule.json") as f:
        schedule = json.load(f)

    gwy: Gateway = await load_test_gwy(dir_name)  # noqa: F811

    zone = gwy.tcs.dhw if gwy.tcs.dhw else gwy.tcs.zones[0]
    assert zone.schedule == schedule[SZ_SCHEDULE]
    assert zone._schedule._full_schedule == schedule

    await gwy.stop()


async def test_schedule_helpers(dir_name):
    """Compare the schedule helpers are consistent and have symmetry."""

    with open(f"{dir_name}/schedule.json") as f:
        schedule = json.load(f)

    new_schedule = deepcopy(schedule)

    if schedule[SZ_ZONE_IDX] == "HW":
        SCH_SCHEDULE_DHW_FULL(schedule)
        schedule[SZ_ZONE_IDX] = "00"
    else:
        SCH_SCHEDULE_ZON_FULL(schedule)

    assert schedule == fragz_to_full_sched(full_sched_to_fragz(schedule))

    if new_schedule[SZ_ZONE_IDX] == "HW":
        new_schedule[SZ_ZONE_IDX] = "00"
        new_schedule[SZ_SCHEDULE][-1][SZ_SWITCHPOINTS][-1][SZ_ENABLED] = not (
            schedule[SZ_SCHEDULE][-1][SZ_SWITCHPOINTS][-1][SZ_ENABLED]
        )
    else:
        new_schedule[SZ_SCHEDULE][-1][SZ_SWITCHPOINTS][-1][SZ_HEAT_SETPOINT] = (
            schedule[SZ_SCHEDULE][-1][SZ_SWITCHPOINTS][-1][SZ_HEAT_SETPOINT] + 1
        )

    # the schedule code relies upon the following inequality...
    # i.e. if the schedule has changed, then the first fragment will be different
    assert full_sched_to_fragz(new_schedule)[0] != (full_sched_to_fragz(schedule)[0])
