#!/usr/bin/env python3
"""Unittests for the ramses_cli client.py class."""

import asyncio
import io
import json
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import click
import pytest
from click.testing import CliRunner

from ramses_cli.client import (
    EXECUTE,
    LISTEN,
    MONITOR,
    PARSE,
    SZ_INPUT_FILE,
    DeviceIdParamType,
    _print_engine_state,
    _save_state,
    async_main,
    cli,
    normalise_config,
    print_results,
    print_summary,
    split_kwargs,
)
from ramses_rf import GracefulExit
from ramses_rf.const import DEV_TYPE_MAP, I_, Code
from ramses_rf.database import MessageIndex
from ramses_rf.gateway import Gateway
from ramses_rf.schemas import SZ_CONFIG, SZ_DISABLE_DISCOVERY
from ramses_tx import exceptions as exc
from ramses_tx.message import Message
from ramses_tx.schemas import SZ_PACKET_LOG, SZ_SERIAL_PORT

STDIN = io.StringIO("053  I --- 01:123456 --:------ 01:123456 3150 002 FC00\r\n")
CMD = "RQ 01:123456 1F09 00"


@pytest.fixture
async def mock_gateway() -> AsyncGenerator[MagicMock, None]:
    """Create a mock Gateway instance for testing."""
    gateway = MagicMock(spec=Gateway)
    # Fix: Explicitly assign a MagicMock to __str__ and tell mypy to ignore the method assignment
    gateway.__str__ = MagicMock(return_value="Gateway")  # type: ignore[method-assign]

    gateway.send_cmd = AsyncMock()
    gateway.dispatcher = MagicMock()
    gateway.dispatcher.send = MagicMock()
    gateway.start = AsyncMock()
    gateway.stop = AsyncMock()
    gateway.get_state = MagicMock(return_value=({}, {}))
    gateway._restore_cached_packets = AsyncMock()
    gateway.add_msg_handler = MagicMock()

    # Fix: Explicitly mock the private protocol attribute
    gateway._protocol = MagicMock()

    # Fix: Create a future attached to the running loop.
    loop = asyncio.get_running_loop()

    future: asyncio.Future[None] = loop.create_future()
    future.set_result(None)
    gateway._protocol._wait_connection_lost = future

    # Add required attributes
    gateway.config = MagicMock()
    gateway.config.disable_discovery = False
    gateway.config.enable_eavesdrop = False
    gateway._loop = MagicMock()
    gateway._loop.call_soon = MagicMock()
    gateway._loop.call_later = MagicMock()
    gateway._loop.time = MagicMock(return_value=0.0)
    gateway._include = {}

    # Mock devices for print_summary
    mock_dev = MagicMock()
    mock_dev.id = "01:123456"
    mock_dev.type = DEV_TYPE_MAP.CTL  # Controller
    mock_dev.schema = {"mock": "schema"}
    mock_dev.params = {"mock": "params"}
    mock_dev.status = {"mock": "status"}
    mock_dev.traits = {"mock": "traits"}
    # Mock message database interaction for show_crazys
    mock_dev._msgz = {
        Code._0005: {"verb": {"pkt": "msg_0005"}},
        Code._000C: {"verb": {"pkt": "msg_000C"}},
    }

    gateway.devices = [mock_dev]
    gateway.tcs = None  # mimic no TCS
    gateway.schema = {"global": "schema"}
    gateway.params = {"global": "params"}
    gateway.status = {"global": "status"}

    # Add msg_db attribute
    gateway.msg_db = MessageIndex(maintain=False)

    # Mock system_by_id for print_results
    mock_sys = MagicMock()
    mock_sys.dhw.schedule = [{"day": "Monday"}]
    mock_sys.zone_by_idx = {"01": MagicMock(schedule=[{"day": "Tuesday"}])}
    # Fix: Use integer key for faultlog to match expectations of print_results
    mock_sys._faultlog.faultlog = {0: "fault_data"}
    gateway.system_by_id = {"01:123456": mock_sys}

    yield gateway


# --- CLI Argument Parsing Tests ---


def test_parse_no_input() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["parse"])
    assert result.exit_code == 2  # missing input file
    assert result.output.startswith("Usage: cli parse [OPTIONS] INPUT_FILE")


