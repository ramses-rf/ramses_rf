#!/usr/bin/env python3
"""Unittests for the ramses_cli client.py class."""

import io
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from ramses_cli.client import cli, parse
from ramses_rf.database import MessageIndex
from ramses_rf.gateway import Gateway

STDIN = io.StringIO("053  I --- 01:123456 --:------ 01:123456 3150 002 FC00\r\n")


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


def test_parse_no_input():
    runner = CliRunner()
    result = runner.invoke(cli, ["parse"])
    assert result.exit_code == 2  # missing input file
    assert result.output.startswith("Usage: cli parse")


def test_parse_input(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("sys.stdin", STDIN)
    runner = CliRunner()
    result = runner.invoke(parse, [parse, "-"])
    assert result.exit_code == 1  # input file supplied
    assert result.output == ""


#
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
# def test_parse():
#     assert False
#
#
# def test_parse_cli():
#     assert False
#
#
# def test_monitor():
#     assert False
#
#
# def test_execute():
#     assert False
#
#
# def test_listen():
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
