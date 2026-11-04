#!/usr/bin/env python3
"""RAMSES RF - The evohome-compatible system."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime as dt, timedelta as td
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, TypeVar

from ramses_rf.address import HGI_DEV_ADDR, Address
from ramses_rf.commands.core import Command as Intent_
from ramses_rf.config import SZ_CLASS
from ramses_rf.const import (
    DEV_TYPE_MAP,
    SYS_MODE_MAP,
    SZ_ACTUATORS,
    SZ_CHANGE_COUNTER,
    SZ_DATETIME,
    SZ_DEVICES,
    SZ_LANGUAGE,
    SZ_SENSOR,
    SZ_SYSTEM_MODE,
    SZ_ZONES,
    DevType,
)
from ramses_rf.devices import (
    BdrSwitch,
    Controller,
    Device,
    OtbGateway,
    UfhController,
)
from ramses_rf.entity import Entity, class_by_attr
from ramses_rf.enums import Action, ThermalMode
from ramses_rf.exceptions import (
    DeviceNotFoundError,
    SchemaInconsistentError,
    SystemSchemaInconsistent,
)
from ramses_rf.helpers import shrink
from ramses_rf.models import DemandState, SystemState, ThermalDemandDTO
from ramses_rf.schemas import (
    SCH_TCS,
    SCH_TCS_DHW,
    SCH_TCS_ZONES_ZON,
    SZ_APPLIANCE_CONTROL,
    SZ_DHW_SYSTEM,
    SZ_MAX_ZONES,
    SZ_ORPHANS,
    SZ_SYSTEM,
    SZ_UFH_SYSTEM,
)
from ramses_rf.topology import Parent
from ramses_rf.typing import SchemaT
from ramses_tx import DeviceIdT, Priority
from ramses_tx.const import DEFAULT_MAX_ZONES
from ramses_tx.typing import PayDictT

from ..messages import Message
from .faultlog import FaultLog
from .helpers import send_system_intent
from .zones import DhwZone, Zone, zone_factory

if TYPE_CHECKING:
    from ramses_rf.address import Address

    from .faultlog import FaultIdxT, FaultLogEntry


from ramses_rf.const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    F9,
    FA,
    FC,
    FF,
)

from ramses_rf.const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    HEARTBEAT_TIMEOUT_DHW,
    I_,
    RP,
    RQ,
    W_,
    Code,
)


_LOGGER = logging.getLogger(__name__)
_TRACE = logging.getLogger("ramses_rf.legacy_trace")

# Polling interval for dormant DHW (Domestic Hot Water) entities.
# Dormant entities, particularly battery-powered DHW sensors (e.g. CS92A),
# change state infrequently and may remain 'Unknown' after boot. We
# explicitly poll their state to hydrate the system. To preserve the
# battery life of wireless sensors, this interval defaults to 24 hours.
# Users can decrease this value if more frequent updates are desired.
# Kept as seconds (int) for backward compat; HEARTBEAT_TIMEOUT_DHW is the
# timedelta equivalent used by DhwSensor.heartbeat_timeout.
DHW_POLLING_INTERVAL_SECS: int = int(HEARTBEAT_TIMEOUT_DHW.total_seconds())


_SystemT = TypeVar("_SystemT", bound="Evohome")

_StoredHwT = TypeVar("_StoredHwT", bound="StoredHw")
_LogbookT = TypeVar("_LogbookT", bound="Logbook")
_MultiZoneT = TypeVar("_MultiZoneT", bound="MultiZone")


SYS_KLASS = SimpleNamespace(
    SYS="system",  # Generic (promotable?) system
    TCS="evohome",
    PRG="programmer",
)


class SystemBase(Parent[Device], Entity):  # 3B00 (multi-relay)
    """The TCS base class orchestrating system-level operations."""

    _SLUG: str | None = None

    childs: list[Device]

    # Populated by the CQRS state projector (issue 1102).  Declared here
    # because tpi_params property is on SystemBase but the dict is only
    # initialised in System.__init__.
    _tpi_params: SchemaT = {}

    def __init__(self, controller: Controller) -> None:
        """Initialise the TCS base class.

        :param controller: The central controller device for this system.
        :type controller: Controller
        """
        _LOGGER.debug(
            "Creating a TCS for CTL: %s (%s)", controller.id, self.__class__
        )

        if controller.id in controller._gateway.device_registry.system_by_id:
            raise SchemaInconsistentError(
                f"Duplicate TCS for CTL: {controller.id}"
            )
        if not isinstance(controller, Controller):  # TODO
            raise SchemaInconsistentError(
                f"Invalid CTL: {controller} (is not a controller)"
            )

        super().__init__(controller._gateway)

        # FIXME: ZZZ entities must know their parent device ID and their own index
        self._z_id = controller.id  # the responsible device is the controller
        self._z_index = None  # ? True (sentinel value to pick up arrays?)

        self.id: DeviceIdT = controller.id

        self.ctl: Controller = controller
        self.tcs = self
        self._child_id = FF  # NOTE: domain_id

        self._app_cntrl: BdrSwitch | OtbGateway | None = None

        self.system_state = SystemState()
        self.demand_state = DemandState()

    def __repr__(self) -> str:
        """Return the string representation of the system."""
        return f"{self.ctl.id} ({self._SLUG})"

    @property
    def appliance_control(self) -> BdrSwitch | OtbGateway | None:
        """The TCS relay, aka 'appliance control' (BDR or OTB)."""
        if self._app_cntrl:
            return self._app_cntrl
        app_cntrl = [
            d for d in self.childs if isinstance(d, (BdrSwitch, OtbGateway))
        ]
        return app_cntrl[0] if len(app_cntrl) == 1 else None

    async def tpi_params(self) -> PayDictT._1100 | None:  # 1100
        """Return the TPI parameters for the system.

        :returns: The TPI parameters dictionary, if available.
        :rtype: PayDictT._1100 | None
        """
        # Read from the CQRS-populated dict (issue 1102 / ramses_cc#1026).
        # The pre-0.60 entity_state.get_value(Code._1100) path is deprecated.
        if self._tpi_params:
            for params in self._tpi_params.values():
                return {
                    "cycle_rate": params.get("cycle_rate"),
                    "min_on_time": params.get("min_on_time"),
                    "min_off_time": params.get("min_off_time"),
                    "proportional_band_width": params.get(
                        "proportional_band_width"
                    ),
                }
        return None

    async def heat_demand(self) -> float | None:  # 3150/FC
        """Return the current heat demand for the system.

        :returns: The heat demand fraction, or None if unknown.
        :rtype: float | None
        """
        return self.demand_state.heat_demand

    async def thermal_mode(self) -> ThermalMode | None:  # 2D49
        """Return the current thermal operating mode of the system.

        :returns: ThermalMode.COOL if in cooling mode, ThermalMode.HEAT if heating,
            or None if unhydrated.
        :rtype: ThermalMode | None
        """
        if self.system_state.cooling_mode is True:
            return ThermalMode.COOL
        if self.system_state.cooling_mode is False:
            return ThermalMode.HEAT
        return None

    async def cooling_mode(self) -> bool | None:  # 2D49
        """Return the cooling mode active state (from 2D49).

        :returns: True if cooling mode is active, False if inactive, or None if unknown.
        :rtype: bool | None
        """
        mode = await self.thermal_mode()
        if mode is None:
            return None
        return mode == ThermalMode.COOL

    async def schema(self) -> SchemaT:
        """Return the system's schema.

        :returns: The schema dictionary.
        :rtype: SchemaT
        """
        schema: SchemaT = {SZ_SYSTEM: {}}

        schema[SZ_SYSTEM][SZ_APPLIANCE_CONTROL] = (
            self.appliance_control.id if self.appliance_control else None
        )

        schema[SZ_ORPHANS] = sorted(
            [
                d.id
                for d in self.childs  # HACK: UFC
                if not d._child_id
                and await d._is_present()  # TODO: and d is not self.ctl
            ]  # and not isinstance(d, UfhController)
        )  # devices without a parent zone, NB: CTL can be a sensor for a zone

        return schema

    async def _schema_min(self) -> SchemaT:
        """Return the system's minimalised schema.

        :returns: The minimalised schema dictionary.
        :rtype: SchemaT
        """
        schema: SchemaT = await self.schema()
        result: SchemaT = {}

        try:
            app_cntrl = schema[SZ_SYSTEM][SZ_APPLIANCE_CONTROL]
            if app_cntrl and Address(app_cntrl).type in (
                DEV_TYPE_MAP.OTB,
                DevType.OTB,
            ):  # DEX
                result[SZ_SYSTEM] = {SZ_APPLIANCE_CONTROL: app_cntrl}
        except (IndexError, TypeError):
            result[SZ_SYSTEM] = {SZ_APPLIANCE_CONTROL: None}

        zones = {}
        for zone_index, zone in schema[SZ_ZONES].items():
            _zone = {}
            if zone[SZ_SENSOR] and Address(zone[SZ_SENSOR]).type in (
                DEV_TYPE_MAP.CTL,
                DevType.CTL,
            ):  # DEX
                _zone = {SZ_SENSOR: zone[SZ_SENSOR]}
            if devices := [
                d
                for d in zone[SZ_ACTUATORS]
                if Address(d).type in (DEV_TYPE_MAP.TRV, DevType.TRV)
            ]:  # DEX
                _zone.update({SZ_ACTUATORS: devices})
            if _zone:
                zones[zone_index] = _zone
        if zones:
            result[SZ_ZONES] = zones

        result |= {
            k: v
            for k, v in schema.items()
            if k in ("orphans",) and v  # add UFH?
        }

        return shrink(SCH_TCS(result))

    async def params(self) -> SchemaT:
        """Return the system's configuration.

        :returns: The configuration parameters dictionary.
        :rtype: SchemaT
        """
        params: SchemaT = {SZ_SYSTEM: {}}
        params[SZ_SYSTEM]["tpi_params"] = await self.entity_state.get_value(
            Code._1100
        )
        return params

    async def status(self) -> SchemaT:
        """Return the system's current state.

        :returns: The state and status dictionary.
        :rtype: SchemaT
        """
        status: SchemaT = {SZ_SYSTEM: {}}
        status[SZ_SYSTEM]["heat_demand"] = await self.heat_demand()

        status[SZ_DEVICES] = {
            d.id: await d.status()
            for d in sorted(self.childs, key=lambda x: x.id)
        }

        return status


class MultiZone(SystemBase):  # 0005 (+/- 000C?)
    """A system variant supporting multiple heating zones."""

    def __init__(  # type: ignore[explicit-any]  # passthrough to SystemBase
        self, *args: Any, **kwargs: Any
    ) -> None:
        """Initialise a multi-zone system."""
        super().__init__(*args, **kwargs)

        self.zones: list[Zone] = []
        self.zone_by_index: dict[str, Zone] = {}  # should not include HW
        self._max_zones: int = getattr(
            self._gateway.config, SZ_MAX_ZONES, DEFAULT_MAX_ZONES
        )

    def get_htg_zone(  # type: ignore[explicit-any]  # schema-tree kwargs
        self, zone_index: str, *, msg: Message | None = None, **schema: Any
    ) -> Zone:
        """Return a heating zone, create it if required.

        Heating zones are uniquely identified by a tcs_id and zone_index pair.
        If created, attach it to this TCS.

        :param zone_index: The hexadecimal string identifier for the zone.
        :type zone_index: str
        :param msg: An optional message to handle upon creation.
        :type msg: Message | None, optional
        :param schema: Keyword arguments defining the zone schema.
        :type schema: Any
        :returns: The created or retrieved heating zone.
        :rtype: Zone
        """
        # Use keep_hints=True so _name survives shrink() and reaches
        # Zone._update_schema for hydration (ramses-rf/ramses_cc#919).
        schema = shrink(SCH_TCS_ZONES_ZON(schema), keep_hints=True)

        zon: Zone | None = self.zone_by_index.get(zone_index)
        if zon is None:  # not found in tcs, create it
            created = zone_factory(self, zone_index, msg=msg, **schema)
            if not isinstance(created, Zone):
                raise TypeError(
                    f"zone_factory returned {type(created).__name__}, "
                    f"expected Zone for zone_index={zone_index}"
                )
            zon = created
            self.zone_by_index[zon.index] = zon
            self.zones.append(zon)

        elif schema:
            zon._update_schema(**schema)

        return zon

    async def schema(self) -> SchemaT:
        """Return the multi-zone system schema.

        :returns: The schema dictionary.
        :rtype: SchemaT
        """
        base_schema = await super().schema()
        return {
            **base_schema,
            SZ_ZONES: {z.index: await z.schema() for z in sorted(self.zones)},
        }

    async def params(self) -> SchemaT:
        """Return the multi-zone system parameters.

        :returns: The parameters dictionary.
        :rtype: SchemaT
        """
        base_params = await super().params()
        return {
            **base_params,
            SZ_ZONES: {z.index: await z.params() for z in sorted(self.zones)},
        }

    async def status(self) -> SchemaT:
        """Return the multi-zone system status.

        :returns: The status dictionary.
        :rtype: SchemaT
        """
        base_status = await super().status()
        return {
            **base_status,
            SZ_ZONES: {z.index: await z.status() for z in sorted(self.zones)},
        }


class ScheduleSync(SystemBase):  # 0006 (+/- 0404?)
    """A system variant managing schedule synchronisation."""

    def __init__(  # type: ignore[explicit-any]  # passthrough to SystemBase
        self, *args: Any, **kwargs: Any
    ) -> None:
        """Initialise schedule synchronisation."""
        super().__init__(*args, **kwargs)

        self._msg_0006: Message | None = None

    async def _schedule_version(
        self, *, force_io: bool = False
    ) -> tuple[int, bool]:
        """Return the global schedule version number and an I/O boolean.

        If `force_io` is True, request the latest change counter from the
        TCS rather than rely upon a recent (cached) value. Cached values
        are only used if less than 3 minutes old.

        :param force_io: Force a network request, defaults to False.
        :type force_io: bool, optional
        :returns: A tuple containing the version number and an I/O flag.
        :rtype: tuple[int, bool]
        """
        # RQ --- 30:185469 01:037519 --:------ 0006 001 00
        # RP --- 01:037519 30:185469 --:------ 0006 004 000500E6

        if not force_io and self._msg_0006:
            msg_dtm = self._msg_0006.dtm
            now = (
                dt.now(UTC).astimezone(msg_dtm.tzinfo)
                if msg_dtm.tzinfo is not None
                else dt.now()
            )
            if msg_dtm > now - td(minutes=3):
                return (
                    self._msg_0006.payload[SZ_CHANGE_COUNTER],
                    False,
                )  # global_ver, did_io

        packet = await send_system_intent(
            self, Action.GET_SCHEDULE_VERSION, data={}, wait_for_reply=True
        )
        if packet:
            self._msg_0006 = Message._from_packet(packet)

        if self._msg_0006 is None:
            raise RuntimeError(
                "No schedule version available after GET_SCHEDULE_VERSION"
            )

        return (
            self._msg_0006.payload[SZ_CHANGE_COUNTER],
            True,
        )  # global_ver, did_io

    def _refresh_schedules(self) -> None:
        """Trigger a refresh of all zone and DHW schedules."""
        zone: Zone

        for zone in getattr(self, SZ_ZONES, []):
            task = asyncio.create_task(zone.get_schedule(force_io=True))
            self._gateway.add_task(task)
        if isinstance(self, StoredHw) and self.dhw:
            task = asyncio.create_task(self.dhw.get_schedule(force_io=True))
            self._gateway.add_task(task)

    async def schedule_version(self) -> int | None:
        """Return the current global schedule version.

        :returns: The current schedule version, or None if unknown.
        :rtype: int | None
        """
        return await self.entity_state.get_value(
            Code._0006, key=SZ_CHANGE_COUNTER
        )

    async def status(self) -> SchemaT:
        """Return the schedule status.

        :returns: The schedule status dictionary.
        :rtype: SchemaT
        """
        base_status = await super().status()
        return {
            **base_status,
            "schedule_version": await self.schedule_version(),
        }


class Language(SystemBase):  # 0100
    """A system variant supporting language configuration."""

    async def language(self) -> str | None:
        """Return the current language configuration.

        :returns: The system language string, or None if unknown.
        :rtype: str | None
        """
        return self.system_state.language

    async def params(self) -> SchemaT:
        """Return the language parameters.

        :returns: The language parameters dictionary.
        :rtype: SchemaT
        """
        params = await super().params()
        params[SZ_SYSTEM][SZ_LANGUAGE] = await self.language()
        return params


class Logbook(SystemBase):  # 0418
    """A system variant supporting fault logbook retrieval."""

    def __init__(  # type: ignore[explicit-any]  # passthrough to SystemBase
        self, *args: Any, **kwargs: Any
    ) -> None:
        """Initialise the fault logbook."""
        super().__init__(*args, **kwargs)

        self._prev_event: Message | None = None
        self._this_event: Message | None = None

        self._prev_fault: Message | None = None
        self._this_fault: Message | None = None

        self._faultlog: FaultLog = FaultLog(self)

    @property
    def faultlog(self) -> FaultLog:
        """Return the system's fault log."""
        return self._faultlog

    async def get_faultlog(
        self,
        /,
        *,
        start: int = 0,
        limit: int | None = None,
        force_refresh: bool = False,
    ) -> dict[FaultIdxT, FaultLogEntry] | None:
        """Retrieve the fault log entries from the system.

        :param start: The starting fault index, defaults to 0.
        :type start: int, optional
        :param limit: The maximum number of entries, defaults to None.
        :type limit: int | None, optional
        :param force_refresh: Force a network request, defaults to False.
        :type force_refresh: bool, optional
        :returns: A dictionary of fault log entries, if available.
        :rtype: dict[FaultIdxT, FaultLogEntry] | None
        """
        return await self._faultlog.get_faultlog(
            start=start, limit=limit, force_refresh=force_refresh
        )

    @property
    def active_faults(self) -> tuple[str, ...] | None:
        """Return the most recently logged faults that are not restored."""
        if self._faultlog.active_faults is None:
            return None
        return tuple(str(f) for f in self._faultlog.active_faults)

    @property
    def latest_event(self) -> str | None:
        """Return the most recently logged event (fault or restore)."""
        if not self._faultlog.latest_event:
            return None
        return str(self._faultlog.latest_event)

    @property
    def latest_fault(self) -> str | None:
        """Return the most recently logged fault, if any."""
        if not self._faultlog.latest_fault:
            return None
        return str(self._faultlog.latest_fault)

    async def status(self) -> SchemaT:
        """Return the logbook status.

        :returns: The logbook status dictionary.
        :rtype: SchemaT
        """
        base_status = await super().status()
        return {
            **base_status,
            "active_faults": self.active_faults,
            "latest_event": self.latest_event,
            "latest_fault": self.latest_fault,
        }


