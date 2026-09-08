#!/usr/bin/env python3
"""RAMSES RF - The evohome-compatible zones."""

from __future__ import annotations

import dataclasses
import logging
import math
from collections.abc import Mapping
from datetime import datetime as dt, timedelta as td
from typing import TYPE_CHECKING, Any, Self, TypeVar

from ramses_rf import exceptions as exc
from ramses_rf.address import Address
from ramses_rf.const import (
    DEV_ROLE_MAP,
    DEV_TYPE_MAP,
    SZ_CIRCUITS,
    SZ_HEAT_DEMAND,
    SZ_NAME,
    SZ_RELAY_DEMAND,
    SZ_SETPOINT,
    SZ_TEMPERATURE,
    SZ_ZONE_INDEX,
    ZON_MODE_MAP,
    ZON_ROLE_MAP,
    DevRole,
    ZoneRole,
)
from ramses_rf.devices import (
    BdrSwitch,
    Controller,
    Device,
    DhwSensor,
    TrvActuator,
)
from ramses_rf.entity import Entity, class_by_attr
from ramses_rf.enums import Action, DevType, ThermalMode
from ramses_rf.helpers import shrink
from ramses_rf.models import (
    DemandState,
    DhwState,
    ScheduleState,
    TemperatureState,
    ThermalDemandDTO,
    TrvState,
    UfhCircuitDTO,
    ZoneState,
)
from ramses_rf.schemas import (
    SCH_TCS_DHW,
    SCH_TCS_ZONES_ZON,
    SZ_ACTUATORS,
    SZ_CLASS,
    SZ_DHW_VALVE,
    SZ_HTG_VALVE,
    SZ_SENSOR,
)
from ramses_rf.topology import Child, Parent
from ramses_rf.typing import DeviceIdT, DevIndexT, WeeklySchedule
from ramses_tx.exceptions import ProtocolSendFailed, ProtocolTimeoutError
from ramses_tx.typing import PayDictT

from ..messages import Message
from .schedule import Schedule

if TYPE_CHECKING:
    from ramses_rf.devices.heat_controllers import UfhCircuit

    from .tcs import Evohome, _MultiZoneT, _StoredHwT

from ramses_rf.const import (  # noqa: F401, isort: skip
    F9,
    FA,
    FC,
    FF,
)

from ramses_rf.const import (  # noqa: F401, isort: skip
    I_,
    RP,
    RQ,
    W_,
    Code,
)


from .helpers import send_system_intent

_LOGGER = logging.getLogger(__name__)
_TRACE = logging.getLogger("ramses_rf.legacy_trace")


class ZoneBase(Child, Parent[Device], Entity):
    """The Zone/DHW base class."""

    _SLUG: str | None = None

    _ROLE_ACTUATORS: str | None = None
    _ROLE_SENSORS: str | None = None

    def __init__(self, tcs: Evohome, zone_index: str) -> None:
        """Initialize a ZoneBase instance for the given TCS and zone index."""
        super().__init__(tcs._gateway)

        # Parallel CQRS States
        self.temp_state = TemperatureState()
        self.demand_state = DemandState()
        self.schedule_state = ScheduleState(zone_index=zone_index, days=())
        self.trv_state = TrvState()
        self.zone_state = ZoneState()

        # FIXME: ZZZ entities must know their parent device ID and their
        # own index
        self._z_id = tcs.id  # the responsible device is the controller
        # the zone index (ctx), 00-0B (or 0F), HW (FA)
        self._z_index: DevIndexT = DevIndexT(zone_index)

        self.id: DeviceIdT = DeviceIdT(f"{tcs.id}_{zone_index}")

        self.tcs: Evohome = tcs
        self.ctl: Controller = tcs.ctl
        self._child_id: str = zone_index

        self._name: str | None = None  # param attr

    # Should be a private method
    @classmethod
    def create_from_schema(
        cls, tcs: _MultiZoneT | _StoredHwT, zone_index: str, **schema: Any
    ) -> Self:
        """Create a CH/DHW zone for a TCS and set its schema attrs.

        The appropriate Zone class should have been determined by a
        factory. Can be a heating zone (of a klass), or the DHW
        subsystem (index must be 'HW').
        """
        zon = cls(tcs, zone_index)  # type: ignore[arg-type]
        zon._update_schema(**schema)
        return zon

    def _update_schema(self, **schema: Any) -> None:
        raise NotImplementedError

    def __repr__(self) -> str:
        """Return the string representation of the zone."""
        return f"{self.id} ({self._SLUG})"

    def __lt__(self, other: object) -> bool:
        """Compare two zones by their zone index."""
        if not isinstance(other, ZoneBase):
            return NotImplemented
        return self.index < other.index

    @property
    def index(self) -> str:
        """Return the zone index string.

        :returns: Hexadecimal zone index string.
        :rtype: str
        """
        return self._child_id

    async def schema(self) -> dict[str, Any]:
        """Return the schema (fixed at instantiation)."""
        return {}

    async def params(self) -> dict[str, Any]:
        """Return configuration (can be changed by user)."""
        return {}

    async def status(self) -> dict[str, Any]:
        """Return the current state."""
        return {}

    async def thermal_mode(self) -> ThermalMode | None:
        """Return the active thermal mode of the zone.

        :returns: Active ThermalMode or None.
        :rtype: ThermalMode | None
        """
        if self.tcs:
            return await self.tcs.thermal_mode()
        return ThermalMode.HEAT

    async def thermal_demand(self) -> ThermalDemandDTO | None:
        """Return zone thermal demand as a CQRS DTO.

        :returns: ThermalDemandDTO or None.
        :rtype: ThermalDemandDTO | None
        """
        heat_demand_value = (
            await self.heat_demand()
            if hasattr(self, "heat_demand")
            else self.demand_state.heat_demand
        )
        if heat_demand_value is None:
            return None
        mode = await self.thermal_mode() or ThermalMode.HEAT
        return ThermalDemandDTO(
            thermal_demand=heat_demand_value,
            mode=mode,
            ufh_index=str(self.index),
        )


