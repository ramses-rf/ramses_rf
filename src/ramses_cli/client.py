#!/usr/bin/env python3
"""A CLI for the ramses_rf library."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, Literal

import asyncclick as click
from asyncclick.exceptions import ClickException, Exit
from colorama import Fore, Style, init as colorama_init

from ramses_rf import Gateway, GracefulExit, Message, exceptions as exc
from ramses_rf.const import (
    DEV_TYPE_MAP,
    DONT_CREATE_MESSAGES,
    SZ_CONFIG,
    SZ_DISABLE_DISCOVERY,
    SZ_ZONE_INDEX,
)
from ramses_rf.discovery_scan import DiscoveryScan
from ramses_rf.gateway import GatewayConfig
from ramses_rf.helpers import deep_merge
from ramses_rf.schemas import (
    SCH_GLOBAL_CONFIG,
    SZ_ENABLE_EAVESDROP,
    SZ_REDUCE_PROCESSING,
)
from ramses_tx import is_valid_dev_id
from ramses_tx.config import EngineConfig
from ramses_tx.const import I_, RP, RQ, W_, Code
from ramses_tx.dtos import PacketDTO
from ramses_tx.logger import CONSOLE_COLS, DEFAULT_DATEFMT, DEFAULT_FMT
from ramses_tx.schemas import (
    SZ_DISABLE_QOS,
    SZ_DISABLE_SENDING,
    SZ_ENFORCE_KNOWN_LIST,
    SZ_EVOFW_FLAG,
    SZ_KNOWN_LIST,
    SZ_PACKET_LOG,
    SZ_PACKET_LOG_PREFIX,
    SZ_SERIAL_PORT,
)

from .debug import SZ_DBG_MODE, start_debugging
from .discovery import GET_FAULTS, GET_SCHED, SET_SCHED, spawn_scripts
from .inspect import (
    dissect_payload,
    extract_packet_details,
    format_dissection_output,
)

if TYPE_CHECKING:
    from _typeshed import SupportsRead

CliKwargsT = dict[  # type: ignore[explicit-any]  # heterogeneous click/config bag
    str, Any
]
CliObjT = tuple[CliKwargsT, CliKwargsT]


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

# this is called after import colorlog to ensure its handlers wrap correctly
logging.basicConfig(
    level=logging.WARNING, format=DEFAULT_FMT, datefmt=DEFAULT_DATEFMT
)


EXECUTE: Final = "execute"
LISTEN: Final = "listen"
MONITOR: Final = "monitor"
PARSE: Final = "parse"
SCAN: Final = "scan"


COLORS = {
    I_: Fore.GREEN,
    RP: Fore.CYAN,
    RQ: Fore.CYAN,
    W_: Style.BRIGHT + Fore.MAGENTA,
}

CONTEXT_SETTINGS: dict[str, list[str]] = dict(
    help_option_names=["-h", "--help"]
)

LIB_KEYS = tuple(SCH_GLOBAL_CONFIG({}).keys()) + (SZ_SERIAL_PORT,)
LIB_CFG_KEYS = tuple(SCH_GLOBAL_CONFIG({})[SZ_CONFIG].keys()) + (
    SZ_EVOFW_FLAG,
)


def normalise_config(
    lib_config: dict[str, dict[str, str | bool | None]],
) -> tuple[str | None, CliKwargsT | None]:
    """Convert a HA config dict into the client library's own format.

    :param lib_config: The configuration dictionary from Home Assistant.
    :return: A tuple containing the serial port and normalized config.
    """
    serial_port = lib_config.pop(SZ_SERIAL_PORT, None)

    # fix for: https://github.com/ramses-rf/ramses_rf/issues/96
    packet_log: str | Mapping[str, str | bool | None] | None = lib_config.get(
        SZ_PACKET_LOG
    )
    if packet_log is None:
        packet_log = {}
    elif isinstance(packet_log, str):
        packet_log = {SZ_PACKET_LOG_PREFIX: packet_log}
    assert isinstance(packet_log, dict)
    lib_config[SZ_PACKET_LOG] = packet_log

    return serial_port, lib_config  # type: ignore[return-value]


def split_kwargs(
    obj: CliObjT, kwargs: CliKwargsT
) -> tuple[CliKwargsT, CliKwargsT]:
    """Split kwargs into cli/library kwargs.

    :param obj: Tuple of CLI and library configuration dictionaries.
    :param kwargs: The keyword arguments to split.
    :return: Tuple containing updated CLI and library configurations.
    """
    cli_kwargs, lib_kwargs = obj

    cli_kwargs.update(
        {k: v for k, v in kwargs.items() if k not in LIB_KEYS + LIB_CFG_KEYS}
    )

    lib_values = {k: v for k, v in kwargs.items() if k in LIB_KEYS}
    if (
        SZ_PACKET_LOG in lib_values
        and lib_values[SZ_PACKET_LOG] is None
        and SZ_PACKET_LOG in lib_kwargs
    ):
        del lib_values[SZ_PACKET_LOG]
    lib_kwargs.update(lib_values)

    lib_kwargs[SZ_CONFIG].update(
        {k: v for k, v in kwargs.items() if k in LIB_CFG_KEYS}
    )

    return cli_kwargs, lib_kwargs


class DeviceIdParamType(click.ParamType[str]):
    """A Click parameter type for Device IDs."""

    name = "device_id"

    def convert(
        self,
        value: str,
        parameter: click.Parameter | None,
        context: click.Context | None,
    ) -> str:
        """Convert the value to a Device ID.

        :param value: The value to convert.
        :param parameter: The parameter being converted.
        :param context: The Click context.
        :return: The converted Device ID.
        :raises click.BadParameter: If value is not a valid Device ID.
        """
        if is_valid_dev_id(value):
            return value.upper()
        self.fail(f"{value!r} is not a valid device_id", parameter, context)
        assert False  # satisfy mypy


# Args/Params for both RF and file
@click.group(
    context_settings=CONTEXT_SETTINGS
)  # , invoke_without_command=True)
@click.option("-z", "--debug-mode", count=True, help="enable debugger")
@click.option("-c", "--config-file", type=click.File("r"))
@click.option(
    "-rk", "--restore-schema", type=click.File("r"), help="from a HA store"
)
@click.option(
    "-rs", "--restore-state", type=click.File("r"), help=" from a HA store"
)
@click.option(
    "-r", "--reduce-processing", count=True, help="-rrr will give packets"
)
@click.option(
    "-lf", "--long-format", is_flag=True, help="dont truncate STDOUT"
)
@click.option(
    "-jf", "--json-format", is_flag=True, help="messages as json output"
)
@click.option("-e/-ne", "--eavesdrop/--no-eavesdrop", default=None)
@click.option(
    "-g", "--print-state", count=True, help="print state (g=schema, gg=all)"
)
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
async def cli(  # type: ignore[explicit-any]  # heterogeneous click kwargs
    ctx: click.Context,
    /,
    config_file: SupportsRead[str | bytes] | None = None,
    eavesdrop: None | bool = None,
    **kwargs: Any,
) -> None:
    """Initialise the CLI client for the ramses_rf library.

    :param ctx: The Click context.
    :param config_file: An optional configuration file to load.
    :param eavesdrop: Whether to enable eavesdropping mode.
    :param kwargs: Additional keyword arguments.
    """
    if kwargs[SZ_DBG_MODE] > 0:  # Do first
        start_debugging(kwargs[SZ_DBG_MODE] == 1)

    kwargs, lib_kwargs = split_kwargs(({}, {SZ_CONFIG: {}}), kwargs)

    if eavesdrop is not None:
        lib_kwargs[SZ_CONFIG][SZ_ENABLE_EAVESDROP] = eavesdrop

    if config_file:  # TODO: validate file with probatio, use YAML
        lib_kwargs = deep_merge(
            lib_kwargs, json.load(config_file)
        )  # CLI takes precedence

    ctx.obj = kwargs, lib_kwargs


# Args/Params for packet log only
class FileCommand(click.Command):  # client.py parse <file>
    """A Click Command class for file-based operations."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[explicit-any]  # click passthrough
        """Initialise FileCommand and configure input-file argument.

        :param args: Positional arguments for Click Command.
        :param kwargs: Keyword arguments for Click Command.
        """
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


