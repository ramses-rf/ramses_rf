"""Tests for the RssiTracker (transport-layer RSSI tracking per HGI).

See: https://github.com/ramses-rf/ramses_cc/issues/1047
"""

from __future__ import annotations

from datetime import datetime as dt, timedelta as td

import pytest

from ramses_tx.rssi_tracker import DEFAULT_TTL, RssiTracker


@pytest.fixture
def now() -> dt:
    """Fixed timestamp for deterministic tests (naive, matching dt_now)."""
    return dt(2026, 1, 1, 12, 0, 0)


class TestRssiTracker:
    """Unit tests for RssiTracker."""

    def test_record_and_best_rssi(self, now: dt) -> None:
        """Basic recording and best_rssi retrieval."""
        tracker = RssiTracker(window_size=3, ttl=None)
        tracker.record("04:123456", "-70", now)
        tracker.record("04:123456", "-65", now)
        tracker.record("04:123456", "-72", now)

        # Best (strongest = highest dBm) of last 3
        assert tracker.best_rssi_for("04:123456") == -65

    def test_window_eviction(self, now: dt) -> None:
        """Old readings are evicted when window is full."""
        tracker = RssiTracker(window_size=3, ttl=None)
        tracker.record("04:123456", "-50", now)
        tracker.record("04:123456", "-60", now)
        tracker.record("04:123456", "-70", now)
        tracker.record("04:123456", "-80", now)

        # Only last 3 readings: -60, -70, -80
        readings = tracker.readings_for("04:123456")
        assert readings == [-60, -70, -80]
        assert tracker.best_rssi_for("04:123456") == -60

    def test_degradation_detection(self, now: dt) -> None:
        """Window detects signal degradation (e.g. door closing)."""
        tracker = RssiTracker(window_size=3, ttl=None)
        # Good signal for a while
        tracker.record("04:123456", "-55", now)
        tracker.record("04:123456", "-56", now)
        tracker.record("04:123456", "-54", now)
        assert tracker.best_rssi_for("04:123456") == -54

        # Door closes — signal degrades
        tracker.record("04:123456", "-90", now)
        tracker.record("04:123456", "-92", now)
        tracker.record("04:123456", "-88", now)
        # Old good readings evicted, now all weak
        assert tracker.best_rssi_for("04:123456") == -88

    def test_recovery_detection(self, now: dt) -> None:
        """Window detects signal recovery (e.g. door opening)."""
        tracker = RssiTracker(window_size=3, ttl=None)
        tracker.record("04:123456", "-90", now)
        tracker.record("04:123456", "-92", now)
        tracker.record("04:123456", "-88", now)
        assert tracker.best_rssi_for("04:123456") == -88

        # Door opens — signal recovers
        tracker.record("04:123456", "-55", now)
        tracker.record("04:123456", "-56", now)
        tracker.record("04:123456", "-54", now)
        assert tracker.best_rssi_for("04:123456") == -54

    def test_multiple_devices(self, now: dt) -> None:
        """Each device has independent tracking."""
        tracker = RssiTracker(window_size=3, ttl=None)
        tracker.record("04:111111", "-70", now)
        tracker.record("04:222222", "-80", now)
        tracker.record("04:111111", "-65", now)

        assert tracker.best_rssi_for("04:111111") == -65
        assert tracker.best_rssi_for("04:222222") == -80
        assert tracker.best_rssi_for("04:333333") is None

    def test_unknown_device(self) -> None:
        """Unknown device returns None."""
        tracker = RssiTracker(ttl=None)
        assert tracker.best_rssi_for("04:999999") is None
        assert tracker.last_seen("04:999999") is None
        assert tracker.readings_for("04:999999") == []

    def test_sentinel_values_ignored(self, now: dt) -> None:
        """Sentinel RSSI values are silently ignored."""
        tracker = RssiTracker(ttl=None)
        tracker.record("04:123456", "...", now)
        tracker.record("04:123456", "---", now)
        tracker.record("04:123456", "///", now)
        tracker.record("04:123456", "", now)

        assert tracker.best_rssi_for("04:123456") is None
        assert tracker.readings_for("04:123456") == []

    def test_unparsable_ignored(self, now: dt) -> None:
        """Unparsable RSSI strings are silently ignored."""
        tracker = RssiTracker(ttl=None)
        tracker.record("04:123456", "abc", now)
        tracker.record("04:123456", "N/A", now)

        assert tracker.best_rssi_for("04:123456") is None

    def test_int_rssi_accepted(self, now: dt) -> None:
        """Integer RSSI values are accepted directly."""
        tracker = RssiTracker(ttl=None)
        tracker.record("04:123456", -70, now)
        tracker.record("04:123456", -65, now)

        assert tracker.best_rssi_for("04:123456") == -65

    def test_last_seen(self, now: dt) -> None:
        """last_seen returns the timestamp of the most recent reading."""
        tracker = RssiTracker(window_size=3, ttl=None)
        t1 = dt(2026, 1, 1, 12, 0, 0)
        t2 = dt(2026, 1, 1, 12, 5, 0)
        t3 = dt(2026, 1, 1, 12, 10, 0)

        tracker.record("04:123456", "-70", t1)
        tracker.record("04:123456", "-65", t2)
        tracker.record("04:123456", "-72", t3)

        assert tracker.last_seen("04:123456") == t3

    def test_known_devices(self, now: dt) -> None:
        """known_devices returns all devices with readings."""
        tracker = RssiTracker(ttl=None)
        tracker.record("04:111111", "-70", now)
        tracker.record("04:222222", "-80", now)

        known = tracker.known_devices()
        assert "04:111111" in known
        assert "04:222222" in known
        assert "04:333333" not in known

    def test_clear(self, now: dt) -> None:
        """clear removes all readings."""
        tracker = RssiTracker(ttl=None)
        tracker.record("04:123456", "-70", now)
        assert tracker.best_rssi_for("04:123456") == -70

        tracker.clear()
        assert tracker.best_rssi_for("04:123456") is None
        assert tracker.known_devices() == frozenset()

    def test_invalid_window_size(self) -> None:
        """Window size < 1 raises ValueError."""
        with pytest.raises(ValueError, match="window_size must be >= 1"):
            RssiTracker(window_size=0)

        with pytest.raises(ValueError, match="window_size must be >= 1"):
            RssiTracker(window_size=-1)

    def test_window_size_1(self, now: dt) -> None:
        """Window size 1 keeps only the latest reading."""
        tracker = RssiTracker(window_size=1, ttl=None)
        tracker.record("04:123456", "-70", now)
        tracker.record("04:123456", "-80", now)

        assert tracker.readings_for("04:123456") == [-80]
        assert tracker.best_rssi_for("04:123456") == -80


