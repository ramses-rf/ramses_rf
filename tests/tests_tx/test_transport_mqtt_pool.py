#!/usr/bin/env python3
"""Tests for the transport-neutral MQTT callback contract (PR 4A).

Covers:
- MqttCallbackPoolAdapter pre-creates logical children from
  configured HGI IDs.
- on_child_online / on_child_offline update child availability.
- on_child_packet routes through the pool's dedup logic.
- on_child_identity confirms HGI identity.
- on_unknown_hgi fires the discovery callback without creating a
  PoolChild.
- on_broker_disconnected marks all callback-driven children stale.
- Outbound frames for callback-driven children go through the
  MqttPoolOutbound publisher.
- LWT offline quarantines RSSI evidence.
- Wildcard RX preserves the correct ingress HGI.
- Unknown wildcard IDs do not mutate the child registry.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from ramses_tx.const import I_, Code
from ramses_tx.transport.base import TransportConfig
from ramses_tx.transport.callbacks import (
    MqttDiscoveryCallback,
    MqttPoolInbound,
    MqttPoolOutbound,
)
from ramses_tx.transport.mqtt_pool import MqttCallbackPoolAdapter
from ramses_tx.transport.pooled import (
    ConnectionState,
    NodeAvailability,
    PooledTransport,
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


def _make_mock_protocol() -> MagicMock:
    """Create a mock real protocol for the pool."""
    proto = MagicMock()
    proto.packet_received = Mock()
    proto.connection_lost = Mock()
    proto.connection_made = Mock()
    proto.send_cmd = AsyncMock(return_value=None)
    proto.set_regex_rules = Mock()
    return proto


class _FakeOutbound:
    """Minimal MqttPoolOutbound implementation for tests."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish_frame(self, child_id: str, frame: str) -> None:
        self.published.append((child_id, frame))


class _FakeDiscovery:
    """Minimal MqttDiscoveryCallback implementation for tests."""

    def __init__(self) -> None:
        self.unknowns: list[tuple[str, str | None]] = []

    def on_unknown_hgi(
        self, hgi_id: DeviceIdT, *, topic: str | None = None
    ) -> None:
        self.unknowns.append((str(hgi_id), topic))


def _make_callback_pool(
    event_loop: asyncio.AbstractEventLoop | None = None,
    proto: MagicMock | None = None,
    hgi_ids: list[str] | None = None,
    outbound: _FakeOutbound | None = None,
    discovery: _FakeDiscovery | None = None,
    accepted_hgi_ids: set[str] | None = None,
) -> tuple[PooledTransport, MqttCallbackPoolAdapter, _FakeOutbound]:
    """Create a PooledTransport with a callback adapter."""
    proto = proto or _make_mock_protocol()
    hgi_ids = hgi_ids or ["18:001111", "18:002222"]
    outbound = outbound or _FakeOutbound()
    n = len(hgi_ids)
    # Get the event loop: use the provided one, or try the running
    # loop, or create a new one for sync tests.
    if event_loop is not None:
        loop = event_loop
    else:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    pool = PooledTransport(
        proto,
        [None] * n,  # no per-child transports
        config=TransportConfig(),
        loop=loop,
    )
    adapter = MqttCallbackPoolAdapter(
        pool,
        hgi_ids,
        outbound,
        discovery_callback=discovery,
        accepted_hgi_ids=accepted_hgi_ids,
    )
    return pool, adapter, outbound


# -- Protocol compliance ---------------------------------------------------


def test_mqtt_pool_outbound_is_runtime_checkable() -> None:
    """MqttPoolOutbound is a runtime_checkable Protocol."""
    pub = _FakeOutbound()
    assert isinstance(pub, MqttPoolOutbound)


def test_mqtt_pool_inbound_is_runtime_checkable() -> None:
    """MqttPoolInbound is a runtime_checkable Protocol."""
    _, adapter, _ = _make_callback_pool()
    assert isinstance(adapter, MqttPoolInbound)