def test_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", STDIN)
    runner = CliRunner()
    result = runner.invoke(cli, ["parse", "-"])
    assert result.exit_code == 0  # OK input file supplied
    assert result.output == ""


def test_monitor_no_port() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["monitor"])
    assert result.exit_code == 2  # missing port name
    assert result.output.startswith("Usage: cli monitor [OPTIONS] SERIAL_PORT")


def test_monitor(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["monitor", "nullmodem"])
    assert result.exit_code == 0  # OK port name supplied
    assert "discovery is enabled" in result.output


def test_monitor_no_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test monitor with explicit no-discovery flag."""
    runner = CliRunner()
    result = runner.invoke(cli, ["monitor", "nullmodem", "--no-discover"])
    assert result.exit_code == 0
    assert "discovery is enabled" not in result.output


def test_execute_no_arg() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["execute"])
    assert result.exit_code == 2  # missing command, port
    assert result.output.startswith("Usage: cli execute [OPTIONS] SERIAL_PORT")


def test_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["execute", CMD])
    assert result.exit_code == 0  # OK command supplied
    assert result.output == " - discovery is force-disabled\n"


def test_listen_no_arg() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["listen"])
    assert result.exit_code == 2  # missing port
    assert result.output.startswith("Usage: cli listen [OPTIONS] SERIAL_PORT")


def test_listen(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["listen", "nullmodem"])
    assert result.exit_code == 0  # OK port supplied
    assert result.output == " - sending is force-disabled\n"


# --- Unit Tests for Logic Functions ---


def test_normalise_config() -> None:
    """Test the configuration normalization logic."""
    # Case 1: Packet log as string
    lib_config: dict[str, Any] = {
        SZ_SERIAL_PORT: "/dev/ttyUSB0",
        SZ_PACKET_LOG: "packet.log",
    }
    port, cfg = normalise_config(lib_config)
    assert port == "/dev/ttyUSB0"
    assert cfg is not None
    assert cfg[SZ_PACKET_LOG]["file_name"] == "packet.log"

    # Case 2: Packet log as dict
    lib_config = {SZ_PACKET_LOG: {"file_name": "packet.log", "rotate": True}}
    port, cfg = normalise_config(lib_config)
    assert port is None
    assert cfg is not None
    assert cfg[SZ_PACKET_LOG]["rotate"] is True


def test_split_kwargs() -> None:
    """Test splitting CLI kwargs from library kwargs."""
    # Setup some mixed kwargs
    kwargs = {
        "debug_mode": 1,
        SZ_SERIAL_PORT: "/dev/ttyUSB0",
        "some_lib_config": True,
    }

    # We need to rely on the actual constants imported in client.py
    # SZ_SERIAL_PORT is in LIB_KEYS.

    obj: tuple[dict[str, Any], dict[str, Any]] = ({}, {SZ_CONFIG: {}})
    cli_args, lib_args = split_kwargs(obj, kwargs)

    # Serial port should move to lib_args
    assert SZ_SERIAL_PORT in lib_args
    assert lib_args[SZ_SERIAL_PORT] == "/dev/ttyUSB0"

    # Debug mode should stay in cli_args
    assert "debug_mode" in cli_args


@pytest.mark.asyncio
async def test_print_summary(
    mock_gateway: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test the summary printing function with various flags."""
    # Mock msg_db to be None to trigger the alternative branch in show_crazys
    mock_gateway.msg_db = None

    kwargs = {
        "show_schema": True,
        "show_params": True,
        "show_status": True,
        "show_knowns": True,
        "show_traits": True,
        "show_crazys": True,
    }

    print_summary(mock_gateway, **kwargs)

    captured = capsys.readouterr()
    output = captured.out

    assert "Schema[Gateway]" in output
    assert "Params[Gateway]" in output
    assert "Status[Gateway]" in output
    assert "allow_list (hints)" in output
    assert '"mock": "traits"' in output
    # Check for crazy output from the mocked _msgz
    assert "msg_0005" in output


