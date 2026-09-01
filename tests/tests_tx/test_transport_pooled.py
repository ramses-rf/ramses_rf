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


# -- Constructor validation ------------------------------------------------


def test_empty_transport_list_raises(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """PooledTransport with empty transport list raises ValueError."""
    proto = _make_mock_protocol()
    with pytest.raises(ValueError, match="at least one child transport"):
        PooledTransport(proto, [], config=TransportConfig(), loop=event_loop)
