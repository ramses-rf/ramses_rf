#!/usr/bin/env python3
"""RAMSES RF - Test the Command.put_*, Command.set_* APIs."""

from collections.abc import Callable, Iterable
from datetime import datetime as dt

from ramses_rf.const import SZ_DOMAIN_ID
from ramses_rf.helpers import shrink
from ramses_tx.address import HGI_DEV_ADDR
from ramses_tx.command import Command
from ramses_tx.const import SZ_TIMESTAMP
from ramses_tx.helpers import parse_fault_log_entry
from ramses_tx.message import Message
from ramses_tx.packet import Packet


# NOTE: not used for 0418
def _test_api_good(
    api: Callable, packets: Iterable[str]
) -> None:  # NOTE: incl. addr_set check
    """Test a verb|code pair that has a Command constructor."""

    for pkt_line in packets:
        pkt = _create_pkt_from_frame(pkt_line.split("#")[0].rstrip())
        msg = Message(pkt)

        cmd = _test_api_from_msg(api, msg)
        assert cmd.payload == msg._pkt.payload  # aka pkt.payload

        if isinstance(packets, dict) and (payload := packets[pkt_line]):
            assert shrink(msg.payload, keep_falsys=True) == eval(payload)


def _test_api_fail(
    api: Callable, packets: Iterable[str]
) -> None:  # NOTE: incl. addr_set check
    """Test a verb|code pair that has a Command constructor."""

    for pkt_line in packets:
        pkt = _create_pkt_from_frame(pkt_line.split("#")[0].rstrip())
        msg = Message(pkt)

        try:
            cmd = _test_api_from_msg(api, msg)
        except (AssertionError, TypeError, ValueError):
            cmd = None
        else:
            assert cmd and cmd.payload == msg._pkt.payload  # aka pkt.payload

        if isinstance(packets, dict) and (payload := packets[pkt_line]):
            assert shrink(msg.payload, keep_falsys=True) == eval(payload)


def _create_pkt_from_frame(pkt_line: str) -> Packet:
    """Create a pkt from a pkt_line and assert their frames match."""

    pkt = Packet.from_port(dt.now(), pkt_line)
    assert str(pkt) == pkt_line[4:]
    return pkt


def _test_api_from_msg(api: Callable, msg: Message) -> Command:
    """Create a cmd from a msg and assert their meta-data (doesn"t assert payload.)."""

    cmd: Command = api(
        msg.dst.id, **{k: v for k, v in msg.payload.items() if k[:1] != "_"}
    )

    if msg.src.id == HGI_DEV_ADDR.id:
        assert cmd == msg._pkt  # assert str(cmd) == str(pkt)
    assert cmd.dst.id == msg._pkt.dst.id
    assert cmd.verb == msg._pkt.verb
    assert cmd.code == msg._pkt.code
    # assert cmd.payload == pkt.payload

    return cmd


SET_0004_FAIL = (
    "...  W --- 18:000730 01:145038 --:------ 0004 022 05000000000000000000000000000000000000000000",  # name is None
    "...  W --- 18:000730 01:145038 --:------ 0004 022 05005468697320497320412056657279204C6F6E6720",  # trailing space
)
SET_0004_GOOD = (
    "...  W --- 18:000730 01:145038 --:------ 0004 022 00004D617374657220426564726F6F6D000000000000",
    "...  W --- 18:000730 01:145038 --:------ 0004 022 05005468697320497320412056657279204C6F6E6767",
)


def test_set_0004() -> None:
    _test_api_good(Command.set_zone_name, SET_0004_GOOD)
    _test_api_fail(Command.set_zone_name, SET_0004_FAIL)


SET_000A_GOOD = (
    "...  W --- 18:000730 01:145038 --:------ 000A 006 010001F40DAC",
    "...  W --- 18:000730 01:145038 --:------ 000A 006 031001F409C4",
    "...  W --- 18:000730 01:145038 --:------ 000A 006 050201F40898",
)


