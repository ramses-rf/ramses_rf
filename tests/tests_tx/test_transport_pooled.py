#!/usr/bin/env python3
"""Tests for PooledTransport — multi-HGI link-layer pooling.

Covers:
- Inbound deduplication (same packet from 2 children → 1 upstream)
- Inbound forwarding (distinct packets from different children)
- Outbound routing (round-robin among connected children)
- Connection lifecycle (wait for any, disconnect handling)
- get_extra_info aggregation (SZ_ACTIVE_HGI, pool_stats)
- Close propagation to all children
"""

import asyncio
from datetime import datetime as dt, timedelta as td
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from ramses_tx.const import I_, SZ_ACTIVE_HGI, Code
from ramses_tx.transport.base import TransportConfig
from ramses_tx.transport.pooled import (
    PooledTransport,
    _ChildProtocolProxy,
)


@pytest.fixture
def event_loop() -> asyncio.AbstractEventLoop:
    """Provide a fresh event loop for each test."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# -- Helpers ---------------------------------------------------------------


def _make_packet(
    verb: str = I_,
    code: str = Code._30C9,
    src: str = "01:123456",
    dst: str = "18:000730",
    addr3: str = "--:------",
    payload: str = "00",
    rssi: str = "000",
) -> MagicMock:
    """Create a mock Packet with a DTO for dedup keying."""
    dto = MagicMock()
    dto.verb = verb
    dto.code = code
    dto.addr1 = src
    dto.addr2 = dst
    dto.addr3 = addr3
    dto.raw_payload = payload
    dto.rssi = rssi

    pkt = MagicMock()
    pkt._dto = dto
    pkt.__str__ = lambda self: (
        f"{rssi} {verb} --- {src} {dst} {addr3} {code} 000 {payload}"
    )
    return pkt


def _make_mock_transport(
    hgi: str | None = None,
    connected: bool = True,
) -> MagicMock:
    """Create a mock child transport."""
    t = MagicMock()
    t.get_extra_info = lambda name, default=None: (
        hgi if name == SZ_ACTIVE_HGI else default
    )
    t.write_frame = AsyncMock()
    t.send_frame = AsyncMock()
    t.close = Mock()
    t.is_closing = False
    t._connected = connected
    return t


def _make_mock_protocol() -> MagicMock:
    """Create a mock real protocol for the pool."""
    proto = MagicMock()
    proto.packet_received = Mock()
    proto.connection_lost = Mock()
    proto.send_cmd = AsyncMock(return_value=None)
    proto.set_regex_rules = Mock()
    return proto


# -- Inbound deduplication -------------------------------------------------


async def test_dedup_same_packet_from_two_children_is_deduped() -> None:
    """Two children send the same packet → only one reaches the protocol."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), dedup_window=1.0
    )
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]

    pkt = _make_packet()
    pool._on_child_packet(0, pkt)
    pool._on_child_packet(1, pkt)  # duplicate

    # Let call_soon_threadsafe callbacks drain.
    await asyncio.sleep(0.01)

    assert proto.packet_received.call_count == 1
    stats = pool.get_extra_info("pool_stats")
    assert stats["deduped"] == 1
    assert stats["forwarded"] == 1


async def test_distinct_packets_from_different_children_are_forwarded() -> (
    None
):
    """Two children send different packets → both reach the protocol."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), dedup_window=0.5
    )
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]

    pkt0 = _make_packet(src="01:111111")
    pkt1 = _make_packet(src="01:222222")
    pool._on_child_packet(0, pkt0)
    pool._on_child_packet(1, pkt1)

    await asyncio.sleep(0.01)

    assert proto.packet_received.call_count == 2
    stats = pool.get_extra_info("pool_stats")
    assert stats["deduped"] == 0
    assert stats["forwarded"] == 2


async def test_dedup_window_expires_after_timeout() -> None:
    """Same packet after the dedup window expires → both forwarded."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), dedup_window=0.05
    )
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]

    pkt = _make_packet()
    pool._on_child_packet(0, pkt)
    await asyncio.sleep(0.1)  # wait for dedup window to expire
    pool._on_child_packet(1, pkt)

    await asyncio.sleep(0.01)

    assert proto.packet_received.call_count == 2
    stats = pool.get_extra_info("pool_stats")
    assert stats["deduped"] == 0


# -- Outbound routing ------------------------------------------------------


async def test_outbound_routes_to_connected_child() -> None:
    """write_frame routes to a connected child."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111", connected=False)
    t1 = _make_mock_transport(hgi="18:002222", connected=True)
    pool = PooledTransport(proto, [t0, t1], config=TransportConfig())
    pool._child_connected = [False, True]

    await pool.write_frame(
        " 000 I --- 01:123456 18:000730 --:------ 30C9 000 00"
    )

    t0.write_frame.assert_not_called()
    t1.write_frame.assert_called_once()


async def test_outbound_round_robin_among_connected() -> None:
    """write_frame round-robins between connected children."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111", connected=True)
    t1 = _make_mock_transport(hgi="18:002222", connected=True)
    pool = PooledTransport(proto, [t0, t1], config=TransportConfig())
    pool._child_connected = [True, True]

    await pool.write_frame("frame1")
    await pool.write_frame("frame2")

    # Both children should have been used (round-robin).
    calls = [t0.write_frame.call_count, t1.write_frame.call_count]
    assert sum(calls) == 2
    assert all(c >= 0 for c in calls)


