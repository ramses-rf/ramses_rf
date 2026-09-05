#!/usr/bin/env python3
"""Tests for the pre-serialization routing contract (PR 2, issue 1119).

Tests cover:
- ``SourcePolicy`` semantics (GATEWAY vs PRESERVE)
- ``RouteRequest`` / ``RoutedCommand`` immutability
- ``WriteOutcome`` classification
- ``PooledTransport.prepare_command()`` child selection + source patching
- ``PooledTransport.write_routed()`` dispatch + outcome classification
- ``TransportInterface`` default implementations (non-pooled pass-through)
- QoS echo matching with routed commands
- Safe failover: AMBIGUOUS raises, NOT_SUBMITTED does not
- Source policy: faked-device commands preserve source
- Cold-start fallback: round-robin when no RSSI data
"""

from __future__ import annotations

import asyncio
import dataclasses as dataclasses_mod
from datetime import datetime as dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ramses_tx.address import HGI_DEV_ADDR
from ramses_tx.const import Code, Verb
from ramses_tx.dtos import CommandDTO
from ramses_tx.routing import (
    RoutedCommand,
    RouteRequest,
    SourcePolicy,
    WriteOutcome,
)
from ramses_tx.transport.pooled import (
    ConnectionState,
    NodeAvailability,
    PoolChild,
    PooledTransport,
)

# -- Helpers ---------------------------------------------------------------


def _make_cmd(
    verb: str = Verb.I_,
    addr1: str = HGI_DEV_ADDR.id,
    addr2: str = "01:123456",
    addr3: str = "--:------",
    code: str = Code._10A0,
    payload: str = "00",
) -> CommandDTO:
    """Create a CommandDTO for testing."""
    return CommandDTO(
        verb=verb,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=code,
        payload=payload,
    )


def _make_child(
    child_id: int,
    hgi_id: str = "18:123456",
    connected: bool = True,
    send_ready: bool = True,
) -> PoolChild:
    """Create a PoolChild for testing."""
    child = PoolChild(
        child_id=child_id,
        port_name=f"mqtt://test-{child_id}",
        hgi_id=hgi_id,
        transport=MagicMock(),
    )
    if connected:
        child.connection_state = ConnectionState.CONNECTED
        child.availability = NodeAvailability.ONLINE
    child.send_ready = send_ready
    child.transport.write_frame = AsyncMock()
    return child


def _make_pooled_transport(
    children: list[PoolChild],
) -> PooledTransport:
    """Create a PooledTransport with the given children for testing."""
    protocol = MagicMock()
    transport = PooledTransport.__new__(PooledTransport)
    transport._children = list(children)
    transport._protocol = protocol
    transport._rr_index = 0
    transport._max_consecutive_errors = 3
    transport._health_timeout = 60.0
    transport._dedup_cache = {}
    transport._dedup_window = 0.5
    transport._max_dedup_keys = 512
    transport._accepted_hgis = None
    transport._protocol_connected = True
    transport._conn_fut = None
    transport._is_closing = False
    return transport


# -- SourcePolicy / RouteRequest / RoutedCommand / WriteOutcome -----------


class TestRoutingTypes:
    """Tests for the immutable routing boundary objects."""

    def test_source_policy_enum_values(self) -> None:
        """SourcePolicy has GATEWAY and PRESERVE values."""
        assert len({SourcePolicy.GATEWAY, SourcePolicy.PRESERVE}) == 2
        assert SourcePolicy.GATEWAY.name == "GATEWAY"
        assert SourcePolicy.PRESERVE.name == "PRESERVE"

    def test_route_request_is_frozen(self) -> None:
        """RouteRequest is immutable."""
        req = RouteRequest(command=_make_cmd())
        with pytest.raises(dataclasses_FrozenInstanceError):
            req.command = _make_cmd()

    def test_route_request_default_source_policy(self) -> None:
        """RouteRequest defaults to GATEWAY source policy."""
        req = RouteRequest(command=_make_cmd())
        assert req.source_policy is SourcePolicy.GATEWAY

    def test_routed_command_is_frozen(self) -> None:
        """RoutedCommand is immutable."""
        rc = RoutedCommand(child_id="0", command=_make_cmd())
        with pytest.raises(dataclasses_FrozenInstanceError):
            rc.child_id = "1"

    def test_write_outcome_enum_values(self) -> None:
        """WriteOutcome has SUBMITTED, NOT_SUBMITTED, AMBIGUOUS."""
        assert (
            len(
                {
                    WriteOutcome.SUBMITTED,
                    WriteOutcome.NOT_SUBMITTED,
                    WriteOutcome.AMBIGUOUS,
                }
            )
            == 3
        )