class TestRssiTrackerTtl:
    """Tests for TTL-based expiry of RSSI readings."""

    def test_ttl_expires_old_readings(self) -> None:
        """Readings older than TTL are expired on access."""
        tracker = RssiTracker(window_size=5, ttl=td(minutes=5))
        old = dt.now() - td(minutes=6)
        tracker.record("04:123456", "-70", old)

        assert tracker.best_rssi_for("04:123456") is None
        assert tracker.last_seen("04:123456") is None
        assert tracker.readings_for("04:123456") == []

    def test_ttl_keeps_fresh_readings(self) -> None:
        """Readings within TTL are retained."""
        tracker = RssiTracker(window_size=5, ttl=td(minutes=5))
        fresh = dt.now() - td(minutes=1)
        tracker.record("04:123456", "-70", fresh)

        assert tracker.best_rssi_for("04:123456") == -70

    def test_ttl_boundary_just_within(self) -> None:
        """Readings just within TTL are retained."""
        tracker = RssiTracker(window_size=5, ttl=td(minutes=5))
        # 4 min 59 sec ago — just within TTL.
        recent = dt.now() - td(minutes=4, seconds=59)
        tracker.record("04:123456", "-70", recent)

        assert tracker.best_rssi_for("04:123456") == -70

    def test_ttl_boundary_just_expired(self) -> None:
        """Readings just past TTL are expired."""
        tracker = RssiTracker(window_size=5, ttl=td(minutes=5))
        # 5 min 1 sec ago — just past TTL.
        old = dt.now() - td(minutes=5, seconds=1)
        tracker.record("04:123456", "-70", old)

        assert tracker.best_rssi_for("04:123456") is None

    def test_ttl_partial_expiry(self) -> None:
        """Only old readings are expired; fresh ones remain."""
        tracker = RssiTracker(window_size=5, ttl=td(minutes=5))
        old = dt.now() - td(minutes=10)
        fresh = dt.now() - td(minutes=1)

        tracker.record("04:123456", "-90", old)
        tracker.record("04:123456", "-50", fresh)

        # Old reading expired, fresh remains.
        assert tracker.best_rssi_for("04:123456") == -50
        assert tracker.readings_for("04:123456") == [-50]

    def test_ttl_none_disables_expiry(self) -> None:
        """ttl=None disables TTL-based expiry entirely."""
        tracker = RssiTracker(window_size=5, ttl=None)
        very_old = dt(2020, 1, 1, 0, 0, 0)
        tracker.record("04:123456", "-70", very_old)

        assert tracker.best_rssi_for("04:123456") == -70

    def test_ttl_known_devices_excludes_expired(self) -> None:
        """known_devices excludes devices with only expired readings."""
        tracker = RssiTracker(window_size=5, ttl=td(minutes=5))
        old = dt.now() - td(minutes=10)
        fresh = dt.now() - td(minutes=1)

        tracker.record("04:111111", "-70", old)
        tracker.record("04:222222", "-80", fresh)

        known = tracker.known_devices()
        assert "04:111111" not in known
        assert "04:222222" in known

    def test_default_ttl_is_none(self) -> None:
        """DEFAULT_TTL is None (no automatic expiry for gateway trackers)."""
        assert DEFAULT_TTL is None

    def test_ttl_mixed_tz_aware_and_naive(self) -> None:
        """TTL expiry handles mixed tz-aware and naive timestamps.

        Regression: MQTT transports produce tz-aware local datetimes
        while dt_now() from serial transports is naive local.  The
        tracker must not crash when comparing them.
        """
        tracker = RssiTracker(window_size=5, ttl=td(minutes=5))
        # Record with tz-aware local timestamp (as from MQTT transport).
        fresh_aware = dt.now().astimezone() - td(minutes=1)
        tracker.record("04:111111", "-70", fresh_aware)

        # best_rssi_for uses dt.now() (naive) internally — must not crash.
        assert tracker.best_rssi_for("04:111111") == -70

        # Record with naive timestamp (as from serial transport).
        fresh_naive = dt.now() - td(minutes=1)
        tracker.record("04:222222", "-80", fresh_naive)

        # Both should work.
        assert tracker.best_rssi_for("04:222222") == -80

        # Expired tz-aware reading.
        old_aware = dt.now().astimezone() - td(minutes=10)
        tracker.record("04:333333", "-90", old_aware)
        assert tracker.best_rssi_for("04:333333") is None

        # known_devices must also handle mixed timestamps.
        known = tracker.known_devices()
        assert "04:111111" in known
        assert "04:222222" in known
        assert "04:333333" not in known
