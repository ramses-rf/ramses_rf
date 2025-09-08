#!/usr/bin/env python3
"""RAMSES RF - Base class for all RAMSES-II objects: devices and constructs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Iterable
from datetime import datetime as dt, timedelta as td
from inspect import getmembers, isclass
from sys import modules
from types import ModuleType
from typing import TYPE_CHECKING, Any, Final

from ramses_rf.helpers import schedule_task
from ramses_tx import Priority, QosParams
from ramses_tx.address import ALL_DEVICE_ID
from ramses_tx.const import MsgId
from ramses_tx.opentherm import OPENTHERM_MESSAGES
from ramses_tx.ramses import CODES_SCHEMA

from . import exceptions as exc
from .const import (
    DEV_TYPE_MAP,
    SZ_ACTUATORS,
    SZ_DOMAIN_ID,
    SZ_NAME,
    SZ_SENSOR,
    SZ_ZONE_IDX,
)
from .schemas import SZ_CIRCUITS

from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
    VerbT,
)

from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    F9,
    FA,
    FC,
    FF,
)

if TYPE_CHECKING:
    from ramses_tx import Command, Message, Packet, VerbT
    from ramses_tx.frame import HeaderT
    from ramses_tx.opentherm import OtDataId
    from ramses_tx.schemas import DeviceIdT, DevIndexT

    from .device import (
        BdrSwitch,
        Controller,
        DhwSensor,
        OtbGateway,
        TrvActuator,
        UfhCircuit,
    )
    from .gateway import Gateway
    from .system import Evohome


_QOS_TX_LIMIT = 12  # TODO: needs work

_SZ_LAST_PKT: Final = "last_msg"
_SZ_NEXT_DUE: Final = "next_due"
_SZ_TIMEOUT: Final = "timeout"
_SZ_FAILURES: Final = "failures"
_SZ_INTERVAL: Final = "interval"
_SZ_COMMAND: Final = "command"

#
# NOTE: All debug flags should be False for deployment to end-users
_DBG_ENABLE_DISCOVERY_BACKOFF: Final[bool] = False

_LOGGER = logging.getLogger(__name__)


def class_by_attr(name: str, attr: str) -> dict[str, Any]:  # TODO: change to __module__
    """Return a mapping of a (unique) attr of classes in a module to that class."""

    def predicate(m: ModuleType) -> bool:
        return isclass(m) and m.__module__ == name and getattr(m, attr, None)

    return {getattr(c[1], attr): c[1] for c in getmembers(modules[name], predicate)}


class _Entity:
    """The ultimate base class for Devices/Zones/Systems.

    This class is mainly concerned with:
     - if the entity can Rx packets (e.g. can the HGI send it an RQ)
    """

    _SLUG: str = None  # type: ignore[assignment]

    def __init__(self, gwy: Gateway) -> None:
        self._gwy = gwy
        self.id: DeviceIdT = None  # type: ignore[assignment]

        self._qos_tx_count = 0  # the number of pkts Tx'd with no matching Rx

    def __repr__(self) -> str:
        return f"{self.id} ({self._SLUG})"

    # TODO: should be a private method
    def deprecate_device(self, pkt: Packet, reset: bool = False) -> None:
        """If an entity is deprecated enough times, stop sending to it."""

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

        raise NotImplementedError

        # super()._handle_msg(msg)  # store the message in the database

        # if self._gwy.hgi and msg.src.id != self._gwy.hgi.id:
        #     self.deprecate_device(msg._pkt, reset=True)

    # FIXME: this is a mess - to deprecate for async version?
    def _send_cmd(self, cmd: Command, **kwargs: Any) -> asyncio.Task | None:
        """Send a Command & return the corresponding Task."""

        # Don't poll this device if it is not responding
        if self._qos_tx_count > _QOS_TX_LIMIT:
            _LOGGER.info(f"{cmd} < Sending was deprecated for {self}")
            return None  # TODO: raise Exception (should be handled before now)

        if [  # TODO: remove this
            k for k in kwargs if k not in ("priority", "num_repeats")
        ]:  # FIXME: deprecate QoS in kwargs, should be qos=QosParams(...)
            raise RuntimeError("Deprecated kwargs: %s", kwargs)

        # cmd._source_entity = self  # TODO: is needed?
        # _msgs.pop(cmd.code, None)  # NOTE: Cause of DHW bug
        return self._gwy.send_cmd(cmd, wait_for_reply=False, **kwargs)

    # FIXME: this is a mess
    async def _async_send_cmd(
        self,
        cmd: Command,
        priority: Priority | None = None,
        qos: QosParams | None = None,  # FIXME: deprecate QoS in kwargs?
    ) -> Packet | None:
        """Send a Command & return the response Packet, or the echo Packet otherwise."""

        # Don't poll this device if it is not responding
        if self._qos_tx_count > _QOS_TX_LIMIT:
            _LOGGER.warning(f"{cmd} < Sending was deprecated for {self}")
            return None  # FIXME: raise Exception (should be handled before now)

        # cmd._source_entity = self  # TODO: is needed?
        return await self._gwy.async_send_cmd(
            cmd,
            max_retries=qos.max_retries if qos else None,
            priority=priority,
            timeout=qos.timeout if qos else None,
            wait_for_reply=qos.wait_for_reply if qos else None,
        )


class _MessageDB(_Entity):
    """Maintain/utilize an entity's state database."""

    _gwy: Gateway
    ctl: Controller
    tcs: Evohome

    # These attr used must be in this class, and the Entity base class
    _z_id: DeviceIdT
    _z_idx: DevIndexT | None  # e.g. 03, HW, is None for CTL, TCS

    def __init__(self, gwy: Gateway) -> None:
        super().__init__(gwy)

        self._msgs_: dict[Code, Message] = {}  # code, should be code/ctx? ?deprecate
        self._msgz_: dict[
            Code, dict[VerbT, dict[bool | str | None, Message]]
        ] = {}  # code/verb/ctx, should be code/ctx/verb?

    def _handle_msg(self, msg: Message) -> None:  # TODO: beware, this is a mess
        """Store a msg in the DBs."""

        if not (
            msg.src.id == self.id[:9]
            or (msg.dst.id == self.id[:9] and msg.verb != RQ)
            or (msg.dst.id == ALL_DEVICE_ID and msg.code == Code._1FC9)
        ):
            return  # ZZZ: don't store these

        if msg.verb in (I_, RP):
            self._msgs_[msg.code] = msg

        if msg.code not in self._msgz_:
            self._msgz_[msg.code] = {msg.verb: {msg._pkt._ctx: msg}}
        elif msg.verb not in self._msgz_[msg.code]:
            self._msgz_[msg.code][msg.verb] = {msg._pkt._ctx: msg}
        else:  # if not self._gwy._zzz:
            self._msgz_[msg.code][msg.verb][msg._pkt._ctx] = msg
        # elif (
        #     self._msgz[msg.code][msg.verb][msg._pkt._ctx] is not msg
        # ):  # MsgIdx ensures this
        #     assert False  # TODO: remove

    @property
    def _msg_db(self) -> list[Message]:  # flattened version of _msgz[code][verb][indx]
        """Return a flattened version of _msgz[code][verb][index].

        The idx is one of:
         - a simple index (e.g. zone_idx, domain_id, aka child_id)
         - a compound ctx (e.g. 0005/000C/0418)
         - True (an array of elements, each with its own idx),
         - False (no idx, is usu. 00),
         - None (not deteminable, rare)
        """
        return [m for c in self._msgz.values() for v in c.values() for m in v.values()]

    def _delete_msg(self, msg: Message) -> None:  # FIXME: this is a mess
        """Remove the msg from all state databases."""

        from .device import Device

        obj: _MessageDB

        if self._gwy._zzz:
            self._gwy._zzz.rem(msg)

        entities: list[_MessageDB] = []
        if isinstance(msg.src, Device):
            entities = [msg.src]
            if getattr(msg.src, "tcs", None):
                entities.append(msg.src.tcs)
                if msg.src.tcs.dhw:
                    entities.append(msg.src.tcs.dhw)
                entities.extend(msg.src.tcs.zones)

        # remove the msg from all the state DBs
        for obj in entities:
            if msg in obj._msgs_.values():
                del obj._msgs_[msg.code]
            with contextlib.suppress(KeyError):
                del obj._msgz_[msg.code][msg.verb][msg._pkt._ctx]

    def _get_msg_by_hdr(self, hdr: HeaderT) -> Message | None:
        """Return a msg, if any, that matches a header."""

        # if self._gwy._zzz:
        #     msgs = self._gwy._zzz.get(hdr=hdr)
        #     return msgs[0] if msgs else None

        msg: Message
        code: Code
        verb: VerbT

        # _ is device_id
        code, verb, _, *args = hdr.split("|")  # type: ignore[assignment]

        try:
            if args and (ctx := args[0]):  # ctx may == True
                msg = self._msgz[code][verb][ctx]
            elif False in self._msgz[code][verb]:
                msg = self._msgz[code][verb][False]
            elif None in self._msgz[code][verb]:
                msg = self._msgz[code][verb][None]
            else:
                return None
        except KeyError:
            return None

        if msg._pkt._hdr != hdr:
            raise LookupError

        return msg

    def _msg_flag(self, code: Code, key: str, idx: int) -> bool | None:
        if flags := self._msg_value(code, key=key):
            return bool(flags[idx])
        return None

    def _msg_value(
        self, code: Code | Iterable[Code], *args: Any, **kwargs: Any
    ) -> dict | list | None:
        if isinstance(code, str | tuple):  # a code or a tuple of codes
            return self._msg_value_code(code, *args, **kwargs)
        # raise RuntimeError
        assert isinstance(code, Message), (
            f"Invalid format: _msg_value({code})"
        )  # catch invalidly formatted code, only Message
        return self._msg_value_msg(code, *args, **kwargs)

    def _msg_value_code(
        self,
        code: Code,
        verb: VerbT | None = None,
        key: str | None = None,
        **kwargs: Any,
    ) -> dict | list | None:
        assert not isinstance(code, tuple) or verb is None, (
            f"Unsupported: using a tuple ({code}) with a verb ({verb})"
        )

        if verb:
            try:
                msgs = self._msgz[code][verb]
            except KeyError:
                msg = None
            else:
                msg = max(msgs.values()) if msgs else None
        elif isinstance(code, tuple):
            msgs = [m for m in self._msgs.values() if m.code in code]
            msg = (
                max(msgs) if msgs else None
            )  # return highest value found in code:value pairs
        else:
            msg = self._msgs.get(code)

        return self._msg_value_msg(msg, key=key, **kwargs)

    def _msg_value_msg(
        self,
        msg: Message | None,
        key: str | None = None,
        zone_idx: str | None = None,
        domain_id: str | None = None,
    ) -> dict | list | None:
        if msg is None:
            return None
        elif msg._expired:
            self._gwy._loop.call_soon(self._delete_msg, msg)  # HA bugs without defer

        if msg.code == Code._1FC9:  # NOTE: list of lists/tuples
            return [x[1] for x in msg.payload]

        idx: str | None = None
        val: str | None = None

        if domain_id:
            idx, val = SZ_DOMAIN_ID, domain_id
        elif zone_idx:
            idx, val = SZ_ZONE_IDX, zone_idx

        if isinstance(msg.payload, dict):
            msg_dict = msg.payload

        elif idx:
            msg_dict = {
                k: v for d in msg.payload for k, v in d.items() if d[idx] == val
            }
        else:
            # TODO: this isn't ideal: e.g. a controller is being treated like a 'stat
            # .I 101 --:------ --:------ 12:126457 2309 006 0107D0-0207D0  # is a CTL
            msg_dict = msg.payload[0]

        assert (not domain_id and not zone_idx) or (msg_dict.get(idx) == val), (
            f"{msg_dict} < Coding error: key={idx}, val={val}"
        )

        if key:
            return msg_dict.get(key)
        return {
            k: v
            for k, v in msg_dict.items()
            if k not in ("dhw_idx", SZ_DOMAIN_ID, SZ_ZONE_IDX) and k[:1] != "_"
        }

    @property
    def traits(self) -> dict:
        """Return the codes seen by the entity."""

        codes = {
            k: (CODES_SCHEMA[k][SZ_NAME] if k in CODES_SCHEMA else None)
            for k in sorted(self._msgs)
            if self._msgs[k].src == (self if hasattr(self, "addr") else self.ctl)
        }

        return {"_sent": list(codes.keys())}

    @property
    def _msgs(self) -> dict[Code, Message]:
        if not self._gwy._zzz:
            return self._msgs_

        sql = """
            SELECT dtm from messages WHERE verb in (' I', 'RP') AND (src = ? OR dst = ?)
        """
        return {  # ? use context instead?
            m.code: m for m in self._gwy._zzz.qry(sql, (self.id[:9], self.id[:9]))
        }  # e.g. 01:123456_HW

    @property
    def _msgz(self) -> dict[Code, dict[VerbT, dict[bool | str | None, Message]]]:
        if not self._gwy._zzz:
            return self._msgz_

        msgs_1: dict[Code, dict[VerbT, dict[bool | str | None, Message]]] = {}
        msg: Message

        for msg in self._msgs.values():
            if msg.code not in msgs_1:
                msgs_1[msg.code] = {msg.verb: {msg._pkt._ctx: msg}}
            elif msg.verb not in msgs_1[msg.code]:
                msgs_1[msg.code][msg.verb] = {msg._pkt._ctx: msg}
            else:
                msgs_1[msg.code][msg.verb][msg._pkt._ctx] = msg

        return msgs_1