def test_set_000a() -> None:
    _test_api_good(Command.set_zone_config, SET_000A_GOOD)


GET_0404_GOOD = {
    "... RQ --- 18:000730 01:076010 --:------ 0404 007 00230008000100": "{'zone_idx': 'HW', 'frag_number': 1, 'total_frags': None}",
    "... RQ --- 18:000730 01:076010 --:------ 0404 007 02200008000100": "{'zone_idx': '02', 'frag_number': 1, 'total_frags': None}",
    "... RQ --- 18:000730 01:076010 --:------ 0404 007 02200008000204": "{'zone_idx': '02', 'frag_number': 2, 'total_frags': 4}",
    "... RQ --- 18:000730 01:076010 --:------ 0404 007 02200008000304": "{'zone_idx': '02', 'frag_number': 3, 'total_frags': 4}",
    "... RQ --- 18:000730 01:076010 --:------ 0404 007 02200008000404": "{'zone_idx': '02', 'frag_number': 4, 'total_frags': 4}",
}


def test_get_0404() -> None:
    _test_api_good(Command.get_schedule_fragment, GET_0404_GOOD)


GET_0418_GOOD = {  # NOTE: this constructor is used only for testing
    "...  I --- 01:145038 --:------ 01:145038 0418 022 000000B0000000000000000000007FFFFF7000000000": "{'log_idx': '00', 'log_entry': None}",
    "...  I --- 01:145038 --:------ 01:145038 0418 022 000000B0060804000000B897A0697FFFFF70001003B6": "{'log_idx': '00', 'log_entry': ('23-11-17T20:03:18', 'fault',      'comms_fault',   'actuator',   '08', '04:000950', 'B0', '0000', 'FFFF7000')}",
}


# NOTE: does not use _test_api_good() as main payload is a tuple, and not a dict
def test_put_0418() -> None:
    for pkt_line in GET_0418_GOOD:
        pkt = _create_pkt_from_frame(pkt_line.split("#")[0].rstrip())
        log_pkt = parse_fault_log_entry(pkt.payload)

        if SZ_TIMESTAMP not in log_pkt:  # ignore null log entries
            continue

        cmd = Command._put_system_log_entry(pkt.src.id, **log_pkt)  # type: ignore[call-arg]
        log_cmd = parse_fault_log_entry(cmd.payload)

        assert log_pkt == log_cmd


SET_1030_GOOD = {  # NOTE: no W|1030 seen in the wild
    "...  W --- 18:000730 01:145038 --:------ 1030 016 01C80137C9010FCA0196CB010FCC0101": "{'zone_idx': '01', 'max_flow_setpoint': 55, 'min_flow_setpoint': 15, 'valve_run_time': 150, 'pump_run_time': 15, 'boolean_cc': 1}",
}


def test_set_1030() -> None:
    _test_api_good(Command.set_mix_valve_params, SET_1030_GOOD)


SET_10A0_GOOD = {  # NOTE: no W|10A0 seen in the wild
    "000  W --- 01:123456 07:031785 --:------ 10A0 006 000F6E050064": "{'dhw_idx': '00', 'setpoint': 39.5, 'overrun': 5, 'differential':  1.0}",
    "000  W --- 01:123456 07:031785 --:------ 10A0 006 000F6E0003E8": "{'dhw_idx': '00', 'setpoint': 39.5, 'overrun': 0, 'differential': 10.0}",
    "000  W --- 01:123456 07:031785 --:------ 10A0 006 0015180301F4": "{'dhw_idx': '00', 'setpoint': 54.0, 'overrun': 3, 'differential':  5.0}",
    "000  W --- 01:123456 07:031785 --:------ 10A0 006 0013240003E8": "{'dhw_idx': '00', 'setpoint': 49.0, 'overrun': 0, 'differential': 10.0}",
    #
    "001  W --- 01:123456 07:031785 --:------ 10A0 006 010F6E050064": "{'dhw_idx': '01', 'setpoint': 39.5, 'overrun': 5, 'differential':  1.0}",
    "001  W --- 01:123456 07:031785 --:------ 10A0 006 010F6E0003E8": "{'dhw_idx': '01', 'setpoint': 39.5, 'overrun': 0, 'differential': 10.0}",
    "001  W --- 01:123456 07:031785 --:------ 10A0 006 0115180301F4": "{'dhw_idx': '01', 'setpoint': 54.0, 'overrun': 3, 'differential':  5.0}",
    "001  W --- 01:123456 07:031785 --:------ 10A0 006 0113240003E8": "{'dhw_idx': '01', 'setpoint': 49.0, 'overrun': 0, 'differential': 10.0}",
}