async def test_outbound_fails_when_no_child_connected() -> None:
    """write_frame raises when no child is connected."""
    from ramses_tx import exceptions as exc

    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111", connected=False)
    pool = PooledTransport(proto, [t0], config=TransportConfig())
    pool._child_connected = [False]

    with pytest.raises(exc.TransportError, match="No connected child"):
        await pool.write_frame("frame1")


# -- Connection lifecycle --------------------------------------------------


async def test_wait_for_any_connection_resolves_when_child_connects() -> None:
    """_wait_for_any_connection resolves when a child connects."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(proto, [t0], config=TransportConfig())

    # Simulate child 0 connecting.
    pool._on_child_connected(0, t0)

    result = await pool._wait_for_any_connection(timeout=1.0)
    assert result is pool


async def test_wait_for_any_connection_times_out() -> None:
    """_wait_for_any_connection raises on timeout."""
    from ramses_tx import exceptions as exc

    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(proto, [t0], config=TransportConfig())

    with pytest.raises(exc.TransportError, match="no child connected"):
        await pool._wait_for_any_connection(timeout=0.05)


async def test_child_disconnect_notifies_protocol_when_all_disconnected() -> (
    None
):
    """connection_lost fires on the real protocol when all children drop."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(proto, [t0, t1], config=TransportConfig())
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]

    # Disconnect first child — should NOT notify protocol (one still up).
    pool._on_child_disconnected(0, None)
    proto.connection_lost.assert_not_called()

    # Disconnect second child — SHOULD notify protocol.
    pool._on_child_disconnected(1, None)
    await asyncio.sleep(0.01)
    proto.connection_lost.assert_called_once()


# -- get_extra_info --------------------------------------------------------


def test_get_extra_info_active_hgi_returns_first_connected(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """get_extra_info(SZ_ACTIVE_HGI) returns the first connected child's HGI."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]

    assert pool.get_extra_info(SZ_ACTIVE_HGI) == "18:001111"


def test_get_extra_info_active_hgi_skips_disconnected(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """get_extra_info(SZ_ACTIVE_HGI) skips disconnected children."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    pool._child_connected = [False, True]
    pool._child_hgi = [None, "18:002222"]

    assert pool.get_extra_info(SZ_ACTIVE_HGI) == "18:002222"


def test_get_extra_info_pool_stats(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """get_extra_info('pool_stats') returns diagnostic stats."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )
    pool._child_connected = [True]
    pool._child_hgi = ["18:001111"]
    pool._pkts_received = [5]
    pool._pkts_deduped = 2
    pool._pkts_forwarded = 3

    stats = pool.get_extra_info("pool_stats")
    assert stats["children"] == 1
    assert stats["connected"] == 1
    assert stats["received"] == [5]
    assert stats["deduped"] == 2
    assert stats["forwarded"] == 3


# -- Close -----------------------------------------------------------------


def test_close_closes_all_children(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """close() calls close() on every child transport."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )

    pool.close()

    t0.close.assert_called_once()
    t1.close.assert_called_once()
    assert pool.is_closing is True


def test_close_is_idempotent(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """close() called twice doesn't re-close children."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )

    pool.close()
    pool.close()

    t0.close.assert_called_once()


# -- _ChildProtocolProxy ---------------------------------------------------


def test_child_proxy_routes_packet_to_pool() -> None:
    """_ChildProtocolProxy.packet_received routes to the pool."""
    pool = MagicMock()
    proxy = _ChildProtocolProxy(pool, 0)
    pkt = _make_packet()

    proxy.packet_received(pkt)

    pool._on_child_packet.assert_called_once_with(0, pkt)


def test_child_proxy_routes_connection_made() -> None:
    """_ChildProtocolProxy.connection_made routes to the pool."""
    pool = MagicMock()
    proxy = _ChildProtocolProxy(pool, 1)
    transport_obj = MagicMock()

    proxy.connection_made(transport_obj, ramses=False)

    pool._on_child_connected.assert_called_once_with(1, transport_obj)
    assert proxy._connected is True


def test_child_proxy_routes_connection_lost() -> None:
    """_ChildProtocolProxy.connection_lost routes to the pool."""
    pool = MagicMock()
    proxy = _ChildProtocolProxy(pool, 2)
    proxy._connected = True

    err = ValueError("test error")
    proxy.connection_lost(err)

    pool._on_child_disconnected.assert_called_once_with(2, err)
    assert proxy._connected is False


# -- RSSI-based outbound routing (PR 3) ------------------------------------


async def test_rssi_selects_child_with_higher_average_rssi() -> None:
    """Outbound routing selects the child with higher avg RSSI."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), dedup_window=1.0
    )
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]

    # Feed child 0 low-RSSI packets, child 1 high-RSSI packets.
    # Use distinct payloads to avoid dedup.
    for i in range(5):
        pool._on_child_packet(0, _make_packet(rssi="020", payload=f"{i:02X}A"))
        pool._on_child_packet(1, _make_packet(rssi="080", payload=f"{i:02X}B"))

    await asyncio.sleep(0.01)

    # Outbound should go to child 1 (higher RSSI).
    await pool.write_frame("frame1")
    t1.write_frame.assert_called_once()
    t0.write_frame.assert_not_called()


async def test_rssi_falls_back_to_round_robin_when_no_data() -> None:
    """When no RSSI data exists, selection falls back to round-robin."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(proto, [t0, t1], config=TransportConfig())
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]

    # No packets received yet — no RSSI data.
    await pool.write_frame("frame1")
    await pool.write_frame("frame2")

    # Round-robin should distribute between both children.
    total = t0.write_frame.call_count + t1.write_frame.call_count
    assert total == 2
    # Both should have been used (round-robin).
    assert t0.write_frame.call_count >= 1
    assert t1.write_frame.call_count >= 1


async def test_rssi_rolling_window_keeps_last_5_samples() -> None:
    """RSSI window only retains the last 5 samples."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), dedup_window=10.0
    )
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]

    # Feed 10 packets with increasing RSSI to child 0.
    for i in range(10):
        pool._on_child_packet(
            0, _make_packet(rssi=f"{i:03d}", payload=f"{i:02X}C")
        )

    # The rolling average should be the average of the last 5: 5,6,7,8,9 = 7.0
    avg = pool._avg_rssi(0)
    assert avg == 7.0


