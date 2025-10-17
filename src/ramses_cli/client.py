#!/usr/bin/env python3
"""A CLI for the ramses_rf library."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Final

import click
from colorama import Fore, Style, init as colorama_init

from ramses_rf import Gateway, GracefulExit, Message, exceptions as exc
from ramses_rf.const import DONT_CREATE_MESSAGES, SZ_ZONE_IDX
from ramses_rf.helpers import deep_merge
from ramses_rf.schemas import (
    SCH_GLOBAL_CONFIG,
    SZ_CONFIG,
    SZ_DISABLE_DISCOVERY,
    SZ_ENABLE_EAVESDROP,
    SZ_REDUCE_PROCESSING,
)
from ramses_tx import is_valid_dev_id
from ramses_tx.logger import CONSOLE_COLS, DEFAULT_DATEFMT, DEFAULT_FMT
from ramses_tx.schemas import (
    SZ_DISABLE_QOS,
    SZ_DISABLE_SENDING,
    SZ_ENFORCE_KNOWN_LIST,
    SZ_EVOFW_FLAG,
    SZ_FILE_NAME,
    SZ_KNOWN_LIST,
    SZ_PACKET_LOG,
    SZ_SERIAL_PORT,
)

from .debug import SZ_DBG_MODE, start_debugging
from .discovery import GET_FAULTS, GET_SCHED, SET_SCHED, spawn_scripts

from ramses_rf.const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    DEV_TYPE_MAP,
    Code,
)

_PROFILE_LIBRARY = False  # NOTE: for profiling of library

if _PROFILE_LIBRARY:
    import cProfile
    import pstats


SZ_INPUT_FILE: Final = "input_file"

# DEFAULT_SUMMARY can be: True, False, or None
SHOW_SCHEMA = False
SHOW_PARAMS = False
SHOW_STATUS = False
SHOW_KNOWNS = False
SHOW_TRAITS = False
SHOW_CRAZYS = False

PRINT_STATE = False  # print engine state
# GET_STATE = False  # get engine state
# SET_STATE = False  # set engine state

# this is called after import colorlog to ensure its handlers wrap the correct streams
logging.basicConfig(level=logging.WARNING, format=DEFAULT_FMT, datefmt=DEFAULT_DATEFMT)


EXECUTE: Final = "execute"
LISTEN: Final = "listen"
MONITOR: Final = "monitor"
PARSE: Final = "parse"


COLORS = {
    I_: Fore.GREEN,
    RP: Fore.CYAN,
    RQ: Fore.CYAN,
    W_: Style.BRIGHT + Fore.MAGENTA,
}

CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])

LIB_KEYS = tuple(SCH_GLOBAL_CONFIG({}).keys()) + (SZ_SERIAL_PORT,)
LIB_CFG_KEYS = tuple(SCH_GLOBAL_CONFIG({})[SZ_CONFIG].keys()) + (SZ_EVOFW_FLAG,)


def normalise_config(lib_config: dict) -> tuple[str, dict]:
    """Convert a HA config dict into the client library's own format."""

    serial_port = lib_config.pop(SZ_SERIAL_PORT, None)

    # fix for: https://github.com/ramses-rf/ramses_rf/issues/96
    packet_log = lib_config.get(SZ_PACKET_LOG)
    if isinstance(packet_log, str):
        packet_log = {SZ_FILE_NAME: packet_log}
    lib_config[SZ_PACKET_LOG] = packet_log

    return serial_port, lib_config


def split_kwargs(obj: tuple[dict, dict], kwargs: dict) -> tuple[dict, dict]:
    """Split kwargs into cli/library kwargs."""
    cli_kwargs, lib_kwargs = obj

    cli_kwargs.update(
        {k: v for k, v in kwargs.items() if k not in LIB_KEYS + LIB_CFG_KEYS}
    )
    lib_kwargs.update({k: v for k, v in kwargs.items() if k in LIB_KEYS})
    lib_kwargs[SZ_CONFIG].update({k: v for k, v in kwargs.items() if k in LIB_CFG_KEYS})

    return cli_kwargs, lib_kwargs


class DeviceIdParamType(click.ParamType):
    name = "device_id"

    def convert(self, value: str, param, ctx):
        if is_valid_dev_id(value):
            return value.upper()
        self.fail(f"{value!r} is not a valid device_id", param, ctx)