class ZoneSchedule(ZoneBase):  # 0404
    """Zone mixin providing schedule retrieval and modification."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize a ZoneSchedule instance."""
        super().__init__(*args, **kwargs)

        self._schedule = Schedule(self)  # type: ignore[arg-type]

    async def get_schedule(
        self, *, force_io: bool = False
    ) -> WeeklySchedule | None:
        """Fetch the weekly schedule from the controller."""
        await self._schedule.get_schedule(force_io=force_io)
        return self.schedule

    async def set_schedule(
        self, schedule: WeeklySchedule | Mapping[str, Any]
    ) -> WeeklySchedule | None:
        """Upload a weekly schedule to the controller.

        :param schedule: 7-day schedule array or outer dictionary payload.
        :type schedule: WeeklySchedule | Mapping[str, Any]
        :returns: The updated WeeklySchedule array.
        :rtype: WeeklySchedule | None
        """
        return await self._schedule.set_schedule(schedule)

    @property
    def schedule(self) -> WeeklySchedule | None:
        """Return the latest schedule (not guaranteed to be up to date)."""
        # inner: [{"day_of_week": 0, "switchpoints": [...],
        # {"day_of_week": 1, ...
        # outer: {"zone_index": "01", "schedule": <inner>

        return self._schedule.schedule

    async def schedule_version(self) -> int | None:
        """Return version number of latest retrieved schedule."""
        return self._schedule.version

    async def status(self) -> dict[str, Any]:
        """Return the zone schedule status dictionary."""
        return {
            **(await super().status()),
            "schedule_version": await self.schedule_version(),
        }


