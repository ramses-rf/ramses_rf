"""RSSI tracking per HGI (transport layer).

Tracks the last N RSSI readings per device that the HGI (gateway's
CC1101 receiver) has heard.  RSSI is a L1/L2 measurement made by the
receiver, not the sender, so it lives on the transport side.

The tracker maintains a small ring buffer per device (default 5
readings for the pool's route-quality tracker, 3 for the gateway-level
communication-quality tracker).  Readings older than ``ttl`` are
expired automatically on access.  This is intentionally **not** a
running average or EMA — a flat window of the last N fresh readings
is predictable and handles both degradation and recovery cleanly.

See: https://github.com/ramses-rf/ramses_cc/issues/1047
"""

from __future__ import annotations

from collections import deque
from datetime import datetime as dt, timedelta as td
from typing import Final

# Default window size — last N RSSI readings per device.
# A small window (3) balances responsiveness with noise immunity.
DEFAULT_WINDOW_SIZE: Final[int] = 3

# Pool route-quality window: 5 samples for the arithmetic-mean RSSI
# policy (issue 1119, PR 2 spec).
POOL_WINDOW_SIZE: Final[int] = 5

# Default TTL for RSSI readings.  ``None`` means no automatic expiry —
# the gateway's communication-quality tracker retains readings and flags
# them stale via ``stale_warn_seconds`` in ``compute_quality``.
# Pool route-quality trackers override this to 5 minutes (300 s) so stale
# evidence does not influence outbound child selection.
# See: multi-hgi-plan.md, "RSSI TTL — initial default selected".
DEFAULT_TTL: Final[td | None] = None

# Pool route-quality TTL: 5 minutes is conservative enough to capture
# gradual changes while keeping stale evidence from persisting after a
# device moves or a dongle is relocated.
POOL_TTL: Final[td] = td(minutes=5)

# Sentinel RSSI values that carry no signal information.
_RSSI_SENTINELS: Final[frozenset[str]] = frozenset({"", "...", "---", "///"})


class RssiTracker:
    """Per-HGI RSSI tracker maintaining the last N fresh readings.

    Lives on the transport side (one instance per HGI/gateway).  The
    domain layer queries ``best_rssi_for(device_id)`` to compute
    communication quality across all HGIs.

    :param window_size: Number of recent readings to retain per device.
    :param ttl: Maximum age of a reading before it is expired.  Set to
        ``None`` to disable TTL-based expiry.
    """

    __slots__ = ("_window_size", "_ttl", "_readings")

    def __init__(
        self,
        window_size: int = DEFAULT_WINDOW_SIZE,
        *,
        ttl: td | None = DEFAULT_TTL,
    ) -> None:
        """Initialise the tracker.

        :param window_size: Readings to keep per device (default 3).
        :param ttl: Maximum age of readings; older ones are expired
            on access.  ``None`` disables TTL expiry.
        """
        if window_size < 1:
            raise ValueError("window_size must be >= 1")
        self._window_size: int = window_size
        self._ttl: td | None = ttl
        # device_id -> deque of (rssi_dBm, timestamp)
        self._readings: dict[str, deque[tuple[int, dt]]] = {}

    def record(self, device_id: str, rssi: str | int, timestamp: dt) -> None:
        """Record an RSSI reading for a device.

        Silently ignores sentinel values (``...``, ``---``, etc.) and
        unparsable strings — these carry no signal information.

        :param device_id: The device that was heard (source address).
        :param rssi: RSSI value as string (from PacketDTO) or int.
        :param timestamp: When the packet was received.
        """
        dBm = _parse_rssi(rssi)
        if dBm is None:
            return

        buf = self._readings.get(device_id)
        if buf is None:
            buf = deque(maxlen=self._window_size)
            self._readings[device_id] = buf
        buf.append((dBm, timestamp))

    def best_rssi_for(self, device_id: str) -> int | None:
        """Return the strongest (highest) RSSI from fresh readings.

        Expires stale readings (older than ``ttl``) before computing.

        :param device_id: The device to query.
        :returns: Best RSSI in dBm (negative), or ``None`` if no
            fresh readings exist for the device.
        """
        buf = self._readings.get(device_id)
        if not buf:
            return None
        self._expire(buf)
        if not buf:
            del self._readings[device_id]
            return None
        return max(rssi for rssi, _ in buf)

    def last_seen(self, device_id: str) -> dt | None:
        """Return the timestamp of the most recent fresh reading.

        :param device_id: The device to query.
        :returns: Timestamp of last fresh reading, or ``None``.
        """
        buf = self._readings.get(device_id)
        if not buf:
            return None
        self._expire(buf)
        if not buf:
            del self._readings[device_id]
            return None
        return buf[-1][1]

    def readings_for(self, device_id: str) -> list[int]:
        """Return the raw list of recent fresh RSSI readings.

        :param device_id: The device to query.
        :returns: List of recent RSSI values in dBm (oldest first),
            or an empty list.
        """
        buf = self._readings.get(device_id)
        if not buf:
            return []
        self._expire(buf)
        if not buf:
            del self._readings[device_id]
            return []
        return [rssi for rssi, _ in buf]

    def known_devices(self) -> frozenset[str]:
        """Return the set of device IDs that have at least one fresh reading.

        :returns: Frozenset of device IDs with fresh RSSI data.
        """
        if self._ttl is None:
            return frozenset(self._readings)
        # Expire stale entries for all devices.
        now = dt.now()
        fresh: set[str] = set()
        for dev_id, buf in self._readings.items():
            self._expire(buf, now=now)
            if buf:
                fresh.add(dev_id)
            else:
                # Mark for deletion — can't modify dict during iteration.
                pass
        # Remove empty buffers.
        self._readings = {
            dev_id: buf for dev_id, buf in self._readings.items() if buf
        }
        return frozenset(fresh)

    def clear(self) -> None:
        """Clear all recorded readings."""
        self._readings.clear()

    def _expire(
        self, buf: deque[tuple[int, dt]], *, now: dt | None = None
    ) -> None:
        """Remove stale readings from the front of the deque.

        Handles both timezone-aware and timezone-naive timestamps
        gracefully — readings from MQTT transports may carry tzinfo
        while ``dt_now()`` from serial transports does not.

        :param buf: The deque to expire stale entries from.
        :param now: Current time (defaults to ``dt.now()``).
        """
        if self._ttl is None:
            return
        if now is None:
            now = dt.now()
        cutoff = now - self._ttl
        # Strip tzinfo for comparison to handle mixed aware/naive.
        cutoff_naive = cutoff.replace(tzinfo=None) if cutoff.tzinfo else cutoff
        while buf:
            ts = buf[0][1]
            ts_naive = ts.replace(tzinfo=None) if ts.tzinfo else ts
            if ts_naive < cutoff_naive:
                buf.popleft()
            else:
                break


def _parse_rssi(rssi: str | int) -> int | None:
    """Parse an RSSI value to signed dBm int.

    Accepts signed dBm (already normalised by ``packet.py``) as either
    a string (``"-74"``) or int (``-74``).  Sentinel values and
    unparsable strings return ``None``.

    :param rssi: RSSI value from PacketDTO or caller.
    :returns: Signed dBm int, or ``None`` if no signal info.
    """
    if isinstance(rssi, int):
        return rssi
    if not isinstance(rssi, str) or rssi in _RSSI_SENTINELS:
        return None
    try:
        return int(rssi)
    except ValueError:
        return None