# Args/Params for both RF and file
@click.group(context_settings=CONTEXT_SETTINGS)  # , invoke_without_command=True)
@click.option("-z", "--debug-mode", count=True, help="enable debugger")
@click.option("-c", "--config-file", type=click.File("r"))
@click.option("-rk", "--restore-schema", type=click.File("r"), help="from a HA store")
@click.option("-rs", "--restore-state", type=click.File("r"), help=" from a HA store")
@click.option("-r", "--reduce-processing", count=True, help="-rrr will give packets")
@click.option("-lf", "--long-format", is_flag=True, help="dont truncate STDOUT")
@click.option("-e/-ne", "--eavesdrop/--no-eavesdrop", default=None)
@click.option("-g", "--print-state", count=True, help="print state (g=schema, gg=all)")
# @click.option("--get-state/--no-get-state", default=GET_STATE, help="get the engine state")
# @click.option("--set-state/--no-set-state", default=SET_STATE, help="set the engine state")
@click.option(  # show_schema
    "-k/-nk",
    "--show-schema/--no-show-schema",
    default=SHOW_SCHEMA,
    help="display system schema",
)
@click.option(  # show_params
    "-p/-np",
    "--show-params/--no-show-params",
    default=SHOW_PARAMS,
    help="display system params",
)
@click.option(  # show_status
    "-s/-ns",
    "--show-status/--no-show-status",
    default=SHOW_STATUS,
    help="display system state",
)
@click.option(  # show_knowns
    "-n/-nn",
    "--show-knowns/--no-show-knowns",
    default=SHOW_KNOWNS,
    help="display known_list (of devices)",
)
@click.option(  # show_traits
    "-t/-nt",
    "--show-traits/--no-show-traits",
    default=SHOW_TRAITS,
    help="display device traits",
)
@click.option(  # show_crazys
    "-x/-nx",
    "--show-crazys/--no-show-crazys",
    default=SHOW_CRAZYS,
    help="display crazy things",
)
@click.pass_context
def cli(ctx, config_file=None, eavesdrop: None | bool = None, **kwargs: Any) -> None:
    """A CLI for the ramses_rf library."""

    if kwargs[SZ_DBG_MODE] > 0:  # Do first
        start_debugging(kwargs[SZ_DBG_MODE] == 1)

    kwargs, lib_kwargs = split_kwargs(({}, {SZ_CONFIG: {}}), kwargs)

    if eavesdrop is not None:
        lib_kwargs[SZ_CONFIG][SZ_ENABLE_EAVESDROP] = eavesdrop

    if config_file:  # TODO: validate with voluptuous, use YAML
        lib_kwargs = deep_merge(
            lib_kwargs, json.load(config_file)
        )  # CLI takes precedence

    ctx.obj = kwargs, lib_kwargs


# Args/Params for packet log only
class FileCommand(click.Command):  # client.py parse <file>
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.params.insert(  # input_file name/path only
            0, click.Argument(("input-file",))
        )
        # self.params.insert(  # --packet-log  # NOTE: useful only for test/dev
        #     1,
        #     click.Option(
        #         ("-o", "--packet-log"),
        #         type=click.Path(),
        #         help="Log all packets to this file",
        #     ),
        # )


# Args/Params for RF packets only
class PortCommand(
    click.Command
):  # client.py <command> <port> --packet-log xxx --evofw3-flag xxx
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.params.insert(0, click.Argument(("serial-port",)))
        """ # self.params.insert(  # --no-discover
        #     1,
        #     click.Option(
        #         ("-d/-nd", "--discover/--no-discover"),
        #         is_flag=True,
        #         default=False,
        #         help="Log all packets to this file",
        #     ),
        # )
        # """
        self.params.insert(  # --packet-log
            2,
            click.Option(
                ("-o", "--packet-log"),
                type=click.Path(),
                help="Log all packets to this file",
            ),
        )
        self.params.insert(  # --evofw-flag
            3,
            click.Option(
                ("-T", "--evofw-flag"),
                type=click.STRING,
                help="Pass this traceflag to evofw",
            ),
        )


#
# 1/4: PARSE (a file, +/- eavesdrop)
@click.command(cls=FileCommand)  # parse a packet log file, then stop
@click.pass_obj
def parse(obj, **kwargs: Any):
    """Command to parse a log file containing messages/packets."""
    config, lib_config = split_kwargs(obj, kwargs)

    lib_config[SZ_INPUT_FILE] = config.pop(SZ_INPUT_FILE)  # just the file path

    return PARSE, lib_config, config