class DhwZone(ZoneSchedule):  # CS92A
    """The DHW class."""

    _SLUG: str | None = ZoneRole.DHW

    def __init__(self, tcs: _StoredHwT, zone_index: str = "HW") -> None:
        """Initialize a DhwZone instance."""
        _LOGGER.debug(
            "Creating a DHW for TCS: %s_HW (%s)", tcs.id, self.__class__
        )

        if tcs.dhw:
            raise exc.SchemaInconsistentError(
                f"Duplicate DHW for TCS: {tcs.id}"
            )
        if zone_index not in (None, "HW"):
            raise exc.SchemaInconsistentError(
                f"Invalid zone index for DHW: {zone_index} (not 'HW'/null)"
            )

        super().__init__(tcs, "HW")
        self.dhw_state = DhwState()

        # DhwZones have a sensor, but actuators are optional,
        # depending on schema
        self._dhw_sensor: DhwSensor | None = None
        self._dhw_valve: BdrSwitch | None = None
        self._htg_valve: BdrSwitch | None = None

    def _update_schema(self, **schema: Any) -> None:
        """Update a DHW zone with new schema attrs.

        Raise an exception if the new schema is not a superset of the
        existing schema.
        """
        schema = shrink(SCH_TCS_DHW(schema))

        if dev_id := schema.get(SZ_SENSOR):
            try:
                dhw_sensor = self._gateway.device_registry.get_device(
                    dev_id,
                    parent=self,
                    child_id=FA,
                    is_sensor=True,
                )
                assert isinstance(dhw_sensor, DhwSensor)  # mypy
                self._dhw_sensor = dhw_sensor
            except (
                exc.DeviceNotFoundError,
                exc.SchemaInconsistentError,
                exc.SystemSchemaInconsistent,
            ) as err:
                _TRACE.warning(
                    "SUPPRESSED in DhwZone._update_schema (sensor): %s", err
                )

        if dev_id := schema.get(DEV_ROLE_MAP[DevRole.HTG]):
            try:
                dhw_valve = self._gateway.device_registry.get_device(
                    dev_id, parent=self, child_id=FA
                )
                assert isinstance(dhw_valve, BdrSwitch)  # mypy
                self._dhw_valve = dhw_valve
            except (
                exc.DeviceNotFoundError,
                exc.SchemaInconsistentError,
                exc.SystemSchemaInconsistent,
            ) as err:
                _TRACE.warning(
                    f"SUPPRESSED in DhwZone._update_schema (dhw_valve): {err}"
                )

        if dev_id := schema.get(DEV_ROLE_MAP[DevRole.HT1]):
            try:
                htg_valve = self._gateway.device_registry.get_device(
                    dev_id, parent=self, child_id=F9
                )
                assert isinstance(htg_valve, BdrSwitch)  # mypy
                self._htg_valve = htg_valve
            except (
                exc.DeviceNotFoundError,
                exc.SchemaInconsistentError,
                exc.SystemSchemaInconsistent,
            ) as err:
                _TRACE.warning(
                    f"SUPPRESSED in DhwZone._update_schema (htg_valve): {err}"
                )

    @property
    def sensor(self) -> DhwSensor | None:
        """Return the DHW temperature sensor device or None."""
        return self._dhw_sensor

    @property
    def hotwater_valve(self) -> BdrSwitch | None:
        """Return the DHW valve switch actuator or None."""
        return self._dhw_valve

    @property
    def heating_valve(self) -> BdrSwitch | None:
        """Return the heating valve switch actuator or None."""
        return self._htg_valve

    async def name(self) -> str:
        """Return the standard DHW zone name."""
        return "Stored HW"

    async def config(self) -> dict[str, Any] | None:  # 10A0
        """Return DHW configuration dictionary."""
        if self.dhw_state.setpoint is None:
            return None
        return {
            SZ_SETPOINT: self.dhw_state.setpoint,
            "overrun": self.dhw_state.overrun,
            "differential": self.dhw_state.differential,
        }

    async def mode(self) -> dict[str, Any] | None:  # 1F41
        """Return DHW operating mode dictionary."""
        if self.dhw_state.mode is None:
            return None
        return {
            "mode": self.dhw_state.mode,
            "active": self.dhw_state.active,
            "until": self.dhw_state.until,
        }

    async def setpoint(self) -> float | None:  # 10A0
        """Return the target DHW setpoint temperature in degrees Celsius."""
        return self.dhw_state.setpoint

    async def set_setpoint(self, value: float) -> Message:  # 10A0
        """Set the target temperature for the DHW zone."""
        return await self.set_config(setpoint=value)

    async def temperature(self) -> float | None:  # 1260
        """Return current hot water temperature in degrees Celsius."""
        return self.temp_state.temperature

    async def heat_demand(self) -> float | None:  # 3150
        """Return the DHW heat demand percentage (0.0 to 1.0)."""
        return self.demand_state.heat_demand

    async def relay_demand(self) -> float | None:  # 0008
        """Return the DHW relay demand percentage (0.0 to 1.0)."""
        return self.demand_state.relay_demand

    async def relay_failsafe(self) -> float | None:  # 0009
        """Return DHW relay failsafe demand percentage (0.0 to 1.0)."""
        return self.demand_state.relay_failsafe

    async def set_mode(
        self,
        *,
        mode: int | str | None = None,
        active: bool | None = None,
        until: dt | str | None = None,
    ) -> Message:
        """Set the DHW mode (mode, active, until)."""
        return await send_system_intent(
            self,
            Action.SET_DHW_MODE,
            {"mode": mode, "active": active, "until": until},
            wait_for_reply=True,
        )

    async def set_boost_mode(self) -> Message:
        """Enable DHW for an hour, despite any schedule."""
        return await self.set_mode(
            mode=ZON_MODE_MAP.TEMPORARY,
            active=True,
            until=dt.now() + td(hours=1),
        )

    async def reset_mode(self) -> Message:  # 1F41
        """Revert the DHW to following its schedule."""
        return await self.set_mode(mode=ZON_MODE_MAP.FOLLOW)

    async def set_config(
        self,
        *,
        setpoint: float | None = None,
        overrun: int | None = None,
        differential: float | None = None,
    ) -> Message:
        """Set the DHW parameters (setpoint, overrun, differential)."""
        # dhw_params = self.entity_state.get_value(Code._10A0)
        # if setpoint is None:
        #     setpoint = dhw_params[SZ_SETPOINT]
        # if overrun is None:
        #     overrun = dhw_params["overrun"]
        # if differential is None:
        #     setpoint = dhw_params["differential"]

        return await send_system_intent(
            self,
            Action.SET_DHW_PARAMS,
            {
                "setpoint": setpoint,
                "overrun": overrun,
                "differential": differential,
            },
        )

    async def reset_config(self) -> Message:  # 10A0
        """Reset the DHW parameters to their default values."""
        return await self.set_config(setpoint=50, overrun=5, differential=1)

    async def schema(self) -> dict[str, Any]:
        """Return the schema of the DHW's."""
        return {
            SZ_SENSOR: self.sensor.id if self.sensor else None,
            SZ_DHW_VALVE: (
                self.hotwater_valve.id if self.hotwater_valve else None
            ),
            SZ_HTG_VALVE: (
                self.heating_valve.id if self.heating_valve else None
            ),
        }

    async def params(self) -> dict[str, Any]:
        """Return the DHW's configuration (excl. schedule)."""
        return {
            "config": await self.config(),
            "mode": await self.mode(),
        }

    async def status(self) -> dict[str, Any]:
        """Return the DHW's current state."""
        return {
            SZ_TEMPERATURE: await self.temperature(),
            SZ_HEAT_DEMAND: await self.heat_demand(),
        }


