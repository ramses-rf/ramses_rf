#!/usr/bin/env python3
"""RAMSES RF - Transport-neutral callback contracts for pool children.

Defines the inbound/outbound callback interfaces that bridge an
external MQTT client (e.g. Home Assistant's shared MQTT connection)
to :class:`~ramses_tx.transport.pooled.PooledTransport` without
creating a per-child transport or TCP connection.

These protocols are HA-import-free.  PR 4B implements them in
``ramses_cc`` using ``homeassistant.components.mqtt``.

Inbound events (``MqttPoolInbound``):

- ``on_child_online`` — LWT online or first packet from a configured
  child.
- ``on_child_offline`` — LWT offline (definitive) or transient broker
  issue (non-definitive).
- ``on_child_recovering`` — child is coming back after a transient
  outage.
- ``on_child_identity`` — HGI identity confirmed (e.g. from puzzle
  response or topic inference).
- ``on_child_packet`` — a parsed packet from the child, carrying
  topic-derived ``ingress_hgi_id``.
- ``on_broker_connected`` / ``on_broker_disconnected`` — broker-level
  state affecting all MQTT children.

Outbound (``MqttPoolOutbound``):

- ``publish_frame`` — publish a serialized frame to a specific
  child's TX topic through the shared MQTT connection.

Discovery (``MqttDiscoveryCallback``):

- ``on_unknown_hgi`` — an unknown HGI was observed on the wildcard
  topic.  Does not create a ``PoolChild``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..packet import Packet
    from ..typing import DeviceIdT


@runtime_checkable
class MqttPoolOutbound(Protocol):
    """Outbound callback for publishing frames to specific children.

    Implemented by the HA-native MQTT bridge (PR 4B).  The pool
    calls this to publish a frame to a specific child's TX topic
    through the shared MQTT connection — no per-child transport
    or TCP connection is created.
    """

    async def publish_frame(self, child_id: str, frame: str) -> None:
        """Publish ``frame`` to the child's TX topic.

        :param child_id: The configured logical child ID (HGI ID).
        :param frame: The serialized RAMSES frame string.
        """


@runtime_checkable
class MqttDiscoveryCallback(Protocol):
    """Discovery callback for unknown HGI IDs.

    Implemented by the coordinator or discovery layer to receive
    notifications about unknown HGIs observed on the wildcard
    topic.  Does **not** create a ``PoolChild``.
    """

    def on_unknown_hgi(
        self,
        hgi_id: DeviceIdT,
        *,
        topic: str | None = None,
    ) -> None:
        """Report an unknown HGI observed on the wildcard topic.

        :param hgi_id: The unknown HGI device ID.
        :param topic: The MQTT topic where it was observed.
        """


@runtime_checkable
class MqttPoolInbound(Protocol):
    """Inbound callback contract for MQTT-driven pool children.

    Implemented by the pool adapter (see
    :class:`~ramses_tx.transport.mqtt_pool.MqttCallbackPoolAdapter`).
    The HA-native MQTT bridge (PR 4B) calls these methods to
    deliver events from a shared MQTT connection.
    """

    def on_child_online(
        self,
        child_id: str,
        *,
        hgi_id: DeviceIdT | None = None,
    ) -> None:
        """Mark a configured child as online.

        Called when an LWT ``online`` message or the first packet
        from the child is received.

        :param child_id: The configured logical child ID (HGI ID).
        :param hgi_id: Optional confirmed HGI identity.
        """

    def on_child_offline(
        self,
        child_id: str,
        *,
        definitive: bool = True,
    ) -> None:
        """Mark a child as offline.

        :param child_id: The configured logical child ID.
        :param definitive: ``True`` for LWT offline (node is down),
            ``False`` for transient broker issues.
        """

    def on_child_recovering(self, child_id: str) -> None:
        """Signal that a child is recovering after a transient outage.

        :param child_id: The configured logical child ID.
        """

    def on_child_identity(
        self,
        child_id: str,
        hgi_id: DeviceIdT,
    ) -> None:
        """Confirm the child's HGI identity.

        :param child_id: The configured logical child ID.
        :param hgi_id: The confirmed HGI device ID.
        """

    def on_child_packet(
        self,
        child_id: str,
        packet: Packet,
        *,
        ingress_hgi_id: DeviceIdT | None = None,
    ) -> None:
        """Process a packet received from the child.

        :param child_id: The configured logical child ID.
        :param packet: The parsed packet.
        :param ingress_hgi_id: Optional HGI identity extracted
            from the MQTT topic.  When provided, overrides the
            child record's HGI for ingress provenance.
        """

    def on_broker_connected(self) -> None:
        """Signal that the MQTT broker connection is established."""

    def on_broker_disconnected(self) -> None:
        """Signal that the MQTT broker connection is lost."""
