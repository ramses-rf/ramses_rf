#!/usr/bin/env python3
"""RAMSES RF - The evohome-compatible system."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime as dt, timedelta as td
from threading import Lock
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn, TypeVar

from ramses_rf.const import (
    SYS_MODE_MAP,
    SZ_ACTUATORS,
    SZ_CHANGE_COUNTER,
    SZ_DATETIME,
    SZ_DEVICES,
    SZ_DHW_IDX,
    SZ_DOMAIN_ID,
    SZ_HEAT_DEMAND,
    SZ_LANGUAGE,
    SZ_SENSOR,
    SZ_SYSTEM_MODE,
    SZ_TEMPERATURE,
    SZ_ZONE_IDX,
    SZ_ZONE_MASK,
    SZ_ZONE_TYPE,
    SZ_ZONES,
)
from ramses_rf.device import (
    BdrSwitch,
    Controller,
    Device,
    OtbGateway,
    Temperature,
    UfhController,
)
from ramses_rf.entity_base import Entity, Parent, class_by_attr
from ramses_rf.helpers import shrink
from ramses_rf.schemas import (
    DEFAULT_MAX_ZONES,
    SCH_TCS,
    SCH_TCS_DHW,
    SCH_TCS_ZONES_ZON,
    SZ_APPLIANCE_CONTROL,
    SZ_CLASS,
    SZ_DHW_SYSTEM,
    SZ_MAX_ZONES,
    SZ_ORPHANS,
    SZ_SYSTEM,
    SZ_UFH_SYSTEM,
)
from ramses_tx import (
    DEV_ROLE_MAP,
    DEV_TYPE_MAP,
    ZON_ROLE_MAP,
    Command,
    DeviceIdT,
    Message,
    Priority,
)
from ramses_tx.typed_dicts import PayDictT

from .faultlog import FaultLog
from .zones import zone_factory

if TYPE_CHECKING:
    from ramses_tx import Address, Packet

    from .faultlog import FaultIdxT, FaultLogEntry
    from .zones import DhwZone, Zone


# TODO: refactor packet routing (filter *before* routing)


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


_SystemT = TypeVar("_SystemT", bound="Evohome")

_StoredHwT = TypeVar("_StoredHwT", bound="StoredHw")
_LogbookT = TypeVar("_LogbookT", bound="Logbook")
_MultiZoneT = TypeVar("_MultiZoneT", bound="MultiZone")


SYS_KLASS = SimpleNamespace(
    SYS="system",  # Generic (promotable?) system
    TCS="evohome",
    PRG="programmer",
)


class SystemBase(Parent, Entity):  # 3B00 (multi-relay)
    """The TCS base class."""

    _SLUG: str = None  # type: ignore[assignment]

    # TODO: check (code so complex, not sure if this is true)
    childs: list[Device]  # type: ignore[assignment]

    def __init__(self, ctl: Controller) -> None:
        _LOGGER.debug("Creating a TCS for CTL: %s (%s)", ctl.id, self.__class__)

        if ctl.id in ctl._gwy.system_by_id:
            raise LookupError(f"Duplicate TCS for CTL: {ctl.id}")
        if not isinstance(ctl, Controller):  # TODO
            raise ValueError(f"Invalid CTL: {ctl} (is not a controller)")

        super().__init__(ctl._gwy)

        # FIXME: ZZZ entities must know their parent device ID and their own idx
        self._z_id = ctl.id  # the responsible device is the controller
        self._z_idx = None  # ? True (sentinel value to pick up arrays?)

        self.id: DeviceIdT = ctl.id

        self.ctl: Controller = ctl
        self.tcs: Evohome = self  # type: ignore[assignment]
        self._child_id = FF  # NOTE: domain_id

        self._app_cntrl: BdrSwitch | OtbGateway | None = None
        self._heat_demand = None

    def __repr__(self) -> str:
        return f"{self.ctl.id} ({self._SLUG})"

    def _setup_discovery_cmds(self) -> None:
        # super()._setup_discovery_cmds()

        for payload in (
            f"00{DEV_ROLE_MAP.APP}",  # appliance_control
            f"00{DEV_ROLE_MAP.HTG}",  # hotwater_valve
            f"01{DEV_ROLE_MAP.HTG}",  # heating_valve
        ):
            cmd = Command.from_attrs(RQ, self.ctl.id, Code._000C, payload)
            self._add_discovery_cmd(cmd, 60 * 60 * 24, delay=0)

        cmd = Command.get_tpi_params(self.id)
        self._add_discovery_cmd(cmd, 60 * 60 * 6, delay=5)

    def _handle_msg(self, msg: Message) -> None:
        def eavesdrop_appliance_control(
            this: Message, *, prev: Message | None = None
        ) -> None:
            """Discover the heat relay (10: or 13:) for this system.

            There's' 3 ways to find a controller's heat relay (in order of reliability):
            1.  The 3220 RQ/RP *to/from a 10:* (1x/5min)
            2a. The 3EF0 RQ/RP *to/from a 10:* (1x/1min)
            2b. The 3EF0 RQ (no RP) *to a 13:* (3x/60min)
            3.  The 3B00 I/I exchange between a CTL & a 13: (TPI cycle rate, usu. 6x/hr)

            Data from the CTL is considered 'authoritative'. The 1FC9 RQ/RP exchange
            to/from a CTL is too rare to be useful.
            """

            # 18:14:14.025 066 RQ --- 01:078710 10:067219 --:------ 3220 005 0000050000
            # 18:14:14.446 065 RP --- 10:067219 01:078710 --:------ 3220 005 00C00500FF
            # 14:41:46.599 064 RQ --- 01:078710 10:067219 --:------ 3EF0 001 00
            # 14:41:46.631 063 RP --- 10:067219 01:078710 --:------ 3EF0 006 0000100000FF

            # 06:49:03.465 045 RQ --- 01:145038 13:237335 --:------ 3EF0 001 00
            # 06:49:05.467 045 RQ --- 01:145038 13:237335 --:------ 3EF0 001 00
            # 06:49:07.468 045 RQ --- 01:145038 13:237335 --:------ 3EF0 001 00
            # 09:03:59.693 051  I --- 13:237335 --:------ 13:237335 3B00 002 00C8
            # 09:04:02.667 045  I --- 01:145038 --:------ 01:145038 3B00 002 FCC8

            if this.code not in (Code._22D9, Code._3220, Code._3B00, Code._3EF0):
                return

            # note the order: most to least reliable
            app_cntrl = None

            if (
                this.code in (Code._22D9, Code._3220) and this.verb == RQ
            ):  # TODO: RPs too?
                # dst could be an Address...
                if this.src == self.ctl and isinstance(this.dst, OtbGateway):  # type: ignore[unreachable]
                    app_cntrl = this.dst  # type: ignore[unreachable]

            elif this.code == Code._3EF0 and this.verb == RQ:
                # dst could be an Address...
                if this.src == self.ctl and isinstance(
                    this.dst,  # type: ignore[unreachable]
                    BdrSwitch | OtbGateway,
                ):
                    app_cntrl = this.dst  # type: ignore[unreachable]

            elif this.code == Code._3B00 and this.verb == I_ and prev is not None:
                if this.src == self.ctl and isinstance(prev.src, BdrSwitch):  # type: ignore[unreachable]
                    if prev.code == this.code and prev.verb == this.verb:  # type: ignore[unreachable]
                        app_cntrl = prev.src

            if app_cntrl is not None:
                app_cntrl.set_parent(self, child_id=FC)  # type: ignore[unreachable]

        # # assert msg.src is self.ctl, f"msg inappropriately routed to {self}"

        super()._handle_msg(msg)

        if msg.code == Code._000C:
            if msg.payload[SZ_ZONE_TYPE] == DEV_ROLE_MAP.APP and msg.payload.get(
                SZ_DEVICES
            ):
                self._gwy.get_device(
                    msg.payload[SZ_DEVICES][0], parent=self, child_id=FC
                )  # sets self._app_cntrl
            return

        if msg.code == Code._3150:
            if msg.payload.get(SZ_DOMAIN_ID) == FC and msg.verb in (I_, RP):
                self._heat_demand = msg.payload

        if self._gwy.config.enable_eavesdrop and not self.appliance_control:
            eavesdrop_appliance_control(msg)

    @property
    def appliance_control(self) -> BdrSwitch | OtbGateway | None:
        """The TCS relay, aka 'appliance control' (BDR or OTB)."""
        if self._app_cntrl:
            return self._app_cntrl
        app_cntrl = [d for d in self.childs if d._child_id == FC]
        return app_cntrl[0] if len(app_cntrl) == 1 else None  # type: ignore[return-value]

    @property
    def tpi_params(self) -> PayDictT._1100 | None:  # 1100
        return self._msg_value(Code._1100)  # type: ignore[return-value]

    @property
    def heat_demand(self) -> float | None:  # 3150/FC
        return self._msg_value(Code._3150, domain_id=FC, key=SZ_HEAT_DEMAND)  # type: ignore[return-value]

    @property
    def is_calling_for_heat(self) -> NoReturn:
        raise NotImplementedError(
            f"{self}: is_calling_for_heat attr is deprecated, use bool(heat_demand)"
        )

    @property
    def schema(self) -> dict[str, Any]:
        """Return the system's schema."""

        schema: dict[str, Any] = {SZ_SYSTEM: {}}

        schema[SZ_SYSTEM][SZ_APPLIANCE_CONTROL] = (
            self.appliance_control.id if self.appliance_control else None
        )

        schema[SZ_ORPHANS] = sorted(
            [
                d.id
                for d in self.childs  # HACK: UFC
                if not d._child_id and d._is_present  # TODO: and d is not self.ctl
            ]  # and not isinstance(d, UfhController)
        )  # devices without a parent zone, NB: CTL can be a sensor for a zone

        return schema

    @property
    def _schema_min(self) -> dict[str, Any]:
        """Return the system's minimal-alised schema."""

        schema: dict[str, Any] = self.schema
        result: dict[str, Any] = {}

        try:
            if schema[SZ_SYSTEM][SZ_APPLIANCE_CONTROL][:2] == DEV_TYPE_MAP.OTB:  # DEX
                result[SZ_SYSTEM] = {
                    SZ_APPLIANCE_CONTROL: schema[SZ_SYSTEM][SZ_APPLIANCE_CONTROL]
                }
        except (IndexError, TypeError):
            result[SZ_SYSTEM] = {SZ_APPLIANCE_CONTROL: None}

        zones = {}
        for idx, zone in schema[SZ_ZONES].items():
            _zone = {}
            if zone[SZ_SENSOR] and zone[SZ_SENSOR][:2] == DEV_TYPE_MAP.CTL:  # DEX
                _zone = {SZ_SENSOR: zone[SZ_SENSOR]}
            if devices := [
                d for d in zone[SZ_ACTUATORS] if d[:2] == DEV_TYPE_MAP.TR0
            ]:  # DEX
                _zone.update({SZ_ACTUATORS: devices})
            if _zone:
                zones[idx] = _zone
        if zones:
            result[SZ_ZONES] = zones

        result |= {
            k: v
            for k, v in schema.items()
            if k in ("orphans",) and v  # add UFH?
        }

        return result  # TODO: check against vol schema

    @property
    def params(self) -> dict[str, Any]:
        """Return the system's configuration."""

        params: dict[str, Any] = {SZ_SYSTEM: {}}
        params[SZ_SYSTEM]["tpi_params"] = self._msg_value(Code._1100)
        return params

    @property
    def status(self) -> dict[str, Any]:
        """Return the system's current state."""

        status: dict[str, Any] = {SZ_SYSTEM: {}}
        status[SZ_SYSTEM]["heat_demand"] = self.heat_demand

        status[SZ_DEVICES] = {
            d.id: d.status for d in sorted(self.childs, key=lambda x: x.id)
        }

        return status