class StoredHw(SystemBase):  # 10A0, 1260, 1F41
    """A system variant managing Domestic Hot Water (DHW)."""

    MIN_SETPOINT = 30.0  # NOTE: these may be removed
    MAX_SETPOINT = 85.0
    DEFAULT_SETPOINT = 50.0

    def __init__(  # type: ignore[explicit-any]  # passthrough to SystemBase
        self, *args: Any, **kwargs: Any
    ) -> None:
        """Initialise the StoredHw system."""
        super().__init__(*args, **kwargs)
        self._dhw: DhwZone | None = None

    # public: declared on the _HasZones runtime_checkable Protocol
    # (topology.py) — renaming breaks isinstance() protocol checks
    def get_dhw_zone(  # type: ignore[explicit-any]  # schema-tree kwargs
        self, *, msg: Message | None = None, **schema: Any
    ) -> DhwZone:
        """Return a DHW zone, create it if required.

        First, use the schema to create/update it, then pass it any msg
        to handle. DHW zones are uniquely identified by a controller ID.
        If a DHW zone is created, attach it to this TCS.

        :param msg: An optional message to handle upon creation.
        :type msg: Message | None, optional
        :param schema: Keyword arguments defining the zone schema.
        :type schema: Any
        :returns: The created or retrieved DHW zone.
        :rtype: DhwZone
        """
        schema = shrink(SCH_TCS_DHW(schema))

        if not self._dhw:
            created = zone_factory(self, "HW", msg=msg, **schema)
            if not isinstance(created, DhwZone):
                raise TypeError(
                    f"zone_factory returned {type(created).__name__}, "
                    "expected DhwZone for DHW zone"
                )
            self._dhw = created

        elif schema:
            self._dhw._update_schema(**schema)

        assert self._dhw is not None  # set above or was already set
        return self._dhw

    def _remove_dhw_zone(self) -> bool:
        """Remove the DhwZone from this system if it is empty.

        A DhwZone is considered empty when it has no hotwater valve, no
        heating valve, and no remaining children.  The sensor is not
        checked — a 07: DHW sensor may have been auto-assigned from
        traffic even though the system has no DHW (see issue 834 where
        a spurious DhwZone was created by a lower-confidence 000C HTG
        binding).

        :returns: ``True`` if the DhwZone was removed, ``False`` if it
            was retained because it still has children.
        :rtype: bool
        """
        if not self._dhw:
            return False

        if (
            self._dhw.hotwater_valve is None
            and self._dhw.heating_valve is None
            and not self._dhw.childs
        ):
            self._dhw = None
            return True
        return False

    @property
    def dhw(self) -> DhwZone | None:
        """Return the DHW zone instance."""
        return self._dhw

    @property
    def dhw_sensor(self) -> Device | None:
        """Return the DHW sensor device."""
        return self._dhw.sensor if self._dhw else None

    @property
    def hotwater_valve(self) -> Device | None:
        """Return the hot water valve device."""
        return self._dhw.hotwater_valve if self._dhw else None

    @property
    def heating_valve(self) -> Device | None:
        """Return the heating valve device."""
        return self._dhw.heating_valve if self._dhw else None

    async def schema(self) -> SchemaT:
        """Return the DHW system schema."""
        base_schema = await super().schema()
        return {
            **base_schema,
            SZ_DHW_SYSTEM: await self._dhw.schema() if self._dhw else {},
        }

    async def params(self) -> SchemaT:
        """Return the DHW system parameters."""
        base_params = await super().params()
        return {
            **base_params,
            SZ_DHW_SYSTEM: await self._dhw.params() if self._dhw else {},
        }

    async def status(self) -> SchemaT:
        """Return the DHW system status."""
        base_status = await super().status()
        return {
            **base_status,
            SZ_DHW_SYSTEM: await self._dhw.status() if self._dhw else {},
        }