def test_set_10a0() -> None:
    _test_api_good(Command.set_dhw_params, SET_10A0_GOOD)


SET_1100_FAIL = (
    "...  W --- 01:145038 13:163733 --:------ 1100 008 000C1400007FFF01",  # no domain_id
)
SET_1100_GOOD = {
    "...  W --- 01:145038 13:035462 --:------ 1100 008 00240414007FFF01": "{'domain_id': '00', 'cycle_rate': 9, 'min_on_time':  1.0, 'min_off_time':  5.0, 'proportional_band_width': None}",
    "...  W --- 01:145038 13:163733 --:------ 1100 008 000C14000000C801": "{'domain_id': '00', 'cycle_rate': 3, 'min_on_time':  5.0, 'min_off_time':  0.0, 'proportional_band_width': 2.0}",
    "...  W --- 01:145038 13:163733 --:------ 1100 008 00180400007FFF01": "{'domain_id': '00', 'cycle_rate': 6, 'min_on_time':  1.0, 'min_off_time':  0.0, 'proportional_band_width': None}",
    "...  W --- 01:145038 13:035462 --:------ 1100 008 FC042814007FFF01": "{'domain_id': 'FC', 'cycle_rate': 1, 'min_on_time': 10.0, 'min_off_time':  5.0, 'proportional_band_width': None}",
    "...  W --- 01:145038 13:035462 --:------ 1100 008 FC082814007FFF01": "{'domain_id': 'FC', 'cycle_rate': 2, 'min_on_time': 10.0, 'min_off_time':  5.0, 'proportional_band_width': None}",
    "...  W --- 01:145038 13:035462 --:------ 1100 008 FC243C14007FFF01": "{'domain_id': 'FC', 'cycle_rate': 9, 'min_on_time': 15.0, 'min_off_time':  5.0, 'proportional_band_width': None}",
    "...  W --- 01:145038 13:035462 --:------ 1100 008 FC240414007FFF01": "{'domain_id': 'FC', 'cycle_rate': 9, 'min_on_time':  1.0, 'min_off_time':  5.0, 'proportional_band_width': None}",
    "...  W --- 01:145038 13:035462 --:------ 1100 008 FC240428007FFF01": "{'domain_id': 'FC', 'cycle_rate': 9, 'min_on_time':  1.0, 'min_off_time': 10.0, 'proportional_band_width': None}",
    "...  W --- 01:145038 13:035462 --:------ 1100 008 FC083C14007FFF01": "{'domain_id': 'FC', 'cycle_rate': 2, 'min_on_time': 15.0, 'min_off_time':  5.0, 'proportional_band_width': None}",
    "...  W --- 01:145038 13:035462 --:------ 1100 008 FC083C00007FFF01": "{'domain_id': 'FC', 'cycle_rate': 2, 'min_on_time': 15.0, 'min_off_time':  0.0, 'proportional_band_width': None}",
}


def test_set_1100() -> None:  # NOTE: bespoke: see params
    packets = SET_1100_GOOD

    for pkt_line in packets:
        pkt = _create_pkt_from_frame(pkt_line)
        msg = Message(pkt)

        msg.payload[SZ_DOMAIN_ID] = msg.payload.get(SZ_DOMAIN_ID, "00")

        cmd = _test_api_from_msg(Command.set_tpi_params, msg)
        assert cmd.payload == msg._pkt.payload

        if isinstance(packets, dict) and (payload := packets[pkt_line]):
            assert shrink(msg.payload, keep_falsys=True) == eval(payload)


