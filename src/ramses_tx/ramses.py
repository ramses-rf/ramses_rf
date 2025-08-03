#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser."""

# TODO: code a lifespan for most packets

from __future__ import annotations

from datetime import timedelta as td
from typing import Any, Final

from .const import SZ_NAME, DevType

from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
    VerbT,
)


SZ_LIFESPAN: Final = "lifespan"  # WIP


#
########################################################################################
# CODES_SCHEMA - HEAT (CH/DHW, Honeywell/Resideo) vs HVAC (ventilation, Itho/Orcon/etc.)

# The master list - all known codes are here, even if there's no corresponding parser
# Anything with a zone-idx should start: ^0[0-9A-F], ^(0[0-9A-F], or ^((0[0-9A-F]

#
CODES_SCHEMA: dict[Code, dict[str, Any]] = {  # rf_unknown
    Code._0001: {
        SZ_NAME: "rf_unknown",
        I_: r"^00FFFF02(00|FF)$",  # loopback
        RQ: r"^00([28A]0)00(0[0-9A-F])(FF|04)$",  # HVAC
        RP: r"^00([28A]0)00(0[0-9A-F])",  # HVAC
        W_: r"^(0[0-9A-F]|FC|FF)000005(01|05)$",
    },  # TODO: there appears to be a dodgy? RQ/RP for UFC
    Code._0002: {  # WIP: outdoor_sensor - CODE_IDX_COMPLEX?
        # is it CODE_IDX_COMPLEX:
        #  - 02...... for outside temp?
        #  - 03...... for other stuff?
        SZ_NAME: "outdoor_sensor",
        I_: r"^0[0-4][0-9A-F]{4}(00|01|02|05)$",  # Domoticz sends ^02!!
        RQ: r"^00$",  # NOTE: sent by an RFG100
    },
    Code._0004: {  # zone_name
        SZ_NAME: "zone_name",
        I_: r"^0[0-9A-F]00([0-9A-F]){40}$",  # RP is same, null_rp: xxxx,7F*20
        RQ: r"^0[0-9A-F]00$",
        W_: r"^0[0-9A-F]00([0-9A-F]){40}$",  # contrived
        SZ_LIFESPAN: td(days=1),
    },
    Code._0005: {  # system_zones
        SZ_NAME: "system_zones",
        # .I --- 34:092243 --:------ 34:092243 0005 012 000A0000-000F0000-00100000
        I_: r"^(00[01][0-9A-F]{5}){1,3}$",
        RQ: r"^00[01][0-9A-F]$",  # f"00{zone_type}", evohome won't respond to 00
        RP: r"^00[01][0-9A-F]{3,5}$",
        SZ_LIFESPAN: False,
    },
    Code._0006: {  # schedule_version  # TODO: what for DHW schedule?
        SZ_NAME: "schedule_version",
        RQ: r"^00$",
        RP: r"^0005[0-9A-F]{4}$",
    },
    Code._0008: {  # relay_demand, TODO: check RP
        SZ_NAME: "relay_demand",
        # 000 I --- 31:012319 08:006244 --:------ 0008 013 0006958C33CA6ECD2067AA53DD
        I_: r"^((0[0-9A-F]|F[9AC])[0-9A-F]{2}|00[0-9A-F]{24})$",
        RQ: r"^00$",
        RP: r"^00[0-9A-F]{2}$",  # seems only 13: RP (TODO: what about 10:, 08/31:)
    },
    Code._0009: {  # relay_failsafe (only is_controller, OTB send an 0009?)
        SZ_NAME: "relay_failsafe",
        # .I --- 01:145038 --:------ 01:145038 0009 006 FC01FFF901FF
        # .I --- 01:145038 --:------ 01:145038 0009 003 0700FF
        # .I --- 10:040239 01:223036 --:------ 0009 003 000000
        # .I --- --:------ --:------ 12:227486 0009 003 0000FF
        I_: r"^((0[0-9A-F]|F[9AC])0[0-1](00|FF))+$",
    },
    Code._000A: {  # zone_params
        SZ_NAME: "zone_params",
        I_: r"^(0[0-9A-F][0-9A-F]{10}){1,8}$",
        W_: r"^0[0-9A-F][0-9A-F]{10}$",
        RQ: r"^0[0-9A-F]((00)?|([0-9A-F]{10})+)$",  # is: r"^0[0-9A-F]([0-9A-F]{10})+$"
        RP: r"^0[0-9A-F][0-9A-F]{10}$",  # null_rp: xx/007FFF7FFF
        # 17:54:13.126 063 RQ --- 34:064023 01:145038 --:------ 000A 001 03
        # 17:54:13.141 045 RP --- 01:145038 34:064023 --:------ 000A 006 031002260B86
        # 19:20:49.460 062 RQ --- 12:010740 01:145038 --:------ 000A 006 080001F40DAC
        # 19:20:49.476 045 RP --- 01:145038 12:010740 --:------ 000A 006 081001F40DAC
        SZ_LIFESPAN: td(days=1),
    },
    Code._000C: {  # zone_devices
        SZ_NAME: "zone_devices",
        # RP --- 01:145038 18:013393 --:------ 000C 018 06-08-00-1099C3 06-08-00-1099C5 06-08-00-1099BF
        # RP --- 01:145038 18:013393 --:------ 000C 016 05-08-00-109901    08-00-109902    08-00-109903
        I_: r"^0[0-9A-F][01][0-9A-F]|7F[0-9A-F]{6}([0-9A-F]{10}|[0-9A-F]{12}){1,7}$",
        RQ: r"^0[0-9A-F][01][0-9A-F]$",  # TODO: f"{zone_idx}{device_type}"
        SZ_LIFESPAN: False,
    },
    Code._000E: {  # unknown_000e
        SZ_NAME: "message_000e",
        I_: r"^0000(14|28)$",
    },
    Code._0016: {  # rf_check
        SZ_NAME: "rf_check",
        RQ: r"^0[0-9A-F]([0-9A-F]{2})?$",  # TODO: officially: r"^0[0-9A-F]{3}$"
        RP: r"^0[0-9A-F]{3}$",
    },
    Code._0100: {  # language
        SZ_NAME: "language",
        RQ: r"^00([0-9A-F]{4}F{4})?$",  # NOTE: RQ/04/0100 has a payload
        RP: r"^00[0-9A-F]{4}F{4}$",
        SZ_LIFESPAN: td(days=1),  # TODO: make longer?
    },
    Code._0150: {  # unknown_0150
        SZ_NAME: "message_0150",
        RQ: r"^00$",
        RP: r"^000000$",
    },
    Code._01D0: {  # unknown_01d0, TODO: definitely a real code, zone_idx is a guess
        SZ_NAME: "message_01d0",
        I_: r"^0[0-9A-F][0-9A-F]{2}$",
        W_: r"^0[0-9A-F][0-9A-F]{2}$",
        # .W --- 04:000722 01:158182 --:------ 01D0 002 0003  # is a guess, the
        # .I --- 01:158182 04:000722 --:------ 01D0 002 0003  # TRV was in zone 00
    },
    Code._01E9: {  # unknown_01e9, TODO: definitely a real code, zone_idx is a guess
        SZ_NAME: "message_01e9",
        I_: r"^0[0-9A-F][0-9A-F]{2}$",
        W_: r"^0[0-9A-F][0-9A-F]{2}$",
        # .W --- 04:000722 01:158182 --:------ 01E9 002 0003  # is a guess, the
        # .I --- 01:158182 04:000722 --:------ 01E9 002 0000  # TRV was in zone 00
    },
    Code._01FF: {  # unknown_01ff, TODO: definitely a real code, Itho Spider
        SZ_NAME: "message_01ff",
        I_: r"^(00|01)[0-9A-F]{50}$",
        RQ: r"^(00|01)[0-9A-F]{50}$",
        W_: r"^00[0-9A-F]{50}$",
    },
    Code._0404: {  # zone_schedule
        SZ_NAME: "zone_schedule",
        I_: r"^0[0-9A-F](20|23)[0-9A-F]{2}08[0-9A-F]{6}$",
        RQ: r"^0[0-9A-F](20|23)000800[0-9A-F]{4}$",
        RP: r"^0[0-9A-F](20|23)0008[0-9A-F]{6}[0-9A-F]{2,82}$",
        W_: r"^0[0-9A-F](20|23)[0-9A-F]{2}08[0-9A-F]{6}[0-9A-F]{2,82}$",  # as per RP
        SZ_LIFESPAN: None,
    },
    Code._0418: {  # system_fault
        SZ_NAME: "system_fault",
        I_: r"^00(00|40|C0)[0-3][0-9A-F]B0[0-9A-F]{6}0000[0-9A-F]{12}FFFF700[012][0-9A-F]{6}$",
        RQ: r"^0000[0-3][0-9A-F]$",  # f"0000{log_idx}", no payload
    },
    Code._042F: {  # unknown_042f, # non-evohome are len==9, seen only once?
        # .I --- 32:168090 --:------ 32:168090 042F 009 000000100F00105050
        # RP --- 10:048122 18:006402 --:------ 042F 009 000200001400163010
        SZ_NAME: "message_042f",
        I_: r"^00([0-9A-F]{2}){7,8}$",
        RQ: r"^00$",
        RP: r"^00([0-9A-F]{2}){7,8}$",
    },
    Code._0B04: {  # unknown_0b04
        # .I --- --:------ --:------ 12:207082 0B04 002 00C8
        SZ_NAME: "message_0b04",
        I_: r"^00(00|C8)$",
    },
    Code._1030: {  # mixvalve_params
        SZ_NAME: "mixvalve_params",
        # .I --- --:------ --:------ 12:138834 1030 016 01C80137C9010FCA0196CB010FCC0101
        I_: r"^0[0-9A-F](C[89A-C]01[0-9A-F]{2}){5}$",
        RP: r"^00((20|21)01[0-9A-F]{2}){2}$",  # rarely seen, HVAC
        W_: r"^0[0-9A-F](C[89A-C]01[0-9A-F]{2}){5}$",  # contrived
    },
    Code._1060: {  # device_battery
        SZ_NAME: "device_battery",
        I_: r"^0[0-9A-F](FF|[0-9A-F]{2})0[01]$",  # HCW: r"^(FF|0[0-9A-F]...
        SZ_LIFESPAN: td(days=1),
    },
    Code._1081: {  # max_ch_setpoint
        SZ_NAME: "max_ch_setpoint",
        RQ: r"^00$",
        RP: r"^00[0-9A-F]{4}$",
    },
    Code._1090: {  # unknown_1090
        # 095 RP --- 23:100224 22:219457 --:------ 1090 005 00-7FFF-01F4
        SZ_NAME: "message_1090",
        RQ: r"^00$",
        RP: r"^00",
    },
    Code._1098: {  # unknown_1098
        SZ_NAME: "message_1098",
        RQ: r"^00$",
        RP: r"^00",
    },
    Code._10A0: {  # dhw_params
        SZ_NAME: "dhw_params",
        # RQ --- 07:045960 01:145038 --:------ 10A0 006 0013740003E4
        # RP --- 10:048122 18:006402 --:------ 10A0 003 001B58
        # NOTE: RFG100 uses a domain id! (00|01)
        # 19:14:24.662 051 RQ --- 30:185469 01:037519 --:------ 10A0 001 00
        # 19:14:31.463 053 RQ --- 30:185469 01:037519 --:------ 10A0 001 01
        I_: r"^(00|01)[0-9A-F]{4}([0-9A-F]{6})?$",  # NOTE: RQ/07/10A0 has a payload
        RQ: r"^(00|01)([0-9A-F]{10})?$",  # NOTE: RQ/07/10A0 has a payload
        W_: r"^(00|01)[0-9A-F]{4}([0-9A-F]{6})?$",  # TODO: needs checking
        SZ_LIFESPAN: td(hours=4),
    },
    Code._10B0: {  # unknown_10b0
        SZ_NAME: "message_10b0",
        RQ: r"^00$",
        RP: r"^00[0-9A-F]{8}$",
    },
    Code._10D0: {  # filter_change - polling interval should be 1/day
        SZ_NAME: "filter_change",
        I_: r"^00[0-9A-F]{6}(0000|FFFF)?$",
        RQ: r"^00(00)?$",
        W_: r"^00FF$",
    },
    Code._10E0: {  # device_info
        SZ_NAME: "device_info",
        I_: r"^(00|FF)([0-9A-F]{30,})?$",  # r"^[0-9A-F]{32,}$" might be OK
        RQ: r"^00$",  # NOTE: 63 seen (no RP), some devices will accept [0-9A-F]{2}
        # RP: r"^[0-9A-F]{2}([0-9A-F]){30,}$",  # NOTE: indx same as RQ
        SZ_LIFESPAN: False,
    },
    Code._10E1: {  # device_id
        SZ_NAME: "device_id",
        RP: r"^00[0-9A-F]{6}$",
        RQ: r"^00$",
        SZ_LIFESPAN: False,
    },
    Code._10E2: {  # unknown_10e2, HVAC?
        SZ_NAME: "unknown_10e2",
        I_: r"^00[0-9A-F]{4}$",
    },
    Code._1100: {  # tpi_params
        SZ_NAME: "tpi_params",
        #  I --- 01:172368 --:------ 01:172368 1100 008 FC180400007FFF00
        #  I --- 01:172368 13:040439 --:------ 1100 008 FC042814007FFF00
        # RQ --- 01:145038 13:163733 --:------ 1100 008 00180400007FFF01  # boiler relay
        # RP --- 13:163733 01:145038 --:------ 1100 008 00180400FF7FFF01
        # RQ --- 01:145038 13:035462 --:------ 1100 008 FC240428007FFF01  # not bolier relay
        # RP --- 13:035462 01:145038 --:------ 1100 008 00240428007FFF01
        I_: r"^(00|FC)[0-9A-F]{6}(00|FF)([0-9A-F]{4}0[01])?$",
        W_: r"^(00|FC)[0-9A-F]{6}(00|FF)([0-9A-F]{4}0[01])?$",  # TODO: is there no I?
        RQ: r"^(00|FC)([0-9A-F]{6}(00|FF)([0-9A-F]{4}0[01])?)?$",  # RQ/13:/00, or RQ/01:/FC:
        SZ_LIFESPAN: td(days=1),
    },
    Code._11F0: {  # unknown_11f0, from heatpump relay
        SZ_NAME: "message_11f0",
        I_: r"^00",
    },
    Code._1260: {  # dhw_temp
        SZ_NAME: "dhw_temp",
        # RQ --- 30:185469 01:037519 --:------ 1260 001 00
        # RP --- 01:037519 30:185469 --:------ 1260 003 000837
        # RQ --- 18:200202 10:067219 --:------ 1260 002 0000
        # RP --- 10:067219 18:200202 --:------ 1260 003 007FFF
        # .I --- 07:045960 --:------ 07:045960 1260 003 0007A9
        I_: r"^(00|01)[0-9A-F]{4}$",  # NOTE: RP is same
        RQ: r"^(00|01)(00)?$",  # TODO: officially: r"^(00|01)$"
        SZ_LIFESPAN: td(hours=1),
    },
    Code._1280: {  # outdoor_humidity
        SZ_NAME: "outdoor_humidity",
        I_: r"^00[0-9A-F]{2}[0-9A-F]{8}?$",
    },
    Code._1290: {  # outdoor_temp
        SZ_NAME: "outdoor_temp",
        I_: r"^00[0-9A-F]{4}$",  # NOTE: RP is same
        RQ: r"^00$",
    },
    Code._1298: {  # co2_level
        SZ_NAME: "co2_level",
        I_: r"^00[0-9A-F]{4}$",
        RQ: r"^00$",
    },
    Code._12A0: {  # indoor_humidity
        SZ_NAME: "indoor_humidity",
        I_: r"^(0[0-9A-F]{3}([0-9A-F]{8}(00)?)?)+$",
        RP: r"^0[0-9A-F]{3}([0-9A-F]{8}(00)?)?$",
        SZ_LIFESPAN: td(hours=1),
    },
    Code._12B0: {  # window_state  (HVAC % window open)
        SZ_NAME: "window_state",
        I_: r"^0[0-9A-F](0000|C800|FFFF)$",  # NOTE: RP is same
        RQ: r"^0[0-9A-F](00)?$",
        SZ_LIFESPAN: td(hours=1),
    },
    Code._12C0: {  # displayed_temp (HVAC room temp)
        SZ_NAME: "displayed_temp",  # displayed room temp
        I_: r"^00[0-9A-F]{2}0[01](FF)?$",
    },
    Code._12C8: {  # air_quality, HVAC
        SZ_NAME: "air_quality",
        I_: r"^00[0-9A-F]{4}$",
    },
    Code._12F0: {  # dhw_flow_rate
        # 2021-11-05T06:25:20.399400 065 RP --- 10:023327 18:131597 --:------ 12F0 003 000307
        # 2021-11-05T06:25:20.669382 066 RP --- 10:023327 18:131597 --:------ 3220 005 00C01307C0
        # 2021-11-05T06:35:20.450201 065 RP --- 10:023327 18:131597 --:------ 12F0 003 000023
        # 2021-11-05T06:35:20.721228 066 RP --- 10:023327 18:131597 --:------ 3220 005 0040130059
        # 2021-12-06T06:35:54.575298 073 RP --- 10:051349 18:135447 --:------ 12F0 003 00059F
        # 2021-12-06T06:35:55.949502 071 RP --- 10:051349 18:135447 --:------ 3220 005 00C0130ECC
        SZ_NAME: "dhw_flow_rate",
        RQ: r"^00$",
        RP: r"^00[0-9A-F]{4}$",
    },
    Code._1300: {  # cv water pressure (usu. for ch)
        SZ_NAME: "ch_pressure",
        RQ: r"^00$",
        RP: r"^00[0-9A-F]{4}$",
    },
    Code._1470: {  # programme_scheme, HVAC (1470, 1F70, 22B0)
        SZ_NAME: "programme_scheme",
        RQ: r"^00$",
        I_: r"^00[0-9A-F]{14}$",
        W_: r"^00[0-9A-F]{2}0{4}800{6}$",
    },
    Code._1F09: {  # system_sync - FF (I), 00 (RP), F8 (W, after 1FC9)
        SZ_NAME: "system_sync",
        I_: r"^(00|01|DB|FF)[0-9A-F]{4}$",  # FF is evohome, DB is Hometronics
        RQ: r"^00$",
        RP: r"^00[0-9A-F]{4}$",  # xx-secs
        W_: r"^F8[0-9A-F]{4}$",
    },
    Code._1F41: {  # dhw_mode
        SZ_NAME: "dhw_mode",
        I_: r"^(00|01)(00|01|FF)0[0-5]F{6}(([0-9A-F]){12})?$",
        RQ: r"^(00|01)$",  # will accept: r"^(00|01)(00)$"
        W_: r"^(00|01)(00|01|FF)0[0-5]F{6}(([0-9A-F]){12})?$",
        SZ_LIFESPAN: td(hours=4),
    },
    Code._1F70: {  # programme_config, HVAC (1470, 1F70, 22B0)
        SZ_NAME: "programme_config",
        I_: r"^00[0-9A-F]{30}$",
        RQ: r"^00[0-9A-F]{30}$",
        W_: r"^00[0-9A-F]{30}$",
    },
    Code._1FC9: {  # rf_bind
        SZ_NAME: "rf_bind",  # idx-code-dev_id
        RQ: r"^00$",
        RP: r"^((0[0-9A-F]|F[69ABCF]|[0-9A-F]{2})([0-9A-F]{10}))+$",
        I_: r"^((0[0-9A-F]|F[69ABCF]|[0-9A-F]{2})([0-9A-F]{10}))+|00|21$",  # NOTE: payload can be 00
        W_: r"^((0[0-9A-F]|F[69ABCF]|[0-9A-F]{2})([0-9A-F]{10}))+$",
    },
    Code._1FCA: {  # unknown_1fca
        SZ_NAME: "message_1fca",
        RQ: r"^00$",
        RP: r"^((0[0-9A-F]|F[9ABCF]|90)([0-9A-F]{10}))+$",  # xx-code-dev_id
        I_: r"^((0[0-9A-F]|F[9ABCF])([0-9A-F]{10}))+$",
        W_: r"^((0[0-9A-F]|F[9ABCF])([0-9A-F]{10}))+$",
    },
    Code._1FD0: {  # unknown_1fd0
        SZ_NAME: "message_1fd0",
        RQ: r"^00$",
        RP: r"^00",
    },
    Code._1FD4: {  # opentherm_sync
        SZ_NAME: "opentherm_sync",
        I_: r"^00([0-9A-F]{4})$",
    },
    Code._2210: {  # unknown_2210, HVAC,
        SZ_NAME: "unknown_2210",
        I_: r"^00[0-9A-F]{82}$",
        RQ: r"^00$",
    },
    Code._2249: {  # setpoint_now?
        SZ_NAME: "setpoint_now",  # setpt_now_next
        I_: r"^(0[0-9A-F]{13}){1,2}$",
    },  # TODO: This could be an array
    Code._22C9: {  # setpoint_bounds (was: ufh_setpoint)
        SZ_NAME: "setpoint_bounds",
        I_: r"^(0[0-9A-F][0-9A-F]{8}0[12]){1,4}(0[12]03)?$",  # (0[12]03)? only if len(array) == 1
        W_: r"^(0[0-9A-F][0-9A-F]{8}0[12])$",  # never an array
    },
    Code._22D0: {  # unknown_22d0, HVAC system switch?
        SZ_NAME: "message_22d0",
        I_: r"^(00|03)",
        W_: r"^03",
    },
    Code._22D9: {  # boiler_setpoint
        SZ_NAME: "boiler_setpoint",
        RQ: r"^00$",
        RP: r"^00[0-9A-F]{4}$",
    },
    Code._22E0: {  # unknown_22e0, HVAC, NB: no I
        SZ_NAME: "unknown_22e0",
        RQ: r"^00$",
        RP: r"^00[0-9A-F]{6}$",
    },
    Code._22E5: {  # unknown_22e5, HVAC, NB: no I
        SZ_NAME: "unknown_22e5",
        RQ: r"^00$",
        RP: r"^00[0-9A-F]{6}$",
    },
    Code._22E9: {  # unknown_22e9, HVAC, NB: no I
        SZ_NAME: "unknown_22e9",
        RQ: r"^00$",
        RP: r"^00[0-9A-F]{6}$",
    },
    Code._22F1: {  # fan_mode, HVAC
        SZ_NAME: "fan_mode",
        RQ: r"^00$",
        RP: r"^00[0-9A-F]{4}$",
        I_: r"^(00|63)(0[0-9A-F]){1,2}$",
    },
    Code._22F2: {  # unknown_22f2, HVAC, NB: no I
        SZ_NAME: "unknown_22f2",
        RQ: r"^00$",  # (00|01)?
        RP: r"^00[0-9A-F]{4}(01[0-9A-F]{4})?$",
    },
    Code._22F3: {  # fan_boost, HVAC
        SZ_NAME: "fan_boost",
        I_: r"^(00|63)(021E)?[0-9A-F]{4}([0-9A-F]{8})?$",
    },  # minutes only?
    Code._22F4: {  # unknown_22f4, HVAC
        SZ_NAME: "unknown_22f4",
        I_: r"^00[0-9A-F]{24}$",
        RQ: r"^00$",
    },
    Code._22F7: {  # fan_bypass_mode (% open), HVAC
        SZ_NAME: "fan_bypass_mode",
        I_: r"^00([0-9A-F]{2}){1,2}$",  # RP is the same
        RQ: r"^00$",
        W_: r"^00[0-9A-F]{2}(EF)?$",
    },
    Code._22F8: {  # fan_22f8 (moisture scenario?), HVAC
        SZ_NAME: "fan_22f8",
        RQ: r"^00$",
        I_: r"^00[0-9A-F]{4}$",
    },
    Code._22B0: {  # programme_status, HVAC (1470, 1F70, 22B0)
        SZ_NAME: "programme_status",
        W_: r"^00[0-9A-F]{2}$",
        I_: r"^00[0-9A-F]{2}$",
    },
    Code._2309: {  # setpoint
        SZ_NAME: "setpoint",
        I_: r"^(0[0-9A-F]{5})+$",
        W_: r"^0[0-9A-F]{5}$",
        # RQ --- 12:010740 01:145038 --:------ 2309 003 03073A # No RPs
        RQ: r"^0[0-9A-F]([0-9A-F]{4})?$",  # NOTE: 12 uses: r"^0[0-9A-F]$"
        SZ_LIFESPAN: td(minutes=30),
    },
    Code._2349: {  # zone_mode
        SZ_NAME: "zone_mode",
        I_: r"^0[0-9A-F]{5}0[0-4][0-9A-F]{6}([0-9A-F]{12})?$",
        W_: r"^0[0-9A-F]{5}0[0-4][0-9A-F]{6}([0-9A-F]{12})?$",
        # .W --- 18:141846 01:050858 --:------ 2349 013 02-0960-04-FFFFFF-0409160607E5
        # .W --- 18:141846 01:050858 --:------ 2349 007 02-08FC-01-FFFFFF
        RQ: r"^0[0-9A-F](00|[0-9A-F]{12})?$",
        # RQ --- 22:070483 01:063844 --:------ 2349 007 06-0708-03-000027
        SZ_LIFESPAN: td(hours=4),
    },
    Code._2389: {  # unknown_2389 - CODE_IDX_COMPLEX?
        # .I 024 03:052382 --:------ 03:052382 2389 003 02001B
        SZ_NAME: "unknown_2389",
        I_: r"^0[0-4][0-9A-F]{4}$",
    },
    Code._2400: {  # unknown_2400, from OTB
        SZ_NAME: "message_2400",
        RQ: r"^00$",
        RP: r"^00",
    },
    Code._2401: {  # unknown_2401, from OTB
        SZ_NAME: "message_2401",
        RQ: r"^00$",
        RP: r"^00",
    },
    Code._2410: {  # unknown_2410, from OTB
        SZ_NAME: "message_2410",
        RQ: r"^00$",
        RP: r"^00",
    },
    Code._2411: {  # fan_params, HVAC
        SZ_NAME: "fan_params",
        I_: r"^(00|01|15|16|17|21)00[0-9A-F]{6}([0-9A-F]{8}){4}[0-9A-F]{4}$",
        RQ: r"^(00|01|15|16|17|21)00[0-9A-F]{2}((00){19})?$",
        W_: r"^(00|01|15|16|17|21)00[0-9A-F]{6}[0-9A-F]{8}(([0-9A-F]{8}){3}[0-9A-F]{4})?$",
    },
    Code._2420: {  # unknown_2420, from OTB
        SZ_NAME: "message_2420",
        RQ: r"^00$",
        RP: r"^00",
    },
    Code._2D49: {  # unknown_2d49
        SZ_NAME: "message_2d49",
        # 10:14:08.526 045  I --- 01:023389 --:------ 01:023389 2D49 003 010000
        # 10:14:12.253 047  I --- 01:023389 --:------ 01:023389 2D49 003 00C800
        # 10:14:12.272 047  I --- 01:023389 --:------ 01:023389 2D49 003 01C800
        # 10:14:12.390 049  I --- 01:023389 --:------ 01:023389 2D49 003 880000
        # 10:14:12.399 048  I --- 01:023389 --:------ 01:023389 2D49 003 FD0000
        I_: r"^(0[0-9A-F]|88|F6|FD)[0-9A-F]{2}(00||FF)$",
    },  # seen with Hometronic systems
    Code._2E04: {  # system_mode
        SZ_NAME: "system_mode",
        I_: r"^0[0-7][0-9A-F]{12}0[01]$",
        RQ: r"^FF$",
        W_: r"^0[0-7][0-9A-F]{12}0[01]$",
        SZ_LIFESPAN: td(hours=4),
    },
    Code._2E10: {  # presence_detect - HVAC
        SZ_NAME: "presence_detect",
        I_: r"^00(00|01)(00)?$",
    },
    Code._30C9: {  # temperature
        SZ_NAME: "temperature",
        I_: r"^(0[0-9A-F][0-9A-F]{4})+$",
        RQ: r"^0[0-9A-F](00)?$",  # TODO: officially: r"^0[0-9A-F]$"
        RP: r"^0[0-9A-F][0-9A-F]{4}$",  # Null: r"^0[0-9A-F]7FFF$"
        SZ_LIFESPAN: td(hours=1),
    },
    Code._3110: {  # ufc_demand - HVAC
        SZ_NAME: "ufc_demand",
        I_: r"^(00|01)00[0-9A-F]{2}(00|10|20)",  # (00|10|20|FF)???
    },
    Code._3120: {  # unknown_3120 - Error Report?
        SZ_NAME: "message_3120",
        I_: r"^00[0-9A-F]{10}FF$",  # only ever: 34:/0070B0000000FF
        RQ: r"^00$",  # 20: will RP an RQ?
        # RP: r"^00[0-9A-F]{10}FF$",  # only ever: 20:/0070B000009CFF
    },
    Code._313E: {  # unknown_313e, HVAC, NB: no I
        SZ_NAME: "unknown_313e",
        RQ: r"^00$",
        RP: r"^00[0-9A-F]{20}$",
    },
    Code._313F: {  # datetime (time report)
        SZ_NAME: "datetime",
        I_: r"^00[0-9A-F]{16}$",  # NOTE: RP is same
        RQ: r"^00$",
        W_: r"^00[0-9A-F]{16}$",
        SZ_LIFESPAN: td(seconds=3),
    },
    Code._3150: {  # heat_demand, also fans with preheat
        SZ_NAME: "heat_demand",
        I_: r"^((0[0-9A-F])[0-9A-F]{2}|FC[0-9A-F]{2})+$",
        SZ_LIFESPAN: td(minutes=20),
    },
    Code._31D9: {  # fan_state
        SZ_NAME: "fan_state",
        # I_: r"^(00|21)[0-9A-F]{32}$",
        # I_: r"^(00|01|21)[0-9A-F]{4}((00|FE)(00|20){12}(00|08))?$",
        I_: r"^(00|01|15|16|17|21)[0-9A-F]{4}(([0-9A-F]{2})(00|20){0,12}(00|01|04|08)?)?$",  # 00-0004-FE
        RQ: r"^(00|01|15|16|17|21)$",
    },
    Code._31DA: {  # hvac_state (fan_state_extended)
        SZ_NAME: "hvac_state",
        I_: r"^(00|01|15|16|17|21)[0-9A-F]{56}(00|20)?$",
        RQ: r"^(00|01|15|16|17|21)$",
        # RQ --- 32:168090 30:082155 --:------ 31DA 001 21
    },
    Code._31E0: {  # fan_demand
        # 10:15:42.712 077  I --- 29:146052 32:023459 --:------ 31E0 003 0000C8
        # 10:21:18.549 078  I --- 29:146052 32:023459 --:------ 31E0 003 000000
        # 07:56:50.522 095  I --- --:------ --:------ 07:044315 31E0 004 00006E00
        SZ_NAME: "fan_demand",
        I_: r"^00([0-9A-F]{4}){1,3}(00|FF)?$",
    },
    Code._3200: {  # boiler (or CV?) output temp
        SZ_NAME: "boiler_output",
        I_: r"^00[0-9A-F]{4}$",
        RQ: r"^00$",
    },
    Code._3210: {  # boiler (or CV?) return temp
        SZ_NAME: "boiler_return",
        RQ: r"^00$",
        RP: r"^00[0-9A-F]{4}$",
    },
    Code._3220: {  # opentherm_msg
        SZ_NAME: "opentherm_msg",
        RQ: r"^00[0-9A-F]{8}$",
        RP: r"^00[0-9A-F]{8}$",
    },
    Code._3221: {  # unknown_3221, from OTB
        SZ_NAME: "message_3221",
        RQ: r"^00$",
        RP: r"^00",
    },
    Code._3222: {  # unknown_3222, HVAC, NB: no I
        SZ_NAME: "unknown_3222",
        RQ: r"^00$",
        RP: r"^00[0-9A-F]{4,24}$",
    },
    Code._3223: {  # unknown_3223, from OTB
        SZ_NAME: "message_3223",
        RQ: r"^00$",
        RP: r"^00",
    },
    Code._3B00: {  # actuator_sync, NOTE: no RQ
        SZ_NAME: "actuator_sync",
        I_: r"^(00|FC)(00|C8)$",
    },
    Code._3EF0: {  # actuator_state
        SZ_NAME: "actuator_state",
        # .I --- 13:106039 --:------ 13:106039 3EF0 003 00-C8FF
        # .I --- 21:038634 --:------ 21:038634 3EF0 006 00-0000-0A0200  #                            # Itho spIDer
        # .I --- 10:030051 --:------ 10:030051 3EF0 009 00-0010-000000-020A64
        # .I --- 08:031043 31:077159 --:------ 3EF0 020 00-1191A72044399D2A50DE43F920478AF7185F3F  # # Jasper BLOB
        I_: r"^..((00|C8)FF|[0-9A-F]{10}|[0-9A-F]{16}|[0-9A-F]{38})$",
        RQ: r"^00(00)?$",
        RP: r"^00((00|C8)FF|[0-9A-F]{10}|[0-9A-F]{16})$",
    },
    Code._3EF1: {  # actuator_cycle
        SZ_NAME: "actuator_cycle",
        # RQ --- 31:004811 13:077615 --:------ 3EF1 001 00
        # RP --- 13:077615 31:004811 --:------ 3EF1 007 00024D001300FF
        # RQ --- 22:068154 13:031208 --:------ 3EF1 002 0000
        # RP --- 13:031208 22:068154 --:------ 3EF1 007 00024E00E000FF
        # RQ --- 31:074182 08:026984 --:------ 3EF1 012 0005D1341DA39B8C7DAFD4C1
        # RP --- 08:026984 31:074182 --:------ 3EF1 018 001396A7E087922FA77794280B66BE16A975
        RQ: r"^00((00)?|[0-9A-F]{22})$",  # NOTE: latter is Japser
        RP: r"^00([0-9A-F]{12}|[0-9A-F]{34})$",  # NOTE: latter is Japser
    },
    Code._4401: {  # unknown_4401 - HVAC
        SZ_NAME: "unknown_4401",
        I_: r"^[0-9A-F]{40}$",
        RP: r"^00$",
        RQ: r"^[0-9A-F]{40}$",
        W_: r"^[0-9A-F]{40}$",
    },
    Code._4E01: {  # xxx (HVAC) - Itho Spider
        SZ_NAME: "hvac_4e01",
        I_: r"^00([0-9A-F]{4}){3,12}00$",
    },
    Code._4E02: {  # xxx (HVAC) - Itho Spider
        SZ_NAME: "hvac_4e02",
        I_: r"^00([0-9A-F]{4}){3,12}(02|03|04|05)([0-9A-F]{4}){3,12}$",
    },
    Code._4E04: {  # xxx (HVAC) - Itho Spider
        SZ_NAME: "hvac_4e04",
        I_: r"^00(00|01|02)[0-9A-F]{2}$",
        W_: r"^00(00|01|02)[0-9A-F]{2}$",
    },
    Code._4E0D: {  # xxx (HVAC) - Itho Spider
        SZ_NAME: "hvac_4e0d",
        I_: r"^(01|02)(00|01)$",
    },
    Code._4E15: {  # xxx (HVAC) - Itho Spider
        SZ_NAME: "hvac_4e15",
        I_: r"^000[0-7]$",
    },
    Code._4E16: {  # xxx (HVAC) - Itho Spider
        SZ_NAME: "hvac_4e16",
        I_: r"^00(00){6}$",
    },
    Code._PUZZ: {
        SZ_NAME: "puzzle_packet",
        I_: r"^00(([0-9A-F]){2})+$",
    },
}
CODE_NAME_LOOKUP = {k: v["name"] for k, v in CODES_SCHEMA.items()}


