#!/usr/bin/env python3
"""RAMSES RF - Unit test for MessageIndex."""

import contextlib
from datetime import datetime as dt, timedelta as td

from ramses_rf.database import MessageIndex
from ramses_tx import Message, Packet


class TestMessageIndex:
    """Test  MessageIndex class."""

    _SRC1 = "32:166025"
    _SRC2 = "01:087939"  # (CTR)
    _NOW = dt.now().replace(microsecond=0)
    pkt1 = Packet(_NOW, "...  I --- 32:166025 --:------ 32:166025 1298 003 007FFF")
    msg1: Message = Message._from_pkt(pkt1)
    pkt2 = Packet(
        _NOW + td(seconds=10),
        "...  I --- 32:166025 --:------ 32:166025 1298 003 001230",
    )
    msg2: Message = Message._from_pkt(pkt2)
    pkt3 = Packet(
        _NOW + td(seconds=20),
        "060  I --- 01:087939 --:------ 01:087939 2309 021 0007D00106400201F40301F40401F40501F40601F4",
    )
    msg3: Message = Message._from_pkt(pkt3)
    pkt4 = Packet(
        _NOW + td(seconds=30),
        "060  I --- 32:166025 --:------ 32:166025 31DA 030 00EF00019E00EF06E17FFF08020766BE09001F0000000000008500850000",
    )
    msg4: Message = Message._from_pkt(pkt4)

    async def test_add_msg(self) -> None:
        """Add a message to the MessageIndex."""
        msg_db = MessageIndex()
        ret: Message | None

        # add a message
        assert self.msg1.payload == {
            "co2_level": None,
        }, "unexpected parsed payload"

        ret = msg_db.add(self.msg1)  # replaced message

        assert ret is None
        assert msg_db.contains(code="1298")
        assert len(msg_db.all()) == 1
        assert (
            str(msg_db.all())
            == "( I --- 32:166025 --:------ 32:166025 1298 003 007FFF,)"
        )

        # add another message with same code
        ret = msg_db.add(self.msg2)  # replaced message

        assert (
            str(ret)
            == "||  32:166025 |            |  I | co2_level        |      || {'co2_level': None}"
        )
        assert len(msg_db.all()) == 1

        # add another message with different code
        ret = msg_db.add(self.msg3)  # replaced message

        assert ret is None
        assert len(msg_db.all()) == 2

        # test clear index
        msg_db.clr()
        assert len(msg_db.all()) == 0

    async def test_qry_msg(self) -> None:
        """Query the MessageIndex."""
        msg_db = MessageIndex()
        msg_db.add(self.msg1)
        msg_db.add(self.msg2)
        msg_db.add(self.msg3)
        msg_db.add(self.msg4)

        # qry by code
        assert msg_db.contains(code="2309"), "code missing"
        assert msg_db.contains(src="01:087939", code="2309"), "src missing"
        assert not msg_db.contains(src="01:12345", code="2309"), (
            "random src should return False"
        )
        assert not msg_db.contains(code="1234"), "a random code should return False"
        assert msg_db.contains(dst="01:087939"), "dst missing"
        assert not msg_db.contains(plk="co2_level"), (
            "payload keys skipped if value is None"
        )

        with contextlib.suppress(ValueError):
            msg_db.qry_field("RANDOM from messages", (self._SRC1, self._SRC1))
        # Only SELECT queries are allowed

        # Use simplest SQLite query on MessageIndex
        sql = """
                SELECT code, plk from messages WHERE (src = ? OR dst = ?)
            """
        res: list[tuple[dt | str, str]] = msg_db.qry_field(
            sql, (self._SRC2, self._SRC2)
        )
        assert res == [
            (
                "2309",
                "|zone_idx|setpoint|",
            ),
        ]

        # Use multi-field SQLite query on MessageIndex
        sql = """
                SELECT code, plk from messages WHERE verb in (' I', 'RP')
                AND (src = ? OR dst = ?)
                AND code in ('1298', '31DA')
                AND (plk LIKE '%co2_level%')
            """
        res = msg_db.qry_field(sql, (self._SRC1, self._SRC1))
        assert res == [  # key 'co2_level' included since value is not None
            ("1298", "|co2_level|"),
            (
                "31DA",
                "|hvac_id|exhaust_fan_speed|fan_info|_unknown_fan_info_flags|co2_level|indoor_humidity|exhaust_temp|indoor_temp|outdoor_temp|speed_capabilities|bypass_position|supply_fan_speed|remaining_mins|post_heat|pre_heat|supply_flow_fault|exhaust_flow_fault|_extra|",
            ),
        ]
        assert msg_db.contains(plk="|co2_level|"), "payload keys missing"

        # Use payload key SQLite query on MessageIndex
        sql = """
                SELECT code, ctx from messages WHERE verb in (' I', 'RP')
                AND (src = ? OR dst = ?)
                AND (plk LIKE '%co2_level%')
            """
        res = msg_db.qry_field(sql, (self._SRC1, self._SRC1))
        assert res == [("1298", "False"), ("31DA", "00")]

        assert msg_db.contains(plk="|co2_level|"), "payload keys missing"

        assert len(msg_db.all()) == 3