PUT_1260_GOOD = {  # TODO: RPs being converted to Is
    "...  I --- 07:017494 --:------ 07:017494 1260 003 00111E": "{'temperature': 43.82}",
    "...  I --- 07:017494 --:------ 07:017494 1260 003 007FFF": "{'temperature': None}",
    # "...  I --- 07:123456 --:------ 07:123456 1260 003 010E74": "{'temperature': 37.0, 'dhw_idx': '01'}",  #  contrived
    # "...  I --- 07:123456 --:------ 07:123456 1260 003 017FFF": "{'temperature': None}",  #                   contrived
    # "... RP --- 01:123456 18:123456 --:------ 1260 003 00116A": "{'temperature': 44.58}",
    # "... RP --- 01:078710 18:002563 --:------ 1260 003 00116A": "{'temperature': 44.58, 'dhw_idx': '00'}",
    # "... RP --- 01:078710 18:002563 --:------ 1260 003 01116A": "{'temperature': 44.58, 'dhw_idx': '01'}",  # contrived
    # "... RP --- 10:124973 18:132629 --:------ 1260 003 000E74": "{'temperature': 37.0}",
}


def test_set_1260() -> None:
    _test_api_good(Command.put_dhw_temp, PUT_1260_GOOD)


SET_1F41_GOOD = {
    # 00  W --- 18:000730 01:050858 --:------ 1F41 006 000000FFFFFF            ": "{'dhw_idx': '00', 'mode': 'follow_schedule'}",
    # 00  W --- 18:000730 01:050858 --:------ 1F41 006 000100FFFFFF            ": "{'dhw_idx': '00', 'mode': 'follow_schedule'}",
    "000  W --- 18:000730 01:050858 --:------ 1F41 006 00FF00FFFFFF            ": "{'dhw_idx': '00', 'mode': 'follow_schedule'}",
    "000  W --- 18:000730 01:050858 --:------ 1F41 006 000102FFFFFF            ": "{'dhw_idx': '00', 'mode': 'permanent_override', 'active': 1}",
    "000  W --- 18:000730 01:050858 --:------ 1F41 012 000004FFFFFF0509160607E5": "{'dhw_idx': '00', 'mode': 'temporary_override', 'active': 0, 'until': '2021-06-22T09:05:00'}",
    "000  W --- 18:000730 01:050858 --:------ 1F41 012 000104FFFFFF2F0E0D0B07E5": "{'dhw_idx': '00', 'mode': 'temporary_override', 'active': 1, 'until': '2021-11-13T14:47:00'}",
    #
    # 01  W --- 18:000730 01:050858 --:------ 1F41 006 010000FFFFFF            ": "{'dhw_idx': '01', 'mode': 'follow_schedule'}",
    # 01  W --- 18:000730 01:050858 --:------ 1F41 006 010100FFFFFF            ": "{'dhw_idx': '01', 'mode': 'follow_schedule'}",
    "001  W --- 18:000730 01:050858 --:------ 1F41 006 01FF00FFFFFF            ": "{'dhw_idx': '01', 'mode': 'follow_schedule'}",
    "001  W --- 18:000730 01:050858 --:------ 1F41 006 010102FFFFFF            ": "{'dhw_idx': '01', 'mode': 'permanent_override', 'active': 1}",
    "001  W --- 18:000730 01:050858 --:------ 1F41 012 010004FFFFFF0509160607E5": "{'dhw_idx': '01', 'mode': 'temporary_override', 'active': 0, 'until': '2021-06-22T09:05:00'}",
    "001  W --- 18:000730 01:050858 --:------ 1F41 012 010104FFFFFF2F0E0D0B07E5": "{'dhw_idx': '01', 'mode': 'temporary_override', 'active': 1, 'until': '2021-11-13T14:47:00'}",
}  # TODO: add other modes
SET_1F41_FAIL = (
    "000  W --- 18:000730 01:050858 --:------ 1F41 006 020000FFFFFF",  # dhw_idx = 02
    "000  W --- 18:000730 01:050858 --:------ 1F41 006 000005FFFFFF",  # zone_mode = 05
    "000  W --- 18:000730 01:050858 --:------ 1F41 006 000005FFFFFF",  # zone_mode = 05
)