def test_discovery_callback_is_runtime_checkable() -> None:
    """MqttDiscoveryCallback is a runtime_checkable Protocol."""
    disc = _FakeDiscovery()
    assert isinstance(disc, MqttDiscoveryCallback)


# -- Pre-creation of logical children --------------------------------------


def test_pre_creates_children_from_configured_hgi_ids() -> None:
    """Children are pre-created with HGI IDs and callback_driven=True."""
    pool, _, _ = _make_callback_pool(hgi_ids=["18:001111", "18:002222"])
    assert len(pool._children) == 2
    assert pool._children[0].hgi_id == DeviceIdT("18:001111")
    assert pool._children[1].hgi_id == DeviceIdT("18:002222")
    assert pool._children[0].callback_driven is True
    assert pool._children[1].callback_driven is True
    assert pool._children[0].transport is None
    assert pool._children[1].transport is None


def test_outbound_publisher_is_wired_into_pool() -> None:
    """The outbound publisher is set on the pool."""
    pool, _, outbound = _make_callback_pool()
    assert pool._outbound_publisher is outbound


# -- on_child_online / on_child_offline ------------------------------------


def test_on_child_online_marks_child_connected_and_sendable() -> None:
    """on_child_online marks the child as connected and send-ready."""
    pool, adapter, _ = _make_callback_pool()
    adapter.on_child_online("18:001111")
    child = pool._child_by_id(0)
    assert child.is_connected
    assert child.availability is NodeAvailability.ONLINE
    assert child.send_ready is True


async def test_on_child_online_notifies_protocol_on_first_connection() -> None:
    """First child online triggers protocol.connection_made."""
    proto = _make_mock_protocol()
    _, adapter, _ = _make_callback_pool(proto=proto)
    adapter.on_child_online("18:001111")
    # connection_made is dispatched via call_soon_threadsafe.
    await asyncio.sleep(0.01)
    assert proto.connection_made.called


async def test_on_child_online_does_not_renotify_protocol() -> None:
    """Second child online does not re-trigger protocol.connection_made."""
    proto = _make_mock_protocol()
    _, adapter, _ = _make_callback_pool(proto=proto)
    adapter.on_child_online("18:001111")
    await asyncio.sleep(0.01)  # drain call_soon_threadsafe
    proto.connection_made.reset_mock()
    adapter.on_child_online("18:002222")
    await asyncio.sleep(0.01)
    assert not proto.connection_made.called


def test_on_child_offline_definitive_marks_disconnected() -> None:
    """Definitive offline marks the child as disconnected."""
    pool, adapter, _ = _make_callback_pool()
    adapter.on_child_online("18:001111")
    adapter.on_child_offline("18:001111", definitive=True)
    child = pool._child_by_id(0)
    assert not child.is_connected
    assert child.availability is NodeAvailability.OFFLINE
    assert child.send_ready is False


def test_on_child_offline_definitive_clears_rssi() -> None:
    """LWT offline quarantines RSSI evidence."""
    pool, adapter, _ = _make_callback_pool()
    adapter.on_child_online("18:001111")
    # Record some RSSI.
    pool._on_child_packet(0, _make_packet(src="01:123456", rssi="050"))
    assert pool._children[0].rssi_tracker._readings  # has data
    adapter.on_child_offline("18:001111", definitive=True)
    assert not pool._children[0].rssi_tracker._readings  # cleared


def test_on_child_offline_transient_marks_stale() -> None:
    """Transient broker issue marks the child as stale, not offline."""
    pool, adapter, _ = _make_callback_pool()
    adapter.on_child_online("18:001111")
    adapter.on_child_offline("18:001111", definitive=False)
    child = pool._child_by_id(0)
    assert child.is_connected  # still connected
    assert child.availability is NodeAvailability.STALE