class MultiZone(SystemBase):  # 0005 (+/- 000C?)
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self.zones: list[Zone] = []
        self.zone_by_idx: dict[str, Zone] = {}  # should not include HW
        self._max_zones: int = getattr(
            self._gwy.config, SZ_MAX_ZONES, DEFAULT_MAX_ZONES
        )

        self._prev_30c9: Message | None = None  # used to eavesdrop zone sensors

    def _setup_discovery_cmds(self) -> None:
        super()._setup_discovery_cmds()

        for zone_type in list(ZON_ROLE_MAP.HEAT_ZONES) + [ZON_ROLE_MAP.SEN]:
            cmd = Command.from_attrs(RQ, self.id, Code._0005, f"00{zone_type}")
            self._add_discovery_cmd(cmd, 60 * 60 * 24, delay=0)

    def _handle_msg(self, msg: Message) -> None:
        """Process any relevant message.

        If `zone_idx` in payload, route any messages to the corresponding zone.
        """

        def eavesdrop_zones(this: Message, *, prev: Message | None = None) -> None:
            [
                self.get_htg_zone(v)
                for d in msg.payload
                for k, v in d.items()
                if k == SZ_ZONE_IDX
            ]

        def eavesdrop_zone_sensors(
            this: Message, *, prev: Message | None = None
        ) -> None:
            """Determine each zone's sensor by matching zone/sensor temperatures."""

            def _testable_zones(changed_zones: dict[str, float]) -> dict[float, str]:
                return {
                    t1: i1
                    for i1, t1 in changed_zones.items()
                    if self.zone_by_idx[i1].sensor is None
                    and t1 not in [t2 for i2, t2 in changed_zones.items() if i2 != i1]
                }

            self._prev_30c9, prev = this, self._prev_30c9
            if prev is None:
                return  # type: ignore[unreachable]

            # TODO: use msgz/I, not RP
            secs: int = self._msg_value(Code._1F09, key="remaining_seconds")  # type: ignore[assignment]
            if secs is None or this.dtm > prev.dtm + td(seconds=secs + 5):
                return  # can only compare against 30C9 pkt from the last cycle

            # _LOGGER.warning("System state (before): %s", self.schema)

            changed_zones: dict[str, float] = {
                z[SZ_ZONE_IDX]: z[SZ_TEMPERATURE]
                for z in this.payload
                if z not in prev.payload and z[SZ_TEMPERATURE] is not None
            }  # zones with changed temps
            if not changed_zones:
                return  # ctl's 30C9 says no zones have changed temps during this cycle

            testable_zones = _testable_zones(changed_zones)
            if not testable_zones:
                return  # no testable zones

            testable_sensors = {
                d.temperature: d
                for d in self._gwy.devices  # NOTE: *not* self.childs
                if isinstance(d, Temperature)  # d.addr.type in DEVICE_HAS_ZONE_SENSOR
                and d.ctl in (self.ctl, None)
                and d.temperature is not None
                and d._msgs[Code._30C9].dtm > prev.dtm  # changed during last cycle
            }
            if not testable_sensors:
                return  # no testable sensors

            matched_pairs = {
                sensor: zone_idx
                for temp_z, zone_idx in testable_zones.items()
                for temp_s, sensor in testable_sensors.items()
                if temp_z == temp_s
            }

            for sensor, zone_idx in matched_pairs.items():
                zone = self.zone_by_idx[zone_idx]
                self._gwy.get_device(sensor.id, parent=zone, is_sensor=True)

            # _LOGGER.warning("System state (after): %s", self.schema)

            # now see if we can allocate the controller as a sensor...
            if any(z for z in self.zones if z.sensor is self.ctl):
                return  # the controller is already a sensor

            remaining_zones = _testable_zones(changed_zones)
            if len(remaining_zones) != 1:
                return  # no testable zones

            temp, zone_idx = tuple(remaining_zones.items())[0]

            # can safely(?) assume this zone is using the CTL as a sensor...
            if not [s for s in testable_sensors if s == temp]:
                zone = self.zone_by_idx[zone_idx]
                self._gwy.get_device(self.ctl.id, parent=zone, is_sensor=True)

            # _LOGGER.warning("System state (finally): %s", self.schema)

        def handle_msg_by_zone_idx(zone_idx: str, msg: Message) -> None:
            if zone := self.zone_by_idx.get(zone_idx):
                zone._handle_msg(msg)
            # elif self._gwy.config.enable_eavesdrop:
            #     self.get_htg_zone(zone_idx)._handle_msg(msg)

        super()._handle_msg(msg)

        if msg.code not in (Code._0005, Code._000A, Code._2309, Code._30C9) and (
            SZ_ZONE_IDX not in msg.payload  # 0004,0008,0009,000C,0404,12B0,2349,3150
        ):
            return

        # TODO: a I/0005 may have changed: del or add zones
        if msg.code == Code._0005:
            if (zone_type := msg.payload[SZ_ZONE_TYPE]) in ZON_ROLE_MAP.HEAT_ZONES:
                [
                    self.get_htg_zone(
                        f"{idx:02X}", **{SZ_CLASS: ZON_ROLE_MAP[zone_type]}
                    )
                    for idx, flag in enumerate(msg.payload[SZ_ZONE_MASK])
                    if flag == 1
                ]
            elif zone_type in DEV_ROLE_MAP.HEAT_DEVICES:
                [
                    self.get_htg_zone(f"{idx:02X}", msg=msg)
                    for idx, flag in enumerate(msg.payload[SZ_ZONE_MASK])
                    if flag == 1
                ]
            return

        # TODO: a I/000C may have changed: del or add devices
        if msg.code == Code._000C:
            if msg.payload[SZ_ZONE_TYPE] not in DEV_ROLE_MAP.HEAT_DEVICES:
                return
            if msg.payload[SZ_DEVICES]:
                self.get_htg_zone(msg.payload[SZ_ZONE_IDX], msg=msg)
            elif zon := self.zone_by_idx.get(msg.payload[SZ_ZONE_IDX]):
                zon._handle_msg(msg)  # tell existing zone: no device
            return

        # the CTL knows, but does not announce temps for multiroom_mode zones
        if msg.code == Code._30C9 and msg._has_array:
            for z in self.zones:
                if z.idx not in (x[SZ_ZONE_IDX] for x in msg.payload):
                    z._get_temp()

        # If some zones still don't have a sensor, maybe eavesdrop?
        if self._gwy.config.enable_eavesdrop and (
            msg.code in (Code._000A, Code._2309, Code._30C9) and msg._has_array
        ):  # could do Code._000A, but only 1/hr
            eavesdrop_zones(msg)

        # Route all messages to their zones, incl. 000C, 0404, others
        if isinstance(msg.payload, dict):
            if zone_idx := msg.payload.get(SZ_ZONE_IDX):
                handle_msg_by_zone_idx(zone_idx, msg)
            # TODO: elif msg.payload.get(SZ_DOMAIN_ID) == FA:  # DHW

        elif isinstance(msg.payload, list) and len(msg.payload):
            # TODO: elif msg.payload.get(SZ_DOMAIN_ID) == FA:  # DHW
            if isinstance(msg.payload[0], dict):  # e.g. 1FC9 is a list of lists:
                for z_dict in msg.payload:
                    handle_msg_by_zone_idx(z_dict.get(SZ_ZONE_IDX), msg)

        # If some zones still don't have a sensor, maybe eavesdrop?
        if (  # TODO: edge case: 1 zone with CTL as SEN
            self._gwy.config.enable_eavesdrop
            and msg.code == Code._30C9
            and (msg._has_array or len(self.zones) == 1)
            and any(z for z in self.zones if not z.sensor)
        ):
            eavesdrop_zone_sensors(msg)

    # TODO: should be a private method
    def get_htg_zone(
        self, zone_idx: str, *, msg: Message | None = None, **schema: Any
    ) -> Zone:
        """Return a heating zone, create it if required.

        First, use the schema to create/update it, then pass it any msg to handle.

        Heating zones are uniquely identified by a tcs_id|zone_idx pair.
        If a zone is created, attach it to this TCS.
        """

        schema = shrink(SCH_TCS_ZONES_ZON(schema))

        zon: Zone = self.zone_by_idx.get(zone_idx)  # type: ignore[assignment]
        if zon is None:
            zon = zone_factory(self, zone_idx, msg=msg, **schema)  # type: ignore[unreachable]
            self.zone_by_idx[zon.idx] = zon
            self.zones.append(zon)

        elif schema:
            zon._update_schema(**schema)

        if msg:
            zon._handle_msg(msg)
        return zon

    @property
    def schema(self) -> dict[str, Any]:
        return {
            **super().schema,
            SZ_ZONES: {z.idx: z.schema for z in sorted(self.zones)},
        }

    @property
    def params(self) -> dict[str, Any]:
        return {
            **super().params,
            SZ_ZONES: {z.idx: z.params for z in sorted(self.zones)},
        }

    @property
    def status(self) -> dict[str, Any]:
        return {
            **super().status,
            SZ_ZONES: {z.idx: z.status for z in sorted(self.zones)},
        }