dataclasses_FrozenInstanceError = dataclasses_mod.FrozenInstanceError


# -- PooledTransport.prepare_command() ------------------------------------


class TestPrepareCommand:
    """Tests for PooledTransport.prepare_command()."""

    def test_prepare_selects_sendable_child(self) -> None:
        """prepare_command selects a sendable child and patches source."""
        child0 = _make_child(0, "18:111111")
        child1 = _make_child(1, "18:222222")
        transport = _make_pooled_transport([child0, child1])

        # Use a non-placeholder HGI source so prepare_command patches it
        request = RouteRequest(
            command=_make_cmd(addr1="18:999999"),
        )
        routed = transport.prepare_command(request)

        assert routed.child_id in ("0", "1")
        assert routed.command.addr1 in ("18:111111", "18:222222")

    def test_prepare_patches_source_to_selected_child(self) -> None:
        """prepare_command patches addr1 to the selected child's HGI ID."""
        child0 = _make_child(0, "18:111111")
        child1 = _make_child(1, "18:222222")
        transport = _make_pooled_transport([child0, child1])

        # Give child0 better RSSI so it's selected
        child0.rssi_tracker.record("01:123456", -50, dt.now())
        child1.rssi_tracker.record("01:123456", -80, dt.now())

        # Use a non-placeholder HGI source so prepare_command patches it
        request = RouteRequest(
            command=_make_cmd(addr1="18:999999"),
            source_policy=SourcePolicy.GATEWAY,
        )
        routed = transport.prepare_command(request)

        assert routed.child_id == "0"
        assert routed.command.addr1 == "18:111111"

    def test_prepare_preserve_policy_does_not_patch(self) -> None:
        """PRESERVE source policy leaves addr1 unchanged."""
        child0 = _make_child(0, "18:111111")
        transport = _make_pooled_transport([child0])

        request = RouteRequest(
            command=_make_cmd(addr1="01:099999"),
            source_policy=SourcePolicy.PRESERVE,
        )
        routed = transport.prepare_command(request)

        assert routed.command.addr1 == "01:099999"

    def test_prepare_skips_hgi80_placeholder(self) -> None:
        """HGI80 children keep the 18:000730 placeholder.

        When the command source is the HGI80 placeholder
        ``18:000730``, ``prepare_command()`` does not re-patch it to
        the selected child's HGI ID — the HGI80 firmware substitutes
        its own ID during transmission.
        """
        child0 = _make_child(0, "18:111111")
        transport = _make_pooled_transport([child0])

        request = RouteRequest(
            command=_make_cmd(addr1="18:000730"),
            source_policy=SourcePolicy.GATEWAY,
        )
        routed = transport.prepare_command(request)

        # Placeholder is kept, not patched to child's HGI ID
        assert routed.command.addr1 == "18:000730"

    def test_prepare_raises_when_no_child_sendable(self) -> None:
        """prepare_command raises TransportError when no child is sendable."""
        child0 = _make_child(0, "18:111111", connected=False)
        transport = _make_pooled_transport([child0])

        from ramses_tx.exceptions import TransportError

        request = RouteRequest(command=_make_cmd())
        with pytest.raises(TransportError, match="No connected child"):
            transport.prepare_command(request)

    def test_prepare_uses_target_device_from_addr2(self) -> None:
        """prepare_command extracts target from addr2 for RSSI selection."""
        child0 = _make_child(0, "18:111111")
        child1 = _make_child(1, "18:222222")
        transport = _make_pooled_transport([child0, child1])

        # Give child1 better RSSI for the target device
        child0.rssi_tracker.record("01:123456", -80, dt.now())
        child1.rssi_tracker.record("01:123456", -50, dt.now())

        request = RouteRequest(
            command=_make_cmd(addr2="01:123456"),
        )
        routed = transport.prepare_command(request)

        assert routed.child_id == "1"

    def test_prepare_does_not_patch_non_hgi_source(self) -> None:
        """Non-18: sources are not patched even with GATEWAY policy."""
        child0 = _make_child(0, "18:111111")
        transport = _make_pooled_transport([child0])

        request = RouteRequest(
            command=_make_cmd(addr1="01:099999"),
            source_policy=SourcePolicy.GATEWAY,
        )
        routed = transport.prepare_command(request)

        assert routed.command.addr1 == "01:099999"