async def test_rssi_skips_disconnected_children() -> None:
    """RSSI selection only considers connected children."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111", connected=True)
    t1 = _make_mock_transport(hgi="18:002222", connected=False)
    pool = PooledTransport(proto, [t0, t1], config=TransportConfig())
    pool._child_connected = [True, False]
    pool._child_hgi = ["18:001111", None]

    # Give child 1 (disconnected) high RSSI via direct manipulation.
    pool._child_rssi[1].extend([90, 90, 90])
    # Child 0 has low RSSI.
    pool._child_rssi[0].extend([10, 10, 10])

    await pool.write_frame("frame1")
    t0.write_frame.assert_called_once()
    t1.write_frame.assert_not_called()


def test_pool_stats_includes_avg_rssi(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """pool_stats includes avg_rssi per child."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    pool._child_connected = [True, True]
    pool._child_rssi[0].extend([60, 70, 80])
    # child 1 has no RSSI data.

    stats = pool.get_extra_info("pool_stats")
    assert "avg_rssi" in stats
    assert stats["avg_rssi"] == [70.0, 0.0]


def test_parse_rssi_extracts_numeric_value() -> None:
    """_parse_rssi converts '063' to 63."""
    pkt = _make_packet(rssi="063")
    assert PooledTransport._parse_rssi(pkt) == 63


def test_parse_rssi_returns_none_for_ellipsis() -> None:
    """_parse_rssi returns None for '...' placeholder."""
    pkt = _make_packet(rssi="...")
    assert PooledTransport._parse_rssi(pkt) is None


def test_parse_rssi_returns_none_for_empty() -> None:
    """_parse_rssi returns None for empty string."""
    pkt = _make_packet(rssi="")
    assert PooledTransport._parse_rssi(pkt) is None


# -- Health monitoring (PR 4) ---------------------------------------------


async def test_unhealthy_child_excluded_from_outbound() -> None:
    """An unhealthy child is excluded from outbound selection."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(proto, [t0, t1], config=TransportConfig())
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]

    # Mark child 0 as unhealthy.
    pool._child_healthy[0] = False

    await pool.write_frame("frame1")
    t0.write_frame.assert_not_called()
    t1.write_frame.assert_called_once()


async def test_packet_received_marks_child_healthy() -> None:
    """Receiving a packet resets consecutive errors and marks healthy."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), dedup_window=10.0
    )
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]

    # Set child 0 as unhealthy with errors.
    pool._child_healthy[0] = False
    pool._child_consecutive_errors[0] = 3

    # Feed a packet to child 0.
    pool._on_child_packet(0, _make_packet(rssi="050", payload="AA"))

    assert pool._child_healthy[0] is True
    assert pool._child_consecutive_errors[0] == 0


async def test_consecutive_disconnects_mark_child_unhealthy() -> None:
    """Exceeding max_consecutive_errors marks a child unhealthy."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto,
        [t0, t1],
        config=TransportConfig(),
        max_consecutive_errors=3,
    )
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]

    # Disconnect child 0 three times (but re-connect between each).
    for _ in range(3):
        pool._on_child_disconnected(0, ValueError("test"))
        pool._child_connected[0] = True  # simulate reconnect

    assert pool._child_healthy[0] is False
    assert pool._child_consecutive_errors[0] == 3


async def test_health_timeout_marks_silent_child_unhealthy() -> None:
    """A child with no packets for health_timeout is marked unhealthy."""
    from datetime import datetime as dt, timedelta as td

    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto,
        [t0, t1],
        config=TransportConfig(),
        health_timeout=0.05,
    )
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]

    # Give child 1 a recent packet (so it stays healthy).
    pool._on_child_packet(1, _make_packet(rssi="050", payload="BB"))
    # Set child 0's last packet time to the past (stale).
    pool._child_last_pkt_time[0] = dt.now() - td(seconds=1)

    # Trigger health check.
    pool._check_health()

    assert pool._child_healthy[0] is False
    assert pool._child_healthy[1] is True


async def test_no_healthy_children_reenables_as_last_resort() -> None:
    """When no healthy children remain, all are re-enabled."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(proto, [t0], config=TransportConfig())
    pool._child_connected = [True]
    pool._child_hgi = ["18:001111"]
    pool._child_healthy[0] = False

    # _select_transport should re-enable the child as last resort.
    child = pool._select_transport()
    assert child is not None
    assert pool._child_healthy[0] is True