class ScheduleSync(SystemBase):  # 0006 (+/- 0404?)
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self._msg_0006: Message = None  # type: ignore[assignment]

        # used to stop concurrent get_schedules
        self.zone_lock = Lock()  # FIXME: threading lock, or asyncio lock?
        self.zone_lock_idx: str | None = None

    def _setup_discovery_cmds(self) -> None:
        super()._setup_discovery_cmds()

        cmd = Command.get_schedule_version(self.id)
        self._add_discovery_cmd(cmd, 60 * 5, delay=5)

    def _handle_msg(self, msg: Message) -> None:  # NOTE: active
        """Periodically retrieve the latest global change counter."""

        super()._handle_msg(msg)

        if msg.code == Code._0006:
            self._msg_0006 = msg

    async def _schedule_version(self, *, force_io: bool = False) -> tuple[int, bool]:
        """Return the global schedule version number, and an indication if I/O was done.

        If `force_io`, then RQ the latest change counter from the TCS rather than
        rely upon a recent (cached) value.

        Cached values are only used if less than 3 minutes old.
        """

        # RQ --- 30:185469 01:037519 --:------ 0006 001 00
        # RP --- 01:037519 30:185469 --:------ 0006 004 000500E6

        if (
            not force_io
            and self._msg_0006
            and self._msg_0006.dtm > dt.now() - td(minutes=3)
        ):
            return (
                self._msg_0006.payload[SZ_CHANGE_COUNTER],
                False,
            )  # global_ver, did_io

        cmd = Command.get_schedule_version(self.ctl.id)
        pkt = await self._gwy.async_send_cmd(
            cmd, wait_for_reply=True, priority=Priority.HIGH
        )
        if pkt:
            self._msg_0006 = Message(pkt)

        return self._msg_0006.payload[SZ_CHANGE_COUNTER], True  # global_ver, did_io

    def _refresh_schedules(self) -> None:
        zone: Zone

        for zone in getattr(self, SZ_ZONES, []):
            self._gwy._loop.create_task(zone.get_schedule(force_io=True))
        if isinstance(self, StoredHw) and self.dhw:
            self._gwy._loop.create_task(self.dhw.get_schedule(force_io=True))

    async def _obtain_lock(self, zone_idx: str) -> None:
        timeout_dtm = dt.now() + td(minutes=3)
        while dt.now() < timeout_dtm:
            self.zone_lock.acquire()
            if self.zone_lock_idx is None:
                self.zone_lock_idx = zone_idx
            self.zone_lock.release()

            if self.zone_lock_idx == zone_idx:
                break
            await asyncio.sleep(0.005)  # gives the other zone enough time

        else:
            raise TimeoutError(
                f"Unable to obtain lock for {zone_idx} (used by {self.zone_lock_idx})"
            )

    def _release_lock(self) -> None:
        self.zone_lock.acquire()
        self.zone_lock_idx = None
        self.zone_lock.release()

    @property
    def schedule_version(self) -> int | None:
        return self._msg_value(Code._0006, key=SZ_CHANGE_COUNTER)  # type: ignore[return-value]

    @property
    def status(self) -> dict[str, Any]:
        return {
            **super().status,
            "schedule_version": self.schedule_version,
        }


