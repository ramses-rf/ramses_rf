#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Entity is the base of all RAMSES-II objects: devices and also system/zone constructs.
"""

import asyncio
import logging
from datetime import datetime as dt
from datetime import timedelta as td
from inspect import getmembers, isclass
from sys import modules
from typing import Any, Dict, List, Optional, Tuple

from .const import (
    SZ_ACTUATORS,
    SZ_DEVICE_ID,
    SZ_DOMAIN_ID,
    SZ_NAME,
    SZ_SENSOR,
    SZ_UFH_IDX,
    SZ_ZONE_IDX,
    Discover,
    __dev_mode__,
)
from .protocol import CorruptStateError, Message
from .protocol.ramses import CODES_SCHEMA
from .protocol.transport import PacketProtocolPort
from .schema import SZ_CIRCUITS

# skipcq: PY-W2000
from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    F9,
    FA,
    FC,
    FF,
)

# skipcq: PY-W2000
from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    _0001,
    _0002,
    _0004,
    _0005,
    _0006,
    _0008,
    _0009,
    _000A,
    _000C,
    _000E,
    _0016,
    _0100,
    _0150,
    _01D0,
    _01E9,
    _0404,
    _0418,
    _042F,
    _0B04,
    _1030,
    _1060,
    _1081,
    _1090,
    _1098,
    _10A0,
    _10B0,
    _10E0,
    _10E1,
    _1100,
    _11F0,
    _1260,
    _1280,
    _1290,
    _1298,
    _12A0,
    _12B0,
    _12C0,
    _12C8,
    _12F0,
    _1300,
    _1F09,
    _1F41,
    _1FC9,
    _1FCA,
    _1FD0,
    _1FD4,
    _2249,
    _22C9,
    _22D0,
    _22D9,
    _22F1,
    _22F3,
    _2309,
    _2349,
    _2389,
    _2400,
    _2401,
    _2410,
    _2420,
    _2D49,
    _2E04,
    _2E10,
    _30C9,
    _3110,
    _3120,
    _313F,
    _3150,
    _31D9,
    _31DA,
    _31E0,
    _3200,
    _3210,
    _3220,
    _3221,
    _3223,
    _3B00,
    _3EF0,
    _3EF1,
    _PUZZ,
)

_QOS_TX_LIMIT = 12  # TODO: needs work

DEV_MODE = __dev_mode__ and False

_LOGGER = logging.getLogger(__name__)
if DEV_MODE:
    _LOGGER.setLevel(logging.DEBUG)


def class_by_attr(name: str, attr: str) -> dict:  # TODO: change to __module__
    """Return a mapping of a (unique) attr of classes in a module to that class.

    For example:
      {DEV_TYPE.OTB: OtbGateway, DEV_TYPE.CTL: Controller}
      {ZON_ROLE.RAD: RadZone,    ZON_ROLE.UFH: UfhZone}
      {"evohome": Evohome}
    """

    return {
        getattr(c[1], attr): c[1]
        for c in getmembers(
            modules[name],
            lambda m: isclass(m) and m.__module__ == name and getattr(m, attr, None),
        )
    }


def discover_decorator(fnc):
    # NOTE: only need to Wrap top-level entities
    def wrapper(self, discover_flag=Discover.DEFAULT) -> None:

        if self._gwy.config.disable_discovery:
            return
        if not discover_flag:
            return
        return fnc(self, discover_flag=discover_flag)

    return wrapper


class MessageDB:
    """Maintain/utilize an entity's state database."""

    def __init__(self, gwy) -> None:
        self._msgs = {}  # code, should be code/ctx? ?deprecate
        self._msgz = {}  # code/verb/ctx, should be code/ctx/verb?

    def _handle_msg(self, msg: Message) -> None:  # TODO: beware, this is a mess
        """Store a msg in _msgs[code] (only latest I/RP) and _msgz[code][verb][ctx]."""

        if msg.verb in (I_, RP):
            self._msgs[msg.code] = msg

        if msg.code not in self._msgz:
            self._msgz[msg.code] = {msg.verb: {msg._pkt._ctx: msg}}
        elif msg.verb not in self._msgz[msg.code]:
            self._msgz[msg.code][msg.verb] = {msg._pkt._ctx: msg}
        else:
            self._msgz[msg.code][msg.verb][msg._pkt._ctx] = msg

    @property
    def _msg_db(self) -> list:  # a flattened version of _msgz[code][verb][indx]
        """Return a flattened version of _msgz[code][verb][index].

        The idx is one of:
         - a simple index (e.g. zone_idx, domain_id, aka child_id)
         - a compund ctx (e.g. 0005/000C/0418)
         - True (an array of elements, each with its own idx),
         - False (no idx, is usu. 00),
         - None (not deteminable, rare)
        """
        return [m for c in self._msgz.values() for v in c.values() for m in v.values()]

    def _get_msg_by_hdr(self, hdr) -> Optional[Message]:
        """Return a msg, if any, that matches a header."""

        code, verb, _, *args = hdr.split("|")

        try:
            if args and (ctx := args[0]):
                msg = self._msgz[code][verb][ctx]
            elif False in self._msgz[code][verb]:
                msg = self._msgz[code][verb][False]
            elif None in self._msgz[code][verb]:
                msg = self._msgz[code][verb][None]
        except KeyError:
            return None

        if msg._pkt._hdr != hdr:
            raise LookupError

        return msg

    def _msg_flag(self, code, key, idx) -> Optional[bool]:

        if flags := self._msg_value(code, key=key):
            return bool(flags[idx])

    def _msg_value(self, code, *args, **kwargs):

        if isinstance(code, (str, tuple)):  # a code or a tuple of codes
            return self._msg_value_code(code, *args, **kwargs)
        return self._msg_value_msg(code, *args, **kwargs)  # assume is a Message

    def _msg_value_code(self, code, verb=None, key=None, **kwargs) -> Optional[dict]:

        assert (
            not isinstance(code, tuple) or verb is None
        ), f"Unsupported: using a tuple ({code}) with a verb ({verb})"

        if verb:
            try:
                msgs = self._msgz[code][verb]
            except KeyError:
                msg = None
            else:
                msg = max(msgs.values()) if msgs else None
        elif isinstance(code, tuple):
            msgs = [m for m in self._msgs.values() if m.code in code]
            msg = max(msgs) if msgs else None
        else:
            msg = self._msgs.get(code)

        return self._msg_value_msg(msg, key=key, **kwargs)

    @staticmethod  # FIXME: messy (uses msg, others use code - move to Message?)
    def _msg_value_msg(msg, key=None, zone_idx=None, domain_id=None) -> Optional[dict]:

        if msg is None:
            return
        elif msg._expired:
            _delete_msg(msg)

        if msg.code == _1FC9:  # NOTE: list of lists/tuples
            return [x[1] for x in msg.payload]

        if domain_id:
            idx, val = SZ_DOMAIN_ID, domain_id
        elif zone_idx:
            idx, val = SZ_ZONE_IDX, zone_idx
        else:
            idx = val = None

        if isinstance(msg.payload, dict):
            msg_dict = msg.payload

        elif idx:
            msg_dict = {
                k: v for d in msg.payload for k, v in d.items() if d[idx] == val
            }
        else:
            # TODO: this isn't ideal: e.g. a controller is being treated like a 'stat
            #  I 101 --:------ --:------ 12:126457 2309 006 0107D0-0207D0  # is a CTL
            msg_dict = msg.payload[0]

        assert (
            not domain_id and not zone_idx or msg_dict.get(idx) == val
        ), f"{msg_dict} < Coding error: key={idx}, val={val}"

        if key:
            return msg_dict.get(key)
        return {
            k: v
            for k, v in msg_dict.items()
            if k not in ("dhw_idx", SZ_DOMAIN_ID, SZ_ZONE_IDX) and k[:1] != "_"
        }


