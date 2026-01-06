#!/usr/bin/env python3
"""Unittests for the ramses_cli discovery.py module."""

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ramses_cli.discovery import (
    EXEC_CMD,
    EXEC_SCR,
    GET_FAULTS,
    GET_SCHED,
    SET_SCHED,
    exec_cmd,
    get_faults,
    get_schedule,
    script_bind_req,
    script_bind_wait,
    script_poll_device,
    script_scan_disc,
    script_scan_fan,
    script_scan_full,
    script_scan_hard,
    script_scan_otb,
    script_scan_otb_hard,
    script_scan_otb_map,
    script_scan_otb_ramses,
    set_schedule,
    spawn_scripts,
)
from ramses_rf.const import SZ_SCHEDULE, SZ_ZONE_IDX
from ramses_rf.gateway import Gateway

# Constants for testing
DEV_ID = "01:123456"


@pytest.fixture
def mock_gateway() -> MagicMock:
    """Create a mock Gateway instance."""
    gateway = MagicMock(spec=Gateway)
    gateway.send_cmd = MagicMock()
    gateway.async_send_cmd = AsyncMock()
    gateway._tasks = []

    # Mock device retrieval
    mock_dev = MagicMock()
    mock_dev.tcs = MagicMock()
    # Ensure nested mocks for schedule/zone calls return async mocks
    mock_dev.tcs.get_faultlog = AsyncMock()
    mock_dev.tcs.get_htg_zone.return_value.get_schedule = AsyncMock()
    mock_dev.tcs.get_htg_zone.return_value.set_schedule = AsyncMock()

    # Mock discover
    mock_dev.discover = AsyncMock()

    # Mock fakeable for binding tests
    mock_dev._make_fake = MagicMock()
    mock_dev._initiate_binding_process = AsyncMock()
    mock_dev._wait_for_binding_request = AsyncMock()

    gateway.get_device.return_value = mock_dev

    return gateway


@pytest.mark.asyncio
async def test_spawn_scripts_exec_cmd(mock_gateway: MagicMock) -> None:
    """Test spawning exec_cmd."""
    kwargs = {EXEC_CMD: "RQ 01:123456 1F09 00"}
    tasks = spawn_scripts(mock_gateway, **kwargs)
    assert len(tasks) == 1
    assert len(mock_gateway._tasks) == 1


@pytest.mark.asyncio
async def test_spawn_scripts_get_faults(mock_gateway: MagicMock) -> None:
    """Test spawning get_faults."""
    kwargs = {GET_FAULTS: DEV_ID}
    tasks = spawn_scripts(mock_gateway, **kwargs)
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_spawn_scripts_get_schedule(mock_gateway: MagicMock) -> None:
    """Test spawning get_schedule."""
    kwargs = {GET_SCHED: (DEV_ID, "01")}
    tasks = spawn_scripts(mock_gateway, **kwargs)
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_spawn_scripts_set_schedule(mock_gateway: MagicMock) -> None:
    """Test spawning set_schedule."""
    sched_json = f'{{"{SZ_ZONE_IDX}": "01", "{SZ_SCHEDULE}": []}}'
    kwargs = {SET_SCHED: (DEV_ID, sched_json)}
    tasks = spawn_scripts(mock_gateway, **kwargs)
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_spawn_scripts_exec_scr_valid(mock_gateway: MagicMock) -> None:
    """Test spawning a valid script."""
    with patch("ramses_cli.discovery.asyncio.create_task") as mock_create_task:
        mock_create_task.return_value = MagicMock()

        script_name = "scan_disc"
        kwargs = {EXEC_SCR: (script_name, DEV_ID)}

        tasks = spawn_scripts(mock_gateway, **kwargs)

        mock_create_task.assert_called()
        assert len(tasks) == 1


@pytest.mark.asyncio
async def test_spawn_scripts_exec_scr_invalid(mock_gateway: MagicMock) -> None:
    """Test spawning an invalid script."""
    kwargs = {EXEC_SCR: ("invalid_script_name", DEV_ID)}
    tasks = spawn_scripts(mock_gateway, **kwargs)
    assert len(tasks) == 0


@pytest.mark.asyncio
async def test_execution_of_exec_cmd(mock_gateway: MagicMock) -> None:
    """Test execution of exec_cmd logic."""
    kwargs = {EXEC_CMD: "RQ 01:123456 1F09 00"}
    await exec_cmd(mock_gateway, **kwargs)
    mock_gateway.async_send_cmd.assert_awaited()


@pytest.mark.asyncio
async def test_execution_of_get_faults(mock_gateway: MagicMock) -> None:
    """Test execution of get_faults logic."""
    await get_faults(mock_gateway, DEV_ID)  # type: ignore[arg-type]
    mock_dev = mock_gateway.get_device(DEV_ID)
    mock_dev.tcs.get_faultlog.assert_awaited_once()


@pytest.mark.asyncio
async def test_execution_of_get_schedule(mock_gateway: MagicMock) -> None:
    """Test execution of get_schedule logic."""
    await get_schedule(mock_gateway, DEV_ID, "01")  # type: ignore[arg-type]
    mock_dev = mock_gateway.get_device(DEV_ID)
    mock_zone = mock_dev.tcs.get_htg_zone("01")
    mock_zone.get_schedule.assert_awaited_once()


