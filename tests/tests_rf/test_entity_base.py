#!/usr/bin/env python3
"""RAMSES RF - Unittests for entity_base."""

from collections.abc import Generator
from datetime import datetime as dt, timedelta as td
from unittest.mock import AsyncMock, MagicMock

import pytest

from ramses_rf.const import I_, RP
from ramses_rf.entity import _Entity
from ramses_rf.gateway import Gateway
from ramses_rf.messages import Message
from ramses_rf.routing import StateHeader
from ramses_rf.state import MessageStore
from ramses_tx import Code, DeviceIdT, Packet


@pytest.fixture
def mock_gateway() -> Generator[MagicMock, None, None]:
    """Create a mock Gateway instance for testing."""
    gateway = MagicMock(spec=Gateway)
    gateway.send_cmd = AsyncMock()
    gateway.dispatcher = MagicMock()
    gateway.dispatcher.send = MagicMock()

    # Add required attributes
    gateway.config = MagicMock()
    gateway.config.disable_discovery = True
    gateway.config.enable_eavesdrop = False

    # Explicitly attach the nested engine mock to bypass the spec restriction
    gateway._engine = MagicMock()
    gateway._engine._include = {}

    gateway._loop = MagicMock()
    gateway._loop.call_soon = MagicMock()
    gateway._loop.call_later = MagicMock()
    gateway._loop.time = MagicMock(return_value=0.0)

    # activate the SQLite MessageStore
    gateway.message_store = MessageStore(maintain=False)

    yield gateway


