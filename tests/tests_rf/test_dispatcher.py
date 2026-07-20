#!/usr/bin/env python3
"""RAMSES RF - Unittests for dispatcher."""

import logging
from collections.abc import Generator
from datetime import datetime as dt, timedelta as td
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ramses_rf import Device, dispatcher
from ramses_rf.const import (
    SZ_BYPASS_POSITION,
    SZ_FAN_MODE,
    SZ_INDOOR_HUMIDITY,
    Code,
    DevType,
)
from ramses_rf.gateway import Gateway, GatewayConfig
from ramses_rf.messages import Message
from ramses_rf.models import HvacState
from ramses_rf.state import MessageStore
from ramses_tx import Address, DeviceIdT, Packet
from ramses_tx.config import EngineConfig
from ramses_tx.const import SZ_REQ_REASON


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

    msg5: Message = Message._from_pkt(
        Packet(
            _NOW + td(seconds=40),
            # heat_demand
            "...  I --- 04:189078 --:------ 01:145038 3150 002 0100",
        )
    )

    msg6: Message = Message._from_pkt(
        Packet(
            _NOW + td(seconds=50),
            # OTB
            "061 RP --- 10:078099 01:087939 --:------ 3220 005 00C0110000",
        )
    )

    @pytest.mark.skip(reason="requires gwy")
    def test_instantiate_devices(self, mock_gateway: MagicMock) -> None:
        """Test device creation from addresses via pipeline stage."""
        dev1 = Device(mock_gateway, Address(DeviceIdT("04:189078")))
        mock_gateway.device_registry.device_by_id.get = MagicMock(return_value=dev1)
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

    def test_detect_array_fragment(self) -> None:
        """Test detection of array fragments."""
        msg1: Message = Message._from_pkt(
            Packet(
                self._NOW,
                "...  I --- 01:158182 --:------ 01:158182 000A 048 001001F40BB8011101F40BB8021101F40BB8031001F40BB8041101F40BB8051101F40BB8061101F40BB8071001F40BB8",
            )
        )
        msg2: Message = Message._from_pkt(
            Packet(
                # delta dtm < 3 secs
                self._NOW + td(seconds=1),
                "...  I --- 01:158182 --:------ 01:158182 000A 006 081001F409C4",
            )
        )
        msg3: Message = Message._from_pkt(
            Packet(
                # delta dtm > 3 secs
                self._NOW + td(seconds=10),
                "...  I --- 01:158182 --:------ 01:158182 000A 006 081001F409C4",
            )
        )
        assert msg1._has_array
        assert dispatcher.detect_array_fragment(msg2, msg1)
        assert not dispatcher.detect_array_fragment(msg3, msg1)


class TestDispatcherErrorHandling:
    """Test Dispatcher exception handling logic."""

    async def test_process_msg_strict_mode(self, mock_gateway: MagicMock) -> None:
        """Test process_msg raises exception in strict mode."""
        # Enable strict mode
        mock_gateway.config.enforce_strict_handling = True

        # Create a message with a valid payload for code 0001
        msg = Message._from_pkt(
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

        msg = Message._from_pkt(
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
        ("pkt_line", "src_id", "dev_type"),
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
        pkt_line: str,
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
        packet = Packet(dtm, pkt_line)
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

        # Inject the mock device into the registry so instantiate_devices
        # maps to it
        mock_gateway.device_registry.device_by_id[src_id] = mock_dev
        mock_gateway.device_registry.get_device.return_value = mock_dev

        # Give the mocked HGI a different ID so the packet is treated as remote
        mock_gateway.hgi.id = "18:000730"

        # 3. Process the message through the dispatcher
        await dispatcher.process_msg(mock_gateway, msg)

        # 4. Assert the message was explicitly dispatched to the device
        # The dispatcher queues the update via
        # gwy._engine._loop.call_soon()
        # which triggers mock_dev._handle_msg(msg) containing the
        # timestamp updates
        mock_gateway._engine._loop.call_soon.assert_any_call(mock_dev._handle_msg, msg)


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

        payload = {SZ_FAN_MODE: "FF", SZ_INDOOR_HUMIDITY: 0.0, SZ_BYPASS_POSITION: None}
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

        payload = {SZ_REQ_REASON: "CO2", SZ_FAN_MODE: "high", SZ_INDOOR_HUMIDITY: 0.55}
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
        return Message._from_pkt(
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

        mock_gateway.device_registry.get_device = MagicMock(side_effect=_get_device)

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
        mock_gateway.device_registry.get_device = MagicMock(return_value=mock_dev)

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
