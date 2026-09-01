"""Tests for the CommunicationQuality read-model (domain layer).

See: https://github.com/ramses-rf/ramses_cc/issues/1047
"""

from __future__ import annotations

from datetime import UTC, datetime as dt, timedelta as td

from ramses_rf.models.state_signal import (
    RSSI_NORMAL,
    RSSI_STRONG,
    RSSI_WEAK,
    STALE_WARN_SECONDS,
    CommunicationQuality,
    compute_quality,
)
from ramses_tx.rssi_tracker import RssiTracker


class TestComputeQuality:
    """Unit tests for compute_quality."""

    def test_single_hgi_strong_signal(self) -> None:
        """Strong RSSI from one HGI → strong quality."""
        now = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        tracker = RssiTracker()
        tracker.record("04:123456", "-55", now)

        q = compute_quality("04:123456", [tracker], now=now)
        assert q.best_rssi == -55
        assert q.rssi_quality == "strong"
        assert q.is_stale is False
        assert q.staleness_seconds == 0.0
        assert q.last_seen == now

    def test_single_hgi_normal_signal(self) -> None:
        """Normal RSSI → normal quality."""
        now = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        tracker = RssiTracker()
        tracker.record("04:123456", "-70", now)

        q = compute_quality("04:123456", [tracker], now=now)
        assert q.best_rssi == -70
        assert q.rssi_quality == "normal"

    def test_single_hgi_weak_signal(self) -> None:
        """Weak RSSI → weak quality."""
        now = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        tracker = RssiTracker()
        tracker.record("04:123456", "-90", now)

        q = compute_quality("04:123456", [tracker], now=now)
        assert q.best_rssi == -90
        assert q.rssi_quality == "weak"

    def test_single_hgi_very_weak_signal(self) -> None:
        """Very weak RSSI → very_weak quality."""
        now = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        tracker = RssiTracker()
        tracker.record("04:123456", "-110", now)

        q = compute_quality("04:123456", [tracker], now=now)
        assert q.best_rssi == -110
        assert q.rssi_quality == "very_weak"

    def test_no_data(self) -> None:
        """No readings → unknown quality, no staleness."""
        now = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        tracker = RssiTracker()

        q = compute_quality("04:123456", [tracker], now=now)
        assert q.best_rssi is None
        assert q.rssi_quality == "unknown"
        assert q.is_stale is False
        assert q.staleness_seconds is None
        assert q.last_seen is None

    def test_multi_hgi_best_across_all(self) -> None:
        """Best RSSI across multiple HGIs is the strongest."""
        now = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        tracker_a = RssiTracker()
        tracker_b = RssiTracker()

        # HGI A hears device weakly
        tracker_a.record("04:123456", "-90", now)
        tracker_a.record("04:123456", "-88", now)

        # HGI B hears device strongly
        tracker_b.record("04:123456", "-55", now)
        tracker_b.record("04:123456", "-57", now)

        q = compute_quality("04:123456", [tracker_a, tracker_b], now=now)
        assert q.best_rssi == -55  # best across both HGIs
        assert q.rssi_quality == "strong"

    def test_multi_hgi_one_weak_one_strong(self) -> None:
        """One HGI weak, one strong → no warning (best wins)."""
        now = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        tracker_a = RssiTracker()
        tracker_b = RssiTracker()

        tracker_a.record("04:123456", "-100", now)
        tracker_b.record("04:123456", "-60", now)

        q = compute_quality("04:123456", [tracker_a, tracker_b], now=now)
        assert q.best_rssi == -60
        assert q.rssi_quality == "strong"

    def test_multi_hgi_all_weak(self) -> None:
        """All HGIs weak → warning (best is still weak)."""
        now = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        tracker_a = RssiTracker()
        tracker_b = RssiTracker()

        tracker_a.record("04:123456", "-100", now)
        tracker_b.record("04:123456", "-98", now)

        q = compute_quality("04:123456", [tracker_a, tracker_b], now=now)
        assert q.best_rssi == -98
        assert q.rssi_quality == "very_weak"

    def test_staleness_fresh(self) -> None:
        """Recent packet → not stale."""
        now = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        tracker = RssiTracker()
        tracker.record("04:123456", "-70", now)

        q = compute_quality("04:123456", [tracker], now=now)
        assert q.is_stale is False
        assert q.staleness_seconds == 0.0

    def test_staleness_stale(self) -> None:
        """Old packet → stale."""
        now = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        old = now - td(seconds=STALE_WARN_SECONDS + 60)

        tracker = RssiTracker()
        tracker.record("04:123456", "-70", old)

        q = compute_quality("04:123456", [tracker], now=now)
        assert q.is_stale is True
        assert q.staleness_seconds is not None
        assert q.staleness_seconds > STALE_WARN_SECONDS

    def test_staleness_custom_threshold(self) -> None:
        """Custom stale threshold works."""
        now = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        recent = now - td(seconds=60)

        tracker = RssiTracker()
        tracker.record("04:123456", "-70", recent)

        # Default threshold (300s) → not stale
        q = compute_quality("04:123456", [tracker], now=now)
        assert q.is_stale is False

        # Custom threshold (30s) → stale
        q = compute_quality(
            "04:123456", [tracker], now=now, stale_warn_seconds=30
        )
        assert q.is_stale is True

    def test_last_seen_multi_hgi(self) -> None:
        """last_seen is the most recent across all HGIs."""
        t1 = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        t2 = dt(2026, 1, 1, 12, 5, 0, tzinfo=UTC)
        t3 = dt(2026, 1, 1, 12, 10, 0, tzinfo=UTC)

        tracker_a = RssiTracker()
        tracker_b = RssiTracker()

        tracker_a.record("04:123456", "-70", t1)
        tracker_a.record("04:123456", "-72", t3)
        tracker_b.record("04:123456", "-80", t2)

        q = compute_quality("04:123456", [tracker_a, tracker_b], now=t3)
        assert q.last_seen == t3

    def test_immutable_snapshot(self) -> None:
        """CommunicationQuality is frozen."""
        now = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        tracker = RssiTracker()
        tracker.record("04:123456", "-70", now)

        q = compute_quality("04:123456", [tracker], now=now)
        assert isinstance(q, CommunicationQuality)

        # Frozen dataclass — can't set attributes
        import dataclasses

        assert dataclasses.is_dataclass(q)
        # frozen=True raises FrozenInstanceError on setattr
        try:
            q.best_rssi = -50  # type: ignore[misc]
            raise AssertionError("Should have raised FrozenInstanceError")
        except AttributeError:
            pass  # Expected

    def test_empty_trackers(self) -> None:
        """No trackers at all → unknown quality."""
        now = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        q = compute_quality("04:123456", [], now=now)
        assert q.best_rssi is None
        assert q.rssi_quality == "unknown"
        assert q.is_stale is False

    def test_threshold_boundaries(self) -> None:
        """Test exact threshold boundaries."""
        now = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

        # Exactly RSSI_STRONG (-60) → strong
        tracker = RssiTracker()
        tracker.record("04:123456", str(RSSI_STRONG), now)
        q = compute_quality("04:123456", [tracker], now=now)
        assert q.rssi_quality == "strong"

        # RSSI_STRONG - 1 (-61) → normal
        tracker2 = RssiTracker()
        tracker2.record("04:123456", str(RSSI_STRONG - 1), now)
        q2 = compute_quality("04:123456", [tracker2], now=now)
        assert q2.rssi_quality == "normal"

        # Exactly RSSI_NORMAL (-75) → normal
        tracker3 = RssiTracker()
        tracker3.record("04:123456", str(RSSI_NORMAL), now)
        q3 = compute_quality("04:123456", [tracker3], now=now)
        assert q3.rssi_quality == "normal"

        # RSSI_NORMAL - 1 (-76) → weak
        tracker4 = RssiTracker()
        tracker4.record("04:123456", str(RSSI_NORMAL - 1), now)
        q4 = compute_quality("04:123456", [tracker4], now=now)
        assert q4.rssi_quality == "weak"

        # Exactly RSSI_WEAK (-95) → weak
        tracker5 = RssiTracker()
        tracker5.record("04:123456", str(RSSI_WEAK), now)
        q5 = compute_quality("04:123456", [tracker5], now=now)
        assert q5.rssi_quality == "weak"

        # RSSI_WEAK - 1 (-96) → very_weak
        tracker6 = RssiTracker()
        tracker6.record("04:123456", str(RSSI_WEAK - 1), now)
        q6 = compute_quality("04:123456", [tracker6], now=now)
        assert q6.rssi_quality == "very_weak"


