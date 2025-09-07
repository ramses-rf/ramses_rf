#!/usr/bin/env python3
"""RAMSES RF - The evohome-compatible zones."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime as dt, timedelta as td
from typing import TYPE_CHECKING, Any, TypeVar

from ramses_rf import exceptions as exc
from ramses_rf.const import (
    DEV_ROLE_MAP,
    DEV_TYPE_MAP,
    SZ_DOMAIN_ID,
    SZ_HEAT_DEMAND,
    SZ_NAME,
    SZ_RELAY_DEMAND,
    SZ_RELAY_FAILSAFE,
    SZ_SETPOINT,
    SZ_TEMPERATURE,
    SZ_WINDOW_OPEN,
    SZ_ZONE_IDX,
    SZ_ZONE_TYPE,
    ZON_MODE_MAP,
    ZON_ROLE_MAP,
    DevRole,
    ZoneRole,
)
from ramses_rf.device import (
    BdrSwitch,
    Controller,
    Device,
    DhwSensor,
    TrvActuator,
    UfhController,
)
from ramses_rf.entity_base import Child, Entity, Parent, class_by_attr
from ramses_rf.helpers import shrink
from ramses_rf.schemas import (
    SCH_TCS_DHW,
    SCH_TCS_ZONES_ZON,
    SZ_ACTUATORS,
    SZ_CLASS,
    SZ_DEVICES,
    SZ_DHW_VALVE,
    SZ_HTG_VALVE,
    SZ_SENSOR,
)
from ramses_tx import Address, Command, Message, Priority

from .schedule import InnerScheduleT, OuterScheduleT, Schedule

if TYPE_CHECKING:
    from ramses_tx import Packet
    from ramses_tx.schemas import DeviceIdT, DevIndexT
    from ramses_tx.typed_dicts import PayDictT

    from .heat import Evohome, _MultiZoneT, _StoredHwT


# Kudos & many thanks to:
# - @dbmandrake: valve_position -> heat_demand transform


from ramses_rf.const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    F9,
    FA,
    FC,
    FF,
)

from ramses_rf.const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

_LOGGER = logging.getLogger(__name__)


class ZoneBase(Child, Parent, Entity):
    """The Zone/DHW base class."""

    _SLUG: str = None

    _ROLE_ACTUATORS: str = None
    _ROLE_SENSORS: str = None

    def __init__(self, tcs: _MultiZoneT | _StoredHwT, zone_idx: str) -> None:
        super().__init__(tcs._gwy)

        # FIXME: ZZZ entities must know their parent device ID and their own idx
        self._z_id = tcs.id  # the responsible device is the controller
        self._z_idx: DevIndexT = zone_idx  # the zone idx (ctx), 00-0B (or 0F), HW (FA)

        self.id: str = f"{tcs.id}_{zone_idx}"

        self.tcs: Evohome = tcs
        self.ctl: Controller = tcs.ctl
        self._child_id: str = zone_idx

        self._name = None  # param attr

    # Should be a private method
    @classmethod
    def create_from_schema(
        cls, tcs: _MultiZoneT, zone_idx: str, **schema: Any
    ) -> ZoneBase:
        """Create a CH/DHW zone for a TCS and set its schema attrs.

        The appropriate Zone class should have been determined by a factory.
        Can be a heating zone (of a klass), or the DHW subsystem (idx must be 'HW').
        """

        zon = cls(tcs, zone_idx)  # type: ignore[arg-type]
        zon._update_schema(**schema)
        return zon

    def _update_schema(self, **schema: Any) -> None:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{self.id} ({self._SLUG})"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ZoneBase):
            return NotImplemented
        return self.idx < other.idx

    @property
    def idx(self) -> str:
        return self._child_id

    @property
    def schema(self) -> dict[str, Any]:
        """Return the schema (can't change without destroying/re-creating entity)."""
        return {}

    @property
    def params(self) -> dict[str, Any]:
        """Return configuration (can be changed by user)."""
        return {}

    @property
    def status(self) -> dict[str, Any]:
        """Return the current state."""
        return {}


class ZoneSchedule(ZoneBase):  # 0404
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self._schedule = Schedule(self)  # type: ignore[arg-type]

    def _handle_msg(self, msg: Message) -> None:
        super()._handle_msg(msg)

        if msg.code in (Code._0006, Code._0404):
            self._schedule._handle_msg(msg)

    async def get_schedule(self, *, force_io: bool = False) -> InnerScheduleT | None:
        await self._schedule.get_schedule(force_io=force_io)
        return self.schedule

    async def set_schedule(self, schedule: OuterScheduleT) -> InnerScheduleT | None:
        await self._schedule.set_schedule(schedule)  # type: ignore[arg-type]
        return self.schedule

    @property
    def schedule(self) -> InnerScheduleT | None:
        """Return the latest retrieved schedule (not guaranteed to be up to date)."""
        # inner: [{"day_of_week": 0, "switchpoints": [...], {"day_of_week": 1, ...
        # outer: {"zone_idx": "01", "schedule": <inner>

        return self._schedule.schedule

    @property
    def schedule_version(self) -> int | None:  # TODO: make int
        """Return the version number associated with the latest retrieved schedule."""
        return self._schedule.version

    @property
    def status(self) -> dict[str, Any]:
        return {
            **super().status,
            "schedule_version": self.schedule_version,
        }


class DhwZone(ZoneSchedule):  # CS92A
    """The DHW class."""

    _SLUG: str = ZoneRole.DHW

    def __init__(self, tcs: _StoredHwT, zone_idx: str = "HW") -> None:
        _LOGGER.debug("Creating a DHW for TCS: %s_HW (%s)", tcs.id, self.__class__)

        if tcs.dhw:
            raise LookupError(f"Duplicate DHW for TCS: {tcs.id}")
        if zone_idx not in (None, "HW"):
            raise ValueError(f"Invalid zone idx for DHW: {zone_idx} (not 'HW'/null)")

        super().__init__(tcs, "HW")

        # DhwZones have a sensor, but actuators are optional, depending on schema
        self._dhw_sensor: DhwSensor | None = None
        self._dhw_valve: BdrSwitch | None = None
        self._htg_valve: BdrSwitch | None = None

    def _setup_discovery_cmds(self) -> None:
        # super()._setup_discovery_cmds()

        for payload in (
            f"00{DEV_ROLE_MAP.DHW}",  # sensor
            f"00{DEV_ROLE_MAP.HTG}",  # hotwater_valve
            f"01{DEV_ROLE_MAP.HTG}",  # heating_valve
        ):
            self._add_discovery_cmd(
                Command.from_attrs(RQ, self.ctl.id, Code._000C, payload), 60 * 60 * 24
            )

        self._add_discovery_cmd(Command.get_dhw_params(self.ctl.id), 60 * 60 * 6)

        self._add_discovery_cmd(Command.get_dhw_mode(self.ctl.id), 60 * 5)
        self._add_discovery_cmd(Command.get_dhw_temp(self.ctl.id), 60 * 15)

    def _handle_msg(self, msg: Message) -> None:
        # def eavesdrop_dhw_sensor(this: Message, *, prev: Message | None = None) -> None:
        # """Eavesdrop packets, or pairs of packets, to maintain the system state.

        # There are only 2 ways to to find a controller's DHW sensor:
        # 1. The 10A0 RQ/RP *from/to a 07:* (1x/4h) - reliable
        # 2. Use sensor temp matching - non-deterministic

        # Data from the CTL is considered more authoritative. The RQ is initiated by the
        # DHW, so is not authoritative. The I/1260 is not to/from a controller, so is
        # not useful.
        # """

        # # 10A0: RQ/07/01, RP/01/07: can get both parent controller & DHW sensor
        # # 047 RQ --- 07:030741 01:102458 --:------ 10A0 006 00181F0003E4
        # # 062 RP --- 01:102458 07:030741 --:------ 10A0 006 0018380003E8

        # # 1260: I/07: can't get which parent controller - would need to match temps
        # # 045  I --- 07:045960 --:------ 07:045960 1260 003 000911

        # # 1F41: I/01: get parent controller, but not DHW sensor
        # # 045  I --- 01:145038 --:------ 01:145038 1F41 012 000004FFFFFF1E060E0507E4
        # # 045  I --- 01:145038 --:------ 01:145038 1F41 006 000002FFFFFF

        # assert self._gwy.config.enable_eavesdrop, "Coding error"

        # if all(
        #     (
        #         this.code == Code._10A0,
        #         this.verb == RP,
        #         this.src is self.ctl,
        #         isinstance(this.dst, DhwSensor),
        #     )
        # ):
        #     self._get_dhw(sensor=this.dst)

        assert (
            msg.src == self.ctl
            and msg.code in (Code._0005, Code._000C, Code._10A0, Code._1260, Code._1F41)
            or msg.payload.get(SZ_DOMAIN_ID) in (F9, FA)
            or msg.payload.get(SZ_ZONE_IDX) == "HW"
        ), f"msg inappropriately routed to {self}"

        super()._handle_msg(msg)

        if (
            msg.code != Code._000C
            or msg.payload[SZ_ZONE_TYPE] not in (DEV_ROLE_MAP.DHW, DEV_ROLE_MAP.HTG)
            or not msg.payload[SZ_DEVICES]
        ):
            return

        assert len(msg.payload[SZ_DEVICES]) == 1

        self._gwy.get_device(
            msg.payload[SZ_DEVICES][0],
            parent=self,
            child_id=msg.payload[SZ_DOMAIN_ID],
            is_sensor=(msg.payload[SZ_ZONE_TYPE] == DEV_ROLE_MAP.DHW),
        )  # sets self._dhw_sensor/_dhw_valve/_htg_valve

        # TODO: may need to move earlier in method
        # # If still don't have a sensor, can eavesdrop 10A0
        # if self._gwy.config.enable_eavesdrop and not self.dhw_sensor:
        #     eavesdrop_dhw_sensor(msg)

    def _update_schema(self, **schema: Any) -> None:
        """Update a DHW zone with new schema attrs.

        Raise an exception if the new schema is not a superset of the existing schema.
        """

        """Set the temp sensor for this DHW zone (07: only)."""
        """Set the heating valve relay for this DHW zone (13: only)."""
        """Set the hotwater valve relay for this DHW zone (13: only).

        Check and ??? the DHW sensor (07:) of this system/CTL (if there is one).

        There is only 1 way to eavesdrop a controller's DHW sensor:
        1.  The 10A0 RQ/RP *from/to a 07:* (1x/4h)

        The RQ is initiated by the DHW, so is not authoritative (the CTL will RP any RQ).
        The I/1260 is not to/from a controller, so is not useful.
        """

        schema = shrink(SCH_TCS_DHW(schema))

        if dev_id := schema.get(SZ_SENSOR):
            dhw_sensor = self._gwy.get_device(
                dev_id, parent=self, child_id=FA, is_sensor=True
            )
            assert isinstance(dhw_sensor, DhwSensor)  # mypy
            self._dhw_sensor = dhw_sensor

        if dev_id := schema.get(DEV_ROLE_MAP[DevRole.HTG]):
            dhw_valve = self._gwy.get_device(dev_id, parent=self, child_id=FA)
            assert isinstance(dhw_valve, BdrSwitch)  # mypy
            self._dhw_valve = dhw_valve

        if dev_id := schema.get(DEV_ROLE_MAP[DevRole.HT1]):
            htg_valve = self._gwy.get_device(dev_id, parent=self, child_id=F9)
            assert isinstance(htg_valve, BdrSwitch)  # mypy
            self._htg_valve = htg_valve

    @property
    def sensor(self) -> DhwSensor | None:  # self._dhw_sensor
        return self._dhw_sensor

    @property
    def hotwater_valve(self) -> BdrSwitch | None:  # self._dhw_valve
        return self._dhw_valve

    @property
    def heating_valve(self) -> BdrSwitch | None:  # self._htg_valve
        return self._htg_valve

    @property
    def name(self) -> str:
        return "Stored HW"

    @property
    def config(self) -> dict[str, Any] | None:  # 10A0
        return self._msg_value(Code._10A0)  # type: ignore[return-value]

    @property
    def mode(self) -> dict[str, Any] | None:  # 1F41
        return self._msg_value(Code._1F41)  # type: ignore[return-value]

    @property
    def setpoint(self) -> float | None:  # 10A0
        return self._msg_value(Code._10A0, key=SZ_SETPOINT)  # type: ignore[return-value]

    @setpoint.setter  # TODO: can value be None?
    def setpoint(self, value: float) -> None:  # 10A0
        self.set_config(setpoint=value)

    @property
    def temperature(self) -> float | None:  # 1260
        return self._msg_value(Code._1260, key=SZ_TEMPERATURE)  # type: ignore[return-value]

    @property
    def heat_demand(self) -> float | None:  # 3150
        return self._msg_value(Code._3150, key=SZ_HEAT_DEMAND)  # type: ignore[return-value]

    @property
    def relay_demand(self) -> float | None:  # 0008
        return self._msg_value(Code._0008, key=SZ_RELAY_DEMAND)  # type: ignore[return-value]

    @property  # only seen with FC, but seems should pair with 0008?
    def relay_failsafe(self) -> float | None:  # 0009
        return self._msg_value(Code._0009, key=SZ_RELAY_FAILSAFE)  # type: ignore[return-value]

    def set_mode(
        self,
        *,
        mode: int | str | None = None,
        active: bool | None = None,
        until: dt | str | None = None,
    ) -> asyncio.Task[Packet]:
        """Set the DHW mode (mode, active, until)."""

        cmd = Command.set_dhw_mode(self.ctl.id, mode=mode, active=active, until=until)
        return self._gwy.send_cmd(cmd, priority=Priority.HIGH, wait_for_reply=True)

    def set_boost_mode(self) -> asyncio.Task[Packet]:
        """Enable DHW for an hour, despite any schedule."""
        return self.set_mode(
            mode=ZON_MODE_MAP.TEMPORARY,
            active=True,
            until=dt.now() + td(hours=1),
        )

    def reset_mode(self) -> asyncio.Task[Packet]:  # 1F41
        """Revert the DHW to following its schedule."""
        return self.set_mode(mode=ZON_MODE_MAP.FOLLOW)

    def set_config(
        self,
        *,
        setpoint: float | None = None,
        overrun: int | None = None,
        differential: float | None = None,
    ) -> asyncio.Task[Packet]:
        """Set the DHW parameters (setpoint, overrun, differential)."""

        # dhw_params = self._msg_value(Code._10A0)
        # if setpoint is None:
        #     setpoint = dhw_params[SZ_SETPOINT]
        # if overrun is None:
        #     overrun = dhw_params["overrun"]
        # if differential is None:
        #     setpoint = dhw_params["differential"]

        cmd = Command.set_dhw_params(
            self.ctl.id,
            setpoint=setpoint,
            overrun=overrun,
            differential=differential,
        )
        return self._gwy.send_cmd(cmd, priority=Priority.HIGH)

    def reset_config(self) -> asyncio.Task[Packet]:  # 10A0
        """Reset the DHW parameters to their default values."""
        return self.set_config(setpoint=50, overrun=5, differential=1)

    @property
    def schema(self) -> dict[str, Any]:
        """Return the schema of the DHW's."""
        return {
            SZ_SENSOR: self.sensor.id if self.sensor else None,
            SZ_DHW_VALVE: self.hotwater_valve.id if self.hotwater_valve else None,
            SZ_HTG_VALVE: self.heating_valve.id if self.heating_valve else None,
        }

    @property
    def params(self) -> dict[str, Any]:
        """Return the DHW's configuration (excl. schedule)."""
        return {a: getattr(self, a) for a in ("config", "mode")}

    @property
    def status(self) -> dict[str, Any]:
        """Return the DHW's current state."""
        return {a: getattr(self, a) for a in (SZ_TEMPERATURE, SZ_HEAT_DEMAND)}


class Zone(ZoneSchedule):
    """The Zone class for all zone types (but not DHW)."""

    _SLUG: str = None
    _ROLE_ACTUATORS: str = DEV_ROLE_MAP.ACT

    def __init__(self, tcs: _MultiZoneT, zone_idx: str) -> None:
        """Create a heating zone.

        The type of zone may not be known at instantiation. Even when it is known, zones
        are still created without a type before they are subsequently promoted, so that
        both schemes (e.g. eavesdropping, vs probing) are the same.

        In addition, an electric zone may subsequently turn out to be a zone valve zone.
        """
        _LOGGER.debug("Creating a Zone: %s_%s (%s)", tcs.id, zone_idx, self.__class__)

        if zone_idx in tcs.zone_by_idx:
            raise LookupError(f"Duplicate ZON for TCS: {tcs.id}_{zone_idx}")
        if int(zone_idx, 16) >= tcs._max_zones:
            raise ValueError(f"Invalid zone_idx: {zone_idx} (exceeds max_zones)")

        super().__init__(tcs, zone_idx)

        self._sensor: Device | None = None
        self.actuators: list[Device] = []
        self.actuator_by_id: dict[DeviceIdT, Device] = {}

    def _update_schema(self, **schema: Any) -> None:
        """Update a heating zone with new schema attrs.

        Raise an exception if the new schema is not a superset of the existing schema.
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
                raise TypeError

            klass = ZON_ROLE_MAP.slug(zone_type)  # not incl. DHW?

            if klass == self._SLUG:
                return

            if klass == ZoneRole.VAL and self._SLUG not in (
                None,
                ZoneRole.ELE,
            ):
                raise ValueError(f"Not a compatible zone class for {self}: {zone_type}")

            elif klass not in ZONE_CLASS_BY_SLUG:
                raise ValueError(f"Not a known zone class (for {self}): {zone_type}")

            if self._SLUG is not None:
                raise exc.SystemSchemaInconsistent(
                    f"{self} changed zone class: from {self._SLUG} to {klass}"
                )

            self.__class__ = ZONE_CLASS_BY_SLUG[klass]
            _LOGGER.debug("Promoted a Zone: %s (%s)", self.id, self.__class__)

            self._setup_discovery_cmds()

        dev_id: DeviceIdT

        # if schema.get(SZ_CLASS) == ZON_ROLE_MAP[ZON_ROLE.ACT]:
        #     schema.pop(SZ_CLASS)
        schema = shrink(SCH_TCS_ZONES_ZON(schema))

        if klass := schema.get(SZ_CLASS):
            set_zone_type(ZON_ROLE_MAP[klass])

        if dev_id := schema.get(SZ_SENSOR):
            self._sensor = self._gwy.get_device(dev_id, parent=self, is_sensor=True)

        for dev_id in schema.get(SZ_ACTUATORS, []):
            self._gwy.get_device(dev_id, parent=self)

    def _setup_discovery_cmds(self) -> None:
        # super()._setup_discovery_cmds()

        for dev_role in (self._ROLE_ACTUATORS, DEV_ROLE_MAP.SEN):
            cmd = Command.from_attrs(
                RQ, self.ctl.id, Code._000C, f"{self.idx}{dev_role}"
            )
            self._add_discovery_cmd(cmd, 60 * 60 * 24, delay=0.5)

        self._add_discovery_cmd(
            Command.get_zone_config(self.ctl.id, self.idx), 60 * 60 * 6, delay=30
        )  # td should be > long sync_cycle duration (> 1hr)
        self._add_discovery_cmd(
            Command.get_zone_name(self.ctl.id, self.idx), 60 * 60 * 6, delay=30
        )

        self._add_discovery_cmd(  # 2349 instead of 2309
            Command.get_zone_mode(self.ctl.id, self.idx), 60 * 5, delay=30
        )
        self._add_discovery_cmd(  # 30C9
            Command.get_zone_temp(self.ctl.id, self.idx), 60 * 5, delay=0
        )  # td should be > sync_cycle duration,?delay in hope of picking up cycle
        self._add_discovery_cmd(
            Command.get_zone_window_state(self.ctl.id, self.idx), 60 * 15, delay=60 * 5
        )  # longer dt as low yield (factory duration is 30 min): prefer eavesdropping

    def _add_discovery_cmd(
        self,
        cmd: Command,
        interval: float,
        *,
        delay: float = 0,
        timeout: float | None = None,
    ) -> None:
        """Schedule a command to run periodically.

        Both `timeout` and `delay` are in seconds.
        """
        super()._add_discovery_cmd(cmd, interval, delay=delay, timeout=timeout)

        if cmd.code != Code._000C:  # or cmd._ctx == f"{self.idx}{ZON_ROLE_MAP.SEN}":
            return

        if [t for t in self._discovery_cmds if t[-2:] in ZON_ROLE_MAP.HEAT_ZONES] and (
            self._discovery_cmds.pop(f"{self.idx}{ZON_ROLE_MAP.ACT}", [])
        ):
            _LOGGER.warning(f"cmd({cmd}): inferior header removed from discovery")

        if (
            self._discovery_cmds.get(f"{self.idx}{ZON_ROLE_MAP.VAL}")
            and (self._discovery_cmds[f"{self.idx}{ZON_ROLE_MAP.ELE}"])
        ):
            _LOGGER.warning(f"cmd({cmd}): inferior header removed from discovery")

    def _handle_msg(self, msg: Message) -> None:
        def eavesdrop_zone_type(this: Message, *, prev: Message | None = None) -> None:
            """TODO.

            There are three ways to determine the type of a zone:
            1. Use a 0005 packet (deterministic)
            2. Eavesdrop (non-deterministic, slow to converge)
            3. via a config file (a schema)
            """
            # ELE/VAL, but not UFH (it seems)
            if this.code in (Code._0008, Code._0009):
                assert self._SLUG in (
                    None,
                    ZoneRole.ELE,
                    ZoneRole.VAL,
                    ZoneRole.MIX,
                ), self._SLUG

                if self._SLUG is None:
                    # this might eventually be: ZON_ROLE.VAL
                    self._update_schema(**{SZ_CLASS: ZON_ROLE_MAP[ZoneRole.ELE]})

            elif this.code == Code._3150:  # TODO: and this.verb in (I_, RP)?
                # MIX/ELE don't 3150
                assert self._SLUG in (
                    None,
                    ZoneRole.RAD,
                    ZoneRole.UFH,
                    ZoneRole.VAL,
                ), self._SLUG

                if isinstance(this.src, TrvActuator):
                    self._update_schema(**{SZ_CLASS: ZON_ROLE_MAP[ZoneRole.RAD]})
                elif isinstance(this.src, BdrSwitch):
                    self._update_schema(**{SZ_CLASS: ZON_ROLE_MAP[ZoneRole.VAL]})
                elif isinstance(this.src, UfhController):
                    self._update_schema(**{SZ_CLASS: ZON_ROLE_MAP[ZoneRole.UFH]})

            assert (
                msg.src == self.ctl or msg.src.type == DEV_TYPE_MAP.UFC
            ) and (  # DEX
                isinstance(msg.payload, dict)
                or [d for d in msg.payload if d.get(SZ_ZONE_IDX) == self.idx]
            ), f"msg inappropriately routed to {self}"

        assert (msg.src == self.ctl or msg.src.type == DEV_TYPE_MAP.UFC) and (  # DEX
            isinstance(msg.payload, list)
            or msg.code == Code._0005
            or msg.payload.get(SZ_ZONE_IDX) == self.idx
        ), f"msg inappropriately routed to {self}"

        super()._handle_msg(msg)

        if msg.code == Code._000C:
            if not msg.payload[SZ_DEVICES]:
                return

            if msg.payload[SZ_ZONE_TYPE] == DEV_ROLE_MAP.SEN:
                dev_id = msg.payload[SZ_DEVICES][0]
                self._sensor = self._gwy.get_device(dev_id, parent=self, is_sensor=True)

            elif msg.payload[SZ_ZONE_TYPE] == DEV_ROLE_MAP.ACT:
                for dev_id in msg.payload[SZ_DEVICES]:
                    self._gwy.get_device(dev_id, parent=self)

            elif msg.payload[SZ_ZONE_TYPE] in ZON_ROLE_MAP.HEAT_ZONES:
                for dev_id in msg.payload[SZ_DEVICES]:
                    self._gwy.get_device(dev_id, parent=self)
                self._update_schema(
                    **{SZ_CLASS: ZON_ROLE_MAP[msg.payload[SZ_ZONE_TYPE]]}
                )

            # TODO: testing this concept, hoping to learn device_id of UFC
            # if msg.payload[SZ_ZONE_TYPE] == DEV_ROLE_MAP.UFH:
            #     cmd = Command.from_attrs(
            #         RQ, self.ctl.id, Code._000C, f"{self.idx}{DEV_ROLE_MAP.UFH}"
            #     )
            #     self._send_cmd(cmd)

        # If zone still doesn't have a zone class, maybe eavesdrop?
        if self._gwy.config.enable_eavesdrop and self._SLUG in (
            None,
            ZoneRole.ELE,
        ):
            eavesdrop_zone_type(msg)

    def _msg_value(self, *args: Any, **kwargs: Any) -> Any:
        return super()._msg_value(*args, **kwargs, zone_idx=self.idx)

    @property
    def sensor(self) -> Device | None:
        return self._sensor

    @property
    def heating_type(self) -> str | None:
        """Return the type of the zone/DHW (e.g. electric_zone, stored_dhw)."""

        if self._SLUG is None:  # isinstance(self, ???)
            return None
        return ZON_ROLE_MAP[self._SLUG]  # type: ignore[no-any-return]

    @property
    def name(self) -> str | None:  # 0004
        """Return the name of the zone."""

        if self._gwy._zzz:
            msgs = self._gwy._zzz.get(code=Code._0004, src=self._z_id, ctx=self._z_idx)
            return msgs[0].payload.get(SZ_NAME) if msgs else None

        return self._msg_value(Code._0004, key=SZ_NAME)  # type: ignore[no-any-return]

    @name.setter
    def name(self, value: str) -> None:
        raise NotImplementedError("The setter has been deprecated, use: .set_name()")

    @property
    def config(self) -> dict[str, Any] | None:  # 000A
        return self._msg_value(Code._000A)  # type: ignore[no-any-return]

    @property
    def mode(self) -> dict[str, Any] | None:  # 2349
        return self._msg_value(Code._2349)  # type: ignore[no-any-return]

    @property
    def setpoint(self) -> float | None:  # 2309 (2349 is a superset of 2309)
        return self._msg_value((Code._2309, Code._2349), key=SZ_SETPOINT)  # type: ignore[no-any-return]

    @setpoint.setter  # TODO: can value be None?
    def setpoint(self, value: float) -> None:  # 000A/2309
        """Set the target temperature, until the next scheduled setpoint."""

        if value is None:
            return self.reset_mode()

        cmd = Command.set_zone_setpoint(self.ctl.id, self.idx, value)
        self._gwy.send_cmd(cmd, priority=Priority.HIGH)

    @property
    def temperature(self) -> float | None:  # 30C9
        return self._msg_value(Code._30C9, key=SZ_TEMPERATURE)  # type: ignore[no-any-return]

    @property
    def heat_demand(self) -> float | None:  # 3150
        """Return the zone's heat demand, estimated from its devices' heat demand."""
        demands = [
            d.heat_demand
            for d in self.actuators  # TODO: actuators
            if hasattr(d, SZ_HEAT_DEMAND) and d.heat_demand is not None
        ]
        return _transform(max(demands + [0])) if demands else None

    @property
    def window_open(self) -> bool | None:  # 12B0
        """Return an estimate of the zone's current window_open state."""
        return self._msg_value(Code._12B0, key=SZ_WINDOW_OPEN)  # type: ignore[no-any-return]

    def _get_temp(self) -> asyncio.Task[Packet] | None:
        """Get the zone's latest temp from the Controller."""
        return self._send_cmd(Command.get_zone_temp(self.ctl.id, self.idx))

    def reset_config(self) -> asyncio.Task[Packet]:  # 000A
        """Reset the zone's parameters to their default values."""
        return self.set_config()

    def set_config(
        self,
        *,
        min_temp: float = 5,
        max_temp: float = 35,
        local_override: bool = False,
        openwindow_function: bool = False,
        multiroom_mode: bool = False,
    ) -> asyncio.Task[Packet]:
        """Set the zone's parameters (min_temp, max_temp, etc.)."""

        cmd = Command.set_zone_config(
            self.ctl.id,
            self.idx,
            min_temp=min_temp,
            max_temp=max_temp,
            local_override=local_override,
            openwindow_function=openwindow_function,
            multiroom_mode=multiroom_mode,
        )
        return self._gwy.send_cmd(cmd, priority=Priority.HIGH)

    def reset_mode(self) -> asyncio.Task[Packet]:  # 2349
        """Revert the zone to following its schedule."""
        return self.set_mode(mode=ZON_MODE_MAP.FOLLOW)

    def set_frost_mode(self) -> asyncio.Task[Packet]:  # 2349
        """Set the zone to the lowest possible setpoint, indefinitely."""
        return self.set_mode(mode=ZON_MODE_MAP.PERMANENT, setpoint=5)  # TODO

    def set_mode(
        self,
        *,
        mode: str | None = None,
        setpoint: float | None = None,
        until: dt | str | None = None,
    ) -> asyncio.Task[Packet]:  # 2309/2349
        """Override the zone's setpoint for a specified duration, or indefinitely."""

        if mode is not None or until is not None:  # Hometronics doesn't support 2349
            cmd = Command.set_zone_mode(
                self.ctl.id, self.idx, mode=mode, setpoint=setpoint, until=until
            )
        elif setpoint is not None:  # unsure if Hometronics supports setpoint of None
            cmd = Command.set_zone_setpoint(self.ctl.id, self.idx, setpoint)
        else:
            raise ValueError("Invalid mode/setpoint")

        return self._gwy.send_cmd(cmd, priority=Priority.HIGH)

    def set_name(self, name: str) -> asyncio.Task[Packet]:
        """Set the zone's name."""

        cmd = Command.set_zone_name(self.ctl.id, self.idx, name)
        return self._gwy.send_cmd(cmd, priority=Priority.HIGH)

    @property
    def schema(self) -> dict[str, Any]:
        """Return the schema of the zone (type, devices)."""

        return {
            f"_{SZ_NAME}": self.name,
            SZ_CLASS: self.heating_type,
            SZ_SENSOR: self._sensor.id if self._sensor else None,
            SZ_ACTUATORS: sorted([d.id for d in self.actuators]),
        }

    @property  # TODO: setpoint
    def params(self) -> dict[str, Any]:
        """Return the zone's configuration (excl. schedule)."""
        return {a: getattr(self, a) for a in ("config", "mode", "name")}

    @property
    def status(self) -> dict[str, Any]:
        """Return the zone's current state."""
        return {
            a: getattr(self, a) for a in (SZ_SETPOINT, SZ_TEMPERATURE, SZ_HEAT_DEMAND)
        }


class EleZone(Zone):  # BDR91A/T  # TODO: 0008/0009/3150
    """For a small electric load controlled by a relay (never calls for heat)."""

    # def __init__(self,...  # NOTE: since zones are promotable, we can't use this here

    _SLUG: str = ZoneRole.ELE
    _ROLE_ACTUATORS: str = DEV_ROLE_MAP.ELE

    def _handle_msg(self, msg: Message) -> None:
        super()._handle_msg(msg)

        # if msg.code == Code._0008:  # ZON zones are ELE zones that also call for heat
        #     self._update_schema(**{SZ_CLASS: ZON_ROLE_MAP[ZON_ROLE.VAL]})
        if msg.code == Code._3150:
            raise TypeError("WHAT 1")
        elif msg.code == Code._3EF0:
            raise TypeError("WHAT 2")

    @property
    def heat_demand(self) -> float | None:
        """Return 0 as the zone's heat demand, as electric zones don't call for heat."""
        return 0

    @property
    def relay_demand(self) -> float | None:  # 0008 (NOTE: CTLs won't RP|0008)
        return self._msg_value(Code._0008, key=SZ_RELAY_DEMAND)  # type: ignore[no-any-return]

    @property
    def status(self) -> dict[str, Any]:
        return {
            **super().status,
            SZ_RELAY_DEMAND: self.relay_demand,
        }


class MixZone(Zone):  # HM80  # TODO: 0008/0009/3150
    """For a modulating valve controlled by a HM80 (will also call for heat).

    Note that HM80s are listen-only devices.
    """

    # def __init__(self,...  # NOTE: since zones are promotable, we can't use this here

    _SLUG: str = ZoneRole.MIX
    _ROLE_ACTUATORS: str = DEV_ROLE_MAP.MIX

    def _setup_discovery_cmds(self) -> None:
        super()._setup_discovery_cmds()

        self._add_discovery_cmd(
            Command.get_mix_valve_params(self.ctl.id, self.idx), 60 * 60 * 6
        )

    @property
    def mix_config(self) -> PayDictT._1030:
        return self._msg_value(Code._1030)  # type: ignore[no-any-return]

    @property
    def params(self) -> dict[str, Any]:
        return {
            **super().status,
            "mix_config": self.mix_config,
        }


class RadZone(Zone):  # HR92/HR80
    """For radiators controlled by HR92s or HR80s (will also call for heat)."""

    # def __init__(self,...  # NOTE: since zones are promotable, we can't use this here

    _SLUG: str = ZoneRole.RAD
    _ROLE_ACTUATORS: str = DEV_ROLE_MAP.RAD


class UfhZone(Zone):  # HCC80/HCE80  # TODO: needs checking
    """For underfloor heating controlled by an HCE80/HCC80 (will also call for heat)."""

    # def __init__(self,...  # NOTE: since zones are promotable, we can't use this here

    _SLUG: str = ZoneRole.UFH
    _ROLE_ACTUATORS: str = DEV_ROLE_MAP.UFH

    @property
    def heat_demand(self) -> float | None:  # 3150
        """Return the zone's heat demand, estimated from its devices' heat demand."""
        if (demand := self._msg_value(Code._3150, key=SZ_HEAT_DEMAND)) is not None:
            return _transform(demand)
        return None


class ValZone(EleZone):  # BDR91A/T
    """For a motorised valve controlled by a BDR91 (will also call for heat)."""

    # def __init__(self,...  # NOTE: since zones are promotable, we can't use this here

    _SLUG: str = ZoneRole.VAL
    _ROLE_ACTUATORS: str = DEV_ROLE_MAP.VAL

    @property
    def heat_demand(self) -> float | None:  # 0008 (NOTE: not 3150)
        """Return the zone's heat demand, using relay demand as a proxy."""
        return self.relay_demand


def _transform(valve_pos: float) -> float:
    """Transform a valve position (0-200) into a demand (%) (as used in the tcs UI)."""
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
    idx: str,
    *,
    msg: Message | None = None,
    **schema: Any,
) -> DhwZone | Zone:
    """Return the zone class for a given zone_idx/klass (Zone or DhwZone).

    Some zones are promotable to a compatible sub class (e.g. ELE->VAL).
    """

    def best_zon_class(
        ctl_addr: Address,
        idx: str,
        *,
        msg: Message | None = None,
        eavesdrop: bool = False,
        **schema: Any,
    ) -> type[DhwZone] | type[Zone]:
        """Return the initial zone class for a given zone_idx/klass (Zone or DhwZone)."""

        # NOTE: for now, zones are always promoted after instantiation

        # # a specified zone class always takes precedence (even if it is wrong)...
        # if cls := ZONE_CLASS_BY_SLUG.get(schema.get(SZ_CLASS)):
        #     _LOGGER.debug(
        #         f"Using an explicitly-defined zone class for: {ctl_addr}_{idx} ({cls})"
        #     )
        #     return cls

        # or, is it a DHW zone, derived from the zone idx...
        if idx == "HW":
            _LOGGER.debug(
                f"Using the default class for: {ctl_addr}_{idx} ({DhwZone._SLUG})"
            )
            return DhwZone

        # try:  # or, a class eavesdropped from the message code/payload...
        #     if cls := best_zon_class(ctl_addr.type, msg=msg, eavesdrop=eavesdrop):
        #         _LOGGER.warning(
        #             f"Using eavesdropped zone class for: {ctl_addr}_{idx} ({cls._SLUG})"
        #         )
        #         return cls  # might be DeviceHvac
        # except TypeError:
        #     pass

        # otherwise, use the generic heating zone class...
        _LOGGER.debug(
            f"Using a promotable zone class for: {ctl_addr}_{idx} ({Zone._SLUG})"
        )
        return Zone

    zon: DhwZone | Zone = best_zon_class(  # type: ignore[type-var]
        tcs.ctl.addr,
        idx,
        msg=msg,
        eavesdrop=tcs._gwy.config.enable_eavesdrop,
        **schema,
    ).create_from_schema(tcs, idx, **schema)

    # assert isinstance(zon, DhwZone | Zone)  # mypy
    return zon


_ZoneT = TypeVar("_ZoneT", bound="ZoneBase")