def test_set_1f41() -> None:
    _test_api_good(Command.set_dhw_mode, SET_1F41_GOOD)


SET_2309_FAIL = (
    "...  W --- 18:000730 01:145038 --:------ 2309 003 017FFF",  # temp is None - should be good?
)
SET_2309_GOOD = (
    "...  W --- 18:000730 01:145038 --:------ 2309 003 00047E",
    "...  W --- 18:000730 01:145038 --:------ 2309 003 0101F4",
)


def test_set_2309() -> None:
    _test_api_good(Command.set_zone_setpoint, SET_2309_GOOD)


SET_2349_GOOD = (
    "...  W --- 18:005567 01:223036 --:------ 2349 007 037FFF00FFFFFF",
    "...  W --- 22:015492 01:076010 --:------ 2349 007 0101F400FFFFFF",
    "...  W --- 18:000730 01:145038 --:------ 2349 007 06028A01FFFFFF",
    "...  W --- 22:081652 01:063844 --:------ 2349 007 0106400300003C",
    "...  W --- 18:000730 01:050858 --:------ 2349 013 06096004FFFFFF240A050107E6",
    "...  W --- 18:000730 01:050858 --:------ 2349 013 02096004FFFFFF1B0D050107E6",
)


def test_set_2349() -> None:
    _test_api_good(Command.set_zone_mode, SET_2349_GOOD)


SET_2E04_GOOD = {
    "...  W --- 30:258720 01:073976 --:------ 2E04 008 00FFFFFFFFFFFF00": "{'system_mode': 'auto'}",
    "...  W --- 30:258720 01:073976 --:------ 2E04 008 01FFFFFFFFFFFF00": "{'system_mode': 'heat_off'}",
    "...  W --- 30:258720 01:073976 --:------ 2E04 008 06FFFFFFFFFFFF00": "{'system_mode': 'auto_with_reset'}",
    #
    "...  W --- 30:258720 01:073976 --:------ 2E04 008 03FFFFFFFFFFFF00": "{'system_mode': 'away',            'until': None}",
    "...  W --- 30:258720 01:073976 --:------ 2E04 008 0300001D0A07E301": "{'system_mode': 'away',            'until': '2019-10-29T00:00:00'}",
    "...  W --- 30:258720 01:073976 --:------ 2E04 008 07FFFFFFFFFFFF00": "{'system_mode': 'custom',          'until': None}",
    "...  W --- 30:258720 01:073976 --:------ 2E04 008 0700001D0A07E301": "{'system_mode': 'custom',          'until': '2019-10-29T00:00:00'}",
    "...  W --- 30:258720 01:073976 --:------ 2E04 008 02FFFFFFFFFFFF00": "{'system_mode': 'eco_boost',       'until': None}",
    "...  W --- 30:258720 01:073976 --:------ 2E04 008 020B011A0607E401": "{'system_mode': 'eco_boost',       'until': '2020-06-26T01:11:00'}",
    "...  W --- 30:258720 01:073976 --:------ 2E04 008 04FFFFFFFFFFFF00": "{'system_mode': 'day_off',         'until': None}",
    "...  W --- 30:258720 01:073976 --:------ 2E04 008 0400001D0A07E301": "{'system_mode': 'day_off',         'until': '2019-10-29T00:00:00'}",
    "...  W --- 30:258720 01:073976 --:------ 2E04 008 05FFFFFFFFFFFF00": "{'system_mode': 'day_off_eco',     'until': None}",
    "...  W --- 30:258720 01:073976 --:------ 2E04 008 0500001D0A07E301": "{'system_mode': 'day_off_eco',     'until': '2019-10-29T00:00:00'}",
    "...  W --- 30:258720 01:073976 --:------ 2E04 008 0521011A0607E401": "{'system_mode': 'day_off_eco',     'until': '2020-06-26T01:33:00'}",  # a contrived time, usu. 00:00
}