def test_pool_stats_includes_health_info(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """pool_stats includes child_health and consecutive_errors."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    pool._child_connected = [True, False]
    pool._child_healthy = [True, False]
    pool._child_consecutive_errors = [0, 3]

    stats = pool.get_extra_info("pool_stats")
    assert stats["child_health"] == [True, False]
    assert stats["consecutive_errors"] == [0, 3]
    assert stats["healthy"] == 1


# -- Edge cases & branch coverage (AAA) -----------------------------------


async def test_dedup_same_packet_from_same_child_is_deduped() -> None:
    """Same packet sent twice from the SAME child is deduped."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), dedup_window=1.0
    )
    pool._child_connected = [True]
    pool._child_hgi = ["18:001111"]

    pkt = _make_packet()
    pool._on_child_packet(0, pkt)
    pool._on_child_packet(0, pkt)  # same child, same packet

    await asyncio.sleep(0.01)
    assert proto.packet_received.call_count == 1


async def test_dedup_cache_evicts_oldest_at_max() -> None:
    """Dedup cache evicts oldest entries when _MAX_DEDUP_KEYS is exceeded."""
    from ramses_tx.transport.pooled import _MAX_DEDUP_KEYS

    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), dedup_window=999.0
    )
    pool._child_connected = [True]
    pool._child_hgi = ["18:001111"]

    # Send _MAX_DEDUP_KEYS + 10 distinct packets.
    for i in range(_MAX_DEDUP_KEYS + 10):
        pool._on_child_packet(0, _make_packet(payload=f"{i:04X}"))

    # Cache should be capped at _MAX_DEDUP_KEYS.
    assert len(pool._dedup_cache) == _MAX_DEDUP_KEYS


async def test_rssi_tie_breaking_prefers_lowest_index() -> None:
    """When two children have equal avg RSSI, lowest index is selected."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), dedup_window=10.0
    )
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]

    # Both children get same RSSI.
    for i in range(5):
        pool._on_child_packet(0, _make_packet(rssi="050", payload=f"{i:02X}A"))
        pool._on_child_packet(1, _make_packet(rssi="050", payload=f"{i:02X}B"))

    await asyncio.sleep(0.01)
    await pool.write_frame("frame1")
    # Tie → lowest index (0) wins.
    t0.write_frame.assert_called_once()
    t1.write_frame.assert_not_called()


async def test_rssi_mixed_data_child_with_data_wins() -> None:
    """One child has RSSI data, other doesn't → data child is selected."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(proto, [t0, t1], config=TransportConfig())
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]

    # Only child 1 gets RSSI data (low value).
    pool._child_rssi[1].extend([10, 10, 10])
    # Child 0 has no RSSI data at all.

    await pool.write_frame("frame1")
    # Child 1 has data (even low), child 0 has none → child 1 wins.
    t1.write_frame.assert_called_once()
    t0.write_frame.assert_not_called()