#
# 2/4: MONITOR (listen to RF, +/- discovery, +/- eavesdrop)
@click.command(cls=PortCommand)  # (optionally) execute a command/script, then monitor
@click.option("-d/-nd", "--discover/--no-discover", default=None)  # --no-discover
@click.option(  # --exec-cmd 'RQ 01:123456 1F09 00'
    "-x", "--exec-cmd", type=click.STRING, help="e.g. 'RQ 01:123456 1F09 00'"
)
@click.option(  # --execute-scr script device_id
    "-X",
    "--exec-scr",
    type=(str, DeviceIdParamType()),
    help="scan_disc|scan_full|scan_hard|bind device_id",
)
@click.option(  # --poll-devices device_id, device_id,...
    "--poll-devices", type=click.STRING, help="e.g. 'device_id, device_id, ...'"
)
@click.pass_obj
def monitor(obj, discover: None | bool = None, **kwargs: Any):
    """Monitor (eavesdrop and/or probe) a serial port for messages/packets."""
    config, lib_config = split_kwargs(obj, kwargs)

    if discover is None:
        if kwargs["exec_scr"] is None and kwargs["poll_devices"] is None:
            print(" - discovery is enabled")
            lib_config[SZ_CONFIG][SZ_DISABLE_DISCOVERY] = False
        else:
            print(" - discovery is disabled")
            lib_config[SZ_CONFIG][SZ_DISABLE_DISCOVERY] = True

    return MONITOR, lib_config, config


#
# 3/4: EXECUTE (send cmds to RF, +/- discovery, +/- eavesdrop)
@click.command(cls=PortCommand)  # execute a (complex) script, then stop
@click.option("-d/-nd", "--discover/--no-discover", default=None)  # --no-discover
@click.option(  # --exec-cmd 'RQ 01:123456 1F09 00'
    "-x", "--exec-cmd", type=click.STRING, help="e.g. 'RQ 01:123456 1F09 00'"
)
@click.option(  # --get-faults ctl_id
    "--get-faults", type=DeviceIdParamType(), help="controller_id"
)
@click.option(  # --get-schedule ctl_id zone_idx|HW
    "--get-schedule",
    default=[None, None],
    type=(DeviceIdParamType(), str),
    help="controller_id, zone_idx (e.g. '0A', 'HW')",
)
@click.option(  # --set-schedule ctl_id zone_idx|HW
    "--set-schedule",
    default=[None, None],
    type=(DeviceIdParamType(), click.File("r")),
    help="controller_id, filename.json",
)
@click.pass_obj
def execute(obj, **kwargs: Any):
    """Execute any specified scripts, return the results, then quit.

    Disables discovery, and enforces a strict allow_list.
    """
    config, lib_config = split_kwargs(obj, kwargs)

    print(" - discovery is force-disabled")
    lib_config[SZ_CONFIG][SZ_DISABLE_DISCOVERY] = True
    lib_config[SZ_CONFIG][SZ_DISABLE_QOS] = False

    if kwargs[GET_FAULTS]:
        known_list = {kwargs[GET_FAULTS]: {}}
    elif kwargs[GET_SCHED][0]:
        known_list = {kwargs[GET_SCHED][0]: {}}
    elif kwargs[SET_SCHED][0]:
        known_list = {kwargs[SET_SCHED][0]: {}}
    else:
        known_list = {}

    if known_list:
        print(" - known list is force-configured/enforced")
        lib_config[SZ_KNOWN_LIST] = known_list
        lib_config[SZ_CONFIG][SZ_ENFORCE_KNOWN_LIST] = True

    return EXECUTE, lib_config, config


#
# 4/4: LISTEN (to RF, +/- eavesdrop - NO sending/discovery)
@click.command(cls=PortCommand)  # (optionally) execute a command, then listen
@click.pass_obj
def listen(obj, **kwargs: Any):
    """Listen to (eavesdrop only) a serial port for messages/packets."""
    config, lib_config = split_kwargs(obj, kwargs)

    print(" - sending is force-disabled")
    lib_config[SZ_CONFIG][SZ_DISABLE_SENDING] = True

    return LISTEN, lib_config, config


def print_results(gwy: Gateway, **kwargs: Any) -> None:
    if kwargs[GET_FAULTS]:
        fault_log = gwy.system_by_id[kwargs[GET_FAULTS]]._faultlog.faultlog

        if fault_log is None:
            print("No fault log, or failed to get the fault log.")
        else:
            [print(f"{k:02X}", v) for k, v in fault_log.items()]

    if kwargs[GET_SCHED][0]:
        system_id, zone_idx = kwargs[GET_SCHED]
        if zone_idx == "HW":
            zone = gwy.system_by_id[system_id].dhw
        else:
            zone = gwy.system_by_id[system_id].zone_by_idx[zone_idx]
        schedule = zone.schedule

        if schedule is None:
            print("Failed to get the schedule.")
        else:
            result = {SZ_ZONE_IDX: zone_idx, "schedule": schedule}
            print(">>> Schedule JSON begins <<<")
            print(json.dumps(result, indent=4))
            print(">>> Schedule JSON ended <<<")

    if kwargs[SET_SCHED][0]:
        system_id, _ = kwargs[GET_SCHED]