for code in CODES_SCHEMA.values():  # map any (missing) RPs to I_s
    if RQ in code and RP not in code and I_ in code:
        code[RP] = code[I_]
#
# .I --- 01:210309 --:------ 01:210309 0009 006 FC00FFF900FF
CODES_WITH_ARRAYS: dict[Code, list[int | tuple[str, ...]]] = {  # 000C/1FC9 are special
    Code._0005: [4, ("34",)],
    Code._0009: [3, ("01", "12", "22")],
    Code._000A: [6, ("01", "12", "22")],  # single element I after a W
    Code._2309: [3, ("01", "12", "22")],
    Code._30C9: [3, ("01", "12", "22")],
    Code._2249: [7, ("23",)],
    Code._22C9: [6, ("02",)],
    Code._3150: [2, ("02",)],
}  # TODO dex: element_length, src.type(s) (and dst.type too)
#
RQ_IDX_COMPLEX: list[Code] = [
    Code._0005,  # context: zone_type
    Code._000A,  # optional payload
    Code._000C,  # context: index, zone_type
    Code._0016,  # optional payload
    Code._0100,  # optional payload
    Code._0404,  # context: index, fragment_idx (fragment_header)
    Code._0418,  # context: index
    Code._10A0,  # optional payload
    Code._1100,  # optional payload
    Code._2309,  # optional payload
    Code._2349,  # optional payload
    Code._3220,  # context: msg_id, and payload
]
RQ_NO_PAYLOAD: list[Code] = [
    k
    for k, v in CODES_SCHEMA.items()
    if v.get(RQ)
    in (r"^FF$", r"^00$", r"^00(00)?$", r"^0[0-9A-F](00)?$", r"^0[0-9A-F]00$")
]
RQ_NO_PAYLOAD.extend((Code._0418,))