class TestTzNaiveTimestamps:
    """Tests for mixing tz-aware and tz-naive timestamps.

    The gateway's RssiTracker records tz-aware timestamps (from
    PacketDTO.dtm), while PooledTransport's per-child trackers use
    dt_now() which returns a tz-naive datetime.  compute_quality
    must normalise these before comparison (issue 1119).
    """

    def test_naive_and_aware_timestamps_no_crash(self) -> None:
        """Mixing naive and aware timestamps must not raise TypeError."""
        now = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        naive_time = dt(2026, 1, 1, 12, 5, 0)  # no tzinfo

        tracker_aware = RssiTracker()
        tracker_naive = RssiTracker()

        tracker_aware.record("04:123456", "-70", now)
        tracker_naive.record("04:123456", "-60", naive_time)

        # Should not raise TypeError
        q = compute_quality(
            "04:123456", [tracker_aware, tracker_naive], now=now
        )
        assert q.best_rssi == -60
        assert q.last_seen is not None

    def test_naive_last_seen_is_normalised_to_utc(self) -> None:
        """Naive timestamps are treated as UTC for comparison."""
        now = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        naive_earlier = dt(2026, 1, 1, 11, 55, 0)  # no tzinfo, 5 min earlier
        aware_later = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

        tracker_a = RssiTracker()
        tracker_b = RssiTracker()

        tracker_a.record("04:123456", "-70", naive_earlier)
        tracker_b.record("04:123456", "-80", aware_later)

        q = compute_quality("04:123456", [tracker_a, tracker_b], now=now)
        # aware_later (12:00 UTC) > naive_earlier (11:55 UTC)
        assert q.last_seen == aware_later

    def test_all_naive_timestamps(self) -> None:
        """All-naive timestamps should work without error."""
        t1 = dt(2026, 1, 1, 12, 0, 0)
        t2 = dt(2026, 1, 1, 12, 5, 0)
        now_aware = t2.replace(tzinfo=UTC)

        tracker_a = RssiTracker()
        tracker_b = RssiTracker()

        tracker_a.record("04:123456", "-70", t1)
        tracker_b.record("04:123456", "-60", t2)

        q = compute_quality("04:123456", [tracker_a, tracker_b], now=now_aware)
        assert q.best_rssi == -60
        assert q.last_seen is not None

    def test_naive_staleness_calculation(self) -> None:
        """Staleness works with naive timestamps (normalised to UTC)."""
        now = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        naive_old = dt(2026, 1, 1, 11, 50, 0)  # 10 min ago, no tz

        tracker = RssiTracker()
        tracker.record("04:123456", "-70", naive_old)

        q = compute_quality("04:123456", [tracker], now=now)
        assert q.is_stale is True
        assert q.staleness_seconds is not None
        assert q.staleness_seconds > 500  # ~600 seconds
