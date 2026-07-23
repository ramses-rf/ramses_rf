# tests/tests_rf/test_device_registry.py
"""Tests for the decoupled independent Device Registry."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest

from ramses_rf.address import Address
from ramses_rf.config import GatewayConfig
from ramses_rf.devices.dev_filter import DeviceFilter
from ramses_rf.devices.dev_registry import DeviceRegistry
from ramses_rf.enums import TopologyAction
from ramses_rf.exceptions import DeviceNotFoundError, SchemaInconsistentError
from ramses_rf.models import DeviceTraits, TopologyChangedEvent
from ramses_rf.typing import DeviceIdT

if TYPE_CHECKING:
    from ramses_rf.devices.dev_base import Device
    from ramses_rf.messages import Message

_LOGGER = logging.getLogger(__name__)


@pytest.fixture
def mock_device_factory() -> MagicMock:
    """Provide a mock device factory callback.

    :returns: A MagicMock simulating device_factory_cb.
    :rtype: MagicMock
    """
    return MagicMock()


@pytest.fixture
def standalone_registry(mock_device_factory: MagicMock) -> DeviceRegistry:
    """Provide a standalone DeviceRegistry instantiated with zero Gateway
    context.

    :param mock_device_factory: The mocked entity creation callback.
    :type mock_device_factory: MagicMock
    :returns: A clean DeviceRegistry instance.
    :rtype: DeviceRegistry
    """
    config = GatewayConfig()
    config.engine.enforce_known_list = False

    device_filter = DeviceFilter(
        include=[],
        exclude=[],
        unwanted=[],
        enforce_known_list=False,
        hgi_id_provider=lambda: None,
    )

    return DeviceRegistry(
        device_filter=device_filter,
        config=config,
        device_factory_cb=mock_device_factory,
    )


def test_registry_add_and_retrieve_device(
    standalone_registry: DeviceRegistry,
) -> None:
    """Test adding a device explicitly to the registry tracking dictionaries.

    :param standalone_registry: The test device registry fixture.
    :type standalone_registry: DeviceRegistry
    :returns: None
    """
    mock_dev = MagicMock()
    mock_dev.id = cast(DeviceIdT, "01:123456")

    # Verify initial state
    assert mock_dev.id not in standalone_registry.device_by_id

    # Add and verify identity tracking
    standalone_registry._add_device(mock_dev)
    assert standalone_registry.device_by_id[mock_dev.id] is mock_dev
    assert mock_dev in standalone_registry.devices


def test_registry_duplicate_device_raises_error(
    standalone_registry: DeviceRegistry,
) -> None:
    """Test that attempting to add a duplicate device ID triggers a consistency
    exception.

    :param standalone_registry: The test device registry fixture.
    :type standalone_registry: DeviceRegistry
    :returns: None
    """
    mock_dev = MagicMock()
    mock_dev.id = cast(DeviceIdT, "01:123456")

    standalone_registry._add_device(mock_dev)

    with pytest.raises(SchemaInconsistentError, match="Device already exists"):
        standalone_registry._add_device(mock_dev)


def test_registry_get_device_triggers_callback(
    standalone_registry: DeviceRegistry,
    mock_device_factory: MagicMock,
) -> None:
    """Test that get_device invokes the injected factory callback if the entity
    does not exist.

    :param standalone_registry: The test device registry fixture.
    :type standalone_registry: DeviceRegistry
    :param mock_device_factory: The mocked entity creation callback.
    :type mock_device_factory: MagicMock
    :returns: None
    """
    dev_id = cast(DeviceIdT, "04:111111")
    mock_spawned_device = MagicMock()
    mock_spawned_device.id = dev_id
    mock_device_factory.return_value = mock_spawned_device

    # Trigger entity lookup/creation
    result_dev = standalone_registry.get_device(dev_id)

    # Verify the callback was triggered with the correct abstract L7 args
    mock_device_factory.assert_called_once()
    args, _ = mock_device_factory.call_args
    assert isinstance(args[0], Address)
    assert args[0].id == dev_id
    assert isinstance(args[2], DeviceTraits)

    # Mock object verification
    assert result_dev is mock_spawned_device


def test_registry_enforces_filter_lists() -> None:
    """Test that the registry respects the boundaries set by the injected
    DeviceFilter service.

    :returns: None
    """
    blocked_id = cast(DeviceIdT, "04:999999")

    # Build a filter that actively flags the ID as invalid or unwanted
    device_filter = DeviceFilter(
        include=[],
        exclude=[blocked_id],
        unwanted=[],
        enforce_known_list=True,
        hgi_id_provider=lambda: None,
    )

    registry = DeviceRegistry(
        device_filter=device_filter,
        config=GatewayConfig(),
        device_factory_cb=MagicMock(),
    )

    with pytest.raises(DeviceNotFoundError):
        registry.get_device(blocked_id)


@pytest.mark.asyncio
async def test_fan_promotion_race_condition() -> None:
    """Test that early PROMOTE_CLASS events correctly apply via the SSOT.

    This reproduces the race condition where a TopologyBuilder emits a
    promotion event before the packet processing pipeline has actually
    instantiated the device in memory.
    """
    # Arrange
    config = GatewayConfig(known_list={})
    filter_mock = MagicMock()
    captured_traits: list[DeviceTraits] = []

    def mock_device_factory(
        addr: Address,
        msg: Message | None,
        traits: DeviceTraits,
    ) -> Device:
        """Mock factory to track what traits were passed at creation."""
        captured_traits.append(traits)
        dev_mock = MagicMock()
        dev_mock.id = addr.id
        dev_mock.addr = addr
        dev_mock._SLUG = traits.device_class
        return cast("Device", dev_mock)

    registry = DeviceRegistry(filter_mock, config, mock_device_factory)
    fan_id = "32:111111"

    event = TopologyChangedEvent(
        action=TopologyAction.UPDATE_DEVICE_CLASS,
        device_id=fan_id,
        metadata={"device_class": "FAN"},
        causation="test_eavesdrop",
    )

    # Act 1: The TopologyBuilder fires the event early (Race Condition)
    registry.handle_topology_event(event)

    # Act 2: The packet pipeline finally requests the device
    dev = registry.get_device(fan_id)

    # Assert
    # 1. The registry must have mutated the Single Source of Truth
    assert fan_id in config.known_list
    assert config.known_list[fan_id].get("class") == "ventilator"

    # 2. The factory must have received the correct promoted traits
    assert len(captured_traits) == 1
    assert captured_traits[0].device_class == "ventilator"

    # 3. The returned device must act as the promoted class
    assert getattr(dev, "_SLUG", None) == "ventilator"
