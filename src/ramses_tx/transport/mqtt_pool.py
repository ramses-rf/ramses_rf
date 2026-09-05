#!/usr/bin/env python3
"""RAMSES RF - MQTT callback pool adapter.

Bridges the transport-neutral MQTT callback contract
(:mod:`ramses_tx.transport.callbacks`) to a
:class:`~ramses_tx.transport.pooled.PooledTransport`.

The adapter pre-creates logical children from configured HGI IDs
and maps callback availability events into the pool's child
registry without creating a per-child transport or TCP connection.
Outbound frames for callback-driven children are published through
an :class:`~ramses_tx.transport.callbacks.MqttPoolOutbound`
publisher.

This module is HA-import-free.  PR 4B implements the
:class:`~ramses_tx.transport.callbacks.MqttPoolOutbound` and
:class:`~ramses_tx.transport.callbacks.MqttPoolInbound` interfaces
in ``ramses_cc`` using ``homeassistant.components.mqtt``.
"""

from __future__ import annotations

import contextlib
import functools
import logging

from ..helpers import dt_now
from ..packet import Packet
from ..typing import DeviceIdT
from .callbacks import MqttDiscoveryCallback, MqttPoolOutbound
from .pooled import (
    ConnectionState,
    NodeAvailability,
    PoolChild,
    PooledTransport,
)

_LOGGER = logging.getLogger(__name__)