@pytest.mark.asyncio
async def test_execution_of_set_schedule(mock_gateway: MagicMock) -> None:
    """Test execution of set_schedule logic."""
    sched_json = f'{{"{SZ_ZONE_IDX}": "01", "{SZ_SCHEDULE}": []}}'
    await set_schedule(mock_gateway, DEV_ID, sched_json)  # type: ignore[arg-type]
    mock_dev = mock_gateway.get_device(DEV_ID)
    mock_zone = mock_dev.tcs.get_htg_zone("01")
    mock_zone.set_schedule.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
async def test_script_decorator_behavior(mock_gateway: MagicMock) -> None:
    """Test that script decorator sends start/end commands and executes body."""
    # Capture the internal coroutine that create_task would schedule
    with patch("ramses_cli.discovery.asyncio.create_task") as mock_create_task:
        script_scan_disc(mock_gateway, DEV_ID)

        # Get the coroutine passed to create_task
        coro = mock_create_task.call_args[0][0]
        # Await it to execute the script body
        await coro

    # Check for puzzle commands (Script begins/done) + actual script logic
    assert mock_gateway.send_cmd.call_count >= 2


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
async def test_script_scan_full(mock_gateway: MagicMock) -> None:
    """Test script_scan_full iterates through codes."""
    # Patch range to only loop once to avoid massive execution time
    with (
        patch("ramses_cli.discovery.range", return_value=iter([1])),
        patch("ramses_cli.discovery.asyncio.create_task") as mock_create_task,
    ):
        script_scan_full(mock_gateway, DEV_ID)

        # Exec the body
        coro = mock_create_task.call_args[0][0]
        await coro

    assert mock_gateway.send_cmd.called


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
async def test_script_scan_hard(mock_gateway: MagicMock) -> None:
    """Test script_scan_hard."""
    # Patch range to limit execution
    with (
        patch("ramses_cli.discovery.range", return_value=iter([0x4FFF])),
        patch("ramses_cli.discovery.asyncio.create_task") as mock_create_task,
    ):
        script_scan_hard(mock_gateway, DEV_ID)

        coro = mock_create_task.call_args[0][0]
        await coro

    # Verify something happened (send_cmd or async_send_cmd)
    assert mock_gateway.send_cmd.called or mock_gateway.async_send_cmd.called


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
async def test_script_scan_fan(mock_gateway: MagicMock) -> None:
    """Test script_scan_fan."""
    with patch("ramses_cli.discovery.asyncio.create_task") as mock_create_task:
        script_scan_fan(mock_gateway, DEV_ID)
        coro = mock_create_task.call_args[0][0]
        await coro

    assert mock_gateway.send_cmd.called


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::RuntimeWarning")
async def test_script_scan_otb_group(mock_gateway: MagicMock) -> None:
    """Test various OTB scan scripts."""
    # We must await each of these to hit the code inside
    # We patch range here as well to cover the OTB hard scan without nesting another with
    with (
        patch("ramses_cli.discovery.asyncio.create_task") as mock_create_task,
        patch("ramses_cli.discovery.range", return_value=iter([1])),
    ):

        async def run_script(script_fnc: Callable[..., Any]) -> None:
            script_fnc(mock_gateway, DEV_ID)
            coro = mock_create_task.call_args[0][0]
            await coro

        await run_script(script_scan_otb)
        await run_script(script_scan_otb_map)
        await run_script(script_scan_otb_ramses)
        await run_script(script_scan_otb_hard)

    assert mock_gateway.send_cmd.call_count > 5


@pytest.mark.asyncio
async def test_script_binding(mock_gateway: MagicMock) -> None:
    """Test binding scripts."""

    class MockFakeable:
        pass

    mock_gateway.get_device.return_value.__class__ = MockFakeable

    with patch("ramses_cli.discovery.Fakeable", MockFakeable):
        await script_bind_req(mock_gateway, DEV_ID)  # type: ignore[arg-type]
        mock_dev = mock_gateway.get_device(DEV_ID)
        mock_dev._initiate_binding_process.assert_awaited()

        await script_bind_wait(mock_gateway, DEV_ID)  # type: ignore[arg-type]
        mock_dev._wait_for_binding_request.assert_awaited()


@pytest.mark.asyncio
async def test_script_binding_fail(mock_gateway: MagicMock) -> None:
    """Test binding script failure when device is not Fakeable."""
    # Ensure device is NOT Fakeable (it's just a vanilla MagicMock by default)
    # We need to ensure isinstance(mock, Fakeable) returns False.

    class RealFakeable:
        pass

    # Patch Fakeable to be a class that our device definitely IS NOT an instance of
    with patch("ramses_cli.discovery.Fakeable", RealFakeable):
        # script_bind_req is a standard async func, so we await it directly
        with pytest.raises((AssertionError, TypeError)):
            await script_bind_req(mock_gateway, DEV_ID)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_script_poll_device(mock_gateway: MagicMock) -> None:
    """Test script_poll_device task creation."""
    # Must be async test to provide loop for create_task
    tasks = script_poll_device(mock_gateway, DEV_ID)  # type: ignore[arg-type]

    assert len(tasks) == 2  # One for each code (0016, 1FC9)
    assert len(mock_gateway._tasks) == 2
