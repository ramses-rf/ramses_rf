#!/usr/bin/env python3
"""Tests for PooledTransport — multi-HGI link-layer pooling.

Covers:
- Inbound deduplication (same packet from 2 children -> 1 upstream)
- Inbound forwarding (distinct packets from different children)
- Outbound routing (stable-first among connected children)
- Connection lifecycle (wait for any, disconnect handling)
- get_extra_info aggregation (SZ_ACTIVE_HGI, pool_stats)
- Close propagation to all children
- PR 1: PoolChild state, ingress provenance, loopback exclusion,
  dict-backed dedup, RSSI TTL, no runtime add/remove
"""

import asyncio
from datetime import datetime as dt, timedelta as td
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from ramses_tx import exceptions as exc
from ramses_tx.const import I_, SZ_ACTIVE_HGI, SZ_IS_EVOFW3, Code
from ramses_tx.transport.base import TransportConfig
from ramses_tx.transport.pooled import (
    ConnectionState,
    IngressFrame,
    NodeAvailability,
    PoolChild,
    PooledTransport,
    _ChildProtocolProxy,
)
from ramses_tx.typing import DeviceIdT


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
    seq: str = "---",
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
    dto.seq = seq

    pkt = MagicMock()
    pkt._dto = dto
    pkt.__str__ = lambda self: (
        f"{rssi} {verb} {seq} {src} {dst} {addr3} {code} 000 {payload}"
    )
    return pkt


def _make_mock_transport(
    hgi: str | None = None,
    connected: bool = True,
    is_evofw3: bool = True,
) -> MagicMock:
    """Create a mock child transport.

    :param is_evofw3: If False, the transport reports SZ_IS_EVOFW3=False
        (HGI80 serial device).  Default True (evofw3/MQTT).
    """
    t = MagicMock()
    t.get_extra_info = lambda name, default=None: (
        hgi
        if name == SZ_ACTIVE_HGI
        else is_evofw3
        if name == SZ_IS_EVOFW3
        else default
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


def _connect_child(
    pool: PooledTransport, child_id: int, transport: MagicMock
) -> None:
    """Mark a child as connected with its transport's HGI identity."""
    pool._on_child_connected(child_id, transport)


def _connect_and_ready(
    pool: PooledTransport, child_id: int, transport: MagicMock
) -> None:
    """Connect a child and make it send-ready via a packet."""
    pool._on_child_connected(child_id, transport)
    # Feed a packet to mark online and send-ready.
    pool._on_child_packet(child_id, _make_packet(rssi="050", payload="FFFF"))


# -- Inbound deduplication -------------------------------------------------


async def test_dedup_same_packet_from_two_children_is_deduped() -> None:
    """Two children send the same packet -> only one reaches protocol."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), dedup_window=1.0
    )
    _connect_child(pool, 0, t0)
    _connect_child(pool, 1, t1)

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
    """Two children send different packets -> both reach the protocol."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), dedup_window=0.5
    )
    _connect_child(pool, 0, t0)
    _connect_child(pool, 1, t1)

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
    """Same packet after the dedup window expires -> both forwarded."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), dedup_window=0.05
    )
    _connect_child(pool, 0, t0)
    _connect_child(pool, 1, t1)

    pkt = _make_packet()
    pool._on_child_packet(0, pkt)
    await asyncio.sleep(0.1)  # wait for dedup window to expire
    pool._on_child_packet(1, pkt)

    await asyncio.sleep(0.01)

    assert proto.packet_received.call_count == 2
    stats = pool.get_extra_info("pool_stats")
    assert stats["deduped"] == 0


async def test_dedup_same_packet_from_same_child_is_deduped() -> None:
    """Same packet sent twice from the SAME child is deduped."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), dedup_window=1.0
    )
    _connect_child(pool, 0, t0)

    pkt = _make_packet()
    pool._on_child_packet(0, pkt)
    pool._on_child_packet(0, pkt)  # same child, same packet

    await asyncio.sleep(0.01)
    assert proto.packet_received.call_count == 1


async def test_dedup_key_includes_sequence_when_present() -> None:
    """Packets with same content but different sequence are NOT deduped."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), dedup_window=10.0
    )
    _connect_child(pool, 0, t0)

    # Same content, different sequence numbers.
    pool._on_child_packet(0, _make_packet(seq="001", payload="00"))
    pool._on_child_packet(0, _make_packet(seq="002", payload="00"))

    await asyncio.sleep(0.01)
    assert proto.packet_received.call_count == 2


async def test_dedup_key_fallback_when_sequence_absent() -> None:
    """Packets with no sequence (---) use fallback base key for dedup."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), dedup_window=1.0
    )
    _connect_child(pool, 0, t0)

    # Both have seq="---", same content -> deduped.
    pool._on_child_packet(0, _make_packet(seq="---", payload="00"))
    pool._on_child_packet(0, _make_packet(seq="---", payload="00"))

    await asyncio.sleep(0.01)
    assert proto.packet_received.call_count == 1


async def test_recent_tx_match_is_not_consumed() -> None:
    """One recorded TX must classify every matching copy as an echo.

    RF is a shared medium: a single transmission is heard by every
    receiver in the pool, so N inbound copies of the same frame must
    all be marked as echoes until the TTL expires (issue 1185).
    """
    proto = _make_mock_protocol()
    pool = PooledTransport(proto, [None], config=TransportConfig())
    packet = _make_packet()
    dto = packet._dto
    key = (
        dto.verb,
        dto.code,
        dto.addr1,
        dto.addr2,
        dto.addr3,
        dto.raw_payload,
    )
    now = dt.now()
    pool._recent_tx_queue.append((now, key))
    pool._recent_tx_counts[key] = 1

    # Every receiver's copy of the same TX matches — the key is not
    # consumed by the first match.
    assert pool._is_recent_tx(packet) is True
    assert pool._is_recent_tx(packet) is True
    assert pool._recent_tx_queue
    assert key in pool._recent_tx_counts