class MqttCallbackPoolAdapter:
    """Bridge MQTT callbacks to a :class:`PooledTransport`.

    Pre-creates logical children from configured HGI IDs and maps
    callback availability events into the pool's child registry.
    Uses an :class:`MqttPoolOutbound` publisher for outbound frames
    so no per-child transport or TCP connection is created.

    This adapter implements the inbound side of the callback
    contract.  The HA-native MQTT bridge (PR 4B) calls these
    methods to deliver events from a shared MQTT connection.

    :param pool: The pooled transport to bridge callbacks into.
    :param configured_hgi_ids: List of configured HGI IDs for
        pre-created logical children.
    :param outbound: The outbound publisher for frame delivery.
    :param discovery_callback: Optional callback for unknown HGIs
        observed on the wildcard topic.
    :param accepted_hgi_ids: Optional set of HGI IDs that are
        accepted pool members (may transmit).  HGIs not in this
        set are receive-only discovery candidates.  If ``None``,
        all configured HGIs are accepted (backward-compatible).
    """

    def __init__(
        self,
        pool: PooledTransport,
        configured_hgi_ids: list[str],
        outbound: MqttPoolOutbound,
        *,
        discovery_callback: MqttDiscoveryCallback | None = None,
        accepted_hgi_ids: set[str] | None = None,
    ) -> None:
        """Initialise the callback pool adapter."""
        self._pool = pool
        self._outbound = outbound
        self._discovery_callback = discovery_callback
        self._hgi_to_child: dict[str, int] = {}

        # Wire the outbound publisher into the pool.
        pool.set_outbound_publisher(outbound)

        # Pre-create children in the pool for each configured HGI.
        # The pool must have enough slots (transports list entries).
        for i, hgi_id in enumerate(configured_hgi_ids):
            if i >= len(pool._children):
                break
            self._hgi_to_child[hgi_id] = i
            child = pool._child_by_id(i)
            child.hgi_id = DeviceIdT(hgi_id)
            child.callback_driven = True
            # Ownerless discovery candidates are receive-only.
            # They can receive packets but cannot be selected for
            # outbound transmission until the user accepts them.
            if accepted_hgi_ids is not None:
                child.accepted = hgi_id in accepted_hgi_ids
            _LOGGER.debug(
                "MqttCallbackPool: pre-created child %d for HGI %s "
                "(accepted=%s)",
                i,
                hgi_id,
                child.accepted,
            )

    def _lookup(self, child_id: str) -> PoolChild | None:
        """Look up a child by configured HGI ID.

        :param child_id: The configured logical child ID (HGI ID).
        :returns: The matching PoolChild, or ``None`` if not found.
        """
        idx = self._hgi_to_child.get(child_id)
        if idx is None:
            return None
        return self._pool._child_by_id(idx)

    # -- Inbound callback contract ---------------------------------------

    def on_child_online(
        self,
        child_id: str,
        *,
        hgi_id: DeviceIdT | None = None,
    ) -> None:
        """Mark a configured child as online.

        Marks the child as connected and online.  If this is the
        first connected child, notifies the real protocol.

        :param child_id: The configured logical child ID (HGI ID).
        :param hgi_id: Optional confirmed HGI identity.
        """
        child = self._lookup(child_id)
        if child is None:
            _LOGGER.warning(
                "MqttCallbackPool: online event for unknown child %s",
                child_id,
            )
            return

        child.connection_state = ConnectionState.CONNECTED
        child.availability = NodeAvailability.ONLINE
        child.send_ready = True
        child.last_pkt_time = dt_now()
        if hgi_id is not None and child.hgi_id is None:
            child.hgi_id = hgi_id

        # Notify the real protocol if this is the first connection.
        # Use call_soon_threadsafe for thread safety — the adapter
        # may be invoked from a non-event-loop MQTT callback thread.
        if not self._pool._protocol_connected:
            self._pool._protocol_connected = True
            with contextlib.suppress(RuntimeError):
                self._pool._loop.call_soon_threadsafe(
                    functools.partial(
                        self._pool._protocol.connection_made,
                        self._pool,
                        ramses=True,
                    )
                )
        # Resolve the connection future if waiting.
        if (
            self._pool._conn_fut is not None
            and not self._pool._conn_fut.done()
        ):
            self._pool._conn_fut.set_result(self._pool)

        _LOGGER.info(
            "MqttCallbackPool: child %s online (HGI=%s), %d/%d connected",
            child_id,
            child.hgi_id,
            len(self._pool._connected_children),
            len(self._pool._children),
        )

    def on_child_offline(
        self,
        child_id: str,
        *,
        definitive: bool = True,
    ) -> None:
        """Mark a child as offline.

        For definitive offline (LWT), marks the child as
        disconnected and quarantines RSSI evidence.  For transient
        broker issues, marks the child as stale.

        :param child_id: The configured logical child ID.
        :param definitive: ``True`` for LWT offline (node is down),
            ``False`` for transient broker issues.
        """
        child = self._lookup(child_id)
        if child is None:
            _LOGGER.warning(
                "MqttCallbackPool: offline event for unknown child %s",
                child_id,
            )
            return

        if definitive:
            # LWT offline is definitive — clear RSSI and mark offline.
            child.mark_disconnected()
        else:
            # Transient broker issue — mark stale but keep connected.
            child.availability = NodeAvailability.STALE

        _LOGGER.info(
            "MqttCallbackPool: child %s offline (definitive=%s)",
            child_id,
            definitive,
        )

        # If no children are connected, notify the real protocol.
        if not self._pool._connected_children and not self._pool._closing:
            self._pool._protocol_connected = False
            with contextlib.suppress(RuntimeError):
                self._pool._loop.call_soon_threadsafe(
                    self._pool._protocol.connection_lost, None
                )

    def on_child_recovering(self, child_id: str) -> None:
        """Signal that a child is recovering after a transient outage.

        :param child_id: The configured logical child ID.
        """
        child = self._lookup(child_id)
        if child is None:
            return
        child.availability = NodeAvailability.STALE
        _LOGGER.info("MqttCallbackPool: child %s recovering", child_id)

    def on_child_identity(
        self,
        child_id: str,
        hgi_id: DeviceIdT,
    ) -> None:
        """Confirm the child's HGI identity.

        :param child_id: The configured logical child ID.
        :param hgi_id: The confirmed HGI device ID.
        """
        child = self._lookup(child_id)
        if child is None:
            return
        child.learn_hgi(hgi_id)
        _LOGGER.info(
            "MqttCallbackPool: child %s identity confirmed as %s",
            child_id,
            hgi_id,
        )

    def on_child_packet(
        self,
        child_id: str,
        packet: Packet,
        *,
        ingress_hgi_id: DeviceIdT | None = None,
    ) -> None:
        """Process a packet received from the child.

        Routes the packet through the pool's dedup and forwarding
        logic.  The ``ingress_hgi_id`` from the MQTT topic is
        passed through without parsing topics inside the router.

        :param child_id: The configured logical child ID.
        :param packet: The parsed packet.
        :param ingress_hgi_id: Optional HGI identity extracted
            from the MQTT topic.
        """
        idx = self._hgi_to_child.get(child_id)
        if idx is None:
            _LOGGER.warning(
                "MqttCallbackPool: packet from unknown child %s",
                child_id,
            )
            return
        self._pool._on_child_packet(idx, packet, ingress_hgi_id=ingress_hgi_id)

    def on_unknown_hgi(
        self,
        hgi_id: DeviceIdT,
        *,
        topic: str | None = None,
    ) -> None:
        """Report an unknown HGI observed on the wildcard topic.

        Does **not** create a ``PoolChild`` — only reports for
        discovery purposes through the registered callback.

        :param hgi_id: The unknown HGI device ID.
        :param topic: The MQTT topic where it was observed.
        """
        if self._discovery_callback is not None:
            self._discovery_callback.on_unknown_hgi(hgi_id, topic=topic)
        else:
            _LOGGER.debug(
                "MqttCallbackPool: unknown HGI %s on topic %s "
                "(no discovery callback registered)",
                hgi_id,
                topic,
            )

    def on_broker_connected(self) -> None:
        """Signal that the MQTT broker connection is established.

        No per-child action — individual children are marked online
        via ``on_child_online`` when their LWT ``online`` messages
        arrive.
        """
        _LOGGER.info("MqttCallbackPool: broker connected")

    def on_broker_disconnected(self) -> None:
        """Signal that the MQTT broker connection is lost.

        Marks all callback-driven children as disconnected and
        non-sendable, since without a broker connection no MQTT
        child can send or receive.  This is distinct from LWT
        offline (which affects a single node) — broker loss
        affects all MQTT children simultaneously.

        If no children remain connected, notifies the real
        protocol via ``connection_lost``.
        """
        _LOGGER.warning("MqttCallbackPool: broker disconnected")
        for child in self._pool._children:
            if child.callback_driven and child.is_connected:
                child.connection_state = ConnectionState.DISCONNECTED
                child.availability = NodeAvailability.STALE
                child.send_ready = False
                # Quarantine RSSI — no valid evidence during outage.
                child.rssi_tracker.clear()

        # If no children are connected, notify the real protocol.
        if not self._pool._connected_children and not self._pool._closing:
            self._pool._protocol_connected = False
            with contextlib.suppress(RuntimeError):
                self._pool._loop.call_soon_threadsafe(
                    self._pool._protocol.connection_lost, None
                )