class SystemMode(SystemBase):  # 2E04
    """A system variant managing the overall system mode."""

    async def system_mode(self) -> SchemaT | None:  # 2E04
        """Return the system mode from Hot State RAM.

        This is a pure read — it does **not** dispatch any commands.
        Hydration is handled by the discovery queue configured in
        ``_setup_discovery_cmds`` (a 2E04 RQ every 5 minutes with a
        5-second initial delay).

        :returns: A dictionary with system mode and until time, or
            ``None`` if the state has not yet been hydrated.
        :rtype: SchemaT | None
        """
        if self.system_state.system_mode is None:
            return None
        return {
            SZ_SYSTEM_MODE: self.system_state.system_mode,
            "until": self.system_state.until,
        }

    async def set_mode(
        self, system_mode: int | str | None, *, until: dt | str | None = None
    ) -> Message:
        """Set a system mode for a specified duration, or indefinitely.

        :param system_mode: 2-digit item from SYS_MODE_MAP, positional.
        :type system_mode: int | str | None
        :param until: End of the set period, defaults to None.
        :type until: dt | str | None, optional
        :returns: The resulting message.
        :rtype: Message
        """
        intent = Intent_(
            src=HGI_DEV_ADDR,
            dst=Address(self.id),
            action=Action.SET_SYSTEM_MODE,
            data={"system_mode": system_mode, "until": until},
        )
        return await self._gateway.dispatcher.send(
            intent, priority=Priority.HIGH
        )

    async def set_auto(self) -> Message:
        """Revert system to Auto, setting zones to FollowSchedule.

        :returns: The resulting message.
        :rtype: Message
        """
        return await self.set_mode(SYS_MODE_MAP.AUTO)

    async def reset_mode(self) -> Message:
        """Revert system to Auto, force all zones to FollowSchedule.

        :returns: The resulting message.
        :rtype: Message
        """
        return await self.set_mode(SYS_MODE_MAP.AUTO_WITH_RESET)

    async def params(self) -> SchemaT:
        """Return the system mode parameters."""
        params = await super().params()
        params[SZ_SYSTEM][SZ_SYSTEM_MODE] = await self.system_mode()
        return params