async def test_dedup_cache_is_dict_backed() -> None:
    """Dedup cache is a dict, not a deque."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), dedup_window=10.0
    )
    _connect_child(pool, 0, t0)

    assert isinstance(pool._dedup_cache, dict)


async def test_dedup_cache_size_bounded() -> None:
    """Dedup cache size is bounded by _MAX_DEDUP_KEYS."""
    from ramses_tx.transport.pooled import _MAX_DEDUP_KEYS

    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), dedup_window=999.0
    )
    _connect_child(pool, 0, t0)

    # Send _MAX_DEDUP_KEYS + 10 distinct packets.
    for i in range(_MAX_DEDUP_KEYS + 10):
        pool._on_child_packet(0, _make_packet(payload=f"{i:04X}"))

    # Cache should be capped at _MAX_DEDUP_KEYS.
    assert len(pool._dedup_cache) <= _MAX_DEDUP_KEYS


# -- Outbound routing ------------------------------------------------------


async def test_outbound_routes_to_connected_child() -> None:
    """write_frame routes to a connected child."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111", connected=False)
    t1 = _make_mock_transport(hgi="18:002222", connected=True)
    pool = PooledTransport(proto, [t0, t1], config=TransportConfig())
    # Only connect child 1.
    _connect_and_ready(pool, 1, t1)

    await pool.write_frame(
        " 000 I --- 01:123456 18:000730 --:------ 30C9 000 00"
    )

    t0.write_frame.assert_not_called()
    t1.write_frame.assert_called_once()


async def test_outbound_stable_first_among_connected() -> None:
    """write_frame selects the first sendable child (stable-first)."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111", connected=True)
    t1 = _make_mock_transport(hgi="18:002222", connected=True)
    pool = PooledTransport(proto, [t0, t1], config=TransportConfig())
    _connect_and_ready(pool, 0, t0)
    _connect_and_ready(pool, 1, t1)

    await pool.write_frame("frame1")
    await pool.write_frame("frame2")

    # Child 0 (first in config order) should get both calls.
    assert t0.write_frame.call_count == 2
    assert t1.write_frame.call_count == 0


async def test_legacy_write_frame_routes_using_destination_address() -> None:
    """RSSI-prefixed frames route using the resolved destination address."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(proto, [t0, t1], config=TransportConfig())
    _connect_and_ready(pool, 0, t0)
    _connect_and_ready(pool, 1, t1)
    pool._children[0].rssi_tracker.clear()
    pool._children[1].rssi_tracker.clear()
    now = dt.now()
    pool._children[0].rssi_tracker.record("01:123456", "-90", now)
    pool._children[1].rssi_tracker.record("01:123456", "-40", now)

    await pool.write_frame(
        "000  W --- 18:999999 01:123456 --:------ 0008 002 0000"
    )

    t0.write_frame.assert_not_called()
    sent_frame = t1.write_frame.await_args.args[0]
    assert sent_frame.split()[3] == "18:002222"
    assert sent_frame.split()[4] == "01:123456"


async def test_unaccepted_serial_child_is_receive_only() -> None:
    """A known unaccepted serial HGI forwards RX but is excluded from TX."""
    proto = _make_mock_protocol()
    transport = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto,
        [transport],
        config=TransportConfig(),
        accepted_hgis={"18:001111"},
    )

    _connect_child(pool, 0, transport)
    pool._on_child_packet(0, _make_packet())
    await asyncio.sleep(0.01)

    assert pool._children[0].accepted is False
    assert pool._children[0].is_sendable is False
    proto.packet_received.assert_called_once()


async def test_sentinel_hgi_zigbee_child_is_receive_only() -> None:
    """A child reporting the unknown-HGI sentinel is never selected for TX.

    Zigbee transports fall back to ``18:000730`` when the real HGI ID
    cannot be derived or configured. Unless that sentinel is itself in
    ``accepted_hgis`` (HGI80 placeholder setups), the child must remain
    receive-only and must not be chosen by ``_select_child``.
    """
    proto = _make_mock_protocol()
    zigbee_child = _make_mock_transport(hgi="18:000730")
    mqtt_child = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto,
        [zigbee_child, mqtt_child],
        config=TransportConfig(),
        accepted_hgis={"18:001111"},
    )

    _connect_and_ready(pool, 0, zigbee_child)
    _connect_and_ready(pool, 1, mqtt_child)
    await asyncio.sleep(0.01)

    assert pool._children[0].accepted is False
    assert pool._children[0].is_sendable is False
    assert pool._select_child() is pool._children[1]


# -- Runtime removal + HGI reassignment (issue 1261) ------------------------


async def test_remove_child_tombstones_not_shifts() -> None:
    """remove_child keeps sibling child_ids stable (no index shift)."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(proto, [t0, t1], config=TransportConfig())
    _connect_and_ready(pool, 0, t0)
    _connect_and_ready(pool, 1, t1)
    await asyncio.sleep(0.01)

    assert pool.remove_child(0) is True
    # Sibling's index/id and routing are unaffected.
    assert pool._child_by_id(1).child_id == 1
    assert pool._children[1].removed is False
    assert pool._select_child() is pool._children[1]
    # Tombstone is excluded from live views but still addressable.
    assert pool._children[0].removed is True
    assert pool._child_by_id(0).removed is True
    assert len(pool._live_children) == 1
    assert pool.remove_child(0) is False  # idempotent
    assert pool.remove_child(9) is False  # out of range


async def test_removed_child_drops_packets_and_writes() -> None:
    """A tombstoned child forwards nothing and rejects TX."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(proto, [t0, t1], config=TransportConfig())
    _connect_and_ready(pool, 0, t0)
    _connect_and_ready(pool, 1, t1)

    pool.remove_child(0)

    # Inbound packet on the removed child is dropped.
    pool._on_child_packet(0, _make_packet(rssi="050"))
    proto.packet_received.assert_not_called()

    # In-flight routed command to the removed child is refused.
    from ramses_tx.routing import RoutedCommand

    routed = RoutedCommand(child_id="0", command=MagicMock())
    outcome = await pool.write_routed(routed, "frame")
    assert outcome.name == "NOT_SUBMITTED"
    t0.write_frame.assert_not_called()
    t0.close.assert_called_once()  # transport was closed on removal


def test_removed_child_reconnect_is_rejected(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """A reconnecting tombstoned child is closed, not rejoined."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )
    pool._on_child_connected(0, t0)
    pool.remove_child(0)
    t0.close.reset_mock()

    reconnect = _make_mock_transport(hgi="18:001111")
    pool._on_child_connected(0, reconnect)
    reconnect.close.assert_called_once()
    assert pool._connected_children == []


def test_move_hgi_transfers_identity_and_rssi(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """move_hgi carries hgi_id, accepted and rssi_tracker to dst."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto,
        [t0, None],
        config=TransportConfig(),
        accepted_hgis={"18:001111"},
        loop=event_loop,
    )
    pool._on_child_connected(0, t0)
    src = pool._child_by_id(0)
    src.rssi_tracker.record("04:111111", "050", dt.now())

    dst = pool._child_by_id(1)
    dst.callback_driven = True
    assert pool.move_hgi(0, 1) is True

    assert src.hgi_id is None
    assert dst.hgi_id == DeviceIdT("18:001111")
    assert dst.accepted is True
    assert dst.rssi_tracker is src.rssi_tracker
    # Source falls back to the accepted-set rule with no HGI.
    assert src.accepted is True


