#!/usr/bin/env python3
"""RAMSES RF - discovery scripts."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final, cast

from ramses_rf import exceptions as exc
from ramses_rf.address import HGI_DEV_ADDR, Address
from ramses_rf.commands.builders import build_dto
from ramses_rf.commands.core import Command as Intent
from ramses_rf.const import SZ_SCHEDULE, SZ_ZONE_IDX
from ramses_rf.devices import Fakeable
from ramses_rf.enums import Action
from ramses_rf.protocol.opentherm import OTB_DATA_IDS
from ramses_rf.protocol_schema import CODES_SCHEMA
from ramses_tx import Command, DeviceIdT, Priority
from ramses_tx.address import NON_DEV_ADDR
from ramses_tx.command_legacy_shim import LegacyCommandShim
from ramses_tx.dtos import CommandDTO

from ramses_rf.const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

if TYPE_CHECKING:
    from ramses_rf import Gateway
    from ramses_rf.devices import Controller
    from ramses_tx import IndexT


EXEC_CMD: Final = "exec_cmd"
GET_FAULTS: Final = "get_faults"
GET_SCHED: Final = "get_schedule"
SET_SCHED: Final = "set_schedule"

EXEC_SCR: Final = "exec_scr"
SCAN_DISC: Final = "scan_disc"
SCAN_FULL: Final = "scan_full"
SCAN_HARD: Final = "scan_hard"
SCAN_XXXX: Final = "scan_xxxx"

_LOGGER = logging.getLogger(__name__)


def script_decorator(fnc: Callable[..., Any]) -> Callable[..., Any]:
    """Decorate a script to broadcast 'Script begins:' and 'Script done.' messages.

    :param fnc: The asynchronous script function to decorate.
    :return: The wrapped asynchronous function.
    """

    @functools.wraps(fnc)
    async def wrapper(gwy: Gateway, *args: Any, **kwargs: Any) -> None:
        cmd = LegacyCommandShim.from_dto(
            build_dto(
                Intent(
                    src=HGI_DEV_ADDR,
                    dst=HGI_DEV_ADDR,
                    action=Action.SEND_PUZZLE,
                    data={"message": "Script begins:"},
                )
            )
        )
        gwy.send_cmd(cmd, priority=Priority.HIGHEST, num_repeats=3)

        await fnc(gwy, *args, **kwargs)

        cmd2 = LegacyCommandShim.from_dto(
            build_dto(
                Intent(
                    src=HGI_DEV_ADDR,
                    dst=HGI_DEV_ADDR,
                    action=Action.SEND_PUZZLE,
                    data={"message": "Script done."},
                )
            )
        )
        gwy.send_cmd(cmd2, priority=Priority.LOWEST, num_repeats=3)

    return wrapper


def spawn_scripts(gwy: Gateway, **kwargs: Any) -> list[asyncio.Task[None]]:
    """Spawn discovery or execution tasks based on provided CLI keyword arguments.

    :param gwy: The main gateway instance handling transport and device indexing.
    :param kwargs: CLI configuration dictionary containing execution flags.
    :return: A list of the generated asyncio tasks running the specified scripts.
    """
    tasks: list[asyncio.Task[None]] = []

    if kwargs.get(EXEC_CMD):
        tasks.append(asyncio.create_task(exec_cmd(gwy, **kwargs)))

    if kwargs.get(GET_FAULTS):
        tasks.append(asyncio.create_task(get_faults(gwy, kwargs[GET_FAULTS])))

    elif kwargs.get(GET_SCHED) and kwargs[GET_SCHED][0]:
        tasks.append(asyncio.create_task(get_schedule(gwy, *kwargs[GET_SCHED])))

    elif kwargs.get(SET_SCHED) and kwargs[SET_SCHED][0]:
        tasks.append(asyncio.create_task(set_schedule(gwy, *kwargs[SET_SCHED])))

    elif kwargs.get(EXEC_SCR):
        script = SCRIPTS.get(f"{kwargs[EXEC_SCR][0]}")
        if script is None:
            _LOGGER.warning(f"Script: {kwargs[EXEC_SCR][0]}() - unknown script")
        else:
            _LOGGER.info(f"Script: {kwargs[EXEC_SCR][0]}().- starts...")
            # script_poll_device returns a list of tasks, others return a coroutine
            result = script(gwy, kwargs[EXEC_SCR][1])
            if isinstance(result, list):
                tasks.extend(result)
            else:
                tasks.append(asyncio.create_task(result))

    gwy._engine._tasks.extend(tasks)
    return tasks


async def exec_cmd(gwy: Gateway, **kwargs: Any) -> None:
    """Execute a single raw command string from the CLI arguments.

    :param gwy: The gateway instance.
    :param kwargs: CLI parameters containing the 'EXEC_CMD' string.
    """
    cmd = Command.from_cli(kwargs[EXEC_CMD])
    await gwy.async_send_cmd(cmd, priority=Priority.HIGH, wait_for_reply=True)


async def get_faults(
    gwy: Gateway, ctl_id: DeviceIdT, start: int = 0, limit: int = 0x3F
) -> None:
    """Retrieve the fault log from a target controller.

    :param gwy: The gateway instance.
    :param ctl_id: The device ID of the controller to query.
    :param start: The index to start querying from.
    :param limit: The maximum number of fault entries to return.
    """
    ctl = cast("Controller", gwy.device_registry.get_device(ctl_id))

    try:
        if ctl.tcs:
            await ctl.tcs.get_faultlog(start=start, limit=limit)  # 0418
    except exc.ExpiredCallbackError as err:
        _LOGGER.error("get_faults(): Function timed out: %s", err)


async def get_schedule(gwy: Gateway, ctl_id: DeviceIdT, zone_idx: str) -> None:
    """Retrieve the zone schedule for a specific zone under a controller.

    :param gwy: The gateway instance.
    :param ctl_id: The device ID of the controller.
    :param zone_idx: The zone index string (e.g. "00" or "HW").
    """
    ctl = cast("Controller", gwy.device_registry.get_device(ctl_id))
    if not ctl.tcs:
        _LOGGER.error("get_schedule(): Controller has no TCS active.")
        return

    zone = ctl.tcs.get_htg_zone(zone_idx)

    try:
        await zone.get_schedule()
    except exc.ExpiredCallbackError as err:
        _LOGGER.error("get_schedule(): Function timed out: %s", err)


async def set_schedule(gwy: Gateway, ctl_id: DeviceIdT, schedule: str) -> None:
    """Set the zone schedule for a specific zone under a controller via JSON payload.

    :param gwy: The gateway instance.
    :param ctl_id: The device ID of the controller.
    :param schedule: A JSON string describing the full schedule dictionary.
    """
    schedule_ = json.loads(schedule)
    zone_idx = schedule_[SZ_ZONE_IDX]

    ctl = cast("Controller", gwy.device_registry.get_device(ctl_id))
    if not ctl.tcs:
        _LOGGER.error("set_schedule(): Controller has no TCS active.")
        return

    zone = ctl.tcs.get_htg_zone(zone_idx)

    try:
        await zone.set_schedule(schedule_[SZ_SCHEDULE])  # 0404
    except exc.ExpiredCallbackError as err:
        _LOGGER.error("set_schedule(): Function timed out: %s", err)


async def script_bind_req(
    gwy: Gateway, dev_id: DeviceIdT, code: Code = Code._2309
) -> None:
    """Make the targeted device artificially enter a supplicant bind phase.

    :param gwy: The gateway instance.
    :param dev_id: The device ID to transition to binding state.
    :param code: The code to offer during the bind request.
    """
    dev = gwy.device_registry.get_device(dev_id)
    assert isinstance(dev, Fakeable)  # mypy
    dev._make_fake()
    await dev._initiate_binding_process([code])


async def script_bind_wait(
    gwy: Gateway, dev_id: DeviceIdT, code: Code = Code._2309, idx: IndexT = "00"
) -> None:
    """Make the targeted device artificially enter a respondent bind phase.

    :param gwy: The gateway instance.
    :param dev_id: The device ID to transition to binding state.
    :param code: The expected bind code to accept.
    :param idx: The internal domain or zone index to map to.
    """
    dev = gwy.device_registry.get_device(dev_id)
    assert isinstance(dev, Fakeable)  # mypy
    dev._make_fake()
    await dev._wait_for_binding_request([code], idx=idx)


def script_poll_device(gwy: Gateway, dev_id: DeviceIdT) -> list[asyncio.Task[None]]:
    """Generate tasks to periodically poll a device for vital status metrics.

    :param gwy: The gateway instance.
    :param dev_id: The targeted device ID.
    :return: A list containing tasks executing the periodic polling.
    """

    async def periodic_send(
        gwy: Gateway,
        cmd: Command,
        count: int = 1,
        interval: float | None = None,
    ) -> None:
        async def periodic_(interval_: float) -> None:
            await asyncio.sleep(interval_)
            gwy.send_cmd(cmd, priority=Priority.LOW)

        if interval is None:
            interval = 0 if count == 1 else 60

        if count <= 0:
            while True:
                await periodic_(interval)
        else:
            for _ in range(count):
                await periodic_(interval)

    _LOGGER.warning("poll_device() invoked...")

    tasks = []

    for code in (Code._0016, Code._1FC9):
        cmd = LegacyCommandShim.from_dto(
            CommandDTO(
                verb=RQ,
                addr1=HGI_DEV_ADDR.id,
                addr2=dev_id,
                addr3=NON_DEV_ADDR.id,
                code=code,
                payload="00",
            )
        )
        tasks.append(asyncio.create_task(periodic_send(gwy, cmd, count=0)))

    gwy._engine._tasks.extend(tasks)
    return tasks


@script_decorator
async def script_scan_disc(gwy: Gateway, dev_id: DeviceIdT) -> None:
    """Trigger the target device's internal discovery poller routine.

    :param gwy: The gateway instance.
    :param dev_id: The device ID to scan.
    """
    _LOGGER.warning("scan_disc() invoked...")

    await gwy.device_registry.get_device(dev_id).discovery.discover()


@script_decorator
async def script_scan_full(gwy: Gateway, dev_id: DeviceIdT) -> None:
    """Execute a comprehensive probe of a target device across all recognized schema codes.

    :param gwy: The gateway instance.
    :param dev_id: The device ID to scan.
    """
    _LOGGER.warning("scan_full() invoked - expect a lot of Warnings")

    gwy.send_cmd(
        LegacyCommandShim.from_dto(
            CommandDTO(
                verb=RQ,
                addr1=HGI_DEV_ADDR.id,
                addr2=dev_id,
                addr3=NON_DEV_ADDR.id,
                code=Code._0016,
                payload="0000",
            )
        ),
        num_repeats=3,
    )

    for code in sorted(CODES_SCHEMA):
        if code == Code._0005:
            for zone_type in range(20):  # known up to 18
                gwy.send_cmd(
                    LegacyCommandShim.from_dto(
                        CommandDTO(
                            verb=RQ,
                            addr1=HGI_DEV_ADDR.id,
                            addr2=dev_id,
                            addr3=NON_DEV_ADDR.id,
                            code=code,
                            payload=f"00{zone_type:02X}",
                        )
                    )
                )

        elif code == Code._000C:
            for zone_idx in range(16):  # also: FA-FF?
                gwy.send_cmd(
                    LegacyCommandShim.from_dto(
                        CommandDTO(
                            verb=RQ,
                            addr1=HGI_DEV_ADDR.id,
                            addr2=dev_id,
                            addr3=NON_DEV_ADDR.id,
                            code=code,
                            payload=f"{zone_idx:02X}00",
                        )
                    )
                )

        elif code == Code._0016:
            continue

        elif code in (Code._01D0, Code._01E9):
            for str_zone_idx in ("00", "01", "FC"):
                gwy.send_cmd(
                    LegacyCommandShim.from_dto(
                        CommandDTO(
                            verb=W_,
                            addr1=HGI_DEV_ADDR.id,
                            addr2=dev_id,
                            addr3=NON_DEV_ADDR.id,
                            code=code,
                            payload=f"{str_zone_idx}00",
                        )
                    )
                )
                gwy.send_cmd(
                    LegacyCommandShim.from_dto(
                        CommandDTO(
                            verb=W_,
                            addr1=HGI_DEV_ADDR.id,
                            addr2=dev_id,
                            addr3=NON_DEV_ADDR.id,
                            code=code,
                            payload=f"{str_zone_idx}03",
                        )
                    )
                )

        elif code == Code._0404:  # FIXME
            cmd1 = LegacyCommandShim.from_dto(
                build_dto(
                    Intent(
                        src=HGI_DEV_ADDR,
                        dst=Address(dev_id),
                        action=Action.GET_SCHEDULE_FRAGMENT,
                        data={"zone_idx": "HW", "frag_number": 1, "total_frags": 0},
                    )
                )
            )
            gwy.send_cmd(cmd1)
            cmd2 = LegacyCommandShim.from_dto(
                build_dto(
                    Intent(
                        src=HGI_DEV_ADDR,
                        dst=Address(dev_id),
                        action=Action.GET_SCHEDULE_FRAGMENT,
                        data={"zone_idx": "00", "frag_number": 1, "total_frags": 0},
                    )
                )
            )
            gwy.send_cmd(cmd2)

        elif code == Code._0418:
            for log_idx in range(2):
                cmd3 = LegacyCommandShim.from_dto(
                    build_dto(
                        Intent(
                            src=HGI_DEV_ADDR,
                            dst=Address(dev_id),
                            action=Action.GET_FAULTLOG_ENTRY,
                            data={"log_idx": log_idx},
                        )
                    )
                )
                gwy.send_cmd(cmd3)

        elif code == Code._1100:
            cmd4 = LegacyCommandShim.from_dto(
                build_dto(
                    Intent(
                        src=HGI_DEV_ADDR,
                        dst=Address(dev_id),
                        action=Action.GET_TPI_PARAMS,
                        data={},
                    )
                )
            )
            gwy.send_cmd(cmd4)

        elif code == Code._2E04:
            cmd = LegacyCommandShim.from_dto(
                build_dto(
                    Intent(
                        src=HGI_DEV_ADDR,
                        dst=Address(dev_id),
                        action=Action.GET_SYSTEM_MODE,
                        data={},
                    )
                )
            )
            gwy.send_cmd(cmd)

        elif code == Code._3220:
            for data_id in (0, 3):  # these are mandatory READ_DATA data_ids
                cmd = LegacyCommandShim.from_dto(
                    build_dto(
                        Intent(
                            src=HGI_DEV_ADDR,
                            dst=Address(dev_id),
                            action=Action.GET_OPENTHERM_DATA,
                            data={"msg_id": data_id},
                        )
                    )
                )
                gwy.send_cmd(cmd)

        elif code == Code._PUZZ:
            continue

        elif (
            code in CODES_SCHEMA
            and RQ in CODES_SCHEMA[code]
            and re.match(CODES_SCHEMA[code][RQ], "00")
        ):
            gwy.send_cmd(
                LegacyCommandShim.from_dto(
                    CommandDTO(
                        verb=RQ,
                        addr1=HGI_DEV_ADDR.id,
                        addr2=dev_id,
                        addr3=NON_DEV_ADDR.id,
                        code=code,
                        payload="00",
                    )
                )
            )

        else:
            gwy.send_cmd(
                LegacyCommandShim.from_dto(
                    CommandDTO(
                        verb=RQ,
                        addr1=HGI_DEV_ADDR.id,
                        addr2=dev_id,
                        addr3=NON_DEV_ADDR.id,
                        code=code,
                        payload="0000",
                    )
                )
            )

    # these are possible/difficult codes
    for code in (Code._0150, Code._2389):
        gwy.send_cmd(
            LegacyCommandShim.from_dto(
                CommandDTO(
                    verb=RQ,
                    addr1=HGI_DEV_ADDR.id,
                    addr2=dev_id,
                    addr3=NON_DEV_ADDR.id,
                    code=code,
                    payload="0000",
                )
            )
        )


@script_decorator
async def script_scan_hard(
    gwy: Gateway, dev_id: DeviceIdT, *, start_code: None | int = None
) -> None:
    """Execute a sequential numeric ping across the theoretical code space.

    :param gwy: The gateway instance.
    :param dev_id: The device ID to probe.
    :param start_code: Hex starting point for the iteration.
    """
    _LOGGER.warning("scan_hard() invoked - expect some Warnings")

    start_code = start_code or 0

    for code in range(start_code, 0x5000):
        await gwy.async_send_cmd(
            LegacyCommandShim.from_dto(
                CommandDTO(
                    verb=RQ,
                    addr1=HGI_DEV_ADDR.id,
                    addr2=dev_id,
                    addr3=NON_DEV_ADDR.id,
                    code=f"{code:04X}",
                    payload="0000",
                )
            ),
            priority=Priority.LOW,
        )


@script_decorator
async def script_scan_fan(gwy: Gateway, dev_id: DeviceIdT) -> None:
    """Probe an HVAC/Ventilator targeted device with standard parameters.

    :param gwy: The gateway instance.
    :param dev_id: The device ID to probe.
    """
    _LOGGER.warning("scan_fan() invoked - expect a lot of nonsense")

    from ramses_rf.protocol.ramses import _DEV_KLASSES_HVAC

    OUT_CODES = (
        Code._0016,
        Code._1470,
    )

    OLD_CODES = dict.fromkeys(
        c for k in _DEV_KLASSES_HVAC.values() for c in k if c not in OUT_CODES
    )
    for code in OLD_CODES:
        gwy.send_cmd(
            LegacyCommandShim.from_dto(
                CommandDTO(
                    verb=RQ,
                    addr1=HGI_DEV_ADDR.id,
                    addr2=dev_id,
                    addr3=NON_DEV_ADDR.id,
                    code=code,
                    payload="00",
                )
            )
        )

    NEW_CODES = (
        Code._0150,
        Code._042F,
        Code._1030,
        Code._10D0,
        Code._10E1,
        Code._2210,
        Code._22B0,
        Code._22E0,
        Code._22E5,
        Code._22E9,
        Code._22F1,
        Code._22F2,
        Code._22F3,
        Code._22F4,
        Code._22F7,
        Code._22F8,
        Code._2400,
        Code._2410,
        Code._2420,
        Code._313E,
        Code._3221,
        Code._3222,
    )

    for code in NEW_CODES:
        if code not in OLD_CODES and code not in OUT_CODES:
            gwy.send_cmd(
                LegacyCommandShim.from_dto(
                    CommandDTO(
                        verb=RQ,
                        addr1=HGI_DEV_ADDR.id,
                        addr2=dev_id,
                        addr3=NON_DEV_ADDR.id,
                        code=code,
                        payload="00",
                    )
                )
            )


@script_decorator
async def script_scan_otb(gwy: Gateway, dev_id: DeviceIdT) -> None:
    """Probe an OpenTherm Bridge targeted device across known data ID tables.

    :param gwy: The gateway instance.
    :param dev_id: The device ID to probe.
    """
    _LOGGER.warning("script_scan_otb_full invoked - expect a lot of nonsense")

    for msg_id in OTB_DATA_IDS:
        cmd = LegacyCommandShim.from_dto(
            build_dto(
                Intent(
                    src=HGI_DEV_ADDR,
                    dst=Address(dev_id),
                    action=Action.GET_OPENTHERM_DATA,
                    data={"msg_id": msg_id},
                )
            )
        )
        gwy.send_cmd(cmd)


@script_decorator
async def script_scan_otb_hard(gwy: Gateway, dev_id: DeviceIdT) -> None:
    """Probe an OpenTherm Bridge target iteratively across numeric data ID space.

    :param gwy: The gateway instance.
    :param dev_id: The device ID to probe.
    """
    _LOGGER.warning("script_scan_otb_hard invoked - expect a lot of nonsense")

    for msg_id in range(0x80):
        cmd = LegacyCommandShim.from_dto(
            build_dto(
                Intent(
                    src=HGI_DEV_ADDR,
                    dst=Address(dev_id),
                    action=Action.GET_OPENTHERM_DATA,
                    data={"msg_id": msg_id},
                )
            )
        )
        gwy.send_cmd(cmd, priority=Priority.LOW)


@script_decorator
async def script_scan_otb_map(gwy: Gateway, dev_id: DeviceIdT) -> None:
    """Execute mapping verifications between native RAMSES codes and OpenTherm properties.

    :param gwy: The gateway instance.
    :param dev_id: The device ID to probe.
    """
    _LOGGER.warning("script_scan_otb_map invoked - expect a lot of nonsense")

    RAMSES_TO_OPENTHERM = {
        Code._22D9: "01",  # boiler setpoint         / ControlSetpoint
        Code._3EF1: "11",  # rel. modulation level   / RelativeModulationLevel
        Code._1300: "12",  # cv water pressure       / CHWaterPressure
        Code._12F0: "13",  # dhw_flow_rate           / DHWFlowRate
        Code._3200: "19",  # boiler output temp      / BoilerWaterTemperature
        Code._1260: "1A",  # dhw temp                / DHWTemperature
        Code._1290: "1B",  # outdoor temp            / OutsideTemperature
        Code._3210: "1C",  # boiler return temp      / ReturnWaterTemperature
        Code._10A0: "38",  # dhw params[SZ_SETPOINT] / DHWSetpoint
        Code._1081: "39",  # max ch setpoint         / MaxCHWaterSetpoint
    }

    for code, msg_id in RAMSES_TO_OPENTHERM.items():
        gwy.send_cmd(
            LegacyCommandShim.from_dto(
                CommandDTO(
                    verb=RQ,
                    addr1=HGI_DEV_ADDR.id,
                    addr2=dev_id,
                    addr3=NON_DEV_ADDR.id,
                    code=code,
                    payload="00",
                )
            ),
            priority=Priority.LOW,
        )
        cmd = LegacyCommandShim.from_dto(
            build_dto(
                Intent(
                    src=HGI_DEV_ADDR,
                    dst=Address(dev_id),
                    action=Action.GET_OPENTHERM_DATA,
                    data={"msg_id": msg_id},
                )
            )
        )
        gwy.send_cmd(cmd, priority=Priority.LOW)


@script_decorator
async def script_scan_otb_ramses(gwy: Gateway, dev_id: DeviceIdT) -> None:
    """Probe an OpenTherm bridge exclusively for native RAMSES codes.

    :param gwy: The gateway instance.
    :param dev_id: The device ID to probe.
    """
    _LOGGER.warning("script_scan_otb_ramses invoked - expect a lot of nonsense")

    _CODES = (
        Code._042F,
        Code._10E0,  # device_info
        Code._10E1,  # device_id
        Code._1FD0,
        Code._2400,
        Code._2401,
        Code._2410,
        Code._2420,
        Code._1300,  # cv water pressure      / CHWaterPressure
        Code._1081,  # max ch setpoint        / MaxCHWaterSetpoint
        Code._10A0,  # dhw params[SZ_SETPOINT] / DHWSetpoint
        Code._22D9,  # boiler setpoint        / ControlSetpoint
        Code._1260,  # dhw temp               / DHWTemperature
        Code._1290,  # outdoor temp           / OutsideTemperature
        Code._3200,  # boiler output temp     / BoilerWaterTemperature
        Code._3210,  # boiler return temp     / ReturnWaterTemperature
        Code._0150,
        Code._12F0,  # dhw flow rate          / DHWFlowRate
        Code._1098,
        Code._10B0,
        Code._3221,
        Code._3223,
        Code._3EF0,  # rel. modulation level  / RelativeModulationLevel (also, below)
        Code._3EF1,  # rel. modulation level  / RelativeModulationLevel
    )

    for c in _CODES:
        gwy.send_cmd(
            LegacyCommandShim.from_dto(
                CommandDTO(
                    verb=RQ,
                    addr1=HGI_DEV_ADDR.id,
                    addr2=dev_id,
                    addr3=NON_DEV_ADDR.id,
                    code=c,
                    payload="00",
                )
            ),
            priority=Priority.LOW,
        )


SCRIPTS = {
    k[7:]: v for k, v in locals().items() if callable(v) and k.startswith("script_")
}
