#!/usr/bin/env python3
"""Unittests for the base device classes and HgiGateway.

This module combines tests for DeviceBase, HgiGateway, and BatteryState
which all reside in ramses_rf/device/base.py.
"""

from __future__ import annotations

from datetime import UTC, datetime as dt, timedelta as td
from unittest.mock import MagicMock

import pytest

from ramses_rf.const import GATEWAY_MESSAGE_TIMEOUT
from ramses_rf.devices.dev_base import BatteryState, DeviceBase, HgiGateway
from ramses_rf.gateway import Gateway
from ramses_tx import Address
from ramses_tx.rssi_tracker import RssiTracker


@pytest.fixture
def mock_gateway() -> MagicMock:
    """Create a mock Gateway instance for testing.

    :return: A mocked Gateway object.
    :rtype: MagicMock
    """
    gwy = MagicMock(spec=Gateway)
    gwy.config.enable_eavesdrop = False
    gwy.config.gateway_timeout = None
    gwy.config.timezone = None
    gwy.config.tzinfo = None
    gwy.tzinfo = None
    gwy._engine = MagicMock()
    gwy._engine._protocol = MagicMock()
    gwy._engine._protocol._this_msg = None
    gwy._this_msg = None
    return gwy


@pytest.fixture
def hgi_gateway(mock_gateway: MagicMock) -> HgiGateway:
    """Create an HgiGateway instance for testing.

    :param mock_gateway: The mock gateway fixture.
    :type mock_gateway: MagicMock
    :return: An initialized HgiGateway.
    :rtype: HgiGateway
    """
    # HGI devices always have an address starting with 18:
    return HgiGateway(mock_gateway, Address("18:123456"))


class TestDeviceBase:
    """Test the DeviceBase logic."""

    def test_heartbeat_availability(self, mock_gateway: MagicMock) -> None:
        """Test is_available heartbeat logic.

        :param mock_gateway: The mock gateway fixture.
        :type mock_gateway: MagicMock
        """
        dev = DeviceBase(mock_gateway, Address("34:123456"))

        # No messages yet - assume available
        assert dev.is_available

        # Recent message
        dev._last_msg_dtm = dt.now(UTC)
        assert dev.is_available

        # Expired heartbeat (Default 1 hour)
        expired_dtm = dt.now(UTC) - td(hours=1, seconds=1)
        dev._last_msg_dtm = expired_dtm
        assert not dev.is_available

    def test_device_promotion_prevention(
        self, mock_gateway: MagicMock
    ) -> None:
        """Test that non-promotable slugs don't trigger promotion.

        :param mock_gateway: The mock gateway fixture.
        :type mock_gateway: MagicMock
        """
        dev = DeviceBase(mock_gateway, Address("34:123456"))

        # Explicitly set slug to ensure it's not in PROMOTABLE_SLUGS
        dev._SLUG = "NON_PROMOTABLE_SLUG"

        dev._last_msg_dtm = dt.now(UTC)

        # Verify the class was not promoted using __class__ to satisfy Mypy
        assert dev.__class__ is DeviceBase

    @pytest.mark.asyncio
    async def test_async_attributes(self, mock_gateway: MagicMock) -> None:
        """Test async baseline properties.

        :param mock_gateway: The mock gateway fixture.
        :type mock_gateway: MagicMock
        """
        dev = DeviceBase(mock_gateway, Address("34:123456"))
        assert await dev.schema() == {}
        assert await dev.params() == {}
        assert await dev.status() == {}


class TestBatteryState:
    """Test the BatteryState mixin class logic."""

    @pytest.mark.asyncio
    async def test_battery_methods_when_faked(
        self, mock_gateway: MagicMock
    ) -> None:
        """Test battery_low and battery_state return defaults if faked.

        :param mock_gateway: The mock gateway fixture.
        :type mock_gateway: MagicMock
        """
        dev = BatteryState(mock_gateway, Address("04:123456"))

        # Simulate a faked device by attaching a mocked binding manager
        dev._binding_manager = MagicMock()
        dev._binding_manager.is_binding = False

        assert dev.is_faked
        assert await dev.battery_low() is False
        assert await dev.battery_state() is None