def test_move_hgi_rejects_invalid_args(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """move_hgi refuses bad ids, tombstones and identity-less src."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0, None], config=TransportConfig(), loop=event_loop
    )
    pool._on_child_connected(0, t0)

    assert pool.move_hgi(9, 1) is False  # src out of range
    assert pool.move_hgi(0, 9) is False  # dst out of range
    assert pool.move_hgi(1, 0) is False  # src has no hgi_id
    pool.remove_child(0)
    assert pool.move_hgi(0, 1) is False  # src tombstoned


async def test_child_connection_lost_fails_wait_promptly() -> None:
    """A child that fails to connect surfaces the error, not a timeout.

    Regression: ``wait_for_connection_made`` previously only woke on
    ``connection_made`` — a child whose transport fails during init
    (e.g. a zigbee:// child with no ZHA) stalled pool construction for
    the full per-child timeout.  ``connection_lost`` must now wake the
    waiter with the real error.
    """
    proto = _make_mock_protocol()
    pool = PooledTransport(proto, [None], config=TransportConfig())
    proxy = _ChildProtocolProxy(pool, 0)

    # Simulate a child transport failing during async init.
    proxy.connection_lost(exc.TransportZigbeeError("no ZHA gateway"))

    start = dt.now()
    with pytest.raises(exc.TransportError, match="failed to connect"):
        await proxy.wait_for_connection_made(timeout=60.0)
    # Must fail fast — not after the 60s timeout.
    assert (dt.now() - start).total_seconds() < 5.0


async def test_outbound_fails_when_no_child_connected() -> None:
    """write_frame raises when no child is connected."""
    from ramses_tx import exceptions as exc

    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111", connected=False)
    pool = PooledTransport(proto, [t0], config=TransportConfig())

    with pytest.raises(exc.TransportError, match="No connected child"):
        await pool.write_frame("frame1")


async def test_outbound_fails_when_child_not_send_ready() -> None:
    """A connected child with no HGI identity is not send-ready."""
    from ramses_tx import exceptions as exc

    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi=None)
    pool = PooledTransport(proto, [t0], config=TransportConfig())
    # Connect but transport reports no HGI — send_ready stays False.
    _connect_child(pool, 0, t0)

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
    _connect_child(pool, 0, t0)
    _connect_child(pool, 1, t1)

    # Disconnect first child — should NOT notify protocol (one still up).
    pool._on_child_disconnected(0, None)
    proto.connection_lost.assert_not_called()

    # Disconnect second child — SHOULD notify protocol.
    pool._on_child_disconnected(1, None)
    await asyncio.sleep(0.01)
    proto.connection_lost.assert_called_once()


async def test_disconnect_only_affects_one_child() -> None:
    """connection_lost on one child does not affect other children."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(proto, [t0, t1], config=TransportConfig())
    _connect_and_ready(pool, 0, t0)
    _connect_and_ready(pool, 1, t1)

    # Disconnect child 0.
    pool._on_child_disconnected(0, None)

    # Child 1 should still be connected and sendable.
    assert pool._children[1].is_connected
    assert pool._children[1].is_sendable
    # Child 0 should be disconnected.
    assert not pool._children[0].is_connected


async def test_reconnect_restores_transport_for_failed_child() -> None:
    """A child that failed at construction has transport=None; a later
    connection_made (e.g. Zigbee availability reconnect) must restore it
    or is_sendable stays False forever."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(proto, [t0, t1], config=TransportConfig())
    _connect_and_ready(pool, 1, t1)

    # Simulate a child whose transport never connected at startup
    # (__dict__ poke: direct assignment narrows the attr to None for
    # mypy, making the post-reconnect asserts "unreachable")
    child = pool._children[0]
    child.__dict__["transport"] = None
    assert not child.is_sendable

    # Reconnect: mark_connected must re-attach the transport object.
    pool._on_child_connected(0, t0)
    assert child.is_connected
    assert child.transport is t0
    assert child.is_sendable


def test_callback_child_reconnect_does_not_store_transport() -> None:
    """Callback-driven children keep transport=None — outbound frames
    go through the pool's adapter, not a per-child transport."""
    cb = PoolChild(
        child_id=0, port_name="mqtt_ha://18:001111", callback_driven=True
    )
    cb.mark_connected(_make_mock_transport(hgi="18:001111"))
    assert cb.transport is None
    assert cb.is_connected


# -- get_extra_info --------------------------------------------------------


def test_get_extra_info_active_hgi_returns_first_connected(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """get_extra_info(SZ_ACTIVE_HGI) returns first connected child's HGI."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    _connect_child(pool, 0, t0)
    _connect_child(pool, 1, t1)

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
    # Only connect child 1.
    _connect_child(pool, 1, t1)

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
    _connect_child(pool, 0, t0)
    pool._children[0].pkts_received = 5
    pool._pkts_deduped = 2
    pool._pkts_forwarded = 3

    stats = pool.get_extra_info("pool_stats")
    assert stats["children"] == 1
    assert stats["connected"] == 1
    assert stats["received"] == [5]
    assert stats["deduped"] == 2
    assert stats["forwarded"] == 3


def test_get_extra_info_pool_rssi_trackers_returns_only_connected(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """pool_rssi_trackers returns trackers for connected children only."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    _connect_child(pool, 0, t0)
    # Child 1 not connected.

    trackers = pool.get_extra_info("pool_rssi_trackers")
    assert len(trackers) == 1  # only child 0 is connected


def test_get_extra_info_pool_rssi_trackers_all_connected(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """pool_rssi_trackers returns all trackers when all connected."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    _connect_child(pool, 0, t0)
    _connect_child(pool, 1, t1)

    trackers = pool.get_extra_info("pool_rssi_trackers")
    assert len(trackers) == 2


def test_get_extra_info_pool_hgi_ids(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """pool_hgi_ids returns HGI IDs of all connected children."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    _connect_child(pool, 0, t0)
    _connect_child(pool, 1, t1)

    hgi_ids = pool.get_extra_info("pool_hgi_ids")
    assert hgi_ids == ["18:001111", "18:002222"]


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


# -- get_extra_info(SZ_IS_EVOFW3) — issue 1185 -----------------------------


def test_get_extra_info_evofw3_all_serial_evofw3(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """get_extra_info(SZ_IS_EVOFW3) returns True when all serial children
    are evofw3."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111", is_evofw3=True)
    t1 = _make_mock_transport(hgi="18:002222", is_evofw3=True)
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    _connect_child(pool, 0, t0)
    _connect_child(pool, 1, t1)

    assert pool.get_extra_info(SZ_IS_EVOFW3) is True


def test_get_extra_info_evofw3_all_serial_hgi80(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """get_extra_info(SZ_IS_EVOFW3) returns False when all serial children
    are HGI80 (not evofw3) and no callback-driven children exist."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111", is_evofw3=False)
    t1 = _make_mock_transport(hgi="18:002222", is_evofw3=False)
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    _connect_child(pool, 0, t0)
    _connect_child(pool, 1, t1)

    # No evofw3 children and no callback-driven children → returns default
    assert pool.get_extra_info(SZ_IS_EVOFW3, default=False) is False


def test_get_extra_info_evofw3_mixed_serial(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """get_extra_info(SZ_IS_EVOFW3) returns True when at least one serial
    child is evofw3 (even if others are HGI80)."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111", is_evofw3=False)  # HGI80
    t1 = _make_mock_transport(hgi="18:002222", is_evofw3=True)  # evofw3
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    _connect_child(pool, 0, t0)
    _connect_child(pool, 1, t1)

    assert pool.get_extra_info(SZ_IS_EVOFW3) is True


def test_get_extra_info_evofw3_callback_driven_returns_true(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """get_extra_info(SZ_IS_EVOFW3) returns True when any child is
    callback-driven (MQTT), even if no serial child is evofw3.

    This is the hybrid-pool case from issue 1185: HGI80 serial primary
    + MQTT callback child.  The pool should report evofw3=True so the
    protocol doesn't apply the HGI80 reverse-patch to MQTT TX.
    """
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111", is_evofw3=False)  # HGI80
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )
    _connect_child(pool, 0, t0)

    # Add a callback-driven child (MQTT) — no transport instance.
    pool._children.append(
        PoolChild(
            child_id=1,
            port_name="mqtt://broker/18:002222",
            callback_driven=True,
        )
    )
    pool._children[1].connection_state = ConnectionState.CONNECTED
    pool._children[1].hgi_id = DeviceIdT("18:002222")
    pool._children[1].send_ready = True

    assert pool.get_extra_info(SZ_IS_EVOFW3) is True


def test_get_extra_info_evofw3_callback_driven_even_if_disconnected(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """get_extra_info(SZ_IS_EVOFW3) returns True for callback-driven
    children even when they are not yet connected.

    The first command may be sent before any MQTT child comes online
    via LWT.  Without this, the protocol patches the HGI ID to
    18:000730 (HGI80 mode), causing outbound packets to use the
    sentinel instead of the real HGI ID.
    """
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111", is_evofw3=False)  # HGI80
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )
    _connect_child(pool, 0, t0)

    # Add a disconnected callback-driven child (MQTT).
    pool._children.append(
        PoolChild(
            child_id=1,
            port_name="mqtt://broker/18:002222",
            callback_driven=True,
            # connection_state defaults to DISCONNECTED
        )
    )

    assert pool.get_extra_info(SZ_IS_EVOFW3) is True


def test_get_extra_info_evofw3_empty_pool_returns_default(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """get_extra_info(SZ_IS_EVOFW3) returns the default for an empty or
    all-disconnected pool with no callback-driven children."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111", is_evofw3=True)
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )
    # Don't connect any child.

    assert pool.get_extra_info(SZ_IS_EVOFW3, default=False) is False


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
    _connect_child(pool, 0, t0)

    repr_str = repr(pool)
    assert "PooledTransport" in repr_str
    assert "children=2" in repr_str
    assert "connected=1" in repr_str


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


async def test_close_notifies_protocol_for_callback_only_pool() -> None:
    """Closing a connected callback-only pool unbinds the protocol."""
    proto = _make_mock_protocol()
    pool = PooledTransport(proto, [None], config=TransportConfig())
    pool._protocol_connected = True

    pool.close()
    await asyncio.sleep(0)

    proto.connection_lost.assert_called_once_with(None)


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


async def test_packets_during_close_are_ignored() -> None:
    """Packets received after close() are not forwarded."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(proto, [t0], config=TransportConfig())
    _connect_child(pool, 0, t0)

    pool.close()
    pool._on_child_packet(0, _make_packet())

    await asyncio.sleep(0.01)
    proto.packet_received.assert_not_called()


# -- _ChildProtocolProxy ---------------------------------------------------


def test_child_proxy_routes_packet_to_pool() -> None:
    """_ChildProtocolProxy.packet_received routes to the pool."""
    pool = MagicMock()
    proxy = _ChildProtocolProxy(pool, 0)
    pkt = _make_packet()

    proxy.packet_received(pkt)

    pool._on_child_packet.assert_called_once_with(0, pkt, ingress_hgi_id=None)


def test_child_proxy_routes_packet_with_ingress_hgi() -> None:
    """_ChildProtocolProxy.packet_received passes ingress_hgi_id."""
    pool = MagicMock()
    proxy = _ChildProtocolProxy(pool, 0)
    pkt = _make_packet()

    proxy.packet_received(pkt, ingress_hgi_id="18:001111")

    pool._on_child_packet.assert_called_once_with(
        0, pkt, ingress_hgi_id="18:001111"
    )


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


# -- PR 1: PoolChild state -------------------------------------------------


def test_pool_child_connection_states() -> None:
    """PoolChild connection state transitions."""
    t = _make_mock_transport(hgi="18:001111")
    child = PoolChild(child_id=0, port_name="/dev/ttyUSB0", transport=t)
    assert not child.is_connected

    # Connect with a transport that reports an HGI.
    child.mark_connected(t)
    assert child.is_connected
    assert child.is_sendable  # type: ignore[unreachable]

    # Disconnect.
    child.mark_disconnected()
    assert not child.is_connected
    assert not child.is_sendable


def test_pool_child_availability_transitions() -> None:
    """PoolChild availability: offline -> online -> stale."""
    child = PoolChild(child_id=0, port_name="mqtt://localhost")
    assert not child.is_online

    child.mark_online()
    assert child.is_online
    last_seen = child.last_pkt_time  # type: ignore[unreachable]
    assert last_seen is not None

    child.mark_stale()
    assert not child.is_online


def test_pool_child_send_ready_requires_identity() -> None:
    """A child that never received a packet is not send-ready."""
    t = _make_mock_transport(hgi=None)
    child = PoolChild(child_id=0, port_name="/dev/ttyUSB0", transport=t)
    # Connect with a transport that has no HGI.
    child.mark_connected(t)
    assert child.is_connected
    assert not child.is_sendable  # no HGI identity yet

    # Learn HGI from a puzzle response.
    child.learn_hgi(DeviceIdT("18:001111"))
    assert child.is_sendable


def test_pool_child_rssi_quarantined_on_disconnect() -> None:
    """RSSI evidence is cleared when a child goes offline."""
    child = PoolChild(child_id=0, port_name="/dev/ttyUSB0")
    child.rssi_tracker.record("01:123456", -50, dt.now())
    assert child.rssi_tracker.best_rssi_for("01:123456") == -50

    child.mark_disconnected()
    # RSSI data should be cleared (quarantined).
    assert child.rssi_tracker.best_rssi_for("01:123456") is None


# -- PR 1: IngressFrame ---------------------------------------------------


def test_ingress_frame_is_frozen() -> None:
    """IngressFrame is immutable."""
    iframe = IngressFrame(
        frame=" 000 I --- 01:123456 18:000730 --:------ 30C9 000 00",
        timestamp="2026-09-04T12:00:00",
        ingress_hgi_id=DeviceIdT("18:001111"),
    )
    assert iframe.frame.startswith(" 000 I")
    assert iframe.ingress_hgi_id == DeviceIdT("18:001111")
    # Frozen dataclass — cannot set attributes.
    with pytest.raises(AttributeError):
        iframe.frame = "other"


# -- PR 1: Loopback exclusion ----------------------------------------------


async def test_loopback_excluded_from_route_rssi() -> None:
    """Frames from an active pool HGI source are not recorded as RSSI."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), dedup_window=10.0
    )
    _connect_child(pool, 0, t0)
    _connect_child(pool, 1, t1)

    # Simulate a loopback: child 0 hears a frame from HGI 18:001111
    # (its own transmission echo).  This should NOT be recorded as RSSI.
    loopback_pkt = _make_packet(src="18:001111", rssi="-83")
    pool._on_child_packet(0, loopback_pkt)

    # Child 0's tracker should NOT have RSSI for 18:001111.
    assert pool._children[0].rssi_tracker.best_rssi_for("18:001111") is None

    # A normal device frame SHOULD be recorded.
    normal_pkt = _make_packet(src="01:123456", rssi="-50")
    pool._on_child_packet(0, normal_pkt)
    assert pool._children[0].rssi_tracker.best_rssi_for("01:123456") == -50


async def test_unrelated_hgi_frames_not_suppressed() -> None:
    """Frames from a pool HGI that is NOT the source are forwarded."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), dedup_window=10.0
    )
    _connect_child(pool, 0, t0)
    _connect_child(pool, 1, t1)

    # A frame where src is 18:002222 (the other HGI) heard by child 0.
    # This is a cross-HGI frame, not loopback — it should be forwarded.
    pkt = _make_packet(src="18:002222", payload="ABCD")
    pool._on_child_packet(0, pkt)

    await asyncio.sleep(0.01)
    assert proto.packet_received.call_count == 1


# -- PR 1: Ingress provenance on Packet envelope --------------------------


async def test_packet_carries_ingress_hgi_id() -> None:
    """The Packet envelope carries the ingress HGI ID after pool forward."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), dedup_window=10.0
    )
    _connect_child(pool, 0, t0)

    pkt = _make_packet(src="04:123456")
    pool._on_child_packet(0, pkt)

    await asyncio.sleep(0.01)
    # The forwarded packet should carry the ingress HGI ID.
    forwarded_pkt = proto.packet_received.call_args[0][0]
    assert forwarded_pkt._ingress_hgi_id == "18:001111"


async def test_explicit_ingress_hgi_overrides_child_record() -> None:
    """Explicit ingress_hgi_id overrides the child record's HGI."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), dedup_window=10.0
    )
    _connect_child(pool, 0, t0)

    pkt = _make_packet(src="04:123456")
    pool._on_child_packet(0, pkt, ingress_hgi_id=DeviceIdT("18:009999"))

    await asyncio.sleep(0.01)
    forwarded_pkt = proto.packet_received.call_args[0][0]
    assert forwarded_pkt._ingress_hgi_id == "18:009999"


async def test_local_echo_and_over_air_copy_different_provenance_same_dedup() -> (
    None
):
    """Local echo and over-air copy have different ingress HGI but dedup.

    Regression test for PR 1 spec: "The selected child's local echo and
    another child's over-air copy retain different ingress provenance but
    deduplicate as one RF frame."
    """
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), dedup_window=10.0
    )
    _connect_child(pool, 0, t0)
    _connect_child(pool, 1, t1)

    # Same RF frame heard by both children.
    pkt0 = _make_packet(src="04:123456", payload="ABCD")
    pkt1 = _make_packet(src="04:123456", payload="ABCD")

    pool._on_child_packet(0, pkt0)
    pool._on_child_packet(1, pkt1)

    await asyncio.sleep(0.01)

    # Only one packet forwarded (deduped).
    assert proto.packet_received.call_count == 1

    # The forwarded packet carries the FIRST child's ingress HGI.
    forwarded_pkt = proto.packet_received.call_args[0][0]
    assert forwarded_pkt._ingress_hgi_id == "18:001111"


async def test_packet_ingress_hgi_id_none_without_pool() -> None:
    """A Packet created outside a pool has ingress_hgi_id=None."""
    from datetime import datetime as dt

    from ramses_tx.packet import Packet

    pkt = Packet(
        dt(2026, 1, 1, 12, 0, 0),
        "000  I --- 01:123456 18:000730 --:------ 30C9 001 00",
    )
    assert pkt.ingress_hgi_id is None


# -- PR 1: No runtime add/remove/accepted ----------------------------------


def test_no_add_child_method(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """PooledTransport does not expose runtime add_child()."""
    proto = _make_mock_protocol()
    pool = PooledTransport(
        proto, [None], config=TransportConfig(), loop=event_loop
    )
    assert not hasattr(pool, "add_child")


def test_no_set_accepted_hgis_method(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """PooledTransport does not expose runtime set_accepted_hgis()."""
    proto = _make_mock_protocol()
    pool = PooledTransport(
        proto, [None], config=TransportConfig(), loop=event_loop
    )
    assert not hasattr(pool, "set_accepted_hgis")


# -- PR 1: Health monitoring (no last-resort re-enable) --------------------


def test_health_timeout_marks_stale_not_offline(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """A child with no packets for health_timeout is marked STALE."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto,
        [t0, t1],
        config=TransportConfig(),
        loop=event_loop,
        health_timeout=0.05,
    )
    _connect_and_ready(pool, 0, t0)
    _connect_and_ready(pool, 1, t1)

    # Set child 0's last packet time to the past (stale).
    pool._children[0].last_pkt_time = dt.now() - td(seconds=1)

    pool._check_health()

    assert pool._children[0].availability is NodeAvailability.STALE
    assert pool._children[1].is_online


