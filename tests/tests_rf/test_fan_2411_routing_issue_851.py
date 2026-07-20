#!/usr/bin/env python3
"""Regression test for ramses_cc issue 851 — FAN loses 15 entities.

Phase 2.95 (commit fc1381b2, "Partial Lobotomy") removed the
``HvacVentilator._handle_msg`` override that previously invoked
``_handle_2411_message`` (sets ``_supports_2411`` and stores the
parameter) and ``_handle_initialized_callback`` (fires the ramses_cc
entity-creation callback).  The replacement CQRS pipeline never
re-wired 2411 routing, so FAN devices never advertised 2411 support
and ramses_cc never created the ~15 parameter ``number`` entities
(comfort temperature, etc.).

These tests verify that 2411 `` I``/``RP`` messages routed through the
CQRS ingestion pipeline (both the dispatcher path and the
StateProjector path) flip ``supports_2411`` to True and fire the
initialized callback on the target FAN device.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import cast
from unittest.mock import MagicMock

import pytest

from ramses_rf import dispatcher
from ramses_rf.const import DevType
from ramses_rf.devices import HvacVentilator
from ramses_rf.gateway import Gateway
from ramses_rf.pipeline.ingestion import StateProjector
from ramses_rf.state import MessageStore
from ramses_tx import Address
from ramses_tx.const import Code
from ramses_tx.typing import DeviceIdT

TEST_DEVICE_ID = "32:153289"
TEST_PARAM_ID = "3F"
TEST_PARAM_VALUE = 50


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

    # Use a real dict so device_by_id lookups behave naturally
    registry = MagicMock()
    registry.device_by_id = {}
    gateway.device_registry = registry

    yield gateway


def _make_fan(gateway: MagicMock) -> HvacVentilator:
    """Create a real HvacVentilator registered in the mock registry."""
    fan = HvacVentilator(gateway, Address(DeviceIdT(TEST_DEVICE_ID)))
    gateway.device_registry.device_by_id[TEST_DEVICE_ID] = fan
    return fan


def _make_2411_msg(verb: str = " I") -> MagicMock:
    """Create a mock 2411 message whose src/dst resolve to the FAN."""
    msg = MagicMock()
    msg.code = Code._2411
    msg.verb = verb
    msg.src = MagicMock()
    msg.src.id = TEST_DEVICE_ID
    msg.dst = MagicMock()
    msg.dst.id = TEST_DEVICE_ID
    msg.payload = {"parameter": TEST_PARAM_ID, "value": TEST_PARAM_VALUE}
    return msg


class TestDispatcher2411Routing:
    """Verify dispatcher._cqrs_ingestion_engine routes 2411 to the FAN."""

    def test_2411_info_flips_supports_and_fires_callback(
        self, mock_gateway: MagicMock
    ) -> None:
        """A 2411 `` I`` packet must set supports_2411 and fire the callback."""
        fan = _make_fan(mock_gateway)
        assert fan._supports_2411 is False

        callback = MagicMock()
        fan.set_initialized_callback(callback)
        assert fan._initialized_callback is callback

        msg = _make_2411_msg(verb=" I")
        dispatcher._cqrs_ingestion_engine(mock_gateway, msg)

        assert fan._supports_2411 is True, "supports_2411 was not flipped by dispatcher"
        assert TEST_PARAM_ID in fan._params_2411
        assert fan._params_2411[TEST_PARAM_ID] == TEST_PARAM_VALUE
        callback.assert_called_once()
        # Callback is one-shot: must be cleared after firing
        assert fan._initialized_callback is None

        if fan._gwy.message_store:
            fan._gwy.message_store.stop()

    def test_2411_rp_also_routed(self, mock_gateway: MagicMock) -> None:
        """A 2411 ``RP`` reply must also be routed (only RQ is skipped)."""
        fan = _make_fan(mock_gateway)
        msg = _make_2411_msg(verb="RP")
        dispatcher._cqrs_ingestion_engine(mock_gateway, msg)

        assert fan._supports_2411 is True

        if fan._gwy.message_store:
            fan._gwy.message_store.stop()

    def test_2411_rq_not_routed(self, mock_gateway: MagicMock) -> None:
        """A 2411 ``RQ`` request carries no telemetry and must be skipped."""
        fan = _make_fan(mock_gateway)
        msg = _make_2411_msg(verb="RQ")
        dispatcher._cqrs_ingestion_engine(mock_gateway, msg)

        assert fan._supports_2411 is False, "RQ must not flip supports_2411"
        assert fan._params_2411 == {}

        if fan._gwy.message_store:
            fan._gwy.message_store.stop()

    def test_non_fan_target_not_affected(self, mock_gateway: MagicMock) -> None:
        """A non-FAN device in the registry must not be touched by 2411 routing."""
        fan = _make_fan(mock_gateway)
        # Add a non-FAN device (a generic mock) at a different id
        other = MagicMock()
        other._SLUG = DevType.CTL
        other.id = "01:000001"
        mock_gateway.device_registry.device_by_id["01:000001"] = other

        msg = _make_2411_msg(verb=" I")
        dispatcher._cqrs_ingestion_engine(mock_gateway, msg)

        # FAN is the src/dst, so it gets routed; the CTL device is not a target
        assert fan._supports_2411 is True
        other._handle_2411_message.assert_not_called()

        if fan._gwy.message_store:
            fan._gwy.message_store.stop()


class TestStateProjector2411Routing:
    """Verify StateProjector._route_2411_to_fan mirrors the dispatcher path."""

    def test_state_projector_routes_2411(self, mock_gateway: MagicMock) -> None:
        """The ingestion StateProjector must also route 2411 to the FAN."""
        fan = _make_fan(mock_gateway)
        callback = MagicMock()
        fan.set_initialized_callback(callback)

        projector = StateProjector(cast(MagicMock, mock_gateway), MagicMock())
        msg = _make_2411_msg(verb=" I")
        projector._route_2411_to_fan(msg)

        assert fan._supports_2411 is True
        assert fan._params_2411.get(TEST_PARAM_ID) == TEST_PARAM_VALUE
        callback.assert_called_once()

        if fan._gwy.message_store:
            fan._gwy.message_store.stop()

    def test_state_projector_process_message_state_routes_2411(
        self, mock_gateway: MagicMock
    ) -> None:
        """process_message_state must trigger 2411 routing for a 2411 msg."""
        fan = _make_fan(mock_gateway)
        projector = StateProjector(cast(MagicMock, mock_gateway), MagicMock())

        msg = _make_2411_msg(verb=" I")
        projector.process_message_state(msg)

        assert fan._supports_2411 is True, (
            "process_message_state did not route 2411 to the FAN"
        )

        if fan._gwy.message_store:
            fan._gwy.message_store.stop()