########################################################################################
# IDX:_xxxxxx: index (and context)

# all known codes should be in only one of IDX_COMPLEX, IDX_NONE, IDX_SIMPLE

# IDX_COMPLEX - *usually has* a context, but doesn't satisfy criteria for IDX_SIMPLE:
CODE_IDX_ARE_COMPLEX: set[Code] = {
    Code._0005,
    Code._000C,  # idx = fx(payload[0:4])
    # Code._0404,  # use "HW" for idx if payload[4:6] == "23"  # TODO: should be used
    Code._0418,  # log_idx (payload[4:6])  #  null RPs are missing an idx
    Code._1100,
    Code._3220,  # data_id (payload[4:6])
}  # TODO: 0005 to ..._NONE?

# IDX_SIMPLE - *can have* a context, but sometimes not (usu. 00): only ever payload[:2],
# either a zone_idx, domain_id or (UFC) circuit_idx (or array of such, i.e. seqx[:2])

_SIMPLE_IDX = ("^0[0-9A-F]", "^(0[0-9A-F]", "^((0[0-9A-F]", "^(00|01)")
CODE_IDX_ARE_SIMPLE: set[Code] = {
    k
    for k, v in CODES_SCHEMA.items()
    for verb in (RQ, I_)
    if k not in CODE_IDX_ARE_COMPLEX and v.get(verb, "").startswith(_SIMPLE_IDX)
}
CODE_IDX_ARE_SIMPLE |= {
    Code._22D0,
    Code._2411,
    Code._31D9,
    Code._31DA,
    Code._3B00,
    Code._4E0D,
}