class Test_entity_base:
    """Test _Entity class (formerly _MessageDB)."""

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
        """Test the base entity behavior for a device."""
        dev = _Entity(mock_gateway)
        dev.id = DeviceIdT("04:189078")
        dev._z_id = dev.id

        # put messages in the message_store (bypass proxy)
        assert dev._gwy.message_store is not None
        dev._gwy.message_store.add(self.msg5)
        dev._gwy.message_store.add(self.msg6)
        dev._gwy.message_store.add(self.msg7)
        assert len(await dev._gwy.message_store.all()) == 3, "len(msg_db.all) wrong"

        # start tests
        assert dev.id == "04:189078"

        # create _msgs using internal Enums instead of strings
        assert await dev.entity_state.get_message_log_flat() == {
            Code._12B0: self.msg7,
            Code._3150: self.msg5,
            Code._3220: self.msg6,
        }, "base message_log_flat wrong"

        # find our Codes
        assert sorted(await dev.entity_state._msg_dev_qry() or []) == sorted(
            [
                Code._3150,
                Code._12B0,
                Code._3220,
            ]
        ), "base _msg_dev_qry wrong"

        # list our messages
        assert sorted(await dev.entity_state.get_all_messages()) == sorted(
            [
                self.msg5,
                self.msg7,
                self.msg6,
            ]
        ), "_msg_list wrong"

        # create _msgz - use StateHeader objects for lookups
        cache = await dev.entity_state._build_state_cache()
        assert (
            cache.get_message(StateHeader.create(Code._12B0, I_, "04:189078", "01"))
            == self.msg7
        )
        assert (
            cache.get_message(StateHeader.create(Code._3150, I_, "04:189078", "01"))
            == self.msg5
        )
        assert (
            cache.get_message(StateHeader.create(Code._3220, RP, "01:145038", "11"))
            == self.msg6
        )
        assert len(cache.get_all()) == 3, "base state_cache wrong"

        mock_gateway.message_store.stop()  # close sqlite3 connection

    async def test_entity_base_zone(self, mock_gateway: MagicMock) -> None:
        """Test the base entity behavior for a zone."""
        dev = _Entity(mock_gateway)
        dev.id = DeviceIdT("04:189078_01")
        dev._z_id = dev.id

        # put messages in the message_store (bypass proxy)
        assert dev._gwy.message_store is not None
        dev._gwy.message_store.add(self.msg5)
        dev._gwy.message_store.add(self.msg6)
        dev._gwy.message_store.add(self.msg7)

        # start tests
        assert dev.id == "04:189078_01"

        # create _msgs
        assert await dev.entity_state.get_message_log_flat() == {
            Code._12B0: self.msg7,
            Code._3150: self.msg5,
        }, "zone message_log_flat wrong"

        # find our Codes
        assert sorted(await dev.entity_state._msg_dev_qry() or []) == sorted(
            [
                Code._3150,
                Code._12B0,
            ]
        ), "zone _msg_dev_qry wrong"

        # list our messages
        assert sorted(await dev.entity_state.get_all_messages()) == sorted(
            [
                self.msg5,
                self.msg7,
            ]
        ), "_msg_list wrong"

        # create _msgz
        cache = await dev.entity_state._build_state_cache()
        assert (
            cache.get_message(StateHeader.create(Code._12B0, I_, "04:189078", "01"))
            == self.msg7
        )
        assert (
            cache.get_message(StateHeader.create(Code._3150, I_, "04:189078", "01"))
            == self.msg5
        )
        assert len(cache.get_all()) == 2, "zone state_cache wrong"

        mock_gateway.message_store.stop()  # close sqlite3 connection

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
        """Test the base entity behavior for DHW."""
        dev = _Entity(mock_gateway)
        dev.id = DeviceIdT("01:145038_HW")
        dev._z_id = dev.id

        # put messages in the message_store (bypass proxy)
        assert dev._gwy.message_store is not None
        dev._gwy.message_store.add(self.msg8)
        dev._gwy.message_store.add(self.msg9)

        # start tests
        assert dev.id == "01:145038_HW"
        assert await dev._gwy.message_store.all() == (self.msg8, self.msg9), (
            "wrong dhw all"
        )

        # create _msgs
        assert await dev.entity_state.get_message_log_flat() == {
            Code._1260: self.msg9,
            Code._3150: self.msg8,
        }, "dhw message_log_flat wrong"

        # find our Codes
        assert sorted(await dev.entity_state._msg_dev_qry() or []) == sorted(
            [
                Code._3150,
                Code._1260,
            ]
        ), "dhw _msg_dev_qry wrong"

        # list our messages
        assert sorted(await dev.entity_state.get_all_messages()) == sorted(
            [
                self.msg8,
                self.msg9,
            ]
        ), "dhw _msg_list wrong"

        # create _msgz
        cache = await dev.entity_state._build_state_cache()
        assert (
            cache.get_message(StateHeader.create(Code._1260, RP, "01:145038", "00"))
            == self.msg9
        )
        assert (
            cache.get_message(StateHeader.create(Code._3150, I_, "01:145038", "FC"))
            == self.msg8
        )
        assert len(cache.get_all()) == 2, "dhw state_cache wrong"

        mock_gateway.message_store.stop()  # close sqlite3 connection

    def test_msg_value_msg_hardening(self, mock_gateway: MagicMock) -> None:
        """Test hardening fixes in _msg_value_msg (empty lists, full list return)."""
        dev = _Entity(mock_gateway)
        dev.id = DeviceIdT("01:123456")

        # Case 1: Empty payload list (should not crash with IndexError)
        msg_empty = MagicMock(spec=Message)
        msg_empty.payload = []
        msg_empty._expired = False
        msg_empty.code = Code._000A

        assert dev.entity_state._msg_value_msg(msg_empty) is None

        # Case 2: Payload is a list, key='*' (should return full list)
        payload_list = [
            {"zone_idx": "00", "val": 10},
            {"zone_idx": "01", "val": 20},
        ]
        msg_list = MagicMock(spec=Message)
        msg_list.payload = payload_list
        msg_list._expired = False
        msg_list.code = Code._000A

        # key='*' -> return full list
        val = dev.entity_state._msg_value_msg(msg_list, key="*")
        assert val == payload_list
        assert isinstance(val, list)

        # key=None -> return full list (default behavior if key arg is omitted in call)
        val = dev.entity_state._msg_value_msg(msg_list)
        assert val == payload_list

        # Case 3: Legacy Fallback - Payload is list, specific key requested, no zone_idx
        # Should return value from index 0
        val = dev.entity_state._msg_value_msg(msg_list, key="val")
        assert val == 10  # from index 0 ('00')

        # Case 4: Correct filtering when zone_idx is provided
        val = dev.entity_state._msg_value_msg(msg_list, key="val", zone_idx="01")
        assert val == 20


def test_opentherm_bridge_polling_schedule() -> None:
    """Verify that OpenTherm Bridge (OTB) devices resolve OpenTherm polling schedules."""
    from unittest.mock import MagicMock

    from ramses_rf.devices.opentherm_bridge import OtbGateway
    from ramses_rf.pipeline.polling import PollingManager

    mock_gwy = MagicMock()
    mock_addr = MagicMock()
    mock_addr.id = "10:123456"
    mock_addr.type = "10"

    otb = OtbGateway(mock_gwy, mock_addr)
    schedule = PollingManager.resolve_schedule_for_device(otb)

    assert "3EF0" in schedule, "OpenTherm Modulation (3EF0) not in OTB schedule"
    assert "3220" in schedule, "OpenTherm Data ID (3220) query not in OTB schedule"
