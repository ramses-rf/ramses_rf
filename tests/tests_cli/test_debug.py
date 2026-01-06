#!/usr/bin/env python3
"""Unittests for the ramses_cli debug.py module."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from ramses_cli.debug import DEBUG_ADDR, DEBUG_PORT, start_debugging


def test_start_debugging_no_wait(capsys: pytest.CaptureFixture[str]) -> None:
    """Test start_debugging with wait_for_client=False."""
    mock_debugpy = MagicMock()

    # Patch sys.modules to intercept the import inside the function
    with patch.dict(sys.modules, {"debugpy": mock_debugpy}):
        start_debugging(wait_for_client=False)

    # Verify listen was called correctly
    mock_debugpy.listen.assert_called_once_with(address=(DEBUG_ADDR, DEBUG_PORT))

    # Verify wait_for_client was NOT called
    mock_debugpy.wait_for_client.assert_not_called()

    # Verify console output
    captured = capsys.readouterr()
    assert (
        f"Debugging is enabled, listening on: {DEBUG_ADDR}:{DEBUG_PORT}" in captured.out
    )
    assert "execution paused" not in captured.out


def test_start_debugging_wait(capsys: pytest.CaptureFixture[str]) -> None:
    """Test start_debugging with wait_for_client=True."""
    mock_debugpy = MagicMock()

    with patch.dict(sys.modules, {"debugpy": mock_debugpy}):
        start_debugging(wait_for_client=True)

    # Verify listen was called
    mock_debugpy.listen.assert_called_once_with(address=(DEBUG_ADDR, DEBUG_PORT))

    # Verify wait_for_client WAS called
    mock_debugpy.wait_for_client.assert_called_once()

    # Verify console output
    captured = capsys.readouterr()
    assert (
        f"Debugging is enabled, listening on: {DEBUG_ADDR}:{DEBUG_PORT}" in captured.out
    )
    assert "execution paused, waiting for debugger to attach..." in captured.out
    assert "debugger is now attached, continuing execution." in captured.out
