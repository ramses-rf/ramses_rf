#!/usr/bin/env python3
"""Unittests for the ramses_cli client.py class."""

import io
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from ramses_cli.client import cli
from ramses_rf.database import MessageIndex
from ramses_rf.gateway import Gateway

STDIN = io.StringIO("053  I --- 01:123456 --:------ 01:123456 3150 002 FC00\r\n")
CMD = "RQ 01:123456 1F09 00"


@pytest.fixture
def mock_gateway() -> Generator[MagicMock, None, None]:
    """Create a mock Gateway instance for testing."""
    gateway = MagicMock(spec=Gateway)
    gateway.send_cmd = AsyncMock()
    gateway.dispatcher = MagicMock()
    gateway.dispatcher.send = MagicMock()

    # Add required attributes
    gateway.config = MagicMock()
    gateway.config.disable_discovery = False
    gateway.config.enable_eavesdrop = False
    gateway._loop = MagicMock()
    gateway._loop.call_soon = MagicMock()
    gateway._loop.call_later = MagicMock()
    gateway._loop.time = MagicMock(return_value=0.0)
    gateway._include = {}
    # Add msg_db attribute accessed by the message store, activates the SQLite MessageIndex
    gateway.msg_db = MessageIndex(maintain=False)

    yield gateway


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


# def test_convert() -> None:
#     assert False
#
#
# def test_cli():
#     assert False
#
#
# def test_file_command():
#     assert False
#
#
# def test_file_cli():
#     assert False
#
#
# def test_port_command():
#     assert False
#
#
# def test_port_cli():
#     assert False
#
#
# def test_parse_cli():
#     assert False
#
#
# def test_print_results():
#     assert False
#
#
# def test__save_state():
#     assert False
#
#
# def test__print_engine_state():
#     assert False
#
#
# def test_print_summary():
#     assert False
