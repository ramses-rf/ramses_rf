#!/usr/bin/env python3
"""RAMSES RF - Unittests for dispatcher."""

import logging
import threading
from collections.abc import Generator
from datetime import datetime as dt, timedelta as td
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ramses_rf import Device, dispatcher, exceptions as exc
from ramses_rf.const import (
    SZ_BYPASS_POSITION,
    SZ_FAN_MODE,
    SZ_INDOOR_HUMIDITY,
    SZ_REQ_REASON,
    Code,
    DevType,
    Verb,
)
from ramses_rf.gateway import Gateway, GatewayConfig
from ramses_rf.messages import Message
from ramses_rf.models import HvacState
from ramses_rf.state import MessageStore
from ramses_tx import Address, DeviceIdT, Packet
from ramses_tx.config import EngineConfig


@pytest.fixture
def mock_gateway() -> Generator[MagicMock, None, None]:
    """Create a mock Gateway instance for testing."""
    gateway = MagicMock(spec=Gateway)
    gateway.send_cmd = AsyncMock()

    # Use the strictly typed GatewayConfig DTO instead of loose mock attributes
    gateway.config = GatewayConfig(
        disable_discovery=False,
        enable_eavesdrop=False,
        reduce_processing=0,
    )

    # Mock the internal engine and its loop to reflect the new architecture
    gateway._engine = MagicMock()
    gateway._engine._loop = MagicMock()
    gateway._engine._loop.call_soon = MagicMock()
    gateway._engine._loop.call_later = MagicMock()
    gateway._engine._loop.time = MagicMock(return_value=0.0)

    # Support legacy proxy access (dispatcher.py currently still uses `gwy._loop`)
    gateway._loop = gateway._engine._loop

    # Correctly mock the device registry structure
    gateway.device_registry = MagicMock()
    gateway.device_registry.device_by_id = {}

    gateway._engine._include = {}

    # activate the SQLite MessageStore
    gateway.message_store = MessageStore(maintain=False)

    yield gateway


class Test_dispatcher_gateway:
    """Test Dispatcher class."""

    _SRC1 = "32:166025"
    _SRC2 = "01:087939"  # (CTR)
    _NONA = "--:------"
    _NOW = dt.now().replace(microsecond=0)

    msg5: Message = Message._from_packet(
        Packet(
            _NOW + td(seconds=40),
            # heat_demand
            "...  I --- 04:189078 --:------ 01:145038 3150 002 0100",
        )
    )

    msg6: Message = Message._from_packet(
        Packet(
            _NOW + td(seconds=50),
            # OTB
            "061 RP --- 10:078099 01:087939 --:------ 3220 005 00C0110000",
        )
    )

    @pytest.mark.skip(reason="requires gwy")
    def test_instantiate_devices(self, mock_gateway: MagicMock) -> None:
        """Test device creation from addresses via pipeline stage."""
        dev1 = Device(mock_gateway, Address(cast(DeviceIdT, "04:189078")))
        mock_gateway.device_registry.device_by_id.get = MagicMock(
            return_value=dev1
        )
        mock_gateway._check_dst_slug = MagicMock(return_value="CTL")

        dispatcher.instantiate_devices(mock_gateway, self.msg5)

        mock_gateway.message_store.stop()  # close sqlite3 connection

    def test_validate_addresses(self, mock_gateway: MagicMock) -> None:
        """Test address validation via pipeline stage."""
        dispatcher.validate_addresses(mock_gateway, self.msg5)
        dispatcher.validate_addresses(mock_gateway, self.msg6)

    def test_validate_slugs(self, mock_gateway: MagicMock) -> None:
        """Test destination slug validation via pipeline stage."""
        dispatcher.validate_slugs(mock_gateway, self.msg5)