class Zone(ZoneSchedule):
    """The Zone class for all zone types (but not DHW)."""

    _SLUG: str | None = None
    _ROLE_ACTUATORS: str = DEV_ROLE_MAP.ACT

    def __init__(self, tcs: _MultiZoneT, zone_index: str) -> None:
        """Create a heating zone.

        The type of zone may not be known at instantiation. Even when it
        is known, zones are still created without a type before they are
        subsequently promoted, so that both schemes (e.g. eavesdropping,
        vs probing) are the same.

        In addition, an electric zone may subsequently turn out to be a
        zone valve zone.
        """
        _LOGGER.debug(
            "Creating a Zone: %s_%s (%s)", tcs.id, zone_index, self.__class__
        )

        if zone_index in tcs.zone_by_index:
            raise exc.SchemaInconsistentError(
                f"Duplicate ZON for TCS: {tcs.id}_{zone_index}"
            )
        if int(zone_index, 16) >= tcs._max_zones:
            raise exc.SchemaInconsistentError(
                f"Invalid zone_index: {zone_index} (exceeds max_zones)"
            )

        super().__init__(tcs, zone_index)

        self._sensor: Device | None = None
        self.actuators: list[Device] = []
        self.actuator_by_id: dict[DeviceIdT, Device] = {}
        self._heating_type: str | None = None

    def _update_schema(self, **schema: Any) -> None:
        """Update a heating zone with new schema attrs.

        Raise an exception if the new schema is not a superset of the
        existing schema.
        """

        def set_zone_type(zone_type: str) -> None:
            """Set the zone's type (e.g. '08'), after validating it.

            There are two possible sources for the type of a zone:
            1. eavesdropping packet codes
            2. analyzing child devices
            """
            if zone_type in (ZON_ROLE_MAP.ACT, ZON_ROLE_MAP.SEN):
                return  # generic zone classes
            if zone_type not in ZON_ROLE_MAP.HEAT_ZONES:
                raise exc.SchemaInconsistentError(
                    f"Invalid zone type: {zone_type}"
                )

            klass = ZON_ROLE_MAP.slug(zone_type)  # not incl. DHW?

            if klass == self._SLUG:
                return

            if klass == ZoneRole.VAL and self._SLUG not in (
                None,
                ZoneRole.ELE,
            ):
                raise exc.SchemaInconsistentError(
                    f"Not a compatible zone class for {self}: {zone_type}"
                )

            elif klass not in ZONE_CLASS_BY_SLUG:
                raise exc.SchemaInconsistentError(
                    f"Not a known zone class (for {self}): {zone_type}"
                )

            current_slug = self._SLUG
            if current_slug is not None and klass != current_slug:
                raise exc.SystemSchemaInconsistent(
                    f"{self} changed zone class: from {current_slug} to {klass}"
                )

            self._heating_type = klass
            if self._SLUG is None and klass in ZONE_CLASS_BY_SLUG:
                target_cls = ZONE_CLASS_BY_SLUG[klass]
                if issubclass(target_cls, Zone):
                    self.__class__ = target_cls

        # if schema.get(SZ_CLASS) == ZON_ROLE_MAP[ZON_ROLE.ACT]:
        #     schema.pop(SZ_CLASS)
        validated = SCH_TCS_ZONES_ZON(schema)

        # Hydrate zone_state.name from the schema's _name before shrink()
        # strips it (ramses-rf/ramses_cc#919: zone names lost after 24h
        # when the MessageStore prunes 0004 packets — the schema is the
        # only persistent source of the name).
        schema_name = validated.get(f"_{SZ_NAME}")
        _LOGGER.debug(
            "Zone %s _update_schema: _name=%r, zone_state.name=%r",
            self.id,
            schema_name,
            self.zone_state.name,
        )
        if schema_name and self.zone_state.name is None:
            self.zone_state = dataclasses.replace(
                self.zone_state, name=str(schema_name)
            )

        schema = shrink(validated)

        if klass := schema.get(SZ_CLASS):
            set_zone_type(ZON_ROLE_MAP[klass])

        # Controller devices (CTL=01:, UFC=02:, HGI=18:) should not be
        # bound as zone sensors or actuators.  However, a faked device
        # may have a controller-class address prefix (e.g. 01:150003)
        # while being explicitly classed as a sensor (e.g. THM) in the
        # known_list.  Check the known_list class first; if the device
        # is not in the known_list, fall back to the address prefix
        # check so that eavesdropped CTL/UFC/HGI devices are still
        # filtered out.
        _controller_classes = frozenset(
            {
                DevType.CTL,
                DevType.UFC,
                DevType.HGI,
            }
        )
        _controller_prefixes = (
            f"{DevType.CTL}:",
            f"{DevType.UFC}:",
            f"{DevType.HGI}:",
        )

        if sensor_id := schema.get(SZ_SENSOR):
            sensor_kl = self._gateway.config.known_list.get(str(sensor_id), {})
            sensor_cls = sensor_kl.get(SZ_CLASS)
            is_ctrl = (
                sensor_cls in _controller_classes
                if sensor_cls is not None
                else str(sensor_id).startswith(_controller_prefixes)
            )
            if not is_ctrl:
                try:
                    self._sensor = self._gateway.device_registry.get_device(
                        sensor_id, parent=self, is_sensor=True
                    )
                except (
                    exc.DeviceNotFoundError,
                    exc.SchemaInconsistentError,
                    exc.SystemSchemaInconsistent,
                ) as err:
                    _TRACE.warning(
                        "SUPPRESSED in Zone._update_schema (sensor): %s", err
                    )

        for act_id in schema.get(SZ_ACTUATORS, []):
            act_kl = self._gateway.config.known_list.get(str(act_id), {})
            act_cls = act_kl.get(SZ_CLASS)
            is_ctrl = (
                act_cls in _controller_classes
                if act_cls is not None
                else str(act_id).startswith(_controller_prefixes)
            )
            if is_ctrl:
                continue
            try:
                self._gateway.device_registry.get_device(act_id, parent=self)
            except (
                exc.DeviceNotFoundError,
                exc.SchemaInconsistentError,
                exc.SystemSchemaInconsistent,
            ) as err:
                _TRACE.warning(
                    "SUPPRESSED in Zone._update_schema (actuator): %s", err
                )

    @property
    def sensor(self) -> Device | None:
        """Return the primary temperature sensor device or None."""
        return self._sensor

    @property
    def heating_type(self) -> str | None:
        """Get the type of the zone/DHW (e.g. electric_zone, stored_dhw)."""
        slug = self._SLUG or self._heating_type
        if slug is None:
            return None
        return str(ZON_ROLE_MAP[slug])

    async def name(self) -> str | None:  # 0004
        """Get the name of the zone.

        Prefers live CQRS ZoneState from RF packets (0004), falling back to
        static schema configuration (_name).

        :returns: The zone name, or None if not set.
        :rtype: str | None
        """
        return self.zone_state.name or self._name

    async def config(self) -> dict[str, Any] | None:  # 000A
        """Return zone configuration dictionary."""
        if self.zone_state.min_temp is None:
            return None
        return {
            "min_temp": self.zone_state.min_temp,
            "max_temp": self.zone_state.max_temp,
            "local_override": self.zone_state.local_override,
            "openwindow_function": self.zone_state.openwindow_function,
            "multiroom_mode": self.zone_state.multiroom_mode,
        }

    async def mode(self) -> dict[str, Any] | None:  # 2349
        """Return zone operating mode dictionary."""
        if self.zone_state.mode is None:
            return None
        return {
            "mode": self.zone_state.mode,
            SZ_SETPOINT: self.zone_state.setpoint,
            "until": self.zone_state.until,
        }

    async def setpoint(self) -> float | None:
        """Return target setpoint temperature in degrees Celsius."""
        # 2309 (2349 is a superset of 2309)
        return self.temp_state.setpoint

    async def setpoint_bounds(self) -> dict[str, Any] | None:  # 22C9, 2209
        """Return zone setpoint bounds if defined by thermostat."""
        result = await self.entity_state.get_value(
            (Code._22C9, Code._2209), zone_index=self.index
        )
        return result if isinstance(result, dict) else None

    async def set_setpoint(
        self, value: float | None
    ) -> Message | None:  # 000A/2309
        """Set the target temperature, until the next scheduled setpoint."""
        if value is None:
            return await self.reset_mode()

        return await send_system_intent(
            self,
            Action.SET_TEMPERATURE,
            {SZ_ZONE_INDEX: self.index, "setpoint": value},
        )

    async def temperature(self) -> float | None:  # 30C9
        """Return current zone temperature in degrees Celsius."""
        return self.temp_state.temperature

    async def heat_demand(self) -> float | None:  # 3150
        """Return estimated zone heat demand from child devices."""
        return self.demand_state.heat_demand

    async def window_open(self) -> bool | None:  # 12B0
        """Return an estimate of the zone's current window_open state."""
        if not self.actuators:
            return self.trv_state.window_open

        states: list[bool | None] = []
        for act in self.actuators:
            if isinstance(act, TrvActuator):
                states.append(await act.window_open())

        if not states:
            return self.trv_state.window_open

        if any(state is True for state in states):
            return True

        if any(state is None for state in states):
            return None

        return False

    async def _get_temp(self) -> Message | None:
        """Get the zone's latest temp from the Controller."""
        try:
            return await send_system_intent(
                self,
                Action.GET_ZONE_TEMP,
                {SZ_ZONE_INDEX: self.index},
            )
        except ProtocolTimeoutError as err:
            _LOGGER.warning("%s: _get_temp timed out: %s", self, err)
            return None
        except ProtocolSendFailed:
            # Silently drop the request if the transport is inactive
            # (e.g., during cache restoration prior to gateway startup).
            _LOGGER.debug(
                "%s: Dropped request: gateway transport is inactive.",
                self,
            )
            return None

    async def reset_config(self) -> Message:  # 000A
        """Reset the zone's parameters to their default values."""
        return await self.set_config()

    async def set_config(
        self,
        *,
        min_temp: float = 5,
        max_temp: float = 35,
        local_override: bool = False,
        openwindow_function: bool = False,
        multiroom_mode: bool = False,
    ) -> Message:
        """Set the zone's parameters (min_temp, max_temp, etc.)."""
        return await send_system_intent(
            self,
            Action.SET_ZONE_CONFIG,
            {
                SZ_ZONE_INDEX: self.index,
                "min_temp": min_temp,
                "max_temp": max_temp,
                "local_override": local_override,
                "openwindow_function": openwindow_function,
                "multiroom_mode": multiroom_mode,
            },
        )

    async def reset_mode(self) -> Message:  # 2349
        """Revert the zone to following its schedule."""
        return await self.set_mode(mode=ZON_MODE_MAP.FOLLOW)

    async def set_frost_mode(self) -> Message:  # 2349
        """Set the zone to the lowest possible setpoint, indefinitely."""
        return await self.set_mode(
            mode=ZON_MODE_MAP.PERMANENT, setpoint=5
        )  # TODO

    async def set_mode(
        self,
        *,
        mode: str | None = None,
        setpoint: float | None = None,
        until: dt | str | None = None,
    ) -> Message:  # 2309/2349
        """Override zone setpoint for a duration or indefinitely."""
        from ramses_rf.enums import Action

        # Hometronics doesn't support 2349
        if mode is not None or until is not None:
            return await send_system_intent(
                self,
                Action.SET_MODE,
                {
                    SZ_ZONE_INDEX: self.index,
                    "mode": mode,
                    "setpoint": setpoint,
                    "until": until,
                },
            )
        # unsure if Hometronics supports setpoint of None
        elif setpoint is not None:
            return await send_system_intent(
                self,
                Action.SET_TEMPERATURE,
                {
                    SZ_ZONE_INDEX: self.index,
                    "setpoint": setpoint,
                },
            )
        else:
            raise exc.CommandInvalid("Invalid mode/setpoint")

    async def set_name(self, name: str) -> Message:
        """Set the zone's name in the CTL."""
        return await send_system_intent(
            self,
            Action.SET_ZONE_NAME,
            {
                SZ_ZONE_INDEX: self.index,
                "name": name,
            },
        )

    async def schema(self) -> dict[str, Any]:
        """Return the schema of the zone (type, devices)."""
        return {
            f"_{SZ_NAME}": await self.name(),
            SZ_CLASS: self.heating_type,
            SZ_SENSOR: self._sensor.id if self._sensor else None,
            SZ_ACTUATORS: sorted([d.id for d in self.actuators]),
        }

    async def params(self) -> dict[str, Any]:
        """Return the zone's configuration (excl. schedule)."""
        return {
            "config": await self.config(),
            "mode": await self.mode(),
            "name": await self.name(),
            "setpoint_bounds": await self.setpoint_bounds(),
        }

    async def status(self) -> dict[str, Any]:
        """Return the zone's current state."""
        return {
            SZ_SETPOINT: await self.setpoint(),
            SZ_TEMPERATURE: await self.temperature(),
            SZ_HEAT_DEMAND: await self.heat_demand(),
        }

    @property
    def circuits(self) -> list[UfhCircuitDTO]:
        """Return the list of UFH circuits bound to this zone.

        :returns: List of immutable UfhCircuitDTO transfer objects.
        :rtype: list[UfhCircuitDTO]
        """
        return []

    @property
    def circuit_entities(self) -> list[UfhCircuit]:
        """Return the list of UFH circuit entities bound to this zone.

        :returns: List of UfhCircuit entity instances.
        :rtype: list[UfhCircuit]
        """
        return []