async def test_on_child_offline_all_children_notifies_protocol() -> None:
    """When all children go offline, protocol.connection_lost is called."""
    proto = _make_mock_protocol()
    pool, adapter, _ = _make_callback_pool(proto=proto)
    adapter.on_child_online("18:001111")
    adapter.on_child_offline("18:001111", definitive=True)
    # Let call_soon_threadsafe drain.
    await asyncio.sleep(0.01)
    assert proto.connection_lost.called


def test_on_child_offline_unknown_child_is_warning() -> None:
    """Offline event for unknown child does not crash."""
    _, adapter, _ = _make_callback_pool()
    adapter.on_child_offline("18:999999")  # not configured
    # No crash — just a warning log.


# -- on_child_packet -------------------------------------------------------


async def test_on_child_packet_routes_through_dedup() -> None:
    """Packets from callback children go through the pool's dedup."""
    proto = _make_mock_protocol()
    _, adapter, _ = _make_callback_pool(proto=proto)
    adapter.on_child_online("18:001111")
    pkt = _make_packet()
    adapter.on_child_packet("18:001111", pkt)
    await asyncio.sleep(0.01)
    assert proto.packet_received.call_count == 1


async def test_on_child_packet_carries_ingress_hgi_id() -> None:
    """Topic-derived ingress_hgi_id is passed through to the packet."""
    pool, adapter, _ = _make_callback_pool()
    adapter.on_child_online("18:001111")
    pkt = _make_packet()
    adapter.on_child_packet(
        "18:001111", pkt, ingress_hgi_id=DeviceIdT("18:001111")
    )
    assert pkt._ingress_hgi_id == "18:001111"


async def test_on_child_packet_dedup_across_children() -> None:
    """Same packet from two callback children is deduped."""
    proto = _make_mock_protocol()
    _, adapter, _ = _make_callback_pool(
        proto=proto, hgi_ids=["18:001111", "18:002222"]
    )
    adapter.on_child_online("18:001111")
    adapter.on_child_online("18:002222")
    pkt = _make_packet()
    adapter.on_child_packet("18:001111", pkt)
    adapter.on_child_packet("18:002222", pkt)  # duplicate
    await asyncio.sleep(0.01)
    assert proto.packet_received.call_count == 1


def test_on_child_packet_unknown_child_is_warning() -> None:
    """Packet from unknown child does not crash."""
    _, adapter, _ = _make_callback_pool()
    pkt = _make_packet()
    adapter.on_child_packet("18:999999", pkt)  # not configured


# -- on_child_identity -----------------------------------------------------


def test_on_child_identity_confirms_hgi() -> None:
    """on_child_identity sets the HGI ID on the child."""
    pool, adapter, _ = _make_callback_pool(hgi_ids=["18:001111"])
    # Clear the pre-set HGI to simulate unknown identity.
    pool._children[0].hgi_id = None
    pool._children[0].send_ready = False
    adapter.on_child_identity("18:001111", DeviceIdT("18:001111"))
    child = pool._child_by_id(0)
    assert child.hgi_id == DeviceIdT("18:001111")
    assert child.send_ready is True


# -- on_unknown_hgi --------------------------------------------------------


def test_on_unknown_hgi_fires_discovery_callback() -> None:
    """Unknown HGI fires the discovery callback."""
    discovery = _FakeDiscovery()
    _, adapter, _ = _make_callback_pool(discovery=discovery)
    adapter.on_unknown_hgi(
        DeviceIdT("18:999999"), topic="RAMSES/GATEWAY/18:999999/rx"
    )
    assert len(discovery.unknowns) == 1
    assert discovery.unknowns[0] == (
        "18:999999",
        "RAMSES/GATEWAY/18:999999/rx",
    )


def test_on_unknown_hgi_no_discovery_callback_is_safe() -> None:
    """Unknown HGI without a discovery callback does not crash."""
    _, adapter, _ = _make_callback_pool(discovery=None)
    adapter.on_unknown_hgi(DeviceIdT("18:999999"))