# Args/Params for decode operations
class DecodeCommand(click.Command):  # client.py decode [payload]
    """A Click Command class for payload decoding operations."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[explicit-any]  # click passthrough
        """Initialise DecodeCommand and configure optional payload argument.

        :param args: Positional arguments for Click Command.
        :param kwargs: Keyword arguments for Click Command.
        """
        super().__init__(*args, **kwargs)
        self.params.insert(
            0,
            click.Argument(("payload",), required=False),
        )


# Args/Params for RF packets only
class PortCommand(
    click.Command
):  # client.py <command> <port> --packet-log xxx --evofw3-flag xxx
    """A Click Command class for serial port operations."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[explicit-any]  # click passthrough
        """Initialise PortCommand and configure serial-port argument.

        :param args: Positional arguments for Click Command.
        :param kwargs: Keyword arguments for Click Command.
        """
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
# 1/5: PARSE (a file, +/- eavesdrop)
@click.command(cls=FileCommand)  # parse a packet log file, then stop
@click.pass_obj
async def parse(  # type: ignore[explicit-any]  # heterogeneous click kwargs
    obj: CliObjT,
    /,
    **kwargs: Any,
) -> tuple[Literal["parse"], dict[str, str], dict[str, str]]:
    """Command to parse a log file containing messages/packets.

    :param obj: The context object containing configuration.
    :param kwargs: Additional keyword arguments.
    :return: Tuple of command name, library config, and CLI config.
    """
    config, lib_config = split_kwargs(obj, kwargs)

    lib_config[SZ_INPUT_FILE] = config.pop(SZ_INPUT_FILE)  # just the file path

    return PARSE, lib_config, config


