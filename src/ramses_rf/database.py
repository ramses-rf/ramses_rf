#!/usr/bin/env python3
"""RAMSES RF - Message database and index."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import OrderedDict
from datetime import datetime as dt, timedelta as td
from typing import NewType, TypedDict

from ramses_tx import Message

DtmStrT = NewType("DtmStrT", str)
MsgDdT = OrderedDict[DtmStrT, Message]


class Params(TypedDict):
    dtm: dt | str | None
    verb: str | None
    src: str | None
    dst: str | None
    code: str | None
    ctx: str | None
    hdr: str | None


_LOGGER = logging.getLogger(__name__)


def _setup_db_adapters() -> None:
    """Set up the database adapters and converters."""

    def adapt_datetime_iso(val: dt) -> str:
        """Adapt datetime.datetime to timezone-naive ISO 8601 datetime."""
        return val.isoformat(timespec="microseconds")

    sqlite3.register_adapter(dt, adapt_datetime_iso)

    def convert_datetime(val: bytes) -> dt:
        """Convert ISO 8601 datetime to datetime.datetime object."""
        return dt.fromisoformat(val.decode())

    sqlite3.register_converter("dtm", convert_datetime)


class MessageIndex:
    """A simple in-memory SQLite3 database for indexing messages."""

    def __init__(self) -> None:
        """Instantiate a message database/index."""

        self._msgs: MsgDdT = OrderedDict()

        self._cx = sqlite3.connect(":memory:")  # Connect to a SQLite DB in memory
        self._cu = self._cx.cursor()  # Create a cursor

        _setup_db_adapters()  # dtm adapter/converter
        self._setup_db_schema()

        self._lock = asyncio.Lock()
        self._last_housekeeping: dt = None  # type: ignore[assignment]
        self._housekeeping_task: asyncio.Task[None] = None  # type: ignore[assignment]

        self.start()

    def __repr__(self) -> str:
        return f"MessageIndex({len(self._msgs)} messages)"

    def start(self) -> None:
        """Start the housekeeper loop."""

        if self._housekeeping_task and not self._housekeeping_task.done():
            return

        self._housekeeping_task = asyncio.create_task(
            self._housekeeping_loop(), name=f"{self.__class__.__name__}.housekeeper"
        )

    def stop(self) -> None:
        """Stop the housekeeper loop."""

        if self._housekeeping_task and not self._housekeeping_task.done():
            self._housekeeping_task.cancel()  # stop the housekeeper

        self._cx.commit()  # just in case
        # self._cx.close()  # may still need to do queries after engine has stopped?

    @property
    def msgs(self) -> MsgDdT:
        """Return the messages in the index in a threadsafe way."""
        return self._msgs

    def _setup_db_schema(self) -> None:
        """Set up the message database schema.

        Fields:

        - dtm  message timestamp
        - verb _I, RQ etc.
        - src  message origin address
        - dst  message destination address
        - code packet code aka command class e.g. _0005, _31DA
        - ctx  message context, created from payload as index + extra markers (Heat)
        - hdr  packet header e.g. 000C|RP|01:223036|0208 (see: src/ramses_tx/frame.py)
        """

        self._cu.execute(
            """
            CREATE TABLE messages (
                dtm    TEXT(26) NOT NULL PRIMARY KEY,
                verb   TEXT(2)  NOT NULL,
                src    TEXT(9)  NOT NULL,
                dst    TEXT(9)  NOT NULL,
                code   TEXT(4)  NOT NULL,
                ctx    TEXT     NOT NULL,
                hdr    TEXT     NOT NULL UNIQUE
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
        """Periodically remove stale messages from the index."""

        async def housekeeping(dt_now: dt, _cutoff: td = td(days=1)) -> None:
            dtm = (dt_now - _cutoff).isoformat(timespec="microseconds")

            self._cu.execute("SELECT dtm FROM messages WHERE dtm => ?", (dtm,))
            rows = self._cu.fetchall()

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
            await housekeeping(self._last_housekeeping)

    def add(self, msg: Message) -> Message | None:
        """Add a single message to the index.

        Returns any message that was removed because it had the same header.

        Throws a warning if there is a duplicate dtm.
        """  # TODO: eventually, may be better to use SqlAlchemy

        dup: tuple[Message, ...] = tuple()  # avoid UnboundLocalError
        old: Message | None = None  # avoid UnboundLocalError

        try:  # TODO: remove, or use only when source is a packet log?
            # await self._lock.acquire()
            dup = self._delete_from(  # HACK: because of contrived pkt logs
                dtm=msg.dtm.isoformat(timespec="microseconds")
            )
            old = self._insert_into(msg)  # will delete old msg by hdr

        except sqlite3.Error:  # UNIQUE constraint failed: ? messages.dtm (so: HACK)
            self._cx.rollback()

        else:
            dtm: DtmStrT = msg.dtm.isoformat(timespec="microseconds")  # type: ignore[assignment]
            self._msgs[dtm] = msg

        finally:
            pass  # self._lock.release()

        if dup:
            _LOGGER.warning(
                "Overwrote dtm for %s: %s (contrived log?)", msg._pkt._hdr, dup[0]._pkt
            )

        return old

    def _insert_into(self, msg: Message) -> Message | None:
        """Insert a message into the index (and return any message replaced by hdr)."""

        msgs = self._delete_from(hdr=msg._pkt._hdr)

        sql = """
            INSERT INTO messages (dtm, verb, src, dst, code, ctx, hdr)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """

        self._cu.execute(
            sql,
            (
                msg.dtm,
                msg.verb,
                msg.src.id,
                msg.dst.id,
                msg.code,
                msg._pkt._ctx,
                msg._pkt._hdr,
            ),
        )

        return msgs[0] if msgs else None

    def rem(
        self, msg: Message | None = None, **kwargs: str
    ) -> tuple[Message, ...] | None:
        """Remove a set of message(s) from the index.

        Returns any messages that were removed.
        """

        if bool(msg) ^ bool(kwargs):
            raise ValueError("Either a Message or kwargs should be provided, not both")
        if msg:
            kwargs["dtm"] = msg.dtm.isoformat(timespec="microseconds")

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

    def _delete_from(self, **kwargs: str) -> tuple[Message, ...]:
        """Remove message(s) from the index (and return any messages removed)."""

        msgs = self._select_from(**kwargs)

        sql = "DELETE FROM messages WHERE "
        sql += " AND ".join(f"{k} = ?" for k in kwargs)

        self._cu.execute(sql, tuple(kwargs.values()))

        return msgs

    def get(self, msg: Message | None = None, **kwargs: str) -> tuple[Message, ...]:
        """Return a set of message(s) from the index."""

        if not (bool(msg) ^ bool(kwargs)):
            raise ValueError("Either a Message or kwargs should be provided, not both")
        if msg:
            kwargs["dtm"] = msg.dtm.isoformat(timespec="microseconds")

        return self._select_from(**kwargs)

    def _select_from(self, **kwargs: str) -> tuple[Message, ...]:
        """Select message(s) from the index (and return any such messages)."""

        sql = "SELECT dtm FROM messages WHERE "
        sql += " AND ".join(f"{k} = ?" for k in kwargs)

        self._cu.execute(sql, tuple(kwargs.values()))

        return tuple(self._msgs[row[0]] for row in self._cu.fetchall())

    def qry(self, sql: str, parameters: tuple[str, ...]) -> tuple[Message, ...]:
        """Return a set of message(s) from the index, given sql and parameters."""

        if "SELECT" not in sql:
            raise ValueError(f"{self}: Only SELECT queries are allowed")

        self._cu.execute(sql, parameters)

        return tuple(self._msgs[row[0]] for row in self._cu.fetchall())

    def all(self, include_expired: bool = False) -> tuple[Message, ...]:
        """Return all messages from the index."""

        self._cu.execute("SELECT * FROM messages")
        return tuple(self._msgs[row[0]] for row in self._cu.fetchall())

    def clr(self) -> None:
        """Clear the message index (remove all messages)."""

        self._cu.execute("DELETE FROM messages")
        self._cx.commit()

        self._msgs.clear()

    # def _msgs(self, device_id: DeviceIdT) -> tuple[Message, ...]:
    #     msgs = [msg for msg in self._msgs.values() if msg.src.id == device_id]
    #     return msgs
