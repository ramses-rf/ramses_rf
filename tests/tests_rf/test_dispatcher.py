#!/usr/bin/env python3
"""RAMSES RF - Unit test for dispatcher."""

from datetime import datetime as dt, timedelta as td

import pytest

from ramses_rf import dispatcher
from ramses_tx import Message, Packet


class Test_dispatcher_gateway:
    """Test  Dispatcher class."""

    _SRC1 = "32:166025"
    _SRC2 = "01:087939"  # (CTR)
    _NONA = "--:------"
    _NOW = dt.now().replace(microsecond=0)

    msg1: Message = Message._from_pkt(
        Packet(_NOW, "...  I --- 32:166025 --:------ 32:166025 1298 003 007FFF")
    )
    msg2: Message = Message._from_pkt(
        Packet(
            _NOW + td(seconds=10),
            "...  I --- 32:166025 --:------ 32:166025 1298 003 001230",  # co2_level
        )
    )
    msg3: Message = Message._from_pkt(
        Packet(
            _NOW + td(seconds=20),
            "060  I --- 01:087939 --:------ 01:087939 2309 021 0007D00106400201F40301F40401F40501F40601F4",
        )
    )
    msg4: Message = Message._from_pkt(
        Packet(
            _NOW + td(seconds=30),
            "060  I --- 32:166025 --:------ 32:166025 31DA 030 00EF00019E00EF06E17FFF08020766BE09001F0000000000008500850000",
        )
    )
    msg5: Message = Message._from_pkt(
        Packet(
            _NOW + td(seconds=40),
            "...  I --- 04:189078 --:------ 01:145038 3150 002 0100",  # heat_demand
        )
    )

    msg6: Message = Message._from_pkt(
        Packet(
            _NOW + td(seconds=50),
            "061 RP --- 10:078099 01:087939 --:------ 3220 005 00C0110000",  # OTB
        )
    )

    @pytest.mark.skip(reason="requires gwy")
    async def test_create_devices_from_addrs(self) -> None:
        dispatcher._create_devices_from_addrs(self.msg1)

    async def test__check_msg_addrs(self) -> None:
        dispatcher._check_msg_addrs(self.msg5)
        dispatcher._check_msg_addrs(self.msg6)

    async def test__check_dst_slug(self) -> None:
        dispatcher._check_dst_slug(self.msg5)