def _save_state(gwy: Gateway) -> None:
    schema, msgs = gwy.get_state()

    with open("state_msgs.log", "w") as f:
        [f.write(f"{dtm} {pkt}\r\n") for dtm, pkt in msgs.items()]  # if not m._expired

    with open("state_schema.json", "w") as f:
        f.write(json.dumps(schema, indent=4))


def _print_engine_state(gwy: Gateway, **kwargs: Any) -> None:
    (schema, packets) = gwy.get_state(include_expired=True)

    if kwargs["print_state"] > 0:
        print(f"schema: {json.dumps(schema, indent=4)}\r\n")
    if kwargs["print_state"] > 1:
        print(f"packets: {json.dumps(packets, indent=4)}\r\n")


def print_summary(gwy: Gateway, **kwargs: Any) -> None:
    entity = gwy.tcs or gwy

    if kwargs.get("show_schema"):
        print(f"Schema[{entity}] = {json.dumps(entity.schema, indent=4)}\r\n")

        # schema = {d.id: d.schema for d in sorted(gwy.devices)}
        # print(f"Schema[devices] = {json.dumps({'schema': schema}, indent=4)}\r\n")

    if kwargs.get("show_params"):
        print(f"Params[{entity}] = {json.dumps(entity.params, indent=4)}\r\n")

        params = {d.id: d.params for d in sorted(gwy.devices)}
        print(f"Params[devices] = {json.dumps({'params': params}, indent=4)}\r\n")

    if kwargs.get("show_status"):
        print(f"Status[{entity}] = {json.dumps(entity.status, indent=4)}\r\n")

        status = {d.id: d.status for d in sorted(gwy.devices)}
        print(f"Status[devices] = {json.dumps({'status': status}, indent=4)}\r\n")

    if kwargs.get("show_knowns"):  # show device hints (show-knowns)
        print(f"allow_list (hints) = {json.dumps(gwy._include, indent=4)}\r\n")

    if kwargs.get("show_traits"):  # show device traits
        result = {
            d.id: d.traits  # {k: v for k, v in d.traits.items() if k[:1] == "_"}
            for d in sorted(gwy.devices)
        }
        print(json.dumps(result, indent=4), "\r\n")

    if kwargs.get("show_crazys"):
        for device in [d for d in gwy.devices if d.type == DEV_TYPE_MAP.CTL]:
            if gwy.msg_db:
                for msg in gwy.msg_db.get(device=device.id, code=Code._0005):
                    print(f"{msg._pkt}")
                for msg in gwy.msg_db.get(device=device.id, code=Code._000C):
                    print(f"{msg._pkt}")
            else:  # TODO(eb): replace next block by
                #  raise NotImplementedError
                for code, verbs in device._msgz.items():
                    if code in (Code._0005, Code._000C):
                        for verb in verbs.values():
                            for pkt in verb.values():
                                print(f"{pkt}")
            print()
        for device in [d for d in gwy.devices if d.type == DEV_TYPE_MAP.UFC]:
            if gwy.msg_db:
                for msg in gwy.msg_db.get(device=device.id):
                    print(f"{msg._pkt}")
            else:  # TODO(eb): replace next block by
                #  raise NotImplementedError
                for code in device._msgz.values():
                    for verb in code.values():
                        for pkt in verb.values():
                            print(f"{pkt}")
            print()


