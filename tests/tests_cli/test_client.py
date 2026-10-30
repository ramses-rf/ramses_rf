#!/usr/bin/env python3
"""Unittests for the ramses_cli client.py class."""

import asyncio
import io
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import asyncclick as click
import pytest
from asyncclick.testing import CliRunner

from ramses_cli.client import (
    EXECUTE,
    LISTEN,
    MONITOR,
    PARSE,
    SZ_INPUT_FILE,
    DeviceIdParamType,
    _print_engine_state,
    _run_cli,
    _save_state,
    async_main,
    cli,
    normalise_config,
    print_results,
    print_summary,
    split_kwargs,
)
from ramses_rf import GracefulExit
from ramses_rf.const import (
    DEV_TYPE_MAP,
    SZ_CONFIG,
    SZ_DISABLE_DISCOVERY,
)
from ramses_rf.gateway import Gateway, GatewayConfig
from ramses_rf.messages import Message
from ramses_rf.state import MessageStore
from ramses_tx import exceptions as exc
from ramses_tx.const import I_, Code
from ramses_tx.schemas import SZ_PACKET_LOG, SZ_SERIAL_PORT

STDIN = io.StringIO(
    "053  I --- 01:123456 --:------ 01:123456 3150 002 FC00\r\n"
)
CMD = "RQ 01:123456 1F09 00"


@pytest.fixture
async def mock_gateway() -> AsyncGenerator[MagicMock, None]:
    """Create a mock Gateway instance for testing."""
    gateway = MagicMock(spec=Gateway)
    # Fix: Explicitly assign a MagicMock to __str__ and tell mypy to ignore the method assignment
    gateway.__str__ = MagicMock(return_value="Gateway")  # type: ignore[method-assign]

    gateway._async_send_dto = AsyncMock()
    gateway.dispatcher = MagicMock()
    gateway.dispatcher.send = AsyncMock()
    gateway.dispatcher.send_background = MagicMock()
    gateway.start = AsyncMock()
    gateway.stop = AsyncMock()
    gateway.get_state = AsyncMock(return_value=({}, {}))
    gateway._restore_cached_packets = AsyncMock()
    gateway.add_msg_handler = MagicMock()

    # Fix: Create the nested engine structure
    gateway._engine = MagicMock()

    # Fix: Explicitly mock the private protocol attribute inside the engine
    gateway._engine._protocol = MagicMock()

    # Fix: Explicitly mock wait_for_connection_lost as an async method
    gateway._engine._protocol.wait_for_connection_lost = AsyncMock()

    # Fix: Create a future attached to the running loop.
    loop = asyncio.get_running_loop()

    future: asyncio.Future[None] = loop.create_future()
    future.set_result(None)
    gateway._engine._protocol._wait_connection_lost = future

    # Add required attributes
    gateway.config = MagicMock()
    gateway.config.disable_discovery = False
    gateway.config.enable_eavesdrop = False

    # Fix: Provide a dictionary for known_list so json.dumps doesn't crash on MagicMock
    gateway.config.known_list = {"18:123456": {"class": "HGI"}}

    # Fix: Mock the loop inside the engine
    gateway._engine._loop = MagicMock()
    gateway._engine._loop.call_soon = MagicMock()
    gateway._engine._loop.call_later = MagicMock()
    gateway._engine._loop.time = MagicMock(return_value=0.0)
    gateway._engine._include = {}

    # Mock devices for print_summary
    mock_dev = MagicMock()
    mock_dev.id = "01:123456"
    mock_dev.type = DEV_TYPE_MAP.CTL  # Controller

    # These are now async methods
    mock_dev.schema = AsyncMock(return_value={"mock": "schema"})
    mock_dev.params = AsyncMock(return_value={"mock": "params"})
    mock_dev.status = AsyncMock(return_value={"mock": "status"})
    mock_dev.traits = AsyncMock(return_value={"mock": "traits"})

    # Mock message database interaction via the entity_state component
    mock_dev.entity_state = MagicMock()
    msg_0005, msg_000c = MagicMock(), MagicMock()
    msg_0005.code, msg_000c.code = Code._0005, Code._000C
    msg_0005.configure_mock(**{"__str__.return_value": "msg_0005"})
    msg_000c.configure_mock(**{"__str__.return_value": "msg_000c"})
    mock_dev.entity_state.get_all_messages = AsyncMock(
        return_value=[msg_0005, msg_000c]
    )

    gateway.device_registry.devices = [mock_dev]
    gateway.tcs = None  # mimic no TCS
    gateway.schema = AsyncMock(return_value={"global": "schema"})
    gateway.params = AsyncMock(return_value={"global": "params"})
    gateway.status = AsyncMock(return_value={"global": "status"})

    # Add msg_db attribute
    gateway.message_store = MessageStore(maintain=False)

    # Mock system_by_id for print_results
    mock_sys = MagicMock()
    mock_sys.dhw.schedule = [{"day": "Monday"}]
    mock_sys.zone_by_index = {"01": MagicMock(schedule=[{"day": "Tuesday"}])}
    # Fix: Use integer key for faultlog to match expectations of print_results
    mock_sys._faultlog.faultlog = {0: "fault_data"}
    gateway.device_registry.system_by_id = {"01:123456": mock_sys}

    yield gateway