class MessageDatabaseSql:
    """Maintain/utilize an entity's state database."""

    CREATE_TABLE = """
        CREATE TABLE msgs (
            hdr data_type PRIMARY KEY,
            ctx data_type DEFAULT "",
            dtm data_type NOT NULL UNIQUE,
            msg data_type NOT NULL,
        ) [WITHOUT ROWID];
    """

    def __init__(self, gwy) -> None:
        self._cur = gwy._cur  # gwy._db.cursor()

    def _handle_msg(self, msg: Message) -> None:  # TODO: beware, this is a mess
        pass

    @property
    def _msg_db(self) -> list:  # a flattened version of _msgz[code][verb][indx]
        pass

    def _get_msg_by_hdr(self, hdr) -> Optional[Message]:
        pass

    def _msg_flag(self, code, key, idx) -> Optional[bool]:
        pass

    def _msg_value(self, code, *args, **kwargs):
        pass

    def _msg_value_code(self, code, verb=None, key=None, **kwargs) -> Optional[dict]:
        pass

    @staticmethod  # FIXME: messy (uses msg, others use code - move to Message?)
    def _msg_value_msg(msg, key=None, zone_idx=None, domain_id=None) -> Optional[dict]:
        pass


class Discovery(MessageDB):
    def __init__(self, gwy, *args, **kwargs) -> None:
        super().__init__(gwy, *args, **kwargs)

        self._disc_tasks = {}
        self._disc_tasks_poller = None

        gwy._loop.call_soon_threadsafe(self._config_discovery)

        if not gwy.config.disable_discovery and isinstance(
            gwy.pkt_protocol, PacketProtocolPort
        ):  # TODO: here, or in get_xxx()?
            gwy._loop.call_soon_threadsafe(self._start_discovery)

    def _config_discovery(self) -> None:
        raise NotImplementedError

    def _start_discovery(self) -> None:
        if not self._disc_tasks_poller or self._disc_tasks_poller.done():
            self._disc_tasks_poller = self._gwy.add_task(self._poll_disc_tasks)

    def _stop_discovery(self) -> None:
        if self._disc_tasks_poller and not self._disc_tasks_poller.done():
            self._disc_tasks_poller.cancel()

    def _add_disc_task(self, cmd, interval, *, delay: float = 0, timeout: float = None):
        """Schedule a command to run periodically."""

        if cmd.rx_header in self._disc_tasks:
            raise ValueError

        timeout = timeout or (cmd.qos.retries + 1) * cmd.qos.timeout.total_seconds()

        self._disc_tasks[cmd.rx_header] = {
            "command": cmd,
            "interval": td(seconds=interval),
            "last_msg": None,
            "last_ran": None,
            "next_due": dt.now() + td(seconds=delay),
            "timeout": timeout,
        }

    async def _poll_disc_tasks(self) -> None:
        """Send any outstanding commands that are past due.

        If a relevant message was received recently enough, reschedule the corresponding
        command for later.
        """
        MAX_CYCLE_SECS = 10
        MIN_CYCLE_SECS = 3

        while True:
            if self._gwy.config.disable_discovery:
                await asyncio.sleep(MIN_CYCLE_SECS)
                continue

            for hdr, task in self._disc_tasks.items():
                msgs = [self._get_msg_by_hdr(I_ + hdr[2:]) for v in (I_, RP)]

                dt_now = dt.now()

                if (
                    (msgs := [m for m in msgs if m is not None])
                    and (msg := max(msgs))
                    and (task["next_due"] < msg.dtm + task["interval"])
                ):
                    task["last_msg"] = msg

                elif task["next_due"] <= dt_now and (
                    msg := await self._send_disc_task(task)
                ):
                    task["last_msg"] = msg

                else:
                    continue

                task["last_ran"] = task["last_msg"].dtm
                task["next_due"] = task["last_ran"] + task["interval"]

            if self._disc_tasks:
                seconds = (
                    min(t["next_due"] for t in self._disc_tasks.values()) - dt.now()
                ).total_seconds()
                await asyncio.sleep(min(max(seconds, MIN_CYCLE_SECS), MAX_CYCLE_SECS))
            else:
                await asyncio.sleep(MAX_CYCLE_SECS)

    async def _send_disc_task(self, task) -> Message:
        """Send a scheduled command and wait for/return the reponse."""
        try:
            return await asyncio.wait_for(
                self._gwy.async_send_cmd(task["command"]), timeout=task["timeout"] * 5
            )

        except asyncio.TimeoutError:
            return  # task["last_msg"] = None?

        except TypeError as exc:
            print(exc)

        # except Exception as exc:
        #     _LOGGER.exception(exc)
        #     raise exc
        pass