#
# 2/5: MONITOR (listen to RF, +/- discovery, +/- eavesdrop)
@click.command(
    cls=PortCommand
)  # (optionally) execute a command/script, then monitor
@click.option(
    "-d/-nd", "--discover/--no-discover", default=None
)  # --no-discover
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
    "--poll-devices",
    type=click.STRING,
    help="e.g. 'device_id, device_id, ...'",
)
@click.pass_obj
async def monitor(  # type: ignore[explicit-any]  # heterogeneous click kwargs
    obj: CliObjT,
    /,
    discover: None | bool = None,
    **kwargs: Any,
) -> tuple[Literal["monitor"], dict[str, str], dict[str, str]]:
    """Monitor (eavesdrop and/or probe) a serial port for messages.

    :param obj: The context object containing configuration.
    :param discover: Whether to enable discovery.
    :param kwargs: Additional keyword arguments.
    :return: Tuple of command name, library config, and CLI config.
    """
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
# 3/5: EXECUTE (send cmds to RF, +/- discovery, +/- eavesdrop)
@click.command(cls=PortCommand)  # execute a (complex) script, then stop
@click.option(
    "-d/-nd", "--discover/--no-discover", default=None
)  # --no-discover
@click.option(  # --exec-cmd 'RQ 01:123456 1F09 00'
    "-x", "--exec-cmd", type=click.STRING, help="e.g. 'RQ 01:123456 1F09 00'"
)
@click.option(  # --get-faults ctl_id
    "--get-faults", type=DeviceIdParamType(), help="controller_id"
)
@click.option(  # --get-schedule ctl_id zone_index|HW
    "--get-schedule",
    default=[None, None],
    type=(DeviceIdParamType(), str),
    help="controller_id, zone_index (e.g. '0A', 'HW')",
)
@click.option(  # --set-schedule ctl_id zone_index|HW
    "--set-schedule",
    default=[None, None],
    type=(DeviceIdParamType(), click.File("r")),
    help="controller_id, filename.json",
)
@click.pass_obj
async def execute(  # type: ignore[explicit-any]  # heterogeneous click kwargs
    obj: CliObjT,
    /,
    **kwargs: Any,
) -> tuple[
    Literal["execute"],
    dict[str | None, str | CliKwargsT],
    dict[str, str],
]:
    """Execute any specified scripts, return the results, then quit.

    Disables discovery, and enforces a strict allow_list.

    :param obj: Tuple containing CLI and library configuration dicts.
    :param kwargs: Additional arguments passed to the command.
    :return: Tuple of command string, library config, and CLI config.
    """
    config, lib_config = split_kwargs(obj, kwargs)

    print(" - discovery is force-disabled")
    lib_config[SZ_CONFIG][SZ_DISABLE_DISCOVERY] = True
    lib_config[SZ_CONFIG][SZ_DISABLE_QOS] = False

    known_list: dict[str | None, CliKwargsT] = {}
    if kwargs[GET_FAULTS]:
        known_list = {kwargs[GET_FAULTS]: {}}
    elif kwargs[GET_SCHED][0]:
        known_list = {kwargs[GET_SCHED][0]: {}}
    elif kwargs[SET_SCHED][0]:
        known_list = {kwargs[SET_SCHED][0]: {}}

    if known_list:
        print(" - known list is force-configured/enforced")
        lib_config[SZ_KNOWN_LIST] = known_list
        lib_config[SZ_CONFIG][SZ_ENFORCE_KNOWN_LIST] = True

    return EXECUTE, lib_config, config  # type: ignore[return-value]


