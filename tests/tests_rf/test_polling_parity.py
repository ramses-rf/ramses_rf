from datetime import UTC, datetime as dt, timedelta as td
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ramses_rf.config import GatewayConfig
from ramses_rf.devices.dev_base import BatteryState, DeviceBase
from ramses_rf.models import DeviceTraits
from ramses_rf.pipeline.polling import DEFAULT_POLLING_SCHEDULES, PollingManager


class MockDevice(DeviceBase):
    """Mock device subclass for unit testing PollingManager schedule resolution."""

    def __init__(
        self,
        gwy: Any,
        device_id: str,
        slug: str = "DEFAULT",
        traits: DeviceTraits | None = None,
    ) -> None:
        mock_addr = MagicMock()
        mock_addr.id = device_id
        mock_addr.type = device_id[:2]
        super().__init__(gwy, mock_addr, traits=traits)
        self._SLUG = slug


class MockBatteryDevice(BatteryState):
    """Mock battery-powered device subclass for unit testing."""

    def __init__(
        self,
        gwy: Any,
        device_id: str,
        traits: DeviceTraits | None = None,
    ) -> None:
        mock_addr = MagicMock()
        mock_addr.id = device_id
        mock_addr.type = device_id[:2]
        super().__init__(gwy, mock_addr, traits=traits)
        self._SLUG = "TRV"


@pytest.fixture
def mock_gateway() -> MagicMock:
    """Create a mock gateway instance with mock device registry and config."""
    gwy = MagicMock()
    gwy.config = GatewayConfig()
    gwy.device_registry.devices = []
    gwy.async_send_cmd = AsyncMock()
    gwy.add_task = MagicMock()
    return gwy


def test_polling_manager_schedule_resolution_defaults(
    mock_gateway: MagicMock,
) -> None:
    # ARRANGE
    poller = PollingManager(mock_gateway, shadow_mode=True)

    ctl_dev = MockDevice(mock_gateway, "01:111111", slug="CTL")
    bdr_dev = MockDevice(mock_gateway, "13:222222", slug="BDR")
    fan_dev = MockDevice(mock_gateway, "32:333333", slug="FAN")

    # ACT
    ctl_schedule = poller.resolve_schedule_for_device(ctl_dev)
    bdr_schedule = poller.resolve_schedule_for_device(bdr_dev)
    fan_schedule = poller.resolve_schedule_for_device(fan_dev)

    # ASSERT
    assert ctl_schedule == DEFAULT_POLLING_SCHEDULES["CTL"]
    assert bdr_schedule == DEFAULT_POLLING_SCHEDULES["BDR"]
    assert fan_schedule == DEFAULT_POLLING_SCHEDULES["FAN"]


def test_polling_manager_custom_trait_override(
    mock_gateway: MagicMock,
) -> None:
    # ARRANGE
    custom_traits = DeviceTraits(polling_interval={"1F41": 1800, "10E0": 43200})
    ctl_dev = MockDevice(mock_gateway, "01:111111", slug="CTL", traits=custom_traits)
    poller = PollingManager(mock_gateway, shadow_mode=True)

    # ACT
    schedule = poller.resolve_schedule_for_device(ctl_dev)

    # ASSERT
    assert schedule["1F41"] == 1800
    assert schedule["10E0"] == 43200
    assert schedule["313F"] == DEFAULT_POLLING_SCHEDULES["CTL"]["313F"]


def test_polling_manager_battery_device_zero_polling(
    mock_gateway: MagicMock,
) -> None:
    # ARRANGE
    battery_dev = MockBatteryDevice(mock_gateway, "04:222222")
    explicit_battery_dev = MockDevice(
        mock_gateway, "04:333333", slug="TRV", traits=DeviceTraits(is_battery=True)
    )
    poller = PollingManager(mock_gateway, shadow_mode=True)

    # ACT
    battery_schedule = poller.resolve_schedule_for_device(battery_dev)
    explicit_schedule = poller.resolve_schedule_for_device(explicit_battery_dev)

    # ASSERT
    # Battery devices sleep and do not listen to RF commands; schedule must be empty
    assert battery_schedule == {}
    assert explicit_schedule == {}


@pytest.mark.asyncio
async def test_polling_manager_shadow_execution_parity(
    mock_gateway: MagicMock,
) -> None:
    # ARRANGE
    poller = PollingManager(mock_gateway, shadow_mode=True)
    ctl_dev = MockDevice(mock_gateway, "01:111111", slug="CTL")
    mock_gateway.device_registry.devices = [ctl_dev]

    poller.update_device_tasks(ctl_dev)

    # Fast-forward next_due to trigger due commands
    past = dt.now(UTC) - td(seconds=10)
    for task in poller.get_scheduled_cmds():
        task.next_due = past

    # ACT
    processed_count = await poller.poll_due_commands()

    # ASSERT
    assert processed_count == len(DEFAULT_POLLING_SCHEDULES["CTL"])
    # Crucial Shadow Mode Guarantee: Zero RF network transmissions
    mock_gateway.async_send_cmd.assert_not_called()


@pytest.mark.asyncio
async def test_polling_manager_disabled_config_parity(
    mock_gateway: MagicMock,
) -> None:
    # ARRANGE
    mock_gateway.config.disable_polling = True
    poller = PollingManager(mock_gateway, shadow_mode=True)
    ctl_dev = MockDevice(mock_gateway, "01:111111", slug="CTL")
    mock_gateway.device_registry.devices = [ctl_dev]

    # ACT
    processed_count = await poller.poll_due_commands()

    # ASSERT
    assert processed_count == 0
    mock_gateway.async_send_cmd.assert_not_called()
