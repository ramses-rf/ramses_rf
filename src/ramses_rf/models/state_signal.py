"""Communication quality read-model (domain layer, per device).

Computes per-device communication quality from transport-layer RSSI
data.  The domain layer queries the HGI-side ``RssiTracker`` to
determine ``best_rssi`` — the strongest recent signal across all
HGIs that have heard the device.

This is a **read model**, not stored state: it is computed on demand
from the transport-layer RSSI trackers.  The domain entity holds a
reference to the tracker(s) and computes quality lazily.

Staleness (time since last transmission) is also tracked here, as it
is a domain concern — the device entity knows when it was last heard.

See: https://github.com/ramses-rf/ramses_cc/issues/1047
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime as dt
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ramses_tx.rssi_tracker import RssiTracker


# RSSI thresholds in dBm (signed, after issue 1046 normalisation).
# These are advisory — callers may use their own thresholds.
RSSI_STRONG: Final[int] = -60  # >= -60 dBm: close to HGI
RSSI_NORMAL: Final[int] = -75  # -60 to -75: typical Evohome distances
RSSI_WEAK: Final[int] = -95  # -75 to -95: weak but usable
# < -95: very weak, likely to miss transmissions

# Staleness thresholds (seconds).
STALE_WARN_SECONDS: Final[int] = 300  # 5 min: no recent transmission
STALE_CRITICAL_SECONDS: Final[int] = 1800  # 30 min: likely comms issue


@dataclass(frozen=True, slots=True)
class CommunicationQuality:
    """Snapshot of a device's communication quality.

    Computed from transport-layer RSSI trackers and the device's last
    message timestamp.  This is an immutable snapshot — create a new
    instance each time quality is evaluated.

    :param best_rssi: Strongest recent RSSI across all HGIs (dBm),
        or ``None`` if no HGI has heard the device.
    :param last_seen: Timestamp of the most recent packet from this
        device across all HGIs, or ``None``.
    :param rssi_quality: Qualitative label: ``"strong"``, ``"normal"``,
        ``"weak"``, ``"very_weak"``, or ``"unknown"``.
    :param is_stale: True if the device has not been heard recently.
    :param staleness_seconds: Seconds since last transmission, or
        ``None`` if never heard.
    """

    best_rssi: int | None
    last_seen: dt | None
    rssi_quality: str
    is_stale: bool
    staleness_seconds: float | None


def _rssi_quality_label(rssi: int | None) -> str:
    """Map an RSSI value to a qualitative label.

    :param rssi: RSSI in dBm (negative), or ``None``.
    :returns: One of ``"strong"``, ``"normal"``, ``"weak"``,
        ``"very_weak"``, or ``"unknown"``.
    """
    if rssi is None:
        return "unknown"
    if rssi >= RSSI_STRONG:
        return "strong"
    if rssi >= RSSI_NORMAL:
        return "normal"
    if rssi >= RSSI_WEAK:
        return "weak"
    return "very_weak"


def compute_quality(
    device_id: str,
    trackers: Sequence[RssiTracker],
    *,
    now: dt | None = None,
    stale_warn_seconds: int = STALE_WARN_SECONDS,
) -> CommunicationQuality:
    """Compute communication quality for a device across all HGIs.

    Queries each HGI's ``RssiTracker`` for the best recent RSSI for
    the device, then takes the best (strongest) across all HGIs.
    Staleness is determined from the most recent timestamp across
    all trackers.

    :param device_id: The device to evaluate.
    :param trackers: RSSI trackers from all active HGIs.
    :param now: Reference timestamp (defaults to current UTC).
    :param stale_warn_seconds: Seconds of silence before staleness
        warning.
    :returns: An immutable ``CommunicationQuality`` snapshot.
    """
    if now is None:
        now = dt.now(UTC)

    best_rssi: int | None = None
    last_seen: dt | None = None

    for tracker in trackers:
        rssi = tracker.best_rssi_for(device_id)
        if rssi is not None:
            if best_rssi is None or rssi > best_rssi:
                best_rssi = rssi

        seen = tracker.last_seen(device_id)
        if seen is not None:
            # Normalise tz-naive timestamps to tz-aware (UTC) so
            # comparisons don't fail when mixing trackers from
            # different layers (gateway uses tz-aware dtm from
            # PacketDTO, pooled transport uses naive dt_now()).
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=UTC)
            if last_seen is None or seen > last_seen:
                last_seen = seen

    staleness_seconds: float | None = None
    is_stale = False
    if last_seen is not None:
        # Handle both tz-aware and tz-naive timestamps
        if last_seen.tzinfo is not None:
            delta = now - last_seen
        else:
            delta = now.replace(tzinfo=None) - last_seen
        staleness_seconds = delta.total_seconds()
        is_stale = staleness_seconds > stale_warn_seconds

    return CommunicationQuality(
        best_rssi=best_rssi,
        last_seen=last_seen,
        rssi_quality=_rssi_quality_label(best_rssi),
        is_stale=is_stale,
        staleness_seconds=staleness_seconds,
    )