def test_on_unknown_hgi_does_not_create_child() -> None:
    """Unknown HGI does not create a PoolChild."""
    pool, adapter, _ = _make_callback_pool()
    initial_count = len(pool._children)
    adapter.on_unknown_hgi(DeviceIdT("18:999999"))
    assert len(pool._children) == initial_count


# -- on_broker_disconnected ------------------------------------------------


def test_on_broker_disconnected_marks_children_disconnected() -> None:
    """Broker disconnect marks all callback-driven children as
    disconnected and non-sendable."""
    pool, adapter, _ = _make_callback_pool(hgi_ids=["18:001111", "18:002222"])
    adapter.on_child_online("18:001111")
    adapter.on_child_online("18:002222")
    adapter.on_broker_disconnected()
    for child in pool._children:
        if child.callback_driven:
            assert child.availability is NodeAvailability.STALE
            assert not child.is_connected
            assert not child.is_sendable
            # RSSI quarantined during broker outage.
            assert not child.rssi_tracker._readings


def test_on_broker_connected_is_safe() -> None:
    """Broker connected event does not crash."""
    _, adapter, _ = _make_callback_pool()
    adapter.on_broker_connected()


# -- Outbound routing for callback-driven children -------------------------


async def test_write_routed_uses_outbound_publisher() -> None:
    """write_routed for a callback-driven child uses the publisher."""
    pool, adapter, outbound = _make_callback_pool()
    adapter.on_child_online("18:001111")
    # Manually call write_routed with child_id=0.
    from ramses_tx.routing import RoutedCommand

    routed = RoutedCommand(child_id="0", command=MagicMock())
    outcome = await pool.write_routed(
        routed, " 000 I --- 01:123456 18:000730 --:------ 30C9 000 00"
    )
    assert outcome.name == "SUBMITTED"
    assert len(outbound.published) == 1
    assert outbound.published[0][0] == "18:001111"


async def test_write_routed_no_outbound_returns_not_submitted() -> None:
    """write_routed without an outbound publisher returns NOT_SUBMITTED."""
    pool = PooledTransport(
        _make_mock_protocol(),
        [None],
        config=TransportConfig(),
    )
    pool._children[0].callback_driven = True
    pool._children[0].hgi_id = DeviceIdT("18:001111")
    pool._children[0].connection_state = ConnectionState.CONNECTED
    pool._children[0].send_ready = True
    from ramses_tx.routing import RoutedCommand

    routed = RoutedCommand(child_id="0", command=MagicMock())
    outcome = await pool.write_routed(routed, "frame")
    assert outcome.name == "NOT_SUBMITTED"


async def test_write_routed_publisher_failure_returns_ambiguous() -> None:
    """Publisher failure returns AMBIGUOUS."""
    pool, adapter, _ = _make_callback_pool()
    adapter.on_child_online("18:001111")
    # Replace publisher with one that raises.
    pool._outbound_publisher = MagicMock()
    pool._outbound_publisher.publish_frame = AsyncMock(
        side_effect=RuntimeError("broker down")
    )
    from ramses_tx.routing import RoutedCommand

    routed = RoutedCommand(child_id="0", command=MagicMock())
    outcome = await pool.write_routed(routed, "frame")
    assert outcome.name == "AMBIGUOUS"


# -- is_sendable for callback-driven children ------------------------------


def test_is_sendable_callback_driven_no_transport() -> None:
    """A callback-driven child with no transport can be sendable."""
    pool, adapter, _ = _make_callback_pool()
    adapter.on_child_online("18:001111")
    child = pool._child_by_id(0)
    assert child.transport is None
    assert child.is_sendable


def test_is_sendable_not_sendable_when_offline() -> None:
    """A callback-driven child is not sendable when offline."""
    pool, adapter, _ = _make_callback_pool()
    adapter.on_child_online("18:001111")
    adapter.on_child_offline("18:001111", definitive=True)
    child = pool._child_by_id(0)
    assert not child.is_sendable