class Language(SystemBase):  # 0100
    def _setup_discovery_cmds(self) -> None:
        super()._setup_discovery_cmds()

        cmd = Command.get_system_language(self.id)
        self._add_discovery_cmd(cmd, 60 * 60 * 24, delay=60 * 15)

    @property
    def language(self) -> str | None:
        return self._msg_value(Code._0100, key=SZ_LANGUAGE)  # type: ignore[return-value]

    @property
    def params(self) -> dict[str, Any]:
        params = super().params
        params[SZ_SYSTEM][SZ_LANGUAGE] = self.language
        return params


class Logbook(SystemBase):  # 0418
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self._prev_event: Message = None  # type: ignore[assignment]
        self._this_event: Message = None  # type: ignore[assignment]

        self._prev_fault: Message = None  # type: ignore[assignment]
        self._this_fault: Message = None  # type: ignore[assignment]

        self._faultlog: FaultLog = FaultLog(self)

    def _setup_discovery_cmds(self) -> None:
        super()._setup_discovery_cmds()

        cmd = Command.get_system_log_entry(self.id, 0)
        self._add_discovery_cmd(cmd, 60 * 5, delay=5)
        # self._gwy.add_task(
        #     self._gwy._loop.create_task(self.get_faultlog())
        # )

    def _handle_msg(self, msg: Message) -> None:  # NOTE: active
        super()._handle_msg(msg)

        if msg.code == Code._0418:  # and msg.verb in (I_, RP):
            self._faultlog.handle_msg(msg)

    async def get_faultlog(
        self,
        /,
        *,
        start: int = 0,
        limit: int | None = None,
        force_refresh: bool = False,
    ) -> dict[FaultIdxT, FaultLogEntry] | None:
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
        """Return the most recently logged event (fault or restore), if any."""
        if not self._faultlog.latest_event:
            return None
        return str(self._faultlog.latest_event)

    @property
    def latest_fault(self) -> str | None:
        """Return the most recently logged fault, if any."""
        if not self._faultlog.latest_fault:
            return None
        return str(self._faultlog.latest_fault)

    @property
    def status(self) -> dict[str, Any]:
        return {
            **super().status,
            "active_faults": self.active_faults,
            "latest_event": self.latest_event,
            "latest_fault": self.latest_fault,
        }


