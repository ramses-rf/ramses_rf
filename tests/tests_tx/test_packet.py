#!/usr/bin/env python3
"""Test the Packet class and its exposed attributes, including lifespan and parsing."""

from datetime import datetime as dt, timedelta as td

import pytest

from ramses_rf.pipeline.lifespan import pkt_lifespan
from ramses_tx.exceptions import PacketInvalid
from ramses_tx.packet import Packet

# Constants for testing frames
DTM = dt(2023, 1, 1, 12, 0, 0)
VALID_FRAME_I = "045  I --- 01:145038 --:------ 01:145038 1F09 003 0004B5"
VALID_FRAME_RQ = "095 RQ --- 18:006402 13:049798 --:------ 1FC9 001 00"


class MockCommand:
    """A mock command to test the Packet._from_cmd constructor."""

    def __init__(self) -> None:
        """Initialize the mock command."""
        self.verb = " I"
        self.addr1 = "01:145038"
        self.addr2 = "--:------"
        self.addr3 = "01:145038"
        self.code = "1F09"
        self.payload = "0004B5"
        self._frame = " I --- 01:145038 --:------ 01:145038 1F09 003 0004B5"


def test_packet_properties() -> None:
    """Test that Packet initializes correctly and exposes properties.

    :return: None
    """
    packet = Packet(DTM, VALID_FRAME_I, comment="A test comment")

    assert packet.dtm == DTM
    assert packet.rssi == "045"
    assert packet.comment == "A test comment"
    assert packet.error_text == ""
    assert packet.verb == " I"
    assert packet.code == "1F09"


def test_packet_partitioning() -> None:
    """Test the static _partition method for log line splitting.

    :return: None
    """
    raw_line = (
        "045  I --- 01:145038 --:------ 01:145038 1F09 003 0004B5 "
        "< hint * error # comment"
    )

    # _partition returns a map object, so we convert to a tuple to assert
    fragment, err_msg, comment = tuple(Packet._partition(raw_line))

    assert fragment == "045  I --- 01:145038 --:------ 01:145038 1F09 003 0004B5"
    assert err_msg == "error"
    assert comment == "comment"


def test_packet_validation_errors() -> None:
    """Test that invalid packets raise PacketInvalid.

    :return: None
    """
    with pytest.raises(PacketInvalid, match="Custom error"):
        Packet(DTM, VALID_FRAME_I, err_msg="Custom error")

    with pytest.raises(PacketInvalid, match="Null packet"):
        # Frame is sliced by 4:, so a frame of length < 4 is effectively empty.
        # This will now successfully trigger our newly added intercept in packet.py.
        Packet(DTM, "   ", comment="Should fail")


def test_packet_constructors() -> None:
    """Test the alternate classmethod constructors.

    :return: None
    """
    dtm_str = DTM.isoformat()

    # Test from_dict with legacy string (backward compatibility)
    pkt_dict = Packet.from_dict(dtm_str, f"{VALID_FRAME_I} # my comment")
    assert pkt_dict.dtm == DTM
    assert pkt_dict.rssi == "045"
    assert pkt_dict.comment == "my comment"

    # Test canonical from_raw_line factory
    pkt_line = Packet.from_raw_line(DTM, VALID_FRAME_I)
    assert pkt_line.rssi == "045"
    assert pkt_line.verb == " I"

    # Test from_file (delegates to from_raw_line)
    pkt_file_valid = Packet.from_file(dtm_str, VALID_FRAME_I)
    assert pkt_file_valid.rssi == "045"
    assert pkt_file_valid.verb == " I"

    # Test from_port (delegates to from_raw_line)
    pkt_port = Packet.from_port(DTM, VALID_FRAME_I)
    assert pkt_port.rssi == "045"

    # Test _from_cmd
    cmd = MockCommand()
    pkt_cmd = Packet._from_cmd(cmd, DTM)

    # _from_cmd prepends "... " to the frame, simulating a blank RSSI from a command
    assert pkt_cmd.rssi == "..."
    assert pkt_cmd.verb == " I"


def test_packet_dto_serialization() -> None:
    """Test Packet DTO serialization and structured dictionary ingestion.

    :return: None
    """
    pkt = Packet(DTM, VALID_FRAME_I, comment="1060| I|01:145038")

    # 1. Test to_dict (Serialization)
    pkt_dict = pkt.to_dict()

    # Calculate the expected timezone-aware string dynamically to pass on any system
    expected_dtm = DTM.astimezone().isoformat(timespec="microseconds")
    assert pkt_dict["dtm"] == expected_dtm

    assert pkt_dict["verb"] == " I"
    assert pkt_dict["code"] == "1F09"
    assert (
        pkt_dict["rssi"] == 45
    )  # Intentionally mapped to int for legacy downstream compatibility
    assert pkt_dict["frame"] == " I --- 01:145038 --:------ 01:145038 1F09 003 0004B5"

    # Check Address resolution strictly maps to the primitive DTO strings
    assert pkt_dict["addr1"] == "01:145038"
    assert pkt_dict["addr2"] == "--:------"
    assert pkt_dict["addr3"] == "01:145038"

    # 2. Test from_dict with a structured dictionary (Deserialization)
    restored_pkt = Packet.from_dict(pkt_dict["dtm"], pkt_dict)

    assert restored_pkt.dtm == DTM.astimezone()
    assert restored_pkt.rssi == "045"  # Automatically padded back to 3 chars
    assert restored_pkt.verb == " I"
    assert restored_pkt._frame == pkt._frame