# -- _child_by_hgi_id lookup -----------------------------------------------


def test_child_by_hgi_id_finds_matching_child() -> None:
    """_child_by_hgi_id returns the child with the given HGI ID."""
    pool, _, _ = _make_callback_pool(hgi_ids=["18:001111", "18:002222"])
    child = pool._child_by_hgi_id("18:002222")
    assert child is not None
    assert child.child_id == 1


def test_child_by_hgi_id_returns_none_for_unknown() -> None:
    """_child_by_hgi_id returns None for an unknown HGI ID."""
    pool, _, _ = _make_callback_pool()
    child = pool._child_by_hgi_id("18:999999")
    assert child is None


# -- Unknown HGI side effects (fact-check gap) -----------------------------


def test_unknown_hgi_does_not_forward_rf_frames() -> None:
    """Unknown HGI on wildcard does not forward RF frames."""
    proto = _make_mock_protocol()
    _, adapter, _ = _make_callback_pool(proto=proto)
    adapter.on_child_online("18:001111")
    # Simulate a packet from an unknown HGI on the wildcard topic.
    # The adapter's on_unknown_hgi is called instead of on_child_packet.
    adapter.on_unknown_hgi(DeviceIdT("18:999999"))
    # No packet was forwarded because on_unknown_hgi does not route
    # packets — it only fires the discovery callback.
    assert proto.packet_received.call_count == 0


def test_unknown_hgi_does_not_mutate_registry() -> None:
    """Unknown HGI does not add a child to the registry."""
    pool, adapter, _ = _make_callback_pool()
    initial_count = len(pool._children)
    adapter.on_unknown_hgi(DeviceIdT("18:999999"))
    assert len(pool._children) == initial_count
    # No child has HGI 18:999999.
    assert pool._child_by_hgi_id("18:999999") is None


def test_unknown_hgi_does_not_become_sendable() -> None:
    """Unknown HGI does not appear as a sendable child."""
    pool, adapter, _ = _make_callback_pool()
    adapter.on_unknown_hgi(DeviceIdT("18:999999"))
    for child in pool._children:
        assert child.hgi_id != DeviceIdT("18:999999")
        assert not child.is_sendable or child.hgi_id != DeviceIdT("18:999999")


# -- LWT vs broker distinction (fact-check gap) ---------------------------


def test_lwt_offline_affects_only_target_child() -> None:
    """LWT offline removes only the affected ESP from eligibility;
    other children remain sendable."""
    pool, adapter, _ = _make_callback_pool(hgi_ids=["18:001111", "18:002222"])
    adapter.on_child_online("18:001111")
    adapter.on_child_online("18:002222")
    # LWT offline on child 0 only.
    adapter.on_child_offline("18:001111", definitive=True)
    child0 = pool._child_by_id(0)
    child1 = pool._child_by_id(1)
    assert not child0.is_sendable
    assert not child0.is_connected
    assert child1.is_sendable
    assert child1.is_connected


def test_broker_loss_affects_all_mqtt_children() -> None:
    """Broker loss affects all MQTT children, not just one."""
    pool, adapter, _ = _make_callback_pool(hgi_ids=["18:001111", "18:002222"])
    adapter.on_child_online("18:001111")
    adapter.on_child_online("18:002222")
    adapter.on_broker_disconnected()
    for child in pool._children:
        if child.callback_driven:
            assert not child.is_sendable
            assert not child.is_connected


# -- write_routed is_sendable guard (fact-check fix) ----------------------


