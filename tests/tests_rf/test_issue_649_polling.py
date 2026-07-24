"""TDD Test for Issue #649: Polling task schedule resolution for FAN devices."""

from unittest.mock import MagicMock

import pytest

from ramses_rf.address import Address
from ramses_rf.devices.dev_registry import DeviceRegistry
from ramses_rf.devices.hvac_ventilators import FilterChange
from ramses_rf.models import DeviceTraits
from ramses_rf.pipeline.polling import PollingManager


@pytest.mark.asyncio
async def test_issue_649_polling_schedule_populated() -> None:
    # Arrange
    mock_gwy = MagicMock()
    mock_gwy.config = MagicMock()
    mock_gwy.config.disable_discovery = False
    mock_gwy.config.known_list = {}
    mock_gwy.config.hgi_id = "18:000000"

    def mock_factory(
        addr: Address, msg: MagicMock, traits: DeviceTraits
    ) -> FilterChange:
        return FilterChange(mock_gwy, addr, traits=traits)

    registry = DeviceRegistry(
        device_filter=MagicMock(),
        config=mock_gwy.config,
        device_factory_cb=mock_factory,
    )

    # Act
    dev = registry.get_device("32:111111")

    # Assert
    schedule = PollingManager.resolve_schedule_for_device(dev)
    assert "10D0" in schedule, "10D0 filter poll not scheduled in PollingManager"