#
# 4/5: LISTEN (to RF, +/- eavesdrop - NO sending/discovery)
@click.command(cls=PortCommand)  # (optionally) execute a command, then listen
@click.pass_obj
async def listen(  # type: ignore[explicit-any]  # heterogeneous click kwargs
    obj: CliObjT,
    /,
    **kwargs: Any,
) -> tuple[
    Literal["listen"],
    dict[str, str | dict[str, str | None] | None],
    CliKwargsT,
]:
    """Listen to (eavesdrop only) a serial port for messages/packets.

    :param obj: The context object containing configuration.
    :param kwargs: Additional keyword arguments.
    :return: Tuple of command name, library config, and CLI config.
    """
    config, lib_config = split_kwargs(obj, kwargs)

    print(" - sending is force-disabled")
    lib_config[SZ_CONFIG][SZ_DISABLE_SENDING] = True

    return LISTEN, lib_config, config


#
# 5/5: SCAN (passive device discovery — listen, classify, export, stop)
@click.command(cls=PortCommand)
@click.option(  # --duration seconds (0 = until Ctrl-C)
    "-d",
    "--duration",
    type=int,
    default=300,
    help="scan duration in seconds (0 = until interrupted)",
)
@click.option(  # --file
    "-f",
    "--file",
    type=click.Path(),
    help="export discovered devices to JSON file",
)
@click.pass_obj
async def scan(  # type: ignore[explicit-any]  # heterogeneous click kwargs
    obj: CliObjT,
    /,
    duration: int = 300,
    **kwargs: Any,
) -> tuple[Literal["scan"], dict[str, str], CliKwargsT]:
    """Passive device scan: listen to RF, classify unknown devices, export.

    No discovery, no eavesdrop, no sending — pure observation.
    Discovered devices are classified by prefix and verb/code pairs.

    :param obj: The context object containing configuration.
    :param duration: Scan duration in seconds (0 = until interrupted).
    :param kwargs: Additional keyword arguments.
    :return: Tuple of command name, library config, and CLI config.
    """
    config, lib_config = split_kwargs(obj, kwargs)

    print(" - sending is force-disabled (passive scan)")
    lib_config[SZ_CONFIG][SZ_DISABLE_SENDING] = True
    lib_config[SZ_CONFIG][SZ_DISABLE_DISCOVERY] = True

    # Pass duration and output file via kwargs (consumed in async_main)
    config["scan_duration"] = duration
    config["scan_output"] = kwargs.get("file")

    return SCAN, lib_config, config


