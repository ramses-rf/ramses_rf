#!/usr/bin/env python3
"""RAMSES RF - discovery scripts."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

from ramses_rf import exceptions as exc
from ramses_rf.const import SZ_SCHEDULE, SZ_ZONE_IDX
from ramses_rf.device import Fakeable
from ramses_tx import CODES_SCHEMA, Command, DeviceIdT, Priority
from ramses_tx.opentherm import OTB_DATA_IDS

# Beware, none of this is reliable - it is all subject to random change
# However, these serve as examples how to use the other modules


from ramses_rf.const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

if TYPE_CHECKING:
    from ramses_rf import Gateway, IndexT


EXEC_CMD: Final = "exec_cmd"
GET_FAULTS: Final = "get_faults"
GET_SCHED: Final = "get_schedule"
SET_SCHED: Final = "set_schedule"

EXEC_SCR: Final = "exec_scr"
SCAN_DISC: Final = "scan_disc"
SCAN_FULL: Final = "scan_full"
SCAN_HARD: Final = "scan_hard"
SCAN_XXXX: Final = "scan_xxxx"

# DEVICE_ID_REGEX = re.compile(DEVICE_ID_REGEX.ANY)


_LOGGER = logging.getLogger(__name__)


def script_decorator(fnc: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fnc)
    def wrapper(gwy: Gateway, *args: Any, **kwargs: Any) -> None:
        gwy.send_cmd(
            Command._puzzle(message="Script begins:"),
            priority=Priority.HIGHEST,
            num_repeats=3,
        )

        fnc(gwy, *args, **kwargs)

        gwy.send_cmd(
            Command._puzzle(message="Script done."),
            priority=Priority.LOWEST,
            num_repeats=3,
        )

        return None

    return wrapper


def spawn_scripts(gwy: Gateway, **kwargs: Any) -> list[asyncio.Task[None]]:
    tasks = []

    if kwargs.get(EXEC_CMD):
        tasks += [asyncio.create_task(exec_cmd(gwy, **kwargs))]

    if kwargs.get(GET_FAULTS):
        tasks += [asyncio.create_task(get_faults(gwy, kwargs[GET_FAULTS]))]

    elif kwargs.get(GET_SCHED) and kwargs[GET_SCHED][0]:
        tasks += [asyncio.create_task(get_schedule(gwy, *kwargs[GET_SCHED]))]

    elif kwargs.get(SET_SCHED) and kwargs[SET_SCHED][0]:
        tasks += [asyncio.create_task(set_schedule(gwy, *kwargs[SET_SCHED]))]

    elif kwargs.get(EXEC_SCR):
        script = SCRIPTS.get(f"{kwargs[EXEC_SCR][0]}")
        if script is None:
            _LOGGER.warning(f"Script: {kwargs[EXEC_SCR][0]}() - unknown script")
        else:
            _LOGGER.info(f"Script: {kwargs[EXEC_SCR][0]}().- starts...")
            tasks += [asyncio.create_task(script(gwy, kwargs[EXEC_SCR][1]))]

    gwy._tasks.extend(tasks)
    return tasks


async def exec_cmd(gwy: Gateway, **kwargs: Any) -> None:
    cmd = Command.from_cli(kwargs[EXEC_CMD])
    await gwy.async_send_cmd(cmd, priority=Priority.HIGH, wait_for_reply=True)


# @script_decorator
# async def script_scan_001(gwy: Gateway, dev_id: DeviceIdT):
#     _LOGGER.warning("scan_001() invoked - expect a lot of nonsense")
#     for idx in range(0x10):
#         gwy.send_cmd(Command.from_attrs(W_, dev_id, Code._000E, f"{idx:02X}0050"))
#         gwy.send_cmd(Command.from_attrs(RQ, dev_id, Code._000E, f"{idx:02X}00C8"))


async def get_faults(
    gwy: Gateway, ctl_id: DeviceIdT, start: int = 0, limit: int = 0x3F
) -> None:
    ctl = gwy.get_device(ctl_id)

    try:
        await ctl.tcs.get_faultlog(start=start, limit=limit)  # 0418
    except exc.ExpiredCallbackError as err:
        _LOGGER.error("get_faults(): Function timed out: %s", err)


async def get_schedule(gwy: Gateway, ctl_id: DeviceIdT, zone_idx: str) -> None:
    zone = gwy.get_device(ctl_id).tcs.get_htg_zone(zone_idx)

    try:
        await zone.get_schedule()
    except exc.ExpiredCallbackError as err:
        _LOGGER.error("get_schedule(): Function timed out: %s", err)


async def set_schedule(gwy: Gateway, ctl_id: DeviceIdT, schedule: str) -> None:
    schedule_ = json.loads(schedule)
    zone_idx = schedule_[SZ_ZONE_IDX]

    zone = gwy.get_device(ctl_id).tcs.get_htg_zone(zone_idx)

    try:
        await zone.set_schedule(schedule_[SZ_SCHEDULE])  # 0404
    except exc.ExpiredCallbackError as err:
        _LOGGER.error("set_schedule(): Function timed out: %s", err)


async def script_bind_req(
    gwy: Gateway, dev_id: DeviceIdT, code: Code = Code._2309
) -> None:
    dev = gwy.get_device(dev_id)
    assert isinstance(dev, Fakeable)  # mypy
    dev._make_fake()
    await dev._initiate_binding_process([code])


async def script_bind_wait(
    gwy: Gateway, dev_id: DeviceIdT, code: Code = Code._2309, idx: IndexT = "00"
) -> None:
    dev = gwy.get_device(dev_id)
    assert isinstance(dev, Fakeable)  # mypy
    dev._make_fake()
    await dev._wait_for_binding_request([code], idx=idx)


def script_poll_device(gwy: Gateway, dev_id: DeviceIdT) -> list[asyncio.Task[None]]:
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
        cmd = Command.from_attrs(RQ, dev_id, code, "00")
        tasks.append(asyncio.create_task(periodic_send(gwy, cmd, count=0)))

    gwy._tasks.extend(tasks)
    return tasks


@script_decorator
async def script_scan_disc(gwy: Gateway, dev_id: DeviceIdT) -> None:
    _LOGGER.warning("scan_disc() invoked...")

    await gwy.get_device(dev_id).discover()  # discover_flag=Discover.DEFAULT)


@script_decorator
async def script_scan_full(gwy: Gateway, dev_id: DeviceIdT) -> None:
    _LOGGER.warning("scan_full() invoked - expect a lot of Warnings")

    gwy.send_cmd(Command.from_attrs(RQ, dev_id, Code._0016, "0000"), num_repeats=3)

    for code in sorted(CODES_SCHEMA):
        if code == Code._0005:
            for zone_type in range(20):  # known up to 18
                gwy.send_cmd(Command.from_attrs(RQ, dev_id, code, f"00{zone_type:02X}"))

        elif code == Code._000C:
            for zone_idx in range(16):  # also: FA-FF?
                gwy.send_cmd(Command.from_attrs(RQ, dev_id, code, f"{zone_idx:02X}00"))

        elif code == Code._0016:
            continue

        elif code in (Code._01D0, Code._01E9):
            for zone_idx in ("00", "01", "FC"):  # type: ignore[assignment]
                gwy.send_cmd(Command.from_attrs(W_, dev_id, code, f"{zone_idx}00"))
                gwy.send_cmd(Command.from_attrs(W_, dev_id, code, f"{zone_idx}03"))

        elif code == Code._0404:  # FIXME
            gwy.send_cmd(Command.get_schedule_fragment(dev_id, "HW", 1, 0))
            gwy.send_cmd(Command.get_schedule_fragment(dev_id, "00", 1, 0))

        elif code == Code._0418:
            for log_idx in range(2):
                gwy.send_cmd(Command.get_system_log_entry(dev_id, log_idx))

        elif code == Code._1100:
            gwy.send_cmd(Command.get_tpi_params(dev_id))

        elif code == Code._2E04:
            gwy.send_cmd(Command.get_system_mode(dev_id))

        elif code == Code._3220:
            for data_id in (0, 3):  # these are mandatory READ_DATA data_ids
                gwy.send_cmd(Command.get_opentherm_data(dev_id, data_id))

        elif code == Code._PUZZ:
            continue

        elif (
            code in CODES_SCHEMA
            and RQ in CODES_SCHEMA[code]
            and re.match(CODES_SCHEMA[code][RQ], "00")
        ):
            gwy.send_cmd(Command.from_attrs(RQ, dev_id, code, "00"))

        else:
            gwy.send_cmd(Command.from_attrs(RQ, dev_id, code, "0000"))

    # these are possible/difficult codes
    for code in (Code._0150, Code._2389):
        gwy.send_cmd(Command.from_attrs(RQ, dev_id, code, "0000"))


@script_decorator
async def script_scan_hard(
    gwy: Gateway, dev_id: DeviceIdT, *, start_code: None | int = None
) -> None:
    _LOGGER.warning("scan_hard() invoked - expect some Warnings")

    start_code = start_code or 0

    for code in range(start_code, 0x5000):
        await gwy.async_send_cmd(
            Command.from_attrs(RQ, dev_id, f"{code:04X}", "0000"),  # type:ignore[arg-type]
            priority=Priority.LOW,
        )


@script_decorator
async def script_scan_fan(gwy: Gateway, dev_id: DeviceIdT) -> None:
    _LOGGER.warning("scan_fan() invoked - expect a lot of nonsense")

    from ramses_tx.ramses import _DEV_KLASSES_HVAC

    OUT_CODES = (
        Code._0016,
        Code._1470,
    )

    OLD_CODES = dict.fromkeys(
        c for k in _DEV_KLASSES_HVAC.values() for c in k if c not in OUT_CODES
    )
    for code in OLD_CODES:
        gwy.send_cmd(Command.from_attrs(RQ, dev_id, code, "00"))

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
            gwy.send_cmd(Command.from_attrs(RQ, dev_id, code, "00"))


@script_decorator
async def script_scan_otb(gwy: Gateway, dev_id: DeviceIdT) -> None:
    _LOGGER.warning("script_scan_otb_full invoked - expect a lot of nonsense")

    for msg_id in OTB_DATA_IDS:
        gwy.send_cmd(Command.get_opentherm_data(dev_id, msg_id))


@script_decorator
async def script_scan_otb_hard(gwy: Gateway, dev_id: DeviceIdT) -> None:
    _LOGGER.warning("script_scan_otb_hard invoked - expect a lot of nonsense")

    for msg_id in range(0x80):
        gwy.send_cmd(Command.get_opentherm_data(dev_id, msg_id), priority=Priority.LOW)


@script_decorator
async def script_scan_otb_map(
    gwy: Gateway, dev_id: DeviceIdT
) -> None:  # Tested only upon a R8820A
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
        gwy.send_cmd(Command.from_attrs(RQ, dev_id, code, "00"), priority=Priority.LOW)
        gwy.send_cmd(Command.get_opentherm_data(dev_id, msg_id), priority=Priority.LOW)


@script_decorator
async def script_scan_otb_ramses(
    gwy: Gateway, dev_id: DeviceIdT
) -> None:  # Tested only upon a R8820A
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
    )  # excl. 3150, 3220

    for c in _CODES:
        gwy.send_cmd(Command.from_attrs(RQ, dev_id, c, "00"), priority=Priority.LOW)


SCRIPTS = {
    k[7:]: v for k, v in locals().items() if callable(v) and k.startswith("script_")
}
