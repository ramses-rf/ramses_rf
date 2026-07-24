from datetime import UTC, datetime as dt, timedelta as td
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ramses_rf.config import GatewayConfig
from ramses_rf.devices.dev_base import DeviceBase
from ramses_rf.discovery import DiscoveryService
from ramses_rf.pipeline.polling import DEFAULT_POLLING_SCHEDULES, PollingManager
from ramses_tx import CommandDTO


class MockDevice(DeviceBase):
    """Mock device subclass for testing PollingManager cutover."""

    def __init__(
        self,
        gwy: Any,
        device_id: str,
        slug: str = "CTL",
    ) -> None:
        mock_addr = MagicMock()
        mock_addr.id = device_id
        mock_addr.type = device_id[:2]
        super().__init__(gwy, mock_addr)
        self._SLUG = slug


@pytest.fixture
def mock_gateway() -> MagicMock:
    """Create a mock gateway instance."""
    gwy = MagicMock()
    gwy.config = GatewayConfig()
    gwy.device_registry.devices = []
    gwy.async_send_cmd = AsyncMock()
    gwy.add_task = MagicMock()
    return gwy


@pytest.mark.asyncio
async def test_polling_manager_live_dispatch_cutover(
    mock_gateway: MagicMock,
) -> None:
    # ARRANGE
    poller = PollingManager(mock_gateway, shadow_mode=False)
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
    # Live execution cutover guarantee: async_send_cmd MUST be called for each task
    assert mock_gateway.async_send_cmd.call_count == processed_count

    # Verify first dispatched CommandDTO structure
    call_args = mock_gateway.async_send_cmd.call_args_list[0][0]
    sent_dto: CommandDTO = call_args[0]
    assert sent_dto.verb == "RQ"
    assert sent_dto.addr1 == "01:111111"
    assert sent_dto.addr2 == "01:111111"


def test_legacy_discovery_poller_disabled(
    mock_gateway: MagicMock,
) -> None:
    # ARRANGE
    mock_entity = MagicMock()
    mock_entity.id = "01:111111"
    disc = DiscoveryService(mock_entity, mock_gateway)

    # ACT
    disc.start_poller()

    # ASSERT
    # Legacy discovery start_poller is deactivated in Phase 4c.3 in favor of L7 PollingManager
    assert disc._poller is None