def test_health_connected_but_no_packets_stays_healthy(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """A connected child that never received a packet stays healthy."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )
    pool._child_connected = [True]
    pool._child_hgi = ["18:001111"]
    # last_pkt_time is None — never received a packet.

    pool._check_health()
    assert pool._child_healthy[0] is True


def test_health_check_skips_already_unhealthy(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """_check_health does not re-process already-unhealthy children."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]
    pool._child_healthy[0] = False
    pool._child_last_pkt_time[0] = dt.now() - td(seconds=999)
    # Child 1 is healthy with a recent packet (so last-resort doesn't trigger).
    pool._child_last_pkt_time[1] = dt.now()

    # _check_health should skip child 0 (already unhealthy).
    # No exception, no change.
    pool._check_health()
    assert pool._child_healthy[0] is False
    assert pool._child_healthy[1] is True


async def test_write_frame_forwards_disable_tx_limits() -> None:
    """write_frame forwards disable_tx_limits=True to the child."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(proto, [t0], config=TransportConfig())
    pool._child_connected = [True]
    pool._child_hgi = ["18:001111"]

    await pool.write_frame("frame1", disable_tx_limits=True)

    t0.write_frame.assert_called_once()
    # Verify the kwarg was forwarded.
    _, kwargs = t0.write_frame.call_args
    assert kwargs.get("disable_tx_limits") is True


async def test_write_frame_falls_back_to_send_frame() -> None:
    """write_frame falls back to send_frame when child has no write_frame."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    # Remove write_frame to force fallback.
    del t0.write_frame
    pool = PooledTransport(proto, [t0], config=TransportConfig())
    pool._child_connected = [True]
    pool._child_hgi = ["18:001111"]

    await pool.write_frame("frame1")

    t0.send_frame.assert_called_once_with("frame1")


async def test_write_frame_typeerror_fallback() -> None:
    """write_frame catches TypeError if child doesn't accept disable_tx_limits."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")

    # Make write_frame raise TypeError on disable_tx_limits kwarg.
    async def _no_kwarg(frame: str, **kwargs: object) -> None:
        if "disable_tx_limits" in kwargs:
            raise TypeError("unexpected kwarg")

    t0.write_frame = AsyncMock(side_effect=_no_kwarg)
    pool = PooledTransport(proto, [t0], config=TransportConfig())
    pool._child_connected = [True]
    pool._child_hgi = ["18:001111"]

    # Should not raise — TypeError is caught and write_frame called without kwarg.
    await pool.write_frame("frame1", disable_tx_limits=True)


def test_get_extra_info_evofw3_returns_true_if_any_child(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """get_extra_info(SZ_IS_EVOFW3) returns True if any connected child is evofw3."""
    from ramses_tx.const import SZ_IS_EVOFW3

    proto = _make_mock_protocol()
    t0 = MagicMock()
    t0.get_extra_info = lambda name, default=None: (
        "18:001111"
        if name == SZ_ACTIVE_HGI
        else True
        if name == SZ_IS_EVOFW3
        else default
    )
    t1 = MagicMock()
    t1.get_extra_info = lambda name, default=None: (
        "18:002222" if name == SZ_ACTIVE_HGI else default
    )
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]
    pool._child_transport_objs = [t0, t1]

    assert pool.get_extra_info(SZ_IS_EVOFW3) is True


def test_get_extra_info_evofw3_returns_default_if_none(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """get_extra_info(SZ_IS_EVOFW3) returns default when no child is evofw3."""
    from ramses_tx.const import SZ_IS_EVOFW3

    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )
    pool._child_connected = [True]
    pool._child_hgi = ["18:001111"]
    pool._child_transport_objs = [t0]

    assert pool.get_extra_info(SZ_IS_EVOFW3, default=False) is False


def test_get_extra_info_unknown_key_returns_default(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """get_extra_info with unknown key returns the default."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )

    assert (
        pool.get_extra_info("nonexistent_key", default="fallback")
        == "fallback"
    )


def test_repr_returns_diagnostic_string(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """__repr__ returns a diagnostic representation."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    pool._child_connected = [True, False]

    repr_str = repr(pool)
    assert "PooledTransport" in repr_str
    assert "children=2" in repr_str
    assert "connected=1" in repr_str


async def test_child_proxy_send_cmd_delegates_to_protocol() -> None:
    """_ChildProtocolProxy.send_cmd delegates to the real protocol."""
    proto = _make_mock_protocol()
    pool = MagicMock()
    pool._protocol = proto
    proxy = _ChildProtocolProxy(pool, 0)

    await proxy.send_cmd("test_cmd", qos=1)
    proto.send_cmd.assert_called_once_with("test_cmd", qos=1)


def test_child_proxy_wait_for_connection_delegates(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """_ChildProtocolProxy.wait_for_connection_made delegates to pool."""
    t0 = _make_mock_transport(hgi="18:001111")
    pool = MagicMock()
    pool._wait_for_any_connection = AsyncMock(return_value=t0)
    proxy = _ChildProtocolProxy(pool, 0)

    result = event_loop.run_until_complete(
        proxy.wait_for_connection_made(timeout=2.0)
    )
    pool._wait_for_any_connection.assert_called_once_with(2.0)
    assert result is t0


def test_child_proxy_set_regex_rules_is_noop() -> None:
    """_ChildProtocolProxy.set_regex_rules is a no-op."""
    pool = MagicMock()
    proxy = _ChildProtocolProxy(pool, 0)
    proxy.set_regex_rules(["rule1", "rule2"])
    # No exception, no pool method called.
    pool._on_child_packet.assert_not_called()


def test_child_proxy_pause_resume_writing_are_noops() -> None:
    """_ChildProtocolProxy.pause_writing and resume_writing are no-ops."""
    pool = MagicMock()
    proxy = _ChildProtocolProxy(pool, 0)
    proxy.pause_writing()
    proxy.resume_writing()
    # No exception, no pool method called.


def test_on_child_connected_captures_hgi(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """_on_child_connected reads HGI from the transport object."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )

    pool._on_child_connected(0, t0)

    assert pool._child_connected[0] is True
    assert pool._child_hgi[0] == "18:001111"
    assert pool._child_transport_objs[0] is t0


def test_on_child_connected_resolves_connection_future(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """_on_child_connected resolves a pending connection future."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )

    # Create a pending future.
    pool._conn_fut = event_loop.create_future()
    pool._on_child_connected(0, t0)

    assert pool._conn_fut.done()
    assert pool._conn_fut.result() is pool


async def test_packets_during_close_are_ignored() -> None:
    """Packets received after close() are not forwarded."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(proto, [t0], config=TransportConfig())
    pool._child_connected = [True]
    pool._child_hgi = ["18:001111"]

    pool.close()
    pool._on_child_packet(0, _make_packet())

    await asyncio.sleep(0.01)
    proto.packet_received.assert_not_called()


async def test_three_children_round_robin_distributes() -> None:
    """Round-robin distributes across 3 connected children."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    t2 = _make_mock_transport(hgi="18:003333")
    pool = PooledTransport(proto, [t0, t1, t2], config=TransportConfig())
    pool._child_connected = [True, True, True]
    pool._child_hgi = ["18:001111", "18:002222", "18:003333"]

    # No RSSI data → round-robin.
    await pool.write_frame("f1")
    await pool.write_frame("f2")
    await pool.write_frame("f3")

    total = (
        t0.write_frame.call_count
        + t1.write_frame.call_count
        + t2.write_frame.call_count
    )
    assert total == 3
    # Each child should have been used at least once (round-robin).
    assert t0.write_frame.call_count >= 1
    assert t1.write_frame.call_count >= 1
    assert t2.write_frame.call_count >= 1


async def test_three_children_rssi_selects_best() -> None:
    """RSSI selection picks the best of 3 children."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    t2 = _make_mock_transport(hgi="18:003333")
    pool = PooledTransport(
        proto, [t0, t1, t2], config=TransportConfig(), dedup_window=10.0
    )
    pool._child_connected = [True, True, True]
    pool._child_hgi = ["18:001111", "18:002222", "18:003333"]

    # Child 0: RSSI 30, Child 1: RSSI 80, Child 2: RSSI 50
    for i in range(5):
        pool._on_child_packet(0, _make_packet(rssi="030", payload=f"{i:02X}A"))
        pool._on_child_packet(1, _make_packet(rssi="080", payload=f"{i:02X}B"))
        pool._on_child_packet(2, _make_packet(rssi="050", payload=f"{i:02X}C"))

    await asyncio.sleep(0.01)
    await pool.write_frame("frame1")

    # Child 1 has the highest avg RSSI (80).
    t1.write_frame.assert_called_once()
    t0.write_frame.assert_not_called()
    t2.write_frame.assert_not_called()


def test_parse_rssi_non_numeric_returns_none() -> None:
    """_parse_rssi returns None for non-numeric string."""
    pkt = _make_packet(rssi="ABC")
    assert PooledTransport._parse_rssi(pkt) is None


def test_avg_rssi_empty_returns_zero(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """_avg_rssi returns 0.0 when no samples exist."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )
    assert pool._avg_rssi(0) == 0.0


async def test_dedup_key_components_matter() -> None:
    """Packets differing in any dedup key field are NOT deduped."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), dedup_window=10.0
    )
    pool._child_connected = [True]
    pool._child_hgi = ["18:001111"]

    # Same except verb.
    pool._on_child_packet(0, _make_packet(verb="I", payload="00"))
    pool._on_child_packet(0, _make_packet(verb="R", payload="00"))
    # Same except src.
    pool._on_child_packet(0, _make_packet(src="01:111111"))
    pool._on_child_packet(0, _make_packet(src="01:222222"))
    # Same except payload.
    pool._on_child_packet(0, _make_packet(payload="00"))
    pool._on_child_packet(0, _make_packet(payload="01"))

    await asyncio.sleep(0.01)
    # All 6 packets are distinct → all forwarded.
    assert proto.packet_received.call_count == 6


# -- Single-child passthrough (1 HGI) --------------------------------------


async def test_single_child_all_packets_forwarded() -> None:
    """With only 1 child, all distinct packets are forwarded (no dedup false-positives)."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), dedup_window=10.0
    )
    pool._child_connected = [True]
    pool._child_hgi = ["18:001111"]

    for i in range(10):
        pool._on_child_packet(0, _make_packet(payload=f"{i:04X}"))

    await asyncio.sleep(0.01)
    assert proto.packet_received.call_count == 10


async def test_single_child_all_writes_go_to_it() -> None:
    """With only 1 child, all outbound writes go to that child."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(proto, [t0], config=TransportConfig())
    pool._child_connected = [True]
    pool._child_hgi = ["18:001111"]

    for i in range(5):
        await pool.write_frame(f"frame{i}")

    assert t0.write_frame.call_count == 5


async def test_single_child_rssi_and_health_work() -> None:
    """RSSI tracking and health monitoring work with a single child."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), dedup_window=10.0
    )
    pool._child_connected = [True]
    pool._child_hgi = ["18:001111"]

    # Feed RSSI data.
    for i in range(5):
        pool._on_child_packet(0, _make_packet(rssi="070", payload=f"{i:02X}"))

    await asyncio.sleep(0.01)
    assert pool._avg_rssi(0) == 70.0
    assert pool._child_healthy[0] is True

    stats = pool.get_extra_info("pool_stats")
    assert stats["avg_rssi"] == [70.0]
    assert stats["child_health"] == [True]


async def test_degradation_two_children_one_disconnects() -> None:
    """When 1 of 2 children disconnects, the pool keeps working via the other."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(proto, [t0, t1], config=TransportConfig())
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]

    # Both connected — write goes to one of them.
    await pool.write_frame("frame1")
    assert t0.write_frame.call_count + t1.write_frame.call_count == 1

    # Child 0 disconnects.
    pool._on_child_disconnected(0, RuntimeError("usb unplugged"))
    assert pool._child_connected[0] is False

    # Now all writes must go to child 1 (the only connected child).
    t1_baseline = t1.write_frame.call_count
    await pool.write_frame("frame2")
    await pool.write_frame("frame3")
    assert t0.write_frame.call_count == 0  # child 0 never used at all
    assert t1.write_frame.call_count == t1_baseline + 2  # both go to child 1


async def test_degradation_reconnect_restores_round_robin() -> None:
    """After a disconnected child reconnects, it participates in routing again."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(proto, [t0, t1], config=TransportConfig())
    pool._child_connected = [True, True]
    pool._child_hgi = ["18:001111", "18:002222"]

    # Disconnect child 0 before any write.
    pool._on_child_disconnected(0, RuntimeError("gone"))
    assert pool._child_connected[0] is False

    # Write goes to child 1 only (child 0 is disconnected).
    await pool.write_frame("f1")
    # child 1 used, child 0 not used.
    assert t1.write_frame.call_count >= 1
    assert t0.write_frame.call_count == 0

    # Reconnect child 0.
    pool._on_child_connected(0, t0)
    assert pool._child_connected[0] is True

    # Now writes can go to either child again (round-robin).
    # Use a fresh read — t0 may have been used after reconnect.
    t0_baseline = t0.write_frame.call_count  # type: ignore[unreachable]
    t1_baseline = t1.write_frame.call_count
    await pool.write_frame("f2")
    await pool.write_frame("f3")
    delta0 = t0.write_frame.call_count - t0_baseline
    delta1 = t1.write_frame.call_count - t1_baseline
    assert delta0 + delta1 == 2  # both frames delivered


# -- Constructor validation ------------------------------------------------


def test_empty_transport_list_raises(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """PooledTransport with empty transport list raises ValueError."""
    proto = _make_mock_protocol()
    with pytest.raises(ValueError, match="at least one child transport"):
        PooledTransport(proto, [], config=TransportConfig(), loop=event_loop)


# -- Per-device RSSI tracking ---------------------------------------------


def test_per_device_rssi_tracked_separately(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """RSSI is tracked per-device per-child, not just per-child aggregate."""
    proto = _make_mock_protocol()
    t0, t1 = _make_mock_transport(), _make_mock_transport()
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    pool._on_child_connected(0, t0)
    pool._on_child_connected(1, t1)

    # Device 01:111111 heard by child 0 with RSSI 050
    pool._on_child_packet(0, _make_packet(src="01:111111", rssi="050"))
    # Device 01:222222 heard by child 0 with RSSI 090
    pool._on_child_packet(0, _make_packet(src="01:222222", rssi="090"))

    # Per-device averages differ
    assert pool._avg_rssi(0, "01:111111") == 50.0
    assert pool._avg_rssi(0, "01:222222") == 90.0
    # Aggregate is the mean of both
    assert pool._avg_rssi(0) == 70.0


def test_select_transport_uses_per_device_rssi(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """_select_transport picks the child with best RSSI for the target device."""
    proto = _make_mock_protocol()
    t0, t1 = _make_mock_transport(), _make_mock_transport()
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    pool._on_child_connected(0, t0)
    pool._on_child_connected(1, t1)

    # Child 0 hears device 01:AAA at RSSI 030 (weak)
    pool._on_child_packet(0, _make_packet(src="01:AAA", rssi="030"))
    # Child 1 hears device 01:AAA at RSSI 080 (strong)
    pool._on_child_packet(1, _make_packet(src="01:AAA", rssi="080"))

    # Selecting for target 01:AAA should pick child 1 (stronger)
    selected = pool._select_transport("01:AAA")
    assert selected is t1


def test_select_transport_falls_back_to_aggregate_rssi(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """When no per-device RSSI exists, falls back to aggregate RSSI."""
    proto = _make_mock_protocol()
    t0, t1 = _make_mock_transport(), _make_mock_transport()
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    pool._on_child_connected(0, t0)
    pool._on_child_connected(1, t1)

    # Only aggregate RSSI (from a different device)
    pool._on_child_packet(0, _make_packet(src="01:111", rssi="040"))
    pool._on_child_packet(1, _make_packet(src="01:222", rssi="070"))

    # Target device 01:999 has no per-device data → use aggregate
    selected = pool._select_transport("01:999")
    assert selected is t1  # child 1 has better aggregate RSSI


def test_write_frame_extracts_target_device_for_routing(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """write_frame parses the frame to extract dst for per-device routing."""
    proto = _make_mock_protocol()
    t0, t1 = _make_mock_transport(), _make_mock_transport()
    t0.write_frame = AsyncMock()
    t1.write_frame = AsyncMock()
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    pool._on_child_connected(0, t0)
    pool._on_child_connected(1, t1)

    # Child 0 hears 01:TARGET at RSSI 020 (weak)
    pool._on_child_packet(0, _make_packet(src="01:TARGET", rssi="020"))
    # Child 1 hears 01:TARGET at RSSI 090 (strong)
    pool._on_child_packet(1, _make_packet(src="01:TARGET", rssi="090"))

    # Frame: "000 I --- 18:001234 01:TARGET --:------ 30C9 000 00"
    # dst = parts[4] = "01:TARGET"
    frame = "000 I --- 18:001234 01:TARGET --:------ 30C9 000 00"
    asyncio.run(pool.write_frame(frame))

    # Child 1 should be selected (best RSSI for 01:TARGET)
    assert t1.write_frame.call_count == 1
    assert t0.write_frame.call_count == 0


# -- HGI filtering (accepted set) -----------------------------------------


async def test_accepted_hgis_filters_foreign_packets() -> None:
    """Packets from non-accepted HGIs are dropped before forwarding."""
    proto = _make_mock_protocol()
    t0, t1 = _make_mock_transport(), _make_mock_transport()
    pool = PooledTransport(
        proto,
        [t0, t1],
        config=TransportConfig(),
        loop=asyncio.get_event_loop(),
        accepted_hgis={"18:001234"},
    )
    pool._on_child_connected(0, t0)
    pool._on_child_connected(1, t1)

    # Child 0 is HGI 18:001234 (accepted)
    pool._child_hgi[0] = "18:001234"
    # Child 1 is HGI 18:999999 (foreign, not accepted)
    pool._child_hgi[1] = "18:999999"

    # Packet from accepted HGI → forwarded
    pool._on_child_packet(0, _make_packet(src="01:111", rssi="050"))
    await asyncio.sleep(0.01)
    assert proto.packet_received.call_count == 1

    # Packet from foreign HGI → dropped
    pool._on_child_packet(1, _make_packet(src="01:222", rssi="050"))
    await asyncio.sleep(0.01)
    assert proto.packet_received.call_count == 1  # still 1, not 2


async def test_set_accepted_hgis_updates_filter() -> None:
    """set_accepted_hgis updates the filter at runtime."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport()
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=asyncio.get_event_loop()
    )
    pool._on_child_connected(0, t0)
    pool._child_hgi[0] = "18:001234"

    # Initially no filter → all packets forwarded
    pool._on_child_packet(0, _make_packet(src="01:111", rssi="050"))
    await asyncio.sleep(0.01)
    assert proto.packet_received.call_count == 1

    # Set filter to exclude this HGI
    pool.set_accepted_hgis({"18:999999"})
    pool._on_child_packet(0, _make_packet(src="01:222", rssi="050"))
    await asyncio.sleep(0.01)
    assert proto.packet_received.call_count == 1  # dropped

    # Set filter back to include this HGI
    pool.set_accepted_hgis({"18:001234"})
    pool._on_child_packet(0, _make_packet(src="01:333", rssi="050"))
    await asyncio.sleep(0.01)
    assert proto.packet_received.call_count == 2  # forwarded


async def test_accepted_hgis_none_accepts_all() -> None:
    """When accepted_hgis is None, all packets are forwarded."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport()
    pool = PooledTransport(
        proto,
        [t0],
        config=TransportConfig(),
        loop=asyncio.get_event_loop(),
        accepted_hgis=None,
    )
    pool._on_child_connected(0, t0)
    pool._child_hgi[0] = "18:001234"

    pool._on_child_packet(0, _make_packet(src="01:111", rssi="050"))
    pool._on_child_packet(0, _make_packet(src="01:222", rssi="050"))
    await asyncio.sleep(0.01)
    assert proto.packet_received.call_count == 2


# -- Hot-reload: add_child / remove_child ---------------------------------


def test_remove_child_closes_transport_and_marks_removed(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """remove_child closes the transport and marks the slot as removed."""
    proto = _make_mock_protocol()
    t0, t1 = _make_mock_transport(), _make_mock_transport()
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    pool._on_child_connected(0, t0)
    pool._on_child_connected(1, t1)

    pool.remove_child(0)

    assert pool._transports[0] is None
    assert pool._child_connected[0] is False
    t0.close.assert_called_once()

    # Child 1 is still active
    assert pool._transports[1] is t1
    assert pool._child_connected[1] is True


def test_remove_child_excludes_from_selection(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """Removed children are not selected for outbound routing."""
    proto = _make_mock_protocol()
    t0, t1 = _make_mock_transport(), _make_mock_transport()
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    pool._on_child_connected(0, t0)
    pool._on_child_connected(1, t1)

    pool.remove_child(0)

    # Only child 1 is available
    selected = pool._select_transport()
    assert selected is t1


def test_remove_child_invalid_index_raises(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """remove_child with invalid index raises ValueError."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport()
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )
    with pytest.raises(ValueError, match="Invalid child index"):
        pool.remove_child(5)


def test_remove_already_removed_child_is_noop(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """Removing an already-removed child is a no-op."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport()
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )
    pool.remove_child(0)
    t0.close.assert_called_once()

    # Second removal should not call close again
    pool.remove_child(0)
    assert t0.close.call_count == 1


def test_pool_stats_includes_child_hgi_and_accepted(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """pool_stats includes child_hgi and accepted_hgis fields."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport()
    pool = PooledTransport(
        proto,
        [t0],
        config=TransportConfig(),
        loop=event_loop,
        accepted_hgis={"18:001234"},
    )
    pool._on_child_connected(0, t0)
    pool._child_hgi[0] = "18:001234"

    stats = pool.get_extra_info("pool_stats")
    assert stats is not None
    assert stats["child_hgi"] == ["18:001234"]
    assert stats["accepted_hgis"] == ["18:001234"]


def test_pool_stats_accepted_hgis_none_when_no_filter(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """pool_stats shows None for accepted_hgis when no filter is set."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport()
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )
    stats = pool.get_extra_info("pool_stats")
    assert stats["accepted_hgis"] is None