def test_no_last_resort_reenable_of_offline_children(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """A disconnected child is never re-enabled as a last resort."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )
    _connect_and_ready(pool, 0, t0)

    # Disconnect the child.
    pool._children[0].mark_disconnected()

    # _select_child should NOT re-enable the disconnected child.
    child = pool._select_child()
    assert child is None  # no sendable child
    assert not pool._children[0].is_connected


def test_quiet_serial_child_stays_connected(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """A quiet connected serial child stays connected/online while
    its route evidence expires (RSSI TTL)."""
    from ramses_tx.rssi_tracker import POOL_TTL

    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )
    _connect_and_ready(pool, 0, t0)

    # Record RSSI with an old timestamp (beyond TTL) for a distinct device.
    old_time = dt.now() - POOL_TTL - td(seconds=1)
    pool._children[0].rssi_tracker.record("04:999999", -50, old_time)

    # The RSSI should be expired on access.
    assert pool._children[0].rssi_tracker.best_rssi_for("04:999999") is None

    # But the child should still be connected.
    assert pool._children[0].is_connected


# -- PR 1: Active HGI IDs derived from child records -----------------------


def test_active_hgi_ids_derived_from_children(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """Active HGI IDs are derived from accepted child records."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    _connect_child(pool, 0, t0)
    _connect_child(pool, 1, t1)

    hgi_ids = pool._active_hgi_ids
    assert len(hgi_ids) == 2
    hgi_strs = [str(h) for h in hgi_ids]
    assert "18:001111" in hgi_strs
    assert "18:002222" in hgi_strs


# -- PR 1: RSSI per-child tracking -----------------------------------------


def test_pool_rssi_trackers_record_per_child(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """Each child's RSSI tracker records independently."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    _connect_child(pool, 0, t0)
    _connect_child(pool, 1, t1)

    pkt_child0 = _make_packet(src="04:123456", rssi="-41")
    pkt_child1 = _make_packet(src="04:123456", rssi="-89")

    pool._on_child_packet(0, pkt_child0)
    pool._on_child_packet(1, pkt_child1)

    trackers = pool.get_extra_info("pool_rssi_trackers")
    assert len(trackers) == 2
    assert trackers[0].best_rssi_for("04:123456") == -41
    assert trackers[1].best_rssi_for("04:123456") == -89


# -- PR 1: Constructor allows empty list (factory validates) ---------------


def test_empty_transport_list_does_not_raise(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """PooledTransport allows empty list — factory validates, not ctor."""
    proto = _make_mock_protocol()
    pool = PooledTransport(
        proto, [], config=TransportConfig(), loop=event_loop
    )
    assert len(pool._children) == 0


# -- Phase 2: signature policy and serial child state --------------------


def test_identity_unknown_child_is_not_sendable() -> None:
    """A child with no HGI identity is not sendable (receive-only)."""
    child = PoolChild(
        child_id=0, port_name="/dev/ttyUSB0", transport=MagicMock()
    )
    child.connection_state = ConnectionState.CONNECTED
    child.accepted = True
    # No HGI identity learned yet.
    assert child.hgi_id is None
    assert not child.is_sendable
    assert not child.send_ready


def test_identity_unknown_child_becomes_sendable_after_learn_hgi() -> None:
    """A child becomes sendable after learning its HGI identity."""
    child = PoolChild(
        child_id=0, port_name="/dev/ttyUSB0", transport=MagicMock()
    )
    child.connection_state = ConnectionState.CONNECTED
    child.availability = NodeAvailability.ONLINE
    child.accepted = True
    assert not child.is_sendable
    child.learn_hgi(DeviceIdT("18:001234"))
    assert child.hgi_id == DeviceIdT("18:001234")
    assert child.send_ready
    assert child.is_sendable


async def test_serial_child_learns_hgi_from_18_src_packet(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """Single serial child learns HGI ID from any 18:xxxxxx src packet.

    With only one serial child in the pool, all 18: src packets must
    come from the HGI physically connected to that port, so learning is
    safe (issue 1185).
    """
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi=None)  # HGI unknown at connect time
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )
    pool._on_child_connected(0, t0)

    # Child connected but not sendable (no HGI ID yet)
    assert not pool._children[0].is_sendable

    # Feed a 3150 packet with src=18:149488 (not _PUZZ)
    pkt = _make_packet(src="18:149488", code=Code._3150, payload="00")
    pool._on_child_packet(0, pkt)
    await asyncio.sleep(0.01)

    # Child should have learned its HGI ID and become sendable
    assert pool._children[0].hgi_id == DeviceIdT("18:149488")
    assert pool._children[0].send_ready
    assert pool._children[0].is_sendable


async def test_multi_serial_children_do_not_learn_hgi_from_rf(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """Multiple serial children must NOT learn HGI from RF packets.

    RF is a shared medium — every serial child receives packets from
    every HGI in range.  Learning from RF would make all children
    learn the same (wrong) HGI ID.  When there are multiple serial
    children, HGI IDs must be provided explicitly (issue 1185).
    """
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi=None)
    t1 = _make_mock_transport(hgi=None)
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    pool._on_child_connected(0, t0)
    pool._on_child_connected(1, t1)

    # Feed a _PUZZ packet with src=18:149488 to child 0
    pkt_puzz = _make_packet(src="18:149488", code=Code._PUZZ, payload="00")
    pool._on_child_packet(0, pkt_puzz)
    await asyncio.sleep(0.01)

    # Feed a 3150 packet with src=18:149488 to child 1
    pkt_3150 = _make_packet(src="18:149488", code=Code._3150, payload="00")
    pool._on_child_packet(1, pkt_3150)
    await asyncio.sleep(0.01)

    # Neither child should have learned — multi-serial pool
    assert pool._children[0].hgi_id is None
    assert pool._children[1].hgi_id is None
    assert not pool._children[0].send_ready
    assert not pool._children[1].send_ready


async def test_callback_child_does_not_learn_hgi_from_18_src_packet(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """Callback-driven (MQTT) child does NOT learn from 18: src packets.

    Only _PUZZ packets trigger learning for callback-driven children,
    because multiple HGIs share the same MQTT broker and any 18:
    packet could be from a different HGI.
    """
    from ramses_tx.transport.pooled import ConnectionState, PoolChild

    proto = _make_mock_protocol()
    pool = PooledTransport(
        proto, [None], config=TransportConfig(), loop=event_loop
    )
    # Make child 0 callback-driven with no HGI
    pool._children[0] = PoolChild(
        child_id=0, port_name="mqtt_ha://18:001234", transport=None
    )
    pool._children[0].callback_driven = True
    pool._children[0].connection_state = ConnectionState.CONNECTED
    pool._children[0].accepted = True

    # Feed a 3150 packet with src=18:149488 (not _PUZZ)
    pkt = _make_packet(src="18:149488", code=Code._3150, payload="00")
    pool._on_child_packet(0, pkt)
    await asyncio.sleep(0.01)

    # Callback child should NOT have learned HGI from non-_PUZZ packet
    assert pool._children[0].hgi_id is None


async def test_write_failure_increments_child_error_counter(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """A write failure in write_routed increments the child's errors."""
    from ramses_tx.routing import RoutedCommand, WriteOutcome

    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t0.write_frame = AsyncMock(side_effect=RuntimeError("port gone"))
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), loop=event_loop
    )
    _connect_and_ready(pool, 0, t0)

    routed = RoutedCommand(child_id="0", command=MagicMock())
    outcome = await pool.write_routed(routed, "test frame")
    assert outcome is WriteOutcome.AMBIGUOUS
    assert pool._children[0].consecutive_errors == 1