# IDX_NONE - *never has* a context: most payloads start 00, but no context even if the
# payload starts with something else (e.g. 2E04)
CODE_IDX_ARE_NONE: set[Code] = {
    k
    for k, v in CODES_SCHEMA.items()
    if k not in CODE_IDX_ARE_COMPLEX | CODE_IDX_ARE_SIMPLE
    and ((RQ in v and v[RQ][:3] == "^00") or (I_ in v and v[I_][:3] == "^00"))
}
CODE_IDX_ARE_NONE |= {Code._22F3, Code._2389, Code._2E04, Code._4401}

# CODE_IDX_DOMAIN - NOTE: not necc. mutex with other 3
CODE_IDX_DOMAIN: dict[Code, str] = {
    Code._0001: "^F[ACF])",
    Code._0008: "^F[9AC]",
    Code._0009: "^F[9AC]",
    Code._1100: "^FC",
    Code._1FC9: "^F[9ABCF]",
    Code._3150: "^FC",
    Code._3B00: "^FC",
}


#
########################################################################################
# CODES_BY_DEV_SLUG - HEAT (CH/DHW) vs HVAC (ventilation)
# TODO: 34: can 3220 - split out RND from THM/STA
_DEV_KLASSES_HEAT: dict[str, dict[Code, dict[VerbT, Any]]] = {
    DevType.RFG: {  # RFG100: RF to Internet gateway (and others)
        Code._0002: {RQ: {}},
        Code._0004: {I_: {}, RQ: {}},
        Code._0005: {RQ: {}},
        Code._0006: {RQ: {}},
        Code._000A: {RQ: {}},
        Code._000C: {RQ: {}},
        Code._000E: {W_: {}},
        Code._0016: {RP: {}},
        Code._0404: {RQ: {}, W_: {}},
        Code._0418: {RQ: {}},
        Code._10A0: {RQ: {}},
        Code._10E0: {I_: {}, RQ: {}, RP: {}},
        Code._1260: {RQ: {}},
        Code._1290: {I_: {}},
        Code._1F41: {RQ: {}},
        Code._1FC9: {RP: {}, W_: {}},
        Code._22D9: {RQ: {}},
        Code._2309: {I_: {}},
        Code._2349: {RQ: {}, RP: {}, W_: {}},
        Code._2E04: {RQ: {}, I_: {}, W_: {}},
        Code._30C9: {RQ: {}},
        Code._313F: {RQ: {}, RP: {}, W_: {}},
        Code._3220: {RQ: {}},
        Code._3EF0: {RQ: {}},
    },
    DevType.CTL: {  # e.g. ATC928: Evohome Colour Controller
        Code._0001: {W_: {}},
        Code._0002: {I_: {}, RP: {}},
        Code._0004: {I_: {}, RP: {}},
        Code._0005: {I_: {}, RP: {}},
        Code._0006: {RP: {}},
        Code._0008: {I_: {}},
        Code._0009: {I_: {}},
        Code._000A: {I_: {}, RP: {}},
        Code._000C: {RP: {}},
        Code._0016: {RQ: {}, RP: {}},
        Code._0100: {RP: {}},
        Code._01D0: {I_: {}},
        Code._01E9: {I_: {}},
        Code._0404: {I_: {}, RP: {}},
        Code._0418: {I_: {}, RP: {}},
        Code._1030: {I_: {}},
        Code._10A0: {I_: {}, RP: {}},
        Code._10E0: {RP: {}},
        Code._1100: {I_: {}, RQ: {}, RP: {}, W_: {}},
        Code._1260: {RP: {}},
        Code._1290: {RP: {}},
        Code._12B0: {I_: {}, RP: {}},
        Code._1F09: {I_: {}, RP: {}, W_: {}},
        Code._1FC9: {I_: {}, RQ: {}, RP: {}, W_: {}},
        Code._1F41: {I_: {}, RP: {}},
        Code._2249: {I_: {}},  # Hometronics, not Evohome
        Code._22D9: {RQ: {}},
        Code._2309: {I_: {}, RP: {}},
        Code._2349: {I_: {}, RP: {}},
        Code._2D49: {I_: {}},
        Code._2E04: {I_: {}, RP: {}},
        Code._30C9: {I_: {}, RP: {}},
        Code._313F: {I_: {}, RP: {}, W_: {}},
        Code._3150: {I_: {}},
        Code._3220: {RQ: {}},
        Code._3B00: {I_: {}},
        Code._3EF0: {RQ: {}},
    },
    DevType.PRG: {  # e.g. HCF82/HCW82: Room Temperature Sensor
        Code._0009: {I_: {}},
        Code._1090: {RP: {}},
        Code._10A0: {RP: {}},
        Code._1100: {I_: {}},
        Code._1F09: {I_: {}},
        Code._2249: {I_: {}},
        Code._2309: {I_: {}},
        Code._30C9: {I_: {}},
        Code._3B00: {I_: {}},
        Code._3EF1: {RP: {}},
    },
    DevType.THM: {  # e.g. Generic Thermostat
        Code._0001: {W_: {}},
        Code._0005: {I_: {}},
        Code._0008: {I_: {}},
        Code._0009: {I_: {}},
        Code._000A: {I_: {}, RQ: {}, W_: {}},
        Code._000C: {I_: {}},
        Code._000E: {I_: {}},
        Code._0016: {RQ: {}},
        Code._042F: {I_: {}},
        Code._1030: {I_: {}},
        Code._1060: {I_: {}},
        Code._1090: {RQ: {}},
        Code._10E0: {I_: {}},
        Code._1100: {I_: {}},
        Code._12C0: {I_: {}},
        Code._1F09: {I_: {}},
        Code._1FC9: {I_: {}},
        Code._22C9: {W_: {}},  # DT4R
        Code._2309: {I_: {}, RQ: {}, W_: {}},
        Code._2349: {RQ: {}, W_: {}},
        Code._30C9: {I_: {}},
        Code._3120: {I_: {}},
        Code._313F: {
            I_: {}
        },  # .W --- 30:253184 34:010943 --:------ 313F 009 006000070E0...
        Code._3220: {RP: {}},  # RND (using OT)
        Code._3B00: {I_: {}},
        Code._3EF0: {RQ: {}},  # when bound direct to a 13:
        Code._3EF1: {RQ: {}},  # when bound direct to a 13:
    },
    DevType.UFC: {  # e.g. HCE80/HCC80: Underfloor Heating Controller
        Code._0001: {RP: {}, W_: {}},  # TODO: Ix RP
        Code._0005: {RP: {}},
        Code._0008: {I_: {}},
        Code._000A: {RP: {}},
        Code._000C: {RP: {}},
        Code._1FC9: {I_: {}},
        Code._10E0: {I_: {}, RP: {}},
        Code._22C9: {I_: {}},  # NOTE: No RP
        Code._22D0: {I_: {}, RP: {}},
        Code._2309: {RP: {}},
        Code._3150: {I_: {}},
    },
    DevType.TRV: {  # e.g. HR92/HR91: Radiator Controller
        Code._0001: {W_: {r"^0[0-9A-F]"}},
        Code._0004: {RQ: {r"^0[0-9A-F]00$"}},
        Code._0016: {RQ: {}, RP: {}},
        Code._0100: {RQ: {r"^00"}},
        Code._01D0: {W_: {}},
        Code._01E9: {W_: {}},
        Code._1060: {I_: {r"^0[0-9A-F]{3}0[01]$"}},
        Code._10E0: {I_: {r"^00[0-9A-F]{30,}$"}},
        Code._12B0: {I_: {r"^0[0-9A-F]{3}00$"}},  # sends every 1h
        Code._1F09: {RQ: {r"^00$"}},
        Code._1FC9: {I_: {}, W_: {}},
        Code._2309: {I_: {r"^0[0-9A-F]{5}$"}},
        Code._30C9: {I_: {r"^0[0-9A-F]"}},
        Code._313F: {RQ: {r"^00$"}},
        Code._3150: {I_: {r"^0[0-9A-F]{3}$"}},
    },
    DevType.DHW: {  # e.g. CS92: (DHW) Cylinder Thermostat
        Code._0016: {RQ: {}},
        Code._1060: {I_: {}},
        Code._10A0: {RQ: {}},  # This RQ/07/10A0 includes a payload
        Code._1260: {I_: {}},
        Code._1FC9: {I_: {}},
    },
    DevType.OTB: {  # e.g. R8810/R8820: OpenTherm Bridge
        Code._0009: {I_: {}},  # 1/24h for a R8820 (not an R8810)
        Code._0150: {RP: {}},  # R8820A only?
        Code._042F: {I_: {}, RP: {}},
        Code._1081: {RP: {}},  # R8820A only?
        Code._1098: {RP: {}},  # R8820A only?
        Code._10A0: {RP: {}},
        Code._10B0: {RP: {}},  # R8820A only?
        Code._10E0: {I_: {}, RP: {}},
        Code._10E1: {RP: {}},  # R8820A only?
        Code._1260: {RP: {}},
        Code._1290: {RP: {}},
        Code._12F0: {RP: {}},  # R8820A only?
        Code._1300: {RP: {}},  # R8820A only?
        Code._1FC9: {I_: {}, W_: {}},
        Code._1FD0: {RP: {}},  # R8820A only?
        Code._1FD4: {I_: {}},  # 2/min for R8810, every ~210 sec for R8820
        Code._22D9: {RP: {}},
        Code._2400: {RP: {}},  # R8820A only?
        Code._2401: {RP: {}},  # R8820A only?
        Code._2410: {RP: {}},  # R8820A only?
        Code._2420: {RP: {}},  # R8820A only?
        Code._3150: {I_: {}},
        Code._3200: {RP: {}},  # R8820A only?
        Code._3210: {RP: {}},  # R8820A only?
        Code._3220: {RP: {}},
        Code._3221: {RP: {}},  # R8820A only?
        Code._3223: {RP: {}},  # R8820A only?
        Code._3EF0: {I_: {}, RP: {}},
        Code._3EF1: {RP: {}},
    },  # see: https://www.opentherm.eu/request-details/?post_ids=2944
    DevType.BDR: {  # e.g. BDR91A/BDR91T: Wireless Relay Box
        Code._0008: {RP: {}},  # doesn't RP/0009
        Code._0016: {RP: {}},
        # Code._10E0: {},  # 13: will not RP/10E0 # TODO: how to indicate that fact here
        Code._1100: {I_: {}, RP: {}},
        Code._11F0: {I_: {}},  # BDR91T in heatpump mode
        Code._1FC9: {RP: {}, W_: {}},
        Code._2D49: {I_: {}},  # BDR91T in heatpump mode
        Code._3B00: {I_: {}},
        Code._3EF0: {I_: {}},
        # RP: {},  # RQ --- 01:145038 13:237335 --:------ 3EF0 001 00
        Code._3EF1: {RP: {}},
    },
    DevType.OUT: {
        Code._0002: {I_: {}},
        Code._1FC9: {I_: {}},
    },  # i.e. HB85 (ext. temperature/luminosity(lux)), HB95 (+ wind speed)
    #
    DevType.JIM: {  # Jasper Interface Module, 08
        Code._0008: {RQ: {}},
        Code._10E0: {I_: {}},
        Code._1100: {I_: {}},
        Code._3EF0: {I_: {}},
        Code._3EF1: {RP: {}},
    },
    DevType.JST: {  # Jasper Stat, 31
        Code._0008: {I_: {}},
        Code._10E0: {I_: {}},
        Code._3EF1: {RQ: {}, RP: {}},
    },
    # DevType.RND: {  # e.g. TR87RF: Single (round) Zone Thermostat
    #     Code._0005: {I_: {}},
    #     Code._0008: {I_: {}},
    #     Code._000A: {I_: {}, RQ: {}},
    #     Code._000C: {I_: {}},
    #     Code._000E: {I_: {}},
    #     Code._042F: {I_: {}},
    #     Code._1060: {I_: {}},
    #     Code._10E0: {I_: {}},
    #     Code._12C0: {I_: {}},
    #     Code._1FC9: {I_: {}},
    #     Code._1FD4: {I_: {}},
    #     Code._2309: {I_: {}, RQ: {}, W_: {}},
    #     Code._2349: {RQ: {}},
    #     Code._30C9: {I_: {}},
    #     Code._3120: {I_: {}},
    #     Code._313F: {I_: {}},  # W --- 30:253184 34:010943 --:------ 313F 009 006000070E0...
    #     Code._3EF0: {I_: {}, RQ: {}},  # when bound direct to a 13:
    #     Code._3EF1: {RQ: {}},  # when bound direct to a 13:
    # },
    # DevType.DTS: {  # e.g. DTS92(E)
    #     Code._0001: {W_: {}},
    #     Code._0008: {I_: {}},
    #     Code._0009: {I_: {}},
    #     Code._000A: {I_: {}, RQ: {}, W_: {}},
    #     Code._0016: {RQ: {}},
    #     # "0B04": {I_: {}},
    #     Code._1030: {I_: {}},
    #     Code._1060: {I_: {}},
    #     Code._1090: {RQ: {}},
    #     Code._1100: {I_: {}},
    #     Code._1F09: {I_: {}},
    #     Code._1FC9: {I_: {}},
    #     Code._2309: {I_: {}, RQ: {}, W_: {}},
    #     Code._2349: {RQ: {}, W_: {}},
    #     Code._30C9: {I_: {}},
    #     Code._313F: {I_: {}},
    #     Code._3B00: {I_: {}},
    #     Code._3EF1: {RQ: {}},
    # },
    # DevType.HCW: {  # e.g. HCF82/HCW82: Room Temperature Sensor
    #     Code._0001: {W_: {}},
    #     Code._0002: {I_: {}},
    #     Code._0008: {I_: {}},
    #     Code._0009: {I_: {}},
    #     Code._1060: {I_: {}},
    #     Code._1100: {I_: {}},
    #     Code._1F09: {I_: {}},
    #     Code._1FC9: {I_: {}},
    #     Code._2309: {I_: {}},
    #     Code._2389: {I_: {}},
    #     Code._30C9: {I_: {}},
    # },
}
# TODO: add 1FC9 everywhere?
_DEV_KLASSES_HVAC: dict[str, dict[Code, dict[VerbT, Any]]] = {
    DevType.DIS: {  # Orcon RF15 Display: ?a superset of a REM
        Code._0001: {RQ: {}},
        Code._042F: {I_: {}},
        Code._10E0: {I_: {}, RQ: {}},
        Code._1470: {RQ: {}},
        Code._1FC9: {I_: {}},
        Code._1F70: {I_: {}},
        Code._22F1: {I_: {}},
        Code._22F3: {I_: {}},
        Code._22F7: {RQ: {}, W_: {}},
        Code._22B0: {W_: {}},
        Code._2411: {RQ: {}, W_: {}},
        Code._313F: {RQ: {}},
        Code._31DA: {RQ: {}},
    },
    DevType.RFS: {  # Itho spIDer: RF to Internet gateway (like a RFG100)
        Code._1060: {I_: {}},
        Code._10E0: {I_: {}, RP: {}},
        Code._12C0: {I_: {}},
        Code._22C9: {I_: {}},
        Code._22F1: {I_: {}},
        Code._22F3: {I_: {}},
        Code._2E10: {I_: {}},
        Code._30C9: {I_: {}},
        Code._3110: {I_: {}},
        Code._3120: {I_: {}},
        Code._31D9: {RQ: {}},
        Code._31DA: {RQ: {}},
        Code._3EF0: {I_: {}},
    },
    DevType.FAN: {
        Code._0001: {RP: {}},
        Code._0002: {I_: {}},
        Code._042F: {I_: {}},
        Code._10D0: {I_: {}, RP: {}},
        Code._10E0: {I_: {}, RP: {}},
        Code._1298: {I_: {}},
        Code._12A0: {I_: {}},
        Code._12C8: {I_: {}},
        Code._1470: {RP: {}},
        Code._1F09: {I_: {}, RP: {}},
        Code._1FC9: {W_: {}},
        Code._2210: {I_: {}, RP: {}},
        Code._22E5: {RP: {}},
        Code._22E9: {RP: {}},
        Code._22F1: {RP: {}},
        Code._22F2: {I_: {}, RP: {}},
        Code._22F3: {},
        Code._22F4: {I_: {}, RP: {}},
        Code._22F7: {I_: {}, RP: {}},
        Code._2411: {I_: {}, RP: {}},
        Code._2E10: {I_: {}},
        Code._3120: {I_: {}},
        Code._3150: {I_: {}},
        Code._313F: {I_: {}, RP: {}},
        Code._31D9: {I_: {}, RP: {}},
        Code._31DA: {I_: {}, RP: {}},
        # Code._31E0: {I_: {}},
        Code._3200: {I_: {}},
        Code._3222: {RP: {}},
    },
    DevType.CO2: {
        Code._042F: {I_: {}},
        Code._10E0: {I_: {}, RP: {}},
        Code._1298: {I_: {}},
        Code._1FC9: {I_: {}},
        Code._22F1: {RQ: {}},
        Code._2411: {RQ: {}},
        Code._2E10: {I_: {}},
        Code._3120: {I_: {}},
        Code._31DA: {RQ: {}},
        Code._31E0: {I_: {}},
    },
    DevType.HUM: {
        Code._042F: {I_: {}},
        Code._1060: {I_: {}},
        Code._10E0: {I_: {}},
        Code._12A0: {I_: {}},
        Code._1FC9: {I_: {}},
        Code._31DA: {RQ: {}},
        Code._31E0: {I_: {}},
    },
    DevType.REM: {  # HVAC: two-way switch; also an "06/22F1"?
        Code._0001: {RQ: {}},  # from a VMI (only?)
        Code._042F: {I_: {}},  # from a VMI (only?)
        Code._1060: {I_: {}},
        Code._10D0: {W_: {}},  # reset filter count from REM
        Code._10E0: {I_: {}, RQ: {}},  # RQ from a VMI (only?)
        Code._1470: {RQ: {}},  # from a VMI (only?)
        Code._1FC9: {I_: {}},
        Code._22F1: {I_: {}},
        Code._22F3: {I_: {}},
        Code._22F7: {RQ: {}, W_: {}},  # from a VMI (only?)
        Code._2411: {RQ: {}, W_: {}},  # from a VMI (only?)
        Code._313F: {RQ: {}, W_: {}},  # from a VMI (only?)
        Code._31DA: {RQ: {}},  # to a VMI (only?)
        # Code._31E0: {I_: {}},
    },  # https://www.ithodaalderop.nl/nl-NL/professional/product/536-0124
    # None: {  # unknown, TODO: make generic HVAC
    #     _4401: {I_: {}},
    # },
}