class TestValidateSlugsDisRemParity:
    """The DIS slug table must cover every REM verb/code — a display is
    a remote plus display/VMI requests — except 1060 (battery state):
    a DIS is mains-powered.

    Regression: a declared-DIS device (e.g. a _faked 37: remote/display)
    sending RQ 10D0 to its FAN was dropped by validate_slugs as
    PacketInvalid, and the FAN's RP 10D0 reply was dropped too.
    """

    _DIS = "37:168270"
    _FAN = "32:153289"
    _NOW = dt.now().replace(microsecond=0)

    @pytest.fixture(autouse=True)
    def _devices(self, mock_gateway: MagicMock) -> None:
        dis_dev = MagicMock(spec=Device)
        dis_dev._SLUG = DevType.DIS
        fan_dev = MagicMock(spec=Device)
        fan_dev._SLUG = DevType.FAN
        mock_gateway.device_registry.device_by_id = {
            self._DIS: dis_dev,
            self._FAN: fan_dev,
        }
        mock_gateway.hgi.id = "18:000730"

    @pytest.mark.parametrize(
        "packet_line",
        [
            # DIS polls the FAN for filter status (VMI request)
            "... RQ --- 37:168270 32:153289 --:------ 10D0 001 00",
            # DIS resets the FAN's filter counter
            "...  W --- 37:168270 32:153289 --:------ 10D0 002 00FF",
            # DIS writes the datetime (VMI write)
            "...  W --- 37:168270 32:153289 --:------ 313F 009 "
            "006000320C040207E6",
        ],
    )
    def test_dis_source_codes_accepted(
        self, mock_gateway: MagicMock, packet_line: str
    ) -> None:
        """A DIS may Tx the REM's request/write codes."""
        msg = Message._from_packet(Packet(self._NOW, packet_line))
        assert dispatcher.validate_slugs(mock_gateway, msg) is True

    def test_dis_10d0_reply_accepted(self, mock_gateway: MagicMock) -> None:
        """The FAN's RP 10D0 reply to a DIS must not be dropped."""
        msg = Message._from_packet(
            Packet(
                self._NOW,
                "... RP --- 32:153289 37:168270 --:------ 10D0 006 "
                "0000B4000000",
            )
        )
        assert dispatcher.validate_slugs(mock_gateway, msg) is True

    def test_dis_1060_rejected(self, mock_gateway: MagicMock) -> None:
        """A DIS has no battery — I 1060 from a DIS stays invalid."""
        msg = Message._from_packet(
            Packet(
                self._NOW,
                "...  I --- 37:168270 --:------ 37:168270 1060 003 00C800",
            )
        )
        with pytest.raises(exc.PacketInvalid):
            dispatcher.validate_slugs(mock_gateway, msg)

    def test_detect_array_fragment(self) -> None:
        """Test detection of array fragments."""
        msg1: Message = Message._from_packet(
            Packet(
                self._NOW,
                "...  I --- 01:158182 --:------ 01:158182 000A 048 001001F40BB8011101F40BB8021101F40BB8031001F40BB8041101F40BB8051101F40BB8061101F40BB8071001F40BB8",
            )
        )
        msg2: Message = Message._from_packet(
            Packet(
                # delta dtm < 3 secs
                self._NOW + td(seconds=1),
                "...  I --- 01:158182 --:------ 01:158182 000A 006 081001F409C4",
            )
        )
        msg3: Message = Message._from_packet(
            Packet(
                # delta dtm > 3 secs
                self._NOW + td(seconds=10),
                "...  I --- 01:158182 --:------ 01:158182 000A 006 081001F409C4",
            )
        )
        assert msg1._has_array
        assert dispatcher.detect_array_fragment(msg2, msg1)
        assert not dispatcher.detect_array_fragment(msg3, msg1)

    def test_sliding_window_survives_intervening_packet(self) -> None:
        """Sliding window reassembly survives an intervening packet.

        The 2-slot _prev_msg approach dropped the array when an unrelated
        packet arrived between fragment 1 and fragment 2.  The sliding
        window (keyed by src_id + code) preserves the pending fragment.
        """

        # Fragment 1: long 000A with _has_array=True
        frag1: Message = Message._from_packet(
            Packet(
                self._NOW,
                "...  I --- 01:158182 --:------ 01:158182 000A 048 001001F40BB8011101F40BB8021101F40BB8031001F40BB8041101F40BB8051101F40BB8061101F40BB8071001F40BB8",
            )
        )
        # Intervening packet from a different source (would corrupt 2-slot)
        intervening: Message = Message._from_packet(
            Packet(
                self._NOW + td(seconds=0.5),
                "...  I --- 02:222222 --:------ 02:222222 30C9 003 000800",
            )
        )
        # Fragment 2: short 000A, same source as fragment 1
        frag2: Message = Message._from_packet(
            Packet(
                self._NOW + td(seconds=1),
                "...  I --- 01:158182 --:------ 01:158182 000A 006 081001F409C4",
            )
        )

        assert frag1._has_array
        # Simulate the sliding window: store frag1, then check frag2
        # In the 2-slot approach, _prev_msg would be `intervening`, not
        # frag1, so detect_array_fragment(frag2, intervening) would be
        # False.  With the sliding window, frag1 is still available.
        assert dispatcher.detect_array_fragment(frag2, frag1)
        assert not dispatcher.detect_array_fragment(frag2, intervening)