# DECODE (ad-hoc payload or packet inspection)
@click.command(cls=DecodeCommand)
@click.option(
    "-c",
    "--code",
    type=click.STRING,
    help="RAMSES opcode (e.g. 2411, 10E0, 31DA).",
)
@click.option(
    "-v", "--verb", type=click.STRING, help="Message verb (e.g. RP, I, RQ, W)."
)
@click.option(
    "-b",
    "--block-size",
    "block_size",
    type=click.STRING,
    default="all",
    help="Word block size in bytes (1, 2, 4, 6, 8, or all; default: all).",
)
@click.option(
    "-s",
    "--skip",
    type=int,
    default=0,
    help="Number of leading bytes to skip.",
)
@click.option(
    "-j",
    "--json",
    "json_output",
    is_flag=True,
    help="Output structured dissection as JSON.",
)
async def decode(
    payload: str | None = None,
    code: str | None = None,
    verb: str | None = None,
    block_size: str = "all",
    skip: int = 0,
    json_output: bool = False,
) -> int:
    """Dissect and analyze an ad-hoc payload or packet string.

    :param payload: Raw hex payload string or full packet log line.
    :param code: Optional RAMSES opcode override.
    :param verb: Optional message verb override.
    :param block_size: Block size filter ('1', '2', '4', '6', '8', 'all').
    :param skip: Number of leading payload bytes to skip.
    :param json_output: Whether to output raw structured JSON.
    :return: Exit code integer.
    """
    raw_input = payload
    if not raw_input or raw_input == "-":
        if not sys.stdin.isatty():
            raw_input = sys.stdin.read().strip()

    if not raw_input:
        print("Error: No payload or packet string provided.", file=sys.stderr)
        return 1

    extracted_code, extracted_verb, payload_hex = extract_packet_details(
        raw_input
    )
    target_code = code if code else extracted_code
    target_verb = verb if verb else extracted_verb

    if not payload_hex:
        print(
            f"Error: Could not extract hex payload from: {raw_input}",
            file=sys.stderr,
        )
        return 1

    block_sizes: tuple[int, ...] = (
        (1, 2, 4, 6, 8) if block_size == "all" else (int(block_size),)
    )

    dissection = dissect_payload(
        payload_hex,
        block_sizes=block_sizes,
        skip_bytes=skip,
        opcode=target_code,
        verb=target_verb,
    )

    if json_output:
        print(json.dumps(dissection, indent=2, default=str))
    else:
        print(format_dissection_output(dissection))

    return 0


def _print_scan_results(scan: DiscoveryScan, output_path: str | None) -> None:
    """Print discovered devices and optionally export to JSON file.

    :param scan: The DiscoveryScan engine instance.
    :param output_path: Optional file path to export JSON to.
    """
    devices = scan.get_devices()
    if not devices:
        print("No unknown devices discovered.")
        return

    print(f"\nFound {len(devices)} device(s):\n")
    print(f"  {'Device ID':<12} {'Type':<6} {'Conf':<7} {'RSSI':>6}  Details")
    print(f"  {'-' * 12} {'-' * 6} {'-' * 7} {'-' * 6}  {'-' * 30}")
    for discovered_device in sorted(devices, key=lambda d: d.device_id):
        rssi_str = (
            f"{discovered_device.rssi:.0f}"
            if discovered_device.rssi is not None
            else "-"
        )
        details = ""
        if discovered_device.zone_index:
            details += f"zone={discovered_device.zone_index}  "
        if discovered_device.bound_to:
            details += f"bound to {discovered_device.bound_to}"
        if discovered_device.is_battery:
            details += "  battery" if details else "battery"
        print(
            f"  {discovered_device.device_id:<12} {discovered_device.likely_type:<6} "
            f"{discovered_device.confidence:<7} {rssi_str:>6}  {details}".rstrip()
        )

    if output_path:
        json_data = scan.export_json()
        with open(output_path, "w") as f:
            f.write(json_data)
        print(f"\nExported to {output_path}")


def print_results(  # type: ignore[explicit-any]  # heterogeneous click kwargs
    gateway: Gateway,
    **kwargs: Any,
) -> None:
    """Print the results of execution commands (faults, schedules).

    :param gateway: The gateway instance.
    :param kwargs: The command arguments.
    """
    if kwargs[GET_FAULTS]:
        fault_log = gateway.device_registry.system_by_id[
            kwargs[GET_FAULTS]
        ]._faultlog.faultlog

        if fault_log:
            for log_index, entry in fault_log.items():
                print(f"{log_index:02X}", entry)
        else:
            print("No fault log, or failed to get the fault log.")

    if kwargs[GET_SCHED][0]:
        system_id, zone_index = kwargs[GET_SCHED]
        sys_entry = gateway.device_registry.system_by_id[system_id]
        zone = (
            sys_entry.dhw
            if zone_index == "HW"
            else sys_entry.zone_by_index[zone_index]
        )
        assert zone
        schedule = zone.schedule

        if schedule is None:
            print("Failed to get the schedule.")
        else:
            result = {SZ_ZONE_INDEX: zone_index, "schedule": schedule}
            print(">>> Schedule JSON begins <<<")
            print(json.dumps(result, indent=4))
            print(">>> Schedule JSON ended <<<")

    if kwargs[SET_SCHED][0]:
        system_id, _ = kwargs[GET_SCHED]


