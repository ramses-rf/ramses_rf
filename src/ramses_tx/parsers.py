#!/usr/bin/env python3
"""RAMSES RF - payload processors.

NOTES: aspirations on a consistent Schema, going forward:

  :mode/state: | :bool:  | :mutex (infinitive. vs -ing):      | :flags:
mode (config.) | enabled | disabled, heat, cool, heat_cool... | ch_enabled, dhw_enabled
state (action) | active  | idle, heating, cooling...          | is_heating, is_cooling

- prefer: enabled: True over xx_enabled: True (if only ever 1 flag)
- prefer:  active: True over is_heating: True (if only ever 1 flag)
- avoid: is_enabled, is_active
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import datetime as dt, timedelta as td
from typing import TYPE_CHECKING, Any

from . import exceptions as exc
from .address import ALL_DEV_ADDR, NON_DEV_ADDR, hex_id_to_dev_id
from .const import (
    DEV_ROLE_MAP,
    DEV_TYPE_MAP,
    FAULT_DEVICE_CLASS,
    FAULT_STATE,
    FAULT_TYPE,
    SYS_MODE_MAP,
    SZ_ACCEPT,
    SZ_ACTIVE,
    SZ_BINDINGS,
    SZ_BYPASS_MODE,
    SZ_BYPASS_STATE,
    SZ_CHANGE_COUNTER,
    SZ_CONFIRM,
    SZ_DATETIME,
    SZ_DEMAND,
    SZ_DEVICE_CLASS,
    SZ_DEVICE_ID,
    SZ_DEVICE_ROLE,
    SZ_DEVICES,
    SZ_DHW_FLOW_RATE,
    SZ_DOMAIN_ID,
    SZ_DOMAIN_IDX,
    SZ_DURATION,
    SZ_FAN_MODE,
    SZ_FAN_RATE,
    SZ_FAULT_STATE,
    SZ_FAULT_TYPE,
    SZ_FRAG_LENGTH,
    SZ_FRAG_NUMBER,
    SZ_FRAGMENT,
    SZ_IS_DST,
    SZ_LANGUAGE,
    SZ_LOCAL_OVERRIDE,
    SZ_LOG_ENTRY,
    SZ_LOG_IDX,
    SZ_MAX_TEMP,
    SZ_MIN_TEMP,
    SZ_MODE,
    SZ_MULTIROOM_MODE,
    SZ_NAME,
    SZ_OEM_CODE,
    SZ_OFFER,
    SZ_OPENWINDOW_FUNCTION,
    SZ_PAYLOAD,
    SZ_PHASE,
    SZ_PRESSURE,
    SZ_RELAY_DEMAND,
    SZ_REMAINING_DAYS,
    SZ_REMAINING_PERCENT,
    SZ_REQ_REASON,
    SZ_SETPOINT,
    SZ_SETPOINT_BOUNDS,
    SZ_SYSTEM_MODE,
    SZ_TEMPERATURE,
    SZ_TIMESTAMP,
    SZ_TOTAL_FRAGS,
    SZ_UFH_IDX,
    SZ_UNTIL,
    SZ_VALUE,
    SZ_WINDOW_OPEN,
    SZ_ZONE_CLASS,
    SZ_ZONE_IDX,
    SZ_ZONE_MASK,
    SZ_ZONE_TYPE,
    ZON_MODE_MAP,
    ZON_ROLE_MAP,
    DevRole,
    FaultDeviceClass,
)
from .fingerprints import check_signature
from .helpers import (
    hex_to_bool,
    hex_to_date,
    hex_to_dtm,
    hex_to_dts,
    hex_to_flag8,
    hex_to_percent,
    hex_to_str,
    hex_to_temp,
    parse_air_quality,
    parse_bypass_position,
    parse_capabilities,
    parse_co2_level,
    parse_exhaust_fan_speed,
    parse_exhaust_flow,
    parse_exhaust_temp,
    parse_fan_info,
    parse_fault_log_entry,
    parse_humidity_element,
    parse_indoor_humidity,
    parse_indoor_temp,
    parse_outdoor_humidity,
    parse_outdoor_temp,
    parse_post_heater,
    parse_pre_heater,
    parse_remaining_mins,
    parse_supply_fan_speed,
    parse_supply_flow,
    parse_supply_temp,
    parse_valve_demand,
)
from .opentherm import (
    EN,
    SZ_DESCRIPTION,
    SZ_MSG_ID,
    SZ_MSG_NAME,
    SZ_MSG_TYPE,
    OtMsgType,
    decode_frame,
)
from .ramses import _31D9_FAN_INFO_VASCO, _2411_PARAMS_SCHEMA
from .typed_dicts import PayDictT
from .version import VERSION

# Kudos & many thanks to:
# - Evsdd: 0404 (wow!)
# - Ierlandfan: 3150, 31D9, 31DA, others
# - ReneKlootwijk: 3EF0
# - brucemiranda: 3EF0, others
# - janvken: 10D0, 1470, 1F70, 22B0, 2411, several others
# - tomkooij: 3110
# - RemyDeRuysscher: 10E0, 31DA (and related), others
# - silverailscolo:  12A0, 31DA, others


from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)
from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    F6,
    F8,
    F9,
    FA,
    FB,
    FC,
    FF,
)

if TYPE_CHECKING:
    from .message import MessageBase as Message  # HACK: merge MsgBase into Msg

_2411_TABLE = {k: v["description"] for k, v in _2411_PARAMS_SCHEMA.items()}

LOOKUP_PUZZ = {
    "10": "engine",  # .    # version str, e.g. v0.14.0
    "11": "impersonating",  # pkt header, e.g. 30C9| I|03:123001 (15 characters, packed)
    "12": "message",  # .   # message only, max len is 16 ascii characters
    "13": "message",  # .   # message only, but without a timestamp, max len 22 chars
    "20": "engine",  # .    # version str, e.g. v0.50.0, has higher-precision timestamp
    "7F": "null",  # .      # packet is null / was nullified: payload to be ignored
}  # "00" is reserved


_INFORM_DEV_MSG = "Support the development of ramses_rf by reporting this packet"


_LOGGER = _PKT_LOGGER = logging.getLogger(__name__)


# rf_unknown
def parser_0001(payload: str, msg: Message) -> Mapping[str, bool | str | None]:
    # When in test mode, a 12: will send a W ?every 6 seconds:
    # 12:39:56.099 061  W --- 12:010740 --:------ 12:010740 0001 005 0000000501
    # 12:40:02.098 061  W --- 12:010740 --:------ 12:010740 0001 005 0000000501
    # 12:40:08.099 058  W --- 12:010740 --:------ 12:010740 0001 005 0000000501

    # sent by a THM when is signal strength test mode (0505, except 1st pkt)
    # 13:48:38.518 080  W --- 12:010740 --:------ 12:010740 0001 005 0000000501
    # 13:48:45.518 074  W --- 12:010740 --:------ 12:010740 0001 005 0000000505
    # 13:48:50.518 077  W --- 12:010740 --:------ 12:010740 0001 005 0000000505

    # sent by a CTL before a rf_check
    # 15:12:47.769 053  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:12:47.869 053 RQ --- 01:145038 13:237335 --:------ 0016 002 00FF
    # 15:12:47.880 053 RP --- 13:237335 01:145038 --:------ 0016 002 0017

    # 12:30:18.083 047  W --- 01:145038 --:------ 01:145038 0001 005 0800000505
    # 12:30:23.084 049  W --- 01:145038 --:------ 01:145038 0001 005 0800000505

    # 15:03:33.187 054  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:03:38.188 063  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:03:43.188 064  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:13:19.757 053  W --- 01:145038 --:------ 01:145038 0001 005 FF00000505
    # 15:13:24.758 054  W --- 01:145038 --:------ 01:145038 0001 005 FF00000505
    # 15:13:29.758 068  W --- 01:145038 --:------ 01:145038 0001 005 FF00000505
    # 15:13:34.759 063  W --- 01:145038 --:------ 01:145038 0001 005 FF00000505

    # sent by a CTL
    # 16:49:46.125 057  W --- 04:166090 --:------ 01:032820 0001 005 0100000505
    # 16:53:34.635 058  W --- 04:166090 --:------ 01:032820 0001 005 0100000505

    # loopback (not Tx'd) by a HGI80 whenever its button is pressed
    # 00:22:41.540 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF02FF
    # 00:22:41.757 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF0200
    # 00:22:43.320 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF02FF
    # 00:22:43.415 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF0200

    # From a CM927:
    # W/--:/--:/12:/00-0000-0501 = Test transmit
    # W/--:/--:/12:/00-0000-0505 = Field strength

    if payload[2:6] in ("2000", "8000", "A000"):
        mode = "hvac"
    elif payload[2:6] in ("0000", "FFFF"):
        mode = "heat"
    else:
        mode = "heat"

    if mode == "hvac":
        result: dict[str, bool | str | None]

        assert payload[:2] == "00", payload[:2]
        # assert payload[2:4] in ("20", "80", "A0"), payload[2:4]
        # assert payload[4:6] == "00", payload[4:6]
        assert payload[8:10] in ("00", "04", "10", "20", "FF"), payload[8:10]

        result = {"payload": payload, "slot_num": payload[6:8]}
        if msg.len >= 6:
            result.update({"param_num": payload[10:12]})
        if msg.len >= 7:
            result.update({"next_slot_num": payload[12:14]})
        if msg.len >= 8:
            _14 = None if payload[14:16] == "FF" else bool(int(payload[14:16]))
            result.update({"boolean_14": _14})
        return result

    assert payload[2:6] in ("0000", "FFFF"), payload[2:6]
    assert payload[8:10] in ("00", "02", "05"), payload[8:10]

    return {
        SZ_PAYLOAD: "-".join((payload[:2], payload[2:6], payload[6:8], payload[8:])),
    }


# outdoor_sensor (outdoor_weather / outdoor_temperature)
def parser_0002(payload: str, msg: Message) -> dict[str, Any]:
    if payload[6:] == "02":  # or: msg.src.type == DEV_TYPE_MAP.OUT:
        return {
            SZ_TEMPERATURE: hex_to_temp(payload[2:6]),
            "_unknown": payload[6:],
        }

    return {"_payload": payload}


# zone_name
def parser_0004(payload: str, msg: Message) -> PayDictT._0004:
    # RQ payload is zz00; limited to 12 chars in evohome UI? if "7F"*20: not a zone

    return {} if payload[4:] == "7F" * 20 else {SZ_NAME: hex_to_str(payload[4:])}


# system_zones (add/del a zone?)  # TODO: needs a cleanup
def parser_0005(payload: str, msg: Message) -> dict | list[dict]:  # TODO: only dict
    # .I --- 01:145038 --:------ 01:145038 0005 004 00000100
    # RP --- 02:017205 18:073736 --:------ 0005 004 0009001F
    # .I --- 34:064023 --:------ 34:064023 0005 012 000A0000-000F0000-00100000

    def _parser(seqx: str) -> dict:
        if msg.src.type == DEV_TYPE_MAP.UFC:  # DEX, or use: seqx[2:4] == ...
            zone_mask = hex_to_flag8(seqx[6:8], lsb=True)
        elif msg.len == 3:  # ATC928G1000 - 1st gen monochrome model, max 8 zones
            zone_mask = hex_to_flag8(seqx[4:6], lsb=True)
        else:
            zone_mask = hex_to_flag8(seqx[4:6], lsb=True) + hex_to_flag8(
                seqx[6:8], lsb=True
            )
        zone_class = ZON_ROLE_MAP.get(seqx[2:4], DEV_ROLE_MAP[seqx[2:4]])
        return {
            SZ_ZONE_TYPE: seqx[2:4],  # TODO: ?remove & keep zone_class?
            SZ_ZONE_MASK: zone_mask,
            SZ_ZONE_CLASS: zone_class,  # TODO: ?remove & keep zone_type?
        }

    if msg.verb == RQ:  # RQs have a context: zone_type
        return {SZ_ZONE_TYPE: payload[2:4], SZ_ZONE_CLASS: DEV_ROLE_MAP[payload[2:4]]}

    if msg._has_array:
        assert msg.verb == I_ and msg.src.type == DEV_TYPE_MAP.RND, (
            f"{msg!r} # expecting I/{DEV_TYPE_MAP.RND}:"
        )  # DEX
        return [_parser(payload[i : i + 8]) for i in range(0, len(payload), 8)]

    return _parser(payload)


# schedule_sync (any changes?)
def parser_0006(payload: str, msg: Message) -> PayDictT._0006:
    """Return the total number of changes to the schedules, including the DHW schedule.

    An RQ is sent every ~60s by a RFG100, an increase will prompt it to send a run of
    RQ|0404s (it seems to assume only the zones may have changed?).
    """
    # 16:10:34.288 053 RQ --- 30:071715 01:145038 --:------ 0006 001 00
    # 16:10:34.291 053 RP --- 01:145038 30:071715 --:------ 0006 004 00050008

    if payload[2:] == "FFFFFF":  # RP to an invalid RQ
        return {}

    assert payload[2:4] == "05"

    return {
        SZ_CHANGE_COUNTER: None if payload[4:] == "FFFF" else int(payload[4:], 16),
    }


# relay_demand (domain/zone/device)
def parser_0008(payload: str, msg: Message) -> PayDictT._0008:
    # https://www.domoticaforum.eu/viewtopic.php?f=7&t=5806&start=105#p73681
    # e.g. Electric Heat Zone

    # .I --- 01:145038 --:------ 01:145038 0008 002 0314
    # .I --- 01:145038 --:------ 01:145038 0008 002 F914
    # .I --- 01:054173 --:------ 01:054173 0008 002 FA00
    # .I --- 01:145038 --:------ 01:145038 0008 002 FC14

    # RP --- 13:109598 18:199952 --:------ 0008 002 0000
    # RP --- 13:109598 18:199952 --:------ 0008 002 00C8

    if msg.src.type == DEV_TYPE_MAP.JST and msg.len == 13:  # Honeywell Japser, DEX
        assert msg.len == 13, "expecting length 13"
        return {  # type: ignore[typeddict-item]
            "ordinal": f"0x{payload[2:8]}",
            "blob": payload[8:],
        }

    return {SZ_RELAY_DEMAND: hex_to_percent(payload[2:4])}  # 3EF0[2:4], 3EF1[10:12]


# relay_failsafe
def parser_0009(payload: str, msg: Message) -> dict | list[dict]:  # TODO: only dict
    """The relay failsafe mode.

    The failsafe mode defines the relay behaviour if the RF communication is lost (e.g.
    when a room thermostat stops communicating due to discharged batteries):
        False (disabled) - if RF comms are lost, relay will be held in OFF position
        True  (enabled)  - if RF comms are lost, relay will cycle at 20% ON, 80% OFF

    This setting may need to be enabled to ensure frost protect mode.
    """
    # can get: 003 or 006, e.g.: FC01FF-F901FF or FC00FF-F900FF
    # .I --- 23:100224 --:------ 23:100224 0009 003 0100FF  # 2-zone ST9520C
    # .I --- 10:040239 01:223036 --:------ 0009 003 000000

    def _parser(seqx: str) -> dict:
        assert seqx[:2] in (F9, FC) or int(seqx[:2], 16) < 16
        return {
            SZ_DOMAIN_ID if seqx[:1] == "F" else SZ_ZONE_IDX: seqx[:2],
            "failsafe_enabled": {"00": False, "01": True}.get(seqx[2:4]),
            "unknown_0": seqx[4:],
        }

    if msg._has_array:
        return [_parser(payload[i : i + 6]) for i in range(0, len(payload), 6)]

    return {
        "failsafe_enabled": {"00": False, "01": True}.get(payload[2:4]),
        "unknown_0": payload[4:],
    }


# zone_params (zone_config)
def parser_000a(
    payload: str, msg: Message
) -> PayDictT._000A | list[PayDictT._000A] | PayDictT.EMPTY:
    def _parser(seqx: str) -> PayDictT._000A:  # null_rp: "007FFF7FFF"
        bitmap = int(seqx[2:4], 16)
        return {
            SZ_MIN_TEMP: hex_to_temp(seqx[4:8]),
            SZ_MAX_TEMP: hex_to_temp(seqx[8:]),
            SZ_LOCAL_OVERRIDE: not bool(bitmap & 1),
            SZ_OPENWINDOW_FUNCTION: not bool(bitmap & 2),
            SZ_MULTIROOM_MODE: not bool(bitmap & 16),
            "_unknown_bitmap": f"0b{bitmap:08b}",  # TODO: try W with this
        }  # cannot determine zone_type from this information

    if msg._has_array:  # NOTE: these arrays can span 2 pkts!
        return [
            {
                SZ_ZONE_IDX: payload[i : i + 2],
                **_parser(payload[i : i + 12]),
            }
            for i in range(0, len(payload), 12)
        ]

    if msg.verb == RQ and msg.len <= 2:  # some RQs have a payload (why?)
        return {}

    assert msg.len == 6, f"{msg!r} # expecting length 006"
    return _parser(payload)


# zone_devices
def parser_000c(payload: str, msg: Message) -> dict[str, Any]:
    # .I --- 34:092243 --:------ 34:092243 000C 018 00-0A-7F-FFFFFF 00-0F-7F-FFFFFF 00-10-7F-FFFFFF  # noqa: E501
    # RP --- 01:145038 18:013393 --:------ 000C 006 00-00-00-10DAFD
    # RP --- 01:145038 18:013393 --:------ 000C 012 01-00-00-10DAF5 01-00-00-10DAFB

    def complex_idx(seqx: str, msg: Message) -> dict:  # complex index
        """domain_id, zone_idx, or ufx_idx|zone_idx."""

        # TODO: 000C to a UFC should be ufh_ifx, not zone_idx
        if msg.src.type == DEV_TYPE_MAP.UFC:  # DEX
            assert int(seqx, 16) < 8, f"invalid ufh_idx: '{seqx}' (0x00)"
            return {
                SZ_UFH_IDX: seqx,
                SZ_ZONE_IDX: None if payload[4:6] == "7F" else payload[4:6],
            }

        if payload[2:4] in (DEV_ROLE_MAP.DHW, DEV_ROLE_MAP.HTG):
            assert int(seqx, 16) < 1 if payload[2:4] == DEV_ROLE_MAP.DHW else 2, (
                f"invalid _idx: '{seqx}' (0x01)"
            )
            return {SZ_DOMAIN_ID: FA if payload[:2] == "00" else F9}

        if payload[2:4] == DEV_ROLE_MAP.APP:
            assert int(seqx, 16) < 1, f"invalid _idx: '{seqx}' (0x02)"
            return {SZ_DOMAIN_ID: FC}

        assert int(seqx, 16) < 16, f"invalid zone_idx: '{seqx}' (0x03)"
        return {SZ_ZONE_IDX: seqx}

    def _parser(
        seqx: str,
    ) -> dict:  # TODO: assumption that all id/idx are same is wrong!
        assert seqx[:2] == payload[:2], (
            f"idx != {payload[:2]} (seqx = {seqx}), short={is_short_000C(payload)}"
        )
        assert int(seqx[:2], 16) < 16
        assert seqx[4:6] == "7F" or seqx[6:] != "F" * 6, f"Bad device_id: {seqx[6:]}"
        return {hex_id_to_dev_id(seqx[6:12]): seqx[4:6]}

    def is_short_000C(payload: str) -> bool:
        """Return True if it is a short 000C (element length is 5, not 6)."""

        if (pkt_len := len(payload)) != 72:
            return pkt_len % 12 != 0

        # 0608-001099C3 0608-001099C5 0608-001099BF 0608-001099BE 0608-001099BD 0608-001099BC  # len(element) = 6
        # 0508-00109901 0800-10990208 0010-99030800 1099-04080010 9905-08001099 0608-00109907  # len(element) = 5
        elif all(payload[i : i + 4] == payload[:4] for i in range(12, pkt_len, 12)):
            return False  # len(element) = 6 (12)

        # 06 08-001099C3 06-08001099 C5-06080010 99-BF060800 10-99BE0608 00-1099BD06 08-001099BC  # len(element) = 6
        # 05 08-00109901 08-00109902 08-00109903 08-00109904 08-00109905 08-00109906 08-00109907  # len(element) = 5
        elif all(payload[i : i + 2] == payload[2:4] for i in range(12, pkt_len, 10)):
            return True  # len(element) = 5 (10)

        raise exc.PacketPayloadInvalid(
            "Unable to determine element length"
        )  # return None

    if payload[2:4] == DEV_ROLE_MAP.HTG and payload[:2] == "01":
        dev_role = DEV_ROLE_MAP[DevRole.HT1]
    else:
        dev_role = DEV_ROLE_MAP[payload[2:4]]

    result = {
        SZ_ZONE_TYPE: payload[2:4],
        **complex_idx(payload[:2], msg),
        SZ_DEVICE_ROLE: dev_role,
    }
    if msg.verb == RQ:  # RQs have a context: index, zone_type, payload is iitt
        return result

    # NOTE: Both these are valid! So collision when len = 036!
    # RP --- 01:239474 18:198929 --:------ 000C 012 06-00-00119A99 06-00-00119B21
    # RP --- 01:069616 18:205592 --:------ 000C 011 01-00-00121B54    00-00121B52
    # RP --- 01:239700 18:009874 --:------ 000C 018 07-08-001099C3 07-08-001099C5 07-08-001099BF
    # RP --- 01:059885 18:010642 --:------ 000C 016 00-00-0011EDAA    00-0011ED92    00-0011EDA0

    devs = (
        [_parser(payload[:2] + payload[i : i + 10]) for i in range(2, len(payload), 10)]
        if is_short_000C(payload)
        else [_parser(payload[i : i + 12]) for i in range(0, len(payload), 12)]
    )

    return {
        **result,
        SZ_DEVICES: [k for d in devs for k, v in d.items() if v != "7F"],
    }


# unknown_000e, from STA
def parser_000e(payload: str, msg: Message) -> dict[str, Any]:
    assert payload in ("000014", "000028"), _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


# rf_check
def parser_0016(payload: str, msg: Message) -> dict[str, Any]:
    # TODO: does 0016 include parent_idx?, but RQ|07:|0000?
    # RQ --- 22:060293 01:078710 --:------ 0016 002 0200
    # RP --- 01:078710 22:060293 --:------ 0016 002 021E
    # RQ --- 12:010740 01:145038 --:------ 0016 002 0800
    # RP --- 01:145038 12:010740 --:------ 0016 002 081E
    # RQ --- 07:031785 01:063844 --:------ 0016 002 0000
    # RP --- 01:063844 07:031785 --:------ 0016 002 002A

    if msg.verb == RQ:  # and msg.len == 1:  # TODO: some RQs have a payload
        return {}

    rf_value = int(payload[2:4], 16)
    return {
        "rf_strength": min(int(rf_value / 5) + 1, 5),
        "rf_value": rf_value,
    }


# language (of device/system)
def parser_0100(payload: str, msg: Message) -> PayDictT._0100 | PayDictT.EMPTY:
    if msg.verb == RQ and msg.len == 1:  # some RQs have a payload
        return {}

    return {
        SZ_LANGUAGE: hex_to_str(payload[2:6]),
        "_unknown_0": payload[6:],
    }


# unknown_0150, from OTB
def parser_0150(payload: str, msg: Message) -> dict[str, Any]:
    assert payload == "000000", _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


# unknown_01d0, from a HR91 (when its buttons are pushed)
def parser_01d0(payload: str, msg: Message) -> dict[str, Any]:
    # 23:57:28.869 045  W --- 04:000722 01:158182 --:------ 01D0 002 0003
    # 23:57:28.931 045  I --- 01:158182 04:000722 --:------ 01D0 002 0003
    # 23:57:31.581 048  W --- 04:000722 01:158182 --:------ 01E9 002 0003
    # 23:57:31.643 045  I --- 01:158182 04:000722 --:------ 01E9 002 0000
    # 23:57:31.749 050  W --- 04:000722 01:158182 --:------ 01D0 002 0000
    # 23:57:31.811 045  I --- 01:158182 04:000722 --:------ 01D0 002 0000

    assert payload[2:] in ("00", "03"), _INFORM_DEV_MSG
    return {
        "unknown_0": payload[2:],
    }


# unknown_01e9, from a HR91 (when its buttons are pushed)
def parser_01e9(payload: str, msg: Message) -> dict[str, Any]:
    # 23:57:31.581348 048  W --- 04:000722 01:158182 --:------ 01E9 002 0003
    # 23:57:31.643188 045  I --- 01:158182 04:000722 --:------ 01E9 002 0000

    assert payload[2:] in ("00", "03"), _INFORM_DEV_MSG
    return {
        "unknown_0": payload[2:],
    }


# unknown_01ff, to/from a Itho Spider/Thermostat
def parser_01ff(payload: str, msg: Message) -> dict[str, Any]:
    # see: https://github.com/zxdavb/ramses_rf/issues/73 & 101

    # lots of '80's, and I see temps are `int(payload[6:8], 16) / 2`, so I wonder if 0x80 is N/A?
    # also is '7F'

    # return {
    #     "dis_temp": None if payload[4:6] == "80" else int(payload[4:6], 16) / 2,
    #     "set_temp": int(payload[6:8], 16) / 2,
    #     "max_temp": int(payload[8:10], 16) / 2,  # 22C9 - temp high
    #     "mode_val": payload[10:12],
    #     "mode_xxx": payload[10:11] in ("9", "B", "D") and payload[11:12] in ("0", "2"),
    # }

    assert payload[:4] in ("0080", "0180"), f"{_INFORM_DEV_MSG} ({payload[:4]})"
    assert payload[12:14] == "00", f"{_INFORM_DEV_MSG} ({payload[12:14]})"
    # assert payload[16:22] in (
    #     "00143C",
    #     "002430",
    #     "7F8080",
    # ), f"{_INFORM_DEV_MSG} ({payload[16:22]})"  # idx|25.9C?
    assert payload[26:30] == "0000", f"{_INFORM_DEV_MSG} ({payload[26:30]})"
    assert payload[34:46] == "80800280FF80", f"{_INFORM_DEV_MSG} ({payload[34:46]})"
    # assert payload[48:] in (
    #     "0000",
    #     "0020",
    #     "0084",
    #     "00A4",
    # ), f"{_INFORM_DEV_MSG} ({payload[48:]})"

    if msg.verb in (I_, RQ):  # from Spider thermostat to gateway
        assert payload[14:16] == "80", f"{_INFORM_DEV_MSG} ({payload[14:16]})"
        # assert payload[22:26] in (
        #     "2832",
        #     "2840",
        # ), f"{_INFORM_DEV_MSG} ({payload[22:26]})"
        # assert payload[30:34] in (
        #     "0104",
        #     "4402",
        #     "C102",
        #     "C402",
        # ), f"{_INFORM_DEV_MSG} ({payload[30:34]})"
        assert payload[46:48] in ("04", "07"), f"{_INFORM_DEV_MSG} ({payload[46:48]})"

    if msg.verb in (RP, W_):  # from Spider gateway to thermostat
        # assert payload[14:16] in (
        #     "00",
        #     "7F",
        #     "80",
        # ), f"{_INFORM_DEV_MSG} ({payload[14:16]})"
        # assert payload[22:26] in (
        #     "2840",
        #     "8080",
        # ), f"{_INFORM_DEV_MSG} ({payload[22:26]})"
        # assert payload[30:34] in (
        #     "0104",
        #     "3100",
        #     "3700",
        #     "B400",
        # ), f"{_INFORM_DEV_MSG} ({payload[30:34]})"
        assert payload[46:48] in (
            "00",
            "04",
            "07",
        ), f"{_INFORM_DEV_MSG} ({payload[46:48]})"

    setpoint_bounds = (
        int(payload[6:8], 16) / 2,  # as: 22C9[2:6] and [6:10] ???
        None if msg.verb in (RP, W_) else int(payload[8:10], 16) / 2,
    )

    return {
        SZ_TEMPERATURE: None if msg.verb in (RP, W_) else int(payload[4:6], 16) / 2,
        SZ_SETPOINT_BOUNDS: setpoint_bounds,
        "time_planning": not bool(int(payload[10:12], 16) & 1 << 6),
        "temp_adjusted": bool(int(payload[10:12], 16) & 1 << 5),
        "_flags_10": payload[10:12],  #
    }


# zone_schedule (fragment)
def parser_0404(payload: str, msg: Message) -> PayDictT._0404:
    # Retrieval of Zone schedule (NB: 200008)
    # RQ --- 30:185469 01:037519 --:------ 0404 007 00-200008-00-0100
    # RP --- 01:037519 30:185469 --:------ 0404 048 00-200008-29-0103-6E2...
    # RQ --- 30:185469 01:037519 --:------ 0404 007 00-200008-00-0203
    # RP --- 01:037519 30:185469 --:------ 0404 048 00-200008-29-0203-4FD...
    # RQ --- 30:185469 01:037519 --:------ 0404 007 00-200008-00-0303
    # RP --- 01:037519 30:185469 --:------ 0404 038 00-200008-1F-0303-C10...

    # Retrieval of DHW schedule (NB: 230008)
    # RQ --- 30:185469 01:037519 --:------ 0404 007 00-230008-00-0100
    # RP --- 01:037519 30:185469 --:------ 0404 048 00-230008-29-0103-618...
    # RQ --- 30:185469 01:037519 --:------ 0404 007 00-230008-00-0203
    # RP --- 01:037519 30:185469 --:------ 0404 048 00-230008-29-0203-ED6...
    # RQ --- 30:185469 01:037519 --:------ 0404 007 00-230008-00-0303
    # RP --- 01:037519 30:185469 --:------ 0404 014 00-230008-07-0303-13F...

    # Write a Zone schedule...
    # .W --- 30:042165 01:076010 --:------ 0404 048 08-200808-29-0104-688...
    # .I --- 01:076010 30:042165 --:------ 0404 007 08-200808-29-0104
    # .W --- 30:042165 01:076010 --:------ 0404 048 08-200808-29-0204-007...
    # .I --- 01:076010 30:042165 --:------ 0404 007 08-200808-29-0204
    # .W --- 30:042165 01:076010 --:------ 0404 048 08-200808-29-0304-8DD...
    # .I --- 01:076010 30:042165 --:------ 0404 007 08-200808-29-0304
    # .W --- 30:042165 01:076010 --:------ 0404 048 08-200808-11-0404-970...
    # .I --- 01:076010 30:042165 --:------ 0404 007 08-200808-11-0400

    # RP --- 01:145038 18:013393 --:------ 0404 007 00-230008-00-01FF  # no schedule

    assert payload[4:6] in ("00", payload[:2]), _INFORM_DEV_MSG

    if int(payload[8:10], 16) * 2 != (frag_length := len(payload[14:])) and (
        msg.verb != I_ or frag_length != 0
    ):
        raise exc.PacketPayloadInvalid(f"Incorrect fragment length: 0x{payload[8:10]}")

    if msg.verb == RQ:  # have a ctx: idx|frag_idx
        return {
            SZ_FRAG_NUMBER: int(payload[10:12], 16),
            SZ_TOTAL_FRAGS: None if payload[12:14] == "00" else int(payload[12:14], 16),
        }

    if msg.verb == I_:  # have a ctx: idx|frag_idx
        return {
            SZ_FRAG_NUMBER: int(payload[10:12], 16),
            SZ_TOTAL_FRAGS: int(payload[12:14], 16),
            SZ_FRAG_LENGTH: None if payload[8:10] == "00" else int(payload[8:10], 16),
        }

    if payload[12:14] == FF:
        return {
            SZ_FRAG_NUMBER: int(payload[10:12], 16),
            SZ_TOTAL_FRAGS: None,
        }

    return {
        SZ_FRAG_NUMBER: int(payload[10:12], 16),
        SZ_TOTAL_FRAGS: int(payload[12:14], 16),
        SZ_FRAG_LENGTH: None if payload[8:10] == "FF" else int(payload[8:10], 16),
        SZ_FRAGMENT: payload[14:],
    }


# system_fault (fault_log_entry) - needs refactoring
def parser_0418(payload: str, msg: Message) -> PayDictT._0418 | PayDictT._0418_NULL:
    null_result: PayDictT._0418_NULL
    full_result: PayDictT._0418

    # assert int(payload[4:6], 16) < 64, f"Unexpected log_idx: 0x{payload[4:6]}"

    # RQ --- 18:017804 01:145038 --:------ 0418 003 000005                                        # log_idx=0x05
    # RP --- 01:145038 18:017804 --:------ 0418 022 000005B0040000000000CD17B5AE7FFFFF7000000001  # log_idx=0x05

    # RQ --- 18:017804 01:145038 --:------ 0418 003 000006                                        # log_idx=0x06
    # RP --- 01:145038 18:017804 --:------ 0418 022 000000B0000000000000000000007FFFFF7000000000  # log_idx=None (00)

    if msg.verb == RQ:  # has a ctx: log_idx
        null_result = {SZ_LOG_IDX: payload[4:6]}  # type: ignore[typeddict-item]
        return null_result

    # NOTE: such payloads have idx=="00": if verb is I, can safely assume log_idx is 0,
    # but for RP it is sentinel for null (we can't know the corresponding RQ's log_idx)
    elif hex_to_dts(payload[18:30]) is None:
        null_result = {SZ_LOG_ENTRY: None}
        if msg.verb == I_:
            null_result = {SZ_LOG_IDX: payload[4:6]} | null_result  # type: ignore[assignment]
        return null_result

    try:
        assert payload[2:4] in FAULT_STATE, f"fault state: {payload[2:4]}"
        assert payload[8:10] in FAULT_TYPE, f"fault type: {payload[8:10]}"
        assert payload[12:14] in FAULT_DEVICE_CLASS, f"device class: {payload[12:14]}"
        # 1C: 'Comms fault, Actuator': seen with boiler relays
        assert int(payload[10:12], 16) < 16 or (
            payload[10:12] in ("1C", F6, F9, FA, FC)
        ), f"domain id: {payload[10:12]}"
    except AssertionError as err:
        _LOGGER.warning(
            f"{msg!r} < {_INFORM_DEV_MSG} ({err}), with a photo of your fault log"
        )

    # log_entry will not be None, because of guard clauses, above
    log_entry: PayDictT.FAULT_LOG_ENTRY = parse_fault_log_entry(payload)  # type: ignore[assignment]

    # log_idx is not intrinsic to the fault & increments as the fault moves down the log
    log_entry.pop(f"_{SZ_LOG_IDX}")  # type: ignore[misc]

    _KEYS = (SZ_TIMESTAMP, SZ_FAULT_STATE, SZ_FAULT_TYPE)
    entry = [v for k, v in log_entry.items() if k in _KEYS]

    if log_entry[SZ_DEVICE_CLASS] != FaultDeviceClass.ACTUATOR:
        entry.append(log_entry[SZ_DEVICE_CLASS])
    elif log_entry[SZ_DOMAIN_IDX] == FC:
        entry.append(DEV_ROLE_MAP[DevRole.APP])  # actual evohome UI
    elif log_entry[SZ_DOMAIN_IDX] == FA:
        entry.append(DEV_ROLE_MAP[DevRole.HTG])  # speculative
    elif log_entry[SZ_DOMAIN_IDX] == F9:
        entry.append(DEV_ROLE_MAP[DevRole.HT1])  # speculative
    else:
        entry.append(FaultDeviceClass.ACTUATOR)

    # TODO: remove the qualifier (the assert is false)
    if log_entry[SZ_DEVICE_CLASS] != FaultDeviceClass.CONTROLLER:
        # assert log_entry[SZ_DOMAIN_IDX] == "00", log_entry[SZ_DOMAIN_IDX]
        # key_name = SZ_ZONE_IDX if int(payload[10:12], 16) < 16 else SZ_DOMAIN_ID
        # log_entry.update({key_name: payload[10:12]})
        entry.append(log_entry[SZ_DOMAIN_IDX])

    if log_entry[SZ_DEVICE_ID] not in ("00:000000", "00:000001", "00:000002"):
        # "00:000001 for Controller? "00:000002 for Unknown?
        entry.append(log_entry[SZ_DEVICE_ID])

    entry.extend((payload[6:8], payload[14:18], payload[30:38]))  # TODO: remove?

    full_result = {
        SZ_LOG_IDX: payload[4:6],  # type: ignore[typeddict-item]
        SZ_LOG_ENTRY: tuple([str(r) for r in entry]),
    }
    return full_result


# unknown_042f, from STA, VMS
def parser_042f(payload: str, msg: Message) -> dict[str, Any]:
    return {
        "counter_1": f"0x{payload[2:6]}",
        "counter_3": f"0x{payload[6:10]}",
        "counter_5": f"0x{payload[10:14]}",
        "unknown_7": f"0x{payload[14:]}",
    }


# TODO: unknown_0b04, from THM (only when its a CTL?)
def parser_0b04(payload: str, msg: Message) -> dict[str, Any]:
    # .I --- --:------ --:------ 12:207082 0B04 002 00C8  # batch of 3, every 24h

    return {
        "unknown_1": payload[2:],
    }


# mixvalve_config (zone), FAN
def parser_1030(payload: str, msg: Message) -> PayDictT._1030:
    # .I --- 01:145038 --:------ 01:145038 1030 016 0A-C80137-C9010F-CA0196-CB0100-CC0101
    # .I --- --:------ --:------ 12:144017 1030 016 01-C80137-C9010F-CA0196-CB010F-CC0101
    # RP --- 32:155617 18:005904 --:------ 1030 007 00-200100-21011F

    def _parser(seqx: str) -> dict:
        assert seqx[2:4] == "01", seqx[2:4]

        param_name = {
            "20": "unknown_20",  # HVAC
            "21": "unknown_21",  # HVAC
            "C8": "max_flow_setpoint",  # 55 (0-99) C
            "C9": "min_flow_setpoint",  # 15 (0-50) C
            "CA": "valve_run_time",  # 150 (0-240) sec, aka actuator_run_time
            "CB": "pump_run_time",  # 15 (0-99) sec
            "CC": "boolean_cc",  # ?boolean?
        }[seqx[:2]]

        return {param_name: int(seqx[4:], 16)}

    assert (msg.len - 1) / 3 in (2, 5), msg.len
    # assert payload[30:] in ("00", "01"), payload[30:]

    params = [_parser(payload[i : i + 6]) for i in range(2, len(payload), 6)]
    return {k: v for x in params for k, v in x.items()}  # type: ignore[return-value]


# device_battery (battery_state)
def parser_1060(payload: str, msg: Message) -> PayDictT._1060:
    """Return the battery state.

    Some devices (04:) will also report battery level.
    """

    assert msg.len == 3, msg.len
    assert payload[4:6] in ("00", "01")

    return {
        "battery_low": payload[4:] == "00",
        "battery_level": None if payload[2:4] == "00" else hex_to_percent(payload[2:4]),
    }


# max_ch_setpoint (supply high limit)
def parser_1081(payload: str, msg: Message) -> PayDictT._1081:
    return {SZ_SETPOINT: hex_to_temp(payload[2:])}


# unknown_1090 (non-Evohome, e.g. ST9520C)
def parser_1090(payload: str, msg: Message) -> PayDictT._1090:
    # 14:08:05.176 095 RP --- 23:100224 22:219457 --:------ 1090 005 007FFF01F4
    # 18:08:05.809 095 RP --- 23:100224 22:219457 --:------ 1090 005 007FFF01F4

    # this is an educated guess
    assert msg.len == 5, _INFORM_DEV_MSG
    assert int(payload[:2], 16) < 2, _INFORM_DEV_MSG

    return {
        "temperature_0": hex_to_temp(payload[2:6]),
        "temperature_1": hex_to_temp(payload[6:10]),
    }


# unknown_1098, from OTB
def parser_1098(payload: str, msg: Message) -> dict[str, Any]:
    assert payload == "00C8", _INFORM_DEV_MSG

    return {
        "_payload": payload,
        "_value": {"00": False, "C8": True}.get(
            payload[2:], hex_to_percent(payload[2:])
        ),
    }


# dhw (cylinder) params  # FIXME: a bit messy
def parser_10a0(payload: str, msg: Message) -> PayDictT._10A0 | PayDictT.EMPTY:
    # RQ --- 07:045960 01:145038 --:------ 10A0 006 00-1087-00-03E4  # RQ/RP, every 24h
    # RP --- 01:145038 07:045960 --:------ 10A0 006 00-109A-00-03E8
    # RP --- 10:048122 18:006402 --:------ 10A0 003 00-1B58

    # these may not be reliable...
    # RQ --- 01:136410 10:067219 --:------ 10A0 002 0000
    # RQ --- 07:017494 01:078710 --:------ 10A0 006 00-1566-00-03E4

    # RQ --- 07:045960 01:145038 --:------ 10A0 006 00-31FF-00-31FF  # null
    # RQ --- 07:045960 01:145038 --:------ 10A0 006 00-1770-00-03E8
    # RQ --- 07:045960 01:145038 --:------ 10A0 006 00-1374-00-03E4
    # RQ --- 07:030741 01:102458 --:------ 10A0 006 00-181F-00-03E4
    # RQ --- 07:036831 23:100224 --:------ 10A0 006 01-1566-00-03E4  # non-evohome

    # these from a RFG...
    # RQ --- 30:185469 01:037519 --:------ 0005 002 000E
    # RP --- 01:037519 30:185469 --:------ 0005 004 000E0300  # two DHW valves
    # RQ --- 30:185469 01:037519 --:------ 10A0 001 01 (01 )

    if msg.verb == RQ and msg.len == 1:  # some RQs have a payload (why?)
        # 045 RQ --- 07:045960 01:145038 --:------ 10A0 006 0013740003E4
        # 037 RQ --- 18:013393 01:145038 --:------ 10A0 001 00
        # 054 RP --- 01:145038 18:013393 --:------ 10A0 006 0013880003E8
        return {}

    assert msg.len in (1, 3, 6), msg.len  # OTB uses 3, evohome uses 6
    assert payload[:2] in ("00", "01"), payload[:2]  # can be two DHW valves/system

    result: PayDictT._10A0 = {}  # type: ignore[typeddict-item]
    if msg.len >= 2:
        setpoint = hex_to_temp(payload[2:6])  # 255 for OTB? iff no DHW?
        result = {SZ_SETPOINT: None if setpoint == 255 else setpoint}  # 30.0-85.0 C
    if msg.len >= 4:
        result["overrun"] = int(payload[6:8], 16)  # 0-10 minutes
    if msg.len >= 6:
        result["differential"] = hex_to_temp(payload[8:12])  # 1.0-10.0 C

    return result


# unknown_10b0, from OTB
def parser_10b0(payload: str, msg: Message) -> dict[str, Any]:
    assert payload == "0000", _INFORM_DEV_MSG

    return {
        "_payload": payload,
        "_value": {"00": False, "C8": True}.get(
            payload[2:], hex_to_percent(payload[2:])
        ),
    }


# filter_change, HVAC
def parser_10d0(payload: str, msg: Message) -> dict[str, Any]:
    # 2022-07-03T22:52:34.571579 045  W --- 37:171871 32:155617 --:------ 10D0 002 00FF
    # 2022-07-03T22:52:34.596526 066  I --- 32:155617 37:171871 --:------ 10D0 006 0047B44F0000
    # then...
    # 2022-07-03T23:14:23.854089 000 RQ --- 37:155617 32:155617 --:------ 10D0 002 0000
    # 2022-07-03T23:14:23.876088 084 RP --- 32:155617 37:155617 --:------ 10D0 006 00B4B4C80000

    # 00-FF resets the counter, 00-47-B4-4F-0000 is the value (71 180 79).
    # Default is 180 180 200. The returned value is the amount of days (180),
    # total amount of days till change (180), percentage (200)

    result: dict[str, bool | float | None]

    if msg.verb == W_:
        return {"reset_counter": payload[2:4] != "00"}

    result = {}

    if payload[2:4] not in ("FF", "FE"):
        result[SZ_REMAINING_DAYS] = int(payload[2:4], 16)

    if payload[4:6] not in ("FF", "FE"):
        result["days_lifetime"] = int(payload[4:6], 16)

    result[SZ_REMAINING_PERCENT] = hex_to_percent(payload[6:8])

    return result


# device_info
def parser_10e0(payload: str, msg: Message) -> dict[str, Any]:
    if payload == "00":  # some HVAC devices will RP|10E0|00
        return {}

    assert msg.len in (19, 28, 29, 30, 36, 38), msg.len  # >= 19, msg.len

    payload = re.sub("(00)*$", "", payload)  # remove trailing 00s
    assert len(payload) >= 18 * 2

    # if DEV_MODE:  # TODO
    try:  # DEX
        check_signature(msg.src.type, payload[2:20])
    except ValueError as err:
        _LOGGER.info(
            f"{msg!r} < {_INFORM_DEV_MSG}, with the make/model of device: {msg.src} ({err})"
        )

    description, _, unknown = payload[36:].partition("00")

    result = {
        SZ_OEM_CODE: payload[14:16],  # 00/FF is CH/DHW, 01/6x is HVAC
        # "_manufacturer_group": payload[2:6],  # 0001-HVAC, 0002-CH/DHW
        "manufacturer_sub_id": payload[6:8],
        "product_id": payload[8:10],  # if CH/DHW: matches device_type (sometimes)
        "date_1": hex_to_date(payload[28:36]) or "0000-00-00",  # hardware?
        "date_2": hex_to_date(payload[20:28]) or "0000-00-00",  # firmware?
        # "software_ver_id": payload[10:12],
        # "list_ver_id": payload[12:14],  # if FF/01 is CH/DHW, then 01/FF
        # # "additional_ver_a": payload[16:18],
        # # "additional_ver_b": payload[18:20],
        # "_signature": payload[2:20],
        "description": bytearray.fromhex(description).decode(),
    }
    if unknown:  # TODO: why only RP|OTB, I|DT4s do this?
        result["_unknown"] = unknown
    return result


# device_id
def parser_10e1(payload: str, msg: Message) -> PayDictT._10E1:
    return {SZ_DEVICE_ID: hex_id_to_dev_id(payload[2:])}


# unknown_10e2 - HVAC
def parser_10e2(payload: str, msg: Message) -> dict[str, Any]:
    # .I --- --:------ --:------ 20:231151 10E2 003 00AD74  # every 2 minutes

    assert payload[:2] == "00", _INFORM_DEV_MSG
    assert len(payload) == 6, _INFORM_DEV_MSG

    return {
        "counter": int(payload[2:], 16),
    }


# tpi_params (domain/zone/device)  # FIXME: a bit messy
def parser_1100(
    payload: str, msg: Message
) -> PayDictT._1100 | PayDictT._1100_IDX | PayDictT._JASPER | PayDictT.EMPTY:
    def complex_idx(seqx: str) -> PayDictT._1100_IDX | PayDictT.EMPTY:
        return {SZ_DOMAIN_ID: seqx} if seqx[:1] == "F" else {}  # type: ignore[typeddict-item]  # only FC

    if msg.src.type == DEV_TYPE_MAP.JIM:  # Honeywell Japser, DEX
        assert msg.len == 19, msg.len
        return {
            "ordinal": f"0x{payload[2:8]}",
            "blob": payload[8:],
        }

    if msg.verb == RQ and msg.len == 1:  # some RQs have a payload (why?)
        return complex_idx(payload[:2])

    assert int(payload[2:4], 16) / 4 in range(1, 13), payload[2:4]
    assert int(payload[4:6], 16) / 4 in range(1, 31), payload[4:6]
    assert int(payload[6:8], 16) / 4 in range(0, 16), payload[6:8]

    # for:             TPI              // heatpump
    #  - cycle_rate:   6 (3, 6, 9, 12)  // ?? (1-9)
    #  - min_on_time:  1 (1-5)          // ?? (1, 5, 10,...30)
    #  - min_off_time: 1 (1-?)          // ?? (0, 5, 10, 15)

    def _parser(seqx: str) -> PayDictT._1100:
        return {
            "cycle_rate": int(int(payload[2:4], 16) / 4),  # cycles/hour
            "min_on_time": int(payload[4:6], 16) / 4,  # min
            "min_off_time": int(payload[6:8], 16) / 4,  # min
            "_unknown_0": payload[8:10],  # always 00, FF?
        }

    result = _parser(payload)

    if msg.len > 5:
        pbw = hex_to_temp(payload[10:14])

        assert pbw is None or 1.5 <= pbw <= 3.0, (
            f"unexpected value for PBW: {payload[10:14]}"
        )

        result.update(
            {
                "proportional_band_width": pbw,
                "_unknown_1": payload[14:],  # always 01?
            }
        )

    return complex_idx(payload[:2]) | result


# unknown_11f0, from heatpump relay
def parser_11f0(payload: str, msg: Message) -> dict[str, Any]:
    assert payload == "000009000000000000", _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


# dhw cylinder temperature
def parser_1260(payload: str, msg: Message) -> PayDictT._1260:
    return {SZ_TEMPERATURE: hex_to_temp(payload[2:])}


# HVAC: outdoor humidity
def parser_1280(payload: str, msg: Message) -> PayDictT._1280:
    return parse_outdoor_humidity(payload[2:])


# outdoor temperature
def parser_1290(payload: str, msg: Message) -> PayDictT._1290:
    # evohome responds to an RQ, also from OTB
    return parse_outdoor_temp(payload[2:])


# HVAC: co2_level, see: 31DA[6:10]
def parser_1298(payload: str, msg: Message) -> PayDictT._1298:
    return parse_co2_level(payload[2:6])


# HVAC: indoor_humidity, array of 3 sets for HRU
def parser_12a0(
    payload: str, msg: Message
) -> PayDictT.INDOOR_HUMIDITY | list[PayDictT._12A0]:
    if len(payload) <= 14:
        return parse_indoor_humidity(payload[2:12])

    return [
        {
            "hvac_idx": payload[i : i + 2],  # used as index
            **parse_humidity_element(payload[i + 2 : i + 12], payload[i : i + 2]),
        }
        for i in range(0, len(payload), 14)
    ]


# window_state (of a device/zone)
def parser_12b0(payload: str, msg: Message) -> PayDictT._12B0:
    assert payload[2:] in ("0000", "C800", "FFFF"), payload[2:]  # "FFFF" means N/A

    return {
        SZ_WINDOW_OPEN: hex_to_bool(payload[2:4]),
    }


# displayed temperature (on a TR87RF bound to a RFG100)
def parser_12c0(payload: str, msg: Message) -> PayDictT._12C0:
    if payload[2:4] == "80":
        temp: float | None = None
    elif payload[4:6] == "00":  # units are 1.0 F
        temp = int(payload[2:4], 16)
    else:  # if payload[4:] == "01":  # units are 0.5 C
        temp = int(payload[2:4], 16) / 2

    result: PayDictT._12C0 = {
        SZ_TEMPERATURE: temp,
        "units": {"00": "Fahrenheit", "01": "Celsius"}[payload[4:6]],  # type: ignore[typeddict-item]
    }
    if len(payload) > 6:
        result["_unknown_6"] = payload[6:]
    return result


# HVAC: air_quality (and air_quality_basis), see: 31DA[2:6]
def parser_12c8(payload: str, msg: Message) -> PayDictT._12C8:
    return parse_air_quality(payload[2:6])


# dhw_flow_rate
def parser_12f0(payload: str, msg: Message) -> PayDictT._12F0:
    return {SZ_DHW_FLOW_RATE: hex_to_temp(payload[2:])}


# ch_pressure
def parser_1300(payload: str, msg: Message) -> PayDictT._1300:
    # 0x9F6 (2550 dec = 2.55 bar) appears to be a sentinel value
    return {SZ_PRESSURE: None if payload[2:] == "09F6" else hex_to_temp(payload[2:])}


# programme_scheme, HVAC
def parser_1470(payload: str, msg: Message) -> dict[str, Any]:
    # Seen on Orcon: see 1470, 1F70, 22B0

    SCHEDULE_SCHEME = {
        "9": "one_per_week",
        "A": "two_per_week",  # week_day, week_end
        "B": "one_each_day",  # seven_per_week (default?)
    }

    assert payload[8:10] == "80", _INFORM_DEV_MSG
    assert msg.verb == W_ or payload[4:8] == "0E60", _INFORM_DEV_MSG
    assert msg.verb == W_ or payload[10:] == "2A0108", _INFORM_DEV_MSG
    assert msg.verb != W_ or payload[4:] == "000080000000", _INFORM_DEV_MSG

    # schedule...
    # [2:3] - 1, every/all days, 1&6, weekdays/weekends, 1-7, each individual day
    # [3:4] - # setpoints/day (default 3)
    assert payload[2:3] in SCHEDULE_SCHEME and (
        payload[3:4] in ("2", "3", "4", "5", "6")
    ), _INFORM_DEV_MSG

    return {
        "scheme": SCHEDULE_SCHEME.get(payload[2:3], f"unknown_{payload[2:3]}"),
        "daily_setpoints": payload[3:4],
        "_value_4": payload[4:8],
        "_value_8": payload[8:10],
        "_value_10": payload[10:],
    }


# system_sync
def parser_1f09(payload: str, msg: Message) -> PayDictT._1F09:
    # 22:51:19.287 067  I --- --:------ --:------ 12:193204 1F09 003 010A69
    # 22:51:19.318 068  I --- --:------ --:------ 12:193204 2309 003 010866
    # 22:51:19.321 067  I --- --:------ --:------ 12:193204 30C9 003 0108C3

    # domain_id from 01:/CTL:
    # - FF for regular sync messages
    # - 00 when responding to a request
    # - F8 after binding a device

    assert msg.len == 3, f"length is {msg.len}, expecting 3"
    assert payload[:2] in ("00", "01", F8, FF)  # W/F8

    seconds = int(payload[2:6], 16) / 10
    next_sync = msg.dtm + td(seconds=seconds)

    return {
        "remaining_seconds": seconds,
        "_next_sync": dt.strftime(next_sync, "%H:%M:%S"),
    }


# dhw_mode
def parser_1f41(payload: str, msg: Message) -> PayDictT._1F41:
    # 053 RP --- 01:145038 18:013393 --:------ 1F41 006 00FF00FFFFFF  # no stored DHW

    assert payload[4:6] in ZON_MODE_MAP, f"{payload[4:6]} (0xjj)"
    assert payload[4:6] == ZON_MODE_MAP.TEMPORARY or msg.len == 6, (
        f"{msg!r}: expected length 6"
    )
    assert payload[4:6] != ZON_MODE_MAP.TEMPORARY or msg.len == 12, (
        f"{msg!r}: expected length 12"
    )
    assert payload[6:12] == "FFFFFF", (
        f"{msg!r}: expected FFFFFF instead of '{payload[6:12]}'"
    )

    result: PayDictT._1F41 = {SZ_MODE: ZON_MODE_MAP.get(payload[4:6])}  # type: ignore[typeddict-item]
    if payload[2:4] != "FF":
        result[SZ_ACTIVE] = {"00": False, "01": True, "FF": None}[payload[2:4]]
    # if payload[4:6] == ZON_MODE_MAP.COUNTDOWN:
    #     result[SZ_UNTIL] = dtm_from_hex(payload[6:12])
    if payload[4:6] == ZON_MODE_MAP.TEMPORARY:
        result[SZ_UNTIL] = hex_to_dtm(payload[12:24])

    return result


# programme_config, HVAC
def parser_1f70(payload: str, msg: Message) -> dict[str, Any]:
    # Seen on Orcon: see 1470, 1F70, 22B0

    try:
        assert payload[:2] == "00", f"expected 00, not {payload[:2]}"
        assert payload[2:4] in ("00", "01"), f"expected (00|01), not {payload[2:4]}"
        assert payload[4:8] == "0800", f"expected 0800, not {payload[4:8]}"
        assert payload[10:14] == "0000", f"expected 0000, not {payload[10:14]}"
        assert msg.verb in (RQ, W_) or payload[14:16] == "15"
        assert msg.verb in (I_, RP) or payload[14:16] == "00"
        assert msg.verb == RQ or payload[22:24] == "60"
        assert msg.verb != RQ or payload[22:24] == "00"
        assert msg.verb == RQ or payload[24:26] in ("E4", "E5", "E6"), _INFORM_DEV_MSG
        assert msg.verb == RP or payload[26:] == "000000"
        assert msg.verb != RP or payload[26:] == "008000"

    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

        # assert int(payload[16:18], 16) < 7, _INFORM_DEV_MSG

    return {
        "day_idx": payload[16:18],  # depends upon 1470[3:4]?
        "setpoint_idx": payload[8:10],  # needs to be mod 1470[3:4]?
        "start_time": f"{int(payload[18:20], 16):02d}:{int(payload[20:22], 16):02d}",
        "fan_speed_wip": payload[24:26],  # # E4/E5/E6   / 00(RQ)
        "_value_02": payload[2:4],  # # 00/01      / 00(RQ)
        "_value_04": payload[4:8],  # # 0800
        "_value_10": payload[10:14],  # 0000
        "_value_14": payload[14:16],  # 15(RP,I)   / 00(RQ,W)
        "_value_22": payload[22:24],  # 60         / 00(RQ)
        "_value_26": payload[26:],  # # 008000(RP) / 000000(I/RQ/W)
    }


# rf_bind
def parser_1fc9(payload: str, msg: Message) -> PayDictT._1FC9:
    def _parser(seqx: str) -> list[str]:
        if seqx[:2] not in ("90",):
            assert (
                seqx[6:] == payload[6:12]  # [6:12] is repeated
            ), f"{seqx[6:]} != {payload[6:12]}"  # all with same controller
        if seqx[:2] not in (
            "21",  # HVAC, Nuaire
            "63",  # HVAC
            "65",  # HVAC, ClimaRad
            "66",  # HVAC, Vasco
            "67",  # HVAC
            "6C",  # HVAC
            "90",  # HEAT
            F6,
            F9,
            FA,
            FB,
            FC,
            FF,
        ):  # or: not in DOMAIN_TYPE_MAP: ??
            assert int(seqx[:2], 16) < 16, _INFORM_DEV_MSG
        return [seqx[:2], seqx[2:6], hex_id_to_dev_id(seqx[6:])]

    if msg.verb == I_ and msg.dst.id in (msg.src.id, ALL_DEV_ADDR.id):
        bind_phase = SZ_OFFER
    elif msg.verb == W_ and msg.src is not msg.dst:
        bind_phase = SZ_ACCEPT
    elif msg.verb == I_:
        bind_phase = SZ_CONFIRM  # len(payload) could be 2 (e.g. 00, 21)
    elif msg.verb == RP:
        bind_phase = None
    else:
        raise exc.PacketPayloadInvalid("Unknown binding format")

    if len(payload) == 2 and bind_phase == SZ_CONFIRM:
        return {SZ_PHASE: bind_phase, SZ_BINDINGS: [[payload]]}  # double-bracket OK

    assert msg.len >= 6 and msg.len % 6 == 0, msg.len  # assuming not RQ
    assert msg.verb in (I_, W_, RP), msg.verb  # devices will respond to a RQ!
    # assert (
    #     msg.src.id == hex_id_to_dev_id(payload[6:12])
    # ), f"{payload[6:12]} ({hex_id_to_dev_id(payload[6:12])})"  # NOTE: use_regex
    bindings = [
        _parser(payload[i : i + 12])
        for i in range(0, len(payload), 12)
        # if payload[i : i + 2] != "90"  # TODO: WIP, what is 90?
    ]
    return {SZ_PHASE: bind_phase, SZ_BINDINGS: bindings}


# unknown_1fca, HVAC?
def parser_1fca(payload: str, msg: Message) -> Mapping[str, str]:
    # .W --- 30:248208 34:021943 --:------ 1FCA 009 00-01FF-7BC990-FFFFFF  # sent x2

    return {
        "_unknown_0": payload[:2],
        "_unknown_1": payload[2:6],
        "device_id_0": hex_id_to_dev_id(payload[6:12]),
        "device_id_1": hex_id_to_dev_id(payload[12:]),
    }


# unknown_1fd0, from OTB
def parser_1fd0(payload: str, msg: Message) -> dict[str, Any]:
    assert payload == "0000000000000000", _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


# opentherm_sync, otb_sync
def parser_1fd4(payload: str, msg: Message) -> PayDictT._1FD4:
    return {"ticker": int(payload[2:], 16)}


# WIP: HVAC auto requests (confirmed for Orcon, others?)
def parser_2210(payload: str, msg: Message) -> dict[str, Any]:
    try:
        assert msg.verb in (RP, I_) or payload == "00"
        assert payload[10:12] == payload[38:40], (
            f"expected byte 19 {payload[10:12]}, not {payload[38:40]}"
        )  # auto requested fan speed %. Identical [38:40] is for supply?
        assert payload[20:22] == payload[48:50] and payload[20:22] in (
            "00",  # idle
            "02",  # requested by CO2 level/sensor
            "03",  # requested by humidity level/sensor
        ), f"expected req_reason (00|02|03), not {payload[20:22]}"
        assert payload[78:80] in ("00", "02"), (
            f"expected byte 39 (00|02), not {payload[78:80]}"
        )
        assert payload[80:82] in ("01", "08"), (
            f"expected byte 40 (01|08), not {payload[80:82]}"
        )
        assert payload[82:] in ("00", "40"), (
            f"expected byte 41- (00|40), not {payload[82:]}"
        )

    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    _req = "IDL"
    if payload[20:22] == "02":
        _req = "CO2"
    elif payload[20:22] == "03":
        _req = "HUM"

    return {
        **parse_exhaust_fan_speed(
            payload[10:12]
        ),  # for Orcon: 29 hex == 41 decimal divided by 2 gives 20.5 (%)
        SZ_REQ_REASON: _req,
        "unknown_78": payload[78:80],
        "unknown_80": payload[80:82],
        "unknown_82": payload[82:],
    }


# now_next_setpoint - Programmer/Hometronics
def parser_2249(payload: str, msg: Message) -> dict | list[dict]:  # TODO: only dict
    # see: https://github.com/jrosser/honeymon/blob/master/decoder.cpp#L357-L370
    # .I --- 23:100224 --:------ 23:100224 2249 007 00-7EFF-7EFF-FFFF

    def _parser(seqx: str) -> dict[str, bool | float | int | str | None]:
        minutes = int(seqx[10:], 16)
        next_setpoint = msg.dtm + td(minutes=minutes)
        return {
            "setpoint_now": hex_to_temp(seqx[2:6]),
            "setpoint_next": hex_to_temp(seqx[6:10]),
            "minutes_remaining": minutes,
            "_next_setpoint": dt.strftime(next_setpoint, "%H:%M:%S"),
        }

    # the ST9520C can support two heating zones, so: msg.len in (7, 14)?
    if msg._has_array:
        return [
            {
                SZ_ZONE_IDX: payload[i : i + 2],
                **_parser(payload[i + 2 : i + 14]),
            }
            for i in range(0, len(payload), 14)
        ]

    return _parser(payload)


# program_enabled, HVAC
def parser_22b0(payload: str, msg: Message) -> dict[str, Any]:
    # Seen on Orcon: see 1470, 1F70, 22B0

    # .W --- 37:171871 32:155617 --:------ 22B0 002 0005  # enable, calendar on
    # .I --- 32:155617 37:171871 --:------ 22B0 002 0005

    # .W --- 37:171871 32:155617 --:------ 22B0 002 0006  # disable, calendar off
    # .I --- 32:155617 37:171871 --:------ 22B0 002 0006

    return {
        "enabled": {"06": False, "05": True}.get(payload[2:4]),
    }


# setpoint_bounds, TODO: max length = 24?
def parser_22c9(payload: str, msg: Message) -> dict | list[dict]:  # TODO: only dict
    # .I --- 02:001107 --:------ 02:001107 22C9 024 00-0834-0A28-01-0108340A2801-0208340A2801-0308340A2801  # noqa: E501
    # .I --- 02:001107 --:------ 02:001107 22C9 006 04-0834-0A28-01

    # .I --- 21:064743 --:------ 21:064743 22C9 006 00-07D0-0834-02
    # .W --- 21:064743 02:250708 --:------ 22C9 006 03-07D0-0834-02
    # .I --- 02:250708 21:064743 --:------ 22C9 008 03-07D0-7FFF-020203

    # Notes on 008|suffix: only seen as I, only when no array, only as 7FFF(0101|0202)03$

    def _parser(seqx: str) -> dict:
        assert seqx[10:] in ("01", "02"), f"is {seqx[10:]}, expecting 01 or 02"

        return {
            SZ_MODE: {"01": "heat", "02": "cool"}[seqx[10:]],  # TODO: or action?
            SZ_SETPOINT_BOUNDS: (hex_to_temp(seqx[2:6]), hex_to_temp(seqx[6:10])),
        }  # lower, upper setpoints

    if msg._has_array:
        return [
            {
                SZ_UFH_IDX: payload[i : i + 2],
                **_parser(payload[i : i + 12]),
            }
            for i in range(0, len(payload), 12)
        ]

    assert msg.len != 8 or payload[10:] in ("010103", "020203"), _INFORM_DEV_MSG

    return _parser(payload[:12])


# unknown_22d0, UFH system mode (heat/cool)
def parser_22d0(payload: str, msg: Message) -> dict[str, Any]:
    def _parser(seqx: str) -> dict:
        # assert seqx[2:4] in ("00", "03", "10", "13", "14"), _INFORM_DEV_MSG
        assert seqx[4:6] == "00", _INFORM_DEV_MSG
        return {
            "idx": seqx[:2],
            "_flags": hex_to_flag8(seqx[2:4]),
            "cool_mode": bool(int(seqx[2:4], 16) & 0x02),
            "heat_mode": bool(int(seqx[2:4], 16) & 0x04),
            "is_active": bool(int(seqx[2:4], 16) & 0x10),
            "_unknown": payload[4:],
        }

    if len(payload) == 8:
        assert payload[6:] in ("00", "02", "0A"), _INFORM_DEV_MSG
    else:
        assert payload[4:] == "001E14030020", _INFORM_DEV_MSG

    return _parser(payload)


# desired boiler setpoint
def parser_22d9(payload: str, msg: Message) -> PayDictT._22D9:
    return {SZ_SETPOINT: hex_to_temp(payload[2:6])}


# WIP: unknown, HVAC
def parser_22e0(payload: str, msg: Message) -> Mapping[str, float | None]:
    # RP --- 32:155617 18:005904 --:------ 22E0 004 00-34-A0-1E
    # RP --- 32:153258 18:005904 --:------ 22E0 004 00-64-A0-1E
    def _parser(seqx: str) -> float:
        assert int(seqx, 16) <= 200 or seqx == "E6"  # only for 22E0, not 22E5/22E9
        return int(seqx, 16) / 200

    try:
        return {
            f"percent_{i}": hex_to_percent(payload[i : i + 2])
            for i in range(2, len(payload), 2)
        }
    except ValueError:
        return {
            "percent_2": hex_to_percent(payload[2:4]),
            "percent_4": _parser(payload[4:6]),
            "percent_6": hex_to_percent(payload[6:8]),
        }


# WIP: unknown, HVAC
def parser_22e5(payload: str, msg: Message) -> Mapping[str, float | None]:
    # RP --- 32:153258 18:005904 --:------ 22E5 004 00-96-C8-14
    # RP --- 32:155617 18:005904 --:------ 22E5 004 00-72-C8-14

    return parser_22e0(payload, msg)


# WIP: unknown, HVAC
def parser_22e9(payload: str, msg: Message) -> Mapping[str, float | str | None]:
    if payload[2:4] == "01":
        return {
            "unknown_4": payload[4:6],
            "unknown_6": payload[6:8],
        }
    return parser_22e0(payload, msg)


# fan_speed (switch_mode), HVAC
def parser_22f1(payload: str, msg: Message) -> dict[str, Any]:
    try:
        assert payload[0:2] in ("00", "63")
        assert not payload[4:] or int(payload[2:4], 16) <= int(payload[4:], 16), (
            "mode_idx > mode_max"
        )
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    if msg._addrs[0] == NON_DEV_ADDR:  # and payload[4:6] == "04":
        from .ramses import _22F1_MODE_ITHO as _22F1_FAN_MODE  # TODO: only if 04

        _22f1_mode_set: tuple[str, ...] = ("", "04")
        _22f1_scheme = "itho"

    # elif msg._addrs[0] == NON_DEV_ADDR:  # and payload[4:6] == "04":
    #     _22F1_FAN_MODE = {
    #         f"{x:02X}": f"speed_{x}" for x in range(int(payload[4:6], 16) + 1)
    #     } | {"00": "off"}

    #     _22f1_mode_set = (payload[4:6], )
    #     _22f1_scheme = "itho_2"

    elif payload[4:6] == "0A":
        from .ramses import _22F1_MODE_NUAIRE as _22F1_FAN_MODE

        _22f1_mode_set = ("", "0A")
        _22f1_scheme = "nuaire"

    elif payload[4:6] == "06":
        from .ramses import _22F1_MODE_VASCO as _22F1_FAN_MODE

        _22f1_mode_set = (
            "",
            "00",
            "06",
        )  # "00" seen incidentally on a ClimaRad 4-button remote: OFF?
        _22f1_scheme = "vasco"

    else:
        from .ramses import _22F1_MODE_ORCON as _22F1_FAN_MODE

        _22f1_mode_set = ("", "04", "07", "0B")  # 0B?
        _22f1_scheme = "orcon"

    try:
        assert payload[2:4] in _22F1_FAN_MODE, f"unknown fan_mode: {payload[2:4]}"
        assert payload[4:6] in _22f1_mode_set, f"unknown mode_set: {payload[4:6]}"
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    return {
        SZ_FAN_MODE: _22F1_FAN_MODE.get(payload[2:4], f"unknown_{payload[2:4]}"),
        "_scheme": _22f1_scheme,
        "_mode_idx": f"{int(payload[2:4], 16) & 0x0F:02X}",
        "_mode_max": payload[4:6] or None,
        # "_payload": payload,
    }


# WIP: unknown, HVAC (flow rate?)
def parser_22f2(payload: str, msg: Message) -> list:  # TODO: only dict
    # ClimeRad minibox uses 22F2 for speed feedback

    def _parser(seqx: str) -> dict:
        assert seqx[:2] in ("00", "01"), f"is {seqx[:2]}, expecting 00/01"

        return {
            "hvac_idx": seqx[:2],
            "measure": hex_to_temp(seqx[2:]),
        }

    return [_parser(payload[i : i + 6]) for i in range(0, len(payload), 6)]


# fan_boost, HVAC
def parser_22f3(payload: str, msg: Message) -> dict[str, Any]:
    # NOTE: for boost timer for high
    try:
        assert msg.len <= 7 or payload[14:] == "0000", f"byte 7: {payload[14:]}"
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    new_speed = {  # from now, until timer expiry
        0x00: "fan_boost",  # #    set fan off, or 'boost' mode?
        0x01: "per_request",  # #  set fan as per payload[6:10]?
        0x02: "per_vent_speed",  # set fan as per current fan mode/speed?
    }.get(int(payload[2:4], 0x10) & 0x07)  # 0b0000-0111

    fallback_speed: str | None
    if msg.len == 7 and payload[9:10] == "06":  # Vasco and ClimaRad REM
        fallback_speed = "per_vent_speed"  # after timer expiry
        # set fan as per current fan mode/speed
    else:
        fallback_speed = {  # after timer expiry
            0x08: "fan_off",  # #      set fan off?
            0x10: "per_request",  # #  set fan as per payload[6:10], or payload[10:]?
            0x18: "per_vent_speed",  # set fan as per current fan mode/speed?
        }.get(int(payload[2:4], 0x10) & 0x38)  # 0b0011-1000

    units = {
        0x00: "minutes",
        0x40: "hours",
        0x80: "index",  # TODO: days, day-of-week, day-of-month?
    }.get(int(payload[2:4], 0x10) & 0xC0)  # 0b1100-0000

    duration = int(payload[4:6], 16) * 60 if units == "hours" else int(payload[4:6], 16)
    result = {}

    if msg.len >= 3:
        result = {
            "minutes" if units != "index" else "index": duration,
            "flags": hex_to_flag8(payload[2:4]),
            "_new_speed_mode": new_speed,
            "_fallback_speed_mode": fallback_speed,
        }

    if msg.len >= 5 and payload[6:10] != "0000":  # new speed?
        result["rate"] = parser_22f1(f"00{payload[6:10]}", msg).get("rate")

    if msg.len >= 7:  # fallback speed?
        result.update({"_unknown_5": payload[10:]})

    return result


# WIP: unknown, HVAC
def parser_22f4(payload: str, msg: Message) -> dict[str, Any]:
    if msg.len == 13 and payload[14:] == "000000000000":
        # ClimaRad Ventura fan & remote
        _pl = payload[:4] + payload[12:14] if payload[10:12] == "00" else payload[8:14]
    else:
        _pl = payload[:6]

    MODE_LOOKUP = {
        0x00: "off",
        0x20: "paused",
        0x40: "auto",
        0x60: "manual",
    }
    mode = int(_pl[2:4], 16) & 0x60
    assert mode in MODE_LOOKUP, mode

    RATE_LOOKUP = {
        0x00: "speed 0",  # "off"?,
        0x01: "speed 1",  # "low", or trickle?
        0x02: "speed 2",  # "medium-low", or low?
        0x03: "speed 3",  # "medium",
        0x04: "speed 4",  # "medium-high", or high?
        0x05: "boost",  # "boost", aka purge?
    }
    rate = int(_pl[4:6], 16) & 0x03
    assert mode != 0x60 or rate in RATE_LOOKUP, rate

    return {
        SZ_FAN_MODE: MODE_LOOKUP[mode],
        SZ_FAN_RATE: RATE_LOOKUP.get(rate),
    }


# bypass_mode, HVAC
def parser_22f7(payload: str, msg: Message) -> dict[str, Any]:
    result = {
        SZ_BYPASS_MODE: {"00": "off", "C8": "on", "FF": "auto"}.get(payload[2:4]),
    }
    if msg.verb != W_ or payload[4:] not in ("", "EF"):
        result[SZ_BYPASS_STATE] = {"00": "off", "C8": "on"}.get(payload[4:])
        result.update(**parse_bypass_position(payload[4:]))  # type: ignore[arg-type]

    return result


# WIP: unknown_mode, HVAC
def parser_22f8(payload: str, msg: Message) -> dict[str, Any]:
    # from: https://github.com/arjenhiemstra/ithowifi/blob/master/software/NRG_itho_wifi/src/IthoPacket.h

    # message command bytes specific for AUTO RFT (536-0150)
    # ithoMessageAUTORFTAutoNightCommandBytes[] = {0x22, 0xF8, 0x03, 0x63, 0x02, 0x03};
    # .W --- 32:111111 37:111111 --:------ 22F8 003 630203

    # message command bytes specific for DemandFlow remote (536-0146)
    # ithoMessageDFLowCommandBytes[] = {0x22, 0xF8, 0x03, 0x00, 0x01, 0x02};
    # ithoMessageDFHighCommandBytes[] = {0x22, 0xF8, 0x03, 0x00, 0x02, 0x02};

    return {
        "value_02": payload[2:4],
        "value_04": payload[4:6],
    }


# setpoint (of device/zones)
def parser_2309(
    payload: str, msg: Message
) -> PayDictT._2309 | list[PayDictT._2309] | PayDictT.EMPTY:
    if msg._has_array:
        return [
            {
                SZ_ZONE_IDX: payload[i : i + 2],
                SZ_SETPOINT: hex_to_temp(payload[i + 2 : i + 6]),
            }
            for i in range(0, len(payload), 6)
        ]

    # RQ --- 22:131874 01:063844 --:------ 2309 003 020708
    if msg.verb == RQ and msg.len == 1:  # some RQs have a payload (why?)
        return {}

    return {SZ_SETPOINT: hex_to_temp(payload[2:])}


# zone_mode  # TODO: messy
def parser_2349(payload: str, msg: Message) -> PayDictT._2349 | PayDictT.EMPTY:
    # RQ --- 34:225071 30:258557 --:------ 2349 001 00
    # RP --- 30:258557 34:225071 --:------ 2349 013 007FFF00FFFFFFFFFFFFFFFFFF
    # RP --- 30:253184 34:010943 --:------ 2349 013 00064000FFFFFF00110E0507E5
    # .I --- 10:067219 --:------ 10:067219 2349 004 00000001

    if msg.verb == RQ and msg.len <= 2:  # some RQs have a payload (why?)
        return {}

    assert msg.len in (7, 13), f"expected len 7,13, got {msg.len}"

    assert payload[6:8] in ZON_MODE_MAP, f"unknown zone_mode: {payload[6:8]}"
    result: PayDictT._2349 = {
        SZ_MODE: ZON_MODE_MAP.get(payload[6:8]),  # type: ignore[typeddict-item]
        SZ_SETPOINT: hex_to_temp(payload[2:6]),
    }

    if msg.len >= 7:  # has a dtm if mode == "04"
        if payload[8:14] == "FF" * 3:  # 03/FFFFFF OK if W?
            assert payload[6:8] != ZON_MODE_MAP.COUNTDOWN, f"{payload[6:8]} (0x00)"
        else:
            assert payload[6:8] == ZON_MODE_MAP.COUNTDOWN, f"{payload[6:8]} (0x01)"
            result[SZ_DURATION] = int(payload[8:14], 16)

    if msg.len >= 13:
        if payload[14:] == "FF" * 6:
            assert payload[6:8] in (
                ZON_MODE_MAP.FOLLOW,
                ZON_MODE_MAP.PERMANENT,
            ), f"{payload[6:8]} (0x02)"
            result[SZ_UNTIL] = None  # TODO: remove?
        else:
            assert payload[6:8] != ZON_MODE_MAP.PERMANENT, f"{payload[6:8]} (0x03)"
            result[SZ_UNTIL] = hex_to_dtm(payload[14:26])

    return result


# unknown_2389, from 03:
def parser_2389(payload: str, msg: Message) -> dict[str, Any]:
    return {
        "_unknown": hex_to_temp(payload[2:6]),
    }


# unknown_2400, from OTB, FAN
def parser_2400(payload: str, msg: Message) -> dict[str, Any]:
    # RP --- 32:155617 18:005904 --:------ 2400 045 00001111-1010929292921110101020110010000080100010100000009191111191910011119191111111111100  # Orcon FAN
    # RP --- 10:048122 18:006402 --:------ 2400 004 0000000F
    # assert payload == "0000000F", _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


# unknown_2401, from OTB
def parser_2401(payload: str, msg: Message) -> dict[str, Any]:
    try:
        assert payload[2:4] == "00", f"byte 1: {payload[2:4]}"
        assert int(payload[4:6], 16) & 0b11110000 == 0, (
            f"byte 2: {hex_to_flag8(payload[4:6])}"
        )
        assert int(payload[6:], 0x10) <= 200, f"byte 3: {payload[6:]}"
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    return {
        "_flags_2": hex_to_flag8(payload[4:6]),
        **parse_valve_demand(payload[6:8]),  # ~3150|FC
        "_value_2": int(payload[4:6], 0x10),
    }


# unknown_2410, from OTB, FAN
def parser_2410(payload: str, msg: Message) -> dict[str, Any]:
    # RP --- 10:048122 18:006402 --:------ 2410 020 00-00000000-00000000-00000001-00000001-00000C  # OTB
    # RP --- 32:155617 18:005904 --:------ 2410 020 00-00003EE8-00000000-FFFFFFFF-00000000-1002A6  # Orcon Fan

    def unstuff(seqx: str) -> tuple:
        val = int(seqx, 16)
        # if val & 0x40:
        #     raise TypeError
        signed = bool(val & 0x80)
        length = (val >> 3 & 0x07) or 1
        d_type = {0b000: "a", 0b001: "b", 0b010: "c", 0b100: "d"}.get(
            val & 0x07, val & 0x07
        )
        return signed, length, d_type

    try:
        assert payload[:6] == "00" * 3, _INFORM_DEV_MSG
        assert payload[10:18] == "00" * 4, _INFORM_DEV_MSG
        assert payload[18:26] in ("00000001", "FFFFFFFF"), _INFORM_DEV_MSG
        assert payload[26:34] in ("00000001", "00000000"), _INFORM_DEV_MSG
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    return {
        "tail": payload[34:],
        "xxx_34": unstuff(payload[34:36]),
        "xxx_36": unstuff(payload[36:38]),
        "xxx_38": unstuff(payload[38:]),
        "cur_value": payload[2:10],
        "min_value": payload[10:18],
        "max_value": payload[18:26],
        "oth_value": payload[26:34],
    }


# fan_params, HVAC
def parser_2411(payload: str, msg: Message) -> dict[str, Any]:
    # There is a relationship between 0001 and 2411
    # RQ --- 37:171871 32:155617 --:------ 0001 005 0020000A04
    # RP --- 32:155617 37:171871 --:------ 0001 008 0020000A004E0B00  # 0A -> 2411|4E
    # RQ --- 37:171871 32:155617 --:------ 2411 003 00004E            # 11th menu option (i.e. 0x0A)
    # RP --- 32:155617 37:171871 --:------ 2411 023 00004E460000000001000000000000000100000001A600

    def counter(x: str) -> int:
        return int(x, 16)

    def centile(x: str) -> float:
        return int(x, 16) / 10

    _2411_DATA_TYPES = {
        "00": (2, counter),  # 4E (0-1), 54 (15-60)
        "01": (2, centile),  # 52 (0.0-25.0) (%)
        "0F": (2, hex_to_percent),  # xx (0.0-1.0) (%)
        "10": (4, counter),  # 31 (0-1800) (days)
        "92": (4, hex_to_temp),  # 75 (0-30) (C)
    }  # TODO: _2411_TYPES.get(payload[8:10], (8, no_op))

    assert payload[4:6] in _2411_TABLE, (
        f"param {payload[4:6]} is unknown"
    )  # _INFORM_DEV_MSG
    description = _2411_TABLE.get(payload[4:6], "Unknown")

    result = {
        "parameter": payload[4:6],
        "description": description,
    }

    if msg.verb == RQ:
        return result

    assert payload[8:10] in _2411_DATA_TYPES, (
        f"param {payload[4:6]} has unknown data_type: {payload[8:10]}"
    )  # _INFORM_DEV_MSG
    length, parser = _2411_DATA_TYPES.get(payload[8:10], (8, lambda x: x))

    result |= {
        "value": parser(payload[10:18][-length:]),  # type: ignore[operator]
        "_value_06": payload[6:10],
    }

    if msg.len == 9:
        return result

    return (
        result
        | {
            "min_value": parser(payload[18:26][-length:]),  # type: ignore[operator]
            "max_value": parser(payload[26:34][-length:]),  # type: ignore[operator]
            "precision": parser(payload[34:42][-length:]),  # type: ignore[operator]
            "_value_42": payload[42:],
        }
    )


# unknown_2420, from OTB
def parser_2420(payload: str, msg: Message) -> dict[str, Any]:
    assert payload == "00000010" + "00" * 34, _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


# _state (of cooling?), from BDR91T, hometronics CTL
def parser_2d49(payload: str, msg: Message) -> PayDictT._2D49:
    assert payload[2:] in ("0000", "00FF", "C800", "C8FF"), _INFORM_DEV_MSG

    return {
        "state": hex_to_bool(payload[2:4]),
    }


# system_mode
def parser_2e04(payload: str, msg: Message) -> PayDictT._2E04:
    # if msg.verb == W_:

    # .I --— 01:020766 --:------ 01:020766 2E04 016 FFFFFFFFFFFFFF0007FFFFFFFFFFFF04  # Manual          # noqa: E501
    # .I --— 01:020766 --:------ 01:020766 2E04 016 FFFFFFFFFFFFFF0000FFFFFFFFFFFF04  # Automatic/times # noqa: E501

    if msg.len == 8:  # evohome
        assert payload[:2] in SYS_MODE_MAP, f"Unknown system mode: {payload[:2]}"

    elif msg.len == 16:  # hometronics, lifestyle ID:
        assert 0 <= int(payload[:2], 16) <= 15 or payload[:2] == FF, payload[:2]
        assert payload[16:18] in (SYS_MODE_MAP.AUTO, SYS_MODE_MAP.CUSTOM), payload[
            16:18
        ]
        assert payload[30:32] == SYS_MODE_MAP.DAY_OFF, payload[30:32]
        # assert False

    else:
        # msg.len in (8, 16)  # evohome 8, hometronics 16
        assert False, f"Packet length is {msg.len} (expecting 8, 16)"

    result: PayDictT._2E04 = {SZ_SYSTEM_MODE: SYS_MODE_MAP[payload[:2]]}
    if payload[:2] not in (
        SYS_MODE_MAP.AUTO,
        SYS_MODE_MAP.HEAT_OFF,
        SYS_MODE_MAP.AUTO_WITH_RESET,
    ):
        result.update(
            {SZ_UNTIL: hex_to_dtm(payload[2:14]) if payload[14:16] != "00" else None}
        )
    return result  # TODO: double-check the final "00"


# presence_detect, HVAC sensor, or Timed boost for Vasco D60
def parser_2e10(payload: str, msg: Message) -> dict[str, Any]:
    assert payload in ("0001", "000000", "000100"), _INFORM_DEV_MSG
    presence: int = int(payload[2:4])
    return {
        "presence_detected": bool(presence),
        "_unknown_4": payload[4:],
    }


# current temperature (of device, zone/s)
def parser_30c9(payload: str, msg: Message) -> dict | list[dict]:  # TODO: only dict
    if msg._has_array:
        return [
            {
                SZ_ZONE_IDX: payload[i : i + 2],
                SZ_TEMPERATURE: hex_to_temp(payload[i + 2 : i + 6]),
            }
            for i in range(0, len(payload), 6)
        ]

    return {SZ_TEMPERATURE: hex_to_temp(payload[2:])}


# ufc_demand, HVAC (Itho autotemp / spider)
def parser_3110(payload: str, msg: Message) -> PayDictT._3110:
    # .I --- 02:250708 --:------ 02:250708 3110 004 0000C820  # cooling, 100%
    # .I --- 21:042656 --:------ 21:042656 3110 004 00000010  # heating, 0%

    SZ_COOLING = "cooling"
    SZ_DISABLE = "disabled"
    SZ_HEATING = "heating"
    SZ_UNKNOWN = "unknown"

    try:
        assert payload[2:4] == "00", f"byte 1: {payload[2:4]}"  # ?circuit_idx?
        assert int(payload[4:6], 16) <= 200, f"byte 2: {payload[4:6]}"
        assert payload[6:] in ("00", "10", "20"), f"byte 3: {payload[6:]}"
        assert payload[6:] in ("10", "20") or payload[4:6] == "00", (
            f"byte 3: {payload[6:]}"
        )
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    mode = {
        0x00: SZ_DISABLE,
        0x10: SZ_HEATING,
        0x20: SZ_COOLING,
    }.get(int(payload[6:8], 16) & 0x30, SZ_UNKNOWN)

    if mode not in (SZ_COOLING, SZ_HEATING):
        return {SZ_MODE: mode}

    return {SZ_MODE: mode, SZ_DEMAND: hex_to_percent(payload[4:6])}


# unknown_3120, from STA, FAN
def parser_3120(payload: str, msg: Message) -> dict[str, Any]:
    # .I --- 34:136285 --:------ 34:136285 3120 007 0070B0000000FF  # every ~3:45:00!
    # RP --- 20:008749 18:142609 --:------ 3120 007 0070B000009CFF
    # .I --- 37:258565 --:------ 37:258565 3120 007 0080B0010003FF

    try:
        assert payload[:2] == "00", f"byte 0: {payload[:2]}"
        assert payload[2:4] in ("00", "70", "80"), f"byte 1: {payload[2:4]}"
        assert payload[4:6] == "B0", f"byte 2: {payload[4:6]}"
        assert payload[6:8] in ("00", "01"), f"byte 3: {payload[6:8]}"
        assert payload[8:10] == "00", f"byte 4: {payload[8:10]}"
        assert payload[10:12] in ("00", "03", "0A", "9C"), f"byte 5: {payload[10:12]}"
        assert payload[12:] == "FF", f"byte 6: {payload[12:]}"
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    return {
        "unknown_0": payload[2:10],
        "unknown_5": payload[10:12],
        "unknown_2": payload[12:],
    }


# WIP: unknown, HVAC
def parser_313e(payload: str, msg: Message) -> dict[str, Any]:
    assert payload[:2] == "00"
    assert payload[12:] == "003C800000"

    result = (
        msg.dtm - td(seconds=int(payload[10:12], 16), minutes=int(payload[2:10], 16))
    ).isoformat()

    return {
        "zulu": result,
        "value_02": payload[2:10],
        "value_10": payload[10:12],
        "value_12": payload[12:],
    }


# datetime
def parser_313f(payload: str, msg: Message) -> PayDictT._313F:  # TODO: look for TZ
    # 2020-03-28T03:59:21.315178 045 RP --- 01:158182 04:136513 --:------ 313F 009 00FC3500A41C0307E4
    # 2020-03-29T04:58:30.486343 045 RP --- 01:158182 04:136485 --:------ 313F 009 00FC8400C51D0307E4
    # 2022-09-20T20:50:32.800676 065 RP --- 01:182924 18:068640 --:------ 313F 009 00F9203234140907E6
    # 2020-05-31T11:37:50.351511 056  I --- --:------ --:------ 12:207082 313F 009 0038021ECB1F0507E4

    # https://www.automatedhome.co.uk/vbulletin/showthread.php?5085-My-HGI80-equivalent-Domoticz-setup-without-HGI80&p=36422&viewfull=1#post36422
    # every day at ~4am TRV/RQ->CTL/RP, approx 5-10secs apart (CTL respond at any time)

    assert msg.src.type != DEV_TYPE_MAP.CTL or payload[2:4] in (
        "F0",
        "F9",
        "FC",
    ), f"{payload[2:4]} unexpected for CTL"  # DEX
    assert (
        msg.src.type not in (DEV_TYPE_MAP.DTS, DEV_TYPE_MAP.DT2) or payload[2:4] == "38"
    ), f"{payload[2:4]} unexpected for DTS"  # DEX
    # assert (
    #     msg.src.type != DEV_TYPE_MAP.FAN or payload[2:4] == "7C"
    # ), f"{payload[2:4]} unexpected for FAN"  # DEX
    assert msg.src.type != DEV_TYPE_MAP.RFG or payload[2:4] == "60", (
        "{payload[2:4]} unexpected for RFG"
    )  # DEX

    return {
        SZ_DATETIME: hex_to_dtm(payload[4:18]),
        SZ_IS_DST: True if bool(int(payload[4:6], 16) & 0x80) else None,
        "_unknown_0": payload[2:4],
    }


# heat_demand (of device, FC domain) - valve status (%open)
def parser_3150(payload: str, msg: Message) -> dict | list[dict]:  # TODO: only dict
    # event-driven, and periodically; FC domain is maximum of all zones
    # TODO: all have a valid domain will UFC/CTL respond to an RQ, for FC, for a zone?

    # .I --- 04:136513 --:------ 01:158182 3150 002 01CA < often seen CA, artefact?

    def complex_idx(seqx: str, msg: Message) -> dict[str, str]:
        # assert seqx[:2] == FC or (int(seqx[:2], 16) < MAX_ZONES)  # <5, 8 for UFC
        idx_name = "ufx_idx" if msg.src.type == DEV_TYPE_MAP.UFC else SZ_ZONE_IDX  # DEX
        return {SZ_DOMAIN_ID if seqx[:1] == "F" else idx_name: seqx[:2]}

    if msg._has_array:
        return [
            {
                **complex_idx(payload[i : i + 2], msg),
                **parse_valve_demand(payload[i + 2 : i + 4]),
            }
            for i in range(0, len(payload), 4)
        ]

    return parse_valve_demand(payload[2:])  # TODO: check UFC/FC is == CTL/FC


# fan state (ventilation status), HVAC
def parser_31d9(payload: str, msg: Message) -> dict[str, Any]:
    # NOTE: Itho and ClimaRad use 0x00-C8 for %, whilst Nuaire uses 0x00-64
    try:
        assert payload[4:6] == "FF" or int(payload[4:6], 16) <= 200, (
            f"byte 2: {payload[4:6]}"
        )
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    bitmap = int(payload[2:4], 16)

    # NOTE: 31D9[4:6] is fan_speed (ClimaRad minibox, Itho) *or* fan_mode (Orcon, Vasco)
    result = {
        **parse_exhaust_fan_speed(payload[4:6]),  # for itho
        SZ_FAN_MODE: payload[4:6],  # orcon, vasco/climarad
        "passive": bool(bitmap & 0x02),
        "damper_only": bool(bitmap & 0x04),  # i.e. valve only
        "filter_dirty": bool(bitmap & 0x20),
        "frost_cycle": bool(bitmap & 0x40),
        "has_fault": bool(bitmap & 0x80),
        "_flags": hex_to_flag8(payload[2:4]),
    }

    # Fan Mode Lookup 1 for Vasco codes
    if msg.len == 3:  # usu: I -->20: (no seq#)
        if (
            (payload[:4] == "0000" or payload[:4] == "0080")  # Senza, meaning of 0x80?
            and msg._addrs[0] == msg._addrs[2]
            and msg._addrs[1] == NON_DEV_ADDR
        ):
            # _31D9_FAN_INFO for Vasco D60 HRU and ClimaRad Minibox, S-Fan, (REM: RQ only, msg.len==1)
            try:
                assert int(payload[4:6], 16) & 0xFF in _31D9_FAN_INFO_VASCO, (
                    f"unknown 31D9 fan_mode lookup key: {payload[4:6]}"
                )
            except AssertionError as err:
                _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")
            fan_mode = _31D9_FAN_INFO_VASCO.get(
                int(payload[4:6], 16) & 0xFF, f"unknown_{payload[4:6]}"
            )
            result[SZ_FAN_MODE] = fan_mode  # replace
            # if not replaced, 31D9 FAN_MODE is a 2 digit string HEX
        return result

    try:
        assert payload[6:8] in ("00", "07", "0A", "FE"), f"byte 3: {payload[6:8]}"
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    result.update({"_unknown_3": payload[6:8]})

    if msg.len == 4:  # usu: I -->20: (no seq#)
        return result

    try:
        assert payload[8:32] in ("00" * 12, "20" * 12), f"byte 4: {payload[8:32]}"
        assert payload[32:] in ("00", "04", "08"), f"byte 16: {payload[32:]}"
    except AssertionError as err:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({err})")

    return {
        **result,
        "_unknown_4": payload[8:32],
        "unknown_16": payload[32:],
    }


# ventilation state (extended), HVAC
def parser_31da(payload: str, msg: Message) -> PayDictT._31DA:
    # see: https://github.com/python/typing/issues/1445
    return {  # type: ignore[typeddict-unknown-key]
        **parse_exhaust_fan_speed(payload[38:40]),  # maybe 31D9[4:6] for some?
        **parse_fan_info(payload[36:38]),  # 22F3-ish
        #
        **parse_air_quality(payload[2:6]),  # 12C8[2:6]
        **parse_co2_level(payload[6:10]),  # 1298[2:6]
        **parse_indoor_humidity(payload[10:12]),  # 12A0?
        **parse_outdoor_humidity(payload[12:14]),
        **parse_exhaust_temp(payload[14:18]),  # to outside
        **parse_supply_temp(payload[18:22]),  # to home
        **parse_indoor_temp(payload[22:26]),  # in home
        **parse_outdoor_temp(payload[26:30]),  # 1290?
        **parse_capabilities(payload[30:34]),
        **parse_bypass_position(payload[34:36]),  # 22F7-ish
        **parse_supply_fan_speed(payload[40:42]),
        **parse_remaining_mins(payload[42:46]),  # mins, ~22F3[2:6]
        **parse_post_heater(payload[46:48]),
        **parse_pre_heater(payload[48:50]),
        **parse_supply_flow(payload[50:54]),  # NOTE: is supply, not exhaust
        **parse_exhaust_flow(payload[54:58]),  # NOTE: order switched from others
    }

    # From an Orcon 15RF Display
    #  1 Software version
    #  4 RH value in home (%)                 SZ_INDOOR_HUMIDITY
    #  5 RH value supply air (%)              SZ_OUTDOOR_HUMIDITY
    #  6 Exhaust air temperature out (°C)     SZ_EXHAUST_TEMPERATURE
    #  7 Supply air temperature to home (°C)  SZ_SUPPLY_TEMPERATURE
    #  8 Temperature from home (°C)           SZ_INDOOR_TEMPERATURE
    #  9 Temperature outside (°C)             SZ_OUTDOOR_TEMPERATURE
    # 10 Bypass position                      SZ_BYPASS_POSITION
    # 11 Exhaust fan speed (%)                SZ_EXHAUST_FAN_SPEED
    # 12 Fan supply speed (%)                 SZ_SUPPLY_FAN_SPEED
    # 13 Remaining after run time (min.)      SZ_REMAINING_TIME - for humidity scenario
    # 14 Preheater control (MaxComfort) (%)   SZ_PRE_HEAT
    # 16 Actual supply flow rate (m3/h)       SZ_SUPPLY_FLOW (Orcon is m3/h, data is L/s)
    # 17 Current discharge flow rate (m3/h)   SZ_EXHAUST_FLOW


# vent_demand, HVAC
def parser_31e0(payload: str, msg: Message) -> dict | list[dict]:  # TODO: only dict
    """Notes are.

    van means “of”.
    - 0 = min. van min. potm would be:
    - 0 = minimum of minimum potentiometer

    See: https://www.industrialcontrolsonline.com/honeywell-t991a
    - modulates air temperatures in ducts

    case 0x31E0:  ' 12768:
    {
        string str4;
        unchecked
        {
            result.Fan = Conversions.ToString((double)(int)data[checked(start + 1)] / 2.0);
            str4 = "";
        }
        str4 = (data[start + 2] & 0xF) switch
        {
            0 => str4 + "0 = min. potm. ",
            1 => str4 + "0 = min. van min. potm ",
            2 => str4 + "0 = min. fan ",
            _ => "",
        };
        switch (data[start + 2] & 0xF0)
        {
        case 16:
            str4 += "100 = max. potm";
            break;
        case 32:
            str4 += "100 = max. van max. potm ";
            break;
        case 48:
            str4 += "100 = max. fan ";
            break;
        }
        result.Data = str4;
        break;
    }
    """

    # .I --- 37:005302 32:132403 --:------ 31E0 008 00-0000-00 01-0064-00  # RF15 CO2 to Orcon HRC400 series SmartComfort Valve

    # .I --- 29:146052 32:023459 --:------ 31E0 003 00-0000
    # .I --- 29:146052 32:023459 --:------ 31E0 003 00-00C8

    # .I --- 32:168240 30:079129 --:------ 31E0 004 00-0000-FF
    # .I --- 32:168240 30:079129 --:------ 31E0 004 00-0000-FF
    # .I --- 32:166025 --:------ 30:079129 31E0 004 00-0000-00

    # .I --- 32:168090 30:082155 --:------ 31E0 004 00-00C8-00
    # .I --- 37:258565 37:261128 --:------ 31E0 004 00-0001-00

    def _parser(seqx: str) -> dict:
        assert seqx[6:] in ("", "00", "FF")
        return {
            # "hvac_idx": seqx[:2],
            "flags": seqx[2:4],
            "vent_demand": hex_to_percent(seqx[4:6]),
            "_unknown_3": payload[6:],
        }

    if len(payload) > 8:
        return [_parser(payload[x : x + 8]) for x in range(0, len(payload), 8)]
    return _parser(payload)


# supplied boiler water (flow) temp
def parser_3200(payload: str, msg: Message) -> PayDictT._3200:
    return {SZ_TEMPERATURE: hex_to_temp(payload[2:])}


# return (boiler) water temp
def parser_3210(payload: str, msg: Message) -> PayDictT._3210:
    return {SZ_TEMPERATURE: hex_to_temp(payload[2:])}


# opentherm_msg, from OTB (and some RND)
def parser_3220(payload: str, msg: Message) -> dict[str, Any]:
    try:
        ot_type, ot_id, ot_value, ot_schema = decode_frame(payload[2:10])
    except AssertionError as err:
        raise AssertionError(f"OpenTherm: {err}") from err
    except ValueError as err:
        raise exc.PacketPayloadInvalid(f"OpenTherm: {err}") from err

    # NOTE: Unknown-DataId isn't an invalid payload & is useful to train the OTB device
    if ot_schema is None and ot_type != OtMsgType.UNKNOWN_DATAID:  # type: ignore[unreachable]
        raise exc.PacketPayloadInvalid(
            f"OpenTherm: Unknown data-id: 0x{ot_id:02X} ({ot_id})"
        )

    result = {
        SZ_MSG_ID: ot_id,
        SZ_MSG_TYPE: str(ot_type),
        SZ_MSG_NAME: ot_value.pop(SZ_MSG_NAME, None),
    }

    if msg.verb == RQ:  # RQs have a context: msg_id (and a payload)
        assert (
            ot_type != OtMsgType.READ_DATA
            or payload[6:10] == "0000"  # likely true for RAMSES
        ), f"OpenTherm: Invalid msg-type|data-value: {ot_type}|{payload[6:10]}"

        if ot_type != OtMsgType.READ_DATA:
            assert ot_type in (
                OtMsgType.WRITE_DATA,
                OtMsgType.INVALID_DATA,
            ), f"OpenTherm: Invalid msg-type for RQ: {ot_type}"

            result.update(ot_value)  # TODO: find some of these packets to review

        result[SZ_DESCRIPTION] = ot_schema.get(EN)
        return result

    # if msg.verb != RP:
    #     raise

    _LIST = (OtMsgType.DATA_INVALID, OtMsgType.UNKNOWN_DATAID, OtMsgType.RESERVED)
    assert ot_type not in _LIST or payload[6:10] in (
        "0000",
        "FFFF",
    ), f"OpenTherm: Invalid msg-type|data-value: {ot_type}|{payload[6:10]}"

    # HACK: These OT data id can pop in/out of 47AB, which is an invalid value
    if payload[6:] == "47AB" and ot_id in (0x12, 0x13, 0x19, 0x1A, 0x1B, 0x1C):
        ot_value[SZ_VALUE] = None
    # HACK: This OT data id can be 1980, which is an invalid value
    if payload[6:] == "1980" and ot_id:  # CH pressure is 25.5 bar!
        ot_value[SZ_VALUE] = None
    # HACK: Done above, not in OT.decode_frame() as they isn't in the OT specification

    if ot_type not in _LIST:
        assert ot_type in (
            OtMsgType.READ_ACK,
            OtMsgType.WRITE_ACK,
        ), f"OpenTherm: Invalid msg-type for RP: {ot_type}"

        result.update(ot_value)

    try:  # These are checking flags in payload of data-id 0x00
        assert ot_id != 0 or (
            [result[SZ_VALUE][i] for i in (2, 3, 4, 5, 6, 7)] == [0] * 6
            # and [result[SZ_VALUE][i] for i in (1, )] == [1]
        ), result[SZ_VALUE]

        assert ot_id != 0 or (
            [result[SZ_VALUE][8 + i] for i in (0, 4, 5, 6, 7)] == [0] * 5
            # and [result[SZ_VALUE][8 + i] for i in (1, 2, 3)] == [0] * 3
        ), result[SZ_VALUE]

    except AssertionError:
        _LOGGER.warning(
            f"{msg!r} < {_INFORM_DEV_MSG}, with a description of your system"
        )

    result[SZ_DESCRIPTION] = ot_schema.get(EN)
    return result


# unknown_3221, from OTB, FAN
def parser_3221(payload: str, msg: Message) -> dict[str, Any]:
    # RP --- 10:052644 18:198151 --:------ 3221 002 000F
    # RP --- 10:048122 18:006402 --:------ 3221 002 0000
    # RP --- 32:155617 18:005904 --:------ 3221 002 000A

    assert int(payload[2:], 16) <= 0xC8, _INFORM_DEV_MSG

    return {
        "_payload": payload,
        SZ_VALUE: int(payload[2:], 16),
    }


# WIP: unknown, HVAC
def parser_3222(payload: str, msg: Message) -> dict[str, Any]:
    assert payload[:2] == "00"

    # e.g. RP|3222|00FE00 (payload = 3 bytes)
    if msg.len == 3:
        assert payload[4:] == "00"  # aka length 0

        return {
            "_value": f"0x{payload[2:4]}",
        }

    # e.g. RP|3222|000604000F100E (payload > 3 bytes)
    return {
        "offset": f"0x{payload[2:4]}",  # bytes
        "length": f"0x{payload[4:6]}",  # bytes
        "_data": f"{'..' * int(payload[2:4])}{payload[6:]}",
    }


# unknown_3223, from OTB
def parser_3223(payload: str, msg: Message) -> dict[str, Any]:
    assert int(payload[2:], 16) <= 0xC8, _INFORM_DEV_MSG

    return {
        "_payload": payload,
        SZ_VALUE: int(payload[2:], 16),
    }


# actuator_sync (aka sync_tpi: TPI cycle sync)
def parser_3b00(payload: str, msg: Message) -> PayDictT._3B00:
    # system timing master: the device that sends I/FCC8 pkt controls the heater relay
    """Decode a 3B00 packet (actuator_sync).

    The heat relay regularly broadcasts a 3B00 at the end(?) of every TPI cycle, the
    frequency of which is determined by the (TPI) cycle rate in 1100.

    The CTL subsequently broadcasts a 3B00 (i.e. at the start of every TPI cycle).

    The OTB does not send these packets, but the CTL sends a regular broadcast anyway
    for the benefit of any zone actuators (e.g. zone valve zones).
    """

    # 053  I --- 13:209679 --:------ 13:209679 3B00 002 00C8
    # 045  I --- 01:158182 --:------ 01:158182 3B00 002 FCC8
    # 052  I --- 13:209679 --:------ 13:209679 3B00 002 00C8
    # 045  I --- 01:158182 --:------ 01:158182 3B00 002 FCC8

    # 063  I --- 01:078710 --:------ 01:078710 3B00 002 FCC8
    # 064  I --- 01:078710 --:------ 01:078710 3B00 002 FCC8

    def complex_idx(payload: str, msg: Message) -> dict:  # has complex idx
        if (
            msg.verb == I_
            and msg.src.type in (DEV_TYPE_MAP.CTL, DEV_TYPE_MAP.PRG)
            and msg.src is msg.dst
        ):  # DEX
            assert payload[:2] == FC
            return {SZ_DOMAIN_ID: FC}
        assert payload[:2] == "00"
        return {}

    assert msg.len == 2, msg.len
    assert payload[:2] == {
        DEV_TYPE_MAP.CTL: FC,
        DEV_TYPE_MAP.BDR: "00",
        DEV_TYPE_MAP.PRG: FC,
    }.get(msg.src.type, "00")  # DEX
    assert payload[2:] == "C8", payload[2:]  # Could it be a percentage?

    return {
        **complex_idx(payload[:2], msg),  # type: ignore[typeddict-item]
        "actuator_sync": hex_to_bool(payload[2:]),
    }


# actuator_state
def parser_3ef0(payload: str, msg: Message) -> PayDictT._3EF0 | PayDictT._JASPER:
    result: dict[str, Any]

    if msg.src.type == DEV_TYPE_MAP.JIM:  # Honeywell Jasper
        assert msg.len == 20, f"expecting len 20, got: {msg.len}"
        return {
            "ordinal": f"0x{payload[2:8]}",
            "blob": payload[8:],
        }

    # TODO: These two should be picked up by the regex
    assert msg.len in (3, 6, 9), f"Invalid payload length: {msg.len}"
    # assert payload[:2] == "00", f"Invalid payload context: {payload[:2]}"

    # NOTE: some [2:4] appear to intend 0x00-0x64 (high_res=False), instead of 0x00-0xC8
    # NOTE: for best compatibility, all will be switched to 0x00-0xC8 (high_res=True)

    if msg.len == 3:  # I|BDR|003 (the following are the only two payloads ever seen)
        # .I --- 13:042805 --:------ 13:042805 3EF0 003 0000FF
        # .I --- 13:023770 --:------ 13:023770 3EF0 003 00C8FF
        assert payload[2:4] in ("00", "C8"), f"byte 1: {payload[2:4]} (not 00/C8)"
        assert payload[4:6] == "FF", f"byte 2: {payload[4:6]} (not FF)"
        mod_level = hex_to_percent(payload[2:4], high_res=True)

    else:  # msg.len >= 6:  # RP|OTB|006 (to RQ|CTL/HGI/RFG)
        # RP --- 10:004598 34:003611 --:------ 3EF0 006 0000100000FF
        # RP --- 10:004598 34:003611 --:------ 3EF0 006 0000110000FF
        # RP --- 10:138822 01:187666 --:------ 3EF0 006 0064100C00FF
        # RP --- 10:138822 01:187666 --:------ 3EF0 006 0064100200FF
        assert payload[4:6] in ("00", "10", "11"), f"byte 2: {payload[4:6]}"
        mod_level = hex_to_percent(payload[2:4], high_res=True)  # 00-64/C8 (or FF)

    result = {
        "modulation_level": mod_level,  # 0008[2:4], 3EF1[10:12]
        "_flags_2": payload[4:6],
    }

    if msg.len >= 6:  # RP|OTB|006 (to RQ|CTL/HGI/RFG)
        # RP --- 10:138822 01:187666 --:------ 3EF0 006 000110FA00FF  # ?corrupt

        # for OTB (there's no reliable) modulation_level <-> flame_state)

        result.update(
            {
                "_flags_3": hex_to_flag8(payload[6:8]),
                "ch_active": bool(int(payload[6:8], 0x10) & 1 << 1),
                "dhw_active": bool(int(payload[6:8], 0x10) & 1 << 2),
                "cool_active": bool(int(payload[6:8], 0x10) & 1 << 4),
                "flame_on": bool(int(payload[6:8], 0x10) & 1 << 3),  # flame_on
                "_unknown_4": payload[8:10],  # FF, 00, 01, 0A
                "_unknown_5": payload[10:12],  # FF, 13, 1C, ?others
            }  # TODO: change to flame_active?
        )

    if msg.len >= 9:  # I/RP|OTB|009 (R8820A only?)
        assert int(payload[12:14], 16) & 0b11111100 == 0, f"byte 6: {payload[12:14]}"
        assert int(payload[12:14], 16) & 0b00000010 == 2, f"byte 6: {payload[12:14]}"
        assert 10 <= int(payload[14:16], 16) <= 90, f"byte 7: {payload[14:16]}"
        assert int(payload[16:18], 16) in (0, 100), f"byte 8: {payload[18:]}"

        result.update(
            {
                "_flags_6": hex_to_flag8(payload[12:14]),
                "ch_enabled": bool(int(payload[12:14], 0x10) & 1 << 0),
                "ch_setpoint": int(payload[14:16], 0x10),
                "max_rel_modulation": hex_to_percent(payload[16:18], high_res=True),
            }
        )

    try:  # Trying to decode flags...
        # assert payload[4:6] != "11" or (
        #     payload[2:4] == "00"
        # ), f"bytes 1+2: {payload[2:6]}"  # 97% is 00 when 11, but not always

        assert payload[4:6] in ("00", "10", "11", "FF"), f"byte 2: {payload[4:6]}"

        assert "_flags_3" not in result or (
            payload[6:8] == "FF" or int(payload[6:8], 0x10) & 0b10100000 == 0
        ), f"byte 3: {result['_flags_3']}"
        # only 10:040239 does 0b01000000, only Itho Autotemp does 0b00010000

        assert "_unknown_4" not in result or (
            payload[8:10] in ("FF", "00", "01", "02", "04", "0A")
        ), f"byte 4: {payload[8:10]}"
        # only 10:040239 does 04

        assert "_unknown_5" not in result or (
            payload[10:12] in ("00", "13", "1C", "FF")
        ), f"byte 5: {payload[10:12]}"

        assert "_flags_6" not in result or (
            int(payload[12:14], 0x10) & 0b11111100 == 0
        ), f"byte 6: {result['_flags_6']}"

    except AssertionError as err:
        _LOGGER.warning(
            f"{msg!r} < {_INFORM_DEV_MSG} ({err}), with a description of your system"
        )
    return result  # type: ignore[return-value]


# actuator_cycle
def parser_3ef1(payload: str, msg: Message) -> PayDictT._3EF1 | PayDictT._JASPER:
    if msg.src.type == DEV_TYPE_MAP.JIM:  # Honeywell Jasper, DEX
        assert msg.len == 18, f"expecting len 18, got: {msg.len}"
        return {
            "ordinal": f"0x{payload[2:8]}",
            "blob": payload[8:],
        }

    if (
        msg.src.type == DEV_TYPE_MAP.JST
    ):  # and msg.len == 12:  # or (12, 20) Japser, DEX
        assert msg.len == 12, f"expecting len 12, got: {msg.len}"
        return {
            "ordinal": f"0x{payload[2:8]}",
            "blob": payload[8:],
        }

    percent = hex_to_percent(payload[10:12])

    if payload[12:] == "FF":  # is BDR
        assert percent is None or percent in (0, 1), f"byte 5: {payload[10:12]}"

    else:  # is OTB
        # assert (
        #     re.compile(r"^00[0-9A-F]{10}10").match(payload)
        # ), "doesn't match: " + r"^00[0-9A-F]{10}10"
        assert payload[2:6] == "7FFF", f"byte 1: {payload[2:6]}"
        assert payload[6:10] == "003C", f"byte 3: {payload[6:10]}"  # 60 seconds
        assert percent is None or percent <= 1, f"byte 5: {payload[10:12]}"

    cycle_countdown = None if payload[2:6] == "7FFF" else int(payload[2:6], 16)
    if cycle_countdown is not None:
        if cycle_countdown > 0x7FFF:
            cycle_countdown -= 0x10000
        assert cycle_countdown < 7200, f"byte 1: {payload[2:6]}"  # 7200 seconds

    actuator_countdown = None if payload[6:10] == "7FFF" else int(payload[6:10], 16)
    if actuator_countdown is not None:
        if actuator_countdown > 0x7FFF:  # "87B3", "9DFA", "DCE1", "E638", "F8F7"
            # actuator_countdown = 0x10000 - actuator_countdown  + cycle_countdown
            actuator_countdown = cycle_countdown  # Needs work
        # assert actuator_countdown <= cycle_countdown, f"byte 3: {payload[6:10]}"

    return {
        "modulation_level": percent,  # 0008[2:4], 3EF0[2:4]
        "actuator_countdown": actuator_countdown,
        "cycle_countdown": cycle_countdown,
        "_unknown_0": payload[12:],
    }


# timestamp, HVAC
def parser_4401(payload: str, msg: Message) -> dict[str, Any]:
    if msg.verb == RP:
        return {}

    # 2022-07-28T14:21:38.895354 095  W --- 37:010164 37:010151 --:------ 4401 020 10  7E-E99E90C8  00-E99E90C7-3BFF  7E-E99E90C8-000B
    # 2022-07-28T14:21:57.414447 076 RQ --- 20:225479 20:257336 --:------ 4401 020 10  2E-E99E90DB  00-00000000-0000  00-00000000-000B
    # 2022-07-28T14:21:57.625474 045  I --- 20:257336 20:225479 --:------ 4401 020 10  2E-E99E90DB  00-E99E90DA-F0FF  BD-00000000-000A
    # 2022-07-28T14:22:02.932576 088 RQ --- 37:010188 20:257336 --:------ 4401 020 10  22-E99E90E0  00-00000000-0000  00-00000000-000B
    # 2022-07-28T14:22:03.053744 045  I --- 20:257336 37:010188 --:------ 4401 020 10  22-E99E90E0  00-E99E90E0-75FF  BD-00000000-000A
    # 2022-07-28T14:22:20.516363 045 RQ --- 20:255710 20:257400 --:------ 4401 020 10  0B-E99E90F2  00-00000000-0000  00-00000000-000B
    # 2022-07-28T14:22:20.571640 085  I --- 20:255251 20:229597 --:------ 4401 020 10  39-E99E90F1  00-E99E90F1-5CFF  40-00000000-000A
    # 2022-07-28T14:22:20.648696 058  I --- 20:257400 20:255710 --:------ 4401 020 10  0B-E99E90F2  00-E99E90F1-D4FF  DA-00000000-000B

    # 2022-11-03T23:00:04.854479 088 RQ --- 20:256717 37:013150 --:------ 4401 020 10  00-00259261  00-00000000-0000  00-00000000-0063
    # 2022-11-03T23:00:05.102491 045  I --- 37:013150 20:256717 --:------ 4401 020 10  00-00259261  00-000C9E4C-1800  00-00000000-0063
    # 2022-11-03T23:00:17.820659 072  I --- 20:256112 20:255825 --:------ 4401 020 10  00-00F1EB91  00-00E8871B-B700  00-00000000-0063
    # 2022-11-03T23:01:25.495391 065  I --- 20:257732 20:257680 --:------ 4401 020 10  00-002E9C98  00-00107923-9E00  00-00000000-0063
    # 2022-11-03T23:01:33.753467 066 RQ --- 20:257732 20:256112 --:------ 4401 020 10  00-0010792C  00-00000000-0000  00-00000000-0063
    # 2022-11-03T23:01:33.997485 072  I --- 20:256112 20:257732 --:------ 4401 020 10  00-0010792C  00-00E88767-AD00  00-00000000-0063
    # 2022-11-03T23:01:52.391989 090  I --- 20:256717 20:255301 --:------ 4401 020 10  00-009870E1  00-002592CC-6300  00-00000000-0063

    def hex_to_epoch(seqx: str) -> None | str:  # seconds since 1-1-1970
        if seqx == "00" * 4:
            return None
        return str(
            dt.fromtimestamp(int(seqx, 16))
        )  # - int(payload[22:26], 16) * 15 * 60))

    # 10 7E-E99E90C8 00-E99E90C7-3BFF 7E-E99E90C8-000B
    # hex(int(dt.fromisoformat("2022-07-28T14:21:38.895354").timestamp())).upper()
    # '0x62E20ED2'

    assert payload[:2] == "10", payload[:2]
    assert payload[12:14] == "00", payload[12:14]
    assert payload[36:38] == "00", payload[36:38]

    assert msg.verb != I_ or payload[24:26] in ("00", "7C", "FF"), payload[24:26]
    assert msg.verb != W_ or payload[24:26] in ("7C", "FF"), payload[24:26]
    assert msg.verb != RQ or payload[24:26] == "00", payload[24:26]

    assert msg.verb != RQ or payload[14:22] == "00" * 4, payload[14:22]
    assert msg.verb != W_ or payload[28:36] != "00" * 4, payload[28:36]

    assert payload[38:40] in ("08", "09", "0A", "0B", "63"), payload[38:40]

    # assert payload[2:4] == payload[26:28], f"{payload[2:4]}, {payload[26:24]}"

    return {
        "last_update_dst": payload[2:4],
        "time_dst": hex_to_epoch(payload[4:12]),
        "_unknown_12": payload[12:14],  # usu.00
        "time_src": hex_to_epoch(payload[14:22]),
        "offset": payload[22:24],  # *15 mins?
        "_unknown_24": payload[24:26],
        "last_update_src": payload[26:28],
        "time_dst_receive_src": hex_to_epoch(payload[28:36]),
        "_unknown_36": payload[36:38],  # usu.00
        "hops_dst_src": payload[38:40],
    }


# temperatures (see: 4e02) - Itho spider/autotemp
def parser_4e01(payload: str, msg: Message) -> dict[str, Any]:
    # .I --- 02:248945 02:250708 --:------ 4E01 018 00-7FFF7FFF7FFF09077FFF7FFF7FFF7FFF-00  # 23.11, 8-group
    # .I --- 02:250984 02:250704 --:------ 4E01 018 00-7FFF7FFF7FFF7FFF08387FFF7FFF7FFF-00  # 21.04

    num_groups = int((msg.len - 2) / 2)  # e.g. (18 - 2) / 2
    assert num_groups * 2 == msg.len - 2, (
        _INFORM_DEV_MSG
    )  # num_groups: len 018 (8-group, 2+8*4), or 026 (12-group, 2+12*4)

    x, y = 0, 2 + num_groups * 4

    assert payload[x : x + 2] == "00", _INFORM_DEV_MSG
    assert payload[y : y + 2] == "00", _INFORM_DEV_MSG

    return {
        "temperatures": [hex_to_temp(payload[i : i + 4]) for i in range(2, y, 4)],
    }


# setpoint_bounds (see: 4e01) - Itho spider/autotemp
def parser_4e02(
    payload: str, msg: Message
) -> dict[str, Any]:  # sent a triplets, 1 min apart
    # .I --- 02:248945 02:250708 --:------ 4E02 034 00-7FFF7FFF7FFF07D07FFF7FFF7FFF7FFF-02-7FFF7FFF7FFF08347FFF7FFF7FFF7FFF  # 20.00-21.00
    # .I --- 02:250984 02:250704 --:------ 4E02 034 00-7FFF7FFF7FFF076C7FFF7FFF7FFF7FFF-02-7FFF7FFF7FFF07D07FFF7FFF7FFF7FFF  #

    num_groups = int((msg.len - 2) / 4)  # e.g. (34 - 2) / 4
    assert num_groups * 4 == msg.len - 2, (
        _INFORM_DEV_MSG
    )  # num_groups: len 034 (8-group, 2+8*4), or 050 (12-group, 2+12*4)

    x, y = 0, 2 + num_groups * 4

    assert payload[x : x + 2] == "00", _INFORM_DEV_MSG  # expect no context
    assert payload[y : y + 2] in (
        "02",
        "03",
        "04",
        "05",
    ), _INFORM_DEV_MSG  # mode: cool/heat?

    setpoints = [
        (hex_to_temp(payload[x + i :][:4]), hex_to_temp(payload[y + i :][:4]))
        for i in range(2, y, 4)
    ]  # lower, upper setpoints

    return {
        SZ_MODE: {"02": "cool", "03": "cool+", "04": "heat", "05": "cool+"}[
            payload[y : y + 2]
        ],
        SZ_SETPOINT_BOUNDS: [s if s != (None, None) else None for s in setpoints],
    }


# hvac_4e04
def parser_4e04(payload: str, msg: Message) -> dict[str, Any]:
    MODE = {
        "00": "off",
        "01": "heat",
        "02": "cool",
    }

    assert payload[2:4] in MODE, _INFORM_DEV_MSG
    assert int(payload[4:], 16) < 0x40 or payload[4:] in (
        "FB",  # error code?
        "FC",  # error code?
        "FD",  # error code?
        "FE",  # error code?
        "FF",  # N/A?
    )

    return {
        SZ_MODE: MODE.get(payload[2:4], "Unknown"),
        "_unknown_2": payload[4:],
    }


# WIP: AT outdoor low - Itho spider/autotemp
def parser_4e0d(payload: str, msg: Message) -> dict[str, Any]:
    # .I --- 02:250704 02:250984 --:------ 4E0D 002 0100  # Itho Autotemp: only(?) master -> slave
    # .I --- 02:250704 02:250984 --:------ 4E0D 002 0101  # why does it have a context?

    return {
        "_payload": payload,
    }


# AT fault circulation - Itho spider/autotemp
def parser_4e14(payload: str, msg: Message) -> dict[str, Any]:
    """
    result = "AT fault circulation";
    result = (((payload[2:] & 0x01) != 0x01) ? " Fault state : no fault "                : " Fault state : fault ")
    result = (((payload[2:] & 0x02) != 0x02) ? (text4 + "Circulation state : no fault ") : (text4 + " Circulation state : fault "))
    """
    return {}


# wpu_state (hvac state) - Itho spider/autotemp
def parser_4e15(payload: str, msg: Message) -> dict[str, Any]:
    # .I --- 21:034158 02:250676 --:------ 4E15 002 0000  # WPU "off" (maybe heating, but compressor off)
    # .I --- 21:064743 02:250708 --:------ 4E15 002 0001  # WPU cooling active
    # .I --- 21:057565 02:250677 --:------ 4E15 002 0002  # WPU heating, compressor active
    # .I --- 21:064743 02:250708 --:------ 4E15 002 0004  # WPU in "DHW mode" boiler active
    # .I --- 21:033160 02:250704 --:------ 4E15 002 0005  # 0x03, and 0x06 not seen in the wild

    if int(payload[2:], 16) & 0xF0:
        pass

    # If none of these, then is 'Off'
    SZ_COOLING = "is_cooling"
    SZ_DHW_ING = "is_dhw_ing"
    SZ_HEATING = "is_heating"
    # SZ_PUMPING = "is_pumping"

    assert int(payload[2:], 16) & 0xF8 == 0x00, (
        _INFORM_DEV_MSG
    )  # check for unknown bit flags
    if int(payload[2:], 16) & 0x03 == 0x03:  # is_cooling *and* is_heating (+/- DHW)
        raise TypeError  # TODO: Use local exception & ?Move to higher layer
    assert int(payload[2:], 16) & 0x07 != 0x06, _INFORM_DEV_MSG  # can't heat and DHW

    return {
        "_flags": hex_to_flag8(payload[2:]),
        # SZ_PUMPING: bool(int(payload[2:], 16) & 0x08),
        SZ_DHW_ING: bool(int(payload[2:], 16) & 0x04),
        SZ_HEATING: bool(int(payload[2:], 16) & 0x02),
        SZ_COOLING: bool(int(payload[2:], 16) & 0x01),
    }


# TODO: hvac_4e16 - Itho spider/autotemp
def parser_4e16(payload: str, msg: Message) -> dict[str, Any]:
    # .I --- 02:250984 02:250704 --:------ 4E16 007 00000000000000  # Itho Autotemp: slave -> master

    assert payload == "00000000000000", _INFORM_DEV_MSG

    return {
        "_payload": payload,
    }


# TODO: Fan characteristics - Itho
def parser_4e20(payload: str, msg: Message) -> dict[str, Any]:
    """
    result = "Fan characteristics: "
    result += [C[ABC][210] hex_to_sint32[i:i+4] for i in range(2, 34, 4)]
    """
    return {}


# TODO: Potentiometer control - Itho
def parser_4e21(payload: str, msg: Message) -> dict[str, Any]:
    """
    result = "Potentiometer control: "
    result += "Rel min: "        + hex_to_sint16(data[2:4])  # 16 bit, 2's complement
    result += "Min of rel min: " + hex_to_sint16(data[4:6])
    result += "Abs min: "        + hex_to_sint16(data[6:8])
    result += "Rel max: "        + hex_to_sint16(data[8:10])
    result += "Max rel: "        + hex_to_sint16(data[10:12])
    result += "Abs max: "        + hex_to_sint16(data[12:14]))
    """
    return {}


#   # faked puzzle pkt shouldn't be decorated
def parser_7fff(payload: str, _: Message) -> dict[str, Any]:
    if payload[:2] != "00":
        _LOGGER.debug("Invalid/deprecated Puzzle packet")
        return {
            "msg_type": payload[:2],
            SZ_PAYLOAD: hex_to_str(payload[2:]),
        }

    if payload[2:4] not in LOOKUP_PUZZ:
        _LOGGER.debug("Invalid/deprecated Puzzle packet")
        return {
            "msg_type": payload[2:4],
            "message": hex_to_str(payload[4:]),
        }

    result: dict[str, None | str] = {}
    if int(payload[2:4]) >= int("20", 16):
        dtm = dt.fromtimestamp(int(payload[4:16], 16) / 1e7)  # TZ-naive
        result["datetime"] = dtm.isoformat(timespec="milliseconds")
    elif payload[2:4] != "13":
        dtm = dt.fromtimestamp(int(payload[4:16], 16) / 1000)  # TZ-naive
        result["datetime"] = dtm.isoformat(timespec="milliseconds")

    msg_type = LOOKUP_PUZZ.get(payload[2:4], SZ_PAYLOAD)

    if payload[2:4] == "11":
        mesg = hex_to_str(payload[16:])
        result[msg_type] = f"{mesg[:4]}|{mesg[4:6]}|{mesg[6:]}"

    elif payload[2:4] == "13":
        result[msg_type] = hex_to_str(payload[4:])

    elif payload[2:4] == "7F":
        result[msg_type] = payload[4:]

    else:
        result[msg_type] = hex_to_str(payload[16:])

    return {**result, "parser": f"v{VERSION}"}


def parser_unknown(payload: str, msg: Message) -> dict[str, Any]:
    # TODO: it may be useful to generically search payloads for hex_ids, commands, etc.

    # These are generic parsers
    if msg.len == 2 and payload[:2] == "00":
        return {
            "_payload": payload,
            "_value": {"00": False, "C8": True}.get(payload[2:], int(payload[2:], 16)),
        }

    if msg.len == 3 and payload[:2] == "00":
        return {
            "_payload": payload,
            "_value": hex_to_temp(payload[2:]),
        }

    raise NotImplementedError


_PAYLOAD_PARSERS = {
    k[7:].upper(): v
    for k, v in locals().items()
    if callable(v) and k.startswith("parser_") and len(k) == 11
}


def parse_payload(msg: Message) -> dict | list[dict]:
    result: dict | list[dict]

    result = _PAYLOAD_PARSERS.get(msg.code, parser_unknown)(msg._pkt.payload, msg)
    if isinstance(result, dict) and msg.seqn.isnumeric():  # e.g. 22F1/3
        result["seqx_num"] = msg.seqn

    return result