class Datetime(SystemBase):  # 313F
    """A system variant managing system date and time."""

    async def get_datetime(self) -> dt | None:
        """Retrieve the current system datetime.

        :returns: The system datetime, or None if unavailable.
        :rtype: dt | None
        """
        intent = Intent_(
            src=HGI_DEV_ADDR,
            dst=Address(self.id),
            action=Action.GET_SYSTEM_TIME,
            data={},
        )
        msg = await self._gateway.dispatcher.send(intent)
        return dt.fromisoformat(msg.payload[SZ_DATETIME])

    async def set_datetime(self, date_time: dt) -> Message:
        """Set the date and time of the system.

        :param date_time: The datetime object to set.
        :type date_time: dt
        :returns: The resulting message.
        :rtype: Message
        """
        intent = Intent_(
            src=HGI_DEV_ADDR,
            dst=Address(self.id),
            action=Action.SET_SYSTEM_TIME,
            data={"datetime": date_time},
        )
        return await self._gateway.dispatcher.send(
            intent, priority=Priority.HIGH
        )


class UfHeating(SystemBase):
    """A system variant supporting underfloor heating."""

    def _ufh_ctls(self) -> list[UfhController]:
        """Return a sorted list of underfloor heating controllers."""
        return sorted([d for d in self.childs if isinstance(d, UfhController)])

    async def schema(self) -> SchemaT:
        """Return the underfloor heating schema."""
        base_schema = await super().schema()
        return {
            **base_schema,
            SZ_UFH_SYSTEM: {d.id: await d.schema() for d in self._ufh_ctls()},
        }

    async def params(self) -> SchemaT:
        """Return the underfloor heating parameters."""
        base_params = await super().params()
        return {
            **base_params,
            SZ_UFH_SYSTEM: {d.id: await d.params() for d in self._ufh_ctls()},
        }

    async def status(self) -> SchemaT:
        """Return the underfloor heating status."""
        base_status = await super().status()
        return {
            **base_status,
            SZ_UFH_SYSTEM: {d.id: await d.status() for d in self._ufh_ctls()},
        }