def test_to_json_from_json_parity() -> None:
    """Test serializing a Packet to JSON via orjson and deserializing it back."""
    pkt = Packet.from_raw_line(DTM, VALID_FRAME_I)

    # 1. Test to_json (Serialization to bytes)
    json_bytes = pkt.to_json()
    assert isinstance(json_bytes, bytes)

    # 2. Test from_json (Deserialization back to Packet)
    restored_pkt = Packet.from_json(json_bytes)
    assert restored_pkt.dtm == DTM.astimezone()
    assert restored_pkt.rssi == "045"
    assert restored_pkt.verb == " I"
    assert restored_pkt._frame == pkt._frame


def test_from_dict_raw_packet_dict_format() -> None:
    """Test from_dict with a RawPacket.__dict__ (as used by lifecycle.get_state).

    Regression test for ramses_cc issue 812: cache restore produced
    'Bad frame: Invalid structure: >>><<<' because RawPacket.__dict__ has
    'raw_packet' (not 'frame') and rssi can be a non-numeric string like '...'.
    """
    dtm_str = DTM.isoformat()

    # Simulate what lifecycle.get_state saves: msg.dto.source_packets[0].__dict__
    raw_pkt_dict: dict[str, object] = {
        "raw_packet": " I --- 01:145038 --:------ 01:145038 1F09 003 0004B5",
        "rssi": "045",
        "verb": " I",
        "seq": "---",
        "device_id_1": "01:145038",
        "device_id_2": "--:------",
        "device_id_3": "01:145038",
        "code": "1F09",
        "payload_len": "003",
        "payload": "0004B5",
    }

    pkt = Packet.from_dict(dtm_str, raw_pkt_dict)
    assert pkt.rssi == "045"
    assert pkt.verb == " I"
    assert pkt.code == "1F09"
    assert pkt._frame == " I --- 01:145038 --:------ 01:145038 1F09 003 0004B5"

    # Also test with non-numeric rssi (e.g. "..." from file-based packets)
    raw_pkt_dict["rssi"] = "..."
    pkt2 = Packet.from_dict(dtm_str, raw_pkt_dict)
    assert pkt2.rssi == "..."
    assert pkt2.code == "1F09"


def test_pkt_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test the packet lifespan calculation logic.

    :param monkeypatch: The pytest monkeypatch fixture.
    :return: None
    """
    # RQ packets should have 0 lifespan
    pkt_rq = Packet(DTM, VALID_FRAME_RQ)
    assert pkt_lifespan(pkt_rq) == td(seconds=0)

    # 1F09 ' I' packet has 360 seconds
    pkt_1f09 = Packet(DTM, VALID_FRAME_I)
    assert pkt_lifespan(pkt_1f09) == td(seconds=360)

    # Force an array scenario for 000A logic paths
    valid_000a = "045  I --- 01:145038 --:------ 01:145038 000A 006 001122334455"
    pkt_000a = Packet(DTM, valid_000a)

    # The internal property cache was removed in Phase 3.1; length logic applies natively
    assert pkt_lifespan(pkt_000a) == td(minutes=60)


def test_packet_representations() -> None:
    """Test the string and repr outputs of the Packet class.

    :return: None
    """
    packet = Packet(DTM, VALID_FRAME_I)

    # The repr should output the time, the original frame components and the header context
    repr_str = repr(packet)
    assert "2023-01-01T12:00:00.000000" in repr_str
    assert "1F09" in repr_str

    # __str__ simply delegates to super().__repr__() which outputs just the formatted frame
    assert str(packet) == " I --- 01:145038 --:------ 01:145038 1F09 003 0004B5"


def test_packet_heartbeat_payload_bypass() -> None:
    """Test that 1-byte '00' heartbeat payloads bypass strict validation.

    :return: None
    """
    # A 3150 heat demand packet normally requires a 2-byte payload.
    # We pass a 1-byte "00" payload, which should be intercepted and allowed.
    heartbeat_frame = "045  I --- 04:123456 --:------ 04:123456 3150 001 00"

    # This should instantiate successfully without throwing PacketPayloadInvalid!
    packet = Packet(DTM, heartbeat_frame)

    assert getattr(packet, "_len", 0) == 1
    assert getattr(packet, "payload", "") == "00"

    # As explicitly designed to preserve legacy test suite behavior, _has_payload remains
    # False (as it fails the strict regex), but the packet instantiation survives.
    assert packet._has_payload is False