class StoredHw(SystemBase):  # 10A0, 1260, 1F41
    MIN_SETPOINT = 30.0  # NOTE: these may be removed
    MAX_SETPOINT = 85.0
    DEFAULT_SETPOINT = 50.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._dhw: DhwZone = None  # type: ignore[assignment]

    def _setup_discovery_cmds(self) -> None:
        super()._setup_discovery_cmds()

        for payload in (
            f"00{DEV_ROLE_MAP.DHW}",  # dhw_sensor
            # f"00{DEV_ROLE_MAP.HTG}",  # hotwater_valve
            # f"01{DEV_ROLE_MAP.HTG}",  # heating_valve
        ):
            cmd = Command.from_attrs(RQ, self.id, Code._000C, payload)
            self._add_discovery_cmd(cmd, 60 * 60 * 24, delay=0)

    def _handle_msg(self, msg: Message) -> None:
        super()._handle_msg(msg)

        if (
            not isinstance(msg.payload, dict)
            or msg.payload.get(SZ_DHW_IDX) is None
            and msg.payload.get(SZ_DOMAIN_ID) not in (F9, FA)
            and msg.payload.get(SZ_ZONE_IDX) != "HW"
        ):  # Code._0008, Code._000C, Code._0404, Code._10A0, Code._1260, Code._1F41
            return

        # TODO: a I/0005 may have changed zones & may need a restart (del) or not (add)
        if (
            msg.code == Code._000C
            and msg.payload[SZ_ZONE_TYPE] in DEV_ROLE_MAP.DHW_DEVICES
        ):
            if msg.payload[SZ_DEVICES]:
                self.get_dhw_zone(msg=msg)  # create DHW zone if required
            elif self._dhw:
                self._dhw._handle_msg(msg)  # tell existing DHW zone: no device
            return

        # RQ --- 18:002563 01:078710 --:------ 10A0 001 00  # every 4h
        # RP --- 01:078710 18:002563 --:------ 10A0 006 00157C0003E8

        # Route all messages to their zones, incl. 000C, 0404, others
        self.get_dhw_zone(msg=msg)

    # TODO: should be a private method
    def get_dhw_zone(self, *, msg: Message | None = None, **schema: Any) -> DhwZone:
        """Return a DHW zone, create it if required.

        First, use the schema to create/update it, then pass it any msg to handle.

        DHW zones are uniquely identified by a controller ID.
        If a DHW zone is created, attach it to this TCS.
        """

        schema = shrink(SCH_TCS_DHW(schema))

        if not self._dhw:
            self._dhw = zone_factory(self, "HW", msg=msg, **schema)  # type: ignore[assignment]

        elif schema:
            self._dhw._update_schema(**schema)

        if msg:
            self._dhw._handle_msg(msg)
        return self._dhw

    @property
    def dhw(self) -> DhwZone | None:
        return self._dhw

    @property
    def dhw_sensor(self) -> Device | None:
        return self._dhw.sensor if self._dhw else None

    @property
    def hotwater_valve(self) -> Device | None:
        return self._dhw.hotwater_valve if self._dhw else None

    @property
    def heating_valve(self) -> Device | None:
        return self._dhw.heating_valve if self._dhw else None

    @property
    def schema(self) -> dict[str, Any]:
        return {
            **super().schema,
            SZ_DHW_SYSTEM: self._dhw.schema if self._dhw else {},
        }

    @property
    def params(self) -> dict[str, Any]:
        return {
            **super().params,
            SZ_DHW_SYSTEM: self._dhw.params if self._dhw else {},
        }

    @property
    def status(self) -> dict[str, Any]:
        return {
            **super().status,
            SZ_DHW_SYSTEM: self._dhw.status if self._dhw else {},
        }