async def test_identity_unknown_child_not_selected_for_outbound(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """A child with unknown identity is not selected for outbound routing."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi=None)  # no HGI identity
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), loop=event_loop
    )
    # Connect both children.
    pool._on_child_connected(0, t0)
    pool._on_child_connected(1, t1)
    # Feed a packet to child 1 to make it online + send-ready.
    pool._on_child_packet(1, _make_packet(rssi="050", payload="FFFF"))

    # Child 0 has no HGI identity and should not be sendable.
    assert not pool._children[0].is_sendable
    # Child 1 should be sendable.
    assert pool._children[1].is_sendable

    # Select a child for outbound — should pick child 1, not 0.
    child = pool._select_child("04:123456")
    assert child is not None
    assert child.child_id == 1


def test_disconnected_child_resets_send_ready() -> None:
    """A disconnected child loses send_ready and must re-validate identity."""
    child = PoolChild(
        child_id=0, port_name="/dev/ttyUSB0", transport=MagicMock()
    )
    child.connection_state = ConnectionState.CONNECTED
    child.availability = NodeAvailability.ONLINE
    child.accepted = True
    child.learn_hgi(DeviceIdT("18:001234"))
    assert child.is_sendable

    child.mark_disconnected()
    assert not child.is_sendable
    # mypy narrows send_ready to Literal[False] after mark_disconnected()
    # (which sets it to False), making this assert "unreachable" — but we
    # want to verify the runtime value here.
    assert not child.send_ready  # type: ignore[unreachable]
    assert child.connection_state is ConnectionState.DISCONNECTED


# -- Stale child fallback ----------------------------------------------------


async def test_select_child_falls_back_to_stale_when_no_online() -> None:
    """Stale children are tried as last resort when no online children."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), health_timeout=0.1
    )
    _connect_and_ready(pool, 0, t0)

    # Let the child go stale (no packets for health_timeout)
    await asyncio.sleep(0.15)
    pool._check_health()
    assert pool._children[0].availability is NodeAvailability.STALE
    assert not pool._children[0].is_sendable

    # But _select_child should still return it as a last resort
    child = pool._select_child()
    assert child is not None
    assert child.child_id == 0