def test_print_results(
    mock_gateway: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test print_results with faults and schedules."""

    # Test Faults
    kwargs: dict[str, Any] = {
        "get_faults": "01:123456",
        "get_schedule": [None, None],
        "set_schedule": [None, None],
    }
    print_results(mock_gateway, **kwargs)
    out = capsys.readouterr().out
    assert "fault_data" in out

    # Test DHW Schedule
    kwargs = {
        "get_faults": None,
        "get_schedule": ["01:123456", "HW"],
        "set_schedule": [None, None],
    }
    print_results(mock_gateway, **kwargs)
    out = capsys.readouterr().out
    assert "Monday" in out

    # Test Zone Schedule
    kwargs = {
        "get_faults": None,
        "get_schedule": ["01:123456", "01"],
        "set_schedule": [None, None],
    }
    print_results(mock_gateway, **kwargs)
    out = capsys.readouterr().out
    assert "Tuesday" in out


def test_print_engine_state(
    mock_gateway: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test _print_engine_state."""
    kwargs = {"print_state": 2}  # 2 implies print packets as well
    _print_engine_state(mock_gateway, **kwargs)

    out = capsys.readouterr().out
    assert "schema" in out
    assert "packets" in out


@pytest.mark.asyncio
async def test_async_main_parse(mock_gateway: MagicMock) -> None:
    """Test async_main logic for the PARSE command."""
    lib_kwargs = {SZ_CONFIG: {"reduce_processing": 0}, SZ_PACKET_LOG: {}}
    kwargs = {
        "long_format": False,
        "restore_schema": None,
        "restore_state": None,
        "print_state": 0,
        SZ_INPUT_FILE: "input.log",
    }

    with (
        patch("ramses_cli.client.Gateway", return_value=mock_gateway),
        patch("ramses_cli.client.normalise_config", return_value=(None, lib_kwargs)),
    ):
        await async_main(PARSE, lib_kwargs, **kwargs)

        mock_gateway.start.assert_awaited_once()
        mock_gateway.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_main_execute(mock_gateway: MagicMock) -> None:
    """Test async_main logic for the EXECUTE command."""
    lib_kwargs = {
        SZ_CONFIG: {
            "reduce_processing": 0,
            SZ_DISABLE_DISCOVERY: True,
        },
        SZ_PACKET_LOG: {},
    }
    kwargs = {
        "long_format": False,
        "restore_schema": None,
        "restore_state": None,
        "print_state": 0,
        "exec_cmd": "RQ 01:123456 1F09 00",
        "exec_scr": None,
        "get_faults": None,
        "get_schedule": [None, None],
        "set_schedule": [None, None],
        # CLI functions like print_results expect these
    }

    # Fix: Define a real awaitable to return, not an AsyncMock object
    async def mock_task() -> None:
        pass

    with (
        patch("ramses_cli.client.Gateway", return_value=mock_gateway),
        patch("ramses_cli.client.spawn_scripts", return_value=[mock_task()]),
        patch("ramses_cli.client.normalise_config", return_value=(None, lib_kwargs)),
    ):
        await async_main(EXECUTE, lib_kwargs, **kwargs)

        mock_gateway.start.assert_awaited_once()
        mock_gateway.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_main_monitor(mock_gateway: MagicMock) -> None:
    """Test async_main logic for the MONITOR command."""
    lib_kwargs = {SZ_CONFIG: {"reduce_processing": 0}, SZ_PACKET_LOG: {}}
    kwargs = {
        "long_format": False,
        "restore_schema": None,
        "restore_state": None,
        "print_state": 0,
        "exec_scr": None,  # Simple monitor
    }

    with (
        patch("ramses_cli.client.Gateway", return_value=mock_gateway),
        # Fix: Return empty list for monitor since it doesn't await the tasks, preventing warnings
        patch("ramses_cli.client.spawn_scripts", return_value=[]),
        patch("ramses_cli.client.normalise_config", return_value=(None, lib_kwargs)),
    ):
        await async_main(MONITOR, lib_kwargs, **kwargs)

        mock_gateway.start.assert_awaited_once()
        mock_gateway.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_main_restore_schema(mock_gateway: MagicMock) -> None:
    """Test async_main with restore_schema functionality."""
    lib_kwargs = {SZ_CONFIG: {"reduce_processing": 0}}

    # Mock a file object for restore_schema
    mock_file = MagicMock()
    mock_file.read.return_value = json.dumps(
        {"data": {"client_state": {"schema": {"restored": "value"}, "packets": []}}}
    )
    # Make json.load work on the mock
    mock_file.__enter__.return_value = mock_file

    kwargs = {
        "long_format": False,
        "restore_schema": mock_file,
        "restore_state": None,
        "print_state": 0,
    }

    with (
        patch("ramses_cli.client.Gateway", return_value=mock_gateway),
        patch(
            "json.load",
            return_value={"data": {"client_state": {"schema": {}, "packets": []}}},
        ),
        patch("ramses_cli.client.normalise_config", return_value=(None, lib_kwargs)),
    ):
        await async_main(LISTEN, lib_kwargs, **kwargs)
        # Verify gateway initialized (implicit in logic)
        mock_gateway.start.assert_awaited()


@pytest.mark.asyncio
async def test_async_main_msg_handler(
    mock_gateway: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test the internal handle_msg callback logic inside async_main."""
    lib_kwargs = {SZ_CONFIG: {"reduce_processing": 0}, SZ_PACKET_LOG: {}}
    kwargs = {
        "long_format": False,
        "restore_schema": None,
        "restore_state": None,
        "print_state": 0,
    }

    # We need to capture the callback passed to add_msg_handler
    captured_callback = None

    def capture_cb(cb: Any) -> None:
        nonlocal captured_callback
        captured_callback = cb

    mock_gateway.add_msg_handler.side_effect = capture_cb

    with (
        patch("ramses_cli.client.Gateway", return_value=mock_gateway),
        patch("ramses_cli.client.normalise_config", return_value=(None, lib_kwargs)),
    ):
        # Run main to trigger registration
        await async_main(PARSE, lib_kwargs, **kwargs)

        assert captured_callback is not None

        # Now test the callback with different message types
        # 1. Puzzle message
        msg1 = MagicMock(spec=Message)
        msg1.dtm = datetime.now()
        msg1.code = Code._PUZZ
        # Mypy dislikes assigning to method slots on mocks without ignore
        msg1.__repr__ = MagicMock(return_value="PUZZLE_MSG")  # type: ignore[method-assign]
        captured_callback(msg1)
        out = capsys.readouterr().out
        assert "PUZZLE_MSG" in out

        # 2. 1F09 (I) message
        # Use a fresh mock object to avoid state pollution
        msg2 = MagicMock(spec=Message)
        msg2.dtm = datetime.now()
        msg2.code = Code._1F09
        msg2.verb = I_
        # Fix: Ensure src attribute exists for HGI check in handle_msg
        msg2.src = MagicMock()
        msg2.src.type = "01"  # Controller type, definitely not HGI
        msg2.__repr__ = MagicMock(return_value="1F09_MSG")  # type: ignore[method-assign]

        captured_callback(msg2)
        out = capsys.readouterr().out
        assert "1F09_MSG" in out


@pytest.mark.asyncio
async def test_async_main_long_format(
    mock_gateway: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test the long_format output branch in handle_msg."""
    lib_kwargs = {SZ_CONFIG: {"reduce_processing": 0}, SZ_PACKET_LOG: {}}
    kwargs = {
        "long_format": True,  # ENABLE LONG FORMAT
        "restore_schema": None,
        "restore_state": None,
        "print_state": 0,
    }

    # Capture the callback
    captured_callback: Any = None

    def capture_cb(cb: Any) -> None:
        nonlocal captured_callback
        captured_callback = cb

    mock_gateway.add_msg_handler.side_effect = capture_cb

    with (
        patch("ramses_cli.client.Gateway", return_value=mock_gateway),
        patch("ramses_cli.client.normalise_config", return_value=(None, lib_kwargs)),
    ):
        await async_main(PARSE, lib_kwargs, **kwargs)

        # Trigger callback
        msg = MagicMock(spec=Message)
        msg.dtm = datetime.now()
        msg.__repr__ = MagicMock(return_value="LONG_MSG")  # type: ignore[method-assign]
        msg.payload = "PAYLOAD"

        assert captured_callback is not None
        captured_callback(msg)

        out = capsys.readouterr().out
        # Verify long format output (timestamp ... repr # payload)
        assert "LONG_MSG" in out
        assert "..." in out
        assert "# PAYLOAD" in out


@pytest.mark.asyncio
async def test_async_main_exceptions(
    mock_gateway: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test exception handling in async_main."""
    lib_kwargs = {SZ_CONFIG: {"reduce_processing": 0}, SZ_PACKET_LOG: {}}
    kwargs = {
        "long_format": False,
        "restore_schema": None,
        "restore_state": None,
        "print_state": 0,
    }

    with (
        patch("ramses_cli.client.Gateway", return_value=mock_gateway),
        patch("ramses_cli.client.normalise_config", return_value=(None, lib_kwargs)),
    ):
        # Test CancelledError
        mock_gateway.start.side_effect = asyncio.CancelledError
        await async_main(PARSE, lib_kwargs, **kwargs)
        out = capsys.readouterr().out
        assert "CancelledError" in out

        # Test GracefulExit
        mock_gateway.start.side_effect = GracefulExit
        await async_main(PARSE, lib_kwargs, **kwargs)
        out = capsys.readouterr().out
        assert "GracefulExit" in out

        # Test RamsesException
        mock_gateway.start.side_effect = exc.RamsesException("Test Error")
        await async_main(PARSE, lib_kwargs, **kwargs)
        out = capsys.readouterr().out
        assert "RamsesException" in out
        assert "Test Error" in out


def test_convert() -> None:
    """Test DeviceIdParamType handling of invalid IDs."""
    param_type = DeviceIdParamType()

    # Test valid ID
    assert param_type.convert("01:123456", None, None) == "01:123456"

    # Test invalid ID raises click.BadParameter
    with pytest.raises(click.BadParameter):
        param_type.convert("invalid", None, None)


def test_cli_debug_mode(mock_gateway: MagicMock) -> None:
    """Test that the debug flag triggers the debugger."""
    with patch("ramses_cli.client.start_debugging") as mock_debug:
        runner = CliRunner()
        # invoke cli with -z (debug) count 1
        runner.invoke(cli, ["-z", "parse", "/dev/null"])
        mock_debug.assert_called_once_with(True)


def test_cli_config_file(mock_gateway: MagicMock) -> None:
    """Test loading a configuration file via the CLI."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        # Create a dummy config file
        with open("test_config.json", "w") as f:
            json.dump({"config": {"disable_discovery": True}}, f)

        # Run CLI with -c pointing to the file
        result = runner.invoke(cli, ["-c", "test_config.json", "parse", "/dev/null"])

        assert result.exit_code == 0
        # Verify the config was actually merged (by checking the internal call)
        # Note: We rely on normalise_config patching in async_main tests usually,
        # but here we just want to ensure it runs without error.


def test_execute_flags(mock_gateway: MagicMock) -> None:
    """Test that execute flags (like --get-faults) enforce the known_list."""
    runner = CliRunner()

    # Running execute with a specific device target should force-enable the known_list
    result = runner.invoke(cli, ["execute", "/dev/null", "--get-faults", "01:123456"])

    assert result.exit_code == 0
    # This specific string is printed when execute logic enforces the list
    assert "known list is force-configured" in result.output


@pytest.mark.asyncio
async def test__save_state(mock_gateway: MagicMock) -> None:
    """Test _save_state writes schema and packets to files."""
    # NOTE: Converted to async to ensure event_loop exists for mock_gateway fixture
    # Setup mock gateway state
    mock_gateway.get_state.return_value = (
        {"schema_key": "schema_data"},
        {"2023-01-01T00:00:00": "pkt_line"},
    )

    with patch("builtins.open", new_callable=mock_open) as mock_file:
        _save_state(mock_gateway)

        # Verify open was called twice (once for log, once for json)
        assert mock_file.call_count == 2

        # Check that expected files were opened
        calls = mock_file.call_args_list
        filenames = [c[0][0] for c in calls]
        assert "state_msgs.log" in filenames
        assert "state_schema.json" in filenames


def test_parse_command_passes_input_file() -> None:
    """Verify that 'client.py parse' correctly puts input_file into lib_kwargs."""

    runner = CliRunner()
    fake_log_file = "test_capture.log"

    # standalone_mode=False allows us to see the return value of the command
    result = runner.invoke(cli, ["parse", fake_log_file], standalone_mode=False)

    assert result.exit_code == 0, f"Command failed: {result.exception}"

    # The 'parse' command returns a tuple: (command_name, lib_kwargs, cli_kwargs)
    command, lib_kwargs, cli_kwargs = result.return_value

    # VERIFY: Did the CLI put the filename into the correct config key?
    assert lib_kwargs.get("input_file") == fake_log_file, (
        f"Expected input_file='{fake_log_file}', got {lib_kwargs.get('input_file')}"
    )

    print("\nSuccess: CLI parsed the input file argument correctly.")