CODES_BY_DEV_SLUG: dict[str, dict[Code, dict[VerbT, Any]]] = {
    DevType.HGI: {  # HGI80: RF to (USB) serial gateway interface
        Code._PUZZ: {I_: {}, RQ: {}, W_: {}},
    },  # HGI80s can do what they like
    **{k: v for k, v in _DEV_KLASSES_HVAC.items() if k is not None},
    **{k: v for k, v in _DEV_KLASSES_HEAT.items() if k is not None},
}

CODES_OF_HEAT_DOMAIN: tuple[Code] = sorted(  # type: ignore[assignment]
    tuple({c for k in _DEV_KLASSES_HEAT.values() for c in k}) + (Code._0B04, Code._2389)
)
CODES_OF_HVAC_DOMAIN: tuple[Code] = sorted(  # type: ignore[assignment]
    tuple({c for k in _DEV_KLASSES_HVAC.values() for c in k})
    + (Code._22F8, Code._4401, Code._4E01, Code._4E02, Code._4E04)
)
CODES_OF_HEAT_DOMAIN_ONLY: tuple[Code, ...] = tuple(
    c for c in sorted(CODES_OF_HEAT_DOMAIN) if c not in CODES_OF_HVAC_DOMAIN
)
CODES_OF_HVAC_DOMAIN_ONLY: tuple[Code, ...] = tuple(
    c for c in sorted(CODES_OF_HVAC_DOMAIN) if c not in CODES_OF_HEAT_DOMAIN
)
_CODES_OF_BOTH_DOMAINS: tuple[Code, ...] = tuple(
    sorted(set(CODES_OF_HEAT_DOMAIN) & set(CODES_OF_HVAC_DOMAIN))
)
_CODES_OF_EITHER_DOMAIN: tuple[Code, ...] = tuple(
    sorted(set(CODES_OF_HEAT_DOMAIN) | set(CODES_OF_HVAC_DOMAIN))
)
_CODES_OF_NO_DOMAIN: tuple[Code, ...] = tuple(
    c for c in CODES_SCHEMA if c not in _CODES_OF_EITHER_DOMAIN
)