async def test_select_child_returns_none_when_no_sendable() -> None:
    """Returns None when no children are sendable AND none are stale."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(proto, [t0], config=TransportConfig())
    # Don't connect — child is not connected, not online, not sendable
    child = pool._select_child()
    assert child is None


async def test_select_child_prefers_online_over_stale() -> None:
    """Online children are preferred over stale children."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(
        proto, [t0, t1], config=TransportConfig(), health_timeout=60.0
    )
    _connect_and_ready(pool, 0, t0)
    _connect_and_ready(pool, 1, t1)

    # Manually mark child 0 as stale (simulate no packets for timeout)
    pool._children[0].mark_stale()
    assert pool._children[0].availability is NodeAvailability.STALE
    assert pool._children[1].availability is NodeAvailability.ONLINE

    # Online child should be selected
    child = pool._select_child()
    assert child is not None
    assert child.child_id == 1


# -- TX success marks child online -------------------------------------------


async def test_write_routed_marks_child_online_on_success() -> None:
    """A successful TX marks the child online (even without echo)."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), health_timeout=0.1
    )
    _connect_and_ready(pool, 0, t0)

    # Let the child go stale
    await asyncio.sleep(0.15)
    pool._check_health()
    assert pool._children[0].availability is NodeAvailability.STALE

    # Successful TX should mark the child online
    await pool.write_frame("I --- 01:123456 18:000730 --:------ 30C9 001 00")
    availability: NodeAvailability = pool._children[0].availability
    assert availability is NodeAvailability.ONLINE


async def test_write_routed_marks_child_online_on_send_frame() -> None:
    """send_frame path also marks the child online on success."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    # Remove write_frame so send_frame is used
    del t0.write_frame
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), health_timeout=0.1
    )
    _connect_and_ready(pool, 0, t0)

    await asyncio.sleep(0.15)
    pool._check_health()
    assert pool._children[0].availability is NodeAvailability.STALE

    await pool.write_frame("I --- 01:123456 18:000730 --:------ 30C9 001 00")
    availability: NodeAvailability = pool._children[0].availability
    assert availability is NodeAvailability.ONLINE