class System(StoredHw, Datetime, Logbook, SystemBase):
    """The main Temperature Control System (TCS) class."""

    _SLUG: str = SYS_KLASS.SYS

    def __init__(  # type: ignore[explicit-any]  # passthrough kwargs
        self, controller: Controller, **kwargs: Any
    ) -> None:
        """Initialise the TCS system.

        :param controller: The central controller device.
        :type controller: Controller
        :param kwargs: Additional keyword arguments for the system.
        :type kwargs: Any
        """
        super().__init__(controller, **kwargs)

        self._heat_demands: SchemaT = {}
        self._relay_demands: SchemaT = {}
        self._relay_failsafes: SchemaT = {}
        self._tpi_params: SchemaT = {}  # 1100, keyed by domain_id

    def _update_schema(  # type: ignore[explicit-any]  # schema-tree kwargs
        self, **schema: Any
    ) -> None:
        """Update a CH/DHW system with new schema attrs.

        Raise an exception if the new schema is not a superset of the
        existing schema.
        """
        _schema: SchemaT
        # Use keep_hints=True so that _name in zone entries survives
        # shrink() and reaches Zone._update_schema for hydration
        # (ramses-rf/ramses_cc#919: zone names lost after 24h).
        # SCH_TCS validation ensures only allowed _ keys (i.e. _name)
        # are present, so keep_hints is safe here.
        schema = shrink(SCH_TCS(schema), keep_hints=True)

        if schema.get(SZ_SYSTEM) and (
            dev_id := schema[SZ_SYSTEM].get(SZ_APPLIANCE_CONTROL)
        ):
            try:
                device = self._gateway.device_registry.get_device(
                    dev_id, parent=self, child_id=FC
                )
                assert isinstance(device, (BdrSwitch, OtbGateway))
                self._app_cntrl = device
            except (
                DeviceNotFoundError,
                SchemaInconsistentError,
                SystemSchemaInconsistent,
            ) as err:
                _TRACE.warning(
                    f"SUPPRESSED in System._update_schema (app_cntrl): {err}"
                )

        _dhw_schema = schema.get(SZ_DHW_SYSTEM)
        if _dhw_schema:
            self.get_dhw_zone(**_dhw_schema)  # self._dhw = ...

        if not isinstance(self, MultiZone):
            return

        _zones_schema = schema.get(SZ_ZONES)
        if _zones_schema:
            [
                self.get_htg_zone(zone_index, **s)
                for zone_index, s in _zones_schema.items()
            ]

    @classmethod
    def create_from_schema(  # type: ignore[explicit-any]  # schema-tree kwargs
        cls, controller: Controller, **schema: Any
    ) -> System:
        """Create a CH/DHW system for a CTL and set its schema attrs.

        The appropriate System class should have been determined by a
        factory. Schema attrs include: class (klass) & others.

        :param controller: The central controller device.
        :type controller: Controller
        :param schema: Schema attributes for the system.
        :type schema: Any
        :returns: The configured system instance.
        :rtype: System
        """
        tcs = cls(controller)
        tcs._update_schema(**schema)
        return tcs

    @property
    def thermal_demands(self) -> dict[str, ThermalDemandDTO] | None:
        """Return current thermal demands per domain as CQRS DTOs.

        Provides a dictionary mapping domain identifiers (e.g. FC) to their
        corresponding active thermal demand DTOs.

        :returns: Dictionary mapping domain ID to ThermalDemandDTO or None.
        :rtype: dict[str, ThermalDemandDTO] | None
        """
        # NOTE: FC: 00-C8 (no F9, FA); kept as domain dict for ramses_cc compat
        if not self._heat_demands:
            return None
        mode = (
            ThermalMode.COOL
            if self.system_state.cooling_mode
            else ThermalMode.HEAT
        )
        return {
            k: ThermalDemandDTO(
                thermal_demand=v.payload.get("heat_demand"),
                mode=mode,
                domain_id=k,
            )
            for k, v in self._heat_demands.items()
        }

    @property
    def relay_demands(self) -> SchemaT | None:  # 0008
        """Return the current relay demands per domain."""
        # FC: 00-C8, F9: 00-C8, FA: 00 or C8 only (01: all 3, 02: FC/FA only)
        if not self._relay_demands:
            return None
        return {
            k: v.payload.get("relay_demand")
            for k, v in self._relay_demands.items()
        }

    @property
    def relay_failsafes(self) -> SchemaT | None:  # 0009
        """Return the current relay failsafes per domain."""
        if not self._relay_failsafes:
            return None
        return {
            k: v.get("failsafe_enabled")
            if isinstance(v, dict)
            else v.payload.get("failsafe_enabled")
            for k, v in self._relay_failsafes.items()
        }

    async def status(self) -> SchemaT:
        """Return the system's current state.

        :returns: The status dictionary.
        :rtype: SchemaT
        """
        status = await super().status()

        status[SZ_SYSTEM]["heat_demands"] = self.thermal_demands
        status[SZ_SYSTEM]["relay_demands"] = self.relay_demands
        status[SZ_SYSTEM]["relay_failsafes"] = self.relay_failsafes

        return status