# --- CLI Argument Parsing Tests ---


@pytest.mark.asyncio
async def test_parse_no_input() -> None:
    runner = CliRunner()
    result = await runner.invoke(cli, ["parse"])
    assert result.exit_code == 2  # missing input file
    assert result.output.startswith("Usage: cli parse [OPTIONS] INPUT_FILE")


@pytest.mark.asyncio
async def test_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", STDIN)
    runner = CliRunner()
    result = await runner.invoke(cli, ["parse", "-"])
    assert result.exit_code == 0  # OK input file supplied
    assert result.output == ""


@pytest.mark.asyncio
async def test_monitor_no_port() -> None:
    runner = CliRunner()
    result = await runner.invoke(cli, ["monitor"])
    assert result.exit_code == 2  # missing port name
    assert result.output.startswith("Usage: cli monitor [OPTIONS] SERIAL_PORT")


@pytest.mark.asyncio
async def test_monitor(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    result = await runner.invoke(cli, ["monitor", "nullmodem"])
    assert result.exit_code == 0  # OK port name supplied
    assert "discovery is enabled" in result.output


@pytest.mark.asyncio
async def test_monitor_no_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test monitor with explicit no-discovery flag."""
    runner = CliRunner()
    result = await runner.invoke(
        cli, ["monitor", "nullmodem", "--no-discover"]
    )
    assert result.exit_code == 0
    assert "discovery is enabled" not in result.output


@pytest.mark.asyncio
async def test_execute_no_arg() -> None:
    runner = CliRunner()
    result = await runner.invoke(cli, ["execute"])
    assert result.exit_code == 2  # missing command, port
    assert result.output.startswith("Usage: cli execute [OPTIONS] SERIAL_PORT")


@pytest.mark.asyncio
async def test_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    result = await runner.invoke(cli, ["execute", CMD])
    assert result.exit_code == 0  # OK command supplied
    assert result.output == " - discovery is force-disabled\n"


@pytest.mark.asyncio
async def test_listen_no_arg() -> None:
    runner = CliRunner()
    result = await runner.invoke(cli, ["listen"])
    assert result.exit_code == 2  # missing port
    assert result.output.startswith("Usage: cli listen [OPTIONS] SERIAL_PORT")


@pytest.mark.asyncio
async def test_listen(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    result = await runner.invoke(cli, ["listen", "nullmodem"])
    assert result.exit_code == 0  # OK port supplied
    assert result.output == " - sending is force-disabled\n"


# --- Unit Tests for Logic Functions ---


def test_normalise_config() -> None:
    """Test the configuration normalization logic."""
    # Case 1: Packet log as string
    lib_config: dict[str, Any] = {
        SZ_SERIAL_PORT: "/dev/ttyUSB0",
        SZ_PACKET_LOG: "packet_log",
    }
    port, cfg = normalise_config(lib_config)
    assert port == "/dev/ttyUSB0"
    assert cfg is not None
    assert cfg[SZ_PACKET_LOG]["packet_log_prefix"] == "packet_log"

    # Case 2: Packet log as dict
    lib_config = {
        SZ_PACKET_LOG: {
            "packet_log_prefix": "packet_log",
            "rotate_bytes": 204800,
        }
    }
    port, cfg = normalise_config(lib_config)
    assert port is None
    assert cfg is not None
    assert cfg[SZ_PACKET_LOG]["rotate_bytes"] == 204800


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


def test_split_kwargs_preserves_configured_packet_log() -> None:
    """An omitted -o option must not replace packet-log configuration."""
    packet_log = {
        "packet_log_prefix": "/var/log/evohome/evohome-2026102808",
        "packet_log_retention_days": 0,
    }
    obj: tuple[dict[str, Any], dict[str, Any]] = (
        {},
        {SZ_CONFIG: {}, SZ_PACKET_LOG: packet_log},
    )

    _, lib_args = split_kwargs(obj, {SZ_PACKET_LOG: None})

    assert lib_args[SZ_PACKET_LOG] == packet_log


@pytest.mark.asyncio
async def test_print_summary(
    mock_gateway: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test the summary printing function with various flags."""
    # Mock msg_db to be None to trigger the alternative branch in show_crazys
    mock_gateway.message_store = None

    kwargs: dict[str, Any] = {
        "show_schema": True,
        "show_params": True,
        "show_status": True,
        "show_knowns": True,
        "show_traits": True,
        "show_crazys": True,
    }

    await print_summary(mock_gateway, **kwargs)

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


@pytest.mark.asyncio
async def test_print_engine_state(
    mock_gateway: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test _print_engine_state."""
    kwargs: dict[str, Any] = {
        "print_state": 2
    }  # 2 implies print packets as well
    await _print_engine_state(mock_gateway, **kwargs)

    out = capsys.readouterr().out
    assert "schema" in out
    assert "packets" in out


@pytest.mark.asyncio
async def test_async_main_kwargs_separation(mock_gateway: MagicMock) -> None:
    """Test that L7 config keys are not passed to EngineConfig.

    This ensures that Phase 2.77 decoupled traits (like known_list and hgi_id)
    are routed strictly to GatewayConfig and blocked from EngineConfig instantiation.
    """

    # ARRANGE
    lib_kwargs: dict[str, Any] = {
        SZ_CONFIG: {},
        SZ_PACKET_LOG: {},
        "known_list": {"18:123456": {"class": "HGI", "faked": True}},
        "hgi_id": "18:123456",
    }
    kwargs: dict[str, Any] = {
        "long_format": False,
        "json_format": False,
        "restore_schema": None,
        "restore_state": None,
        "print_state": 0,
        SZ_INPUT_FILE: "input.log",
    }

    # ACT
    with (
        patch(
            "ramses_cli.client.Gateway", return_value=mock_gateway
        ) as mock_gwy_class,
        patch(
            "ramses_cli.client.normalise_config",
            return_value=(None, lib_kwargs),
        ),
    ):
        await async_main(PARSE, lib_kwargs, **kwargs)

        call_kwargs = mock_gwy_class.call_args.kwargs
        config_obj: GatewayConfig = call_kwargs["config"]

        # ASSERT - L7 receives the dictionary
        assert isinstance(config_obj, GatewayConfig)
        assert config_obj.known_list == {
            "18:123456": {"class": "HGI", "faked": True}
        }
        assert config_obj.hgi_id == "18:123456"

        # ASSERT - L3 does NOT receive the nested dict from the kwargs unpacker
        # EngineConfig defaults to None for these properties. We verify they
        # were successfully filtered out during the dictionary comprehension.
        assert config_obj.engine.known_list is None
        assert config_obj.engine.hgi_id is None


@pytest.mark.asyncio
async def test_async_main_parse(mock_gateway: MagicMock) -> None:
    """Test async_main logic for the PARSE command and config validation."""
    lib_kwargs: dict[str, Any] = {
        SZ_CONFIG: {"reduce_processing": 0},
        SZ_PACKET_LOG: {},
    }
    kwargs: dict[str, Any] = {
        "long_format": False,
        "json_format": False,
        "restore_schema": None,
        "restore_state": None,
        "print_state": 0,
        SZ_INPUT_FILE: "input.log",
    }

    with (
        patch(
            "ramses_cli.client.Gateway", return_value=mock_gateway
        ) as mock_gwy_class,
        patch(
            "ramses_cli.client.normalise_config",
            return_value=(None, lib_kwargs),
        ),
    ):
        await async_main(PARSE, lib_kwargs, **kwargs)

        mock_gateway.start.assert_awaited_once()
        mock_gateway.stop.assert_awaited_once()

        # Verify GatewayConfig was explicitly used and built correctly
        call_kwargs = mock_gwy_class.call_args.kwargs
        assert "config" in call_kwargs
        assert isinstance(call_kwargs["config"], GatewayConfig)
        assert call_kwargs["config"].reduce_processing == 0


@pytest.mark.asyncio
async def test_async_main_real_gateway_init() -> None:
    """Verify real Gateway initialization using GatewayConfig.

    This test intentionally does NOT mock the Gateway class itself,
    ensuring the real Gateway.__init__ runs and successfully parses
    the GatewayConfig object (fixes Issue #479).
    """
    lib_kwargs: dict[str, Any] = {
        SZ_CONFIG: {
            "reduce_processing": 0,
            "disable_discovery": True,
        },
        SZ_PACKET_LOG: {},
        SZ_INPUT_FILE: "dummy.log",
    }
    kwargs: dict[str, Any] = {
        "long_format": False,
        "restore_schema": None,
        "restore_state": None,
        "print_state": 0,
    }

    with (
        patch(
            "ramses_cli.client.normalise_config",
            return_value=(None, lib_kwargs),
        ),
        patch(
            "ramses_rf.gateway.Gateway.start", new_callable=AsyncMock
        ) as mock_start,
        patch(
            "ramses_rf.gateway.Gateway.stop", new_callable=AsyncMock
        ) as mock_stop,
    ):
        await async_main(PARSE, lib_kwargs, **kwargs)

        mock_start.assert_awaited_once()
        mock_stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_main_execute(mock_gateway: MagicMock) -> None:
    """Test async_main logic for the EXECUTE command."""
    lib_kwargs: dict[str, Any] = {
        SZ_CONFIG: {
            "reduce_processing": 0,
            SZ_DISABLE_DISCOVERY: True,
        },
        SZ_PACKET_LOG: {},
    }
    kwargs: dict[str, Any] = {
        "long_format": False,
        "restore_schema": None,
        "restore_state": None,
        "print_state": 0,
        "exec_cmd": "RQ 01:123456 1F09 00",
        "exec_scr": None,
        "get_faults": None,
        "get_schedule": [None, None],
        "set_schedule": [None, None],
    }

    async def mock_task() -> None:
        pass

    with (
        patch("ramses_cli.client.Gateway", return_value=mock_gateway),
        patch("ramses_cli.client.spawn_scripts", return_value=[mock_task()]),
        patch(
            "ramses_cli.client.normalise_config",
            return_value=(None, lib_kwargs),
        ),
    ):
        await async_main(EXECUTE, lib_kwargs, **kwargs)

        mock_gateway.start.assert_awaited_once()
        mock_gateway.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_main_monitor(mock_gateway: MagicMock) -> None:
    """Test async_main logic for the MONITOR command."""
    lib_kwargs: dict[str, Any] = {
        SZ_CONFIG: {"reduce_processing": 0},
        SZ_PACKET_LOG: {},
    }
    kwargs: dict[str, Any] = {
        "long_format": False,
        "restore_schema": None,
        "restore_state": None,
        "print_state": 0,
        "exec_scr": None,  # Simple monitor
    }

    with (
        patch("ramses_cli.client.Gateway", return_value=mock_gateway),
        patch("ramses_cli.client.spawn_scripts", return_value=[]),
        patch(
            "ramses_cli.client.normalise_config",
            return_value=(None, lib_kwargs),
        ),
    ):
        await async_main(MONITOR, lib_kwargs, **kwargs)

        mock_gateway.start.assert_awaited_once()
        mock_gateway.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_main_restore_schema(mock_gateway: MagicMock) -> None:
    """Test async_main with restore_schema functionality."""
    lib_kwargs: dict[str, Any] = {SZ_CONFIG: {"reduce_processing": 0}}

    # Mock a file object for restore_schema
    mock_file = MagicMock()
    mock_file.read.return_value = json.dumps(
        {
            "data": {
                "client_state": {
                    "schema": {"restored": "value"},
                    "packets": [],
                }
            }
        }
    )
    # Make json.load work on the mock
    mock_file.__enter__.return_value = mock_file

    kwargs: dict[str, Any] = {
        "long_format": False,
        "restore_schema": mock_file,
        "restore_state": None,
        "print_state": 0,
    }

    with (
        patch("ramses_cli.client.Gateway", return_value=mock_gateway),
        patch(
            "json.load",
            return_value={
                "data": {"client_state": {"schema": {}, "packets": []}}
            },
        ),
        patch(
            "ramses_cli.client.normalise_config",
            return_value=(None, lib_kwargs),
        ),
    ):
        await async_main(LISTEN, lib_kwargs, **kwargs)
        # Verify gateway initialized (implicit in logic)
        mock_gateway.start.assert_awaited()


@pytest.mark.asyncio
async def test_async_main_msg_handler(
    mock_gateway: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test the internal handle_msg callback logic inside async_main."""
    lib_kwargs: dict[str, Any] = {
        SZ_CONFIG: {"reduce_processing": 0},
        SZ_PACKET_LOG: {},
    }
    kwargs: dict[str, Any] = {
        "long_format": False,
        "json_format": False,
        "restore_schema": None,
        "restore_state": None,
        "print_state": 0,
    }

    # We need to capture the callback passed to add_msg_handler
    captured_callback: Callable[[Any], Awaitable[None]] | None = None

    def capture_cb(cb: Any) -> None:
        nonlocal captured_callback
        captured_callback = cb

    mock_gateway.add_msg_handler.side_effect = capture_cb

    with (
        patch("ramses_cli.client.Gateway", return_value=mock_gateway),
        patch(
            "ramses_cli.client.normalise_config",
            return_value=(None, lib_kwargs),
        ),
    ):
        # Run main to trigger registration
        await async_main(PARSE, lib_kwargs, **kwargs)

        assert captured_callback is not None

        # Now test the callback with different message types
        # 1. Puzzle message
        msg1 = MagicMock(spec=Message)
        msg1.dtm = dt.now()
        msg1.code = Code._PUZZ
        # Mypy dislikes assigning to method slots on mocks without ignore
        msg1.__repr__ = MagicMock(return_value="PUZZLE_MSG")  # type: ignore[method-assign]

        with patch("ramses_cli.client.Message", return_value=msg1):
            await captured_callback(MagicMock())
        out = capsys.readouterr().out
        assert "PUZZLE_MSG" in out

        # 2. 1F09 (I) message
        # Use a fresh mock object to avoid state pollution
        msg2 = MagicMock(spec=Message)
        msg2.dtm = dt.now()
        msg2.code = Code._1F09
        msg2.verb = I_
        # Fix: Ensure src attribute exists for HGI check in handle_msg
        msg2.src = MagicMock()
        msg2.src.type = "01"  # Controller type, definitely not HGI
        msg2.__repr__ = MagicMock(return_value="1F09_MSG")  # type: ignore[method-assign]

        with patch("ramses_cli.client.Message", return_value=msg2):
            await captured_callback(MagicMock())
        out = capsys.readouterr().out
        assert "1F09_MSG" in out


@pytest.mark.asyncio
async def test_async_main_long_format(
    mock_gateway: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test the long_format output branch in handle_msg."""
    lib_kwargs: dict[str, Any] = {
        SZ_CONFIG: {"reduce_processing": 0},
        SZ_PACKET_LOG: {},
    }
    kwargs: dict[str, Any] = {
        "long_format": True,  # ENABLE LONG FORMAT
        "restore_schema": None,
        "restore_state": None,
        "print_state": 0,
    }

    # Capture the callback
    captured_callback: Callable[[Any], Awaitable[None]] | None = None

    def capture_cb(cb: Any) -> None:
        nonlocal captured_callback
        captured_callback = cb

    mock_gateway.add_msg_handler.side_effect = capture_cb

    with (
        patch("ramses_cli.client.Gateway", return_value=mock_gateway),
        patch(
            "ramses_cli.client.normalise_config",
            return_value=(None, lib_kwargs),
        ),
    ):
        await async_main(PARSE, lib_kwargs, **kwargs)

        # Trigger callback
        msg = MagicMock(spec=Message)
        msg.dtm = dt.now()
        msg.__repr__ = MagicMock(return_value="LONG_MSG")  # type: ignore[method-assign]
        msg.payload = "PAYLOAD"

        assert captured_callback is not None
        with patch("ramses_cli.client.Message", return_value=msg):
            await captured_callback(MagicMock())

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
    lib_kwargs: dict[str, Any] = {
        SZ_CONFIG: {"reduce_processing": 0},
        SZ_PACKET_LOG: {},
    }
    kwargs: dict[str, Any] = {
        "long_format": False,
        "restore_schema": None,
        "restore_state": None,
        "print_state": 0,
    }

    with (
        patch("ramses_cli.client.Gateway", return_value=mock_gateway),
        patch(
            "ramses_cli.client.normalise_config",
            return_value=(None, lib_kwargs),
        ),
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


@pytest.mark.asyncio
async def test_cli_debug_mode(mock_gateway: MagicMock) -> None:
    """Test that the debug flag triggers the debugger."""
    with patch("ramses_cli.client.start_debugging") as mock_debug:
        runner = CliRunner()
        # invoke cli with -z (debug) count 1
        await runner.invoke(cli, ["-z", "parse", "/dev/null"])
        mock_debug.assert_called_once_with(True)


@pytest.mark.asyncio
async def test_cli_config_file(mock_gateway: MagicMock) -> None:
    """Test loading a configuration file via the CLI."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        # Write config file synchronously before invoking CLI
        with open("test_config.json", "w") as f:
            json.dump({"config": {"disable_discovery": True}}, f)

        # Run CLI with -c pointing to the locally closed file
        result = await runner.invoke(
            cli, ["-c", "test_config.json", "parse", "/dev/null"]
        )

        assert result.exit_code == 0
        # Verify the config was actually merged (by checking the internal call)
        # Note: We rely on normalise_config patching in async_main tests usually,
        # but here we just want to ensure it runs without error.


@pytest.mark.asyncio
async def test_execute_flags(mock_gateway: MagicMock) -> None:
    """Test that execute flags (like --get-faults) enforce the known_list."""
    runner = CliRunner()

    # Running execute with a specific device target should force-enable the known_list
    result = await runner.invoke(
        cli, ["execute", "/dev/null", "--get-faults", "01:123456"]
    )

    assert result.exit_code == 0
    # This specific string is printed when execute logic enforces the list
    assert "known list is force-configured" in result.output


@pytest.mark.asyncio
async def test__save_state(mock_gateway: MagicMock) -> None:
    """Test _save_state writes schema and packets to files."""
    # Setup mock gateway state
    mock_gateway.get_state.return_value = (
        {"schema_key": "schema_data"},
        {"2023-01-01T00:00:00": "packet_line"},
    )

    with patch("builtins.open", new_callable=mock_open) as mock_file:
        await _save_state(mock_gateway)

        # Verify open was called twice (once for log, once for json)
        assert mock_file.call_count == 2

        # Check that expected files were opened
        calls = mock_file.call_args_list
        filenames = [c[0][0] for c in calls]
        assert "state_msgs.log" in filenames
        assert "state_schema.json" in filenames


@pytest.mark.asyncio
async def test_parse_command_passes_input_file() -> None:
    """Verify that 'client.py parse' correctly puts input_file into lib_kwargs."""

    runner = CliRunner()
    fake_log_file = "test_capture.log"

    # standalone_mode=False allows us to see the return value of the command
    result = await runner.invoke(
        cli, ["parse", fake_log_file], standalone_mode=False
    )

    assert result.exit_code == 0, f"Command failed: {result.exception}"

    # The 'parse' command returns a tuple: (command_name, lib_kwargs, cli_kwargs)
    command, lib_kwargs, cli_kwargs = result.return_value

    # VERIFY: Did the CLI put the filename into the correct config key?
    assert lib_kwargs.get("input_file") == fake_log_file, (
        f"Expected input_file='{fake_log_file}', got {lib_kwargs.get('input_file')}"
    )

    print("\nSuccess: CLI parsed the input file argument correctly.")


# --- Core App Lifecycle Execution Tests ---


def test_run_cli_missing_command(capsys: pytest.CaptureFixture[str]) -> None:
    """Test that _run_cli catches ClickException on missing commands."""
    with (
        patch("sys.argv", ["client.py"]),
        patch("sys.exit", side_effect=SystemExit) as mock_exit,
        pytest.raises(SystemExit),
    ):
        _run_cli()

    mock_exit.assert_called_once()
    captured = capsys.readouterr()
    assert "Usage:" in captured.out or "Usage:" in captured.err


def test_run_cli_help(capsys: pytest.CaptureFixture[str]) -> None:
    """Test that _run_cli catches click.exceptions.Exit when --help is passed."""
    with (
        patch("sys.argv", ["client.py", "--help"]),
        patch("sys.exit", side_effect=SystemExit) as mock_exit,
        pytest.raises(SystemExit),
    ):
        _run_cli()

    mock_exit.assert_called_once_with(0)
    captured = capsys.readouterr()
    assert "Usage:" in captured.out or "Usage:" in captured.err


def test_run_cli_valid() -> None:
    """Test that _run_cli correctly bridges into asyncio.run(async_main(...))."""

    async def dummy_main(*args: Any, **kwargs: Any) -> None:
        """A safe, awaitable mock for async_main to avoid nested loop conflicts."""
        pass

    with (
        patch("sys.argv", ["client.py", "parse", "dummy.log"]),
        patch(
            "ramses_cli.client.async_main", side_effect=dummy_main
        ) as mock_main,
        patch("sys.exit", side_effect=SystemExit) as mock_exit,
    ):
        _run_cli()

        mock_exit.assert_not_called()
        mock_main.assert_called_once()


# ---------------------------------------------------------------------------
# Tests for the scan command and _print_scan_results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_main_scan(mock_gateway: MagicMock) -> None:
    """Test async_main logic for the SCAN command."""
    from ramses_cli.client import SCAN

    lib_kwargs: dict[str, Any] = {
        SZ_CONFIG: {"reduce_processing": 0},
        SZ_PACKET_LOG: {},
    }
    kwargs: dict[str, Any] = {
        "long_format": False,
        "restore_schema": None,
        "restore_state": None,
        "print_state": 0,
        SZ_INPUT_FILE: "input.log",
        "scan_duration": 0,  # 0 = until interrupted, but we'll cancel quickly
        "scan_output": None,
    }

    with (
        patch("ramses_cli.client.Gateway", return_value=mock_gateway),
        patch(
            "ramses_cli.client.normalise_config",
            return_value=(None, lib_kwargs),
        ),
        patch("ramses_cli.client.DiscoveryScan") as mock_scan_cls,
    ):
        mock_scan_engine = MagicMock()
        mock_scan_engine.start = MagicMock()
        mock_scan_engine.stop = MagicMock()
        mock_scan_engine.get_devices.return_value = []
        mock_scan_cls.return_value = mock_scan_engine

        # Use a very short sleep to avoid hanging the test
        async def short_sleep(_: float) -> None:
            pass

        with patch("asyncio.sleep", side_effect=short_sleep):
            await async_main(SCAN, lib_kwargs, **kwargs)

        mock_scan_engine.start.assert_called_once()
        mock_scan_engine.stop.assert_called_once()
        mock_gateway.start.assert_awaited_once()
        mock_gateway.stop.assert_awaited_once()


def test_print_scan_results_with_devices(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test _print_scan_results formats device output correctly."""
    from ramses_cli.client import _print_scan_results
    from ramses_rf.discovery_scan import DiscoveredDevice

    devices = [
        DiscoveredDevice(
            device_id="01:145038",
            first_seen="2026-01-01T00:00:00",
            last_seen="2026-01-01T00:00:01",
            likely_type="CTL",
            codes_seen=[Code._2E04, Code._000C],
            bound_to=None,
            zone_index="0A",
            rssi=-68.0,
            confidence="high",
            is_battery=False,
            source_count=5,
            destination_count=2,
        ),
        DiscoveredDevice(
            device_id="04:056053",
            first_seen="2026-01-01T00:00:00",
            last_seen="2026-01-01T00:00:01",
            likely_type="TRV",
            codes_seen=[Code._3150],
            bound_to="01:145038",
            zone_index="02",
            rssi=-72.0,
            confidence="high",
            is_battery=True,
            source_count=3,
            destination_count=0,
        ),
    ]

    mock_scan = MagicMock()
    mock_scan.get_devices.return_value = devices

    _print_scan_results(mock_scan, None)

    captured = capsys.readouterr()
    assert "Found 2 device(s)" in captured.out
    assert "01:145038" in captured.out
    assert "CTL" in captured.out
    assert "04:056053" in captured.out
    assert "TRV" in captured.out
    assert "zone=02" in captured.out
    assert "bound to 01:145038" in captured.out
    assert "battery" in captured.out


def test_print_scan_results_no_devices(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test _print_scan_results when no devices are found."""
    from ramses_cli.client import _print_scan_results

    mock_scan = MagicMock()
    mock_scan.get_devices.return_value = []

    _print_scan_results(mock_scan, None)

    captured = capsys.readouterr()
    assert "No unknown devices discovered" in captured.out


def test_print_scan_results_export_to_file(
    tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Test _print_scan_results exports JSON to file when output path is given."""
    from ramses_cli.client import _print_scan_results
    from ramses_rf.discovery_scan import DiscoveredDevice

    devices = [
        DiscoveredDevice(
            device_id="32:157747",
            first_seen="2026-01-01T00:00:00",
            last_seen="2026-01-01T00:00:01",
            likely_type="FAN",
            codes_seen=[Code._31DA],
            bound_to=None,
            zone_index=None,
            rssi=-70.0,
            confidence="medium",
            is_battery=False,
            source_count=2,
            destination_count=0,
        ),
    ]

    mock_scan = MagicMock()
    mock_scan.get_devices.return_value = devices
    mock_scan.export_json.return_value = '{"devices": []}'

    output_file = str(tmp_path / "found.json")
    _print_scan_results(mock_scan, output_file)

    captured = capsys.readouterr()
    assert f"Exported to {output_file}" in captured.out
    assert (tmp_path / "found.json").exists()


@pytest.mark.asyncio
async def test_scan_command_disables_sending_and_discovery() -> None:
    """Test that the scan command callback forces disable_sending and disable_discovery."""
    from ramses_cli.client import SCAN, cli
    from ramses_rf.const import (
        SZ_CONFIG,
        SZ_DISABLE_DISCOVERY,
    )

    lib_config: dict[str, Any] = {SZ_CONFIG: {}}
    cli_config: dict[str, Any] = {}

    runner = CliRunner()
    with patch(
        "ramses_cli.client.split_kwargs", return_value=(cli_config, lib_config)
    ):
        # standalone_mode=False returns the command's return value
        result = await runner.invoke(
            cli,
            [
                "scan",
                "/dev/ttyUSB0",
                "--file",
                "found.json",
                "--duration",
                "60",
            ],
            standalone_mode=False,
        )

        assert result.exit_code == 0, f"CLI failed: {result.output}"

        # scan() returns (SCAN, lib_config, cli_config)
        command, lib_kwargs, cli_kwargs = result.return_value
        assert command == SCAN
        assert lib_kwargs[SZ_CONFIG]["disable_sending"] is True
        assert lib_kwargs[SZ_CONFIG][SZ_DISABLE_DISCOVERY] is True
        assert cli_kwargs["scan_duration"] == 60
        assert cli_kwargs["scan_output"] == "found.json"