class SysMode(SystemBase):  # 2E04
    def _setup_discovery_cmds(self) -> None:
        super()._setup_discovery_cmds()

        cmd = Command.get_system_mode(self.id)
        self._add_discovery_cmd(cmd, 60 * 5, delay=5)

    @property
    def system_mode(self) -> dict[str, Any] | None:  # 2E04
        return self._msg_value(Code._2E04)  # type: ignore[return-value]

    def set_mode(
        self, system_mode: int | str | None, *, until: dt | str | None = None
    ) -> asyncio.Task[Packet]:
        """
        Set a system mode for a specified duration, or indefinitely.

        :param system_mode: 2-digit item from SYS_MODE_MAP, positional
        :param until: optional: end of set period
        :return:
        """
        cmd = Command.set_system_mode(self.id, system_mode, until=until)
        return self._gwy.send_cmd(cmd, priority=Priority.HIGH, wait_for_reply=True)

    def set_auto(self) -> asyncio.Task[Packet]:
        """Revert system to Auto, set non-PermanentOverride zones to FollowSchedule."""
        return self.set_mode(SYS_MODE_MAP.AUTO)

    def reset_mode(self) -> asyncio.Task[Packet]:
        """Revert system to Auto, force *all* zones to FollowSchedule."""
        return self.set_mode(SYS_MODE_MAP.AUTO_WITH_RESET)

    @property
    def params(self) -> dict[str, Any]:
        params = super().params
        params[SZ_SYSTEM][SZ_SYSTEM_MODE] = self.system_mode
        return params