def _write_state(schema: Mapping[str, object], msgs: dict[str, str]) -> None:
    """Write the state to the file system (blocking)."""
    with open("state_msgs.log", "w") as f:
        for dtm, packet_data in msgs.items():
            f.write(f"{dtm} {packet_data}\r\n")  # if not m._expired

    with open("state_schema.json", "w") as f:
        f.write(json.dumps(schema, indent=4))


async def _save_state(gateway: Gateway) -> None:
    """Save the gateway state to files.

    :param gateway: The gateway instance.
    """
    schema, msgs = await gateway.get_state()
    await asyncio.to_thread(_write_state, schema, msgs)


async def _print_engine_state(  # type: ignore[explicit-any]  # heterogeneous click kwargs
    gateway: Gateway,
    **kwargs: Any,
) -> None:
    """Print the current engine state (schema and packets).

    :param gateway: The gateway instance.
    :param kwargs: Command arguments to determine verbosity.
    """
    (schema, packets) = await gateway.get_state(include_expired=True)

    if kwargs["print_state"] > 0:
        print(f"schema: {json.dumps(schema, indent=4)}\r\n")
    if kwargs["print_state"] > 1:
        print(f"packets: {json.dumps(packets, indent=4)}\r\n")


async def print_summary(  # type: ignore[explicit-any]  # heterogeneous click kwargs
    gateway: Gateway,
    **kwargs: Any,
) -> None:
    """Print a summary of the system state, schema, params, and status.

    :param gateway: The gateway instance.
    :param kwargs: Command arguments to determine what to display.
    """
    entity = gateway.tcs or gateway

    if kwargs.get("show_schema"):
        print(
            f"Schema[{entity}] = {json.dumps(await entity.schema(), indent=4)}\r\n"
        )

        # schema = {d.id: await d.schema() for d in sorted(gateway.device_registry.devices)}
        # print(f"Schema[devices] = {json.dumps({'schema': schema}, indent=4)}\r\n")

    if kwargs.get("show_params"):
        print(
            f"Params[{entity}] = {json.dumps(await entity.params(), indent=4)}\r\n"
        )

        params = {
            d.id: await d.params()
            for d in sorted(gateway.device_registry.devices)
        }
        print(
            f"Params[devices] = {json.dumps({'params': params}, indent=4)}\r\n"
        )

    if kwargs.get("show_status"):
        print(
            f"Status[{entity}] = {json.dumps(await entity.status(), indent=4)}\r\n"
        )

        status = {
            d.id: await d.status()
            for d in sorted(gateway.device_registry.devices)
        }
        print(
            f"Status[devices] = {json.dumps({'status': status}, indent=4)}\r\n"
        )

    if kwargs.get("show_knowns"):  # show device hints (show-knowns)
        known = json.dumps(gateway.config.known_list, indent=4)
        print(f"allow_list (hints) = {known}\r\n")

    if kwargs.get("show_traits"):  # show device traits
        result = {
            # {k: v for k, v in (await d.traits()).items() if k[:1] == "_"}
            d.id: await d.traits()
            for d in sorted(gateway.device_registry.devices)
        }
        print(json.dumps(result, indent=4), "\r\n")

    if kwargs.get("show_crazys"):
        for device in [
            d
            for d in gateway.device_registry.devices
            if d.type == DEV_TYPE_MAP.CTL
        ]:
            for m in await device.entity_state.get_all_messages():
                if m.code in (Code._0005, Code._000C):
                    print(f"{m}")
            print()
        for device in [
            d
            for d in gateway.device_registry.devices
            if d.type == DEV_TYPE_MAP.UFC
        ]:
            for m in await device.entity_state.get_all_messages():
                print(f"{m}")
            print()


