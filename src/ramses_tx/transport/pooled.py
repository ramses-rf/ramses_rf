#!/usr/bin/env python3
"""RAMSES RF - Pooled transport for multi-HGI link-layer pooling.

Combines multiple physical transports (serial, MQTT, ser2net) into a
single coherent :class:`TransportInterface` that the protocol layer
sees as one transport.  Inbound packets from any child are
deduplicated within a sliding time window and forwarded upstream.
Outbound frames are routed to the child transport with the best
rolling-average RSSI, falling back to round-robin when no RSSI data
is available yet.  Unhealthy children are detected via a configurable
health timeout and excluded from outbound selection until they
recover.

This is Roadmap Item 9, PR 1 (issue 1119).

PR 1 rework: replaces parallel mutable arrays with an encapsulated
:class:`PoolChild` record per route, adds ingress provenance
(``ingress_hgi_id`` separate from RAMSES ``addr1``), loopback
exclusion from route RSSI, a dict-backed O(1) dedup cache with
sequence-aware keys, and RSSI TTL expiry.  Runtime
``add_child()``/``remove_child()``/``set_accepted_hgis()`` are
removed — construction and config-entry reload build immutable child
registries for the first release.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from dataclasses import dataclass, field
from datetime import datetime as dt, timedelta as td
from enum import Enum, auto
from typing import Any, TypeAlias

from .. import exceptions as exc
from ..address import HGI_DEV_ADDR
from ..const import SZ_ACTIVE_HGI, SZ_IS_EVOFW3, Code
from ..helpers import dt_now
from ..interfaces import ProtocolInterface, TransportInterface
from ..packet import Packet
from ..routing import RoutedCommand, RouteRequest, SourcePolicy, WriteOutcome
from ..rssi_tracker import POOL_TTL, POOL_WINDOW_SIZE, RssiTracker
from ..typing import DeviceIdT, RamsesProtocolT
from .base import TransportConfig
from .callbacks import MqttPoolOutbound

_LOGGER = logging.getLogger(__name__)

#: Default deduplication window in seconds.  Two packets with the same
#: content key arriving within this window from different child
#: transports are considered duplicates.
_DEFAULT_DEDUP_WINDOW: float = 0.5

#: Maximum number of dedup keys retained in the cache.
_MAX_DEDUP_KEYS: int = 512

#: RSSI value used when a child has no data yet (treated as neutral).
_RSSI_UNKNOWN: int = -999  # Sentinel: no RSSI data (worse than any real dBm)

#: Default health-check interval in seconds.  A child that has not
#: received any packets for this duration is marked unhealthy.
#: Set to 180s (3 min) because RAMSES traffic can be sparse — some
#: devices poll every 2-3 minutes, and a 60s timeout caused false
#: unhealthy markings during quiet periods (observed in pool testing
#: with real MQTT HGIs, issue 1119).
_DEFAULT_HEALTH_TIMEOUT: float = 180.0

#: Number of consecutive errors before a child is marked unhealthy.
_DEFAULT_MAX_CONSECUTIVE_ERRORS: int = 5

#: Dedup key: (verb, code, src, dst, addr3, seq, raw_payload).
#: The sequence field is included when present (not ``---``) because
#: it is sender-assigned and stable across HGIs (confirmed from
#: captured fixtures: 50/50 paired packets had identical sequence).
#: When sequence is absent, the fallback base key is
#: (verb, code, src, dst, addr3, "", raw_payload).
_DedupKeyT: TypeAlias = tuple[str, str, str, str, str, str, str]


class ConnectionState(Enum):
    """Connection state of a pool child transport.

    Separates the transport-layer connection (TCP/serial link up)
    from node availability (RF packets flowing).
    """

    DISCONNECTED = auto()
    CONNECTED = auto()


class NodeAvailability(Enum):
    """Node availability for a pool child.

    ``ONLINE``: recently received packets, healthy.
    ``STALE``: connected but no packets within health_timeout.
    ``OFFLINE``: explicitly disconnected or too many errors.
    """

    ONLINE = auto()
    STALE = auto()
    OFFLINE = auto()


@dataclass
class PoolChild:
    """Encapsulates all per-child state for one pool route.

    Replaces the parallel mutable arrays (``_child_connected``,
    ``_child_hgi``, ``_child_transport_objs``, etc.) with a single
    coherent record.  Each child owns its transport, connection state,
    HGI identity, RSSI tracker, health tracking, and counters.

    :param child_id: Stable identifier (construction order index).
    :param port_name: Transport address (serial path, MQTT URL).
    :param transport: The child transport, or ``None`` if construction
        failed or the child was removed.
    :param accepted: Whether this child's HGI is in the accepted set.
    :param callback_driven: If True, this child is driven by MQTT
        callbacks (PR 4A) rather than a per-child transport.  No
        ``transport`` instance is created; outbound frames go
        through the pool's outbound publisher.
    """

    child_id: int
    port_name: str
    transport: TransportInterface | None = None
    transport_obj: Any = None
    connection_state: ConnectionState = ConnectionState.DISCONNECTED
    availability: NodeAvailability = NodeAvailability.OFFLINE
    hgi_id: DeviceIdT | None = None
    accepted: bool = True
    callback_driven: bool = False
    rssi_tracker: RssiTracker = field(
        default_factory=lambda: RssiTracker(POOL_WINDOW_SIZE, ttl=POOL_TTL)
    )
    last_pkt_time: dt | None = None
    consecutive_errors: int = 0
    pkts_received: int = 0
    send_ready: bool = False

    @property
    def is_connected(self) -> bool:
        """Return True if the transport link is connected."""
        return self.connection_state is ConnectionState.CONNECTED

    @property
    def is_online(self) -> bool:
        """Return True if the node is online (packets flowing)."""
        return self.availability is NodeAvailability.ONLINE

    @property
    def is_sendable(self) -> bool:
        """Return True if this child can be selected for outbound.

        A child is sendable when it is connected, accepted, and
        send-ready (has identity or has received at least one packet).
        Callback-driven children (PR 4A) do not require a transport
        instance — outbound frames go through the pool's outbound
        publisher.
        """
        return (
            self.is_connected
            and self.accepted
            and self.send_ready
            and (self.transport is not None or self.callback_driven)
        )

    def mark_connected(self, transport_obj: Any) -> None:
        """Mark the child as connected and capture transport metadata.

        :param transport_obj: The connected transport object.
        """
        self.connection_state = ConnectionState.CONNECTED
        self.transport_obj = transport_obj
        # Read HGI identity from the transport if available.
        hgi = transport_obj.get_extra_info(SZ_ACTIVE_HGI)
        if hgi is not None:
            self.hgi_id = DeviceIdT(hgi)
        # A connected child with known HGI is send-ready.
        if self.hgi_id is not None:
            self.send_ready = True

    def mark_disconnected(self) -> None:
        """Mark the child as disconnected and reset state."""
        self.connection_state = ConnectionState.DISCONNECTED
        self.availability = NodeAvailability.OFFLINE
        self.transport_obj = None
        self.send_ready = False
        # Quarantine RSSI evidence when offline (issue 1119).
        self.rssi_tracker.clear()
        self.last_pkt_time = None

    def mark_online(self) -> None:
        """Mark the child as online (packet received)."""
        self.availability = NodeAvailability.ONLINE
        self.last_pkt_time = dt_now()
        self.consecutive_errors = 0
        # Receiving a packet with a known HGI makes us send-ready.
        if self.hgi_id is not None:
            self.send_ready = True

    def mark_stale(self) -> None:
        """Mark the child as stale (no packets within health timeout)."""
        if self.availability is NodeAvailability.ONLINE:
            self.availability = NodeAvailability.STALE

    def record_error(self) -> None:
        """Record a consecutive error (disconnection)."""
        self.consecutive_errors += 1

    def learn_hgi(self, hgi_id: DeviceIdT) -> None:
        """Learn the child's HGI identity from a puzzle response.

        :param hgi_id: The HGI device ID learned from the packet.
        """
        if self.hgi_id is None:
            self.hgi_id = hgi_id
            self.send_ready = True
            _LOGGER.info(
                "PooledTransport: child %d HGI learned as %s "
                "from puzzle response",
                self.child_id,
                hgi_id,
            )


@dataclass(frozen=True, slots=True)
class IngressFrame:
    """Immutable metadata for an inbound frame at the callback boundary.

    Carries ``ingress_hgi_id`` separately from RAMSES ``addr1``
    because ``addr1`` identifies the RF transmitter, not the
    receiving HGI.  Serial/Zigbee child proxies resolve the
    configured/validated child identity and carry it explicitly;
    wildcard MQTT callbacks extract it from the topic.

    :param frame: The raw ASCII frame string.
    :param timestamp: Optional timestamp string from the transport.
    :param ingress_hgi_id: The HGI/child that heard the frame.
    """

    frame: str
    timestamp: str | None
    ingress_hgi_id: DeviceIdT | None


class _ChildProtocolProxy(ProtocolInterface):
    """Protocol proxy inserted between a child transport and the pool.

    Each child transport is created with this proxy as its protocol.
    The proxy intercepts ``packet_received`` and routes the packet to
    the :class:`PooledTransport` for deduplication and upstream
    forwarding.  Connection lifecycle events are also forwarded so the
    pool can track which children are alive.

    :param pool: The owning pooled transport.
    :type pool: PooledTransport
    :param child_id: The child's stable ID in the pool.
    :type child_id: int
    """

    def __init__(self, pool: PooledTransport, child_id: int) -> None:
        """Initialise the child protocol proxy."""
        self._pool = pool
        self._child_id = child_id
        self._connected: bool = False
        self._conn_event: asyncio.Event = asyncio.Event()

    # -- ProtocolInterface ----------------------------------------------

    def connection_made(
        self, transport: Any, /, *, ramses: bool = False
    ) -> None:
        """Forward connection_made to the pool for tracking."""
        self._connected = True
        self._conn_event.set()
        self._pool._on_child_connected(self._child_id, transport)

    def connection_lost(self, error: Exception | None) -> None:
        """Forward connection_lost to the pool for tracking."""
        self._connected = False
        self._conn_event.clear()
        self._pool._on_child_disconnected(self._child_id, error)

    def packet_received(
        self, packet: Packet, *, ingress_hgi_id: DeviceIdT | None = None
    ) -> None:
        """Route the packet to the pool for dedup + upstream forward.

        :param packet: The parsed packet from the child transport.
        :param ingress_hgi_id: Optional HGI identity from the callback
            boundary (e.g. extracted from an MQTT topic).  When
            ``None``, the pool resolves it from the child record.
        """
        self._pool._on_child_packet(
            self._child_id, packet, ingress_hgi_id=ingress_hgi_id
        )

    def pause_writing(self) -> None:
        """No-op — flow control is handled per-child."""

    def resume_writing(self) -> None:
        """No-op — flow control is handled per-child."""

    async def send_cmd(
        self,
        command: Any,
        /,
        *,
        qos: Any = None,
    ) -> Packet | None:
        """Not used by transports — delegated to the real protocol."""
        return await self._pool._protocol.send_cmd(command, qos=qos)

    async def wait_for_connection_made(
        self, timeout: float = 1.0
    ) -> TransportInterface:
        """Wait until **this** child connects (not any child)."""
        try:
            await asyncio.wait_for(self._conn_event.wait(), timeout=timeout)
        except TimeoutError as err:
            raise exc.TransportError(
                f"Child transport {self._child_id} did not connect "
                f"within {timeout}s"
            ) from err
        # Return the pool — callers just need a TransportInterface.
        return self._pool

    def set_regex_rules(self, rules: Any) -> None:
        """No-op — regex rules are set on the real protocol by the factory."""


class PooledTransport(TransportInterface):
    """Aggregate multiple physical transports into one interface.

    The pool presents a single :class:`TransportInterface` to the
    protocol/engine layer.  Internally it manages N child transports,
    each created with a :class:`_ChildProtocolProxy` that routes
    inbound packets through the pool's deduplication filter before
    forwarding them to the real protocol.

    Outbound frames are routed to the connected child with the best
    rolling-average RSSI (5-sample window with TTL expiry).  When no
    RSSI data is available for any child, selection falls back to
    round-robin.  Unhealthy children (no packets for
    ``health_timeout`` seconds, or exceeding
    ``max_consecutive_errors``) are excluded from selection.

    Children are immutable after construction — runtime
    ``add_child()``/``remove_child()`` are deferred until a
    concurrency-safe synchronization model is designed (issue 1119).

    :param protocol: The real protocol that receives deduplicated
        packets.
    :type protocol: RamsesProtocolT
    :param transports: List of child transports (or ``None`` placeholders
        for failed children).
    :type transports: list[TransportInterface | None]
    :param config: Transport configuration shared by all children.
    :type config: TransportConfig
    :param loop: The asyncio event loop.
    :type loop: asyncio.AbstractEventLoop | None
    :param dedup_window: Deduplication window in seconds.
    :type dedup_window: float
    :param health_timeout: Seconds without inbound packets before a
        connected child is marked stale.
    :type health_timeout: float
    :param max_consecutive_errors: Number of consecutive errors before
        a child is marked offline.
    :type max_consecutive_errors: int
    :param accepted_hgis: Optional set of HGI IDs that are allowed.
        When set, packets from children whose HGI is not in this set
        are dropped.  Construction-only — no runtime mutation.
    :type accepted_hgis: set[str] | None
    :param port_names: Optional list of port names for diagnostics.
    :type port_names: list[str] | None
    """

    def __init__(
        self,
        protocol: RamsesProtocolT,
        transports: list[TransportInterface | None],
        /,
        *,
        config: TransportConfig,
        loop: asyncio.AbstractEventLoop | None = None,
        dedup_window: float = _DEFAULT_DEDUP_WINDOW,
        health_timeout: float = _DEFAULT_HEALTH_TIMEOUT,
        max_consecutive_errors: int = _DEFAULT_MAX_CONSECUTIVE_ERRORS,
        accepted_hgis: set[str] | None = None,
        port_names: list[str] | None = None,
    ) -> None:
        """Initialise the pooled transport."""
        self._protocol: RamsesProtocolT = protocol
        self._config: TransportConfig = config
        self._loop: asyncio.AbstractEventLoop = (
            loop or asyncio.get_event_loop()
        )
        self._closing: bool = False
        self._protocol_connected: bool = False

        self._dedup_window: td = td(seconds=dedup_window)
        # Dict-backed dedup cache: key -> timestamp.
        # O(1) lookup instead of O(N) deque scan.
        self._dedup_cache: dict[_DedupKeyT, dt] = {}

        # Build immutable child registry.
        self._children: list[PoolChild] = []
        for i, t in enumerate(transports):
            pname = port_names[i] if port_names else f"child-{i}"
            accepted = True
            if accepted_hgis is not None:
                # Will be re-evaluated when HGI is learned.
                accepted = True  # default: accept until HGI is known
            child = PoolChild(
                child_id=i,
                port_name=pname,
                transport=t,
                accepted=accepted,
            )
            self._children.append(child)

        # Store accepted_hgis as a frozenset for O(1) membership tests.
        self._accepted_hgis: frozenset[str] | None = (
            frozenset(accepted_hgis) if accepted_hgis is not None else None
        )

        # Round-robin outbound counter.
        self._rr_index: int = 0

        # Health tracking thresholds.
        self._health_timeout: td = td(seconds=health_timeout)
        self._max_consecutive_errors: int = max_consecutive_errors

        # Connection future — resolved when at least one child connects.
        self._conn_fut: asyncio.Future[TransportInterface] | None = None

        # Aggregate stats.
        self._pkts_deduped: int = 0
        self._pkts_forwarded: int = 0

        # Outbound publisher for callback-driven children (PR 4A).
        # When set, children with ``callback_driven=True`` and no
        # transport instance publish frames through this callback.
        self._outbound_publisher: MqttPoolOutbound | None = None

    # -- Child access ----------------------------------------------------

    @property
    def _active_children(self) -> list[PoolChild]:
        """Return children with a transport or callback-driven."""
        return [
            c
            for c in self._children
            if c.transport is not None or c.callback_driven
        ]

    @property
    def _connected_children(self) -> list[PoolChild]:
        """Return connected children."""
        return [c for c in self._children if c.is_connected]

    @property
    def _active_hgi_ids(self) -> list[DeviceIdT]:
        """Return HGI IDs of all connected children with known HGI.

        Derived from child records — no separate mutable set.
        """
        return [
            c.hgi_id
            for c in self._children
            if c.is_connected and c.hgi_id is not None
        ]

    def _child_by_id(self, child_id: int) -> PoolChild:
        """Return the child with the given ID.

        :param child_id: The stable child ID.
        :returns: The PoolChild.
        :raises IndexError: If the ID is out of range.
        """
        return self._children[child_id]

    def _child_by_hgi_id(self, hgi_id: str) -> PoolChild | None:
        """Return the child with the given HGI ID.

        :param hgi_id: The HGI device ID to look up.
        :returns: The matching PoolChild, or ``None`` if not found.
        """
        for child in self._children:
            if child.hgi_id is not None and str(child.hgi_id) == hgi_id:
                return child
        return None

    def set_outbound_publisher(self, publisher: MqttPoolOutbound) -> None:
        """Set the outbound publisher for callback-driven children.

        When set, children with ``callback_driven=True`` and no
        transport instance publish frames through this callback
        instead of a per-child transport.

        :param publisher: The outbound publisher implementing
            :class:`~ramses_tx.transport.callbacks.MqttPoolOutbound`.
        """
        self._outbound_publisher = publisher

    # -- TransportInterface ---------------------------------------------

    def close(self) -> None:
        """Close all child transports."""
        if self._closing:
            return
        self._closing = True
        for child in self._children:
            if child.transport is None:
                continue
            try:
                child.transport.close()
            except Exception as err:  # pragma: no cover - defensive
                _LOGGER.debug(
                    "Error closing child %d: %s", child.child_id, err
                )

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        """Return aggregate extra info from connected children.

        Preserves the compatibility keys consumed by ``ramses_rf`` and
        ``ramses_cc``: ``pool_hgi_ids``, ``pool_rssi_trackers``,
        ``pool_stats``, ``SZ_ACTIVE_HGI``, ``SZ_IS_EVOFW3``.
        """
        if name == "pool_rssi_trackers":
            return [c.rssi_tracker for c in self._children if c.is_connected]
        if name == SZ_ACTIVE_HGI:
            for c in self._children:
                if c.is_connected and c.hgi_id is not None:
                    return c.hgi_id
            return default
        if name == "pool_hgi_ids":
            return [
                str(c.hgi_id)
                for c in self._children
                if c.is_connected and c.hgi_id is not None
            ]
        if name == SZ_IS_EVOFW3:
            for c in self._children:
                if c.is_connected and c.transport_obj is not None:
                    val = c.transport_obj.get_extra_info(SZ_IS_EVOFW3, False)
                    if val:
                        return True
            # Callback-driven children (e.g. ramses_esp via MQTT)
            # are treated as evofw3-compatible.
            if any(
                c.is_connected and c.callback_driven for c in self._children
            ):
                return True
            return default
        if name == "pool_stats":
            return {
                "children": len(self._children),
                "connected": sum(1 for c in self._children if c.is_connected),
                "healthy": sum(
                    1 for c in self._children if c.is_connected and c.is_online
                ),
                "received": [c.pkts_received for c in self._children],
                "deduped": self._pkts_deduped,
                "forwarded": self._pkts_forwarded,
                "avg_rssi": [
                    round(self._best_rssi(c), 1) for c in self._children
                ],
                "child_health": [c.is_online for c in self._children],
                "consecutive_errors": [
                    c.consecutive_errors for c in self._children
                ],
                "child_hgi": [
                    str(c.hgi_id) if c.hgi_id else None for c in self._children
                ],
                "accepted_hgis": (
                    sorted(self._accepted_hgis)
                    if self._accepted_hgis is not None
                    else None
                ),
            }
        return default

    async def send_frame(self, frame: str) -> None:
        """Send a frame via a selected child transport."""
        await self.write_frame(frame)

    # -- Pre-serialization routing contract (PR 2) ----------------------

    def prepare_command(self, request: RouteRequest) -> RoutedCommand:
        """Select a child and resolve the source address before serialization.

        Extracts the target device from the command's positional
        addresses using the authoritative ``packet_addrs()`` helper,
        selects the best child using RSSI/cold-start fallback, and
        resolves the source address based on ``SourcePolicy`` and the
        selected child's evofw3/HGI80 capability.

        :param request: Immutable route request.
        :returns: Routed command with pinned child ID and final DTO.
        :raises TransportError: If no child is sendable.
        """
        cmd = request.command

        # Extract target device from the command's positional addresses.
        # addr2 is the destination in standard RAMSES frames.
        target_device = cmd.addr2 if cmd.addr2 != "--:------" else None

        child = self._select_child(target_device)
        if child is None:
            raise exc.TransportError(
                "No connected child transport available for send"
            )

        # Resolve source address based on SourcePolicy.
        final_cmd = cmd
        if request.source_policy is SourcePolicy.GATEWAY:
            child_hgi = child.hgi_id
            if (
                child_hgi
                and cmd.addr1[:2] == "18"
                and cmd.addr1 != HGI_DEV_ADDR.id
                and cmd.addr1 != str(child_hgi)
            ):
                # evofw3: patch source to the selected child's HGI ID.
                final_cmd = dataclasses.replace(cmd, addr1=str(child_hgi))
                _LOGGER.debug(
                    "PooledTransport.prepare_command: patched source "
                    "%s -> %s for child %d (evofw3)",
                    cmd.addr1,
                    child_hgi,
                    child.child_id,
                )
            # HGI80: leave 18:000730 placeholder as-is — the firmware
            # substitutes its own ID during transmission.
        # SourcePolicy.PRESERVE: never modify the source.

        _LOGGER.debug(
            "PooledTransport.prepare_command: selected child %d "
            "(hgi=%s) for target %s, source_policy=%s",
            child.child_id,
            child.hgi_id,
            target_device,
            request.source_policy.name,
        )

        return RoutedCommand(child_id=str(child.child_id), command=final_cmd)

    async def write_routed(
        self,
        routed: RoutedCommand,
        frame: str,
        *,
        disable_tx_limits: bool = False,
    ) -> WriteOutcome:
        """Dispatch a routed command to the pinned child.

        Looks up the child by ``routed.child_id`` and dispatches the
        pre-serialized frame.  Transport-level repeats reuse the same
        child and frame.

        :param routed: The routed command from ``prepare_command()``.
        :param frame: The serialized frame (from ``str(routed.command)``).
        :param disable_tx_limits: If True, bypass per-child rate
            limiting.
        :returns: Conservative write outcome classification.
        """
        try:
            child = self._child_by_id(int(routed.child_id))
        except (IndexError, ValueError):
            return WriteOutcome.NOT_SUBMITTED

        # Callback-driven children (PR 4A): publish through the
        # outbound publisher instead of a per-child transport.
        if child.callback_driven and child.transport is None:
            if not child.is_sendable:
                return WriteOutcome.NOT_SUBMITTED
            if self._outbound_publisher is None or child.hgi_id is None:
                return WriteOutcome.NOT_SUBMITTED
            try:
                await self._outbound_publisher.publish_frame(
                    str(child.hgi_id), frame
                )
                return WriteOutcome.SUBMITTED
            except Exception:
                return WriteOutcome.AMBIGUOUS

        if child.transport is None:
            return WriteOutcome.NOT_SUBMITTED

        write = getattr(child.transport, "write_frame", None)
        if write is None:
            try:
                await child.transport.send_frame(frame)
                return WriteOutcome.SUBMITTED
            except Exception:
                return WriteOutcome.AMBIGUOUS

        try:
            await write(frame, disable_tx_limits=disable_tx_limits)
            return WriteOutcome.SUBMITTED
        except TypeError:
            # Child's write_frame doesn't accept disable_tx_limits.
            try:
                await write(frame)
                return WriteOutcome.SUBMITTED
            except Exception:
                return WriteOutcome.AMBIGUOUS
        except Exception:
            return WriteOutcome.AMBIGUOUS

    async def write_frame(
        self, frame: str, disable_tx_limits: bool = False
    ) -> None:
        """Legacy outbound dispatch — delegates to the routing API.

        This method is kept for backward compatibility with callers
        that still use ``write_frame()`` directly (e.g. ``send_frame()``
        or third-party code).  The preferred path is
        ``prepare_command()`` + ``write_routed()``.

        When called directly, the frame is parsed to extract the target
        device, a child is selected, and the frame is dispatched.  Source
        re-patching is done on the serialized frame as a fallback.

        :param frame: The raw ASCII frame to transmit.
        :param disable_tx_limits: If True, bypass per-child rate
            limiting.
        """
        # Parse the frame to extract target device for child selection.
        target_device: str | None = None
        parts = frame.split()
        if len(parts) >= 4:
            target_device = parts[3]

        child = self._select_child(target_device)
        if child is None:
            raise exc.TransportError(
                "No connected child transport available for send"
            )

        # Fallback source re-patching on the serialized frame.
        # The preferred path (prepare_command) does this on the DTO
        # before serialization.
        src_addr = parts[2] if len(parts) >= 4 else None
        child_hgi = child.hgi_id
        if (
            child_hgi
            and src_addr
            and src_addr[:2] == "18"
            and src_addr != HGI_DEV_ADDR.id
            and src_addr != str(child_hgi)
        ):
            parts[2] = str(child_hgi)
            leading = ""
            if frame and frame[0].isspace():
                leading = frame[0]
            frame = leading + " ".join(parts)
            _LOGGER.debug(
                "PooledTransport.write_frame: re-patched source %s -> %s "
                "for child %d (legacy path)",
                src_addr,
                child_hgi,
                child.child_id,
            )

        write = getattr(child.transport, "write_frame", None)
        if write is None:
            await child.transport.send_frame(frame)  # type: ignore[union-attr]
        else:
            try:
                await write(frame, disable_tx_limits=disable_tx_limits)
            except TypeError:
                await write(frame)

    # -- Internal: inbound dedup + forward -------------------------------

    def _on_child_packet(
        self,
        child_id: int,
        packet: Packet,
        *,
        ingress_hgi_id: DeviceIdT | None = None,
    ) -> None:
        """Process a packet from a child transport.

        Deduplicates against the dict-backed cache and forwards to the
        real protocol if not a duplicate.  Records the packet's RSSI
        in the child's tracker for outbound routing, excluding
        loopback frames from active pool HGI sources.  Updates the
        child's health timestamp.

        :param child_id: The stable child ID.
        :param packet: The parsed packet.
        :param ingress_hgi_id: Optional HGI identity from the callback
            boundary.  When provided, overrides the child record's HGI.
        """
        child = self._child_by_id(child_id)
        child.pkts_received += 1

        if self._closing:
            return

        # Learn the child's HGI ID from the puzzle response (7FFF)
        # or any packet whose src is a known HGI.
        if child.hgi_id is None:
            src_id = packet._dto.addr1
            if src_id and packet._dto.code == Code._PUZZ:
                child.learn_hgi(DeviceIdT(src_id))

        # HGI filtering: if an accepted set is configured, drop packets
        # from children whose HGI is not accepted.
        hgi = child.hgi_id
        if self._accepted_hgis is not None and hgi is not None:
            if str(hgi) not in self._accepted_hgis:
                return

        # Carry ingress provenance onto the Packet envelope (PR 1 item 7).
        # Explicit callback value takes precedence, then the child record.
        resolved_ingress = ingress_hgi_id or child.hgi_id
        if resolved_ingress is not None:
            packet._ingress_hgi_id = str(resolved_ingress)

        # Update health tracking — any packet proves the child is alive.
        child.mark_online()

        # Record RSSI in the child's tracker, EXCLUDING loopback
        # frames from active pool HGI sources.  When one pool HGI
        # transmits, both HGIs hear it: the transmitter produces a
        # local echo and the other hears an over-air copy.  These
        # are not route-quality evidence for the transmitting HGI.
        src_id = packet._dto.addr1
        if src_id:
            active_hgi_ids = {
                str(c.hgi_id)
                for c in self._children
                if c.is_connected and c.hgi_id is not None
            }
            if src_id not in active_hgi_ids:
                child.rssi_tracker.record(src_id, packet._dto.rssi, dt_now())

        # Dict-backed dedup with sequence-aware key.
        key = self._dedup_key(packet)
        now = dt_now()

        # Purge stale entries from the dedup cache.
        cutoff = now - self._dedup_window
        # Collect stale keys (can't modify dict during iteration).
        stale_keys = [k for k, t in self._dedup_cache.items() if t < cutoff]
        for k in stale_keys:
            del self._dedup_cache[k]

        # Check for duplicate — O(1) dict lookup.
        if key in self._dedup_cache:
            self._pkts_deduped += 1
            _LOGGER.debug(
                "PooledTransport: deduped packet from child %d: %s",
                child_id,
                packet,
            )
            return

        # Not a duplicate — record and forward.
        self._dedup_cache[key] = now
        # Enforce max cache size.
        if len(self._dedup_cache) > _MAX_DEDUP_KEYS:
            # Evict oldest entry (linear scan, but rare).
            oldest_key = min(
                self._dedup_cache, key=lambda k: self._dedup_cache[k]
            )
            del self._dedup_cache[oldest_key]

        self._pkts_forwarded += 1

        try:
            self._loop.call_soon_threadsafe(
                self._protocol.packet_received, packet
            )
        except RuntimeError as err:
            _LOGGER.debug(
                "PooledTransport: event loop closed, cannot forward: %s",
                err,
            )

    @staticmethod
    def _dedup_key(packet: Packet) -> _DedupKeyT:
        """Build a deduplication key from packet content.

        The key includes the transport-assigned sequence when present
        (not ``---``) because it is sender-assigned and stable across
        HGIs (confirmed from captured fixtures).  When sequence is
        absent, an empty string is used as the fallback.

        :param packet: The packet to key.
        :returns: A tuple of (verb, code, src, dst, addr3, seq, payload).
        """
        dto = packet._dto
        seq = dto.seq if dto.seq and dto.seq != "---" else ""
        return (
            dto.verb,
            dto.code,
            dto.addr1,
            dto.addr2,
            dto.addr3,
            seq,
            dto.raw_payload,
        )

    def _best_rssi(
        self, child: PoolChild, device_id: str | None = None
    ) -> float:
        """Return the best RSSI for a child, optionally for a device.

        :param child: The PoolChild to query.
        :param device_id: Optional device ID for per-device RSSI.
        :returns: Best RSSI in dBm, or 0 if no data.
        """
        tracker = child.rssi_tracker
        if device_id is not None:
            val = tracker.best_rssi_for(device_id)
            if val is not None:
                return float(val)
            return float(_RSSI_UNKNOWN)
        # No device specified: return best RSSI across all known devices.
        best = _RSSI_UNKNOWN
        for dev_id in tracker.known_devices():
            val = tracker.best_rssi_for(dev_id)
            if val is not None and val > best:
                best = val
        return float(best)

    # -- Internal: connection lifecycle ---------------------------------

    def _on_child_connected(self, child_id: int, transport_obj: Any) -> None:
        """Mark a child as connected and capture its HGI ID."""
        child = self._child_by_id(child_id)
        child.mark_connected(transport_obj)

        _LOGGER.info(
            "PooledTransport: child %d connected (HGI=%s), %d/%d connected",
            child_id,
            child.hgi_id,
            len(self._connected_children),
            len(self._children),
        )

        # Notify the real protocol that the transport is connected.
        if not self._protocol_connected:
            self._protocol_connected = True
            self._protocol.connection_made(self, ramses=True)

        # Resolve the connection future if waiting.
        if self._conn_fut is not None and not self._conn_fut.done():
            self._conn_fut.set_result(self)

    def _on_child_disconnected(
        self, child_id: int, error: Exception | None
    ) -> None:
        """Mark a child as disconnected and record the error.

        Increments the consecutive error counter; if it exceeds the
        threshold, the child is marked offline.
        """
        child = self._child_by_id(child_id)
        child.mark_disconnected()
        child.record_error()

        if child.consecutive_errors >= self._max_consecutive_errors:
            if child.availability is not NodeAvailability.OFFLINE:
                child.availability = NodeAvailability.OFFLINE
                _LOGGER.warning(
                    "PooledTransport: child %d marked offline "
                    "(%d consecutive errors)",
                    child_id,
                    child.consecutive_errors,
                )

        _LOGGER.info(
            "PooledTransport: child %d disconnected (%s), %d/%d connected",
            child_id,
            error,
            len(self._connected_children),
            len(self._children),
        )

        # If no children are connected, notify the real protocol.
        if not self._connected_children and not self._closing:
            self._protocol_connected = False
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(
                    self._protocol.connection_lost, error
                )

    async def _wait_for_any_connection(
        self, timeout: float = 1.0
    ) -> TransportInterface:
        """Wait until at least one child transport is connected."""
        if self._connected_children:
            return self

        if self._conn_fut is None or self._conn_fut.done():
            self._conn_fut = self._loop.create_future()

        try:
            return await asyncio.wait_for(
                asyncio.shield(self._conn_fut), timeout=timeout
            )
        except TimeoutError as err:
            raise exc.TransportError(
                f"PooledTransport: no child connected within {timeout}s"
            ) from err

    # -- Internal: outbound routing -------------------------------------

    def _select_child(
        self, target_device: str | None = None
    ) -> PoolChild | None:
        """Select a child for outbound transmission.

        Uses per-device RSSI when ``target_device`` is provided and
        per-device samples exist.  Falls back to aggregate RSSI, then
        round-robin among connected, sendable children when no RSSI
        data is available.  Returns ``None`` if no child is sendable.
        """
        # Check health timeouts before selecting.
        self._check_health()

        # Only consider sendable children.
        candidates = [c for c in self._children if c.is_sendable]
        if not candidates:
            return None

        # Compute average RSSI for each candidate (keyed by child_id
        # because PoolChild is a mutable dataclass and not hashable).
        rssi_values = {
            c.child_id: self._best_rssi(c, target_device) for c in candidates
        }
        _LOGGER.debug(
            "PooledTransport: _select_child target=%s candidates=%s "
            "rssi_values=%s",
            target_device,
            [c.child_id for c in candidates],
            rssi_values,
        )

        # If per-device RSSI returned nothing for all candidates,
        # fall back to aggregate RSSI.
        if target_device and all(
            v == float(_RSSI_UNKNOWN) for v in rssi_values.values()
        ):
            rssi_values = {c.child_id: self._best_rssi(c) for c in candidates}

        # If no child has RSSI data, fall back to round-robin.
        if all(v == float(_RSSI_UNKNOWN) for v in rssi_values.values()):
            n = len(self._children)
            for _ in range(n):
                self._rr_index = (self._rr_index + 1) % n
                child = self._child_by_id(self._rr_index)
                if child in candidates:
                    return child
            return candidates[0]

        # Select the child with the best (highest) average RSSI.
        # Ties are broken by lowest child_id for determinism.
        best_child = max(
            candidates,
            key=lambda c: (rssi_values[c.child_id], -c.child_id),
        )
        _LOGGER.debug(
            "PooledTransport: selected child %d (rssi=%s) for target %s",
            best_child.child_id,
            rssi_values[best_child.child_id],
            target_device,
        )
        return best_child

    def _check_health(self) -> None:
        """Check all children for health timeout and mark stale.

        A connected child that has not received any packets within
        ``health_timeout`` is marked stale.  Stale children are not
        re-enabled as a last resort — offline is a definitive state
        that requires explicit reconnection.
        """
        now = dt_now()

        for child in self._children:
            if child.transport is None and not child.callback_driven:
                continue
            if not child.is_connected:
                continue
            if not child.is_online:
                continue

            last_pkt = child.last_pkt_time
            if last_pkt is None:
                # Connected but never received a packet — still healthy
                # (connection is recent enough).
                continue

            if now - last_pkt > self._health_timeout:
                child.mark_stale()
                _LOGGER.warning(
                    "PooledTransport: child %d marked stale "
                    "(no packets for %.1fs)",
                    child.child_id,
                    (now - last_pkt).total_seconds(),
                )

    # -- Diagnostics -----------------------------------------------------

    def __repr__(self) -> str:
        """Return a diagnostic representation of the pool."""
        active = len(self._active_children)
        connected = len(self._connected_children)
        return f"PooledTransport(children={active}, connected={connected})"

    @property
    def is_closing(self) -> bool:
        """Return True if the pool is closing or has closed."""
        return self._closing