async def test_write_frame_legacy_marks_child_online() -> None:
    """Legacy write_frame path also marks the child online on success."""
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    pool = PooledTransport(
        proto, [t0], config=TransportConfig(), health_timeout=0.1
    )
    _connect_and_ready(pool, 0, t0)

    await asyncio.sleep(0.15)
    pool._check_health()
    assert pool._children[0].availability is NodeAvailability.STALE

    # Use the legacy write_frame path directly
    await pool.write_frame("I --- 01:123456 18:000730 --:------ 30C9 001 00")
    availability: NodeAvailability = pool._children[0].availability
    assert availability is NodeAvailability.ONLINE


async def test_write_routed_callback_driven_marks_child_online() -> None:
    """A successful callback-driven publish marks the child online."""
    from ramses_tx.routing import RoutedCommand, WriteOutcome

    proto = _make_mock_protocol()
    pool = PooledTransport(
        proto, [None], config=TransportConfig(), health_timeout=0.1
    )
    # Make child 0 callback-driven with a known HGI
    pool._children[0].callback_driven = True
    pool._children[0].hgi_id = DeviceIdT("18:001111")
    pool._children[0].connection_state = ConnectionState.CONNECTED
    pool._children[0].accepted = True
    pool._children[0].send_ready = True
    pool._children[0].mark_online()  # Start online

    # Set up a mock outbound publisher
    publisher = MagicMock()
    publisher.publish_frame = AsyncMock()
    pool.set_outbound_publisher(publisher)

    # Let the child go stale
    await asyncio.sleep(0.15)
    pool._check_health()
    assert pool._children[0].availability is NodeAvailability.STALE

    # Successful publish should mark the child online
    routed = RoutedCommand(child_id="0", command=MagicMock())
    outcome = await pool.write_routed(
        routed, "I --- 01:123456 18:000730 --:------ 30C9 001 00"
    )
    assert outcome is WriteOutcome.SUBMITTED
    availability: NodeAvailability = pool._children[0].availability
    assert availability is NodeAvailability.ONLINE


