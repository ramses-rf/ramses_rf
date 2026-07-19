#!/usr/bin/env python3
"""RAMSES RF - Test the Command.put_*, Command.set_* APIs."""

import inspect
from collections.abc import Callable, Iterable
from datetime import datetime as dt
from typing import Any

from ramses_rf.address import Address
from ramses_rf.commands.builders import build_dto
from ramses_rf.commands.core import Command as Intent
from ramses_rf.const import SZ_DOMAIN_ID
from ramses_rf.enums import Action
from ramses_rf.helpers import shrink
from ramses_rf.messages import Message
from ramses_tx.address import HGI_DEV_ADDR
from ramses_tx.command import Command
from ramses_tx.command_legacy_shim import LegacyCommandShim
from ramses_tx.const import SZ_TIMESTAMP
from ramses_tx.helpers import parse_fault_log_entry
from ramses_tx.packet import Packet
from ramses_tx.typing import DeviceIdT


def _get_schedule_fragment(
    ctl_id: DeviceIdT | str,
    zone_idx: int | str,
    frag_number: int,
    total_frags: int | None,
    **kwargs: Any,
) -> Command:
    return LegacyCommandShim.from_dto(
        build_dto(
            Intent(
                src=HGI_DEV_ADDR,
                dst=Address(ctl_id),
                action=Action.GET_SCHEDULE_FRAGMENT,
                data={
                    "zone_idx": zone_idx,
                    "frag_number": frag_number,
                    "total_frags": total_frags if total_frags is not None else 0,
                },
            )
        )
    )


def _put_system_log_entry(
    ctl_id: DeviceIdT | str,
    fault_state: str,
    fault_type: str,
    device_class: str,
    device_id: DeviceIdT | str | None = None,
    domain_idx: int | str = "00",
    _log_idx: int | str | None = None,
    timestamp: dt | str | None = None,
    **kwargs: Any,
) -> Command:
    return LegacyCommandShim.from_dto(
        build_dto(
            Intent(
                src=HGI_DEV_ADDR,
                dst=Address(ctl_id),
                action=Action.PUT_FAULTLOG_ENTRY,
                data={
                    "fault_state": fault_state,
                    "fault_type": fault_type,
                    "device_class": device_class,
                    "device_id": device_id,
                    "domain_idx": domain_idx,
                    "log_idx": _log_idx,
                    "timestamp": timestamp,
                },
            )
        )
    )


def _set_mix_valve_params(
    ctl_id: DeviceIdT | str,
    zone_idx: int | str,
    *,
    max_flow_setpoint: int = 55,
    min_flow_setpoint: int = 15,
    valve_run_time: int = 150,
    pump_run_time: int = 15,
    **kwargs: Any,
) -> Command:
    return LegacyCommandShim.from_dto(
        build_dto(
            Intent(
                src=HGI_DEV_ADDR,
                dst=Address(ctl_id),
                action=Action.SET_MIX_VALVE_PARAMS,
                data={
                    "zone_idx": zone_idx,
                    "max_flow_setpoint": max_flow_setpoint,
                    "min_flow_setpoint": min_flow_setpoint,
                    "valve_run_time": valve_run_time,
                    "pump_run_time": pump_run_time,
                    "boolean_cc": kwargs.pop("boolean_cc", 1),
                },
            )
        )
    )


def _set_tpi_params(
    ctl_id: DeviceIdT | str,
    domain_id: int | str | None,
    *,
    cycle_rate: int = 3,
    min_on_time: int = 5,
    min_off_time: int = 5,
    proportional_band_width: float | None = None,
) -> Command:
    return LegacyCommandShim.from_dto(
        build_dto(
            Intent(
                src=HGI_DEV_ADDR,
                dst=Address(ctl_id),
                action=Action.SET_TPI_PARAMS,
                data={
                    "domain_id": domain_id or "00",
                    "cycle_rate": cycle_rate,
                    "min_on_time": min_on_time,
                    "min_off_time": min_off_time,
                    "proportional_band_width": proportional_band_width,
                },
            )
        )
    )


def _set_system_mode(
    ctl_id: DeviceIdT | str,
    system_mode: int | str | None,
    *,
    until: dt | str | None = None,
) -> Command:
    return LegacyCommandShim.from_dto(
        build_dto(
            Intent(
                src=HGI_DEV_ADDR,
                dst=Address(ctl_id),
                action=Action.SET_SYSTEM_MODE,
                data={
                    "system_mode": system_mode,
                    "until": until,
                },
            )
        )
    )