class Evohome(
    ScheduleSync, Language, SystemMode, MultiZone, UfHeating, System
):
    """The Evohome system class."""

    _SLUG: str = SYS_KLASS.TCS  # evohome

    # older evohome don't have zone_type=ELE


class Chronotherm(Evohome):
    """The Chronotherm system class."""

    _SLUG: str = SYS_KLASS.SYS


class Hometronics(System):
    """The Hometronics system class."""

    _SLUG: str = SYS_KLASS.SYS

    # These are only ever been seen from a Hometronics controller
    # .I --- 01:023389 --:------ 01:023389 2D49 003 00C800
    # .I --- 01:023389 --:------ 01:023389 2D49 003 01C800
    # .I --- 01:023389 --:------ 01:023389 2D49 003 880000
    # .I --- 01:023389 --:------ 01:023389 2D49 003 FD0000

    # Hometronic does not react to W/2349 but rather requires W/2309

    #
    # def _setup_discovery_cmds(self) -> None:
    #     # super()._setup_discovery_cmds()

    #     # will RP to: 0005/configured_zones_alt, but not: configured_zones
    #     # will RP to: 0004

    RQ_SUPPORTED = (
        Code._0004,
        Code._000C,
        Code._2E04,
        Code._313F,
    )  # TODO: WIP
    RQ_UNSUPPORTED = ("xxxx",)  # 10E0?