class TestHgiGateway:
    """Test HgiGateway class."""

    def test_initialization(self, hgi_gateway: HgiGateway) -> None:
        """Test that the gateway device initializes with expected
        defaults.

        :param hgi_gateway: The gateway fixture.
        :type hgi_gateway: HgiGateway
        """
        # Bypassing strict type inference with getattr
        assert getattr(hgi_gateway, "ctl", False) is None
        assert hgi_gateway._child_id == "gw"
        assert getattr(hgi_gateway, "tcs", False) is None

    @pytest.mark.asyncio
    async def test_is_active_no_msg(self, hgi_gateway: HgiGateway) -> None:
        """Test is_active returns False when no messages are received.

        :param hgi_gateway: The gateway fixture.
        :type hgi_gateway: HgiGateway
        """
        hgi_gateway._gateway._engine._protocol._this_msg = None
        assert not await hgi_gateway.is_active()

    @pytest.mark.asyncio
    async def test_is_active_recent_msg(self, hgi_gateway: HgiGateway) -> None:
        """Test is_active returns True when a recent message exists.

        :param hgi_gateway: The gateway fixture.
        :type hgi_gateway: HgiGateway
        """
        mock_msg = MagicMock()
        mock_msg.timestamp = dt.now(UTC)

        hgi_gateway._gateway._engine._protocol._this_msg = mock_msg
        assert await hgi_gateway.is_active()

    @pytest.mark.asyncio
    async def test_is_active_expired_msg(
        self, hgi_gateway: HgiGateway
    ) -> None:
        """Test is_active returns False when the latest message is too
        old.

        :param hgi_gateway: The gateway fixture.
        :type hgi_gateway: HgiGateway
        """
        mock_msg = MagicMock()
        expired_dtm = dt.now(UTC) - (GATEWAY_MESSAGE_TIMEOUT + td(seconds=1))
        mock_msg.timestamp = expired_dtm

        hgi_gateway._gateway._engine._protocol._this_msg = mock_msg
        assert not await hgi_gateway.is_active()

    @pytest.mark.asyncio
    async def test_is_active_naive_datetime(
        self, hgi_gateway: HgiGateway
    ) -> None:
        """Test is_active handles naive datetimes gracefully.

        :param hgi_gateway: The gateway fixture.
        :type hgi_gateway: HgiGateway
        """
        mock_msg = MagicMock()
        mock_msg.timestamp = dt.now()

        hgi_gateway._gateway._engine._protocol._this_msg = mock_msg
        assert await hgi_gateway.is_active()

    def test_message_timeout_custom(self, hgi_gateway: HgiGateway) -> None:
        """Test that the custom gateway timeout is correctly extracted.

        :param hgi_gateway: The gateway fixture.
        :type hgi_gateway: HgiGateway
        """
        # Inject a custom timeout into the mocked gateway config
        hgi_gateway._gateway.config.gateway_timeout = 15

        assert hgi_gateway.message_timeout == td(minutes=15)

    @pytest.mark.asyncio
    async def test_is_active_custom_timeout(
        self, hgi_gateway: HgiGateway
    ) -> None:
        """Test is_active evaluates correctly against a custom timeout.

        :param hgi_gateway: The gateway fixture.
        :type hgi_gateway: HgiGateway
        """
        # Set a custom timeout of 15 minutes
        hgi_gateway._gateway.config.gateway_timeout = 15

        mock_msg = MagicMock()
        # Create a timestamp 10 minutes in the past
        # Under the default 5-minute timeout, this would be inactive.
        # Under our custom 15-minute timeout, this must be active.
        mock_msg.timestamp = dt.now(UTC) - td(minutes=10)

        hgi_gateway._gateway._engine._protocol._this_msg = mock_msg
        assert await hgi_gateway.is_active() is True