def _set_system_time(
    ctl_id: DeviceIdT | str,
    datetime: dt | str,
    is_dst: bool = False,
) -> Command:
    return LegacyCommandShim.from_dto(
        build_dto(
            Intent(
                src=HGI_DEV_ADDR,
                dst=Address(ctl_id),
                action=Action.SET_SYSTEM_TIME,
                data={
                    "datetime": datetime,
                    "is_dst": is_dst,
                },
            )
        )
    )


def _put_actuator_state(
    dev_id: DeviceIdT | str,
    modulation_level: float,
) -> Command:
    return LegacyCommandShim.from_dto(
        build_dto(
            Intent(
                src=Address(dev_id),
                dst=Address(dev_id),
                action=Action.PUT_ACTUATOR_STATE,
                data={
                    "modulation_level": modulation_level,
                },
            )
        )
    )


def _put_actuator_cycle(
    src_id: DeviceIdT | str,
    dst_id: DeviceIdT | str,
    modulation_level: float,
    actuator_countdown: int,
    *,
    cycle_countdown: int | None = None,
) -> Command:
    return LegacyCommandShim.from_dto(
        build_dto(
            Intent(
                src=Address(src_id),
                dst=Address(dst_id),
                action=Action.PUT_ACTUATOR_CYCLE,
                data={
                    "modulation_level": modulation_level,
                    "actuator_countdown": actuator_countdown,
                    "cycle_countdown": cycle_countdown,
                },
            )
        )
    )


# NOTE: not used for 0418
def _test_api_good(
    api: Callable, packets: Iterable[str]
) -> None:  # NOTE: incl. addr_set check
    """Test a verb|code pair that has a Command constructor."""

    for pkt_line in packets:
        pkt = _create_pkt_from_frame(pkt_line.split("#")[0].rstrip())
        msg = Message._from_pkt(pkt)

        cmd = _test_api_from_msg(api, msg, pkt)
        assert cmd.payload == pkt.payload  # aka pkt.payload

        if isinstance(packets, dict) and (payload := packets[pkt_line]):
            assert shrink(msg.payload, keep_falsys=True) == eval(payload)


def _test_api_fail(
    api: Callable, packets: Iterable[str]
) -> None:  # NOTE: incl. addr_set check
    """Test a verb|code pair that has a Command constructor."""

    for pkt_line in packets:
        pkt = _create_pkt_from_frame(pkt_line.split("#")[0].rstrip())
        msg = Message._from_pkt(pkt)

        try:
            cmd = _test_api_from_msg(api, msg, pkt)
        except (AssertionError, TypeError, ValueError):
            cmd = None
        else:
            assert cmd and cmd.payload == pkt.payload  # aka pkt.payload

        if isinstance(packets, dict) and (payload := packets[pkt_line]):
            assert shrink(msg.payload, keep_falsys=True) == eval(payload)


def _create_pkt_from_frame(pkt_line: str) -> Packet:
    """Create a pkt from a pkt_line and assert their frames match."""

    pkt = Packet.from_port(dt.now(), pkt_line)
    assert str(pkt) == pkt_line[4:]
    return pkt


def _test_api_from_msg(api: Callable, msg: Message, pkt: Packet) -> Command:
    """Create a cmd from a msg and assert their meta-data."""

    sig = inspect.signature(api)
    kwargs = {k: v for k, v in msg.payload.items() if k[:1] != "_"}

    has_varkw = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    if not has_varkw:
        kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}

    cmd: Command = api(msg.dst.id, **kwargs)

    if msg.src.id == HGI_DEV_ADDR.id:
        # assert str(cmd) == str(pkt)
        assert cmd._frame == pkt._frame
    assert cmd.dst.id == pkt.dst.id
    assert cmd.verb == pkt.verb
    assert cmd.code == pkt.code
    # assert cmd.payload == pkt.payload

    return cmd


GET_0404_GOOD = {
    "... RQ --- 18:000730 01:076010 --:------ 0404 007 00230008000100": "{'zone_idx': 'HW', 'frag_number': 1, 'total_frags': None}",
    "... RQ --- 18:000730 01:076010 --:------ 0404 007 02200008000100": "{'zone_idx': '02', 'frag_number': 1, 'total_frags': None}",
    "... RQ --- 18:000730 01:076010 --:------ 0404 007 02200008000204": "{'zone_idx': '02', 'frag_number': 2, 'total_frags': 4}",
    "... RQ --- 18:000730 01:076010 --:------ 0404 007 02200008000304": "{'zone_idx': '02', 'frag_number': 3, 'total_frags': 4}",
    "... RQ --- 18:000730 01:076010 --:------ 0404 007 02200008000404": "{'zone_idx': '02', 'frag_number': 4, 'total_frags': 4}",
}


