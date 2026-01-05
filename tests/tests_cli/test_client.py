#!/usr/bin/env python3
"""Unittests for the ramses_cli client.py class."""

import io
import json
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import click
import pytest
from click.testing import CliRunner

from ramses_cli.client import (
    EXECUTE,
    LISTEN,
    PARSE,
    SZ_INPUT_FILE,
    DeviceIdParamType,
    _save_state,
    async_main,
    cli,
    normalise_config,
    print_summary,
)
from ramses_rf.database import MessageIndex
from ramses_rf.gateway import Gateway
from ramses_rf.schemas import SZ_CONFIG, SZ_DISABLE_DISCOVERY
from ramses_tx.schemas import SZ_PACKET_LOG, SZ_SERIAL_PORT

STDIN = io.StringIO("053  I --- 01:123456 --:------ 01:123456 3150 002 FC00\r\n")
CMD = "RQ 01:123456 1F09 00"


@pytest.fixture
def mock_gateway() -> Generator[MagicMock, None, None]:
    """Create a mock Gateway instance for testing."""
    gateway = MagicMock(spec=Gateway)
    gateway.send_cmd = AsyncMock()
    gateway.dispatcher = MagicMock()
    gateway.dispatcher.send = MagicMock()
    gateway.start = AsyncMock()
    gateway.stop = AsyncMock()
    gateway.get_state = MagicMock(return_value=({}, {}))
    gateway._restore_cached_packets = AsyncMock()

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
    mock_dev.schema = {"mock": "schema"}
    mock_dev.params = {"mock": "params"}
    mock_dev.status = {"mock": "status"}
    mock_dev.traits = {"mock": "traits"}
    gateway.devices = [mock_dev]
    gateway.tcs = None  # mimic no TCS
    gateway.schema = {"global": "schema"}
    gateway.params = {"global": "params"}
    gateway.status = {"global": "status"}

    # Add msg_db attribute
    gateway.msg_db = MessageIndex(maintain=False)

    yield gateway


# --- CLI Argument Parsing Tests (Existing) ---


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
    assert result.output == " - discovery is enabled\n"


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


# --- Unit Tests for Logic Functions (New) ---


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


def test_print_summary(
    mock_gateway: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test the summary printing function with various flags."""
    kwargs = {
        "show_schema": True,
        "show_params": True,
        "show_status": True,
        "show_knowns": True,
        "show_traits": True,
        "show_crazys": False,  # Harder to mock message DB iteration simply
    }

    print_summary(mock_gateway, **kwargs)

    captured = capsys.readouterr()
    output = captured.out

    assert "Schema[global]" in output
    assert "Params[global]" in output
    assert "Status[global]" in output
    assert "allow_list (hints)" in output
    assert '"mock": "traits"' in output


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
        # Patch protocol wait to avoid hanging
        mock_gateway._protocol._wait_connection_lost = AsyncMock()

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

    with (
        patch("ramses_cli.client.Gateway", return_value=mock_gateway),
        patch("ramses_cli.client.spawn_scripts", return_value=[AsyncMock()]),
        patch("ramses_cli.client.normalise_config", return_value=(None, lib_kwargs)),
    ):
        await async_main(EXECUTE, lib_kwargs, **kwargs)

        # mock_spawn.assert_called_once()  # spawn_scripts called
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
        # Patch wait_connection_lost
        mock_gateway._protocol._wait_connection_lost = AsyncMock()

        await async_main(LISTEN, lib_kwargs, **kwargs)

        # Verify gateway initialized (implicit in logic)
        mock_gateway.start.assert_awaited()


def test_convert() -> None:
    """Test DeviceIdParamType handling of invalid IDs."""
    param_type = DeviceIdParamType()

    # Test valid ID
    assert param_type.convert("01:123456", None, None) == "01:123456"

    # Test invalid ID raises click.BadParameter
    with pytest.raises(click.BadParameter):
        param_type.convert("invalid", None, None)


def test__save_state(mock_gateway: MagicMock) -> None:
    """Test _save_state writes schema and packets to files."""
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