class Programmer(Evohome):
    """The Programmer system class."""

    _SLUG: str = SYS_KLASS.PRG


class Sundial(Evohome):
    """The Sundial system class."""

    _SLUG: str = SYS_KLASS.SYS


# e.g. {"evohome": Evohome}
SYS_CLASS_BY_SLUG: dict[str, type[System]] = class_by_attr(__name__, "_SLUG")


def system_factory(  # type: ignore[explicit-any]  # schema-tree kwargs
    controller: Controller, *, msg: Message | None = None, **schema: Any
) -> System:
    """Return the system class for a given controller/schema.

    :param controller: The central controller device.
    :type controller: Controller
    :param msg: An optional message to handle.
    :type msg: Message | None, optional
    :param schema: Additional schema attributes.
    :type schema: Any
    :returns: The created system instance.
    :rtype: System
    """

    def best_tcs_class(  # type: ignore[explicit-any]  # schema-tree kwargs
        controller_address: Address,
        *,
        msg: Message | None = None,
        eavesdrop: bool = False,
        **schema: Any,
    ) -> type[System]:
        """Return the best system class for a given CTL/schema.

        :param controller_address: The central controller address.
        :type controller_address: Address
        :param msg: An optional message.
        :type msg: Message | None, optional
        :param eavesdrop: Whether eavesdropping is enabled.
        :type eavesdrop: bool, optional
        :param schema: Additional schema attributes.
        :type schema: Any
        :returns: The appropriate system class type.
        :rtype: type[System]
        """
        klass: str | None = schema.get(SZ_CLASS)

        # a specified system class always takes precedence (even if it is wrong)...
        if klass and (cls := SYS_CLASS_BY_SLUG.get(klass)):
            _LOGGER.debug(
                f"Using an explicitly-defined system class for: {controller_address} "
                f"({cls._SLUG})"
            )
            return cls

        # otherwise, use the default system class...
        _LOGGER.debug(
            "Using a generic system class for: %s (%s)",
            controller_address,
            Evohome._SLUG,
        )
        return Evohome

    return best_tcs_class(
        controller.addr,
        msg=msg,
        eavesdrop=controller._gateway.config.enable_eavesdrop,
        **schema,
    ).create_from_schema(controller, **schema)
