#!/usr/bin/env python3
"""RAMSES RF - Unittests for entity_base."""

from collections.abc import Generator
from datetime import datetime as dt, timedelta as td
from unittest.mock import AsyncMock, MagicMock

import pytest

from ramses_rf.database import MessageIndex
from ramses_rf.entity_base import _MessageDB
from ramses_rf.gateway import Gateway
from ramses_tx import Code, DeviceIdT, Message, Packet


@pytest.fixture
def mock_gateway() -> Generator[MagicMock, None, None]:
    """Create a mock Gateway instance for testing."""
    gateway = MagicMock(spec=Gateway)
    gateway.send_cmd = AsyncMock()
    gateway.dispatcher = MagicMock()
    gateway.dispatcher.send = MagicMock()

    # Add required attributes
    gateway.config = MagicMock()
    gateway.config.disable_discovery = False
    gateway.config.enable_eavesdrop = False
    gateway._loop = MagicMock()
    gateway._loop.call_soon = MagicMock()
    gateway._loop.call_later = MagicMock()
    gateway._loop.time = MagicMock(return_value=0.0)
    gateway._include = {}
    # activate the SQLite MessageIndex
    gateway.msg_db = MessageIndex(maintain=False)

    yield gateway


class Test_entity_base:
    """Test _MessageDB class."""

    _SRC1 = "32:166025"
    _SRC2 = "01:087939"  # (CTR)
    _NONA = "--:------"
    _NOW = dt.now().replace(microsecond=0)

    msg5: Message = Message._from_pkt(
        Packet(
            _NOW + td(seconds=40),
            "...  I --- 04:189078 --:------ 01:145038 3150 002 0100",  # heat_demand
        )
    )

    msg6: Message = Message._from_pkt(
        Packet(
            _NOW + td(seconds=50),
            "061 RP --- 01:145038 04:189078 --:------ 3220 005 00C0110000",  # OTB
        )
    )

    msg7: Message = Message._from_pkt(
        Packet(
            _NOW + td(seconds=60),
            "...  I --- 04:189078 --:------ 01:145038 12B0 003 010000",  # window_open
        )
    )

    async def test_entity_base_dev(self, mock_gateway: MagicMock) -> None:
        # issues fetching results
        dev = _MessageDB(mock_gateway)
        dev.id = DeviceIdT("04:189078")
        dev._z_id = dev.id

        # put messages in the msg_db
        dev._handle_msg(self.msg5)
        dev._handle_msg(self.msg6)
        dev._handle_msg(self.msg7)
        assert dev._gwy.msg_db
        assert len(dev._gwy.msg_db.all()) == 3, "len(msg_db.all) wrong"

        # start tests
        assert dev.id == "04:189078"

        sql = """
            SELECT dtm from messages WHERE
            verb in (' I', 'RP')
            AND (src = ? OR dst = ?)
            AND ctx LIKE ?
        """
        assert dev._gwy.msg_db.qry(
            sql, (dev.id[:9], dev.id[:9], f"%{dev.id[10:]}%")
        ) == (
            self.msg5,
            self.msg7,
            self.msg6,
        ), "base qry wrong"

        # create _msgs
        assert dev._msgs == {"12B0": self.msg7, "3150": self.msg5, "3220": self.msg6}, (
            "base _msgs wrong"
        )

        # find our Codes
        assert dev._msg_dev_qry() == [
            Code._3150,
            Code._12B0,
            Code._3220,
        ], "base _msg_dev_qry wrong"

        # list our messages
        assert dev._msg_list == [self.msg5, self.msg7, self.msg6], "_msg_list wrong"

        # create _msgz
        assert dev._msgz == {
            "12B0": {" I": {"01": self.msg7}},
            "3150": {" I": {"01": self.msg5}},
            "3220": {"RP": {"11": self.msg6}},
        }, "base _msgz wrong"

        mock_gateway.msg_db.stop()  # close sqlite3 connection

    async def test_entity_base_zone(self, mock_gateway: MagicMock) -> None:
        # works as expected
        dev = _MessageDB(mock_gateway)
        dev.id = DeviceIdT("04:189078_01")
        dev._z_id = dev.id

        # put messages in the msg_db
        dev._handle_msg(self.msg5)
        dev._handle_msg(self.msg6)
        dev._handle_msg(self.msg7)

        # start tests
        assert dev.id == "04:189078_01"
        assert dev._gwy.msg_db

        sql = """
            SELECT dtm from messages WHERE
            verb in (' I', 'RP')
            AND (src = ? OR dst = ?)
            AND ctx LIKE ?
        """
        assert dev._gwy.msg_db.qry(
            sql, (dev.id[:9], dev.id[:9], f"%{dev.id[10:]}%")
        ) == (
            self.msg5,
            self.msg7,
        ), "zone qry wrong"

        # create _msgs
        assert dev._msgs == {"12B0": self.msg7, "3150": self.msg5}, "zone _msgs wrong"

        # find our Codes
        assert dev._msg_dev_qry() == [
            Code._3150,
            Code._12B0,
        ], "zone _msg_dev_qry wrong"

        # list our messages
        assert dev._msg_list == [self.msg5, self.msg7], "_msg_list wrong"

        # create _msgz
        assert dev._msgz == {
            "12B0": {" I": {"01": self.msg7}},
            "3150": {" I": {"01": self.msg5}},
        }, "zone _msgz wrong"

        mock_gateway.msg_db.stop()  # close sqlite3 connection

    msg8: Message = Message._from_pkt(
        Packet(
            _NOW + td(seconds=70),
            "045  I --- 01:145038 --:------ 01:145038 3150 002 FC90",  # heat_demand
        )
    )
    msg9: Message = Message._from_pkt(
        Packet(
            _NOW + td(seconds=80),
            "045 RP --- 01:145038 18:006402 --:------ 1260 003 00182B",  # setpoint
        )
    )

    async def test_entity_base_dhw(self, mock_gateway: MagicMock) -> None:
        # works as expected
        dev = _MessageDB(mock_gateway)
        dev.id = DeviceIdT("01:145038_HW")
        dev._z_id = dev.id

        # put messages in the msg_db
        dev._handle_msg(self.msg8)
        dev._handle_msg(self.msg9)

        # start tests
        assert dev.id == "01:145038_HW"
        assert dev._gwy.msg_db
        assert dev._gwy.msg_db.all() == (self.msg8, self.msg9), "wrong dhw all"

        sql = """
                SELECT dtm from messages WHERE
                verb in (' I', 'RP')
                AND (src = ? OR dst = ?)
                AND (ctx IN ('FC', 'FA', 'F9', 'FA') OR plk LIKE ?)
            """
        _ctx_qry = "%dhw_idx%"
        # SELECT just fields
        # assert dev._gwy.msg_db.qry_field(
        #     sql, (dev.id[:9], dev.id[:9], _ctx_qry)
        # ) == [('FC',), ('00',)]

        # fetch Messages
        assert dev._gwy.msg_db.qry(sql, (dev.id[:9], dev.id[:9], _ctx_qry)) == (
            self.msg8,
            self.msg9,
        ), "dhw qry wrong"

        # create _msgs
        assert dev._msgs == {"1260": self.msg9, "3150": self.msg8}, "dhw _msgs wrong"

        # find our Codes
        assert dev._msg_dev_qry() == [
            Code._3150,
            Code._1260,
        ], "dhw _msg_dev_qry wrong"

        # list our messages
        assert dev._msg_list == [self.msg8, self.msg9], "dhw _msg_list wrong"

        # create _msgz
        assert dev._msgz == {
            "1260": {"RP": {"00": self.msg9}},
            "3150": {" I": {"FC": self.msg8}},
        }, "dhw _msgz wrong"

        mock_gateway.msg_db.stop()  # close sqlite3 connection