async def async_main(command: str, lib_kwargs: dict, **kwargs: Any) -> None:
    """Do certain things."""

    def handle_msg(msg: Message) -> None:
        """Process the message as it arrives (a callback).

        In this case, the message is merely printed.
        """

        if kwargs["long_format"]:  # HACK for test/dev
            print(
                f"{msg.dtm.isoformat(timespec='microseconds')} ... {msg!r}"
                f"  # {msg.payload}"  # or f'  # ("{msg.src!r}", "{msg.dst!r}")'
            )
            return

        dtm = f"{msg.dtm:%H:%M:%S.%f}"[:-3]
        con_cols = CONSOLE_COLS

        if msg.code == Code._PUZZ:
            print(f"{Style.BRIGHT}{Fore.YELLOW}{dtm} {msg}"[:con_cols])
        elif msg.src and msg.src.type == DEV_TYPE_MAP.HGI:
            print(f"{Style.BRIGHT}{COLORS.get(msg.verb)}{dtm} {msg}"[:con_cols])
        elif msg.code == Code._1F09 and msg.verb == I_:
            print(f"{Fore.YELLOW}{dtm} {msg}"[:con_cols])
        elif msg.code in (Code._000A, Code._2309, Code._30C9) and msg._has_array:
            print(f"{Fore.YELLOW}{dtm} {msg}"[:con_cols])
        else:
            print(f"{COLORS.get(msg.verb)}{dtm} {msg}"[:con_cols])

    serial_port, lib_kwargs = normalise_config(lib_kwargs)

    if kwargs["restore_schema"]:
        print(" - restoring client schema from a HA cache...")
        state = json.load(kwargs["restore_schema"])["data"]["client_state"]
        lib_kwargs = lib_kwargs | state["schema"]

    # if serial_port == "/dev/ttyMOCK":
    #     from tests.deprecated.mocked_rf import MockGateway  # FIXME: for test/dev
    #     gwy = MockGateway(serial_port, **lib_kwargs)
    # else:
    gwy = Gateway(serial_port, **lib_kwargs)  # passes action to gateway

    if lib_kwargs[SZ_CONFIG][SZ_REDUCE_PROCESSING] < DONT_CREATE_MESSAGES:
        # library will not send MSGs to STDOUT, so we'll send PKTs instead
        colorama_init(autoreset=True)  # WIP: remove strip=True
        gwy.add_msg_handler(handle_msg)

    if kwargs["restore_state"]:
        print(" - restoring packets from a HA cache...")
        state = json.load(kwargs["restore_state"])["data"]["client_state"]
        await gwy._restore_cached_packets(state["packets"])

    print("\r\nclient.py: Starting engine...")

    try:  # main code here
        await gwy.start()

        # TODO:
        # python client.py -rrr listen /dev/ttyUSB0
        # cat *.log | head | python client.py parse

        if command == EXECUTE:
            tasks = spawn_scripts(gwy, **kwargs)
            await asyncio.gather(*tasks)

        elif command == MONITOR:
            _ = spawn_scripts(gwy, **kwargs)
            await gwy._protocol._wait_connection_lost

        elif command in (LISTEN, PARSE):
            await gwy._protocol._wait_connection_lost

    except asyncio.CancelledError:
        msg = "ended via: CancelledError (e.g. SIGINT)"
    except GracefulExit:
        msg = "ended via: GracefulExit"
    except KeyboardInterrupt:  # FIXME: why isn't this captured here? see main
        msg = "ended via: KeyboardInterrupt"
    except exc.RamsesException as err:
        msg = f"ended via: RamsesException: {err}"
    else:  # if no Exceptions raised, e.g. EOF when parsing, or Ctrl-C?
        msg = "ended without error (e.g. EOF)"
    finally:
        await gwy.stop()  # what happens if we have an exception here?

    print(f"\r\nclient.py: Engine stopped: {msg}")

    # if kwargs["save_state"]:
    #    _save_state(gwy)

    if kwargs["print_state"]:
        _print_engine_state(gwy, **kwargs)

    elif command == EXECUTE:
        print_results(gwy, **kwargs)

    print_summary(gwy, **kwargs)


cli.add_command(parse)
cli.add_command(monitor)
cli.add_command(execute)
cli.add_command(listen)


def main() -> None:
    print("\r\nclient.py: Starting ramses_rf...")

    try:
        result = cli(standalone_mode=False)
    except click.NoSuchOption as err:
        print(f"Error: {err}")
        sys.exit(-1)

    if isinstance(result, int):
        sys.exit(result)

    (command, lib_kwargs, kwargs) = result

    if sys.platform == "win32":
        print(" - event_loop_policy set for win32")  # do before asyncio.run()
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        if _PROFILE_LIBRARY:
            profile = cProfile.Profile()
            profile.run("asyncio.run(main(command, lib_kwargs, **kwargs))")
        else:
            asyncio.run(async_main(command, lib_kwargs, **kwargs))
    except KeyboardInterrupt:  # , SystemExit):
        print("\r\nclient.py: Engine stopped: ended via: KeyboardInterrupt")

    if _PROFILE_LIBRARY:
        ps = pstats.Stats(profile)
        ps.sort_stats(pstats.SortKey.TIME).print_stats(20)

    print(" - finished ramses_rf.\r\n")


if __name__ == "__main__":
    main()
