#!/usr/bin/env python3
"""RAMSES RF - Pooled transport for multi-HGI link-layer pooling.

Combines multiple physical transports (serial, MQTT, ser2net) into a
single coherent :class:`TransportInterface` that the protocol layer
sees as one transport.  Inbound packets from any child are deduplicated
within a sliding time window and forwarded upstream.  Outbound frames
are routed to the child transport with the best rolling-average RSSI,
falling back to round-robin when no RSSI data is available yet.
Unhealthy children are detected via a configurable health timeout and
excluded from outbound selection until they recover.

This is Roadmap Item 9, PRs 2-4 (issue 1122).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from datetime import datetime as dt, timedelta as td
from typing import Any, TypeAlias

from .. import exceptions as exc
from ..address import HGI_DEV_ADDR
from ..const import SZ_ACTIVE_HGI, SZ_IS_EVOFW3, Code
from ..helpers import dt_now
from ..interfaces import ProtocolInterface, TransportInterface
from ..packet import Packet
from ..rssi_tracker import RssiTracker
from ..typing import RamsesProtocolT
from .base import TransportConfig

_LOGGER = logging.getLogger(__name__)

#: Default deduplication window in seconds.  Two packets with the same
#: content key arriving within this window from different child
#: transports are considered duplicates.
_DEFAULT_DEDUP_WINDOW: float = 0.5

#: Maximum number of dedup keys retained in the sliding window.
_MAX_DEDUP_KEYS: int = 512

#: RSSI value used when a child has no data yet (treated as neutral).
_RSSI_UNKNOWN: int = 0

#: Default health-check interval in seconds.  A child that has not
#: received any packets for this duration is marked unhealthy.
#: Set to 180s (3 min) because RAMSES traffic can be sparse — some
#: devices poll every 2-3 minutes, and a 60s timeout caused false
#: unhealthy markings during quiet periods (observed in pool testing
#: with real MQTT HGIs, issue 1119).
_DEFAULT_HEALTH_TIMEOUT: float = 180.0

#: Number of consecutive errors before a child is marked unhealthy.
_DEFAULT_MAX_CONSECUTIVE_ERRORS: int = 5

#: Key for deduplication: (verb, code, src, dst, addr3, raw_payload).
_DedupKeyT: TypeAlias = tuple[str, str, str, str, str, str]


class _ChildProtocolProxy(ProtocolInterface):
    """Protocol proxy inserted between a child transport and the pool.

    Each child transport is created with this proxy as its protocol.
    The proxy intercepts ``packet_received`` and routes the packet to
    the :class:`PooledTransport` for deduplication and upstream
    forwarding.  Connection lifecycle events are also forwarded so the
    pool can track which children are alive.

    :param pool: The owning pooled transport.
    :type pool: PooledTransport
    :param index: The child's position in the pool's transport list.
    :type index: int
    """

    def __init__(self, pool: PooledTransport, index: int) -> None:
        """Initialise the child protocol proxy."""
        self._pool = pool
        self._index = index
        self._connected: bool = False
        self._conn_event: asyncio.Event = asyncio.Event()

    # -- ProtocolInterface ----------------------------------------------

    def connection_made(
        self, transport: Any, /, *, ramses: bool = False
    ) -> None:
        """Forward connection_made to the pool for tracking."""
        self._connected = True
        self._conn_event.set()
        self._pool._on_child_connected(self._index, transport)

    def connection_lost(self, error: Exception | None) -> None:
        """Forward connection_lost to the pool for tracking."""
        self._connected = False
        self._conn_event.clear()
        self._pool._on_child_disconnected(self._index, error)

    def packet_received(self, packet: Packet) -> None:
        """Route the packet to the pool for dedup + upstream forward."""
        self._pool._on_child_packet(self._index, packet)

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
                f"Child transport {self._index} did not connect "
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
    rolling-average RSSI (5-sample window).  When no RSSI data is
    available for any child, selection falls back to round-robin.
    Unhealthy children (no packets for ``health_timeout`` seconds, or
    exceeding ``max_consecutive_errors``) are excluded from selection
    until they recover.

    :param protocol: The real protocol that receives deduplicated
        packets.
    :type protocol: RamsesProtocolT
    :param transports: List of child transports to pool.
    :type transports: list[TransportInterface]
    :param config: Transport configuration shared by all children.
    :type config: TransportConfig
    :param loop: The asyncio event loop.
    :type loop: asyncio.AbstractEventLoop | None
    :param dedup_window: Deduplication window in seconds.  Packets
        with the same content key arriving within this window from
        different children are suppressed.  Defaults to 0.5s.
    :type dedup_window: float
    :param health_timeout: Seconds without inbound packets before a
        connected child is marked unhealthy.  Defaults to 60s.
    :type health_timeout: float
    :param max_consecutive_errors: Number of consecutive errors before
        a child is marked unhealthy.  Defaults to 5.
    :type max_consecutive_errors: int
    :param accepted_hgis: Optional set of HGI IDs that are allowed to
        forward packets.  When set, packets from children whose HGI is
        not in this set are dropped before dedup/forwarding.  When
        ``None`` (default), all child packets are forwarded.  Used by
        ramses_cc to implement schema-driven pool membership
        (``_owner: me`` → accepted, ``_owner: not-me`` → rejected).
    :type accepted_hgis: set[str] | None
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
    ) -> None:
        """Initialise the pooled transport."""
        # Allow empty transport list during construction —
        # pooled_transport_factory creates the pool first (with an
        # empty list) then injects children via _transports.
        # The check is deferred to _wait_for_any_connection.

        self._protocol: RamsesProtocolT = protocol
        self._transports: list[TransportInterface | None] = list(transports)
        self._config: TransportConfig = config
        self._loop: asyncio.AbstractEventLoop = (
            loop or asyncio.get_event_loop()
        )
        self._closing: bool = False
        self._protocol_connected: bool = False

        self._dedup_window: td = td(seconds=dedup_window)
        self._dedup_cache: deque[tuple[dt, _DedupKeyT]] = deque(
            maxlen=_MAX_DEDUP_KEYS
        )

        # Per-child connection state.
        self._child_connected: list[bool] = [False] * len(transports)
        self._child_hgi: list[str | None] = [None] * len(transports)
        self._child_transport_objs: list[Any] = [None] * len(transports)

        # Round-robin outbound counter (used as fallback when no RSSI).
        self._rr_index: int = 0

        # Per-child RssiTracker instances for device-aware outbound
        # routing.  Each tracker maintains the last N RSSI readings per
        # device heard by that child.  Uses the shared RssiTracker from
        # ramses_tx/rssi_tracker.py (PR 1123, issue 1047) — consistent
        # with the gateway-level tracker used for CommunicationQuality.
        self._child_rssi_trackers: list[RssiTracker] = [
            RssiTracker() for _ in transports
        ]

        # Accepted HGI set for schema-driven filtering.  When set,
        # only packets from children whose HGI is in this set are
        # forwarded.  Updated by ramses_cc when the user accepts/rejects
        # an HGI in the discovery review.
        self._accepted_hgis: set[str] | None = accepted_hgis

        # Per-child health tracking (PR 4).
        self._health_timeout: td = td(seconds=health_timeout)
        self._max_consecutive_errors: int = max_consecutive_errors
        self._child_last_pkt_time: list[dt | None] = [None] * len(transports)
        self._child_consecutive_errors: list[int] = [0] * len(transports)
        self._child_healthy: list[bool] = [True] * len(transports)

        # Connection future — resolved when at least one child connects.
        self._conn_fut: asyncio.Future[TransportInterface] | None = None

        # Stats for diagnostics.
        self._pkts_received: list[int] = [0] * len(transports)
        self._pkts_deduped: int = 0
        self._pkts_forwarded: int = 0

    # -- TransportInterface ---------------------------------------------

    def close(self) -> None:
        """Close all child transports."""
        if self._closing:
            return
        self._closing = True
        for t in self._transports:
            if t is None:
                continue
            try:
                t.close()
            except Exception as err:  # pragma: no cover - defensive
                _LOGGER.debug("Error closing child transport: %s", err)

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        """Return aggregate extra info from connected children.

        For ``SZ_ACTIVE_HGI`` returns the first connected child's HGI
        ID.  For ``SZ_IS_EVOFW3`` returns True if any connected child
        is evofw3.
        """
        if name == "pool_rssi_trackers":
            # Expose per-child RSSI trackers so the gateway can
            # compute communication_quality across all HGIs (best RSSI).
            return [
                self._child_rssi_trackers[i]
                for i in range(len(self._transports))
                if self._child_connected[i]
            ]
        if name == SZ_ACTIVE_HGI:
            for hgi in self._child_hgi:
                if hgi is not None:
                    return hgi
            return default
        if name == "pool_hgi_ids":
            # Return all connected children's HGI IDs (for discovery scan
            # to exclude pool members from "new device" notifications).
            return [hgi for hgi in self._child_hgi if hgi is not None]
        if name == SZ_IS_EVOFW3:
            for i, connected in enumerate(self._child_connected):
                if connected and self._child_transport_objs[i] is not None:
                    val = self._child_transport_objs[i].get_extra_info(
                        SZ_IS_EVOFW3, False
                    )
                    if val:
                        return True
            return default
        if name == "pool_stats":
            return {
                "children": len(self._transports),
                "connected": sum(self._child_connected),
                "healthy": sum(
                    1
                    for i in range(len(self._transports))
                    if self._child_connected[i] and self._child_healthy[i]
                ),
                "received": list(self._pkts_received),
                "deduped": self._pkts_deduped,
                "forwarded": self._pkts_forwarded,
                "avg_rssi": [
                    round(self._best_rssi(i), 1)
                    for i in range(len(self._transports))
                ],
                "child_health": list(self._child_healthy),
                "consecutive_errors": list(self._child_consecutive_errors),
                "child_hgi": list(self._child_hgi),
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

    async def write_frame(
        self, frame: str, disable_tx_limits: bool = False
    ) -> None:
        """Route an outbound frame to a selected child transport.

        Selection uses the highest rolling-average RSSI among
        connected children for the target device (if known from the
        frame), falling back to aggregate RSSI, then round-robin when
        no RSSI data is available.

        The frame's source address (addr1) is re-patched to match the
        selected child's HGI ID before forwarding.  This is needed
        because the protocol patches addr1 to the pool's "active" HGI
        (the first connected child), but the pool may route the frame
        to a different child with better RSSI.  Without re-patching,
        the frame would be transmitted by the wrong HGI with the wrong
        source ID.

        For HGI80 children (``_is_evofw3`` is False), the protocol
        patches addr1 to the placeholder ``18:000730`` and the HGI80
        firmware substitutes its own hardware ID during transmission.
        In this case, re-patching is skipped — the placeholder is
        correct for any HGI80 child.

        :param frame: The raw ASCII frame to transmit.
        :type frame: str
        :param disable_tx_limits: If True, bypass per-child rate
            limiting.  Forwarded to the selected child's
            ``write_frame``.
        :type disable_tx_limits: bool
        """
        # Parse the frame to extract target device and source address.
        # Frame format (from CommandDTO.__str__):
        #   " I --- 18:001234 01:123456 --:------ 30C9 001 00"
        #    0  1   2         3         4          5    6   7
        #  verb --- addr1     addr2     addr3      code len payload
        target_device: str | None = None
        src_addr: str | None = None
        parts = frame.split()
        if len(parts) >= 4:
            src_addr = parts[2]
            target_device = parts[3]

        child = self._select_transport(target_device)
        if child is None:
            raise exc.TransportError(
                "No connected child transport available for send"
            )

        # Find the selected child's index and HGI ID.
        child_idx: int | None = None
        for i, t in enumerate(self._transports):
            if t is child:
                child_idx = i
                break
        child_hgi = (
            self._child_hgi[child_idx] if child_idx is not None else None
        )

        # Re-patch the frame's source address to match the selected
        # child's HGI ID.  This is only needed when the source is an
        # HGI address (18:) that was patched by the protocol to the
        # pool's "active" HGI (first connected child).  Skip if:
        # - child HGI is unknown (not yet connected/discovered)
        # - frame has no parseable source address
        # - source is NOT an HGI address (e.g. faked device 37:001234
        #   impersonating a REM — the HGI is the transmitter, not the
        #   source; replacing it would break impersonation)
        # - source is the placeholder 18:000730 (HGI80 firmware
        #   will substitute its own ID — correct for any HGI80 child)
        # - source already matches the child's HGI (no change needed)
        if (
            child_hgi
            and src_addr
            and src_addr[:2] == "18"  # only re-patch HGI sources
            and src_addr != HGI_DEV_ADDR.id
            and src_addr != child_hgi
        ):
            parts[2] = child_hgi
            # Preserve leading whitespace (verb can be ' I' with a
            # leading space in RAMSES frames).
            leading = ""
            if frame and frame[0].isspace():
                leading = frame[0]
            frame = leading + " ".join(parts)
            _LOGGER.debug(
                "PooledTransport: re-patched frame source %s -> %s "
                "for child %d",
                src_addr,
                child_hgi,
                child_idx,
            )

        # Child transports (PortTransport, MqttTransport) accept the
        # disable_tx_limits kwarg even though TransportInterface
        # doesn't declare it — use getattr to call the concrete method.
        write = getattr(child, "write_frame", None)
        if write is None:
            await child.send_frame(frame)
        else:
            try:
                await write(frame, disable_tx_limits=disable_tx_limits)
            except TypeError:
                # Child's write_frame doesn't accept disable_tx_limits
                await write(frame)

    # -- Internal: inbound dedup + forward -------------------------------

    def _on_child_packet(self, index: int, packet: Packet) -> None:
        """Process a packet from a child transport.

        Deduplicates against the sliding window and forwards to the
        real protocol if not a duplicate.  Records the packet's RSSI
        in the child's rolling sample window for outbound routing.
        Updates the child's health timestamp (a packet received resets
        the consecutive error counter and marks the child healthy).
        """
        self._pkts_received[index] += 1

        if self._closing:
            return

        # Learn the child's HGI ID from the puzzle response (7FFF)
        # or any packet whose src is a known HGI.  This is needed
        # because pool children skip the signature handshake to
        # avoid resetting ESP32-based USB HGIs.
        if self._child_hgi[index] is None:
            src_id = packet._dto.addr1
            if src_id and packet._dto.code == Code._PUZZ:
                self._child_hgi[index] = src_id
                _LOGGER.info(
                    "PooledTransport: child %d HGI learned as %s "
                    "from puzzle response",
                    index,
                    src_id,
                )

        # HGI filtering: if an accepted set is configured, drop packets
        # from children whose HGI is not accepted (foreign/neighbour's
        # gateway).  This is a single dict lookup — negligible overhead.
        hgi = self._child_hgi[index]
        if self._accepted_hgis is not None and hgi is not None:
            if hgi not in self._accepted_hgis:
                return

        # Update health tracking — any packet proves the child is alive.
        self._child_last_pkt_time[index] = dt_now()
        if self._child_consecutive_errors[index] > 0:
            self._child_consecutive_errors[index] = 0
        if not self._child_healthy[index]:
            self._child_healthy[index] = True
            _LOGGER.info(
                "PooledTransport: child %d recovered (healthy again)",
                index,
            )

        # Record RSSI in the child's RssiTracker for device-aware
        # outbound routing.  RssiTracker handles sentinel filtering
        # and ring-buffer management (PR 1123, issue 1047).
        src_id = packet._dto.addr1
        if src_id:
            self._child_rssi_trackers[index].record(
                src_id, packet._dto.rssi, dt_now()
            )

        key = self._dedup_key(packet)
        now = dt_now()

        # Purge stale entries from the dedup cache.
        cutoff = now - self._dedup_window
        while self._dedup_cache and self._dedup_cache[0][0] < cutoff:
            self._dedup_cache.popleft()

        # Check for duplicate.
        for _, existing_key in self._dedup_cache:
            if existing_key == key:
                self._pkts_deduped += 1
                _LOGGER.debug(
                    "PooledTransport: deduped packet from child %d: %s",
                    index,
                    packet,
                )
                return

        # Not a duplicate — record and forward.
        self._dedup_cache.append((now, key))
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

        :param packet: The packet to key.
        :type packet: Packet
        :returns: A tuple of (verb, code, src, dst, addr3, raw_payload).
        :rtype: _DedupKeyT
        """
        dto = packet._dto
        return (
            dto.verb,
            dto.code,
            dto.addr1,
            dto.addr2,
            dto.addr3,
            dto.raw_payload,
        )

    def _best_rssi(self, index: int, device_id: str | None = None) -> float:
        """Return the best RSSI for a child, optionally for a specific device.

        Uses :meth:`RssiTracker.best_rssi_for` (max of last N readings)
        when a device ID is provided.  Falls back to the best RSSI
        across all known devices for the child when no device is
        specified.  Returns ``_RSSI_UNKNOWN`` (0) if no data.

        :param index: The child index.
        :type index: int
        :param device_id: Optional device ID for per-device RSSI.
        :type device_id: str | None
        :returns: Best RSSI in dBm, or 0 if no data.
        :rtype: float
        """
        tracker = self._child_rssi_trackers[index]
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

    def _on_child_connected(self, index: int, transport_obj: Any) -> None:
        """Mark a child as connected and capture its HGI ID."""
        self._child_connected[index] = True
        self._child_transport_objs[index] = transport_obj

        # Read the child's active HGI from its extra info.
        hgi = transport_obj.get_extra_info(SZ_ACTIVE_HGI)
        self._child_hgi[index] = hgi

        _LOGGER.info(
            "PooledTransport: child %d connected (HGI=%s), %d/%d connected",
            index,
            hgi,
            sum(self._child_connected),
            len(self._transports),
        )

        # Notify the real protocol that the transport is connected.
        # The engine calls protocol.wait_for_connection_made() after
        # creating the transport, so we must forward this event.
        if not self._protocol_connected:
            self._protocol_connected = True
            self._protocol.connection_made(self, ramses=True)

        # Resolve the connection future if waiting.
        if self._conn_fut is not None and not self._conn_fut.done():
            self._conn_fut.set_result(self)

    def _on_child_disconnected(
        self, index: int, error: Exception | None
    ) -> None:
        """Mark a child as disconnected and record the error.

        Increments the consecutive error counter; if it exceeds the
        threshold, the child is marked unhealthy.
        """
        self._child_connected[index] = False
        self._child_hgi[index] = None

        # Health tracking — increment consecutive errors (PR 4).
        self._child_consecutive_errors[index] += 1
        if (
            self._child_consecutive_errors[index]
            >= self._max_consecutive_errors
        ):
            if self._child_healthy[index]:
                self._child_healthy[index] = False
                _LOGGER.warning(
                    "PooledTransport: child %d marked unhealthy "
                    "(%d consecutive errors)",
                    index,
                    self._child_consecutive_errors[index],
                )

        _LOGGER.info(
            "PooledTransport: child %d disconnected (%s), %d/%d connected",
            index,
            error,
            sum(self._child_connected),
            len(self._transports),
        )

        # If no children are connected, notify the real protocol.
        if not any(self._child_connected) and not self._closing:
            self._protocol_connected = False
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(
                    self._protocol.connection_lost, error
                )

    async def _wait_for_any_connection(
        self, timeout: float = 1.0
    ) -> TransportInterface:
        """Wait until at least one child transport is connected."""
        if any(self._child_connected):
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

    def _select_transport(
        self, target_device: str | None = None
    ) -> TransportInterface | None:
        """Select a child transport for outbound transmission.

        Uses per-device RSSI when ``target_device`` is provided and
        per-device samples exist for that device.  Falls back to
        aggregate RSSI, then round-robin among connected, healthy
        children when no RSSI data is available.  Returns ``None`` if
        no child is connected and healthy.
        """
        # Check health timeouts before selecting.
        self._check_health()

        # Only consider connected AND healthy AND non-removed children.
        candidates = [
            i
            for i in range(len(self._transports))
            if self._transports[i] is not None
            and self._child_connected[i]
            and self._child_healthy[i]
        ]
        if not candidates:
            return None

        # Compute average RSSI for each candidate.
        # Prefer per-device RSSI when a target device is known.
        rssi_values = {
            i: self._best_rssi(i, target_device) for i in candidates
        }
        _LOGGER.debug(
            "PooledTransport: _select_transport target=%s candidates=%s "
            "rssi_values=%s",
            target_device,
            candidates,
            rssi_values,
        )

        # If per-device RSSI returned nothing for all candidates,
        # fall back to aggregate RSSI.
        if target_device and all(
            v == float(_RSSI_UNKNOWN) for v in rssi_values.values()
        ):
            rssi_values = {i: self._best_rssi(i) for i in candidates}

        # If no child has RSSI data, fall back to round-robin.
        if all(v == float(_RSSI_UNKNOWN) for v in rssi_values.values()):
            n = len(self._transports)
            for _ in range(n):
                self._rr_index = (self._rr_index + 1) % n
                if self._rr_index in candidates:
                    return self._transports[self._rr_index]
            return self._transports[candidates[0]]

        # Select the child with the best (highest) average RSSI.
        # Ties are broken by lowest index for determinism.
        best_index = max(
            candidates,
            key=lambda i: (rssi_values[i], -i),
        )
        _LOGGER.debug(
            "PooledTransport: selected child %d (rssi=%s) for target %s",
            best_index,
            rssi_values[best_index],
            target_device,
        )
        return self._transports[best_index]

    def _check_health(self) -> None:
        """Check all children for health timeout and mark unhealthy.

        A connected child that has not received any packets within
        ``health_timeout`` is marked unhealthy.  If there are no
        healthy, connected children left, unhealthy children are
        re-evaluated as a last resort (better to try than to fail).
        """
        now = dt_now()
        any_healthy = False

        for i in range(len(self._transports)):
            if self._transports[i] is None:
                continue
            if not self._child_connected[i]:
                continue

            if not self._child_healthy[i]:
                continue

            last_pkt = self._child_last_pkt_time[i]
            if last_pkt is None:
                # Connected but never received a packet — check if
                # the connection is recent enough to still be healthy.
                continue

            if now - last_pkt > self._health_timeout:
                self._child_healthy[i] = False
                _LOGGER.warning(
                    "PooledTransport: child %d marked unhealthy "
                    "(no packets for %.1fs)",
                    i,
                    (now - last_pkt).total_seconds(),
                )
            else:
                any_healthy = True

        # If no healthy children remain, re-enable all connected ones
        # as a last resort (better to attempt transmission than fail).
        if not any_healthy:
            re_enabled = []
            for i in range(len(self._transports)):
                if (
                    self._transports[i] is not None
                    and self._child_connected[i]
                    and not self._child_healthy[i]
                ):
                    self._child_healthy[i] = True
                    re_enabled.append(i)
            if re_enabled:
                _LOGGER.info(
                    "PooledTransport: no healthy children, re-enabling "
                    "as last resort: %s",
                    re_enabled,
                )

    # -- Hot-reload: add/remove children at runtime ----------------------

    async def add_child(
        self,
        port_name: str,
        port_config: Any = None,
        extra: dict[str, object] | None = None,
    ) -> int:
        """Create a new child transport and add it to the pool.

        Uses :func:`_create_single_child` from the factory to create
        the transport (serial, MQTT, or Zigbee), then appends it to
        the pool's internal lists.  The child's HGI ID is
        auto-discovered when it connects.

        :param port_name: Transport address (serial path, MQTT URL,
            Zigbee URL).
        :type port_name: str
        :param port_config: Optional serial port configuration.
        :type port_config: Any
        :param extra: Optional extra configuration for the transport.
        :type extra: dict[str, object] | None
        :returns: The index of the new child in the pool.
        :rtype: int
        """
        if self._closing:
            raise exc.TransportError(
                "Cannot add child to a closing PooledTransport"
            )

        from ..typing import SerPortNameT
        from .factory import _create_single_child

        index = len(self._transports)
        proxy = _ChildProtocolProxy(self, index)

        child = await _create_single_child(
            proxy,
            config=self._config,
            port_name=SerPortNameT(port_name),
            port_config=port_config,
            extra=extra,
            loop=self._loop,
        )

        self._transports.append(child)
        self._child_connected.append(False)
        self._child_hgi.append(None)
        self._child_transport_objs.append(None)
        self._child_rssi_trackers.append(RssiTracker())
        self._child_last_pkt_time.append(None)
        self._child_consecutive_errors.append(0)
        self._child_healthy.append(True)
        self._pkts_received.append(0)

        _LOGGER.info(
            "PooledTransport: added child %d (port=%s), "
            "pool now has %d children",
            index,
            port_name,
            len(self._transports),
        )
        return index

    def remove_child(self, index: int) -> None:
        """Close and remove a child from the pool.

        The child transport is closed and all per-child state is
        removed.  Indices of remaining children are **not** shifted
        — the child is replaced with a ``None`` placeholder to keep
        index stability for proxies that may still reference it.

        :param index: The child index to remove.
        :type index: int
        """
        if index < 0 or index >= len(self._transports):
            raise ValueError(f"Invalid child index: {index}")

        if self._transports[index] is None:
            return  # already removed

        try:
            self._transports[index].close()  # type: ignore[union-attr]
        except Exception as err:  # pragma: no cover - defensive
            _LOGGER.debug("Error closing child %d: %s", index, err)

        # Mark as removed but keep slot for index stability.
        self._transports[index] = None
        self._child_connected[index] = False
        self._child_hgi[index] = None
        self._child_transport_objs[index] = None
        self._child_rssi_trackers[index].clear()
        self._child_last_pkt_time[index] = None
        self._child_consecutive_errors[index] = 0
        self._child_healthy[index] = False
        self._pkts_received[index] = 0

        _LOGGER.info(
            "PooledTransport: removed child %d, pool now has %d active",
            index,
            sum(1 for t in self._transports if t is not None),
        )

    def set_accepted_hgis(self, hgi_ids: set[str] | None) -> None:
        """Update the set of accepted HGI IDs for packet filtering.

        When set, only packets from children whose HGI is in this set
        are forwarded to the protocol.  Packets from non-accepted HGIs
        are dropped before dedup/forwarding (one dict lookup overhead).

        Set to ``None`` to disable filtering (accept all).

        Called by ramses_cc when the user accepts/rejects an HGI in
        the discovery review, or when ``_owner`` / ``_disabled``
        traits change in the schema.

        :param hgi_ids: Set of accepted HGI IDs, or None to accept all.
        :type hgi_ids: set[str] | None
        """
        self._accepted_hgis = set(hgi_ids) if hgi_ids is not None else None
        _LOGGER.info(
            "PooledTransport: accepted HGIs updated to %s",
            (
                sorted(self._accepted_hgis)
                if self._accepted_hgis is not None
                else "all (no filter)"
            ),
        )

    # -- Diagnostics -----------------------------------------------------

    def __repr__(self) -> str:
        """Return a diagnostic representation of the pool."""
        active = sum(1 for t in self._transports if t is not None)
        return (
            f"PooledTransport(children={active}, "
            f"connected={sum(self._child_connected)})"
        )

    @property
    def is_closing(self) -> bool:
        """Return True if the pool is closing or has closed."""
        return self._closing
