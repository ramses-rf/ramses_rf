#!/usr/bin/env python3
"""Unittests for ramses_tx/command.py"""

import logging
from datetime import datetime as dt, timedelta as td
from typing import Final

from ramses_tx.command import Command
from ramses_tx.const import SYS_MODE_MAP, ZON_MODE_MAP
from ramses_tx.exceptions import CommandInvalid

#

_LOGGER = logging.getLogger(__name__)

#######################################################################################

# until an hour from now as "2025-08-02 14:00:00"
_UNTIL = (dt.now().replace(minute=0, second=0, microsecond=0) + td(hours=2)).strftime(
    "%Y-%m-%d %H:%M:%S"
)

TEST_COMMANDS: Final = [
    " W --- 18:000730 12:123456 --:------ 1F41 006 00FF00FFFFFF",  # . set_dhw_mode
    " W --- 18:000730 12:123456 --:------ 1F41 006 000002FFFFFF",  # . set_dhw_mode_perm, active-false
    " W --- 18:000730 12:123456 --:------ 2E04 008 00FFFFFFFFFFFF00",  # set_system_mode
    " W --- 18:000730 12:123456 --:------ 2349 007 027FFF00FFFFFF",  # set_zone_mode
    " W --- 18:000730 12:123456 --:------ 2349 007 0101F402FFFFFF",  # set_zone_mode_perm, setpoint
]


async def test_set_dhw_mode_follow() -> None:
    """Test parameter checks from ZON_MODE_MAP key"""
    cmd = Command.set_dhw_mode(ctl_id="12:123456", mode=ZON_MODE_MAP["follow_schedule"])
    #  cls,
    #  ctl_id: DeviceIdT | str,
    #  mode: int | str | None = None,
    #  active: bool | None = None,
    #  until: dt | str | None = None,
    #  duration: int | None = None,  # never passed on by ramses_cc

    assert str(cmd) == TEST_COMMANDS[0]


async def test_set_dhw_mode_follow_int() -> None:
    """Test parameter checks from int"""
    cmd = Command.set_dhw_mode(ctl_id="12:123456", mode=0)

    assert str(cmd) == TEST_COMMANDS[0]


async def test_set_dhw_mode_perm_false() -> None:
    """Test parameter checks mode 2, active false"""  # from Peter Nash
    cmd = Command.set_dhw_mode(
        ctl_id="12:123456", mode=ZON_MODE_MAP.PERMANENT, active=False
    )

    assert str(cmd) == TEST_COMMANDS[1]


async def test_set_dhw_mode_follow_extra() -> None:
    """Test parameter checks extra"""
    try:
        _ = Command.set_dhw_mode(
            ctl_id="12:123456", mode=ZON_MODE_MAP["follow_schedule"], duration=1
        )
    except CommandInvalid:
        pass
    else:
        assert False


async def test_set_dhw_mode_untilduration() -> None:
    """Test parameter checks extra"""
    try:
        _ = Command.set_dhw_mode(
            ctl_id="12:123456",
            mode="temporary_override",
            active=True,
            duration=3600,  # never passed on by ramses_cc
            until=_UNTIL,  # Invalid args: At least one of until or duration must be None
        )
    except CommandInvalid:
        pass
    else:
        assert False


async def test_set_system_mode_auto_none() -> None:
    """Test parameter checks from int"""
    cmd = Command.set_system_mode(ctl_id="12:123456", system_mode=None)

    assert str(cmd) == TEST_COMMANDS[2]


async def test_set_system_mode_auto() -> None:
    """Test parameter checks"""
    cmd = Command.set_system_mode(ctl_id="12:123456", system_mode=SYS_MODE_MAP["auto"])
    # cls,
    # ctl_id: DeviceIdT | str,
    # system_mode: int | str | None,
    # *,
    # until: dt | str | None = None,

    assert str(cmd) == TEST_COMMANDS[2]


async def test_set_system_mode_auto_int() -> None:
    """Test parameter checks from int"""
    cmd = Command.set_system_mode(ctl_id="12:123456", system_mode=0)

    assert str(cmd) == TEST_COMMANDS[2]


async def test_set_system_mode_heatoff() -> None:
    """Test parameter checks mode 1"""

    try:
        _ = Command.set_system_mode(
            ctl_id="12:123456",
            system_mode=SYS_MODE_MAP.HEAT_OFF,  # until should be None
            until="456789566",
        )
    except CommandInvalid:
        pass
    else:
        assert False


async def test_set_zone_mode_noargs() -> None:
    """Test parameter checks extra"""

    try:
        _ = Command.set_zone_mode(
            ctl_id="12:123456",
            zone_idx=4,
        )
    except CommandInvalid:
        pass
    else:
        assert False


async def test_set_zone_mode_follow() -> None:
    """Test parameter checks"""
    cmd = Command.set_zone_mode(
        ctl_id="12:123456", mode=ZON_MODE_MAP["follow_schedule"], zone_idx=2
    )
    #  cls,
    #  ctl_id: DeviceIdT | str,
    #  mode: int | str | None = None,
    #  zone_idx: _ZoneIdxT,
    #  *
    #  active: bool | None = None,
    #  until: dt | str | None = None,
    #  duration: int | None = None,

    assert str(cmd) == TEST_COMMANDS[3]


async def test_set_zone_mode_follow_extra() -> None:
    """Test parameter checks extra"""

    try:
        _ = Command.set_zone_mode(
            ctl_id="12:123456",
            zone_idx=1,
            mode=ZON_MODE_MAP["follow_schedule"],
            duration=1,  # never passed on by ramses_cc
        )
    except CommandInvalid:
        pass
    else:
        assert False


async def test_set_zone_mode_perm_setp() -> None:
    """Test parameter checks mode 2, active false"""  # from Peter Nash
    cmd = Command.set_zone_mode(
        ctl_id="12:123456", zone_idx=1, mode=ZON_MODE_MAP.PERMANENT, setpoint=5
    )

    assert str(cmd) == TEST_COMMANDS[4]
