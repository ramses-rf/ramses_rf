#!/usr/bin/env python3
"""Unittests for ramses_tx/command.py"""

import logging
from datetime import datetime as dt, timedelta as td
from typing import Final

import pytest

from ramses_rf.address import HGI_DEV_ADDR, Address
from ramses_rf.commands.builders import build_dto
from ramses_rf.commands.core import Command as Intent
from ramses_rf.enums import Action
from ramses_tx.command import Command
from ramses_tx.command_legacy_shim import LegacyCommandShim
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
    # Arrange
    expected = TEST_COMMANDS[0]

    # Act
    cmd = Command.set_dhw_mode(ctl_id="12:123456", mode=ZON_MODE_MAP["follow_schedule"])
    #  cls,
    #  ctl_id: DeviceIdT | str,
    #  mode: int | str | None = None,
    #  active: bool | None = None,
    #  until: dt | str | None = None,
    #  duration: int | None = None,  # never passed on by ramses_cc

    # Assert
    assert str(cmd) == expected


async def test_set_dhw_mode_follow_int() -> None:
    """Test parameter checks from int"""
    # Arrange
    expected = TEST_COMMANDS[0]

    # Act
    cmd = Command.set_dhw_mode(ctl_id="12:123456", mode=0)

    # Assert
    assert str(cmd) == expected


async def test_set_dhw_mode_perm_false() -> None:
    """Test parameter checks mode 2, active false"""  # from Peter Nash
    # Arrange
    expected = TEST_COMMANDS[1]

    # Act
    cmd = Command.set_dhw_mode(
        ctl_id="12:123456", mode=ZON_MODE_MAP.PERMANENT, active=False
    )

    # Assert
    assert str(cmd) == expected


async def test_set_dhw_mode_follow_extra() -> None:
    """Test parameter checks extra"""
    # Arrange
    mode = ZON_MODE_MAP["follow_schedule"]

    # Act & Assert
    with pytest.raises(CommandInvalid):
        _ = Command.set_dhw_mode(ctl_id="12:123456", mode=mode, duration=1)


async def test_set_dhw_mode_untilduration() -> None:
    """Test parameter checks extra"""
    # Arrange
    mode = "temporary_override"

    # Act & Assert
    with pytest.raises(CommandInvalid):
        _ = Command.set_dhw_mode(
            ctl_id="12:123456",
            mode=mode,
            active=True,
            duration=3600,  # never passed on by ramses_cc
            until=_UNTIL,  # Invalid args: At least one of until or duration must be None
        )


async def test_set_system_mode_auto_none() -> None:
    """Test parameter checks from int"""
    # Arrange
    expected = TEST_COMMANDS[2]

    # Act
    cmd = Command.set_system_mode(ctl_id="12:123456", system_mode=None)

    # Assert
    assert str(cmd) == expected


async def test_set_system_mode_auto() -> None:
    """Test parameter checks"""
    # Arrange
    expected = TEST_COMMANDS[2]

    # Act
    cmd = Command.set_system_mode(ctl_id="12:123456", system_mode=SYS_MODE_MAP["auto"])
    # cls,
    # ctl_id: DeviceIdT | str,
    # system_mode: int | str | None,
    # *,
    # until: dt | str | None = None,

    # Assert
    assert str(cmd) == expected


async def test_set_system_mode_auto_int() -> None:
    """Test parameter checks from int"""
    # Arrange
    expected = TEST_COMMANDS[2]

    # Act
    cmd = Command.set_system_mode(ctl_id="12:123456", system_mode=0)

    # Assert
    assert str(cmd) == expected


async def test_set_system_mode_heatoff() -> None:
    """Test parameter checks mode 1"""
    # Arrange
    system_mode = SYS_MODE_MAP.HEAT_OFF

    # Act & Assert
    with pytest.raises(CommandInvalid):
        _ = Command.set_system_mode(
            ctl_id="12:123456",
            system_mode=system_mode,  # until should be None
            until="456789566",
        )


async def test_set_zone_mode_noargs() -> None:
    """Test parameter checks extra"""
    # Arrange
    ctl_id = "12:123456"

    # Act & Assert
    with pytest.raises(CommandInvalid):
        _ = LegacyCommandShim.from_dto(
            build_dto(
                Intent(
                    src=HGI_DEV_ADDR,
                    dst=Address(ctl_id),
                    action=Action.SET_MODE,
                    data={"zone_idx": 4},
                )
            )
        )


async def test_set_zone_mode_follow() -> None:
    """Test parameter checks"""
    # Arrange
    expected = TEST_COMMANDS[3]

    # Act
    cmd = LegacyCommandShim.from_dto(
        build_dto(
            Intent(
                src=HGI_DEV_ADDR,
                dst=Address("12:123456"),
                action=Action.SET_MODE,
                data={"zone_idx": 2, "mode": ZON_MODE_MAP["follow_schedule"]},
            )
        )
    )
    #  cls,
    #  ctl_id: DeviceIdT | str,
    #  mode: int | str | None = None,
    #  zone_idx: _ZoneIdxT,
    #  *
    #  active: bool | None = None,
    #  until: dt | str | None = None,
    #  duration: int | None = None,

    # Assert
    assert str(cmd) == expected


async def test_set_zone_mode_follow_extra() -> None:
    """Test parameter checks extra"""
    # Arrange
    mode = ZON_MODE_MAP["follow_schedule"]

    # Act & Assert
    with pytest.raises(CommandInvalid):
        _ = LegacyCommandShim.from_dto(
            build_dto(
                Intent(
                    src=HGI_DEV_ADDR,
                    dst=Address("12:123456"),
                    action=Action.SET_MODE,
                    data={"zone_idx": 1, "mode": mode, "duration": 1},
                )
            )
        )


async def test_set_zone_mode_perm_setp() -> None:
    """Test parameter checks mode 2, active false"""  # from Peter Nash
    # Arrange
    expected = TEST_COMMANDS[4]

    # Act
    cmd = LegacyCommandShim.from_dto(
        build_dto(
            Intent(
                src=HGI_DEV_ADDR,
                dst=Address("12:123456"),
                action=Action.SET_MODE,
                data={"zone_idx": 1, "mode": ZON_MODE_MAP.PERMANENT, "setpoint": 5},
            )
        )
    )

    # Assert
    assert str(cmd) == expected


async def test_clone_with_source() -> None:
    """Test that clone_with_source creates an identical command with a new source."""
    # Arrange
    original_cmd = Command("RQ --- 18:000730 01:145038 --:------ 000A 002 0800")
    new_source = "18:123456"
    assert original_cmd.src.id == "18:000730"

    # Act
    cloned_cmd = original_cmd.clone_with_source(new_source)

    # Assert
    # Assert cloned command is properly mutated
    assert cloned_cmd is not original_cmd
    assert cloned_cmd.src.id == "18:123456"
    assert cloned_cmd.dst.id == "01:145038"
    assert cloned_cmd.verb == "RQ"
    assert cloned_cmd.code == "000A"
    assert cloned_cmd.payload == "0800"
    assert str(cloned_cmd) == "RQ --- 18:123456 01:145038 --:------ 000A 002 0800"

    # Enforce strict immutability: the original command MUST NOT have changed
    assert original_cmd.src.id == "18:000730"


async def test_rq_missing_target() -> None:
    """Test parameter checks for RQ missing target."""
    # Arrange & Act & Assert
    with pytest.raises(CommandInvalid):
        _ = Command._from_attrs(
            verb="RQ",
            code="0016",
            payload="00",
            addr0="--:------",
            addr1="--:------",
        )
