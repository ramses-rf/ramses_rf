#!/usr/bin/env python3
"""RAMSES RF - Message database and index."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import OrderedDict
from datetime import datetime as dt, timedelta as td
from typing import TYPE_CHECKING, Any, NewType

from ramses_tx import CODES_SCHEMA, Code, Message

if TYPE_CHECKING:
    DtmStrT = NewType("DtmStrT", str)
    MsgDdT = OrderedDict[DtmStrT, Message]

_LOGGER = logging.getLogger(__name__)


def _setup_db_adapters() -> None:
    """Set up the database adapters and converters."""

    def adapt_datetime_iso(val: dt) -> str:
        """Adapt datetime.datetime to timezone-naive ISO 8601 datetime to match _msgs dtm keys."""
        return val.isoformat(timespec="microseconds")

    sqlite3.register_adapter(dt, adapt_datetime_iso)

    def convert_datetime(val: bytes) -> dt:
        """Convert ISO 8601 datetime to datetime.datetime object to import dtm in msg_db."""
        return dt.fromisoformat(val.decode())

    sqlite3.register_converter("DTM", convert_datetime)


def payload_keys(parsed_payload: list[dict] | dict) -> str:  # type: ignore[type-arg]
    """
    Copy payload keys for fast query check.

    :param parsed_payload: pre-parsed message payload dict
    :return: string of payload keys, separated by the | char
    """
    _keys: str = "|"

    def append_keys(ppl: dict) -> str:  # type: ignore[type-arg]
        _ks: str = ""
        for k, v in ppl.items():
            if (
                k not in _ks and k not in _keys and v is not None
            ):  # ignore keys with None value
                _ks += k + "|"
        return _ks

    if isinstance(parsed_payload, list):
        for d in parsed_payload:
            _keys += append_keys(d)
    elif isinstance(parsed_payload, dict):
        _keys += append_keys(parsed_payload)
    return _keys


class MessageIndex:
    """A simple in-memory SQLite3 database for indexing RF messages.
    Index holds the latest message to & from all devices by header
    (example of a hdr: 000C|RP|01:223036|0208)."""

    _housekeeping_task: asyncio.Task[None]

    def __init__(self, maintain: bool = True) -> None:
        """Instantiate a message database/index."""

        self.maintain = maintain
        self._msgs: MsgDdT = OrderedDict()  # stores all messages for retrieval. Filled & cleaned up in housekeeping_loop.

        # Connect to a SQLite DB in memory
        self._cx = sqlite3.connect(
            ":memory:", detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
        )
        # detect_types should retain dt type on store/retrieve
        self._cu = self._cx.cursor()  # Create a cursor

        _setup_db_adapters()  # DTM adapter/converter
        self._setup_db_schema()

        if self.maintain:
            self._lock = asyncio.Lock()
            self._last_housekeeping: dt = None  # type: ignore[assignment]
            self._housekeeping_task = None  # type: ignore[assignment]

        self.start()

    def __repr__(self) -> str:
        return f"MessageIndex({len(self._msgs)} messages)"  # or msg_db.count()

    def start(self) -> None:
        """Start the housekeeper loop."""

        if self.maintain:
            if self._housekeeping_task and (not self._housekeeping_task.done()):
                return

            self._housekeeping_task = asyncio.create_task(
                self._housekeeping_loop(), name=f"{self.__class__.__name__}.housekeeper"
            )

    def stop(self) -> None:
        """Stop the housekeeper loop."""

        if self._housekeeping_task and (not self._housekeeping_task.done()):
            self._housekeeping_task.cancel()  # stop the housekeeper

        self._cx.commit()  # just in case
        # self._cx.close()  # may still need to do queries after engine has stopped?

    @property
    def msgs(self) -> MsgDdT:
        """Return the messages in the index in a threadsafe way."""
        return self._msgs

    def _setup_db_schema(self) -> None:
        """Set up the message database schema.

        messages TABLE Fields:

        - dtm  message timestamp
        - verb " I", "RQ" etc.
        - src  message origin address
        - dst  message destination address
        - code packet code aka command class e.g. _0005, _31DA
        - ctx  message context, created from payload as index + extra markers (Heat)
        - hdr  packet header e.g. 000C|RP|01:223036|0208 (see: src/ramses_tx/frame.py)
        - plk the keys stored in the parsed payload, separated by the | char
        """

        self._cu.execute(
            """
            CREATE TABLE messages (
                dtm    DTM      NOT NULL PRIMARY KEY,
                verb   TEXT(2)  NOT NULL,
                src    TEXT(12) NOT NULL,
                dst    TEXT(12) NOT NULL,
                code   TEXT(4)  NOT NULL,
                ctx    TEXT,
                hdr    TEXT     NOT NULL UNIQUE,
                plk    TEXT     NOT NULL
            )
            """
        )

        self._cu.execute("CREATE INDEX idx_verb ON messages (verb)")
        self._cu.execute("CREATE INDEX idx_src ON messages (src)")
        self._cu.execute("CREATE INDEX idx_dst ON messages (dst)")
        self._cu.execute("CREATE INDEX idx_code ON messages (code)")
        self._cu.execute("CREATE INDEX idx_ctx ON messages (ctx)")
        self._cu.execute("CREATE INDEX idx_hdr ON messages (hdr)")

        self._cx.commit()

    async def _housekeeping_loop(self) -> None:
        """Periodically remove stale messages from the index,
        unless self.maintain is False."""

        async def housekeeping(dt_now: dt, _cutoff: td = td(days=1)) -> None:
            """
            Deletes all messages older than a given delta from the dict using the MessageIndex.
            :param dt_now: current timestamp
            :param _cutoff: the oldest timestamp to retain, default is 24 hours ago
            """
            dtm = dt_now - _cutoff  # .isoformat(timespec="microseconds") < needed?

            self._cu.execute("SELECT dtm FROM messages WHERE dtm => ?", (dtm,))
            rows = self._cu.fetchall()  # fetch dtm of current messages to retain

            try:  # make this operation atomic, i.e. update self._msgs only on success
                await self._lock.acquire()
                self._cu.execute("DELETE FROM messages WHERE dtm < ?", (dtm,))
                msgs = OrderedDict({row[0]: self._msgs[row[0]] for row in rows})
                self._cx.commit()

            except sqlite3.Error:  # need to tighten?
                self._cx.rollback()
            else:
                self._msgs = msgs
            finally:
                self._lock.release()

        while True:
            self._last_housekeeping = dt.now()
            await asyncio.sleep(3600)
            _LOGGER.info("Starting next MessageIndex housekeeping")
            await housekeeping(self._last_housekeeping)

    def add(self, msg: Message) -> Message | None:
        """
        Add a single message to the MessageIndex.
        Logs a warning if there is a duplicate dtm.
        :returns: any message that was removed because it had the same header
        """
        # TODO: eventually, may be better to use SqlAlchemy

        dup: tuple[Message, ...] = tuple()  # avoid UnboundLocalError
        old: Message | None = None  # avoid UnboundLocalError

        try:  # TODO: remove this, or apply only when source is a real packet log?
            # await self._lock.acquire()
            dup = self._delete_from(  # HACK: because of contrived pkt logs
                dtm=msg.dtm  # stored as such with DTM formatter
            )
            old = self._insert_into(msg)  # will delete old msg by hdr (not dtm!)

        except (
            sqlite3.Error
        ):  # UNIQUE constraint failed: ? messages.dtm or .hdr (so: HACK)
            self._cx.rollback()

        else:
            # _msgs dict requires a timestamp reformat
            dtm: DtmStrT = msg.dtm.isoformat(timespec="microseconds")  # type: ignore[assignment]
            self._msgs[dtm] = msg

        finally:
            pass  # self._lock.release()

        if (
            dup and msg.src is not msg.dst and not msg.dst.id.startswith("18:")  # HGI
        ):  # when src==dst, expect to add duplicate, don't warn
            _LOGGER.debug(
                "Overwrote dtm (%s) for %s: %s (contrived log?)",
                msg.dtm,
                msg._pkt._hdr,
                dup[0]._pkt,
            )
        if old is not None:
            _LOGGER.debug("Old msg replaced: %s", old)

        return old

    def add_record(self, src: str, code: str = "", verb: str = "") -> None:
        """
        Add a single record to the MessageIndex with timestamp now() and no Message contents.
        """
        # Used by OtbGateway init, via entity_base.py
        dtm: DtmStrT = dt.strftime(dt.now(), "%Y-%m-%dT%H:%M:%S")  # type: ignore[assignment]
        hdr = f"{code}|{verb}|{src}|00"  # dummy record has no contents

        dup = self._delete_from(hdr=hdr)

        sql = """
            INSERT INTO messages (dtm, verb, src, dst, code, ctx, hdr, plk)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        try:
            self._cu.execute(
                sql,
                (
                    dtm,
                    verb,
                    src,
                    src,
                    code,
                    None,
                    hdr,
                    "|",
                ),
            )
        except sqlite3.Error:
            self._cx.rollback()

        if dup:  # expected when more than one heat system in schema
            _LOGGER.debug("Replaced record with same hdr: %s", hdr)

    def _insert_into(self, msg: Message) -> Message | None:
        """
        Insert a message into the index.
        :returns: any message replaced (by same hdr)
        """
        assert msg._pkt._hdr is not None, "Skipping: Packet has no hdr: {msg._pkt}"

        if msg._pkt._ctx is True:
            msg_pkt_ctx = "True"
        elif msg._pkt._ctx is False:
            msg_pkt_ctx = "False"
        else:
            msg_pkt_ctx = msg._pkt._ctx  # can be None

        _old_msgs = self._delete_from(hdr=msg._pkt._hdr)

        sql = """
            INSERT INTO messages (dtm, verb, src, dst, code, ctx, hdr, plk)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """

        self._cu.execute(
            sql,
            (
                msg.dtm,
                str(msg.verb),
                msg.src.id,
                msg.dst.id,
                str(msg.code),
                msg_pkt_ctx,
                msg._pkt._hdr,
                payload_keys(msg.payload),
            ),
        )
        _LOGGER.debug(f"Added {msg} to gwy.msg_db")

        return _old_msgs[0] if _old_msgs else None

    def rem(
        self, msg: Message | None = None, **kwargs: str | dt
    ) -> tuple[Message, ...] | None:
        """Remove a set of message(s) from the index.

        :returns: any messages that were removed.
        """
        # _LOGGER.debug(f"SQL REM msg={msg} bool{bool(msg)} kwargs={kwargs} bool(kwargs)")
        # SQL REM
        # msg=||  02:044328 | | I | heat_demand | FC || {'domain_id': 'FC', 'heat_demand': 0.74}
        # boolTrue
        # kwargs={}
        # bool(kwargs)

        if not bool(msg) ^ bool(kwargs):
            raise ValueError("Either a Message or kwargs should be provided, not both")
        if msg:
            kwargs["dtm"] = msg.dtm  # .isoformat(timespec="microseconds")

        msgs = None
        try:  # make this operation atomic, i.e. update self._msgs only on success
            # await self._lock.acquire()
            msgs = self._delete_from(**kwargs)

        except sqlite3.Error:  # need to tighten?
            self._cx.rollback()

        else:
            for msg in msgs:
                dtm: DtmStrT = msg.dtm.isoformat(timespec="microseconds")  # type: ignore[assignment]
                self._msgs.pop(dtm)

        finally:
            pass  # self._lock.release()

        return msgs

    def _delete_from(self, **kwargs: bool | dt | str) -> tuple[Message, ...]:
        """Remove message(s) from the index.
        :returns: any messages that were removed"""

        msgs = self._select_from(**kwargs)

        sql = "DELETE FROM messages WHERE "
        sql += " AND ".join(f"{k} = ?" for k in kwargs)

        self._cu.execute(sql, tuple(kwargs.values()))

        return msgs

    # MessageIndex msg_db query methods > copy to docs/source/ramses_rf.rst
    # (ex = entity_base.py query methods
    #
    # +----+--------------+-------------+------------+------+--------------------------+
    # | ix |method name   | args        | returns    | uses | used by                  |
    # +====+==============+=============+============+======+==========================+
    # | i1 | get          | Msg/kwargs  | tuple[Msg] | i3   |                          |
    # +----+--------------+-------------+------------+------+--------------------------+
    # | i2 | contains     | kwargs      | bool       | i4   |                          |
    # +----+--------------+-------------+------------+------+--------------------------+
    # | i3 | _select_from | kwargs      | tuple[Msg] | i4   |                          |
    # +----+--------------+-------------+------------+------+--------------------------+
    # | i4 | qry_dtms     | kwargs      | list(dtm)  |      |                          |
    # +----+--------------+-------------+------------+------+--------------------------+
    # | i5 | qry          | sql, kwargs | tuple[Msg] |      | _msgs()                  |
    # +----+--------------+-------------+------------+------+--------------------------+
    # | i6 | qry_field    | sql, kwargs | tuple[fld] |      | e4, e5                   |
    # +----+--------------+-------------+------------+------+--------------------------+
    # | i7 | get_rp_codes | src, dst    | list[Code] |      | Discovery#supported_cmds |
    # +----+--------------+-------------+------------+------+--------------------------+

    def get(
        self, msg: Message | None = None, **kwargs: bool | dt | str
    ) -> tuple[Message, ...]:
        """
        Public method to get a set of message(s) from the index.
        :param msg: Message to return, by dtm (expect a single result as dtm is unique key)
        :param kwargs: data table field names and criteria, e.g. (hdr=...)
        :return: tuple of matching Messages
        """

        if not (bool(msg) ^ bool(kwargs)):
            raise ValueError("Either a Message or kwargs should be provided, not both")

        if msg:
            kwargs["dtm"] = msg.dtm  # .isoformat(timespec="microseconds")

        return self._select_from(**kwargs)

    def contains(self, **kwargs: bool | dt | str) -> bool:
        """
        Check if the MessageIndex contains at least 1 record that matches the provided fields.
        :param kwargs: (exact) SQLite table field_name: required_value pairs
        :return: True if at least one message fitting the given conditions is present, False when qry returned empty
        """

        return len(self.qry_dtms(**kwargs)) > 0

    def _select_from(self, **kwargs: bool | dt | str) -> tuple[Message, ...]:
        """
        Select message(s) using the MessageIndex.
        :param kwargs: (exact) SQLite table field_name: required_value pairs
        :returns: a tuple of qualifying messages
        """

        return tuple(
            self._msgs[row[0].isoformat(timespec="microseconds")]
            for row in self.qry_dtms(**kwargs)
        )

    def qry_dtms(self, **kwargs: bool | dt | str) -> list[Any]:
        """
        Select from the ImageIndex a list of dtms that match the provided arguments.
        :param kwargs: data table field names and criteria
        :return: list of unformatted dtms that match, useful for msg lookup, or an empty list if 0 matches
        """
        # tweak kwargs as stored in SQLite, inverse from _insert_into():
        kw = {key: value for key, value in kwargs.items() if key != "ctx"}
        if "ctx" in kwargs:
            if isinstance(kwargs["ctx"], str):
                kw["ctx"] = kwargs["ctx"]
            elif kwargs["ctx"]:
                kw["ctx"] = "True"
            else:
                kw["ctx"] = "False"

        sql = "SELECT dtm FROM messages WHERE "
        sql += " AND ".join(f"{k} = ?" for k in kw)

        self._cu.execute(sql, tuple(kw.values()))
        return self._cu.fetchall()

    def qry(self, sql: str, parameters: tuple[str, ...]) -> tuple[Message, ...]:
        """
        Get a tuple of messages from _msgs using the index, given sql and parameters.
        :param sql: a bespoke SQL query SELECT string that should return dtm as first field
        :param parameters: tuple of kwargs with the selection filter
        :return: a tuple of qualifying messages
        """

        if "SELECT" not in sql:
            raise ValueError(f"{self}: Only SELECT queries are allowed")

        self._cu.execute(sql, parameters)

        lst: list[Message] = []
        # stamp = list(self._msgs)[0] if len(self._msgs) > 0 else "N/A"  # for debug
        for row in self._cu.fetchall():
            ts: DtmStrT = row[0].isoformat(
                timespec="microseconds"
            )  # must reformat from DTM
            # _LOGGER.debug(
            #     f"QRY Msg key raw: {row[0]} Reformatted: {ts} _msgs stamp format: {stamp}"
            # )
            # QRY Msg key raw: 2022-09-08 13:43:31.536862 Reformatted: 2022-09-08T13:43:31.536862
            # _msgs stamp format: 2022-09-08T13:40:52.447364
            if ts in self._msgs:
                lst.append(self._msgs[ts])
            else:  # happens in tests with artificial msg from heat
                _LOGGER.info("MessageIndex timestamp %s not in device messages", ts)
        return tuple(lst)

    def get_rp_codes(self, parameters: tuple[str, ...]) -> list[Code]:
        """
        Get a list of Codes from the index, given parameters.
        :param parameters: tuple of additional kwargs
        :return: list of Code: value pairs
        """

        def get_code(code: str) -> Code:
            for Cd in CODES_SCHEMA:
                if code == Cd:
                    return Cd
            raise LookupError(f"Failed to find matching code for {code}")

        sql = """
                SELECT code from messages WHERE verb is 'RP' AND (src = ? OR dst = ?)
            """
        if "SELECT" not in sql:
            raise ValueError(f"{self}: Only SELECT queries are allowed")

        self._cu.execute(sql, parameters)
        res = self._cu.fetchall()
        return [get_code(res[0]) for res[0] in self._cu.fetchall()]

    def qry_field(
        self, sql: str, parameters: tuple[str, ...]
    ) -> list[tuple[dt | str, str]]:
        """
        Get a list of fields from the index, given select sql and parameters.
        :param sql: a bespoke SQL query SELECT string
        :param parameters: tuple of additional kwargs
        :return: list of key: value pairs as defined in sql
        """

        if "SELECT" not in sql:
            raise ValueError(f"{self}: Only SELECT queries are allowed")

        self._cu.execute(sql, parameters)
        return self._cu.fetchall()

    def all(self, include_expired: bool = False) -> tuple[Message, ...]:
        """Get all messages from the index."""

        self._cu.execute("SELECT * FROM messages")

        lst: list[Message] = []
        # stamp = list(self._msgs)[0] if len(self._msgs) > 0 else "N/A"
        for row in self._cu.fetchall():
            ts: DtmStrT = row[0].isoformat(timespec="microseconds")
            # _LOGGER.debug(
            #     f"ALL Msg key raw: {row[0]} Reformatted: {ts} _msgs stamp format: {stamp}"
            # )
            # ALL Msg key raw: 2022-05-02 10:02:02.744905
            # Reformatted: 2022-05-02T10:02:02.744905
            # _msgs stamp format: 2022-05-02T10:02:02.744905
            if ts in self._msgs:
                # if include_expired or not self._msgs[ts].HAS_EXPIRED:  # not working
                lst.append(self._msgs[ts])
            else:  # happens in tests with dummy msg from heat init
                _LOGGER.info("MessageIndex ts %s not in device messages", ts)
        return tuple(lst)

    def clr(self) -> None:
        """Clear the message index (remove indexes of all messages)."""

        self._cu.execute("DELETE FROM messages")
        self._cx.commit()

        self._msgs.clear()

    # def _msgs(self, device_id: DeviceIdT) -> tuple[Message, ...]:
    #     msgs = [msg for msg in self._msgs.values() if msg.src.id == device_id]
    #     return msgs
