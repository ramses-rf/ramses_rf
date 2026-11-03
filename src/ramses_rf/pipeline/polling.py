"""RAMSES RF - Layer 7 SSOT-Driven Polling Manager.

Orchestrates periodic polling schedules for network devices based on
Layer 7 Schema-as-Source-of-Truth (SSOT) traits and fallback defaults.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime as dt, timedelta as td
from typing import TYPE_CHECKING, Final

from ramses_rf.const import FF, Code, DevType
from ramses_rf.devices.helpers import build_rq_cmd
from ramses_rf.exceptions import RamsesException
from ramses_rf.helpers import schedule_task
from ramses_rf.protocol.opentherm import (
    OPENTHERM_POLL_DATA_IDS,
    encode_opentherm_payload,
)
from ramses_rf.typing import DeviceIdT, PollingIntervalsT

if TYPE_CHECKING:
    from ramses_rf.devices.dev_base import DeviceBase
    from ramses_rf.gateway import Gateway

_LOGGER = logging.getLogger(__name__)

INTERVAL_HOURLY: Final[int] = 3600  # 1 hour in seconds
INTERVAL_EVERY_6_HOURS: Final[int] = 21600  # 6 hours in seconds
INTERVAL_EVERY_12_HOURS: Final[int] = 43200  # 12 hours in seconds
INTERVAL_DAILY: Final[int] = 86400  # 24 hours in seconds
INTERVAL_ZONE_TEMP: Final[int] = 900  # 15 minutes — zone temperature polling
# Keep-alive poll interval for FANs.  Must stay below
# HEARTBEAT_TIMEOUT_FAN (15 min): some FANs broadcast far less often
# than the ~90s cycle the heartbeat assumes (once per ~8h reported), so
# without a reply-generating poll their entities go unavailable
# (ramses-rf/ramses_rf issue 1288).  RP replies stamp device liveness.
INTERVAL_FAN_KEEP_ALIVE: Final[int] = 600  # 10 minutes
# Retry cadence for an unanswered FAN keep-alive poll.  Some FANs answer
# only about half their polls, so a single miss already creates a
# silence longer than HEARTBEAT_TIMEOUT_FAN and their entities flap
# (ramses-rf/ramses_rf issue 1353).  While a previously-heard FAN is
# still available, an unanswered keep-alive is retried at this cadence
# instead of waiting out the full interval.
INTERVAL_FAN_KEEP_ALIVE_RETRY: Final[int] = 120  # 2 minutes
# First-poll delay for the Evohome system mode (2E04).  Cached 2E04
# packets are no longer replayed on restore (a stale mode would be
# presented as authoritative), so the real mode is fetched shortly
# after startup rather than after a full daily interval
# (https://github.com/ramses-rf/ramses_cc/issues/1242).
INTERVAL_STARTUP_2E04: Final[int] = 60  # seconds after task registration
# First-poll delay for every newly-registered task.  Without it a task
# waits a full interval after (re)start, so hourly codes stay silent for
# the first hour and daily codes for a day — polling looks dead after
# every restart, and debugging restarts never reach the first poll
# (ramses-rf/ramses_cc#1335).  Due tasks send serially with
# POLL_INTER_CMD_GAP, so the startup burst self-paces.
INTERVAL_STARTUP_POLL: Final[int] = 120  # seconds after task registration

# Master default polling schedules table for all device classes.
# Battery-powered devices (TRV, THM, DHW, REM, HUM) set intervals to None
# to explicitly indicate that polling is disabled for those command codes.
DEFAULT_POLLING_SCHEDULES: Final[dict[str, dict[Code | str, int | None]]] = {
    # Evohome Controller
    DevType.CTL: {
        Code._10E0: INTERVAL_DAILY,  # Device Specification / Info
        Code._1F41: INTERVAL_HOURLY,  # DHW System Mode / Operating State
        Code._2E04: INTERVAL_DAILY,  # Evohome System Mode
        Code._313F: INTERVAL_EVERY_12_HOURS,  # System Time & Date Sync
        # 1100 (TPI params) — the CTL does not broadcast 1100; it must be
        # polled.  Without polling, tcs.tpi_params returns None (issue 1102
        # / ramses_cc#1026).  12h interval: TPI params are set during
        # installation and rarely change.
        Code._1100: INTERVAL_EVERY_12_HOURS,  # TPI Params
        # 0004 (zone name) is polled per-zone, not per-device — see
        # update_device_tasks for the zone-level expansion.  The interval
        # here is a marker; actual tasks are keyed by (device_id, "0004",
        # zone_index).  Issue 947: zone names lost after cache clear because
        # the CTL does not broadcast 0004 unless queried.
        Code._0004: INTERVAL_EVERY_6_HOURS,  # Zone Name (per-zone, expanded below)
        # 30C9 (zone temperature) is polled per-zone for zones that have no
        # dedicated sensor — see update_device_tasks for the zone-level
        # expansion and sensor filter.  Issue 1013: multi-room zones without
        # a sensor are absent from the CTL's 30C9 broadcast array, so the
        # eavesdropper cannot learn a representative TRV sensor without
        # active polling to supply zone.temperature().
        Code._30C9: INTERVAL_ZONE_TEMP,  # Zone Temperature (per-zone, filtered)
        # 2349 (zone mode) is polled per-zone — the CTL does not broadcast
        # 2349; it sends it only as I directed to an HGI or as RP in
        # response to an RQ.  Without polling, zone.mode() returns None
        # and the zone climate entity cannot display the operating mode
        # (auto / permanent override / temporary override).  The setpoint
        # is also in 2349, but 2309 (which IS broadcast) covers that.
        Code._2349: INTERVAL_ZONE_TEMP,  # Zone Mode (per-zone, all zones)
        # 000C (zone device bindings) is polled per-zone for zones that
        # have no bound sensor — see update_device_tasks for the
        # zone-level expansion.  The CTL never broadcasts 000C; it must
        # be polled.  The role-04 (zone_sensor) reply is the authoritative
        # declaration of a zone's temperature sensor: a broadcast-only
        # sensor (THM/RND) sends no zone-indexed traffic of its own, so
        # without this poll it is never bound and the sensorless zone
        # falls back to the sole actuator's temperature
        # (ramses-rf/ramses_cc#1334, ramses-rf/ramses_cc#1344).  Once a
        # sensor is bound, the task is pruned on the next refresh.
        Code._000C: INTERVAL_EVERY_6_HOURS,  # Zone Sensor Binding (per-zone, sensorless only)
    },
    # Boiler Relay / Switch
    DevType.BDR: {
        Code._10E0: INTERVAL_DAILY,  # Device Specification / Info
    },
    # OpenTherm Bridge
    DevType.OTB: {
        Code._10E0: INTERVAL_DAILY,  # Device Specification / Info
        Code._3EF0: INTERVAL_HOURLY,  # OpenTherm Modulation State
        Code._3220: INTERVAL_HOURLY,  # OpenTherm Data ID Query
    },
    # Underfloor Heating Controller
    DevType.UFC: {
        Code._10E0: INTERVAL_DAILY,  # Device Specification / Info
        Code._1F09: INTERVAL_HOURLY,  # UFH Controller Status
    },
    # HVAC Ventilation Fan
    DevType.FAN: {
        Code._10E0: INTERVAL_FAN_KEEP_ALIVE,  # Device Info — liveness poll
        Code._10D0: INTERVAL_DAILY,  # Filter — re-probe until supported
        # Once a FAN has confirmed 10D0 support, the roles swap: 10D0
        # replies carry both filter data and liveness at keep-alive rate,
        # and 10E0 drops back to daily — see resolve_schedule_for_device.
        # 3150 (fan speed / heat demand) is NOT polled — FANs broadcast
        # I/3150 unsolicited but do not respond to RQ/3150.  Polling them
        # generates "Unexpected verb/code for dst (FAN) to Rx" warnings
        # (issue 1187).
    },
    # HVAC Carbon Dioxide Sensor (mains-powered)
    DevType.CO2: {
        Code._10E0: INTERVAL_DAILY,  # Device Specification / Info
    },
    # Battery-Powered Devices - Polling disabled to preserve battery life
    DevType.TRV: {Code._10E0: None, Code._1060: None},
    DevType.THM: {Code._10E0: None, Code._1060: None},
    DevType.DHW: {Code._10E0: None, Code._1060: None},
    DevType.REM: {Code._10E0: None, Code._1060: None},
    DevType.HUM: {Code._10E0: None, Code._1060: None},
    # Fallback Default
    "DEFAULT": {
        Code._10E0: INTERVAL_DAILY,  # Device Specification / Info
    },
}

POLL_INTER_CMD_GAP: Final[float] = (
    0.5  # Rate limit gap between consecutive TX commands
)
DEFAULT_POLL_CYCLE_SECS: Final[float] = 300.0  # 5 minutes maximum idle sleep
POLL_REPLY_TIMEOUT: Final[float] = (
    300.0  # Poll counts as missed if the device stays silent this long
)


def _ensure_aware(dtm: dt) -> dt:
    """Ensure a datetime object is timezone-aware, defaulting to UTC if naive.

    :param dtm: The input datetime instance.
    :type dtm: dt
    :returns: A timezone-aware datetime instance.
    :rtype: dt
    """
    if dtm.tzinfo is None:
        return dtm.replace(tzinfo=UTC)
    return dtm


@dataclass
class PollingTask:
    """Represents a scheduled polling task for a device command.

    :param device_id: The ID of the target device.
    :type device_id: DeviceIdT
    :param code: The command code string (e.g., '10E0').
    :type code: str
    :param interval: Interval between polls in seconds.
    :type interval: int
    :param next_due: Datetime when the next poll is scheduled.
    :type next_due: dt
    :param last_polled: Datetime when the last poll occurred.
    :type last_polled: dt | None
    :param failures: Count of consecutive polling failures.
    :type failures: int
    :param last_missed_at: Datetime of the last poll counted as missed.
    :type last_missed_at: dt | None
    :param payload: Optional payload suffix (e.g., zone_index for 0004).
    :type payload: str | None
    :param retry_interval: Cadence (seconds) for re-polling while a poll
        remains unanswered and the device is still available, or None
        for no retries (default).
    :type retry_interval: int | None
    """

    device_id: DeviceIdT
    code: str
    interval: int
    next_due: dt
    last_polled: dt | None = None
    failures: int = 0
    last_missed_at: dt | None = None
    payload: str | None = None
    retry_interval: int | None = None


class PollingManager:
    """SSOT-driven polling engine for Layer 7 device entities.

    Evaluates device traits from the schema to determine scheduled command
    polling intervals, falling back to class defaults where necessary.
    Battery-powered devices are strictly excluded from polling.
    """

    def __init__(
        self,
        gateway: Gateway,
        *,
        shadow_mode: bool = True,
        cycle_interval: float = DEFAULT_POLL_CYCLE_SECS,
    ) -> None:
        """Initialize the PollingManager.

        :param gateway: The Gateway instance managing the device registry.
        :type gateway: Gateway
        :param shadow_mode: If True, log schedules without dispatching RF commands.
        :type shadow_mode: bool
        :param cycle_interval: Maximum loop sleep interval in seconds.
        :type cycle_interval: float
        """
        self._gateway = gateway
        self.shadow_mode: bool = shadow_mode
        self._cycle_interval: float = cycle_interval

        # Keys are (device_id, code) for device-level tasks, or
        # (device_id, code, zone_index) for zone-level tasks (e.g., 0004).
        self._tasks: dict[tuple[str, ...], PollingTask] = {}
        self._poller_task: asyncio.Task[object] | None = None
        self._running: bool = False
        # Set when tasks are added/rescheduled so a sleeping poll loop
        # re-evaluates immediately instead of waiting out its old sleep.
        self._wake_event = asyncio.Event()

    @property
    def is_running(self) -> bool:
        """Return True if the polling loop is active.

        :returns: Active running status of the polling engine.
        :rtype: bool
        """
        return (
            self._running
            and self._poller_task is not None
            and not self._poller_task.done()
        )

    @staticmethod
    def resolve_schedule_for_device(device: DeviceBase) -> PollingIntervalsT:
        """Resolve the effective polling schedule for a given device.

        Combines default device schedule tables with SSOT schema trait overrides,
        ensuring battery-powered and faked devices always resolve to zero polling.

        :param device: The target device instance.
        :type device: DeviceBase
        :returns: A dictionary mapping active command codes to interval seconds.
        :rtype: PollingIntervalsT
        """
        slug = getattr(device, "_SLUG", "DEFAULT")
        if slug == DevType.HGI:
            return {}

        # Faked devices are virtual — they don't exist on the RF network,
        # so polling them only generates timeouts and log spam (e.g. a
        # faked REM created via ramses_cc's add_faked_rem service).
        if device.is_faked:
            return {}

        # Battery devices sleep and cannot receive RF requests; never poll them
        if device.is_battery:
            return {}

        fallback_schedule = DEFAULT_POLLING_SCHEDULES.get(
            slug, DEFAULT_POLLING_SCHEDULES["DEFAULT"]
        )

        schedule: dict[str, int | None] = dict(fallback_schedule)

        # Override with explicit SSOT schema traits if provided
        if device.polling_interval is not None:
            schedule.update(device.polling_interval)

        # FAN keep-alive role swap (issue 1288): until the device confirms
        # 10D0 support, the universally-answered 10E0 provides liveness at
        # keep-alive rate and 10D0 only re-probes daily.  Once supported,
        # 10D0 replies carry liveness *and* filter data, so 10E0 drops back
        # to a daily refresh.  Explicit per-device overrides win either way.
        user_intervals = device.polling_interval or {}
        if slug == DevType.FAN and getattr(device, "supports_10d0", False):
            if Code._10E0 not in user_intervals:
                schedule[Code._10E0] = INTERVAL_DAILY
            if Code._10D0 not in user_intervals:
                schedule[Code._10D0] = INTERVAL_FAN_KEEP_ALIVE

        # Filter out disabled entries (None or <= 0) to return active intervals only
        return {
            code: interval
            for code, interval in schedule.items()
            if interval is not None and interval > 0
        }

    def update_device_tasks(self, device: DeviceBase) -> set[tuple[str, ...]]:
        """Update or register scheduled polling tasks for a device entity.

        For CTL devices with zones, the ``0004`` (zone name) code is
        expanded into one task per zone, keyed by
        ``(device_id, "0004", zone_index)``.  This restores the per-zone
        zone-name polling that was lost when the pre-0.60 DiscoveryService
        was removed (issue 947 / ramses-rf/ramses_cc#947).

        For CTL devices with zones, the ``2349`` (zone mode) code is
        expanded into one task per zone, keyed by
        ``(device_id, "2349", zone_index)``.  The CTL does not broadcast
        2349 — it sends it only as I directed to an HGI or as RP in
        response to an RQ.  Without polling, ``zone.mode()`` returns None
        and the zone climate entity cannot display the operating mode.

        For CTL devices with zones, the ``30C9`` (zone temperature) code is
        expanded into one task per zone **that has no dedicated sensor**,
        keyed by ``(device_id, "30C9", zone_index)``.  This restores the
        per-zone temperature polling that was lost when the pre-0.60
        DiscoveryService was removed (issue 1013).  Multi-room zones
        without a sensor are absent from the CTL's 30C9 broadcast array,
        so the eavesdropper cannot learn a representative TRV sensor
        without active polling to supply ``zone.temperature()``.  Once a
        sensor is learned, the task is pruned on the next refresh — the
        CTL's broadcast array covers that zone again.

        For CTL devices with zones, the ``000C`` (zone device bindings)
        code is expanded into one task per zone **that has no bound
        sensor**, keyed by ``(device_id, "000C", zone_index)`` with
        payload ``<zone_index>04`` (role 04 = zone_sensor).  The binding
        table is the authoritative declaration of a zone's temperature
        sensor — a broadcast-only sensor (THM/RND) never acquires a
        zone_index from its own traffic, so without this probe it stays
        an orphan and the zone keeps taking its temperature from the
        sole actuator (ramses-rf/ramses_cc#1334, ramses-rf/ramses_cc#1344).
        Once a sensor binds, the task is pruned on the next refresh.

        For OTB devices, the ``3220`` (OpenTherm Data ID) code is
        expanded into individual tasks per Data ID, keyed by
        ``(device_id, "3220", data_id_hex)``.  This queries status
        and parameter Data IDs with valid parity-encoded 5-byte payloads
        (ramses-rf/ramses_cc#975).

        :param device: The device entity to register or refresh.
        :type device: DeviceBase
        :returns: Set of active task keys for the device.
        :rtype: set[tuple[str, ...]]
        """
        schedule = self.resolve_schedule_for_device(device)
        now = dt.now(UTC)
        active_keys: set[tuple[str, ...]] = set()

        # Collect zone indices for per-zone code expansion (0004, 000C,
        # 2349, 30C9).  CTL devices have a .tcs attribute with .zones list.
        tcs = getattr(device, "tcs", None)
        zone_indexs: list[str] = []
        if (
            Code._0004 in schedule
            or Code._000C in schedule
            or Code._2349 in schedule
            or Code._30C9 in schedule
        ) and tcs is not None:
            zones = getattr(tcs, "zones", [])
            zone_indexs = [z.index for z in zones if hasattr(z, "index")]

        # For 30C9, only poll zones that have no dedicated sensor — zones
        # with a sensor receive their temperature via the CTL's 30C9
        # broadcast array.  Multi-room zones (no sensor) are absent from
        # that array, so we must poll to enable eavesdropper sensor learning.
        # 000C uses the same filter: the role-04 probe exists to discover a
        # sensor the schema doesn't know about, so a zone that already has
        # one doesn't need it.
        zone_indexs_no_sensor: list[str] = []
        if (
            Code._30C9 in schedule or Code._000C in schedule
        ) and tcs is not None:
            zone_by_index = getattr(tcs, "zone_by_index", {})
            for zi in zone_indexs:
                zone = zone_by_index.get(zi)
                if (
                    zone is not None
                    and getattr(zone, "sensor", None) is not None
                ):
                    continue  # zone has a sensor — CTL broadcasts its temp
                zone_indexs_no_sensor.append(zi)

        slug = getattr(device, "_SLUG", None)

        for code, interval in schedule.items():
            if code == Code._0004 and zone_indexs:
                # Expand into per-zone tasks
                for zone_index in zone_indexs:
                    zkey = (device.id, code, zone_index)
                    active_keys.add(zkey)
                    payload = f"{zone_index}00"
                    if zkey not in self._tasks:
                        self._tasks[zkey] = PollingTask(
                            device_id=device.id,
                            code=code,
                            interval=interval,
                            next_due=now
                            + td(seconds=min(interval, INTERVAL_STARTUP_POLL)),
                            payload=payload,
                        )
                        self._wake_event.set()
                    else:
                        task = self._tasks[zkey]
                        if task.interval != interval:
                            task.next_due = now + td(seconds=interval)
                            task.interval = interval
                            self._wake_event.set()
                        task.payload = payload
            elif code == Code._2349 and zone_indexs:
                # Expand into per-zone tasks (all zones — mode is needed
                # regardless of whether the zone has a sensor).  2349 RQ
                # payload is 2 bytes (zone_index + "00"), same as 0004.
                for zone_index in zone_indexs:
                    zkey = (device.id, code, zone_index)
                    active_keys.add(zkey)
                    payload = f"{zone_index}00"
                    if zkey not in self._tasks:
                        self._tasks[zkey] = PollingTask(
                            device_id=device.id,
                            code=code,
                            interval=interval,
                            next_due=now
                            + td(seconds=min(interval, INTERVAL_STARTUP_POLL)),
                            payload=payload,
                        )
                        self._wake_event.set()
                    else:
                        task = self._tasks[zkey]
                        if task.interval != interval:
                            task.next_due = now + td(seconds=interval)
                            task.interval = interval
                            self._wake_event.set()
                        task.payload = payload
            elif code == Code._30C9 and zone_indexs_no_sensor:
                # Expand into per-zone tasks (only zones without a sensor)
                for zone_index in zone_indexs_no_sensor:
                    zkey = (device.id, code, zone_index)
                    active_keys.add(zkey)
                    payload = f"{zone_index}"  # 30C9 RQ payload is 1 byte
                    if zkey not in self._tasks:
                        self._tasks[zkey] = PollingTask(
                            device_id=device.id,
                            code=code,
                            interval=interval,
                            next_due=now
                            + td(seconds=min(interval, INTERVAL_STARTUP_POLL)),
                            payload=payload,
                        )
                        self._wake_event.set()
                    else:
                        task = self._tasks[zkey]
                        if task.interval != interval:
                            task.next_due = now + td(seconds=interval)
                            task.interval = interval
                            self._wake_event.set()
                        task.payload = payload
            elif code == Code._000C:
                # Expand into per-zone tasks (only zones without a sensor).
                # The 000C RQ payload is 2 bytes: zone_index + device_role
                # "04" (zone_sensor) — the binding-table query that returns
                # the zone's authoritative temperature sensor.  A zone that
                # already has a sensor doesn't need the probe; a CTL with
                # no zones never gets a device-level 000C task (a bare
                # "00" payload is not a valid role query).
                for zone_index in zone_indexs_no_sensor:
                    zkey = (device.id, code, zone_index)
                    active_keys.add(zkey)
                    payload = f"{zone_index}04"
                    if zkey not in self._tasks:
                        self._tasks[zkey] = PollingTask(
                            device_id=device.id,
                            code=code,
                            interval=interval,
                            next_due=now
                            + td(seconds=min(interval, INTERVAL_STARTUP_POLL)),
                            payload=payload,
                        )
                        self._wake_event.set()
                    else:
                        task = self._tasks[zkey]
                        if task.interval != interval:
                            task.next_due = now + td(seconds=interval)
                            task.interval = interval
                            self._wake_event.set()
                        task.payload = payload
            elif code == Code._3220 and slug == DevType.OTB:
                # Expand into per-Data-ID OpenTherm query tasks
                for data_id in OPENTHERM_POLL_DATA_IDS:
                    ot_key = (device.id, code, f"{data_id:02X}")
                    active_keys.add(ot_key)
                    payload = encode_opentherm_payload(data_id)
                    if ot_key not in self._tasks:
                        self._tasks[ot_key] = PollingTask(
                            device_id=device.id,
                            code=code,
                            interval=interval,
                            next_due=now
                            + td(seconds=min(interval, INTERVAL_STARTUP_POLL)),
                            payload=payload,
                        )
                        self._wake_event.set()
                    else:
                        task = self._tasks[ot_key]
                        if task.interval != interval:
                            task.next_due = now + td(seconds=interval)
                            task.interval = interval
                            self._wake_event.set()
                        task.payload = payload
            else:
                dkey = (device.id, code)
                active_keys.add(dkey)
                # FAN keep-alive tasks get a retry cadence so a single
                # unanswered poll cannot flap the device (issue 1353).
                retry_secs = (
                    INTERVAL_FAN_KEEP_ALIVE_RETRY
                    if slug == DevType.FAN
                    and interval == INTERVAL_FAN_KEEP_ALIVE
                    else None
                )
                if dkey not in self._tasks:
                    # 2E04 gets an early first poll so the real system
                    # mode is hydrated shortly after startup instead of
                    # after a full daily interval (issue
                    # ramses-rf/ramses_cc#1242).  Its RQ payload must be
                    # FF — the canonical payload used by
                    # build_get_system_mode and seen in all real-world
                    # RQ|2E04 captures; the generic "00" fallback may
                    # not be answered by every controller
                    # (ramses-rf/ramses_cc#1323).
                    delay = (
                        INTERVAL_STARTUP_2E04
                        if code == Code._2E04
                        else min(interval, INTERVAL_STARTUP_POLL)
                    )
                    self._tasks[dkey] = PollingTask(
                        device_id=device.id,
                        code=code,
                        interval=interval,
                        next_due=now + td(seconds=delay),
                        payload=FF if code == Code._2E04 else None,
                        retry_interval=retry_secs,
                    )
                    self._wake_event.set()
                else:
                    task = self._tasks[dkey]
                    task.retry_interval = retry_secs
                    if task.interval != interval:
                        # Reschedule from now so the new cadence applies
                        # at once — e.g. the 10E0/10D0 keep-alive swap
                        # when supports_10d0 is confirmed (issue 1288),
                        # or a user polling_interval override.
                        task.next_due = now + td(seconds=interval)
                        task.interval = interval
                        self._wake_event.set()

        return active_keys

    def get_scheduled_cmds(self) -> list[PollingTask]:
        """Return a list of all currently tracked polling tasks.

        :returns: A list of active PollingTask objects.
        :rtype: list[PollingTask]
        """
        return list(self._tasks.values())

    def start(self) -> None:
        """Start the background polling loop if not disabled in config."""
        if self._running:
            return

        if getattr(self._gateway.config, "disable_polling", False):
            _LOGGER.info("PollingManager: Polling disabled by GatewayConfig.")
            return

        self._running = True
        self._poller_task = schedule_task(self._poll_loop)
        self._poller_task.set_name("l7_polling_manager")
        self._gateway.add_task(self._poller_task)
        _LOGGER.info(
            "PollingManager started (shadow_mode=%s)", self.shadow_mode
        )

    async def stop(self) -> None:
        """Stop the background polling loop gracefully."""
        self._running = False
        if self._poller_task and not self._poller_task.done():
            self._poller_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poller_task
        self._poller_task = None
        _LOGGER.info("PollingManager stopped.")

    def _calculate_next_sleep_interval(self) -> float:
        """Calculate dynamic sleep delay in seconds based on next due task.

        :returns: Dynamic sleep interval in seconds bounded between 1s and cycle_interval.
        :rtype: float
        """
        if not self._tasks:
            return self._cycle_interval

        now = dt.now(UTC)
        next_due = min(task.next_due for task in self._tasks.values())
        delay = (next_due - now).total_seconds()
        return max(1.0, min(delay, self._cycle_interval))

    async def _poll_loop(self) -> None:
        """Evaluate and execute scheduled tasks in the background."""
        while self._running:
            try:
                await self.poll_due_commands()
            except (RamsesException, TimeoutError) as err:
                _LOGGER.error("Error in PollingManager loop: %s", err)

            # Clear before computing the sleep so a concurrent schedule
            # update is either reflected in the computed interval or
            # leaves the event set for the wait below — no lost wake-up.
            self._wake_event.clear()
            sleep_secs = self._calculate_next_sleep_interval()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wake_event.wait(), timeout=sleep_secs
                )

    async def poll_due_commands(self) -> int:
        """Evaluate tracked tasks and process commands that are due.

        :returns: Number of tasks processed during this cycle.
        :rtype: int
        """
        if getattr(self._gateway.config, "disable_polling", False):
            return 0

        # Refresh tasks for all devices currently in registry and prune stale tasks
        active_keys: set[tuple[str, ...]] = set()
        for device in list(self._gateway.device_registry.devices):
            active_keys.update(self.update_device_tasks(device))

        for key in list(self._tasks):
            if key not in active_keys:
                del self._tasks[key]

        now = dt.now(UTC)

        # Count unanswered polls.  A poll is missed once POLL_REPLY_TIMEOUT
        # has elapsed without the device transmitting; any message from the
        # device (broadcast or reply) clears the count via state_projector.
        if not self.shadow_mode:
            for task in self._tasks.values():
                if (
                    task.last_polled is None
                    or task.last_missed_at == task.last_polled
                    or (now - task.last_polled).total_seconds()
                    < POLL_REPLY_TIMEOUT
                ):
                    continue
                task.last_missed_at = task.last_polled
                dev = self._gateway.device_registry.device_by_id.get(
                    task.device_id
                )
                last_seen = getattr(dev, "_last_msg_dtm", None)
                if not isinstance(last_seen, dt) or (
                    last_seen <= task.last_polled
                ):
                    task.failures += 1
                    if dev is not None and hasattr(dev, "_missed_polls"):
                        dev._missed_polls += 1
                        _LOGGER.debug(
                            "Poll %s to %s unanswered (missed=%s)",
                            task.code,
                            task.device_id,
                            dev._missed_polls,
                        )

            # Retry unanswered keep-alive polls at their (shorter) retry
            # cadence instead of waiting out the full interval: a FAN
            # that answers only some of its polls would otherwise go
            # silent for longer than HEARTBEAT_TIMEOUT_FAN on a single
            # miss and flap to unavailable (ramses-rf/ramses_rf issue
            # 1353).  Retries only run while a previously-heard device
            # is still available; once it answers (or is unavailable
            # anyway) the regular interval resumes.  Because retries
            # refresh last_polled within POLL_REPLY_TIMEOUT, they are
            # not themselves counted as missed polls.
            for task in self._tasks.values():
                if task.retry_interval is None or task.last_polled is None:
                    continue
                dev = self._gateway.device_registry.device_by_id.get(
                    task.device_id
                )
                last_seen = getattr(dev, "_last_msg_dtm", None)
                if not isinstance(last_seen, dt):
                    continue  # never heard — keep the regular cadence
                if last_seen > task.last_polled:
                    # answered since the last poll: restore the regular
                    # cadence if a retry had pulled next_due forward
                    due = task.last_polled + td(seconds=task.interval)
                    if task.next_due < due:
                        task.next_due = due
                elif getattr(dev, "is_available", False):
                    due = task.last_polled + td(seconds=task.retry_interval)
                    if task.next_due > due:
                        task.next_due = due
                        self._wake_event.set()

        processed_count = 0

        for task in list(self._tasks.values()):
            if task.next_due > now:
                continue

            if processed_count > 0 and not self.shadow_mode:
                await asyncio.sleep(POLL_INTER_CMD_GAP)

            processed_count += 1
            if self.shadow_mode:
                _LOGGER.debug(
                    "[SHADOW POLLING] Device %s command %s due (interval=%ss, payload=%s)",
                    task.device_id,
                    task.code,
                    task.interval,
                    task.payload or "00",
                )
                task.last_polled = now
                task.next_due = now + td(seconds=task.interval)
            else:
                _LOGGER.info(
                    "Polling device %s command %s", task.device_id, task.code
                )
                task.last_polled = now
                task.next_due = now + td(seconds=task.interval)
                cmd_dto = build_rq_cmd(
                    task.device_id, task.code, payload=task.payload or "00"
                )
                try:
                    await self._gateway._async_send_dto(cmd_dto)
                except (RamsesException, TimeoutError) as err:
                    _LOGGER.warning(
                        "PollingManager failed to send command %s to %s: %s",
                        task.code,
                        task.device_id,
                        err,
                    )

        return processed_count