# -- PooledTransport.write_routed() ---------------------------------------


class TestWriteRouted:
    """Tests for PooledTransport.write_routed()."""

    async def test_write_routed_submitted(self) -> None:
        """write_routed returns SUBMITTED on successful dispatch."""
        child0 = _make_child(0, "18:111111")
        transport = _make_pooled_transport([child0])

        routed = RoutedCommand(child_id="0", command=_make_cmd())
        outcome = await transport.write_routed(routed, str(_make_cmd()))

        assert outcome is WriteOutcome.SUBMITTED
        assert child0.transport.write_frame.called

    async def test_write_routed_not_submitted_when_child_missing(self) -> None:
        """write_routed returns NOT_SUBMITTED when child_id is invalid."""
        child0 = _make_child(0, "18:111111")
        transport = _make_pooled_transport([child0])

        routed = RoutedCommand(child_id="99", command=_make_cmd())
        outcome = await transport.write_routed(routed, str(_make_cmd()))

        assert outcome is WriteOutcome.NOT_SUBMITTED

    async def test_write_routed_ambiguous_on_exception(self) -> None:
        """write_routed returns AMBIGUOUS when child raises."""
        child0 = _make_child(0, "18:111111")
        child0.transport.write_frame = AsyncMock(
            side_effect=RuntimeError("write failed")
        )
        transport = _make_pooled_transport([child0])

        routed = RoutedCommand(child_id="0", command=_make_cmd())
        outcome = await transport.write_routed(routed, str(_make_cmd()))

        assert outcome is WriteOutcome.AMBIGUOUS

    async def test_write_routed_passes_disable_tx_limits(self) -> None:
        """write_routed passes disable_tx_limits to child's write_frame."""
        child0 = _make_child(0, "18:111111")
        transport = _make_pooled_transport([child0])

        routed = RoutedCommand(child_id="0", command=_make_cmd())
        await transport.write_routed(
            routed, str(_make_cmd()), disable_tx_limits=True
        )

        child0.transport.write_frame.assert_called_once()
        call_kwargs = child0.transport.write_frame.call_args
        assert call_kwargs.kwargs.get("disable_tx_limits") is True

    async def test_write_routed_falls_back_when_no_disable_tx_limits(
        self,
    ) -> None:
        """write_routed falls back when child doesn't accept disable_tx_limits."""
        child0 = _make_child(0, "18:111111")
        # Make write_frame raise TypeError for disable_tx_limits, then
        # accept it without the kwarg.
        call_count = 0

        async def _write_frame_side_effect(
            frame: str, **kwargs: object
        ) -> None:
            nonlocal call_count
            call_count += 1
            if "disable_tx_limits" in kwargs:
                raise TypeError("unexpected kwarg")

        child0.transport.write_frame = AsyncMock(
            side_effect=_write_frame_side_effect
        )
        transport = _make_pooled_transport([child0])

        routed = RoutedCommand(child_id="0", command=_make_cmd())
        outcome = await transport.write_routed(
            routed, str(_make_cmd()), disable_tx_limits=True
        )

        assert outcome is WriteOutcome.SUBMITTED
        assert call_count == 2  # first call with kwarg, second without


# -- TransportInterface default implementations ---------------------------