class TestCommunicationQuality:
    """Test DeviceBase.communication_quality with pooled transports.

    Regression tests for the fix that gathers RSSI trackers from all
    connected pool children via ``transport.get_extra_info(
    "pool_rssi_trackers")`` so that ``best_rssi`` reflects the strongest
    signal across all HGIs, not just the primary.
    """

    def _make_pool_transport(self, trackers: list[RssiTracker]) -> MagicMock:
        """Create a mock transport that exposes pool_rssi_trackers.

        :param trackers: The list of RssiTracker objects to return.
        :type trackers: list[RssiTracker]
        :return: A mock transport object.
        :rtype: MagicMock
        """
        transport = MagicMock()
        transport.get_extra_info = lambda name, default=None: (
            trackers if name == "pool_rssi_trackers" else default
        )
        return transport

    def _make_gateway_with_tracker(
        self, tracker: RssiTracker, transport: MagicMock | None = None
    ) -> MagicMock:
        """Create a mock gateway with an RSSI tracker and optional transport.

        :param tracker: The gateway's primary RSSI tracker.
        :type tracker: RssiTracker
        :param transport: Optional transport (e.g. PooledTransport).
        :type transport: MagicMock | None
        :return: A mock gateway.
        :rtype: MagicMock
        """
        gwy = MagicMock(spec=Gateway)
        gwy._rssi_tracker = tracker
        gwy._engine = MagicMock()
        gwy._engine._transport = transport
        return gwy

    def test_single_hgi_no_pool(self) -> None:
        """Without a pool, communication_quality uses the gateway tracker."""
        now = dt(2026, 1, 1, 12, 0, 0)
        tracker = RssiTracker(ttl=None)
        tracker.record("32:153289", "-70", now)

        gwy = self._make_gateway_with_tracker(tracker, transport=None)
        dev = DeviceBase(gwy, Address("32:153289"))
        dev._last_msg_dtm = now

        q = dev.communication_quality
        assert q is not None
        assert q.best_rssi == -70
        assert q.rssi_quality == "normal"

    def test_pool_uses_strongest_across_all_hgis(self) -> None:
        """With 2 pool trackers, best_rssi is the strongest (-39 vs -89)."""
        now = dt(2026, 1, 1, 12, 0, 0)

        # Primary gateway tracker (would show weak signal alone)
        gw_tracker = RssiTracker(ttl=None)
        gw_tracker.record("32:153289", "-89", now)

        # Pool child 0: strong signal (-39)
        tracker_0 = RssiTracker(ttl=None)
        tracker_0.record("32:153289", "-39", now)

        # Pool child 1: weak signal (-89)
        tracker_1 = RssiTracker(ttl=None)
        tracker_1.record("32:153289", "-89", now)

        transport = self._make_pool_transport([tracker_0, tracker_1])
        gwy = self._make_gateway_with_tracker(gw_tracker, transport=transport)
        dev = DeviceBase(gwy, Address("32:153289"))
        dev._last_msg_dtm = now

        q = dev.communication_quality
        assert q is not None
        assert q.best_rssi == -39  # strongest, not weakest
        assert q.rssi_quality == "strong"

    def test_pool_all_weak_reports_weak(self) -> None:
        """When all pool trackers are weak, quality is weak."""
        now = dt(2026, 1, 1, 12, 0, 0)

        gw_tracker = RssiTracker(ttl=None)
        gw_tracker.record("32:153289", "-90", now)

        tracker_0 = RssiTracker(ttl=None)
        tracker_0.record("32:153289", "-91", now)

        tracker_1 = RssiTracker(ttl=None)
        tracker_1.record("32:153289", "-89", now)

        transport = self._make_pool_transport([tracker_0, tracker_1])
        gwy = self._make_gateway_with_tracker(gw_tracker, transport=transport)
        dev = DeviceBase(gwy, Address("32:153289"))
        dev._last_msg_dtm = now

        q = dev.communication_quality
        assert q is not None
        assert q.best_rssi == -89  # best of the weak ones
        assert q.rssi_quality == "weak"

    def test_pool_empty_trackers_falls_back_to_gateway(self) -> None:
        """Empty pool_rssi_trackers falls back to the gateway tracker."""
        now = dt(2026, 1, 1, 12, 0, 0)

        gw_tracker = RssiTracker(ttl=None)
        gw_tracker.record("32:153289", "-55", now)

        transport = self._make_pool_transport([])
        gwy = self._make_gateway_with_tracker(gw_tracker, transport=transport)
        dev = DeviceBase(gwy, Address("32:153289"))
        dev._last_msg_dtm = now

        q = dev.communication_quality
        assert q is not None
        assert q.best_rssi == -55  # from gateway tracker, not pool

    def test_no_gateway_tracker_returns_none(self) -> None:
        """Without a gateway RSSI tracker, returns None."""
        gwy = MagicMock(spec=Gateway)
        gwy._rssi_tracker = None
        gwy._engine = MagicMock()
        gwy._engine._transport = None

        dev = DeviceBase(gwy, Address("32:153289"))
        assert dev.communication_quality is None

    def test_faked_device_not_reported_as_weak(self) -> None:
        """A faked device with weak RSSI is still faked — discovery skips it.

        This test verifies that ``is_faked`` returns True when a binding
        manager is attached, so the discovery weak-signal check can skip
        faked devices.  The communication_quality itself still computes
        (it's the caller's responsibility to check ``is_faked`` first).
        """
        now = dt(2026, 1, 1, 12, 0, 0)
        tracker = RssiTracker(ttl=None)
        tracker.record("37:168270", "-90", now)

        gwy = self._make_gateway_with_tracker(tracker, transport=None)
        dev = DeviceBase(gwy, Address("37:168270"))
        dev._last_msg_dtm = now

        # Simulate a faked device by attaching a binding manager
        dev._binding_manager = MagicMock()
        dev._binding_manager.is_binding = False

        # is_faked must be True so discovery can skip it
        assert dev.is_faked is True

        # communication_quality still computes (caller checks is_faked)
        q = dev.communication_quality
        assert q is not None
        assert q.rssi_quality == "weak"
        # But the caller (discovery.py) would skip this device via:
        #   if getattr(device, "is_faked", False): continue
