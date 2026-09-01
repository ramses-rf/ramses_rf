#!/usr/bin/env python3
"""RAMSES RF - Pooled transport for multi-HGI link-layer pooling.

Combines multiple physical transports (serial, MQTT, ser2net) into a
single coherent :class:`TransportInterface` that the protocol layer
sees as one transport.  Inbound packets from any child are deduplicated
within a sliding time window and forwarded upstream.  Outbound frames
are routed to a selected child transport (round-robin in this PR;
RSSI-based selection follows in a subsequent PR).

This is Roadmap Item 9, PR 2 (issue 1122).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from datetime import datetime as dt, timedelta as td
from typing import Any, TypeAlias

from .. import exceptions as exc
from ..const import SZ_ACTIVE_HGI, SZ_IS_EVOFW3
from ..helpers import dt_now
from ..interfaces import ProtocolInterface, TransportInterface
from ..packet import Packet
from ..typing import RamsesProtocolT
from .base import TransportConfig

_LOGGER = logging.getLogger(__name__)

#: Default deduplication window in seconds.  Two packets with the same
#: content key arriving within this window from different child
#: transports are considered duplicates.
_DEFAULT_DEDUP_WINDOW: float = 0.5

#: Maximum number of dedup keys retained in the sliding window.
_MAX_DEDUP_KEYS: int = 512

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

    # -- ProtocolInterface ----------------------------------------------

    def connection_made(
        self, transport: Any, /, *, ramses: bool = False
    ) -> None:
        """Forward connection_made to the pool for tracking."""
        self._connected = True
        self._pool._on_child_connected(self._index, transport)

    def connection_lost(self, error: Exception | None) -> None:
        """Forward connection_lost to the pool for tracking."""
        self._connected = False
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
        """Wait until at least one child connects."""
        return await self._pool._wait_for_any_connection(timeout)

    def set_regex_rules(self, rules: Any) -> None:
        """No-op — regex rules are set on the real protocol by the factory."""


class PooledTransport(TransportInterface):
    """Aggregate multiple physical transports into one interface.

    The pool presents a single :class:`TransportInterface` to the
    protocol/engine layer.  Internally it manages N child transports,
    each created with a :class:`_ChildProtocolProxy` that routes
    inbound packets through the pool's deduplication filter before
    forwarding them to the real protocol.

    Outbound frames are routed to a selected child transport.  The
    selection strategy is round-robin in this PR; RSSI-based selection
    will be added in a subsequent PR (Roadmap Item 9, PR 3).

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
    """

    def __init__(
        self,
        protocol: RamsesProtocolT,
        transports: list[TransportInterface],
        /,
        *,
        config: TransportConfig,
        loop: asyncio.AbstractEventLoop | None = None,
        dedup_window: float = _DEFAULT_DEDUP_WINDOW,
    ) -> None:
        """Initialise the pooled transport."""
        if not transports:
            raise ValueError(
                "PooledTransport requires at least one child transport"
            )

        self._protocol: RamsesProtocolT = protocol
        self._transports: list[TransportInterface] = transports
        self._config: TransportConfig = config
        self._loop: asyncio.AbstractEventLoop = (
            loop or asyncio.get_event_loop()
        )
        self._closing: bool = False

        self._dedup_window: td = td(seconds=dedup_window)
        self._dedup_cache: deque[tuple[dt, _DedupKeyT]] = deque(
            maxlen=_MAX_DEDUP_KEYS
        )

        # Per-child connection state.
        self._child_connected: list[bool] = [False] * len(transports)
        self._child_hgi: list[str | None] = [None] * len(transports)
        self._child_transport_objs: list[Any] = [None] * len(transports)

        # Round-robin outbound counter.
        self._rr_index: int = 0

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
        if name == SZ_ACTIVE_HGI:
            for hgi in self._child_hgi:
                if hgi is not None:
                    return hgi
            return default
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
                "received": list(self._pkts_received),
                "deduped": self._pkts_deduped,
                "forwarded": self._pkts_forwarded,
            }
        return default

    async def send_frame(self, frame: str) -> None:
        """Send a frame via a selected child transport."""
        await self.write_frame(frame)

    async def write_frame(
        self, frame: str, disable_tx_limits: bool = False
    ) -> None:
        """Route an outbound frame to a selected child transport.

        Selection is round-robin among connected children in this PR.
        RSSI-based selection will be added in PR 3.

        :param frame: The raw ASCII frame to transmit.
        :type frame: str
        :param disable_tx_limits: If True, bypass per-child rate
            limiting.  Forwarded to the selected child's
            ``write_frame``.
        :type disable_tx_limits: bool
        """
        child = self._select_transport()
        if child is None:
            raise exc.TransportError(
                "No connected child transport available for send"
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
        real protocol if not a duplicate.
        """
        self._pkts_received[index] += 1

        if self._closing:
            return

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

        # Tag the packet's source HGI for downstream tracking.
        # Packet has __slots__, so we use the _extra dict pattern.
        # The protocol reads SZ_ACTIVE_HGI from the transport, so we
        # set it on the pool for get_extra_info() queries.
        hgi = self._child_hgi[index]
        if hgi is not None:
            # Update the pool's "active" HGI to the source child's HGI.
            # This is a simplification — PR 3 will track per-packet source.
            pass

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

        # Resolve the connection future if waiting.
        if self._conn_fut is not None and not self._conn_fut.done():
            self._conn_fut.set_result(self)

    def _on_child_disconnected(
        self, index: int, error: Exception | None
    ) -> None:
        """Mark a child as disconnected."""
        self._child_connected[index] = False
        self._child_hgi[index] = None

        _LOGGER.info(
            "PooledTransport: child %d disconnected (%s), %d/%d connected",
            index,
            error,
            sum(self._child_connected),
            len(self._transports),
        )

        # If no children are connected, notify the real protocol.
        if not any(self._child_connected) and not self._closing:
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

    def _select_transport(self) -> TransportInterface | None:
        """Select a child transport for outbound transmission.

        Round-robin among connected children.  Returns ``None`` if no
        child is connected.
        """
        connected = [i for i, c in enumerate(self._child_connected) if c]
        if not connected:
            return None

        # Round-robin: advance the counter and pick the next connected
        # child, skipping disconnected ones.
        n = len(self._transports)
        for _ in range(n):
            self._rr_index = (self._rr_index + 1) % n
            if self._rr_index in connected:
                return self._transports[self._rr_index]

        return self._transports[connected[0]]

    # -- Diagnostics -----------------------------------------------------

    def __repr__(self) -> str:
        """Return a diagnostic representation of the pool."""
        return (
            f"PooledTransport(children={len(self._transports)}, "
            f"connected={sum(self._child_connected)})"
        )

    @property
    def is_closing(self) -> bool:
        """Return True if the pool is closing or has closed."""
        return self._closing
