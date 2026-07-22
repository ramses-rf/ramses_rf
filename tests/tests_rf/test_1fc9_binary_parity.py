"""Unit and parity tests for 1FC9 binary parsing vs legacy string-slicing parser."""

from unittest.mock import MagicMock

from ramses_rf.const import I_, SZ_PHASE, W_, Code
from ramses_rf.messages import Message
from ramses_rf.models import TopologyChangedEvent
from ramses_rf.parsers.system import parser_1fc9
from ramses_rf.pipeline.topology_builder import TopologyBuilder
from ramses_tx.address import Address
from ramses_tx.const import SZ_BINDINGS


def test_1fc9_binary_parsing_parity_with_legacy_parser() -> None:
    # Arrange
    test_cases = [
        (
            "FC0008053376FC3150053376FB3150053376FC1FC9053376",
            I_,
            "01:078710",
            "01:078710",
            "offer",
        ),
        (
            "003EF0290693",
            W_,
            "10:067219",
            "01:078710",
            "accept",
        ),
        (
            "00FFFF053376",
            I_,
            "01:078710",
            "10:067219",
            "confirm",
        ),
        (
            "FA000806368EFC3B0006368EFA1FC906368E",
            I_,
            "01:145038",
            "63:262142",
            "offer",
        ),
    ]

    for payload_hex, verb, src_id, dst_id, expected_phase in test_cases:
        # Act 1: Legacy parser execution
        mock_msg = MagicMock(spec=Message)
        mock_msg.verb = verb
        mock_msg.src = Address(src_id)
        mock_msg.dst = Address(dst_id)
        mock_msg.len = len(payload_hex) // 2

        legacy_result = parser_1fc9(payload_hex, mock_msg)

        # Act 2: New Binary Parser execution in TopologyBuilder
        events: list[TopologyChangedEvent] = []
        builder = TopologyBuilder(emit_event_cb=events.append, enable_eavesdrop=True)

        mock_pkt = MagicMock()
        mock_pkt.payload = payload_hex

        mock_header = MagicMock()
        mock_header.code = Code._1FC9
        mock_header.verb = verb

        topology_msg = MagicMock(spec=Message)
        topology_msg.header = mock_header
        topology_msg.src = Address(src_id)
        topology_msg.dst = Address(dst_id)
        topology_msg._pkt = mock_pkt

        builder._evaluate_rf_bind_rules(topology_msg)

        # Assert
        assert legacy_result[SZ_PHASE] == expected_phase

        legacy_bindings = legacy_result[SZ_BINDINGS]
        bind_events = [e for e in events if e.action.name == "BIND_DEVICE"]

        assert len(bind_events) == len(legacy_bindings)

        for legacy_b, event in zip(legacy_bindings, bind_events, strict=True):
            exp_domain, exp_opcode, exp_dev_id = legacy_b
            assert event.metadata["domain_id"] == exp_domain
            assert event.metadata["opcode"] == exp_opcode
            assert event.metadata["phase"] == expected_phase
            assert event.child_id == exp_dev_id or event.parent_id == exp_dev_id