_CODE_FROM_NON_CTL: tuple[Code, ...] = tuple(
    dict.fromkeys(
        c
        for k, v1 in CODES_BY_DEV_SLUG.items()
        for c, v2 in v1.items()
        if k != DevType.CTL and (I_ in v2 or RP in v2)
    )
)
_CODE_FROM_CTL = _DEV_KLASSES_HEAT[DevType.CTL].keys()

_CODE_ONLY_FROM_CTL: tuple[Code, ...] = tuple(
    c for c in _CODE_FROM_CTL if c not in _CODE_FROM_NON_CTL
)
CODES_ONLY_FROM_CTL: tuple[Code, ...] = (
    Code._1030,
    Code._1F09,
    Code._22D0,
    Code._313F,
)  # I packets, TODO: 31Dx too?

#
########################################################################################
# Other Stuff

# ### WIP:
# _result = {}
# for domain in (_DEV_KLASSES_HVAC, ):
#     for klass, kv in domain.items():
#         if klass in (DEV_TYPE.DIS, DEV_TYPE.RFS):
#             continue
#         for code, cv in kv.items():
#             for verb in cv:
#                 _result.update({(verb, code): _result.get((verb, code), 0) + 1})

# _HVAC_VC_PAIR_BY_CLASS = {
#     (v, c): k
#     for c, cv in kv.items()
#     for v in cv
#     for k, kv in _DEV_KLASSES_HVAC.items()
#     if (v, c) in [k for k, v in _result.items() if v == 1]
# }