class _Discovery(_MessageDB):
    MAX_CYCLE_SECS = 30
    MIN_CYCLE_SECS = 3

    def __init__(self, gwy: Gateway) -> None:
        super().__init__(gwy)

        self._discovery_cmds: dict[HeaderT, dict] = None  # type: ignore[assignment]
        self._discovery_poller: asyncio.Task | None = None

        self._supported_cmds: dict[str, bool | None] = {}
        self._supported_cmds_ctx: dict[str, bool | None] = {}

        if not gwy.config.disable_discovery:
            # self._start_discovery_poller()  # Can't use derived classes dont exist yet
            gwy._loop.call_soon(self._start_discovery_poller)

    @property  # TODO: needs tidy up
    def discovery_cmds(self) -> dict[HeaderT, dict]:
        """Return the pollable commands."""
        if self._discovery_cmds is None:
            self._discovery_cmds = {}
            self._setup_discovery_cmds()
        return self._discovery_cmds

    @property
    def supported_cmds(self) -> dict[Code, Any]:
        """Return the current list of pollable command codes."""
        return {
            code: (CODES_SCHEMA[code][SZ_NAME] if code in CODES_SCHEMA else None)
            for code in sorted(self._msgz)
            if self._msgz[code].get(RP) and self._is_not_deprecated_cmd(code)
        }

    @property
    def supported_cmds_ot(self) -> dict[MsgId, Any]:
        """Return the current list of pollable OT msg_ids."""

        def _to_data_id(msg_id: MsgId | str) -> OtDataId:
            return int(msg_id, 16)  # type: ignore[return-value]

        def _to_msg_id(data_id: OtDataId | int) -> MsgId:
            return f"{data_id:02X}"  # type: ignore[return-value]

        return {
            f"0x{msg_id}": OPENTHERM_MESSAGES[_to_data_id(msg_id)].get("en")  # type: ignore[misc]
            for msg_id in sorted(self._msgz[Code._3220].get(RP, []))  # type: ignore[type-var]
            if (
                self._is_not_deprecated_cmd(Code._3220, ctx=msg_id)
                and _to_data_id(msg_id) in OPENTHERM_MESSAGES
            )
        }

    def _is_not_deprecated_cmd(self, code: Code, ctx: str | None = None) -> bool:
        """Return True if the code|ctx pair is not deprecated."""

        if ctx is None:
            supported_cmds = self._supported_cmds
            idx = str(code)
        else:
            supported_cmds = self._supported_cmds_ctx
            idx = f"{code}|{ctx}"

        return supported_cmds.get(idx, None) is not False

    def _setup_discovery_cmds(self) -> None:
        raise NotImplementedError

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

        if cmd.rx_header is None:  # TODO: raise TypeError
            _LOGGER.warning(f"cmd({cmd}): invalid (null) header not added to discovery")
            return

        if cmd.rx_header in self.discovery_cmds:
            _LOGGER.info(f"cmd({cmd}): duplicate header not added to discovery")
            return

        if delay:
            delay += random.uniform(0.05, 0.45)

        self.discovery_cmds[cmd.rx_header] = {
            _SZ_COMMAND: cmd,
            _SZ_INTERVAL: td(seconds=max(interval, self.MAX_CYCLE_SECS)),
            _SZ_LAST_PKT: None,
            _SZ_NEXT_DUE: dt.now() + td(seconds=delay),
            _SZ_TIMEOUT: timeout,
            _SZ_FAILURES: 0,
        }

    def _start_discovery_poller(self) -> None:
        """Start the discovery poller (if it is not already running)."""

        if self._discovery_poller and not self._discovery_poller.done():
            return

        self._discovery_poller = schedule_task(self._poll_discovery_cmds)
        self._discovery_poller.set_name(f"{self.id}_discovery_poller")
        self._gwy.add_task(self._discovery_poller)

    async def _stop_discovery_poller(self) -> None:
        """Stop the discovery poller (only if it is running)."""
        if not self._discovery_poller or self._discovery_poller.done():
            return

        self._discovery_poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._discovery_poller

    async def _poll_discovery_cmds(self) -> None:
        """Send any outstanding commands that are past due.

        If a relevant message was received recently enough, reschedule the corresponding
        command for later.
        """

        while True:
            await self.discover()

            if self.discovery_cmds:
                next_due = min(t[_SZ_NEXT_DUE] for t in self.discovery_cmds.values())
                delay = max((next_due - dt.now()).total_seconds(), self.MIN_CYCLE_SECS)
            else:
                delay = self.MAX_CYCLE_SECS

            await asyncio.sleep(min(delay, self.MAX_CYCLE_SECS))

    async def discover(self) -> None:
        def find_latest_msg(hdr: HeaderT, task: dict) -> Message | None:
            """Return the latest message for a header from any source (not just RPs)."""
            msgs: list[Message] = [
                m
                for m in [self._get_msg_by_hdr(hdr[:5] + v + hdr[7:]) for v in (I_, RP)]
                if m is not None
            ]

            try:
                if task[_SZ_COMMAND].code in (Code._000A, Code._30C9):
                    msgs += [self.tcs._msgz[task[_SZ_COMMAND].code][I_][True]]
            except KeyError:
                pass

            return max(msgs) if msgs else None

        def backoff(hdr: HeaderT, failures: int) -> td:
            """Backoff the interval if there are/were any failures."""

            if not _DBG_ENABLE_DISCOVERY_BACKOFF:  # FIXME: data gaps
                return self.discovery_cmds[hdr][_SZ_INTERVAL]  # type: ignore[no-any-return]

            if failures > 5:
                secs = 60 * 60 * 6
                _LOGGER.error(
                    f"No response for {hdr} ({failures}/5): throttling to 1/6h"
                )
            elif failures > 2:
                _LOGGER.warning(
                    f"No response for {hdr} ({failures}/5): retrying in {self.MAX_CYCLE_SECS}s"
                )
                secs = self.MAX_CYCLE_SECS
            else:
                _LOGGER.info(
                    f"No response for {hdr} ({failures}/5): retrying in {self.MIN_CYCLE_SECS}s"
                )
                secs = self.MIN_CYCLE_SECS

            return td(seconds=secs)

        async def send_disc_cmd(
            hdr: HeaderT, task: dict, timeout: float = 15
        ) -> Packet | None:  # TODO: use constant instead of 15
            """Send a scheduled command and wait for/return the response."""

            try:
                pkt: Packet | None = await asyncio.wait_for(
                    self._gwy.async_send_cmd(task[_SZ_COMMAND]),
                    timeout=timeout,  # self.MAX_CYCLE_SECS?
                )

            # TODO: except: handle no QoS

            except exc.ProtocolError as err:  # InvalidStateError, SendTimeoutError
                _LOGGER.warning(f"{self}: Failed to send discovery cmd: {hdr}: {err}")

            except TimeoutError as err:  # safety valve timeout
                _LOGGER.warning(
                    f"{self}: Failed to send discovery cmd: {hdr} within {timeout} secs: {err}"
                )

            else:
                return pkt

            return None

        for hdr, task in self.discovery_cmds.items():
            dt_now = dt.now()

            if (msg := find_latest_msg(hdr, task)) and (
                task[_SZ_NEXT_DUE] < msg.dtm + task[_SZ_INTERVAL]
            ):  # if a newer message is available, take it
                task[_SZ_FAILURES] = 0  # only if task[_SZ_LAST_PKT].verb == RP?
                task[_SZ_LAST_PKT] = msg._pkt
                task[_SZ_NEXT_DUE] = msg.dtm + task[_SZ_INTERVAL]

            if task[_SZ_NEXT_DUE] > dt_now:
                continue  # if (most recent) last_msg is is not yet due...

            # since we may do I/O, check if the code|msg_id is deprecated
            task[_SZ_NEXT_DUE] = dt_now + task[_SZ_INTERVAL]  # might undeprecate later

            if not self._is_not_deprecated_cmd(task[_SZ_COMMAND].code):
                continue
            if not self._is_not_deprecated_cmd(
                task[_SZ_COMMAND].code, ctx=task[_SZ_COMMAND].payload[4:6]
            ):  # only for Code._3220
                continue

            # we'll have to do I/O...
            task[_SZ_NEXT_DUE] = dt_now + backoff(hdr, task[_SZ_FAILURES])  # JIC

            if pkt := await send_disc_cmd(hdr, task):  # TODO: OK 4 some exceptions
                task[_SZ_FAILURES] = 0  # only if task[_SZ_LAST_PKT].verb == RP?
                task[_SZ_LAST_PKT] = pkt
                task[_SZ_NEXT_DUE] = pkt.dtm + task[_SZ_INTERVAL]
            else:
                task[_SZ_FAILURES] += 1
                task[_SZ_LAST_PKT] = None
                task[_SZ_NEXT_DUE] = dt_now + backoff(hdr, task[_SZ_FAILURES])

    def _deprecate_code_ctx(
        self, pkt: Packet, ctx: str = None, reset: bool = False
    ) -> None:
        """If a code|ctx is deprecated twice, stop polling for it."""

        def deprecate(supported_dict: dict[str, bool | None], idx: str) -> None:
            if idx not in supported_dict:
                supported_dict[idx] = None
            elif supported_dict[idx] is None:
                _LOGGER.info(
                    f"{pkt} < Polling now deprecated for code|ctx={idx}: "
                    "it appears to be unsupported"
                )
                supported_dict[idx] = False

        def reinstate(supported_dict: dict[str, bool | None], idx: str) -> None:
            if self._is_not_deprecated_cmd(idx, None) is False:
                _LOGGER.info(
                    f"{pkt} < Polling now reinstated for code|ctx={idx}: "
                    "it now appears supported"
                )
            if idx in supported_dict:
                supported_dict.pop(idx)

        if ctx is None:
            supported_cmds = self._supported_cmds
            idx: str = pkt.code
        else:
            supported_cmds = self._supported_cmds_ctx
            idx = f"{pkt.code}|{ctx}"

        (reinstate if reset else deprecate)(supported_cmds, idx)


