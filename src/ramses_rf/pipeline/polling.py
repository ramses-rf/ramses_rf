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

from ramses_rf.const import DevType
from ramses_rf.helpers import schedule_task
from ramses_rf.typing import DeviceIdT, PollingIntervalsT

if TYPE_CHECKING:
    from ramses_rf.devices.dev_base import DeviceBase
    from ramses_rf.gateway import Gateway

_LOGGER = logging.getLogger(__name__)

# Master default polling schedules table for all device classes.
# Battery-powered devices (TRV, THM, DHW) set intervals to None to explicitly indicate
# that polling is disabled for those command codes.
DEFAULT_POLLING_SCHEDULES: Final[dict[str, dict[str, int | None]]] = {
    DevType.CTL: {"10E0": 86400, "1F41": 3600, "313F": 43200},
    DevType.BDR: {"10E0": 86400},
    DevType.UFC: {"10E0": 86400, "1F09": 3600},
    DevType.FAN: {"10E0": 86400, "3150": 3600},
    DevType.TRV: {"10E0": None, "1060": None},  # Battery device - explicitly disabled
    DevType.THM: {"10E0": None, "1060": None},  # Battery device - explicitly disabled
    DevType.DHW: {"10E0": None, "1060": None},  # Battery device - explicitly disabled
    "DEFAULT": {"10E0": 86400},
}

DEFAULT_POLL_CYCLE_SECS: Final[float] = 300.0  # 5 minutes maximum idle sleep


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
    """

    device_id: DeviceIdT
    code: str
    interval: int
    next_due: dt
    last_polled: dt | None = None
    failures: int = 0


class PollingManager:
    """SSOT-driven polling engine for Layer 7 device entities.

    Evaluates device traits from the schema to determine scheduled command
    polling intervals, falling back to class defaults where necessary.
    Battery-powered devices are strictly excluded from polling.
    """

    def __init__(
        self,
        gwy: Gateway,
        *,
        shadow_mode: bool = True,
        cycle_interval: float = DEFAULT_POLL_CYCLE_SECS,
    ) -> None:
        """Initialize the PollingManager.

        :param gwy: The Gateway instance managing the device registry.
        :type gwy: Gateway
        :param shadow_mode: If True, log schedules without dispatching RF commands.
        :type shadow_mode: bool
        :param cycle_interval: Maximum loop sleep interval in seconds.
        :type cycle_interval: float
        """
        self._gwy = gwy
        self.shadow_mode: bool = shadow_mode
        self._cycle_interval: float = cycle_interval

        self._tasks: dict[tuple[DeviceIdT, str], PollingTask] = {}
        self._poller_task: asyncio.Task[None] | None = None
        self._running: bool = False

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

    def resolve_schedule_for_device(self, device: DeviceBase) -> PollingIntervalsT:
        """Resolve the effective polling schedule for a given device entity.

        Battery-powered devices sleep and do not listen to RF requests; they are
        never polled (returns an empty schedule dictionary).

        For mains-powered devices, combines explicit schema traits
        (`dev.polling_interval`) with fallback defaults for the class (`_SLUG`),
        filtering out any entries set to None.

        :param device: The target device entity.
        :type device: DeviceBase
        :returns: A dictionary mapping active command codes to interval seconds.
        :rtype: PollingIntervalsT
        """
        # Battery devices sleep and cannot receive RF requests; never poll them
        if device.is_battery:
            return {}

        slug = getattr(device, "_SLUG", "DEFAULT")
        fallback_schedule = DEFAULT_POLLING_SCHEDULES.get(
            slug, DEFAULT_POLLING_SCHEDULES["DEFAULT"]
        )

        schedule: dict[str, int | None] = dict(fallback_schedule)

        # Override with explicit SSOT schema traits if provided
        if device.polling_interval is not None:
            schedule.update(device.polling_interval)

        # Filter out disabled entries (None or <= 0) to return active intervals only
        return {
            code: interval
            for code, interval in schedule.items()
            if interval is not None and interval > 0
        }

    def update_device_tasks(self, device: DeviceBase) -> None:
        """Update or register scheduled polling tasks for a device entity.

        :param device: The device entity to register or refresh.
        :type device: DeviceBase
        """
        schedule = self.resolve_schedule_for_device(device)
        now = dt.now(UTC)

        for code, interval in schedule.items():
            key = (device.id, code)
            if key not in self._tasks:
                self._tasks[key] = PollingTask(
                    device_id=device.id,
                    code=code,
                    interval=interval,
                    next_due=now + td(seconds=interval),
                )
            else:
                self._tasks[key].interval = interval

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

        if getattr(self._gwy.config, "disable_polling", False):
            _LOGGER.info("PollingManager: Polling disabled by GatewayConfig.")
            return

        self._running = True
        self._poller_task = schedule_task(self._poll_loop)
        self._poller_task.set_name("l7_polling_manager")
        self._gwy.add_task(self._poller_task)
        _LOGGER.info("PollingManager started (shadow_mode=%s)", self.shadow_mode)

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
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error in PollingManager loop: %s", err)

            sleep_secs = self._calculate_next_sleep_interval()
            await asyncio.sleep(sleep_secs)

    async def poll_due_commands(self) -> int:
        """Evaluate tracked tasks and process commands that are due.

        :returns: Number of tasks processed during this cycle.
        :rtype: int
        """
        if getattr(self._gwy.config, "disable_polling", False):
            return 0

        # Refresh tasks for all devices currently in registry
        for dev in list(self._gwy.device_registry.devices):
            self.update_device_tasks(dev)

        now = dt.now(UTC)
        processed_count = 0

        for task in self._tasks.values():
            if task.next_due > now:
                continue

            processed_count += 1
            if self.shadow_mode:
                _LOGGER.debug(
                    "[SHADOW POLLING] Device %s command %s due (interval=%ss)",
                    task.device_id,
                    task.code,
                    task.interval,
                )
                task.last_polled = now
                task.next_due = now + td(seconds=task.interval)
            else:
                _LOGGER.info("Polling device %s command %s", task.device_id, task.code)
                task.last_polled = now
                task.next_due = now + td(seconds=task.interval)
                # Live dispatch (used in PR 3c execution cutover)

        return processed_count
