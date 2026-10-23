"""Unit tests for TopologyChangedEvent bus and Gateway schema callback handshake."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock

import pytest

from ramses_rf import Gateway
from ramses_rf.enums import TopologyAction
from ramses_rf.gateway import GatewayConfig
from ramses_rf.models import TopologyChangedEvent
from ramses_tx.config import EngineConfig
from ramses_tx.typing import DeviceIdT


@pytest.mark.asyncio
async def test_topology_event_properties() -> None:
    # Arrange
    single_event = TopologyChangedEvent(
        action=TopologyAction.UPDATE_DEVICE_CLASS,
        device_id=cast(DeviceIdT, "04:123456"),
    )
    rel_event = TopologyChangedEvent(
        action=TopologyAction.BIND_DEVICE,
        parent_id=cast(DeviceIdT, "01:111111"),
        child_id=cast(DeviceIdT, "04:222222"),
    )

    # Act & Assert
    assert single_event.is_single_device is True
    assert single_event.is_relationship is False
    assert single_event.target_device_id == "04:123456"

    assert rel_event.is_single_device is False
    assert rel_event.is_relationship is True
    assert rel_event.target_device_id == "04:222222"


@pytest.mark.asyncio
async def test_gateway_schema_updated_callback_handshake() -> None:
    # Arrange
    cfg = GatewayConfig(engine=EngineConfig(disable_sending=True))
    gwy = Gateway("/dev/ttyUSB0", config=cfg)
    callback_mock = AsyncMock()

    # Act
    gwy.set_schema_updated_callback(callback_mock)

    # Assert: Callback getter returns configured callback
    assert gwy.schema_updated_callback is not None

    # Act: Trigger notification helper directly
    await gwy._notify_schema_updated()

    # Assert: Callback was invoked with schema dictionary
    callback_mock.assert_called_once()
    schema_arg = callback_mock.call_args[0][0]
    assert isinstance(schema_arg, dict)
    assert "main_tcs" in schema_arg


@pytest.mark.asyncio
async def test_device_registry_topology_event_triggers_schema_callback() -> (
    None
):
    # Arrange
    cfg = GatewayConfig(engine=EngineConfig(disable_sending=True))
    gwy = Gateway("/dev/ttyUSB0", config=cfg)
    callback_mock = AsyncMock()
    gwy.set_schema_updated_callback(callback_mock)

    event = TopologyChangedEvent(
        action=TopologyAction.UPDATE_DEVICE_CLASS,
        device_id=cast(DeviceIdT, "04:123456"),
    )

    # Act: Dispatch topology event to device registry
    gwy.device_registry.handle_topology_event(event)
    await asyncio.sleep(0.05)  # Allow background create_task to execute

    # Assert: Callback received schema update
    callback_mock.assert_called_once()


# ── TopologyChangedEvent structural checks (from ha_sim_test R54) ──────


def test_topology_event_is_frozen() -> None:
    """TopologyChangedEvent is immutable (frozen dataclass)."""
    event = TopologyChangedEvent(
        action=TopologyAction.BIND_DEVICE,
        parent_id=cast(DeviceIdT, "01:111111"),
        child_id=cast(DeviceIdT, "04:222222"),
    )
    with pytest.raises((AttributeError, Exception)):
        event.action = TopologyAction.CREATE_CONTROLLER  # type: ignore[misc]


def test_topology_event_has_metadata() -> None:
    """TopologyChangedEvent carries metadata dict."""
    event = TopologyChangedEvent(
        action=TopologyAction.BIND_DEVICE,
        parent_id=cast(DeviceIdT, "01:111111"),
        child_id=cast(DeviceIdT, "04:222222"),
        metadata={"zone_index": "01"},
    )
    assert "zone_index" in event.metadata


def test_topology_event_has_event_id() -> None:
    """TopologyChangedEvent has a UUID event_id."""
    event = TopologyChangedEvent(
        action=TopologyAction.BIND_DEVICE,
        parent_id=cast(DeviceIdT, "01:111111"),
        child_id=cast(DeviceIdT, "04:222222"),
    )
    assert hasattr(event, "event_id")


def test_topology_action_has_all_values() -> None:
    """TopologyAction enum has all expected values."""
    expected = {
        "update_traits",
        "bind_device",
        "create_controller",
        "create_circuit",
    }
    actual = {str(a) for a in TopologyAction}
    assert expected.issubset(actual), f"missing: {expected - actual}"
    # PR 914 renamed PROMOTE_CLASS to UPDATE_DEVICE_CLASS
    assert "promote_class" in actual or "update_device_class" in actual