async def test_write_routed_offline_child_not_submitted() -> None:
    """write_routed for an offline callback-driven child returns
    NOT_SUBMITTED (is_sendable guard)."""
    pool, adapter, outbound = _make_callback_pool()
    adapter.on_child_online("18:001111")
    adapter.on_child_offline("18:001111", definitive=True)
    from ramses_tx.routing import RoutedCommand

    routed = RoutedCommand(child_id="0", command=MagicMock())
    outcome = await pool.write_routed(routed, "frame")
    assert outcome.name == "NOT_SUBMITTED"
    assert len(outbound.published) == 0


async def test_write_routed_broker_down_returns_not_submitted() -> None:
    """write_routed for a callback-driven child after broker
    disconnect returns NOT_SUBMITTED."""
    pool, adapter, outbound = _make_callback_pool()
    adapter.on_child_online("18:001111")
    adapter.on_broker_disconnected()
    from ramses_tx.routing import RoutedCommand

    routed = RoutedCommand(child_id="0", command=MagicMock())
    outcome = await pool.write_routed(routed, "frame")
    assert outcome.name == "NOT_SUBMITTED"
    assert len(outbound.published) == 0


# -- accepted_hgi_ids: ownerless discovery candidates are receive-only ------


def test_accepted_hgi_ids_marks_ownerless_as_not_accepted(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """Ownerless discovery candidates have accepted=False."""
    pool, adapter, _ = _make_callback_pool(
        event_loop=event_loop,
        hgi_ids=["18:001111", "18:002222"],
        accepted_hgi_ids={"18:001111"},  # only 18:001111 is accepted
    )
    child0 = pool._child_by_id(0)
    child1 = pool._child_by_id(1)
    assert child0.accepted is True  # accepted
    assert child1.accepted is False  # ownerless discovery candidate


def test_ownerless_candidate_not_sendable_even_when_online(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """An ownerless candidate that is online is NOT sendable."""
    pool, adapter, _ = _make_callback_pool(
        event_loop=event_loop,
        hgi_ids=["18:001111", "18:002222"],
        accepted_hgi_ids={"18:001111"},
    )
    # Bring the ownerless candidate online.
    adapter.on_child_online("18:002222")
    child1 = pool._child_by_id(1)
    assert child1.is_connected
    assert child1.send_ready
    assert not child1.accepted
    assert not child1.is_sendable  # accepted=False blocks sendable


def test_ownerless_candidate_not_selected_for_outbound(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """An ownerless candidate is not selected for outbound routing."""
    pool, adapter, outbound = _make_callback_pool(
        event_loop=event_loop,
        hgi_ids=["18:001111", "18:002222"],
        accepted_hgi_ids={"18:001111"},
    )
    # Bring both children online.
    adapter.on_child_online("18:001111")
    adapter.on_child_online("18:002222")
    # _select_child should only return accepted children.
    selected = pool._select_child("04:123456")
    assert selected is not None
    assert selected.child_id == 0  # only the accepted child


async def test_ownerless_candidate_cannot_transmit(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """write_routed for an ownerless candidate returns NOT_SUBMITTED."""
    pool, adapter, outbound = _make_callback_pool(
        event_loop=event_loop,
        hgi_ids=["18:001111", "18:002222"],
        accepted_hgi_ids={"18:001111"},
    )
    adapter.on_child_online("18:002222")  # ownerless candidate online
    from ramses_tx.routing import RoutedCommand

    routed = RoutedCommand(child_id="1", command=MagicMock())
    outcome = await pool.write_routed(routed, "frame")
    assert outcome.name == "NOT_SUBMITTED"
    assert len(outbound.published) == 0


def test_no_accepted_hgi_ids_means_all_accepted(
    event_loop: asyncio.AbstractEventLoop,
) -> None:
    """When accepted_hgi_ids is None, all children are accepted (backward compat)."""
    pool, _, _ = _make_callback_pool(
        event_loop=event_loop,
        hgi_ids=["18:001111", "18:002222"],
        accepted_hgi_ids=None,
    )
    assert pool._child_by_id(0).accepted is True
    assert pool._child_by_id(1).accepted is True