def test_get_0404() -> None:
    _test_api_good(_get_schedule_fragment, GET_0404_GOOD)


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

        cmd = _put_system_log_entry(
            pkt.src.id,
            **log_pkt,  # type: ignore[call-arg]
        )
        log_cmd = parse_fault_log_entry(cmd.payload)

        assert log_pkt == log_cmd


# NOTE: no W|1030 seen in the wild
SET_1030_GOOD = {
    (
        "...  W --- 18:000730 01:145038 --:------ 1030 016 01C80137C9010FCA0196CB010FCC0101"
    ): (
        "{'zone_idx': '01', 'max_flow_setpoint': 55, 'min_flow_setpoint': 15, "
        "'valve_run_time': 150, 'pump_run_time': 15, 'boolean_cc': 1}"
    ),
}


def test_set_1030() -> None:
    _test_api_good(_set_mix_valve_params, SET_1030_GOOD)


# NOTE: no W|10A0 seen in the wild

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
        msg = Message._from_pkt(pkt)

        msg.payload[SZ_DOMAIN_ID] = msg.payload.get(SZ_DOMAIN_ID, "00")

        cmd = _test_api_from_msg(_set_tpi_params, msg, pkt)
        assert cmd.payload == pkt.payload

        if isinstance(packets, dict) and (payload := packets[pkt_line]):
            assert shrink(msg.payload, keep_falsys=True) == eval(payload)


SET_2309_FAIL = (
    "...  W --- 18:000730 01:145038 --:------ 2309 003 017FFF",  # temp is None - should be good?
)
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


def test_set_2e04() -> None:  # NOTE: bespoke: payload
    packets = SET_2E04_GOOD

    for pkt_line in packets:
        pkt = _create_pkt_from_frame(pkt_line.split("#")[0].rstrip())
        msg = Message._from_pkt(pkt)

        cmd = _test_api_from_msg(_set_system_mode, msg, pkt)
        assert cmd.payload == pkt.payload

        if isinstance(packets, dict) and (payload := packets[pkt_line]):
            actual = shrink(msg.payload, keep_falsys=True)
            actual.pop("zone_idx", None)
            assert actual == eval(payload)


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


def put_sensor_temp(dev_id: str, temperature: float) -> Command:
    return LegacyCommandShim.from_dto(
        build_dto(
            Intent(
                src=Address(dev_id),
                dst=Address(dev_id),
                action=Action.PUT_SENSOR_TEMP,
                data={"temperature": temperature},
            )
        )
    )


def test_put_30c9() -> None:
    _test_api_good(put_sensor_temp, PUT_30C9_GOOD)


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

        msg = Message._from_pkt(pkt)

        cmd = _test_api_from_msg(_set_system_time, msg, pkt)
        assert cmd.payload[:4] == pkt.payload[:4]
        assert cmd.payload[6:] == pkt.payload[6:]


PUT_3EF0_FAIL = ("...  I --- 13:123456 --:------ 13:123456 3EF0 003 00AAFF",)
PUT_3EF0_GOOD = (
    "...  I --- 13:123456 --:------ 13:123456 3EF0 003 0000FF",
    "...  I --- 13:123456 --:------ 13:123456 3EF0 003 00C8FF",
)


def test_put_3ef0() -> None:
    _test_api_good(_put_actuator_state, PUT_3EF0_GOOD)


PUT_3EF1_GOOD = (  # TODO: needs checking
    "... RP --- 13:123456 01:123456 --:------ 3EF1 007 000126012600FF",
    "... RP --- 13:123456 18:123456 --:------ 3EF1 007 007FFF003C0010",  # NOTE: should be: RP|10|3EF1
)


def test_put_3ef1() -> None:  # NOTE: bespoke: params, ?payload
    for pkt_line in PUT_3EF1_GOOD:
        pkt = _create_pkt_from_frame(pkt_line)
        msg = Message._from_pkt(pkt)

        kwargs = dict(msg.payload)
        modulation_level = kwargs.pop("modulation_level")
        actuator_countdown = kwargs.pop("actuator_countdown")

        sig = inspect.signature(_put_actuator_cycle)
        valid_kwargs = {
            k: v for k, v in kwargs.items() if k[:1] != "_" and k in sig.parameters
        }

        cmd = _put_actuator_cycle(
            msg.src.id,
            msg.dst.id,
            modulation_level,
            actuator_countdown,
            **valid_kwargs,
        )

        if msg.src.id != HGI_DEV_ADDR.id:
            assert cmd.src.id == pkt.src.id
        assert cmd.dst.id == pkt.dst.id
        assert cmd.verb == pkt.verb
        assert cmd.code == pkt.code

        assert cmd.payload[:-2] == pkt.payload[:-2]