class Entity(_Discovery):
    """The base class for Devices/Zones/Systems."""


class Parent(Entity):  # A System, Zone, DhwZone or a UfhController
    """A Parent can be a System (TCS), a heating Zone, a DHW Zone, or a UfhController.

    For a System, children include the appliance controller, the children of all Zones
    (incl. the DHW Zone), and also any UFH controllers.

    For a heating Zone, children are limited to a sensor, and a number of actuators.
    For the DHW Zone, the children are limited to a sensor, a DHW valve, and/or a
    heating valve.

    There is a `set_parent` method, but no `set_child` method.
    """

    actuator_by_id: dict[DeviceIdT, BdrSwitch | UfhCircuit | TrvActuator]
    actuators: list[BdrSwitch | UfhCircuit | TrvActuator]

    circuit_by_id: dict[str, Any]

    _app_cntrl: BdrSwitch | OtbGateway | None
    _dhw_sensor: DhwSensor | None
    _dhw_valve: BdrSwitch | None
    _htg_valve: BdrSwitch | None

    def __init__(self, *args: Any, child_id: str = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self._child_id: str = child_id  # type: ignore[assignment]

        # self._sensor: Child = None
        self.child_by_id: dict[str, Child] = {}
        self.childs: list[Child] = []

    # def _handle_msg(self, msg: Message) -> None:
    #     def eavesdrop_ufh_circuits():
    #         if msg.code == Code._22C9:
    #             # .I --- 02:044446 --:------ 02:044446 22C9 024 00-076C0A28-01 01-06720A28-01 02-06A40A28-01 03-06A40A2-801  # NOTE: fragments
    #             # .I --- 02:044446 --:------ 02:044446 22C9 006 04-07D00A28-01                                               # [{'ufh_idx': '04',...
    #             circuit_idxs = [c[SZ_UFH_IDX] for c in msg.payload]

    #             for cct_idx in circuit_idxs:
    #                 self.get_circuit(cct_idx, msg=msg)

    #             # BUG: this will fail with > 4 circuits, as uses two pkts for this msg
    #             # if [c for c in self.child_by_id if c not in circuit_idxs]:
    #             #     raise CorruptStateError

    #     super()._handle_msg(msg)

    #     if self._gwy.config.enable_eavesdrop:
    #         eavesdrop_ufh_circuits()

    @property
    def zone_idx(self) -> str:
        """Return the domain id.

        For zones and circuits, the domain id is an idx, e.g.: '00', '01', '02'...
        For systems, it is 'FF', otherwise it is one of 'F9', 'FA' or 'FC'.
        """
        return self._child_id

    @zone_idx.setter  # TODO: should be a private setter
    def zone_idx(self, value: str) -> None:
        """Set the domain id, after validating it."""
        self._child_id = value

    def _add_child(
        self, child: Any, *, child_id: str = None, is_sensor: bool = None
    ) -> None:
        """Add a child device to this Parent, after validating the association.

        Also sets various other parent-specific object references (e.g. parent._sensor).

        This method should be invoked by the child's corresponding `set_parent` method.
        """

        # NOTE: here to prevent circular references
        from .device import (
            BdrSwitch,
            DhwSensor,
            OtbGateway,
            OutSensor,
            TrvActuator,
            UfhCircuit,
            UfhController,
        )
        from .system import DhwZone, System, Zone

        if hasattr(self, "childs") and child not in self.childs:  # Any parent
            assert isinstance(
                self, System | Zone | DhwZone | UfhController
            )  # TODO: remove me

        if is_sensor and child_id == FA:  # DHW zone (sensor)
            assert isinstance(self, DhwZone)  # TODO: remove me
            assert isinstance(child, DhwSensor)
            if self._dhw_sensor and self._dhw_sensor is not child:
                raise exc.SystemSchemaInconsistent(
                    f"{self} changed dhw_sensor (from {self._dhw_sensor} to {child})"
                )
            self._dhw_sensor = child

        elif is_sensor and hasattr(self, SZ_SENSOR):  # HTG zone
            assert isinstance(self, Zone)  # TODO: remove me
            if self.sensor and self.sensor is not child:
                raise exc.SystemSchemaInconsistent(
                    f"{self} changed zone sensor (from {self.sensor} to {child})"
                )
            self._sensor = child

        elif is_sensor:
            raise TypeError(
                f"not a valid combination for {self}: {child}|{child_id}|{is_sensor}"
            )

        elif hasattr(self, SZ_CIRCUITS):  # UFH circuit
            assert isinstance(self, UfhController)  # TODO: remove me
            if child not in self.circuit_by_id:
                self.circuit_by_id[child.id] = child

        elif hasattr(self, SZ_ACTUATORS):  # HTG zone
            assert isinstance(self, Zone)  # TODO: remove me
            assert isinstance(child, BdrSwitch | UfhCircuit | TrvActuator)
            if child not in self.actuators:
                self.actuators.append(child)
                self.actuator_by_id[child.id] = child  # type: ignore[assignment,index]

        elif child_id == F9:  # DHW zone (HTG valve)
            assert isinstance(self, DhwZone)  # TODO: remove me
            assert isinstance(child, BdrSwitch)
            if self._htg_valve and self._htg_valve is not child:
                raise exc.SystemSchemaInconsistent(
                    f"{self} changed htg_valve (from {self._htg_valve} to {child})"
                )
            self._htg_valve = child

        elif child_id == FA:  # DHW zone (DHW valve)
            assert isinstance(self, DhwZone)  # TODO: remove me
            assert isinstance(child, BdrSwitch)
            if self._dhw_valve and self._dhw_valve is not child:
                raise exc.SystemSchemaInconsistent(
                    f"{self} changed dhw_valve (from {self._dhw_valve} to {child})"
                )
            self._dhw_valve = child

        elif child_id == FC:  # Appliance Controller
            assert isinstance(self, System)  # TODO: remove me
            assert isinstance(child, BdrSwitch | OtbGateway)
            if self._app_cntrl and self._app_cntrl is not child:
                raise exc.SystemSchemaInconsistent(
                    f"{self} changed app_cntrl (from {self._app_cntrl} to {child})"
                )
            self._app_cntrl = child

        elif child_id == FF:  # System
            assert isinstance(self, System)  # TODO: remove me?
            assert isinstance(child, UfhController | OutSensor)
            pass

        else:
            raise TypeError(
                f"not a valid combination for {self}: {child}|{child_id}|{is_sensor}"
            )

        self.childs.append(child)
        self.child_by_id[child.id] = child


class Child(Entity):  # A Zone, Device or a UfhCircuit
    """A Device can be the Child of a Parent (a System, a heating Zone, or a DHW Zone).

    A Device may/may not have a Parent, but all devices will have the gateway as a
    parent, so that they can always be found via `gwy.child_by_id[device_id]`.

    In addition, the gateway has `system_by_id`, the Systems have `zone_by_id`, and the
    heating Zones have `actuator_by_id` dicts.

    There is a `set_parent` method, but no `set_child` method.
    """

    def __init__(
        self,
        *args: Any,
        parent: Parent = None,
        is_sensor: bool | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)

        self._parent = parent
        self._is_sensor = is_sensor

        self._child_id: str | None = None  # TODO: should be: str?

    def _handle_msg(self, msg: Message) -> None:
        from .device import Controller, Device, UfhController

        def eavesdrop_parent_zone() -> None:
            if isinstance(msg.src, UfhController):
                return

            if SZ_ZONE_IDX not in msg.payload:
                return

            if isinstance(self, Device):  # FIXME: a mess...
                # the following is a mess - may just be better off deprecating it
                if self.type in DEV_TYPE_MAP.HEAT_ZONE_ACTUATORS:
                    self.set_parent(msg.dst, child_id=msg.payload[SZ_ZONE_IDX])

                elif self.type in DEV_TYPE_MAP.THM_DEVICES:
                    self.set_parent(
                        msg.dst, child_id=msg.payload[SZ_ZONE_IDX], is_sensor=True
                    )

        super()._handle_msg(msg)

        if not self._gwy.config.enable_eavesdrop or (
            msg.src is msg.dst or not isinstance(msg.dst, Controller)  # UfhController))
        ):
            return

        if not self._parent or not self._child_id:
            eavesdrop_parent_zone()

    def _get_parent(
        self, parent: Parent, *, child_id: str = None, is_sensor: bool | None = None
    ) -> tuple[Parent, str | None]:
        """Get the device's parent, after validating it."""

        # NOTE: here to prevent circular references
        from .device import (
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
        from .system import DhwZone, Evohome, System, Zone

        if isinstance(self, UfhController):
            child_id = FF

        if isinstance(parent, Controller):  # A controller can't be a Parent
            parent = parent.tcs

        if isinstance(parent, Evohome) and child_id:
            if child_id in (F9, FA):
                parent = parent.get_dhw_zone()
            # elif child_id == FC:
            #     pass
            elif int(child_id, 16) < parent._max_zones:
                parent = parent.get_htg_zone(child_id)

        elif isinstance(parent, Zone) and not child_id:
            child_id = child_id or parent.idx

        # elif isinstance(parent, DhwZone) and child_id:
        #     child_id = child_id or parent.idx  # ?"HW"

        elif isinstance(parent, UfhController) and not child_id:
            raise TypeError(
                f"{self}: can't set child_id to: {child_id} "
                f"(for Circuits, it must be a circuit_idx)"
            )

        # if child_id is None:
        #     child_id = parent._child_id  # or, for zones: parent.idx

        if self._parent and self._parent != parent:
            raise exc.SystemSchemaInconsistent(
                f"{self} can't change parent "
                f"({self._parent}_{self._child_id} to {parent}_{child_id})"
            )

        # if self._child_id is not None and self._child_id != child_id:
        #     raise CorruptStateError(
        #         f"{self} can't set domain to: {child_id}, "
        #         f"({self._parent}_{self._child_id} to {parent}_{child_id})"
        #     )

        # if self._parent:
        #     if self._parent.ctl is not parent:
        #         raise CorruptStateError(f"parent mismatch: {self._parent.ctl} is not {parent}")
        #     if self._child_id and self._child_id != child_id:
        #         raise CorruptStateError(f"child_id mismatch: {self._child_id} != {child_id}")

        PARENT_RULES: dict[Any, dict] = {
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
                    f"{self}: can't set child_id to: {child_id} "
                    f"(it must match its parent's zone idx, {parent.idx})"
                )

        elif isinstance(parent, DhwZone):  # usu. FA (HW), could be F9
            if child_id not in (F9, FA):  # may not be known if eavesdrop'd
                raise TypeError(
                    f"{self}: can't set child_id to: {child_id} "
                    f"(for DHW, it must be F9 or FA)"
                )

        elif isinstance(parent, System):  # usu. FC
            if child_id not in (FC, FF):  # was: not in (F9, FA, FC, "HW"):
                raise TypeError(
                    f"{self}: can't set child_id to: {child_id} "
                    f"(for TCS, it must be FC)"
                )

        elif not isinstance(parent, UfhController):  # is like CTL/TCS combined
            raise TypeError(
                f"{self}: can't set Parent to: {parent} "
                f"(it must be System, DHW, Zone, or UfhController)"
            )

        return parent, child_id

    # TODO: should be a private method
    def set_parent(
        self, parent: Parent | None, *, child_id: str = None, is_sensor: bool = None
    ) -> Parent:
        """Set the device's parent, after validating it.

        This method will then invoke the parent's corresponding `set_child` method.

        Devices don't have parents, rather: parents have children; a mis-configured
        system could easily leave a device as a child of multiple parents (or bound
        to multiple controllers).

        It is assumed that a device is only bound to one controller, either a (Evohome)
        controller, or an UFH controller.
        """

        from .device import (  # NOTE: here to prevent circular references
            Controller,
            UfhController,
        )

        parent, child_id = self._get_parent(
            parent, child_id=child_id, is_sensor=is_sensor
        )
        ctl = parent if isinstance(parent, UfhController) else parent.ctl

        if self.ctl and self.ctl is not ctl:
            # NOTE: assume a device is bound to only one CTL (usu. best practice)
            raise exc.SystemSchemaInconsistent(
                f"{self} can't change controller: {self.ctl} to {ctl} "
                "(or perhaps the device has multiple controllers?"
            )

        parent._add_child(self, child_id=child_id, is_sensor=is_sensor)
        # parent.childs.append(self)
        # parent.child_by_id[self.id] = self

        self._child_id = child_id
        self._parent = parent

        assert isinstance(ctl, Controller)  # mypy hint

        self.ctl: Controller = ctl
        self.tcs: Evohome = ctl.tcs

        return parent