class EleZone(Zone):  # BDR91A/T  # TODO: 0008/0009/3150
    """Electric load controlled by a relay (never calls for heat)."""

    # NOTE: since zones are promotable, we can't use this here
    # def __init__(self,...

    _SLUG: str | None = ZoneRole.ELE
    _ROLE_ACTUATORS: str = DEV_ROLE_MAP.ELE

    async def heat_demand(self) -> float | None:
        """Return 0, as electric zones do not call for heat."""
        return 0

    # 0008 (NOTE: CTLs won't RP|0008)
    async def relay_demand(self) -> float | None:
        """Return the electric relay demand percentage (0.0 to 1.0)."""
        return self.demand_state.relay_demand

    async def status(self) -> dict[str, Any]:
        """Return the current zone operating status dictionary."""
        return {
            **(await super().status()),
            SZ_RELAY_DEMAND: await self.relay_demand(),
        }


class MixZone(Zone):  # HM80  # TODO: 0008/0009/3150
    """Modulating valve controlled by HM80 (also calls for heat).

    Note that HM80s are listen-only devices.
    """

    # NOTE: since zones are promotable, we can't use this here
    # def __init__(self,...

    _SLUG: str | None = ZoneRole.MIX
    _ROLE_ACTUATORS: str = DEV_ROLE_MAP.MIX

    async def mix_config(self) -> PayDictT._1030 | None:
        """Return the mixing valve configuration (1030) or None."""
        return await self.entity_state.get_value(Code._1030)

    async def params(self) -> dict[str, Any]:
        """Return zone parameters including mixing configuration."""
        return {
            **(await super().params()),
            "mix_config": await self.mix_config(),
        }