class TestDefaultTransportInterface:
    """Tests for the default routing implementations on non-pooled transports."""

    def test_default_prepare_command_returns_self(self) -> None:
        """Default prepare_command returns child_id='self' and original command."""
        from ramses_tx.interfaces import TransportInterface

        # Create a minimal concrete transport for testing
        class _DummyTransport(TransportInterface):
            def close(self) -> None:
                pass

            def get_extra_info(self, name: str, default: Any = None) -> Any:
                return default

            async def send_frame(self, frame: str) -> None:
                pass

            async def write_frame(self, frame: str) -> None:
                pass

        transport = _DummyTransport()
        cmd = _make_cmd()
        request = RouteRequest(command=cmd)
        routed = transport.prepare_command(request)

        assert routed.child_id == "self"
        assert routed.command is cmd

    async def test_default_write_routed_delegates_to_write_frame(self) -> None:
        """Default write_routed calls write_frame and returns SUBMITTED."""
        from ramses_tx.interfaces import TransportInterface

        class _DummyTransport(TransportInterface):
            def __init__(self) -> None:
                self.write_called = False

            def close(self) -> None:
                pass

            def get_extra_info(self, name: str, default: Any = None) -> Any:
                return default

            async def send_frame(self, frame: str) -> None:
                pass

            async def write_frame(self, frame: str) -> None:
                self.write_called = True

        transport = _DummyTransport()
        routed = RoutedCommand(child_id="self", command=_make_cmd())
        outcome = await transport.write_routed(routed, "test frame")

        assert outcome is WriteOutcome.SUBMITTED
        assert transport.write_called

    async def test_default_write_routed_ambiguous_on_error(self) -> None:
        """Default write_routed returns AMBIGUOUS when write_frame raises."""
        from ramses_tx.interfaces import TransportInterface

        class _DummyTransport(TransportInterface):
            def close(self) -> None:
                pass

            def get_extra_info(self, name: str, default: Any = None) -> Any:
                return default

            async def send_frame(self, frame: str) -> None:
                pass

            async def write_frame(self, frame: str) -> None:
                raise OSError("write error")

        transport = _DummyTransport()
        routed = RoutedCommand(child_id="self", command=_make_cmd())
        outcome = await transport.write_routed(routed, "test frame")

        assert outcome is WriteOutcome.AMBIGUOUS


# -- Cold-start fallback (round-robin) ------------------------------------


class TestColdStartFallback:
    """Tests for round-robin fallback when no RSSI data is available."""

    def test_cold_start_round_robin(self) -> None:
        """When no RSSI data, children are selected round-robin."""
        child0 = _make_child(0, "18:111111")
        child1 = _make_child(1, "18:222222")
        transport = _make_pooled_transport([child0, child1])

        request = RouteRequest(command=_make_cmd())

        # First call should select one child, second call the other
        routed1 = transport.prepare_command(request)
        routed2 = transport.prepare_command(request)

        child_ids = {routed1.child_id, routed2.child_id}
        assert child_ids == {"0", "1"}

    def test_cold_start_with_rssi_uses_best(self) -> None:
        """When RSSI data exists, best RSSI child is selected."""
        child0 = _make_child(0, "18:111111")
        child1 = _make_child(1, "18:222222")
        transport = _make_pooled_transport([child0, child1])

        child0.rssi_tracker.record("01:123456", -50, dt.now())
        child1.rssi_tracker.record("01:123456", -80, dt.now())

        request = RouteRequest(command=_make_cmd(addr2="01:123456"))
        routed = transport.prepare_command(request)

        assert routed.child_id == "0"  # better RSSI


# -- QoS echo matching with routed commands -------------------------------


class TestRoutedQoSEcho:
    """Tests that QoS echo matching works with routed commands."""

    async def test_pending_cmd_matches_routed_command(self) -> None:
        """_pending_cmd is set from the routed command, not the original."""
        from ramses_tx.protocol.core import PortProtocol
        from ramses_tx.routing import RoutedCommand

        protocol = PortProtocol(
            MagicMock(),
            echo_timeout=0.05,
            max_retry_limit=2,
            send_timeout_limit=1.0,
        )

        # Create a mock transport that patches the source
        mock_transport = MagicMock()
        mock_transport.get_extra_info.return_value = None

        def _prepare(request: RouteRequest) -> RoutedCommand:
            # Patch source to a different HGI
            patched = dataclasses_mod.replace(
                request.command, addr1="18:999999"
            )
            return RoutedCommand(child_id="0", command=patched)

        mock_transport.prepare_command = MagicMock(side_effect=_prepare)
        mock_transport.write_routed = AsyncMock(
            return_value=WriteOutcome.SUBMITTED
        )

        protocol.connection_made(mock_transport, ramses=True)

        cmd = _make_cmd(addr1="18:000730")
        send_task = asyncio.create_task(protocol.send_cmd(cmd))
        await asyncio.sleep(0.01)

        # _pending_cmd should have the patched source
        assert protocol._pending_cmd is not None
        assert protocol._pending_cmd.addr1 == "18:999999"

        # Send echo with the patched source
        from ramses_tx.packet import Packet

        echo_pkt = MagicMock(spec=Packet)
        echo_pkt._is_echo = True
        echo_pkt._hdr = protocol._pending_cmd.tx_header
        echo_pkt._hdr_ = protocol._pending_cmd.tx_header
        protocol._packet_received(echo_pkt)

        result = await send_task
        assert result == echo_pkt

        protocol.connection_lost(None)