_HVAC_VC_PAIR_BY_CLASS: dict[DevType, tuple[tuple[VerbT, Code], ...]] = {
    DevType.CO2: ((I_, Code._1298),),
    DevType.FAN: ((I_, Code._31D9), (I_, Code._31DA), (RP, Code._31DA)),
    DevType.HUM: ((I_, Code._12A0),),
    DevType.REM: ((I_, Code._22F1), (I_, Code._22F3)),
}
HVAC_KLASS_BY_VC_PAIR: dict[tuple[VerbT, Code], DevType] = {
    t: k for k, v in _HVAC_VC_PAIR_BY_CLASS.items() for t in v
}


SZ_DESCRIPTION: Final = "description"
SZ_MIN_VALUE: Final = "min_value"
SZ_MAX_VALUE: Final = "max_value"
SZ_PRECISION: Final = "precision"
SZ_DATA_TYPE: Final = "data_type"

_22F1_MODE_ITHO: dict[str, str] = {
    "00": "off",  # not seen
    "01": "trickle",  # not seen
    "02": "low",
    "03": "medium",
    "04": "high",  # aka boost with 22F3
}

_22F1_MODE_NUAIRE: dict[str, str] = {
    "02": "normal",
    "03": "boost",  # aka purge
    "09": "heater_off",
    "0A": "heater_auto",
}  # DRI-ECO-2S (normal/boost only), DRI-ECO-4S

