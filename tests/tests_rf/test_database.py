#!/usr/bin/env python3
"""RAMSES RF - Unit test for MessageIndex."""

from datetime import datetime as dt

from ramses_rf.database import MessageIndex
from ramses_tx import Message, Packet


async def test_add_msg() -> None:
    """Add a message to the MessageIndex."""

    msg_db = MessageIndex()

    pkt1 = Packet(dt.now(), "...  I --- 32:166025 --:------ 32:166025 1298 003 007FFF")
    msg1: Message = Message._from_pkt(pkt1)

    if msg_db:  # central SQLite MessageIndex
        ret = msg_db.add(msg1)
        # replaced message that might be returned

    assert ret is None
    assert msg_db.contains(code="1298")
    assert len(msg_db.all()) == 1
    assert (
        str(msg_db.all()) == "( I --- 32:166025 --:------ 32:166025 1298 003 007FFF,)"
    )

    pkt2 = Packet(dt.now(), "...  I --- 32:166025 --:------ 32:166025 1298 003 007FFF")
    msg2: Message = Message._from_pkt(pkt2)

    if msg_db:  # central SQLite MessageIndex
        ret = msg_db.add(msg2)
        # replaced message that might be returned
    assert (
        str(ret)
        == "||  32:166025 |            |  I | co2_level        |      || {'co2_level': None}"
    )

    msg_db.clr()
    assert len(msg_db.all()) == 0