class Entity(Discovery):
    """The ultimate base class for Devices/Zones/Systems.

    This class is mainly concerned with:
     - if the entity can Rx packets (e.g. can the HGI send it an RQ)
    """

    _SLUG = None

    def __init__(self, gwy) -> None:
        super().__init__(gwy)

        self._gwy = gwy
        self.id = None

        self._qos_tx_count = 0  # the number of pkts Tx'd with no matching Rx

    def __repr__(self) -> str:
        return f"{self.id} ({self._SLUG})"

    def _qos_function(self, pkt, reset=False) -> None:
        if reset:
            self._qos_tx_count = 0
            return

        self._qos_tx_count += 1
        if self._qos_tx_count == _QOS_TX_LIMIT:
            _LOGGER.warning(
                f"{pkt} < Sending now deprecated for {self} "
                "(consider adjusting device_id filters)"
            )  # TODO: take whitelist into account

    def _handle_msg(self, msg: Message) -> None:  # TODO: beware, this is a mess
        """Store a msg in _msgs[code] (only latest I/RP) and _msgz[code][verb][ctx]."""

        super()._handle_msg(msg)  # store the message in the database

        if (
            self._gwy.pkt_protocol is None
            or msg.src.id != self._gwy.pkt_protocol._hgi80.get(SZ_DEVICE_ID)
        ):
            self._qos_function(msg._pkt, reset=True)

    def _make_cmd(self, code, dest_id, payload="00", verb=RQ, **kwargs) -> None:
        self._send_cmd(self._gwy.create_cmd(verb, dest_id, code, payload, **kwargs))

    def _send_cmd(self, cmd, **kwargs) -> None:
        if self._gwy.config.disable_sending:
            _LOGGER.info(f"{cmd} < Sending is disabled")
            return

        if self._qos_tx_count > _QOS_TX_LIMIT:
            _LOGGER.info(f"{cmd} < Sending is deprecated for {self}")
            return

        cmd._source_entity = self
        # self._msgs.pop(cmd.code, None)  # NOTE: Cause of DHW bug
        self._gwy.send_cmd(cmd)  # BUG, should be: await async_send_cmd()

    @property
    def traits(self) -> dict:
        """Return the codes seen by the entity."""

        codes = {
            k: (CODES_SCHEMA[k][SZ_NAME] if k in CODES_SCHEMA else None)
            for k in sorted(self._msgs)
            if self._msgs[k].src is (self if hasattr(self, "addr") else self.ctl)
        }

        return {"_sent": list(codes.keys())}