class TestDispatcherErrorHandling:
    """Test Dispatcher exception handling logic."""

    async def test_process_msg_strict_mode(
        self, mock_gateway: MagicMock
    ) -> None:
        """Test process_msg raises exception in strict mode."""
        # Enable strict mode
        mock_gateway.config.enforce_strict_handling = True

        # Create a message with a valid payload for code 0001
        msg = Message._from_packet(
            Packet(
                dt.now(),
                "...  I --- 01:000001 --:------ 01:000001 0001 005 00FFFF0200",
            )
        )

        # Force a ValueError within process_msg by mocking the first pipeline
        # stage
        with (
            patch(
                "ramses_rf.dispatcher.validate_addresses",
                side_effect=ValueError("Test Error"),
            ),
            pytest.raises(ValueError, match="Test Error"),
        ):
            await dispatcher.process_msg(mock_gateway, msg)

    async def test_process_msg_safe_mode(
        self, mock_gateway: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test process_msg logs warning with trace in safe mode."""
        # Disable strict mode (safe mode)
        mock_gateway.config.enforce_strict_handling = False

        msg = Message._from_packet(
            Packet(
                dt.now(),
                "...  I --- 01:000001 --:------ 01:000001 0001 005 00FFFF0200",
            )
        )

        # Force a ValueError within process_msg by mocking the first pipeline
        # stage
        with (
            patch(
                "ramses_rf.dispatcher.validate_addresses",
                side_effect=ValueError("Test Error"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            await dispatcher.process_msg(mock_gateway, msg)

        # Assert exception was caught and logged
        assert "Test Error" in caplog.text
        # Check that it was logged as a WARNING
        assert any(r.levelname == "WARNING" for r in caplog.records)
        # Check that traceback information is present (exc_info=True)
        assert any(r.exc_info is not None for r in caplog.records)


class TestDispatcherHeartbeats:
    """Test that heartbeat (empty) payloads are correctly dispatched to
    devices.
    """

    @pytest.mark.parametrize(
        ("packet_line", "src_id", "dev_type"),
        [
            # TRV sending a 3150 heat demand heartbeat (1-byte "00"
            # payload, I verb)
            (
                "045  I --- 04:123456 --:------ 04:123456 3150 001 00",
                "04:123456",
                "TRV",
            ),
            # FAN sending a 2411 fan parameters heartbeat (1-byte "00"
            # payload, RP verb)
            (
                "045 RP --- 32:155617 29:123160 --:------ 2411 001 00",
                "32:155617",
                "FAN",
            ),
            # TRV sending a 12B0 window state heartbeat (1-byte "00"
            # payload, I verb)
            (
                "045  I --- 04:123456 --:------ 04:123456 12B0 001 00",
                "04:123456",
                "TRV",
            ),
            # TRV sending an empty 2309 setpoint heartbeat (1-byte "00"
            # payload, I verb)
            (
                "045  I --- 04:123456 --:------ 04:123456 2309 001 00",
                "04:123456",
                "TRV",
            ),
        ],
    )
    async def test_heartbeat_dispatch(
        self,
        mock_gateway: MagicMock,
        packet_line: str,
        src_id: str,
        dev_type: str,
    ) -> None:
        """Test that empty payload heartbeats are routed to update device
        timestamps.
        """
        # 1. Parse the packet into a Message
        # This confirms that message.py correctly validates and bypasses
        # empty heartbeats
        dtm = dt.now()
        packet = Packet(dtm, packet_line)
        msg = Message(packet.to_dto())

        # Confirm it safely processed as an empty heartbeat message
        assert msg._has_payload is False
        assert msg.payload == {}

        # 2. Setup the mock registry and device
        # We mock a device matching the source ID and set its slug to pass
        # validation
        mock_dev = MagicMock(spec=Device)
        mock_dev.id = src_id
        mock_dev._SLUG = dev_type
        mock_dev._is_binding = False
        mock_dev.is_faked = False
        mock_dev._last_msg_dtm = None

        # Inject the mock device into the registry so instantiate_devices
        # maps to it
        mock_gateway.device_registry.device_by_id[src_id] = mock_dev
        mock_gateway.device_registry.get_device.return_value = mock_dev

        # Give the mocked HGI a different ID so the packet is treated as remote
        mock_gateway.hgi.id = "18:000730"

        # 3. Process the message through the dispatcher
        await dispatcher.process_msg(mock_gateway, msg)

        # 4. Assert the message was explicitly processed and timestamp updated
        assert mock_dev._last_msg_dtm == msg.dtm

    @pytest.mark.parametrize(
        ("packet_line", "src_id", "dev_type"),
        [
            # FAN may never Tx an RQ (issue 1255 regression)
            (
                "045 RQ --- 32:155617 29:123160 --:------ 31DA 001 00",
                "32:155617",
                "FAN",
            ),
            # FAN's 22F3 entry has no permitted Tx verbs
            (
                "045  I --- 32:155617 --:------ 32:155617 22F3 007 "
                "00023C03040000",
                "32:155617",
                "FAN",
            ),
            # REM may never Tx an RP
            (
                "045 RP --- 37:170000 32:155617 --:------ 22F1 003 000204",
                "37:170000",
                "REM",
            ),
            # TRV may only RQ/W_ 2349, not Tx it
            (
                "045  I --- 04:123456 --:------ 04:123456 2349 007 "
                "00070800FFFFFF",
                "04:123456",
                "TRV",
            ),
        ],
    )
    async def test_dropped_packet_still_refreshes_liveness(
        self,
        mock_gateway: MagicMock,
        caplog: pytest.LogCaptureFixture,
        packet_line: str,
        src_id: str,
        dev_type: str,
    ) -> None:
        """A packet rejected by validate_slugs still proves its source
        device is transmitting, so it must refresh the liveness timestamp
        (issue 1255): a FAN that only ever sends 'unexpected' frames would
        otherwise flap to unavailable every HEARTBEAT_TIMEOUT_FAN (15 min).
        """
        dtm = dt.now()
        packet = Packet(dtm, packet_line)
        msg = Message(packet.to_dto())

        mock_dev = MagicMock(spec=Device)
        mock_dev.id = src_id
        mock_dev._SLUG = dev_type
        mock_dev._is_binding = False
        mock_dev.is_faked = False
        mock_dev._last_msg_dtm = None

        mock_gateway.device_registry.device_by_id[src_id] = mock_dev
        mock_gateway.device_registry.get_device.return_value = mock_dev
        mock_gateway.hgi.id = "18:000730"

        with caplog.at_level(logging.WARNING):
            await dispatcher.process_msg(mock_gateway, msg)

        # The packet was rejected at slug validation (PacketInvalid)...
        assert "PacketInvalid" in caplog.text
        # ...but the source device was still marked alive before the drop
        assert mock_dev._last_msg_dtm == msg.dtm

    async def test_liveness_stamp_is_monotonic(
        self,
        mock_gateway: MagicMock,
    ) -> None:
        """A stale packet completing out of order must not regress
        _last_msg_dtm (ramses_cc issue 1278): cached-packet replay feeds
        packets through fire-and-forget handler tasks whose completion
        order does not match packet order, so last-write-wins would leave
        the device stamped with the oldest cached packet's dtm.
        """
        fresh_dtm = dt.now()
        stale_dtm = fresh_dtm - td(hours=3)

        packet_line = "045 RQ --- 32:153289 29:123160 --:------ 31DA 001 00"
        fresh_msg = Message(Packet(fresh_dtm, packet_line).to_dto())
        stale_msg = Message(Packet(stale_dtm, packet_line).to_dto())

        mock_dev = MagicMock(spec=Device)
        mock_dev.id = "32:153289"
        mock_dev._SLUG = DevType.FAN
        mock_dev._is_binding = False
        mock_dev.is_faked = False
        mock_dev._last_msg_dtm = None

        mock_gateway.device_registry.device_by_id["32:153289"] = mock_dev
        mock_gateway.device_registry.get_device.return_value = mock_dev
        mock_gateway.hgi.id = "18:000730"

        # Replay completion order: the old cached packet finishes last
        await dispatcher.process_msg(mock_gateway, fresh_msg)
        assert mock_dev._last_msg_dtm == fresh_msg.dtm

        await dispatcher.process_msg(mock_gateway, stale_msg)
        assert mock_dev._last_msg_dtm == fresh_msg.dtm


class TestHvacStateNullMarkerFiltering:
    """Test that _update_hvac_state does not overwrite good state with
    null-marker values from 31DA/31D9 snapshots.

    See issue #742: HVAC sensors bounce to None/FF/0 every ~10 min because
    31DA polling snapshots include "no sensor" values for unsupported sensors.
    """

    def _make_device(self) -> MagicMock:
        """Create a mock HVAC device with a real HvacState."""
        dev = MagicMock()
        dev.id = "32:153289"
        dev._SLUG = DevType.FAN
        dev.hvac_state = HvacState()
        dev.events = []
        return dev

    def _make_msg(self, code: Code, payload: dict) -> MagicMock:
        """Create a mock message with the given code and payload."""
        msg = MagicMock()
        msg.code = code
        msg.payload = payload
        return msg

    def test_none_bypass_position_does_not_overwrite_good_value(self) -> None:
        """bypass_position=None (EF = not implemented) must not overwrite."""
        dev = self._make_device()
        dev.hvac_state = HvacState(bypass_position=0.5)

        payload = {SZ_BYPASS_POSITION: None}
        msg = self._make_msg(Code._31DA, payload)

        dispatcher._update_hvac_state(dev, payload, msg)
        assert dev.hvac_state.bypass_position == 0.5

    def test_ff_fan_mode_does_not_overwrite_good_value(self) -> None:
        """fan_mode='FF' (no data from 31D9) must not overwrite."""
        dev = self._make_device()
        dev.hvac_state = HvacState(fan_mode="low")

        payload = {SZ_FAN_MODE: "FF"}
        msg = self._make_msg(Code._31D9, payload)

        dispatcher._update_hvac_state(dev, payload, msg)
        assert dev.hvac_state.fan_mode == "low"

    def test_zero_humidity_does_not_overwrite_good_value(self) -> None:
        """indoor_humidity=0.0 (00 = no sensor) must not overwrite."""
        dev = self._make_device()
        dev.hvac_state = HvacState(indoor_humidity=0.55)

        payload = {SZ_INDOOR_HUMIDITY: 0.0}
        msg = self._make_msg(Code._31DA, payload)

        dispatcher._update_hvac_state(dev, payload, msg)
        assert dev.hvac_state.indoor_humidity == 0.55

    def test_good_values_still_update(self) -> None:
        """Normal (non-null-marker) values must still update the state."""
        dev = self._make_device()
        dev.hvac_state = HvacState(fan_mode="low", indoor_humidity=0.40)

        payload = {SZ_FAN_MODE: "high", SZ_INDOOR_HUMIDITY: 0.55}
        msg = self._make_msg(Code._22F1, payload)

        dispatcher._update_hvac_state(dev, payload, msg)
        assert dev.hvac_state.fan_mode == "high"
        assert dev.hvac_state.indoor_humidity == 0.55

    def test_null_markers_do_not_set_initial_state(self) -> None:
        """Null markers must not set state from None to a null value."""
        dev = self._make_device()
        # All fields start as None

        payload = {
            SZ_FAN_MODE: "FF",
            SZ_INDOOR_HUMIDITY: 0.0,
            SZ_BYPASS_POSITION: None,
        }
        msg = self._make_msg(Code._31DA, payload)

        dispatcher._update_hvac_state(dev, payload, msg)
        assert dev.hvac_state.fan_mode is None
        assert dev.hvac_state.indoor_humidity is None
        assert dev.hvac_state.bypass_position is None

    # --- req_reason → request_reason mapping (PR #745) ---

    def test_req_reason_maps_to_request_reason(self) -> None:
        """SZ_REQ_REASON (parser key 'req_reason') must map to
        HvacState.request_reason, not be passed as 'req_reason'.

        Regression test for the CQRS state extraction failure:
            HvacState.__init__() got an unexpected keyword argument
            'req_reason'
        """
        dev = self._make_device()

        payload = {SZ_REQ_REASON: "HUM"}
        msg = self._make_msg(Code._2210, payload)

        dispatcher._update_hvac_state(dev, payload, msg)
        assert dev.hvac_state.request_reason == "HUM"

    def test_req_reason_co2(self) -> None:
        """req_reason='CO2' (payload byte 02) must map to request_reason."""
        dev = self._make_device()

        payload = {SZ_REQ_REASON: "CO2"}
        msg = self._make_msg(Code._2210, payload)

        dispatcher._update_hvac_state(dev, payload, msg)
        assert dev.hvac_state.request_reason == "CO2"

    def test_req_reason_idle(self) -> None:
        """req_reason='IDL' (payload byte 00) must map to request_reason."""
        dev = self._make_device()

        payload = {SZ_REQ_REASON: "IDL"}
        msg = self._make_msg(Code._2210, payload)

        dispatcher._update_hvac_state(dev, payload, msg)
        assert dev.hvac_state.request_reason == "IDL"

    def test_req_reason_does_not_overwrite_with_none(self) -> None:
        """A None req_reason must not overwrite an existing request_reason."""
        dev = self._make_device()
        dev.hvac_state = HvacState(request_reason="HUM")

        payload = {SZ_REQ_REASON: None}
        msg = self._make_msg(Code._2210, payload)

        dispatcher._update_hvac_state(dev, payload, msg)
        assert dev.hvac_state.request_reason == "HUM"

    def test_req_reason_with_other_fields(self) -> None:
        """req_reason must update alongside other fields without error."""
        dev = self._make_device()

        payload = {
            SZ_REQ_REASON: "CO2",
            SZ_FAN_MODE: "high",
            SZ_INDOOR_HUMIDITY: 0.55,
        }
        msg = self._make_msg(Code._2210, payload)

        dispatcher._update_hvac_state(dev, payload, msg)
        assert dev.hvac_state.request_reason == "CO2"
        assert dev.hvac_state.fan_mode == "high"
        assert dev.hvac_state.indoor_humidity == 0.55

    def test_req_reason_absent_does_not_clear(self) -> None:
        """If req_reason is absent from the payload, request_reason must
        not be touched."""
        dev = self._make_device()
        dev.hvac_state = HvacState(request_reason="HUM")

        payload = {SZ_FAN_MODE: "low"}
        msg = self._make_msg(Code._22F1, payload)

        dispatcher._update_hvac_state(dev, payload, msg)
        assert dev.hvac_state.request_reason == "HUM"
        assert dev.hvac_state.fan_mode == "low"

    def test_no_req_reason_error_raised(self) -> None:
        """Ensure _update_hvac_state does not raise TypeError for
        req_reason (the original bug).

        Before the fix, SZ_REQ_REASON was in the fields list and was
        passed directly as updates['req_reason'] to dataclasses.replace,
        causing:
            TypeError: HvacState.__init__() got an unexpected keyword
            argument 'req_reason'
        """
        dev = self._make_device()

        payload = {SZ_REQ_REASON: "HUM", SZ_FAN_MODE: "auto"}
        msg = self._make_msg(Code._2210, payload)

        # Must not raise
        dispatcher._update_hvac_state(dev, payload, msg)
        assert dev.hvac_state.request_reason == "HUM"


class TestForeignHgiDispatcher:
    """Test that foreign HGI sources are not rejected by instantiate_devices.

    See https://github.com/ramses-rf/ramses_cc/issues/822 (comment 5017168119):
    a foreign HGI (18:072981) sends RQs to the controller, but the
    device-registry filter rejects it (not in known_list) when
    enforce_known_list is True.  The un-suppressed get_device(src) call
    drops the entire packet and adds the foreign HGI to the _unwanted list,
    causing repeating FILTER EXCEPTION warnings.
    """

    _FOREIGN_HGI = "18:072981"
    _CTL_ID = "01:216136"
    _ACTIVE_HGI = "18:000730"
    _NOW = dt.now().replace(microsecond=0)

    def _make_msg(self) -> Message:
        """Create a message from a foreign HGI to the controller."""
        return Message._from_packet(
            Packet(
                self._NOW,
                f"... RQ --- {self._FOREIGN_HGI} {self._CTL_ID} --:------ 0004 001 00",
            )
        )

    def test_foreign_hgi_src_skipped_with_enforce_known_list(
        self, mock_gateway: MagicMock
    ) -> None:
        """With enforce_known_list=True, foreign HGI src is not created
        (the filter would reject it) but the packet is NOT dropped.
        """
        # Configure: enforce_known_list=True, eavesdrop on
        mock_gateway.config = GatewayConfig(
            disable_discovery=True,
            enable_eavesdrop=True,
            reduce_processing=0,
            engine=EngineConfig(enforce_known_list=True),
        )
        mock_gateway.hgi.id = self._ACTIVE_HGI

        # Registry is empty — both src and dst are unknown
        mock_gateway.device_registry.device_by_id = {}

        # get_device would raise for the foreign HGI (it's not known),
        # but return a mock for the CTL (dst, via eavesdrop path)
        def _get_device(dev_id: str) -> MagicMock:
            from ramses_rf import exceptions as exc

            if dev_id == self._FOREIGN_HGI:
                raise exc.DeviceNotFoundError(f"Can't create {dev_id}")
            mock_dev = MagicMock(spec=Device)
            mock_dev.id = dev_id
            mock_dev._SLUG = DevType.CTL
            return mock_dev

        mock_gateway.device_registry.get_device = MagicMock(
            side_effect=_get_device
        )

        msg = self._make_msg()
        result = dispatcher.instantiate_devices(mock_gateway, msg)

        # Packet must NOT be dropped
        assert result is True
        # get_device must NOT have been called for the foreign HGI src
        called_ids = [
            call.args[0]
            for call in mock_gateway.device_registry.get_device.call_args_list
        ]
        assert self._FOREIGN_HGI not in called_ids

    def test_foreign_hgi_src_created_without_enforce_known_list(
        self, mock_gateway: MagicMock
    ) -> None:
        """With enforce_known_list=False, the foreign HGI src IS created
        normally (as an HgiGateway) — existing behaviour is preserved.
        """
        mock_gateway.config = GatewayConfig(
            disable_discovery=True,
            enable_eavesdrop=True,
            reduce_processing=0,
            engine=EngineConfig(enforce_known_list=False),
        )
        mock_gateway.hgi.id = self._ACTIVE_HGI

        mock_gateway.device_registry.device_by_id = {}

        mock_dev = MagicMock(spec=Device)
        mock_dev.id = self._FOREIGN_HGI
        mock_dev._SLUG = DevType.HGI
        mock_gateway.device_registry.get_device = MagicMock(
            return_value=mock_dev
        )

        msg = self._make_msg()
        result = dispatcher.instantiate_devices(mock_gateway, msg)

        # Packet must NOT be dropped
        assert result is True
        # get_device MUST have been called for the foreign HGI src
        called_ids = [
            call.args[0]
            for call in mock_gateway.device_registry.get_device.call_args_list
        ]
        assert self._FOREIGN_HGI in called_ids


class TestResolveLogicalTargets:
    """Test resolution of logical targets from messages."""

    def test_zone_temp_fallback_to_main_tcs(
        self, mock_gateway: MagicMock
    ) -> None:
        """Test _resolve_logical_targets falls back to gwy.tcs for zone targets.

        Regression test for #942: zone temp packets from a secondary CTL
        (where src_dev.tcs is None) must still route to the main TCS zones.
        """
        # 1. Setup the Gateway with a main TCS and one zone
        mock_tcs = MagicMock()
        mock_zone = MagicMock()
        mock_tcs.zone_by_index = {"00": mock_zone}
        mock_gateway.tcs = mock_tcs

        # 2. Setup the source device (CTL) which has no TCS property set
        src_id = "01:150003"
        mock_src_dev = MagicMock()
        mock_src_dev.tcs = None
        mock_src_dev.type = "01"
        mock_gateway.device_registry.device_by_id = {src_id: mock_src_dev}

        # 3. Setup the message and payload (a typical 30C9 zone temp broadcast)
        msg = MagicMock()
        msg.src.id = src_id
        msg.dst.id = src_id
        msg.code = Code._30C9
        msg._has_array = False

        payload = {"zone_index": "00", "temperature": 21.0}

        # 4. Resolve the targets
        targets = dispatcher._resolve_logical_targets(
            mock_gateway, msg, payload
        )

        # 5. Assert that the zone from the main TCS was included in the targets
        assert mock_zone in targets, (
            "Zone target was not resolved using fallback gwy.tcs"
        )

    def test_zone_temp_routes_to_designated_sensor(
        self, mock_gateway: MagicMock
    ) -> None:
        """Verify 30C9 routes to parent zone when src is the designated sensor."""
        # Arrange
        mock_zone = MagicMock()
        mock_zone.temp_state = MagicMock()
        mock_zone.zone_state = MagicMock()

        mock_sensor = MagicMock()
        mock_sensor.id = "22:017762"
        mock_sensor.type = "22"
        mock_sensor._parent = mock_zone
        mock_sensor.tcs = None

        mock_trv = MagicMock()
        mock_trv.id = "04:005646"
        mock_trv.type = "04"
        mock_trv._parent = mock_zone
        mock_trv.tcs = None

        mock_zone.sensor = mock_sensor
        mock_zone.actuators = [mock_trv]

        mock_gateway.device_registry.device_by_id = {
            mock_sensor.id: mock_sensor,
            mock_trv.id: mock_trv,
        }

        msg = MagicMock()
        msg.src.id = mock_sensor.id
        msg.dst.id = mock_sensor.id
        msg.code = Code._30C9
        msg._has_array = False
        payload = {"temperature": 21.5}

        # Act
        targets = dispatcher._resolve_logical_targets(
            mock_gateway, msg, payload
        )

        # Assert
        assert mock_zone in targets
        assert mock_sensor in targets

    def test_zone_temp_ignores_non_sensor_trv_when_sensor_present(
        self, mock_gateway: MagicMock
    ) -> None:
        """Verify 30C9 from TRV does not route to zone when separate sensor is set."""
        # Arrange
        mock_zone = MagicMock()
        mock_zone.temp_state = MagicMock()
        mock_zone.zone_state = MagicMock()

        mock_sensor = MagicMock()
        mock_sensor.id = "22:017762"
        mock_sensor.type = "22"
        mock_sensor._parent = mock_zone
        mock_sensor.tcs = None

        mock_trv = MagicMock()
        mock_trv.id = "04:005646"
        mock_trv.type = "04"
        mock_trv._parent = mock_zone
        mock_trv.tcs = None

        mock_zone.sensor = mock_sensor
        mock_zone.actuators = [mock_trv]

        mock_gateway.device_registry.device_by_id = {
            mock_sensor.id: mock_sensor,
            mock_trv.id: mock_trv,
        }

        msg = MagicMock()
        msg.src.id = mock_trv.id
        msg.dst.id = mock_trv.id
        msg.code = Code._30C9
        msg._has_array = False
        payload = {"temperature": 19.5}

        # Act
        targets = dispatcher._resolve_logical_targets(
            mock_gateway, msg, payload
        )

        # Assert
        assert mock_zone not in targets
        assert mock_trv in targets

    def test_zone_temp_routes_to_sole_actuator_when_no_sensor(
        self, mock_gateway: MagicMock
    ) -> None:
        """Verify 30C9 routes to parent zone when sole actuator is the only device."""
        # Arrange
        mock_zone = MagicMock()
        mock_zone.temp_state = MagicMock()
        mock_zone.zone_state = MagicMock()

        mock_trv = MagicMock()
        mock_trv.id = "04:005646"
        mock_trv.type = "04"
        mock_trv._parent = mock_zone
        mock_trv.tcs = None

        mock_zone.sensor = None
        mock_zone._declared_sensor_id = None
        mock_zone.actuators = [mock_trv]

        mock_gateway.device_registry.device_by_id = {
            mock_trv.id: mock_trv,
        }

        msg = MagicMock()
        msg.src.id = mock_trv.id
        msg.dst.id = mock_trv.id
        msg.code = Code._30C9
        msg._has_array = False
        payload = {"temperature": 20.0}

        # Act
        targets = dispatcher._resolve_logical_targets(
            mock_gateway, msg, payload
        )

        # Assert
        assert mock_zone in targets
        assert mock_trv in targets

    def test_zone_temp_ignores_trvs_when_multiple_actuators_and_no_sensor(
        self, mock_gateway: MagicMock
    ) -> None:
        """Verify 30C9 from TRV does not route to zone when multiple TRVs have no designated sensor."""
        # Arrange
        mock_zone = MagicMock()
        mock_zone.temp_state = MagicMock()
        mock_zone.zone_state = MagicMock()

        mock_trv1 = MagicMock()
        mock_trv1.id = "04:005646"
        mock_trv1.type = "04"
        mock_trv1._parent = mock_zone
        mock_trv1.tcs = None

        mock_trv2 = MagicMock()
        mock_trv2.id = "04:005647"
        mock_trv2.type = "04"
        mock_trv2._parent = mock_zone
        mock_trv2.tcs = None

        mock_zone.sensor = None
        mock_zone.actuators = [mock_trv1, mock_trv2]

        mock_gateway.device_registry.device_by_id = {
            mock_trv1.id: mock_trv1,
            mock_trv2.id: mock_trv2,
        }

        msg = MagicMock()
        msg.src.id = mock_trv1.id
        msg.dst.id = mock_trv1.id
        msg.code = Code._30C9
        msg._has_array = False
        payload = {"temperature": 19.0}

        # Act
        targets = dispatcher._resolve_logical_targets(
            mock_gateway, msg, payload
        )

        # Assert
        assert mock_zone not in targets
        assert mock_trv1 in targets

    def test_sensor_setpoint_does_not_route_to_zone(
        self, mock_gateway: MagicMock
    ) -> None:
        """Verify a sensor's self-addressed 2309 never reaches a TCS zone.

        Regression test for ramses-rf/ramses_cc#1208: a DTS92 broadcasts
        its own (device-local) setpoint with idx 00, which is not the
        controller's zone index and must not overwrite zone 00's setpoint.
        """
        # Arrange
        mock_zone = MagicMock()
        mock_zone.temp_state = MagicMock()
        mock_zone.zone_state = MagicMock()

        mock_sensor = MagicMock()
        mock_sensor.id = "22:038387"
        mock_sensor.type = "22"
        mock_sensor._parent = mock_zone
        mock_sensor.tcs = None

        mock_tcs = MagicMock()
        mock_tcs.id = "01:168186"
        mock_tcs.zone_by_index = {"00": mock_zone}
        mock_gateway.tcs = mock_tcs

        mock_zone.sensor = mock_sensor
        mock_zone.actuators = []

        mock_gateway.device_registry.device_by_id = {
            mock_sensor.id: mock_sensor,
        }

        msg = MagicMock()
        msg.src.id = mock_sensor.id
        msg.dst.id = "--:------"
        msg.code = Code._2309
        msg._has_array = False
        payload = {"zone_index": "00", "setpoint": 11.0}

        # Act
        targets = dispatcher._resolve_logical_targets(
            mock_gateway, msg, payload
        )

        # Assert: the sensor's device-local idx must not resolve to a zone
        assert mock_zone not in targets
        assert mock_sensor in targets

    def test_ctl_setpoint_routes_to_zone(
        self, mock_gateway: MagicMock
    ) -> None:
        """Verify a controller's 2309 broadcast still routes to the zone."""
        # Arrange
        mock_zone = MagicMock()

        mock_tcs = MagicMock()
        mock_tcs.id = "01:168186"
        mock_tcs.zone_by_index = {"00": mock_zone}
        mock_gateway.tcs = mock_tcs

        mock_ctl = MagicMock()
        mock_ctl.id = "01:168186"
        mock_ctl.type = "01"
        mock_ctl.tcs = mock_tcs

        mock_gateway.device_registry.device_by_id = {
            mock_ctl.id: mock_ctl,
        }

        msg = MagicMock()
        msg.src.id = mock_ctl.id
        msg.dst.id = mock_ctl.id
        msg.code = Code._2309
        msg._has_array = False
        payload = {"zone_index": "00", "setpoint": 24.0}

        # Act
        targets = dispatcher._resolve_logical_targets(
            mock_gateway, msg, payload
        )

        # Assert
        assert mock_zone in targets

    def test_w_echo_zone_index_does_not_route_to_zone(
        self, mock_gateway: MagicMock
    ) -> None:
        """Verify a W echo carrying a zone_index never reaches the zone.

        Regression test for ramses-rf/ramses_cc#1381: our own
        reset_zone_mode echoes back as `W 2349` (dst = CTL) carrying
        setpoint 7FFF (None); projecting it blanked the zone's setpoint
        for up to a whole scan interval until the controller's I 2349
        reply restored it.  Commands are outbound intent, not reports.
        """
        # Arrange
        mock_zone = MagicMock()

        mock_tcs = MagicMock()
        mock_tcs.id = "01:168186"
        mock_tcs.zone_by_index = {"03": mock_zone}

        mock_hgi = MagicMock()
        mock_hgi.id = "18:001234"
        mock_hgi.type = "18"
        mock_hgi.tcs = None

        mock_ctl = MagicMock()
        mock_ctl.id = "01:168186"
        mock_ctl.type = "01"
        mock_ctl.tcs = mock_tcs

        mock_gateway.device_registry.device_by_id = {
            mock_hgi.id: mock_hgi,
            mock_ctl.id: mock_ctl,
        }

        msg = MagicMock()
        msg.src.id = mock_hgi.id
        msg.dst.id = mock_ctl.id
        msg.code = Code._2349
        msg.verb = Verb.W_
        msg._has_array = False
        payload = {
            "zone_index": "03",
            "mode": "follow_schedule",
            "setpoint": None,
            "until": None,
        }

        # Act
        targets = dispatcher._resolve_logical_targets(
            mock_gateway, msg, payload
        )

        # Assert: the command echo must not project onto the zone
        assert mock_zone not in targets

    def test_rq_zone_index_does_not_route_to_zone(
        self, mock_gateway: MagicMock
    ) -> None:
        """Verify an RQ carrying a zone_index never reaches the zone."""
        # Arrange
        mock_zone = MagicMock()

        mock_tcs = MagicMock()
        mock_tcs.id = "01:168186"
        mock_tcs.zone_by_index = {"03": mock_zone}

        mock_hgi = MagicMock()
        mock_hgi.id = "18:001234"
        mock_hgi.type = "18"
        mock_hgi.tcs = None

        mock_ctl = MagicMock()
        mock_ctl.id = "01:168186"
        mock_ctl.type = "01"
        mock_ctl.tcs = mock_tcs

        mock_gateway.device_registry.device_by_id = {
            mock_hgi.id: mock_hgi,
            mock_ctl.id: mock_ctl,
        }

        msg = MagicMock()
        msg.src.id = mock_hgi.id
        msg.dst.id = mock_ctl.id
        msg.code = Code._2309
        msg.verb = Verb.RQ
        msg._has_array = False
        payload = {"zone_index": "03"}

        # Act
        targets = dispatcher._resolve_logical_targets(
            mock_gateway, msg, payload
        )

        # Assert
        assert mock_zone not in targets

    def test_ctl_i_2349_still_routes_to_zone(
        self, mock_gateway: MagicMock
    ) -> None:
        """Verify the controller's I 2349 reply still routes to the zone.

        The W/RQ exclusion must not suppress the controller's own
        reports — its I/RP confirms remain authoritative for the zone.
        """
        # Arrange
        mock_zone = MagicMock()

        mock_tcs = MagicMock()
        mock_tcs.id = "01:168186"
        mock_tcs.zone_by_index = {"03": mock_zone}

        mock_ctl = MagicMock()
        mock_ctl.id = "01:168186"
        mock_ctl.type = "01"
        mock_ctl.tcs = mock_tcs

        mock_hgi = MagicMock()
        mock_hgi.id = "18:001234"
        mock_hgi.type = "18"
        mock_hgi.tcs = None

        mock_gateway.device_registry.device_by_id = {
            mock_ctl.id: mock_ctl,
            mock_hgi.id: mock_hgi,
        }

        msg = MagicMock()
        msg.src.id = mock_ctl.id
        msg.dst.id = mock_hgi.id
        msg.code = Code._2349
        msg.verb = Verb.I_
        msg._has_array = False
        payload = {
            "zone_index": "03",
            "mode": "follow_schedule",
            "setpoint": 16.0,
            "until": None,
        }

        # Act
        targets = dispatcher._resolve_logical_targets(
            mock_gateway, msg, payload
        )

        # Assert
        assert mock_zone in targets

    def test_system_mode_echo_targets_dst_controller(
        self, mock_gateway: MagicMock
    ) -> None:
        """Verify a 2E04 W echo targets the addressed TCS, not the main one.

        Regression test for https://github.com/ramses-rf/ramses_rf/issues/1284:
        for a W 2E04 the echo's src is the HGI, so resolving the TCS via
        src_dev alone lands the update on gateway.tcs — a mode set on a
        secondary controller must not overwrite the main TCS's system_state.
        """
        # Arrange
        mock_main_tcs = MagicMock()
        mock_second_tcs = MagicMock()

        mock_hgi = MagicMock()
        mock_hgi.id = "18:001234"
        mock_hgi.tcs = None

        mock_ctl = MagicMock()
        mock_ctl.id = "01:150003"
        mock_ctl.type = "01"
        mock_ctl.tcs = mock_second_tcs

        mock_gateway.tcs = mock_main_tcs
        mock_gateway.device_registry.device_by_id = {
            mock_hgi.id: mock_hgi,
            mock_ctl.id: mock_ctl,
        }

        msg = MagicMock()
        msg.src.id = mock_hgi.id
        msg.dst.id = mock_ctl.id
        msg.code = Code._2E04
        msg._has_array = False
        payload = {"system_mode": "away", "until": None}

        # Act
        targets = dispatcher._resolve_logical_targets(
            mock_gateway, msg, payload
        )

        # Assert
        assert mock_second_tcs in targets
        assert mock_main_tcs not in targets

    def test_system_mode_rp_targets_src_controller(
        self, mock_gateway: MagicMock
    ) -> None:
        """Verify a 2E04 RP from a secondary controller targets its own TCS."""
        # Arrange
        mock_main_tcs = MagicMock()
        mock_second_tcs = MagicMock()

        mock_hgi = MagicMock()
        mock_hgi.id = "18:001234"
        mock_hgi.tcs = None

        mock_ctl = MagicMock()
        mock_ctl.id = "01:150003"
        mock_ctl.type = "01"
        mock_ctl.tcs = mock_second_tcs

        mock_gateway.tcs = mock_main_tcs
        mock_gateway.device_registry.device_by_id = {
            mock_hgi.id: mock_hgi,
            mock_ctl.id: mock_ctl,
        }

        msg = MagicMock()
        msg.src.id = mock_ctl.id
        msg.dst.id = mock_hgi.id
        msg.code = Code._2E04
        msg._has_array = False
        payload = {"system_mode": "auto", "until": None}

        # Act
        targets = dispatcher._resolve_logical_targets(
            mock_gateway, msg, payload
        )

        # Assert
        assert mock_second_tcs in targets
        assert mock_main_tcs not in targets


class TestCommandDispatcherSend:
    """Test CommandDispatcher.send returns Message instances consistently."""

    @pytest.mark.asyncio
    async def test_send_with_wait_for_reply_returns_message(
        self, mock_gateway: MagicMock
    ) -> None:
        """Verify send(wait_for_reply=True) returns a Message from ConversationManager."""
        # Arrange
        import asyncio

        from ramses_rf.commands.core import Command
        from ramses_rf.commands.dispatcher import CommandDispatcher
        from ramses_rf.enums import Action

        cmd_dispatcher = CommandDispatcher(mock_gateway)
        intent = Command(
            src=Address("18:000730"),
            dst=Address("01:078710"),
            action=Action.GET_SYSTEM_TIME,
            data={},
        )

        packet = Packet.from_port(
            dt.now(),
            "000 RP --- 01:078710 18:000730 --:------ 313F 009 00000400041C0A07E3",
        )
        expected_msg = Message._from_packet(packet)

        fut: asyncio.Future[Message] = asyncio.Future()
        fut.set_result(expected_msg)

        mock_conv_mgr = MagicMock()
        mock_conv_mgr.find_pending_reply = MagicMock(return_value=None)
        mock_conv_mgr.track_intent = AsyncMock(return_value=fut)
        mock_gateway.conversation_manager = mock_conv_mgr
        mock_gateway._async_send_dto = AsyncMock(return_value=packet)

        # Act
        result = await cmd_dispatcher.send(intent, wait_for_reply=True)

        # Assert
        assert isinstance(result, Message)
        assert result == expected_msg

    @pytest.mark.asyncio
    async def test_send_without_wait_for_reply_returns_message(
        self, mock_gateway: MagicMock
    ) -> None:
        """Verify send(wait_for_reply=False) converts echo Packet to Message."""
        # Arrange
        from ramses_rf.commands.core import Command
        from ramses_rf.commands.dispatcher import CommandDispatcher
        from ramses_rf.enums import Action

        cmd_dispatcher = CommandDispatcher(mock_gateway)
        intent = Command(
            src=Address("18:000730"),
            dst=Address("01:078710"),
            action=Action.GET_SYSTEM_TIME,
            data={},
        )

        packet = Packet.from_port(
            dt.now(),
            "000  I --- 18:000730 01:078710 --:------ 313F 009 00000400041C0A07E3",
        )
        mock_gateway.conversation_manager = None
        mock_gateway._async_send_dto = AsyncMock(return_value=packet)

        # Act
        result = await cmd_dispatcher.send(intent, wait_for_reply=False)

        # Assert
        assert isinstance(result, Message)
        assert result.code == Code._313F

    @pytest.mark.asyncio
    async def test_send_background_creates_task(
        self, mock_gateway: MagicMock
    ) -> None:
        """Verify send_background schedules transmission via event loop task."""
        # Arrange
        import asyncio

        from ramses_rf.commands.core import Command
        from ramses_rf.commands.dispatcher import CommandDispatcher
        from ramses_rf.enums import Action

        cmd_dispatcher = CommandDispatcher(mock_gateway)
        intent = Command(
            src=Address("18:000730"),
            dst=Address("01:078710"),
            action=Action.GET_SYSTEM_TIME,
            data={},
        )
        packet = Packet.from_port(
            dt.now(),
            "000  I --- 18:000730 01:078710 --:------ 313F 009 00000400041C0A07E3",
        )
        mock_gateway.conversation_manager = None
        mock_gateway._loop = asyncio.get_running_loop()
        mock_gateway._async_send_dto = AsyncMock(return_value=packet)
        mock_gateway.add_task = MagicMock()

        # Act
        task = cmd_dispatcher.send_background(intent, wait_for_reply=False)

        # Assert
        assert isinstance(task, asyncio.Task)
        mock_gateway.add_task.assert_called_once_with(task)
        result = await task
        assert isinstance(result, Message)

    @pytest.mark.asyncio
    async def test_send_deprecated_device_raises_protocol_send_failed(
        self, mock_gateway: MagicMock
    ) -> None:
        """Verify sending to a deprecated device raises ProtocolSendFailed."""
        # Arrange
        from ramses_rf.commands.core import Command
        from ramses_rf.commands.dispatcher import CommandDispatcher
        from ramses_rf.enums import Action
        from ramses_tx.exceptions import ProtocolSendFailed

        target_dev = MagicMock()
        target_dev._qos_tx_count = 15
        mock_registry = MagicMock()
        mock_registry.device_by_id = {"01:078710": target_dev}
        mock_gateway.device_registry = mock_registry

        cmd_dispatcher = CommandDispatcher(mock_gateway)
        intent = Command(
            src=Address("18:000730"),
            dst=Address("01:078710"),
            action=Action.GET_SYSTEM_TIME,
            data={},
        )

        # Act & Assert
        with pytest.raises(ProtocolSendFailed, match="Sending deprecated"):
            await cmd_dispatcher.send(intent)

    @pytest.mark.asyncio
    async def test_dispatcher_state_cache_update(
        self, mock_gateway: MagicMock
    ) -> None:
        """Verify process_msg updates state_cache and message_store on valid message ingestion."""
        # Arrange
        mock_gateway.message_store = MagicMock()
        mock_gateway.message_store.add = MagicMock()
        mock_gateway.state_projector = MagicMock()
        mock_gateway._state_cache = {}
        mock_gateway._history_lock = threading.Lock()
        packet = Packet(
            dt.now(),
            "...  I --- 04:189078 --:------ 01:145038 3150 002 0100",
        )
        msg = Message._from_packet(packet)

        # Act
        await dispatcher.process_msg(mock_gateway, msg)

        # Assert
        assert msg.state_header in mock_gateway._state_cache
        mock_gateway.message_store.add.assert_called_once_with(msg)