async def async_main(  # type: ignore[explicit-any]  # heterogeneous click kwargs
    command: str,
    lib_kwargs: CliKwargsT,
    **kwargs: Any,
) -> None:
    """Execute the main asynchronous logic for the CLI.

    :param command: The command to execute (e.g., "monitor", "parse").
    :param lib_kwargs: Configuration arguments for the library.
    :param kwargs: Additional CLI arguments.
    """

    async def handle_msg(dto: PacketDTO) -> None:
        """Process the message as it arrives (a callback).

        In this case, the message is merely printed.
        """
        _msg = Message(dto)

        if kwargs["long_format"]:  # HACK for test/dev
            print(
                f"{_msg.dtm.isoformat(timespec='microseconds')} ... "
                f"{_msg!r}  # {_msg.payload}"  # or f'  # ("{msg.src!r}", "{msg.dst!r}")'
            )
            return

        if kwargs["json_format"]:
            out = {}
            out["timestamp"] = _msg.dtm.timestamp()
            out["type"] = _msg.verb
            out["source_id"] = (
                str(_msg.src.id) if str(_msg.src.id) != "--:------" else None
            )
            out["destination_id"] = (
                str(_msg.dst.id) if str(_msg.dst.id) != "--:------" else None
            )
            out["code"] = _msg.code
            out["payload"] = _msg.payload
            print(json.dumps(out))
            return

        dtm = f"{_msg.dtm:%H:%M:%S.%f}"[:-3]
        con_cols = CONSOLE_COLS

        if _msg.code == Code._PUZZ:
            print(f"{Style.BRIGHT}{Fore.YELLOW}{dtm} {_msg}"[:con_cols])
        elif _msg.src and _msg.src.type == DEV_TYPE_MAP.HGI:
            print(
                f"{Style.BRIGHT}{COLORS.get(_msg.verb)}{dtm} {_msg}"[:con_cols]
            )
        elif _msg.code == Code._1F09 and _msg.verb == I_:
            print(f"{Fore.YELLOW}{dtm} {_msg}"[:con_cols])
        elif (
            _msg.code in (Code._000A, Code._2309, Code._30C9)
            and _msg._has_array
        ):
            print(f"{Fore.YELLOW}{dtm} {_msg}"[:con_cols])
        else:
            print(f"{COLORS.get(_msg.verb)}{dtm} {_msg}"[:con_cols])

    serial_port, lib_kwargs = normalise_config(lib_kwargs)  # type: ignore[assignment]

    if kwargs["restore_schema"]:
        print(" - restoring client schema from a HA cache...")
        state = json.load(kwargs["restore_schema"])["data"]["client_state"]
        lib_kwargs = lib_kwargs | state["schema"]

    # Explicitly extract input_file if present to ensure it's passed as a named arg
    input_file = lib_kwargs.pop(SZ_INPUT_FILE, None)

    # Extract the configuration variables targeting the GatewayConfig Dataclass
    config_dict = lib_kwargs.pop(SZ_CONFIG, {})

    gwy_config_kwargs: CliKwargsT = {}

    # 1. Map inner SZ_CONFIG keys
    for key, config_val in config_dict.items():
        if key == SZ_REDUCE_PROCESSING and config_val is not None:
            gwy_config_kwargs[key] = int(config_val)
        else:
            gwy_config_kwargs[key] = config_val

    # 2. Extract known GatewayConfig keys from lib_kwargs
    known_config_keys = {
        "packet_log",
        "port_config",
        "block_list",
        "known_list",
        "hgi_id",
        "debug_mode",
        "disable_sending",
        "disable_qos",
        "enforce_known_list",
        "evofw_flag",
    }
    for key in list(lib_kwargs.keys()):
        if key in known_config_keys:
            gwy_config_kwargs[key] = lib_kwargs.pop(key)

    if input_file is not None:
        gwy_config_kwargs["input_file"] = input_file

    # 3. Whatever remains in lib_kwargs represents schema parameters
    if lib_kwargs:
        gwy_config_kwargs["schema"] = lib_kwargs

    # Separate the kwargs for the engine vs the gateway
    # Exclude L7 traits completely from the Engine constructor
    l7_only_keys = {"known_list", "block_list", "hgi_id"}

    engine_fields = {f.name for f in dataclasses.fields(EngineConfig)}
    gwy_fields = {f.name for f in dataclasses.fields(GatewayConfig)}

    engine_kwargs = {
        k: v
        for k, v in gwy_config_kwargs.items()
        if k in engine_fields and k not in l7_only_keys
    }
    gwy_kwargs = {
        k: v for k, v in gwy_config_kwargs.items() if k in gwy_fields
    }

    gwy_config = GatewayConfig(
        engine=EngineConfig(**engine_kwargs), **gwy_kwargs
    )

    # Instantiate Gateway
    gwy = Gateway(
        serial_port,
        config=gwy_config,
    )  # passes action to gateway

    if gwy_config.reduce_processing < DONT_CREATE_MESSAGES:
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

        if command == EXECUTE:  # 3/5
            tasks = spawn_scripts(gwy, **kwargs)
            await asyncio.gather(*tasks)

        elif command == MONITOR:  # 2/5
            _ = spawn_scripts(gwy, **kwargs)
            await gwy._engine._protocol.wait_for_connection_lost(timeout=86400)
            # timeout set to timeout=86400, to stop type checker complaint if sent to None

        elif command == PARSE:  # 1/5
            # Wait indefinitely until EOF closes the transport
            await gwy._engine._protocol.wait_for_connection_lost(timeout=86400)
            # timeout set to timeout=86400, to stop type checker complaint if sent to None

        elif command == LISTEN:  # 4/5
            await gwy._engine._protocol.wait_for_connection_lost(timeout=86400)
            # timeout set to timeout=86400, to stop type checker complaint if sent to None

        elif command == SCAN:  # 5/5
            scan_engine = DiscoveryScan(gwy)
            scan_engine.start()
            duration = kwargs.get("scan_duration", 300)
            if duration > 0:
                print(f" - scanning for {duration} seconds...")
                await asyncio.sleep(duration)
            else:
                print(" - scanning until interrupted (Ctrl-C)...")
                await gwy._engine._protocol.wait_for_connection_lost(
                    timeout=86400
                )
            scan_engine.stop()
            _print_scan_results(scan_engine, kwargs.get("scan_output"))

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
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            # Task cancellation is expected during shutdown, especially
            # after EOF
            await asyncio.wait_for(gwy.stop(), timeout=7.0)

    print(f"\r\nclient.py: Engine stopped: {msg}")

    # if kwargs["save_state"]:
    #    await _save_state(gwy)

    if kwargs["print_state"]:
        await _print_engine_state(gwy, **kwargs)

    elif command == EXECUTE:
        print_results(gwy, **kwargs)

    await print_summary(gwy, **kwargs)


