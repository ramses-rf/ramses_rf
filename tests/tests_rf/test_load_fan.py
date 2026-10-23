#!/usr/bin/env python3
"""Tests for ramses_rf.schemas.load_fan — verifies it is implemented, not a stub."""

from __future__ import annotations

import inspect
from collections.abc import Generator
from typing import cast
from unittest.mock import MagicMock

import pytest

from ramses_rf.devices import HvacVentilator
from ramses_rf.gateway import Gateway
from ramses_rf.schemas import load_fan
from ramses_rf.state import MessageStore
from ramses_tx import Address
from ramses_tx.typing import DeviceIdT

FAN_DEVICE_ID = "32:153289"
REMOTE_DEVICE_ID = "32:111111"
SENSOR_DEVICE_ID = "32:222222"


@pytest.fixture
def mock_gateway() -> Generator[MagicMock, None, None]:
    """Create a mock Gateway with a real device_registry mapping."""
    gateway = MagicMock(spec=Gateway)
    gateway.config = MagicMock()
    gateway.config.disable_discovery = False
    gateway.config.enable_eavesdrop = False
    gateway._loop = MagicMock()
    gateway._loop.call_soon = MagicMock()
    gateway._loop.call_later = MagicMock()
    gateway._loop.time = MagicMock(return_value=0.0)
    gateway._include = {}
    gateway.message_store = MessageStore(maintain=False)

    engine = MagicMock()
    engine.enforce_known_list = False
    engine._enforce_known_list = False
    engine._exclude = {}
    engine._include = {}
    engine.include_list = {}
    gateway._engine = engine
    gateway.engine = engine

    registry = MagicMock()
    registry.device_by_id = {}

    # get_device must return a real device when present in the registry,
    # otherwise create one via the HvacVentilator / Device constructor.
    def _get_device(dev_id: str, **kwargs: object) -> object:
        if dev_id in registry.device_by_id:
            return registry.device_by_id[dev_id]
        dev = HvacVentilator(gateway, Address(cast(DeviceIdT, dev_id)))
        registry.device_by_id[dev_id] = dev
        return dev

    registry.get_device = _get_device
    gateway.device_registry = registry

    yield gateway


def _make_fan(gateway: MagicMock) -> HvacVentilator:
    """Create a real HvacVentilator registered in the mock registry."""
    fan = HvacVentilator(gateway, Address(cast(DeviceIdT, FAN_DEVICE_ID)))
    gateway.device_registry.device_by_id[FAN_DEVICE_ID] = fan
    return fan


def test_load_fan_source_contains_update_schema() -> None:
    """load_fan must delegate to _update_schema (not be a stub)."""
    source = inspect.getsource(load_fan)
    assert "_update_schema" in source, (
        "load_fan does not call _update_schema — it may be a stub"
    )


def test_load_fan_source_has_no_todo_marker() -> None:
    """load_fan must not contain a # TODO stub marker."""
    source = inspect.getsource(load_fan)
    assert "# TODO" not in source, (
        "load_fan contains a # TODO marker — it may be a stub"
    )


def test_load_fan_processes_remotes_and_sensors(
    mock_gateway: MagicMock,
) -> None:
    """load_fan must process remotes/sensors from the FAN schema."""
    fan = _make_fan(mock_gateway)

    schema = {
        "remotes": [REMOTE_DEVICE_ID],
        "sensors": [SENSOR_DEVICE_ID],
    }

    result = load_fan(mock_gateway, cast(DeviceIdT, FAN_DEVICE_ID), schema)

    # load_fan returns the FAN device
    assert result is fan

    # remotes/sensors were registered on the FAN
    assert REMOTE_DEVICE_ID in fan._remote_ids
    assert SENSOR_DEVICE_ID in fan._sensor_ids

    # the child devices were created in the registry
    assert REMOTE_DEVICE_ID in mock_gateway.device_registry.device_by_id
    assert SENSOR_DEVICE_ID in mock_gateway.device_registry.device_by_id

    if fan._gateway.message_store:
        fan._gateway.message_store.stop()