def _delete_msg(msg) -> None:
    """Remove the msg from all state databases."""

    entities = [msg.src]
    if getattr(msg.src, "tcs", None):
        entities.append(msg.src.tcs)
        if msg.src.tcs.dhw:
            entities.append(msg.src.tcs.dhw)
        entities.extend(msg.src.tcs.zones)

    # remove the msg from all the state DBs
    for obj in entities:
        if msg in obj._msgs.values():
            del obj._msgs[msg.code]
        try:
            del obj._msgz[msg.code][msg.verb][msg._pkt._ctx]
        except KeyError:
            pass


class Parent:  # A System, Zone, DhwZone or a UfhController
    """A Parent can be a System (TCS), a heating Zone, a DHW Zone, or a UfhController.

    For a System, children include the appliance controller, the children of all Zones
    (incl. the DHW Zone), and also any UFH controllers.

    For a heating Zone, children are limited to a sensor, and a number of actuators.
    For the DHW Zone, the children are limited to a sensor, a DHW valve, and/or a
    heating valve.

    There is a `set_parent` method, but no `set_child` method.
    """

    def __init__(self, *args, child_id: str = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self._child_id = child_id

        # self._sensor: Child = None
        self.child_by_id: Dict[str:Child] = {}
        self.childs: List[Child] = []

    def _handle_msg(self, msg: Message) -> None:
        def eavesdrop_ufh_circuits():
            if msg.code == _22C9:
                #  I --- 02:044446 --:------ 02:044446 22C9 024 00-076C0A28-01 01-06720A28-01 02-06A40A28-01 03-06A40A2-801  # NOTE: fragments
                #  I --- 02:044446 --:------ 02:044446 22C9 006 04-07D00A28-01                                               # [{'ufh_idx': '04',...
                circuit_idxs = [c[SZ_UFH_IDX] for c in msg.payload]

                for cct_idx in circuit_idxs:
                    self.get_circuit(cct_idx, msg=msg)

                # BUG: this will fail with > 4 circuits, as uses two pkts for this msg
                # if [c for c in self.child_by_id if c not in circuit_idxs]:
                #     raise CorruptStateError

        super()._handle_msg(msg)

        if not self._gwy.config.enable_eavesdrop:
            return

        # if True:
        eavesdrop_ufh_circuits()

    @property
    def zone_idx(self) -> str:
        """Return the domain id.

        For zones and circuits, the domain id is an idx, e.g.: '00', '01', '02'...
        For systems, it is 'FF', otherwise it is one of 'F9', 'FA' or 'FC'.
        """
        return self._child_id

    @zone_idx.setter
    def zone_idx(self, value) -> None:
        """Set the domain id, after validating it."""
        self._child_id = value

    def _set_child(
        self, child: Any, *, child_id: str = None, is_sensor: bool = None
    ) -> None:
        """Add/Set the parent's child device, after validating the association.

        Also sets various other parent-specific object references (e.g. parent._sensor).

        This method should be invoked by the child's corresponding `set_parent` method.
        """

        # NOTE: here to prevent circular references
        from .devices import (
            BdrSwitch,
            DhwSensor,
            OtbGateway,
            OutSensor,
            TrvActuator,
            UfhCircuit,
            UfhController,
        )
        from .systems import System
        from .zones import DhwZone, Zone

        if hasattr(self, "childs") and child not in self.childs:  # Any parent
            assert isinstance(
                self, (System, Zone, DhwZone, UfhController)
            )  # TODO: remove me

        if is_sensor and child_id == FA:  # DHW zone (sensor)
            assert isinstance(self, DhwZone)  # TODO: remove me
            assert isinstance(child, DhwSensor)
            if self._dhw_sensor and self._dhw_sensor is not child:
                raise CorruptStateError(
                    f"{self} changed dhw_sensor (from {self._dhw_sensor} to {child})"
                )
            self._dhw_sensor = child

        elif is_sensor and hasattr(self, SZ_SENSOR):  # HTG zone
            assert isinstance(self, Zone)  # TODO: remove me
            if self.sensor and self.sensor is not child:
                raise CorruptStateError(
                    f"{self} changed zone sensor (from {self.sensor} to {child})"
                )
            self._sensor = child

        elif is_sensor:
            raise TypeError(
                f"not a valid combination for {self}: "
                f"{child}|{child_id}|{is_sensor}"
            )

        elif hasattr(self, SZ_CIRCUITS):  # UFH circuit
            assert isinstance(self, UfhController)  # TODO: remove me
            if child not in self.circuits:
                self.circuits.append(child)
                self.circuit_by_id[child.id] = child

        elif hasattr(self, SZ_ACTUATORS):  # HTG zone
            assert isinstance(self, Zone)  # TODO: remove me
            assert isinstance(child, (BdrSwitch, UfhCircuit, TrvActuator)), (
                "what" if True else "why"
            )
            if child not in self.actuators:
                self.actuators.append(child)
                self.actuator_by_id[child.id] = child

        elif child_id == F9:  # DHW zone (HTG valve)
            assert isinstance(self, DhwZone)  # TODO: remove me
            assert isinstance(child, BdrSwitch)
            if self._htg_valve and self._htg_valve is not child:
                raise CorruptStateError(
                    f"{self} changed htg_valve (from {self._htg_valve} to {child})"
                )
            self._htg_valve = child

        elif child_id == FA:  # DHW zone (DHW valve)
            assert isinstance(self, DhwZone)  # TODO: remove me
            assert isinstance(child, BdrSwitch)
            if self._dhw_valve and self._dhw_valve is not child:
                raise CorruptStateError(
                    f"{self} changed dhw_valve (from {self._dhw_valve} to {child})"
                )
            self._dhw_valve = child

        elif child_id == FC:  # Appliance Controller
            assert isinstance(self, System)  # TODO: remove me
            assert isinstance(child, (BdrSwitch, OtbGateway))
            if self._app_cntrl and self._app_cntrl is not child:
                raise CorruptStateError(
                    f"{self} changed app_cntrl (from {self._app_cntrl} to {child})"
                )
            self._app_cntrl = child

        elif child_id == FF:  # System
            assert isinstance(self, System)  # TODO: remove me?
            assert isinstance(child, (UfhController, OutSensor))
            pass

        else:
            raise TypeError(
                f"not a valid combination for {self}: "
                f"{child}|{child_id}|{is_sensor}"
            )

        self.childs.append(child)
        self.child_by_id[child.id] = child

        if DEV_MODE:
            _LOGGER.warning(
                "parent.set_child(), Parent: %s_%s, %s: %s",
                self.id,
                child_id,
                "Sensor" if is_sensor else "Device",
                child,
            )


class Child:  # A Zone, Device or a UfhCircuit
    """A Device can be the Child of a Parent (a System, a heating Zone, or a DHW Zone).

    A Device may/may not have a Parent, but all devices will have the gateway as a
    parent, so that they can always be found via `gwy.child_by_id[device_id]`.

    In addition, the gateway has `system_by_id`, the Systems have `zone_by_id`, and the
    heating Zones have `actuator_by_id` dicts.

    There is a `set_parent` method, but no `set_child` method.
    """

    def __init__(
        self, *args, parent: Parent = None, is_sensor: bool = None, **kwargs
    ) -> None:
        super().__init__(*args, **kwargs)

        self._parent: Parent = parent

        self._child_id: str = None
        self._is_sensor: bool = is_sensor

    def _handle_msg(self, msg: Message) -> None:
        from .devices import Controller, UfhController

        def eavesdrop_parent_zone():
            if isinstance(msg.src, UfhController):
                return

            if SZ_ZONE_IDX not in msg.payload:
                return

            if msg.code in (_1060, _12B0, _2309, _3150):  # not 30C9
                self.set_parent(msg.dst, child_id=msg.payload[SZ_ZONE_IDX])

        super()._handle_msg(msg)

        if not self._gwy.config.enable_eavesdrop or (
            msg.src is msg.dst
            or not isinstance(msg.dst, (Controller,))  # UfhController))
        ):
            return

        if True or not self._parent or not self._child_id:  # BUG:
            eavesdrop_parent_zone()

    def _get_parent(
        self, parent: Parent, *, child_id: str = None, is_sensor: bool = None
    ) -> Tuple[Parent, str]:
        """Get the device's parent, after validating it."""

        # NOTE: here to prevent circular references
        from .devices import (
            BdrSwitch,
            Controller,
            DhwSensor,
            OtbGateway,
            OutSensor,
            Thermostat,
            TrvActuator,
            UfhCircuit,
            UfhController,
        )
        from .systems import System
        from .zones import DhwZone, Zone

        if isinstance(self, UfhController):
            child_id = FF

        if isinstance(parent, Controller):  # A controller cant be a Parent
            parent: System = parent.tcs

        if isinstance(parent, System) and child_id:
            if child_id in (F9, FA):
                parent: DhwZone = parent.get_dhw_zone()
            # elif child_id == FC:
            #     pass
            elif int(child_id, 16) < self._gwy.config.max_zones:
                parent: Zone = parent.get_htg_zone(child_id)

        elif isinstance(parent, Zone) and not child_id:
            child_id = child_id or parent.idx

        # elif isinstance(parent, DhwZone) and child_id:
        #     child_id = child_id or parent.idx  # ?"HW"

        elif isinstance(parent, UfhController) and not child_id:
            raise TypeError(
                f"{self}: cant set child_id to: {child_id} "
                f"(for Circuits, it must be a circuit_idx)"
            )

        # if child_id is None:
        #     child_id = parent._child_id  # or, for zones: parent.idx

        if self._parent and self._parent != parent:
            raise CorruptStateError(
                f"{self} cant change parent "
                f"({self._parent}_{self._child_id} to {parent}_{child_id})"
            )

        # if self._child_id is not None and self._child_id != child_id:
        #     raise CorruptStateError(
        #         f"{self} cant set domain to: {child_id}, "
        #         f"({self._parent}_{self._child_id} to {parent}_{child_id})"
        #     )

        # if self._parent:
        #     if self._parent.ctl is not parent:
        #         raise CorruptStateError(f"parent mismatch: {self._parent.ctl} is not {parent}")
        #     if self._child_id and self._child_id != child_id:
        #         raise CorruptStateError(f"child_id mismatch: {self._child_id} != {child_id}")

        PARENT_RULES = {
            DhwZone: {SZ_ACTUATORS: (BdrSwitch,), SZ_SENSOR: (DhwSensor,)},
            System: {
                SZ_ACTUATORS: (BdrSwitch, OtbGateway, UfhController),
                SZ_SENSOR: (OutSensor,),
            },
            UfhController: {SZ_ACTUATORS: (UfhCircuit,), SZ_SENSOR: ()},
            Zone: {
                SZ_ACTUATORS: (BdrSwitch, TrvActuator, UfhCircuit),
                SZ_SENSOR: (Controller, Thermostat, TrvActuator),
            },
        }

        for k, v in PARENT_RULES.items():
            if isinstance(parent, k):
                rules = v
                break
        else:
            raise TypeError(
                f"for Parent {parent}: not a valid parent "
                f"(it must be {tuple(PARENT_RULES.keys())})"
            )

        if is_sensor and not isinstance(self, rules[SZ_SENSOR]):
            raise TypeError(
                f"for Parent {parent}: Sensor {self} must be {rules[SZ_SENSOR]}"
            )
        if not is_sensor and not isinstance(self, rules[SZ_ACTUATORS]):
            raise TypeError(
                f"for Parent {parent}: Actuator {self} must be {rules[SZ_ACTUATORS]}"
            )

        if isinstance(parent, Zone):
            if child_id != parent.idx:
                raise TypeError(
                    f"{self}: cant set child_id to: {child_id} "
                    f"(it must match its parent's zone idx, {parent.idx})"
                )

        elif isinstance(parent, DhwZone):  # usu. FA (HW), could be F9
            if child_id not in (F9, FA):  # may not be known if eavesdrop'd
                raise TypeError(
                    f"{self}: cant set child_id to: {child_id} "
                    f"(for DHW, it must be F9 or FA)"
                )

        elif isinstance(parent, System):  # usu. FC
            if child_id not in (FC, FF):  # was: not in (F9, FA, FC, "HW"):
                raise TypeError(
                    f"{self}: cant set child_id to: {child_id} "
                    f"(for TCS, it must be FC)"
                )

        elif not isinstance(parent, UfhController):  # is like CTL/TCS combined
            raise TypeError(
                f"{self}: cant set Parent to: {parent} "
                f"(it must be System, DHW, Zone, or UfhController)"
            )

        return parent, child_id

    def set_parent(
        self, parent: Parent, *, child_id: str = None, is_sensor: bool = None
    ) -> Parent:
        """Set the device's parent, after validating it.

        This method will then invoke the parent's corresponding `set_child` method.

        Devices don't have parents, rather: parents have children; a mis-configured
        system could easily leave a device as a child of multiple parents (or bound
        to multiple controllers).

        It is assumed that a device is only bound to one controller, either a (Evohome)
        controller, or an UFH controller.
        """

        from .devices import UfhController  # NOTE: here to prevent circular references

        parent, child_id = self._get_parent(
            parent, child_id=child_id, is_sensor=is_sensor
        )

        ctl = parent if isinstance(parent, UfhController) else parent.ctl

        if self.ctl and self.ctl is not ctl:
            # NOTE: assume a device is bound to only one CTL (usu. best practice)
            raise CorruptStateError(
                f"{self} cant change controller: {self.ctl} to {ctl} "
                "(or perhaps the device has multiple controllers?"
            )

        parent._set_child(self, child_id=child_id, is_sensor=is_sensor)
        # parent.childs.append(self)
        # parent.child_by_id[self.id] = self

        self._child_id = child_id
        self._parent = parent

        self.ctl = ctl
        self.tcs = ctl.tcs

        if DEV_MODE:
            _LOGGER.warning(
                "child.set_parent(), Parent: %s_%s, %s: %s",
                parent.id,
                child_id,
                "Sensor" if is_sensor else "Device",
                self,
            )

        return parent