class Datetime(SystemBase):  # 313F
    def _setup_discovery_cmds(self) -> None:
        super()._setup_discovery_cmds()

        cmd = Command.get_system_time(self.id)
        self._add_discovery_cmd(cmd, 60 * 60, delay=0)

    def _handle_msg(self, msg: Message) -> None:
        super()._handle_msg(msg)

        # FIXME: refactoring protocol stack
        if msg.code == Code._313F and msg.verb in (I_, RP) and self._gwy._transport:
            diff = abs(dt.fromisoformat(msg.payload[SZ_DATETIME]) - self._gwy._dt_now())
            if diff > td(minutes=5):
                _LOGGER.warning(f"{msg!r} < excessive datetime difference: {diff}")

    async def get_datetime(self) -> dt | None:
        cmd = Command.get_system_time(self.id)
        pkt = await self._gwy.async_send_cmd(cmd, wait_for_reply=True)
        msg = Message._from_pkt(pkt)
        return dt.fromisoformat(msg.payload[SZ_DATETIME])

    async def set_datetime(self, dtm: dt) -> Packet:
        """Set the date and time of the system."""

        cmd = Command.set_system_time(self.id, dtm)
        return await self._gwy.async_send_cmd(cmd, priority=Priority.HIGH)


class UfHeating(SystemBase):
    def _ufh_ctls(self) -> list[UfhController]:
        return sorted([d for d in self.childs if isinstance(d, UfhController)])

    @property
    def schema(self) -> dict[str, Any]:
        return {
            **super().schema,
            SZ_UFH_SYSTEM: {d.id: d.schema for d in self._ufh_ctls()},
        }

    @property
    def params(self) -> dict[str, Any]:
        return {
            **super().params,
            SZ_UFH_SYSTEM: {d.id: d.params for d in self._ufh_ctls()},
        }

    @property
    def status(self) -> dict[str, Any]:
        return {
            **super().status,
            SZ_UFH_SYSTEM: {d.id: d.status for d in self._ufh_ctls()},
        }


