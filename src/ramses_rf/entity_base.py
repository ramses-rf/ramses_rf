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
from ramses_tx import Address, Priority, QosParams
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
_ID_SLICE = 9
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
     - if the entity can Rx packets (e.g. can the HGI send it an RQ?)
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

    def _handle_msg(self, msg: Message) -> None:
        """Store a msg in the DBs."""

        raise NotImplementedError  # to be handled by implementing classes

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
    """Maintain/utilize an entity's state database.

        EntityBase msg_db query methods

        (ix = database.py.MessageIndex method)

    .. table:: Database Query Methods
       :widths: auto

       ====  ======================  ====================  ============  ==========  ==========
        e.   method name             args                  returns       uses        used by
       ====  ======================  ====================  ============  ==========  ==========
       e1    _get_msg_by_hdr         hdr                   Message       i3          discover
       e2    _msg_value              code(s), Msg, args    dict[k,v]     e3,e4
       e3    _msg_value_code         code, verb, key       dict[k,v]     e4,e5,e6    e6
       e4    _msg_value_msg          Msg, (code)           dict[k,v]                 e2,e3
       e5    _msg_qry_by_code_key    code, key, (verb=)                              e6,
       e6    _msg_value_qry_by_code  key   code, key       str/float     e3,e5
       e7    _msg_qry                sql                                             e8
       e8    _msg_count              sql                                 e7
       e9    supported_cmds                                list(Codes)   i7
       e10   _msgs()                                                     i5
       ====  ======================  ====================  ============  ==========  ==========

    """

    _gwy: Gateway
    ctl: Controller
    tcs: Evohome

    # These attr used must be in this class
    _z_id: DeviceIdT
    _z_idx: DevIndexT | None  # e.g. 03, HW. Is None for CTL, TCS.
    # idx is one of:
    # - a simple index (e.g. zone_idx, domain_id, aka child_id)
    # - a compound ctx (e.g. 0005/000C/0418)
    # - True (an array of elements, each with its own idx),
    # - False (no idx, is usu. 00),
    # - None (not determinable, rare)

    def __init__(self, gwy: Gateway) -> None:
        super().__init__(gwy)

        self._msgs_: dict[
            Code, Message
        ] = {}  # TODO(eb): deprecated, used in test, remove Q1 2026
        if not self._gwy.msg_db:  # TODO(eb): deprecated since 0.52.1, remove Q1 2026
            self._msgz_: dict[
                Code, dict[VerbT, dict[bool | str | None, Message]]
            ] = {}  # code/verb/ctx,

        # As of 0.52.1 we use SQLite MessageIndex, see ramses_rf/database.py
        # _msgz_ (nested) was only used in this module. Note:
        # _msgz (now rebuilt from _msgs) is also used in:
        #   - client.py: for code in device._msgz.values()
        #   - base.py: Code._1060 in self._msgz
        #   [x] device.heat (no longer used)

    def _handle_msg(self, msg: Message) -> None:
        """Store a msg in the DBs.
        Uses SQLite MessageIndex since 0.52.1
        """

        if not (
            msg.src.id == self.id[:_ID_SLICE]  # do store if dev is msg.src
            or (
                msg.dst.id == self.id[:_ID_SLICE] and msg.verb != RQ
            )  # skip RQs to self
            or (
                msg.dst.id == ALL_DEVICE_ID and msg.code == Code._1FC9
            )  # skip rf_bind rq
        ):
            return  # don't store the rest

        if self._gwy.msg_db:  # central SQLite MessageIndex
            _LOGGER.debug(
                "For %s (_z_id %s) add to msg_db: %s, src %s, dst %s",
                self.id,
                self._z_id,
                msg,
                msg.src,
                msg.dst,
            )
            debug_code: Code = Code._3150  # for debugging only log these, pick your own
            if msg.code == debug_code and msg.src.id.startswith("01:"):
                _LOGGER.debug(
                    "Added msg from %s with code %s to _gwy.msg_db. hdr=%s",
                    msg.src,
                    msg.code,
                    msg._pkt._hdr,
                )
                # print(self._gwy.get(src=str(msg.src[:9]), code=debug_code))  # < success!
                # Result in test log: lookup fails
                # msg.src = 01:073976 (CTL)
                # Added msg from 01:073976 (CTL) with code 0005 to _gwy.msg_db
                # query is for: 01:073976  < no suffix, extended lookup to [:12] chars
            self._gwy.msg_db.add(msg)

            # ignore any replaced message that might be returned
        else:  # TODO(eb): remove Q1 2026
            if msg.code not in self._msgz_:  # deprecated since 0.52.1
                # Store msg verb + ctx by code in nested self._msgz_ Dict
                self._msgz_[msg.code] = {msg.verb: {msg._pkt._ctx: msg}}
            elif msg.verb not in self._msgz_[msg.code]:
                # Same, 1 level deeper
                self._msgz_[msg.code][msg.verb] = {msg._pkt._ctx: msg}
            else:
                # Same, replacing previous message
                self._msgz_[msg.code][msg.verb][msg._pkt._ctx] = msg

        # Also store msg by code in flat self._msgs_ dict (stores the latest I/RP msgs by code)
        # TODO(eb): deprecated since 0.52.1, remove next block _msgs_ Q1 2026
        if msg.verb in (I_, RP):  # drop RQ's
            # if msg.code == Code._3150 and msg.src.id.startswith(
            #     "02:"
            # ):  # print for UFC only, 1 failing test
            #     print(
            #         f"Added msg with code {msg.code} to {self.id}._msgs_.  hdr={msg._pkt._hdr}"
            #     )
            self._msgs_[msg.code] = msg

    @property
    def _msg_list(self) -> list[Message]:
        """Return a flattened list of all messages logged on this device."""
        # (only) used in gateway.py#get_state() and in tests/tests/test_eavesdrop_schema.py
        # TODO remove _msg_list Q1 2026
        if self._gwy.msg_db:
            msg_list_qry: list[Message] = []
            code_list = self._msg_dev_qry()
            if code_list:
                for c in code_list:
                    if c in self._msgs:
                        # safeguard against lookup failures ("sim" packets?)
                        msg_list_qry.append(self._msgs[c])
                    else:
                        # evohome has these errors
                        # _msg_list could not fetch self._msgs[7FFF] for 18:072981 (_z_id 18:072981)
                        _LOGGER.debug(
                            "_msg_list could not fetch self._msgs[%s] for %s (_z_id %s)",
                            c,
                            self.id,
                            self._z_id,
                        )
            return msg_list_qry
        # else create from legacy nested dict
        return [
            msg
            for code in self._msgz.values()
            for ctx in code.values()
            for msg in ctx.values()
        ]

    def _add_record(
        self, id: DeviceIdT, code: Code | None = None, verb: str = " I"
    ) -> None:
        """Add a (dummy) record to the central SQLite MessageIndex."""
        # used by heat.py init
        if self._gwy.msg_db:
            self._gwy.msg_db.add_record(id, code=str(code), verb=verb)
        # else:
        #     _LOGGER.warning("Missing MessageIndex")
        # raise NotImplementedError

    def _delete_msg(self, msg: Message) -> None:  # FIXME: this is a mess
        """Remove the msg from all state databases. Used for expired msgs."""

        from .device import Device

        obj: _MessageDB

        # delete from the central SQLite MessageIndex
        if self._gwy.msg_db:
            self._gwy.msg_db.rem(msg)

        entities: list[_MessageDB] = []
        if isinstance(msg.src, Device):
            entities = [msg.src]
            if getattr(msg.src, "tcs", None):
                entities.append(msg.src.tcs)
                if msg.src.tcs.dhw:
                    entities.append(msg.src.tcs.dhw)
                entities.extend(msg.src.tcs.zones)

        # remove the msg from all the state DBs
        # TODO(eb): remove Q1 2026
        for obj in entities:
            if msg in obj._msgs_.values():
                del obj._msgs_[msg.code]
            if not self._gwy.msg_db:  # _msgz_ is deprecated, only used during migration
                with contextlib.suppress(KeyError):
                    del obj._msgz_[msg.code][msg.verb][msg._pkt._ctx]

    ### entity_base query methods

    def _get_msg_by_hdr(self, hdr: HeaderT) -> Message | None:
        """Return a msg, if any, that matches a given header."""

        if self._gwy.msg_db:
            # use central SQLite MessageIndex
            msgs = self._gwy.msg_db.get(hdr=hdr)
            # only 1 result expected since hdr is a unique key in _gwy.msg_db
            if msgs:
                if msgs[0]._pkt._hdr != hdr:
                    raise LookupError
                return msgs[0]
        else:
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
        return None

    def _msg_flag(self, code: Code, key: str, idx: int) -> bool | None:
        if flags := self._msg_value(code, key=key):
            return bool(flags[idx])
        return None

    def _msg_value(
        self, code: Code | Iterable[Code], *args: Any, **kwargs: Any
    ) -> dict | list | None:
        """
        Get the value for a Code from the database or from a Message object provided.

        :param code: filter messages by Code or a tuple of codes (optional)
        :param args: Message (optional)
        :param kwargs: zone to filter on (optional)
        :return: a dict containing key: value pairs, or a list of those
        """
        if isinstance(code, str | tuple):  # a code or a tuple of codes
            return self._msg_value_code(code, *args, **kwargs)

        assert isinstance(code, Message), (
            f"Invalid format: _msg_value({code})"
        )  # catch invalidly formatted code, only handle Message from here
        return self._msg_value_msg(code, *args, **kwargs)

    def _msg_value_code(
        self,
        code: Code,
        verb: VerbT | None = None,
        key: str | None = None,
        **kwargs: Any,
    ) -> dict | list | None:
        """
        Query the _msgz message dict or the SQLite MessageIndex for the most recent
        key: value pairs(s) for a given code.

        :param code: filter messages by Code or a tuple of Codes, optional
        :param verb: filter on I, RQ, RP, optional, only with a single Code
        :param key: value keyword to retrieve, not together with verb RQ
        :param kwargs: extra filter, e.g. zone_idx='01'
        :return: a dict containing key: value pairs, or a list of those
        """
        assert not isinstance(code, tuple) or verb is None, (
            f"Unsupported: using a tuple ({code}) with a verb ({verb})"
        )

        if verb:
            if verb == VerbT("RQ"):
                # must be a single code
                assert not isinstance(code, tuple) or verb is None, (
                    f"Unsupported: using a keyword ({key}) with verb RQ. Ignoring key"
                )
                key = None
            try:
                if self._gwy.msg_db:  # central SQLite MessageIndex, use verb= kwarg
                    code = Code(
                        self._msg_qry_by_code_key(code, key, **kwargs, verb=verb)
                    )
                    msg = self._msgs.get(code)
                else:  # deprecated lookup in nested _msgz
                    msgs = self._msgz[code][verb]
                    msg = max(msgs.values()) if msgs else None
            except KeyError:
                msg = None

        elif isinstance(code, tuple):
            msgs = [m for m in self._msgs.values() if m.code in code]
            msg = max(msgs) if msgs else None
            # return highest = latest? value found in code:value pairs
        else:  # single Code
            # for Zones, this doesn't work, returns first result = often wrong
            # TODO fix in _msg_qry_by_code_key()
            msg = self._msgs.get(code)

        return self._msg_value_msg(msg, key=key, **kwargs)

    def _msg_value_msg(
        self,
        msg: Message | None,
        key: str = "*",
        zone_idx: str | None = None,
        domain_id: str | None = None,
    ) -> dict | list | None:
        """
        Get from a Message all or a specific key with its value(s),
        optionally filtering for a zone or a domain

        :param msg: a Message to inspect
        :param key: the key to filter on
        :param zone_idx: the zone to filter on
        :param domain_id: the domain to filter on
        :return: a dict containing key: value pairs, or a list of those
        """
        if msg is None:
            return None
        elif msg._expired:
            self._gwy._loop.call_soon(self._delete_msg, msg)  # HA bugs without defer

        if msg.code == Code._1FC9:  # NOTE: list of lists/tuples
            return [x[1] for x in msg.payload]

        idx: str | None = None
        val: str | None = None  # holds the expected matching id value

        if domain_id:
            idx, val = SZ_DOMAIN_ID, domain_id
        elif zone_idx:
            idx, val = SZ_ZONE_IDX, zone_idx

        if isinstance(msg.payload, dict):
            msg_dict = msg.payload  # could be a mismatch on idx, accept
        elif idx:  # a list of dicts, e.g. SZ_DOMAIN_ID=FC
            msg_dict = {
                k: v for d in msg.payload for k, v in d.items() if d[idx] == val
            }
        else:  # a list without idx
            # TODO: this isn't ideal: e.g. a controller is being treated like a 'stat
            # .I 101 --:------ --:------ 12:126457 2309 006 0107D0-0207D0  # is a CTL
            msg_dict = msg.payload[0]  # we pick the first

        assert (
            (not domain_id and not zone_idx)
            or (msg_dict.get(idx) == val)
            or (idx == SZ_DOMAIN_ID)
        ), (
            f"full dict:{msg_dict}, payload:{msg.payload} < Coding error: key='{idx}', val='{val}'"
        )  # should not be there

        if (
            key == "*" or not key
        ):  # from a SQLite wildcard query, return first=only? k,v
            return {
                k: v
                for k, v in msg_dict.items()
                if k not in ("dhw_idx", SZ_DOMAIN_ID, SZ_ZONE_IDX) and k[:1] != "_"
            }
        return msg_dict.get(key)

    # SQLite methods, since 0.52.0

    def _msg_dev_qry(self) -> list[Code] | None:
        """
        Retrieve from the MessageIndex a list of Code keys involving this device.

        :return: list of Codes or an empty list when the query returned empty
        """

        if self._gwy.msg_db:
            # SQLite query on MessageIndex
            res: list[Code] = []

            if len(self.id) == 9:
                # fetch a ctl's message codes (add all its children?)
                sql = """
                    SELECT code from messages WHERE
                    verb in (' I', 'RP')
                    AND (src = ? OR dst = ?)
                    AND ctx LIKE ?
                """
                _ctx_qry = "%"

            elif self.id[_ID_SLICE:] == "_HW":
                # fetch a DHW entity's message codes
                sql = """
                    SELECT code from messages WHERE
                    verb in (' I', 'RP')
                    AND (src = ? OR dst = ?)
                    AND (ctx IN ('FC', 'FA', 'F9', 'FA') OR plk LIKE ?)
                """
                _ctx_qry = "%dhw_idx%"

            else:
                # fetch a zone's message codes
                sql = """
                    SELECT code from messages WHERE
                    verb in (' I', 'RP')
                    AND (src = ? OR dst = ?)
                    AND ctx LIKE ?
                """
                _ctx_qry = f"%{self.id[_ID_SLICE + 1 :]}%"

            for rec in self._gwy.msg_db.qry_field(
                sql, (self.id[:_ID_SLICE], self.id[:_ID_SLICE], _ctx_qry)
            ):
                _LOGGER.debug(
                    "Fetched from index: %s for %s (_z_id %s)",
                    rec[0],
                    self.id,
                    self._z_id,
                )
                # Example: "Fetched from index: code 1FD4 for 01:123456 (_z_id 01)"
                res.append(Code(str(rec[0])))
            return res
        else:
            _LOGGER.warning("Missing MessageIndex")
            raise NotImplementedError

    def _msg_qry_by_code_key(
        self,
        code: Code | tuple[Code] | None = None,
        key: str | None = None,
        **kwargs: Any,
    ) -> Code | None:
        """
        Retrieve from the MessageIndex the most current Code for a code(s) &
        keyword combination involving this device.

        :param code: (optional) a message Code to use, e.g. Code._31DA or a tuple of Codes
        :param key: (optional) message keyword to fetch, e.g. SZ_HUMIDITY
        :param kwargs: optional verb='vb' single verb
        :return: Code of most recent query result message or None when query returned empty
        """
        if self._gwy.msg_db:
            code_qry: str = "= "
            if code is None:
                code_qry = "LIKE '%'"  # wildcard
            elif isinstance(code, tuple):
                for cd in code:
                    code_qry += f"'{str(cd)}' OR code = '"
                code_qry = code_qry[:-13]  # trim last OR
            else:
                code_qry += str(code)
            if kwargs["verb"] and kwargs["verb"] in (" I", "RP"):
                vb = f"('{str(kwargs['verb'])}',)"
            else:
                vb = "(' I', 'RP',)"
            ctx_qry = "%"
            if kwargs["zone_idx"]:
                ctx_qry = f"%{kwargs['zone_idx']}%"
            elif kwargs["dhw_idx"]:  # DHW
                ctx_qry = f"%{kwargs['dhw_idx']}%"
            key_qry = "%" if key is None else f"%{key}%"

            # SQLite query on MessageIndex
            sql = """
                SELECT dtm, code from messages WHERE
                verb in ?
                AND (src = ? OR dst = ?)
                AND (code ?)
                AND (ctx LIKE ?)
                AND (plk LIKE ?)
            """
            latest: dt = dt(0, 0, 0)
            res = None

            for rec in self._gwy.msg_db.qry_field(
                sql,
                (
                    vb,
                    self.id[:_ID_SLICE],
                    self.id[:_ID_SLICE],
                    code_qry,
                    ctx_qry,
                    key_qry,
                ),
            ):
                _LOGGER.debug(
                    "_msg_qry_by_code_key fetched rec: %s, code: %s", rec, code_qry
                )
                assert isinstance(rec[0], dt)  # mypy hint
                if rec[0] > latest:  # dtm, only use most recent
                    res = Code(rec[1])
                    latest = rec[0]
            return res
        else:
            _LOGGER.warning("Missing MessageIndex")
            raise NotImplementedError

    def _msg_value_qry_by_code_key(
        self,
        code: Code | None = None,
        key: str | None = None,
        **kwargs: Any,
    ) -> str | float | None:
        """
        Retrieve from the _msgs dict the most current value of a specific code & keyword combination
        or the first key's value when no key is specified.

        :param code: (optional) a single message Code to use, e.g. 31DA
        :param key: (optional) message keyword to fetch the value for, e.g. SZ_HUMIDITY or * (wildcard)
        :param kwargs: not used as of 0.52.1
        :return: a single string or float value or None when qry returned empty
        """
        val_msg: dict | list | None = None
        val: object = None
        cd: Code | None = self._msg_qry_by_code_key(code, key)
        if cd is None or cd not in self._msgs:
            _LOGGER.warning("Code %s not in device %s's messages", cd, self.id)
        else:
            val_msg = self._msg_value_msg(
                self._msgs[cd],
                key=key,  # key can be wildcard *
            )
        if val_msg:
            val = val_msg[0]
            _LOGGER.debug("Extracted val %s for code %s, key %s", val, code, key)

        if isinstance(val, float):
            return float(val)
        else:
            return str(val)

    def _msg_qry(self, sql: str) -> list[dict]:
        """
        SQLite custom query for an entity's stored payloads using the full MessageIndex.
        See ramses_rf/database.py

        :param sql: custom SQLite query on MessageIndex. Can include multiple CODEs in SELECT.
        :return: list of payload dicts from the selected messages, or an empty list
        """

        res: list[dict] = []
        if sql and self._gwy.msg_db:
            # example query:
            # """SELECT code from messages WHERE verb in (' I', 'RP') AND (src = ? OR dst = ?)
            # AND (code = '31DA' OR ...) AND (plk LIKE '%{SZ_FAN_INFO}%' OR ...)""" = 2 params
            for rec in self._gwy.msg_db.qry_field(
                sql, (self.id[:_ID_SLICE], self.id[:_ID_SLICE])
            ):
                _pl = self._msgs[Code(rec[0])].payload
                # add payload dict to res(ults)
                res.append(_pl)  # only if newer, handled by MessageIndex
        return res

    def _msg_count(self, sql: str) -> int:
        """
        Get the number of messages in a query result.

        :param sql: custom SQLite query on MessageIndex.
        :return: amount of messages in entity's database, 0 for no results
        """
        return len(self._msg_qry(sql))

    @property
    def traits(self) -> dict[str, Any]:
        """Get the codes seen by the entity."""

        codes = {
            code: (CODES_SCHEMA[code][SZ_NAME] if code in CODES_SCHEMA else None)
            for code in sorted(self._msgs)
            if self._msgs[code].src == (self if hasattr(self, "addr") else self.ctl)
        }

        return {"_sent": list(codes.keys())}

    @property
    def _msgs(self) -> dict[Code, Message]:
        """
        Get a flat dict af all I/RP messages logged with this device as src or dst.

        :return: flat dict of messages by Code
        """
        if not self._gwy.msg_db:
            return self._msgs_
            # _LOGGER.warning("Missing MessageIndex")
            # raise NotImplementedError

        # if self.id[:3] == "18:":  # HGI, confirm this is correct, tests suggest so
        #     return {}

        # a routine to debug dict creation, see test_systems.py:
        # print(f"Create _msgs for {self.id}:")
        # results = self._gwy.msg_db._cu.execute("SELECT dtm, src, code from messages WHERE verb in (' I', 'RP') and code is '3150'")
        # for r in results:
        #     print(r)

        if len(self.id) == 9:
            # fetch a ctl's message dtms (add all its children?)
            sql = """
                SELECT dtm from messages WHERE
                verb in (' I', 'RP')
                AND (src = ? OR dst = ?)
                AND ctx LIKE ?
            """
            _ctx_qry = "%"

        elif self.id[_ID_SLICE:] == "_HW":
            # fetch a DHW entity's message dtms
            sql = """
                SELECT dtm from messages WHERE
                verb in (' I', 'RP')
                AND (src = ? OR dst = ?)
                AND (ctx IN ('FC', 'FA', 'F9', 'FA') OR plk LIKE ?)
            """
            _ctx_qry = "%dhw_idx%"
            # TODO add Children messages? self.ctl.dhw
        else:
            # fetch a zone's message dtms
            sql = """
                SELECT dtm from messages WHERE
                verb in (' I', 'RP')
                AND (src = ? OR dst = ?)
                AND ctx LIKE ?
            """
            _ctx_qry = f"%{self.id[_ID_SLICE + 1 :]}%"

        _msg_dict = {  # since 0.52.3 use ctx (context) instead of just the address
            m.code: m
            for m in self._gwy.msg_db.qry(
                sql, (self.id[:_ID_SLICE], self.id[:_ID_SLICE], _ctx_qry)
            )  # e.g. 01:123456_HW, 01:123456_02 (Zone)
        }
        # if CTL, remove 3150, 3220 heat_demand, both are only stored on children
        # HACK
        # if self.id[:3] == "01:" and self._SLUG == "CTL":
        # with next ON: 2 errors , both 1x UFC, 1x CTR
        # with next OFF: 4 errors, all CTR
        # if Code._3150 in _msg_dict:  # Note: CTL can send a 3150 (see heat_ufc_00)
        #     _msg_dict.pop(Code._3150)  # keep, prefer to have 2 extra instead of missing 1
        # if Code._3220 in _msg_dict:
        #     _msg_dict.pop(Code._3220)
        # _LOGGER.debug(f"Removed 3150/3220 from %s._msgs dict", self.id)
        return _msg_dict

    @property
    def _msgz(self) -> dict[Code, dict[VerbT, dict[bool | str | None, Message]]]:
        """
        Get a nested dict of all I/RP messages logged with this device as either src or dst.
        Based on SQL query on MessageIndex with device as src or dst.

        :return: dict of messages involving this device, nested by Code, Verb, Context
        """
        if not self._gwy.msg_db:
            return self._msgz_  # TODO(eb): remove and uncomment next Q1 2026
            # _LOGGER.warning("Missing MessageIndex")
            # raise NotImplementedError

        # build _msgz from MessageIndex/_msgs:
        msgs_1: dict[Code, dict[VerbT, dict[bool | str | None, Message]]] = {}
        msg: Message

        for msg in self._msgs.values():  # contains only verbs I, RP
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
            # self._start_discovery_poller()  # Can't use derived classes don't exist yet
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
        if self._gwy.msg_db:
            return {
                code: CODES_SCHEMA[code][SZ_NAME]
                for code in sorted(
                    self._gwy.msg_db.get_rp_codes(
                        (self.id[:_ID_SLICE], self.id[:_ID_SLICE])
                    )
                )
                if self._is_not_deprecated_cmd(code)
            }
        return {  # TODO(eb): deprecated since 0.52.1, remove Q1 2026
            code: (CODES_SCHEMA[code][SZ_NAME] if code in CODES_SCHEMA else None)
            for code in sorted(self._msgz)
            if self._msgz[code].get(RP) and self._is_not_deprecated_cmd(code)
        }

    @property
    def supported_cmds_ot(self) -> dict[MsgId, Any]:
        """Return the current list of pollable OT msg_ids."""

        def _to_data_id(msg_id: MsgId | str) -> OtDataId:
            return int(msg_id, 16)  # type: ignore[return-value]

        # def _to_msg_id(data_id: OtDataId | int) -> MsgId:  # not used
        #     return f"{data_id:02X}"  # type: ignore[return-value]

        res: list[str] = []
        if self._gwy.msg_db:
            # SQLite query for ctx field on MessageIndex
            sql = """
                SELECT ctx from messages WHERE
                verb = 'RP'
                AND code = '3220'
                AND (src = ? OR dst = ?)
            """
            for rec in self._gwy.msg_db.qry_field(
                sql, (self.id[:_ID_SLICE], self.id[:_ID_SLICE])
            ):
                _LOGGER.debug("Fetched OT ctx from index: %s", rec[0])
                res.append(rec[0])
        else:  # TODO(eb): remove next Q1 2026
            res_dict: dict[bool | str | None, Message] | list[Any] = self._msgz[
                Code._3220
            ].get(RP, [])
            assert isinstance(res_dict, dict)
            res = list(res_dict.keys())
            # raise NotImplementedError

        return {
            f"0x{msg_id}": OPENTHERM_MESSAGES[_to_data_id(msg_id)].get("en")  # type: ignore[misc]
            for msg_id in sorted(res)
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
            """
            :return: the latest message for a header from any source (not just RPs).
            """
            msgs: list[Message] = [
                m
                for m in [self._get_msg_by_hdr(hdr[:5] + v + hdr[7:]) for v in (I_, RP)]
                if m is not None
            ]

            try:
                if task[_SZ_COMMAND].code in (Code._000A, Code._30C9):
                    if self._gwy.msg_db:  # use bespoke MessageIndex qry
                        sql = """
                            SELECT dtm from messages WHERE
                            code = ?
                            AND verb in (' I', 'RP')
                            AND ctx = 'True'
                            AND (src = ? OR dst = ?)
                        """
                        res = self._gwy.msg_db.qry(
                            sql,
                            (
                                task[_SZ_COMMAND].code,
                                self.tcs.id[:_ID_SLICE],
                                self.tcs.id[:_ID_SLICE],
                            ),
                        )
                        if len(res) > 0:
                            msgs += res[0]  # expect 1 Message in returned tuple
                        else:
                            _LOGGER.debug(
                                f"No msg found for hdr {hdr}, task code {task[_SZ_COMMAND].code}"
                            )
                    else:  # TODO(eb) remove next Q1 2026
                        msgs += [self.tcs._msgz[task[_SZ_COMMAND].code][I_][True]]
                        # raise NotImplementedError
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
                continue  # if (most recent) last_msg is not yet due...

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

            if isinstance(self, Device):  # FIXME: a mess... see issue ramses_cc #249
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