class RadZone(Zone):  # HR92/HR80
    """For radiators controlled by HR92s or HR80s (will also call heat)."""

    # NOTE: since zones are promotable, we can't use this here
    # def __init__(self,...

    _SLUG: str | None = ZoneRole.RAD
    _ROLE_ACTUATORS: str = DEV_ROLE_MAP.RAD


class UfhZone(Zone):  # HCC80/HCE80/HCC100
    """For underfloor heating controlled by HCE80/HCC80/HCC100.

    Calls for heat from underfloor heating controllers.
    """

    # NOTE: since zones are promotable, we can't use this here
    # def __init__(self,...

    _SLUG: str | None = ZoneRole.UFH
    _ROLE_ACTUATORS: str = DEV_ROLE_MAP.UFH

    async def heat_demand(self) -> float | None:  # 3150
        """Return the zone's heat demand aggregated from bound UFH circuits.

        Underfloor heating wax actuators operate on linear PWM/TPI duty
        cycles (0.0 to 1.0) and are not subject to TRV pin stroke
        transformation.

        :returns: Linear heat demand percentage (0.0 to 1.0) or None.
        :rtype: float | None
        """
        if self.circuit_entities:
            demands = [
                circuit.heat_demand
                for circuit in self.circuit_entities
                if circuit.heat_demand is not None
            ]
            if demands:
                return max(demands)
        return self.demand_state.heat_demand

    @property
    def circuit_entities(self) -> list[UfhCircuit]:
        """Return the list of UFH circuit entities bound to this UFH zone.

        :returns: List of UfhCircuit entity instances sorted by circuit index.
        :rtype: list[UfhCircuit]
        """
        circuits: list[UfhCircuit] = []
        tcs = getattr(self, "tcs", None)
        if tcs:
            for child in getattr(tcs, "childs", []):
                if getattr(child, "_SLUG", None) == DevType.UFC or getattr(
                    child, "type", None
                ) in (DevType.UFC, DEV_TYPE_MAP.UFC, "02"):
                    for circuit in getattr(child, SZ_CIRCUITS, []):
                        if (
                            getattr(circuit, SZ_ZONE_INDEX, None) == self.index
                            or getattr(circuit, "_zone", None) is self
                        ) and circuit not in circuits:
                            circuits.append(circuit)
        return sorted(circuits, key=lambda circuit: circuit.ufh_index)

    @property
    def circuits(self) -> list[UfhCircuitDTO]:
        """Return the list of UFH circuits bound to this UFH zone.

        :returns: List of immutable UfhCircuitDTO transfer objects.
        :rtype: list[UfhCircuitDTO]
        """
        return [circuit.to_dto() for circuit in self.circuit_entities]


