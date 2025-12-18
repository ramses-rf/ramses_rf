#!/usr/bin/env python3
"""RAMSES RF - Test the CLI utility."""

import io
import sys

import pytest

sys.path.append("../tests")  # HACK: to access client.py

try:
    import colorama  # noqa: F401
except ModuleNotFoundError as err:
    pytest.skip(f"{err}", allow_module_level=True)  # No module named 'colorama'


from ramses_cli import _DBG_FORCE_CLI_DEBUGGING  # noqa: E402
from ramses_cli.client import PARSE, cli  # noqa: E402

# TODO: add tests for:
# client execute /dev/ttyACM0 -x "RQ 01:145038 1F09 00"
# client execute /dev/ttyACM0 --get-schedule 01:145038 01


if _DBG_FORCE_CLI_DEBUGGING:
    pytest.skip(
        f"_DBG_FORCE_CLI_DEBUGGING = {_DBG_FORCE_CLI_DEBUGGING}",
        allow_module_level=True,
    )


STDIN = io.StringIO("053  I --- 01:123456 --:------ 01:123456 3150 002 FC00\r\n")

CLI_CONFIG_BASE = {
    "debug_mode": 0,
    "restore_schema": None,
    "restore_state": None,
    "long_format": False,
    "print_state": 0,
    "show_schema": False,
    "show_params": False,
    "show_status": False,
    "show_knowns": False,
    "show_traits": False,
    "show_crazys": False,
}

CLI_CONFIG_EXECUTE = CLI_CONFIG_BASE | {
    "discover": None,
    "exec_cmd": None,
    "get_faults": None,
    "get_schedule": (None, None),
    "set_schedule": (None, None),
}
CLI_CONFIG_MONITOR = CLI_CONFIG_BASE | {
    "exec_cmd": None,
    "exec_scr": None,
    "poll_devices": None,
}
CLI_CONFIG_LISTEN_ = CLI_CONFIG_BASE
CLI_CONFIG_PARSE__ = CLI_CONFIG_BASE

LIB_CONFIG_BASE = {
    "config": {"reduce_processing": 0, "evofw_flag": None, "disable_discovery": False},
    "serial_port": None,
    "packet_log": None,
}

LIB_CONFIG_EXECUTE = {
    "config": {
        "reduce_processing": 0,
        "evofw_flag": None,
        "disable_discovery": True,
        "disable_qos": False,  # the client enforces this
    },
    "serial_port": "/dev/ttyUSB0",
    "packet_log": None,
}
LIB_CONFIG_MONITOR = {
    "config": {"reduce_processing": 0, "evofw_flag": None, "disable_discovery": False},
    "serial_port": "/dev/ttyUSB0",
    "packet_log": None,
}
LIB_CONFIG_LISTEN_ = {
    "config": {"reduce_processing": 0, "evofw_flag": None, "disable_sending": True},
    "serial_port": "/dev/ttyUSB0",
    "packet_log": None,
}
LIB_CONFIG_PARSE__ = {
    "config": {"reduce_processing": 0},
    "input_file": "-",  # will be replaced by sys.stdin by fileinput
}

BASIC_TESTS = (  # can't use "-z"
    (["client.py", "execute", "/dev/ttyUSB0"], CLI_CONFIG_EXECUTE, LIB_CONFIG_EXECUTE),
    (["client.py", "monitor", "/dev/ttyUSB0"], CLI_CONFIG_MONITOR, LIB_CONFIG_MONITOR),
    (["client.py", "listen", "/dev/ttyUSB0"], CLI_CONFIG_LISTEN_, LIB_CONFIG_LISTEN_),
    (
        ["client.py", "parse", "filename-stand_in"],
        CLI_CONFIG_PARSE__,
        LIB_CONFIG_PARSE__,
    ),
)


def id_fnc(param: int) -> str:
    return f"{BASIC_TESTS[param][0][1]:7}"


@pytest.mark.parametrize("index", range(len(BASIC_TESTS)), ids=id_fnc)
def test_client_basic(
    monkeypatch: pytest.MonkeyPatch, index: int, tests: tuple = BASIC_TESTS
) -> None:
    monkeypatch.setattr("sys.argv", tests[index][0])
    if tests[index][0][1] == PARSE:
        monkeypatch.setattr("sys.stdin", STDIN)

    cmd_string, lib_config, cli_config = cli(standalone_mode=False)

    if lib_config.get("input_file"):
        lib_config["input_file"] = LIB_CONFIG_PARSE__["input_file"]

    assert cmd_string == tests[index][0][1]
    assert cli_config == tests[index][1]
    assert lib_config == tests[index][2]