cli.add_command(parse)
cli.add_command(monitor)
cli.add_command(execute)
cli.add_command(listen)
cli.add_command(scan)
cli.add_command(decode)


def _run_cli() -> None:
    """Run the CLI via asyncclick and execute the application engine."""
    try:
        result = cli(standalone_mode=False)
    except ClickException as err:
        err.show()
        sys.exit(err.exit_code)
    except Exit as err:
        sys.exit(err.exit_code)

    if isinstance(result, int):
        sys.exit(result)

    (command, lib_kwargs, kwargs) = result
    asyncio.run(async_main(command, lib_kwargs, **kwargs))


def main() -> None:  # pragma: no cover
    """Entry point for the CLI.

    Parses arguments, sets up the event loop (including Windows-specific
    policies), and runs the main loop (optionally with profiling).
    """
    print("\r\nclient.py: Starting ramses_rf...")

    if sys.platform == "win32":
        print(" - event_loop_policy set for win32")  # do before asyncio.run()
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    profile = None
    try:
        if _PROFILE_LIBRARY:
            profile = cProfile.Profile()
            profile.run("_run_cli()")
        else:
            _run_cli()
    except KeyboardInterrupt:  # , SystemExit):
        print("\r\nclient.py: Engine stopped: ended via: KeyboardInterrupt")

    if _PROFILE_LIBRARY:
        ps = pstats.Stats(profile)
        ps.sort_stats(pstats.SortKey.TIME).print_stats(20)

    print(" - finished ramses_rf.\r\n")


if __name__ == "__main__":  # pragma: no cover
    main()
