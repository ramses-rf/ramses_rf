#!/usr/bin/env python3
"""RAMSES RF - Unittests for dispatcher."""

from collections.abc import Generator
from datetime import datetime as dt, timedelta as td
from unittest.mock import AsyncMock, MagicMock

import pytest

from ramses_rf import Device, dispatcher
from ramses_rf.database import MessageIndex
from ramses_rf.gateway import Gateway
from ramses_tx import Address, DeviceIdT, Message, Packet


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


class Test_dispatcher_gateway:
    """Test  Dispatcher class."""

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
            "061 RP --- 10:078099 01:087939 --:------ 3220 005 00C0110000",  # OTB
        )
    )

    @pytest.mark.skip(reason="requires gwy")
    async def test_create_devices_from_addrs(self, mock_gateway: MagicMock) -> None:
        # device_by_id = {
        dev1 = Device(mock_gateway, Address(DeviceIdT("04:189078")))
        # dev2 = Device(mock_gateway, Address(DeviceIdT("01:145038")))
        # }
        mock_gateway.device_by_id = MagicMock(return_value=dev1)
        mock_gateway._check_dst_slug = MagicMock(return_value="CTL")
        dispatcher._create_devices_from_addrs(mock_gateway, self.msg5)

    async def test_check_msg_addrs(self) -> None:
        dispatcher._check_msg_addrs(self.msg5)
        dispatcher._check_msg_addrs(self.msg6)

    async def test_check_dst_slug(self) -> None:
        dispatcher._check_dst_slug(self.msg5)

    async def test_detect_array_fragment(self) -> None:
        msg1: Message = Message._from_pkt(
            Packet(
                self._NOW,
                "...  I --- 01:158182 --:------ 01:158182 000A 048 001001F40BB8011101F40BB8021101F40BB8031001F40BB8041101F40BB8051101F40BB8061101F40BB8071001F40BB8",
            )
        )
        msg2: Message = Message._from_pkt(
            Packet(
                self._NOW + td(seconds=1),  # delta dtm < 3 secs
                "...  I --- 01:158182 --:------ 01:158182 000A 006 081001F409C4",
            )
        )
        msg3: Message = Message._from_pkt(
            Packet(
                self._NOW + td(seconds=10),  # delta dtm > 3 secs
                "...  I --- 01:158182 --:------ 01:158182 000A 006 081001F409C4",
            )
        )
        assert msg1._has_array
        assert dispatcher.detect_array_fragment(msg2, msg1)
        assert not dispatcher.detect_array_fragment(msg3, msg1)