_22F1_MODE_ORCON: dict[str, str] = {
    "00": "away",
    "01": "low",
    "02": "medium",
    "03": "high",  # # The order of the next two may be swapped
    "04": "auto",  # #   economy, as per RH and CO2 <= 1150 ppm (unsure which is which)
    "05": "auto_alt",  # comfort, as per RH and CO2 <=  950 ppm (unsure which is which)
    "06": "boost",
    "07": "off",
}

_22F1_MODE_VASCO: dict[str, str] = {  # for VASCO D60 and ClimaRad Minibox remotes
    "00": "off",
    "01": "away",  # 000106 minimum
    "02": "low",  # 000206
    "03": "medium",  # 000306
    "04": "high",  # 000406, aka boost with 22F3
    "05": "auto",
}

_22F1_SCHEMES: dict[str, dict[str, str]] = {
    "itho": _22F1_MODE_ITHO,
    "nuaire": _22F1_MODE_NUAIRE,
    "orcon": _22F1_MODE_ORCON,
    "vasco": _22F1_MODE_VASCO,
}

# unclear if true for only Orcon/*all* models
_2411_PARAMS_SCHEMA: dict[str, dict[str, Any]] = {
    "31": {  # slot 09
        SZ_DESCRIPTION: "Time to change filter (days)",
        SZ_MIN_VALUE: 0,
        SZ_MAX_VALUE: 1800,
        SZ_PRECISION: 30,
        SZ_DATA_TYPE: "10",
    },
    "3D": {  # slot 00
        SZ_DESCRIPTION: "Away mode Supply fan rate (%)",
        SZ_MIN_VALUE: 0.0,
        SZ_MAX_VALUE: 0.4,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "0F",
    },
    "3E": {  # slot 01
        SZ_DESCRIPTION: "Away mode Exhaust fan rate (%)",
        SZ_MIN_VALUE: 0.0,
        SZ_MAX_VALUE: 0.4,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "0F",
    },
    "3F": {  # slot 02
        SZ_DESCRIPTION: "Low mode Supply fan rate (%)",
        SZ_MIN_VALUE: 0.0,
        SZ_MAX_VALUE: 0.8,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "0F",
    },
    "40": {  # slot 03
        SZ_DESCRIPTION: "Low mode Exhaust fan rate (%)",
        SZ_MIN_VALUE: 0.0,
        SZ_MAX_VALUE: 0.8,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "0F",
    },
    "41": {  # slot 04
        SZ_DESCRIPTION: "Medium mode Supply fan rate (%)",
        SZ_MIN_VALUE: 0.0,
        SZ_MAX_VALUE: 1.0,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "0F",
    },
    "42": {  # slot 05
        SZ_DESCRIPTION: "Medium mode Exhaust fan rate (%)",
        SZ_MIN_VALUE: 0.1,
        SZ_MAX_VALUE: 1.0,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "0F",
    },
    "43": {  # slot 06
        SZ_DESCRIPTION: "High mode Supply fan rate (%)",
        SZ_MIN_VALUE: 0.0,
        SZ_MAX_VALUE: 1.0,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "0F",
    },
    "44": {  # slot 07
        SZ_DESCRIPTION: "High mode Exhaust fan rate (%)",
        SZ_MIN_VALUE: 0.1,
        SZ_MAX_VALUE: 1.0,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "0F",
    },
    "4E": {  # slot 0A
        SZ_DESCRIPTION: "Moisture scenario position (0=medium, 1=high)",
        SZ_MIN_VALUE: 0,
        SZ_MAX_VALUE: 1,
        SZ_PRECISION: 1,
        SZ_DATA_TYPE: "00",
    },
    "52": {  # slot 0B
        SZ_DESCRIPTION: "Sensor sensitivity (%)",
        SZ_MIN_VALUE: 0,
        SZ_MAX_VALUE: 25.0,
        SZ_PRECISION: 0.1,
        SZ_DATA_TYPE: "0F",
    },
    "54": {  # slot 0C
        SZ_DESCRIPTION: "Moisture sensor overrun time (mins)",
        SZ_MIN_VALUE: 15,
        SZ_MAX_VALUE: 60,
        SZ_PRECISION: 1,
        SZ_DATA_TYPE: "00",
    },
    "75": {  # slot 0D
        SZ_DESCRIPTION: "Comfort temperature (°C)",
        SZ_MIN_VALUE: 0.0,
        SZ_MAX_VALUE: 30.0,
        SZ_PRECISION: 0.01,
        SZ_DATA_TYPE: 92,
    },
    "95": {  # slot 08
        SZ_DESCRIPTION: "Boost mode Supply/exhaust fan rate (%)",
        SZ_MIN_VALUE: 0.0,
        SZ_MAX_VALUE: 1.0,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "0F",
    },
}

# ventilation speed description
_31D9_FAN_INFO_VASCO: dict[int, str] = {
    0x00: "off",
    0x01: "1 (trickle)",  # aka low
    0x02: "2 (low)",  # aka medium
    0x03: "3 (medium)",  # aka high
    0x04: "4 (boost)",
    0x05: "auto",
    0xC8: "III (boost)",  # same code sent for speed II and III, mode manual
    0x50: "I (low)",
    0x1E: "0 (very low)",
}

# ventilation speed
_31DA_FAN_INFO: dict[int, str] = {
    0x00: "off",
    0x01: "speed 1, low",  # aka low
    0x02: "speed 2, medium",  # aka medium
    0x03: "speed 3, high",  # aka high
    0x04: "speed 4",
    0x05: "speed 5",
    0x06: "speed 6",
    0x07: "speed 7",
    0x08: "speed 8",
    0x09: "speed 9",
    0x0A: "speed 10",
    0x0B: "speed 1 temporary override",  # timer
    0x0C: "speed 2 temporary override",  # timer
    0x0D: "speed 3 temporary override",  # timer/boost? (timer 1, 2, 3)
    0x0E: "speed 4 temporary override",
    0x0F: "speed 5 temporary override",
    0x10: "speed 6 temporary override",
    0x11: "speed 7 temporary override",
    0x12: "speed 8 temporary override",
    0x13: "speed 9 temporary override",
    0x14: "speed 10 temporary override",
    0x15: "away",  # absolute minimum speed
    0x16: "absolute minimum",  # trickle?
    0x17: "boost",  # absolute maximum",  # boost?
    0x18: "auto",
    0x19: "auto_night",
    0x1A: "-unknown 0x1A-",
    0x1B: "-unknown 0x1B-",
    0x1C: "-unknown 0x1C-",
    0x1D: "-unknown 0x1D-",
    0x1E: "-unknown 0x1E-",
    0x1F: "-unknown 0x1F-",
}


#
########################################################################################
# CODES_BY_ZONE_TYPE
#
# RAMSES_ZONES: dict[str, str] = {
#     "ALL": {
#         Code._0004: {I_: {}, RP: {}},
#         Code._000C: {RP: {}},
#         Code._000A: {I_: {}, RP: {}},
#         Code._2309: {I_: {}, RP: {}},
#         Code._2349: {I_: {}, RP: {}},
#         Code._30C9: {I_: {}, RP: {}},
#     },
#     ZON_ROLE.RAD: {
#         Code._12B0: {I_: {}, RP: {}},
#         "3150a": {},
#     },
#     ZON_ROLE.ELE: {
#         Code._0008: {I_: {}},
#         Code._0009: {I_: {}},
#     },
#     ZON_ROLE.VAL: {
#         Code._0008: {I_: {}},
#         Code._0009: {I_: {}},
#         "3150a": {},
#     },
#     ZON_ROLE.UFH: {
#         Code._3150: {I_: {}},
#     },
#     ZON_ROLE.MIX: {
#         Code._0008: {I_: {}},
#         "3150a": {},
#     },
#     ZON_ROLE.DHW: {
#         Code._10A0: {RQ: {}, RP: {}},
#         Code._1260: {I_: {}},
#         Code._1F41: {I_: {}},
#     },
# }
# RAMSES_ZONES_ALL = RAMSES_ZONES.pop("ALL")
# RAMSES_ZONES_DHW = RAMSES_ZONES[ZON_ROLE.DHW]
# [RAMSES_ZONES[k].update(RAMSES_ZONES_ALL) for k in RAMSES_ZONES if k != ZON_ROLE.DHW]