class System(StoredHw, Datetime, Logbook, SystemBase):
    """The Temperature Control System class."""

    _SLUG: str = SYS_KLASS.SYS

    def __init__(self, ctl: Controller, **kwargs: Any) -> None:
        super().__init__(ctl, **kwargs)

        self._heat_demands: dict[str, Any] = {}
        self._relay_demands: dict[str, Any] = {}
        self._relay_failsafes: dict[str, Any] = {}

    def _update_schema(self, **schema: Any) -> None:
        """Update a CH/DHW system with new schema attrs.

        Raise an exception if the new schema is not a superset of the existing schema.
        """

        _schema: dict[str, Any]
        schema = shrink(SCH_TCS(schema))

        if schema.get(SZ_SYSTEM) and (
            dev_id := schema[SZ_SYSTEM].get(SZ_APPLIANCE_CONTROL)
        ):
            self._app_cntrl = self._gwy.get_device(dev_id, parent=self, child_id=FC)  # type: ignore[assignment]

        if _schema := (schema.get(SZ_DHW_SYSTEM)):  # type: ignore[assignment]
            self.get_dhw_zone(**_schema)  # self._dhw = ...

        if not isinstance(self, MultiZone):
            return

        if _schema := (schema.get(SZ_ZONES)):  # type: ignore[assignment]
            [self.get_htg_zone(idx, **s) for idx, s in _schema.items()]

    @classmethod
    def create_from_schema(cls, ctl: Controller, **schema: Any) -> System:
        """Create a CH/DHW system for a CTL and set its schema attrs.

        The appropriate System class should have been determined by a factory.
        Schema attrs include: class (klass) & others.
        """

        tcs = cls(ctl)
        tcs._update_schema(**schema)
        return tcs

    def _handle_msg(self, msg: Message) -> None:
        super()._handle_msg(msg)

        if not isinstance(msg.payload, dict):
            return

        if (idx := msg.payload.get(SZ_DOMAIN_ID)) and msg.verb in (I_, RP):
            idx = msg.payload[SZ_DOMAIN_ID]
            if msg.code == Code._0008:
                self._relay_demands[idx] = msg
            elif msg.code == Code._0009:
                self._relay_failsafes[idx] = msg
            elif msg.code == Code._3150:
                self._heat_demands[idx] = msg
            elif msg.code not in (
                Code._0001,
                Code._000C,
                Code._0404,
                Code._0418,
                Code._1100,
                Code._3B00,
            ):
                assert False, f"Unexpected code with a domain_id: {msg.code}"

    @property
    def heat_demands(self) -> dict[str, Any] | None:  # 3150
        # FC: 00-C8 (no F9, FA), TODO: deprecate as FC only?
        if not self._heat_demands:
            return None
        return {k: v.payload["heat_demand"] for k, v in self._heat_demands.items()}

    @property
    def relay_demands(self) -> dict[str, Any] | None:  # 0008
        # FC: 00-C8, F9: 00-C8, FA: 00 or C8 only (01: all 3, 02: FC/FA only)
        if not self._relay_demands:
            return None
        return {k: v.payload["relay_demand"] for k, v in self._relay_demands.items()}

    @property
    def relay_failsafes(self) -> dict[str, Any] | None:  # 0009
        if not self._relay_failsafes:
            return None
        return {}  # FIXME: failsafe_enabled

    @property
    def status(self) -> dict[str, Any]:
        """Return the system's current state."""

        status = super().status
        # assert SZ_SYSTEM in status  # TODO: removeme

        status[SZ_SYSTEM]["heat_demands"] = self.heat_demands
        status[SZ_SYSTEM]["relay_demands"] = self.relay_demands
        status[SZ_SYSTEM]["relay_failsafes"] = self.relay_failsafes

        return status


class Evohome(ScheduleSync, Language, SysMode, MultiZone, UfHeating, System):
    _SLUG: str = SYS_KLASS.TCS  # evohome

    # older evohome don't have zone_type=ELE


class Chronotherm(Evohome):
    _SLUG: str = SYS_KLASS.SYS


class Hometronics(System):
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

    RQ_SUPPORTED = (Code._0004, Code._000C, Code._2E04, Code._313F)  # TODO: WIP
    RQ_UNSUPPORTED = ("xxxx",)  # 10E0?


class Programmer(Evohome):
    _SLUG: str = SYS_KLASS.PRG


class Sundial(Evohome):
    _SLUG: str = SYS_KLASS.SYS


# e.g. {"evohome": Evohome}
SYS_CLASS_BY_SLUG: dict[str, type[System]] = class_by_attr(__name__, "_SLUG")


def system_factory(
    ctl: Controller, *, msg: Message | None = None, **schema: Any
) -> System:
    """Return the system class for a given controller/schema (defaults to evohome)."""

    def best_tcs_class(
        ctl_addr: Address,
        *,
        msg: Message | None = None,
        eavesdrop: bool = False,
        **schema: Any,
    ) -> type[System]:
        """Return the system class for a given CTL/schema (defaults to evohome)."""

        klass: str = schema.get(SZ_CLASS)  # type: ignore[assignment]

        # a specified system class always takes precedence (even if it is wrong)...
        if klass and (cls := SYS_CLASS_BY_SLUG.get(klass)):
            _LOGGER.debug(
                f"Using an explicitly-defined system class for: {ctl_addr} ({cls._SLUG})"
            )
            return cls

        # otherwise, use the default system class...
        _LOGGER.debug(f"Using a generic system class for: {ctl_addr} ({Device._SLUG})")
        return Evohome

    return best_tcs_class(
        ctl.addr,
        msg=msg,
        eavesdrop=ctl._gwy.config.enable_eavesdrop,
        **schema,
    ).create_from_schema(ctl, **schema)