# -- Cross-child echo detection (issue 1185) -------------------------------


async def test_tx_via_transport_child_echo_via_callback_child() -> None:
    """An over-air copy arriving via another child is marked as echo.

    Reproduces the live failure: a spoofed-source parameter poll was
    transmitted by a serial/zigbee child and heard over the air by a
    remote MQTT-connected HGI.  The MQTT copy reached the pool without
    an echo mark because only MQTT-published TXs were recorded in the
    pool's recent-TX table.
    """
    from ramses_tx.packet import Packet
    from ramses_tx.routing import RoutedCommand, WriteOutcome

    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:254172")  # serial/zigbee child
    pool = PooledTransport(proto, [t0, None], config=TransportConfig())
    _connect_and_ready(pool, 0, t0)
    # Child 1 is callback-driven (remote MQTT-connected HGI).
    pool._children[1].callback_driven = True
    pool._children[1].hgi_id = DeviceIdT("18:130236")
    pool._children[1].accepted = True
    pool._children[1].send_ready = True

    tx_frame = "RQ --- 29:176861 32:153289 --:------ 2411 003 000001"
    routed = RoutedCommand(child_id="0", command=MagicMock())
    outcome = await pool.write_routed(routed, tx_frame)
    assert outcome is WriteOutcome.SUBMITTED
    t0.write_frame.assert_called_once()

    # The remote MQTT HGI hears the frame over the air and forwards it.
    await asyncio.sleep(0.01)  # flush pending forwards, then reset
    proto.packet_received.reset_mock()
    rx_pkt = Packet.from_port(
        dt.now(), "000 RQ --- 29:176861 32:153289 --:------ 2411 003 000001"
    )
    pool._on_child_packet(1, rx_pkt)

    await asyncio.sleep(0.01)
    assert rx_pkt._is_echo is True
    assert proto.packet_received.call_count == 1


async def test_tx_heard_by_multiple_children_all_copies_marked() -> None:
    """Every receiver's copy of one TX is marked as an echo.

    A single transmission is heard by every receiver in the pool;
    consuming the recent-TX match on the first copy would leak the
    remaining copies upstream as genuine traffic.
    """
    from ramses_tx.packet import Packet
    from ramses_tx.routing import RoutedCommand, WriteOutcome

    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:254172")
    t1 = _make_mock_transport(hgi="18:149488")
    pool = PooledTransport(
        proto, [t0, t1, None], config=TransportConfig(), dedup_window=60.0
    )
    _connect_and_ready(pool, 0, t0)
    _connect_and_ready(pool, 1, t1)
    pool._children[2].callback_driven = True
    pool._children[2].hgi_id = DeviceIdT("18:130236")
    pool._children[2].accepted = True
    pool._children[2].send_ready = True

    tx_frame = "RQ --- 29:176861 32:153289 --:------ 2411 003 000001"
    routed = RoutedCommand(child_id="0", command=MagicMock())
    outcome = await pool.write_routed(routed, tx_frame)
    assert outcome is WriteOutcome.SUBMITTED

    # Both the other serial child and the remote MQTT HGI report the
    # same over-air frame.
    await asyncio.sleep(0.01)  # flush pending forwards, then reset
    proto.packet_received.reset_mock()
    rx_line = "000 RQ --- 29:176861 32:153289 --:------ 2411 003 000001"
    rx_pkt1 = Packet.from_port(dt.now(), rx_line)
    rx_pkt2 = Packet.from_port(dt.now(), rx_line)
    pool._on_child_packet(1, rx_pkt1)
    pool._on_child_packet(2, rx_pkt2)

    await asyncio.sleep(0.01)
    assert rx_pkt1._is_echo is True
    assert rx_pkt2._is_echo is True
    # Every copy is marked, but echo copies dedupe among themselves:
    # only the first is forwarded upstream — enough for the protocol's
    # WantEcho FSM — while repeat copies are dropped.
    assert proto.packet_received.call_count == 1


async def test_unrelated_frame_is_not_marked_as_echo() -> None:
    """A packet that does not match a recent TX is not marked echo."""
    from ramses_tx.packet import Packet
    from ramses_tx.routing import RoutedCommand, WriteOutcome

    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:254172")
    pool = PooledTransport(proto, [t0, None], config=TransportConfig())
    _connect_and_ready(pool, 0, t0)
    pool._children[1].callback_driven = True
    pool._children[1].hgi_id = DeviceIdT("18:130236")
    pool._children[1].accepted = True
    pool._children[1].send_ready = True

    tx_frame = "RQ --- 29:176861 32:153289 --:------ 2411 003 000001"
    routed = RoutedCommand(child_id="0", command=MagicMock())
    outcome = await pool.write_routed(routed, tx_frame)
    assert outcome is WriteOutcome.SUBMITTED

    # Different payload — a genuine inbound packet, not our echo.
    await asyncio.sleep(0.01)  # flush pending forwards, then reset
    proto.packet_received.reset_mock()
    rx_pkt = Packet.from_port(
        dt.now(), "000 RQ --- 29:176861 32:153289 --:------ 2411 003 00003E"
    )
    pool._on_child_packet(1, rx_pkt)

    await asyncio.sleep(0.01)
    assert rx_pkt._is_echo is False
    assert proto.packet_received.call_count == 1


async def test_pool_children_accessor() -> None:
    """``children`` exposes an immutable snapshot of the pool registry.

    Backs the ramses_cc zigbee/children iteration that previously read
    the private ``_children`` list.
    """
    proto = _make_mock_protocol()
    t0 = _make_mock_transport(hgi="18:001111")
    t1 = _make_mock_transport(hgi="18:002222")
    pool = PooledTransport(proto, [t0, t1], config=TransportConfig())

    children = pool.children
    assert isinstance(children, tuple)
    assert len(children) == 2
    assert [c.child_id for c in children] == [0, 1]