class ValZone(EleZone):  # BDR91A/T
    """For a motorised valve controlled by a BDR91 (will also call heat)."""

    # NOTE: since zones are promotable, we can't use this here
    # def __init__(self,...

    _SLUG: str | None = ZoneRole.VAL
    _ROLE_ACTUATORS: str = DEV_ROLE_MAP.VAL

    async def heat_demand(self) -> float | None:  # 0008 (NOTE: not 3150)
        """Return the zone's heat demand, using relay demand as a proxy."""
        return await self.relay_demand()


def _transform(valve_pos: float) -> float:
    """Transform valve position (0-200) into demand percentage."""
    # import math
    valve_pos = valve_pos * 100
    if valve_pos <= 30:
        return 0
    t0, t1, t2 = (0, 30, 70) if valve_pos <= 70 else (30, 70, 100)
    return math.floor((valve_pos - t1) * t1 / (t2 - t1) + t0 + 0.5) / 100


# e.g. {"RAD": RadZone}
ZONE_CLASS_BY_SLUG: dict[str, type[DhwZone] | type[Zone]] = class_by_attr(
    __name__, "_SLUG"
)


def zone_factory(
    tcs: _StoredHwT | _MultiZoneT,
    zone_index: str,
    *,
    msg: Message | None = None,
    **schema: Any,
) -> DhwZone | Zone:
    """Return zone class for given zone_index and schema."""

    def best_zon_class(
        controller_address: Address,
        zone_index: str,
        *,
        msg: Message | None = None,
        eavesdrop: bool = False,
        **schema: Any,
    ) -> type[DhwZone] | type[Zone]:
        """Return initial zone class for given zone_index and schema."""
        # NOTE: for now, zones are always promoted after instantiation

        # a specified zone class always takes precedence (even if it
        # is wrong)...
        if (sz_cls := schema.get(SZ_CLASS)) and (
            cls := ZONE_CLASS_BY_SLUG.get(str(sz_cls))
        ):
            _LOGGER.debug(
                f"Using an explicitly-defined zone class for: {controller_address}_{zone_index} ({cls})"
            )
            return cls

        # or, is it a DHW zone, derived from the zone index...
        if zone_index == "HW":
            _LOGGER.debug(
                f"Using the default class for: {controller_address}_{zone_index} ({DhwZone._SLUG})"
            )
            return DhwZone

        # otherwise, use the generic heating zone class...
        _LOGGER.debug(
            f"Using a promotable zone class for: {controller_address}_{zone_index} ({Zone._SLUG})"
        )
        return Zone

    zon = best_zon_class(
        tcs.ctl.addr,
        zone_index,
        msg=msg,
        eavesdrop=tcs._gateway.config.enable_eavesdrop,
        **schema,
    ).create_from_schema(tcs, zone_index, **schema)

    return zon


_ZoneT = TypeVar("_ZoneT", bound="ZoneBase")