def test_set_2e04() -> None:
    _test_api_good(Command.set_system_mode, SET_2E04_GOOD)


PUT_30C9_FAIL = (
    "...  I --- 13:074756 --:------ 13:074756 30C9 003 007FFF",
    "...  I --- 01:197498 --:------ 01:197498 30C9 024 01086D02087003086604070A0508DF06083307083008085C",
)
PUT_30C9_GOOD = (
    "...  I --- 04:068997 --:------ 04:068997 30C9 003 007FFF",
    "...  I --- 04:068997 --:------ 04:068997 30C9 003 000838",
    "...  I --- 03:123456 --:------ 03:123456 30C9 003 0007C1",
    "...  I --- 03:123456 --:------ 03:123456 30C9 003 007FFF",
    "...  I --- 13:074756 --:------ 03:074756 30C9 003 00086D",
)


def test_put_30c9() -> None:
    _test_api_good(Command.put_sensor_temp, PUT_30C9_GOOD)


SET_313F_GOOD = (
    "...  W --- 30:258720 01:073976 --:------ 313F 009 006000320C040207E6",
    "...  W --- 30:258720 01:073976 --:------ 313F 009 0060011E09010707E6",
    "...  W --- 30:258720 01:073976 --:------ 313F 009 006002210D080C07E5",
    "...  W --- 30:042165 01:076010 --:------ 313F 009 006003090A0D0207E6",
    "...  W --- 30:042165 01:076010 --:------ 313F 009 0060041210040207E6",
)


def test_set_313f() -> None:  # NOTE: bespoke: payload
    for pkt_line in SET_313F_GOOD:
        pkt = Packet.from_port(dt.now(), pkt_line)
        assert str(pkt)[:4] == pkt_line[4:8]
        assert str(pkt)[6:] == pkt_line[10:]

        msg = Message(pkt)

        cmd = _test_api_from_msg(Command.set_system_time, msg)
        assert cmd.payload[:4] == msg._pkt.payload[:4]
        assert cmd.payload[6:] == msg._pkt.payload[6:]


PUT_3EF0_FAIL = ("...  I --- 13:123456 --:------ 13:123456 3EF0 003 00AAFF",)
PUT_3EF0_GOOD = (
    "...  I --- 13:123456 --:------ 13:123456 3EF0 003 0000FF",
    "...  I --- 13:123456 --:------ 13:123456 3EF0 003 00C8FF",
)


def test_put_3ef0() -> None:
    _test_api_good(Command.put_actuator_state, PUT_3EF0_GOOD)


PUT_3EF1_GOOD = (  # TODO: needs checking
    "... RP --- 13:123456 01:123456 --:------ 3EF1 007 000126012600FF",
    "... RP --- 13:123456 18:123456 --:------ 3EF1 007 007FFF003C0010",  # NOTE: should be: RP|10|3EF1
)


def test_put_3ef1() -> None:  # NOTE: bespoke: params, ?payload
    for pkt_line in PUT_3EF1_GOOD:
        pkt = _create_pkt_from_frame(pkt_line)
        msg = Message(pkt)

        kwargs = msg.payload
        modulation_level = kwargs.pop("modulation_level")
        actuator_countdown = kwargs.pop("actuator_countdown")

        cmd = Command.put_actuator_cycle(
            msg.src.id,
            msg.dst.id,
            modulation_level,
            actuator_countdown,
            **{k: v for k, v in kwargs.items() if k[:1] != "_"},
        )

        if msg.src.id != HGI_DEV_ADDR.id:
            assert cmd.src.id == pkt.src.id
        assert cmd.dst.id == pkt.dst.id
        assert cmd.verb == pkt.verb
        assert cmd.code == pkt.code

        assert cmd.payload[:-2] == pkt.payload[:-2]
