#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Contains e.g.:
:term:`CODES_SCHEMA` the master list of all known Ramses-II Code signatures
for both HEAT and HVAC.
:term:`_DEV_KLASSES_HEAT` defining Codes expected for each HEAT device class (SLUG).
:term:`_DEV_KLASSES_HVAC` defining Codes expected for each HVAC device class (SLUG).
:term:`_22F1_MODE_XXX` dicts defining valid fan commands.
:term:`_2411_PARAMS_SCHEMA` defining HVAC fan parameters.
"""

# TODO: code a lifespan for most packets

from __future__ import annotations

from typing import Any, Final

from ramses_tx.const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
    Verb,
)

from ..enums import DevType

SZ_LIFESPAN: Final = "lifespan"  # WIP


#
########################################################################################
# CODES_SCHEMA - HEAT (CH/DHW, Honeywell/Resideo) vs HVAC (ventilation, Itho/Orcon/etc.)

# The master list - all known codes are here, even if there's no corresponding parser
# Anything with a zone-index should start: ^0[0-9A-F], ^(0[0-9A-F], or ^((0[0-9A-F]

#
CODE_NAME_LOOKUP: dict[Code, str] = {
    Code._0001: "rf_unknown",
    Code._0002: "outdoor_sensor",
    Code._0004: "zone_name",
    Code._0005: "system_zones",
    Code._0006: "system_device_id",
    Code._0008: "relay_demand",
    Code._0009: "system_fault",
    Code._000A: "zone_config",
    Code._000C: "zone_devices",
    Code._000E: "oem_code",
    Code._0016: "system_mode",
    Code._0100: "language",
    Code._0150: "ot_0150",
    Code._01D0: "rf_bind",
    Code._01E9: "rf_bind",
    Code._01FF: "hvac_01ff",
    Code._0404: "schedule",
    Code._0418: "system_fault",
    Code._042F: "system_log_index",
    Code._0B04: "rf_bind",
    Code._1030: "system_sync",
    Code._1060: "dhw_temp",
    Code._1081: "heat_1081",
    Code._1090: "heat_1090",
    Code._1098: "ot_1098",
    Code._10A0: "dhw_params",
    Code._10B0: "ot_10b0",
    Code._10D0: "hvac_10d0",
    Code._10E0: "rf_bind",
    Code._10E1: "rf_bind",
    Code._10E2: "hvac_10e2",
    Code._1100: "rf_bind",
    Code._11F0: "dhw_11f0",
    Code._1260: "dhw_temp",
    Code._1280: "outdoor_humidity",
    Code._1290: "outdoor_temp",
    Code._1298: "co2_level",
    Code._12A0: "indoor_humidity",
    Code._12C0: "indoor_temp",
    Code._12C8: "air_quality",
    Code._12F0: "dhw_setpoint",
    Code._1300: "heat_1300",
    Code._1470: "hvac_1470",
    Code._1F09: "rf_bind",
    Code._1F41: "dhw_mode",
    Code._1F70: "hvac_1f70",
    Code._1FC9: "rf_bind",
    Code._1FD0: "ot_1fd0",
    Code._1FD4: "ot_1fd4",
    Code._2209: "hvac_2209",
    Code._2210: "exhaust_fan_speed",
    Code._2249: "hvac_2249",
    Code._22B0: "hvac_22b0",
    Code._22C9: "ufh_demand",
    Code._22D0: "hvac_22d0",
    Code._22D9: "hvac_22d9",
    Code._22F1: "fan_mode",
    Code._22F2: "hvac_22f2",
    Code._22F3: "fan_boost",
    Code._22F4: "fan_mode",
    Code._22F7: "bypass_mode",
    Code._22F8: "hvac_22f8",
    Code._2309: "setpoint",
    Code._2349: "heat_2349",
    Code._2389: "other_demand",
    Code._2400: "ot_2400",
    Code._2401: "ot_2401",
    Code._2410: "ot_2410",
    Code._2411: "fan_params",
    Code._2420: "ot_2420",
    Code._2D49: "other_demand",
    Code._2E04: "system_mode",
    Code._2E10: "hvac_2e10",
    Code._30C9: "temperature",
    Code._3110: "hvac_3110",
    Code._3120: "hvac_3120",
    Code._313F: "datetime",
    Code._3150: "heat_demand",
    Code._31D9: "fan_mode",
    Code._31DA: "ventilation_state",
    Code._31E0: "vent_demand",
    Code._3200: "heat_3200",
    Code._3210: "ot_3210",
    Code._3220: "opentherm_msg",
    Code._3221: "ot_3221",
    Code._3222: "rf_bind",
    Code._3223: "ot_3223",
    Code._3B00: "actuator_state",
    Code._3EF0: "actuator_state",
    Code._3EF1: "actuator_state",
    Code._4401: "hvac_4401",
    Code._4E0D: "hvac_4e0d",
    Code._4E16: "hvac_4e16",
    Code._PUZZ: "puzzle_packet",
}

CODES_WITH_ARRAYS: dict[Code, list[int | tuple[str, ...]]] = {
    Code._0005: [4, ("34",)],
    Code._0009: [3, ("01", "12", "22")],
    Code._000A: [6, ("01", "12", "22")],
    Code._2309: [3, ("01", "12", "22")],
    Code._30C9: [3, ("01", "02", "12", "22")],
    Code._2249: [7, ("23",)],
    Code._22C9: [6, ("02",)],
    Code._3150: [2, ("02",)],
}

RQ_INDEX_COMPLEX: list[Code] = [
    Code._0005,
    Code._000A,
    Code._000C,
    Code._0016,
    Code._0100,
    Code._0404,
    Code._0418,
    Code._10A0,
    Code._1100,
    Code._2309,
    Code._2349,
    Code._2411,
    Code._3220,
]

RQ_NO_PAYLOAD: list[Code] = [
    Code._0001,
    Code._0002,
    Code._0004,
    Code._0006,
    Code._0008,
    Code._000E,
    Code._0100,
    Code._01D0,
    Code._01E9,
    Code._0418,
    Code._0B04,
    Code._1030,
    Code._1060,
    Code._1081,
    Code._1090,
    Code._10A0,
    Code._10D0,
    Code._10E0,
    Code._10E1,
    Code._10E2,
    Code._1100,
    Code._11F0,
    Code._1260,
    Code._1280,
    Code._1290,
    Code._1298,
    Code._12A0,
    Code._12B0,
    Code._12C0,
    Code._12C8,
    Code._12F0,
    Code._1300,
    Code._1470,
    Code._1F09,
    Code._1F41,
    Code._1F70,
    Code._1FC9,
    Code._2209,
    Code._2210,
    Code._2249,
    Code._22B0,
    Code._22C9,
    Code._22D0,
    Code._22D9,
    Code._22F1,
    Code._22F3,
    Code._22F4,
    Code._22F7,
    Code._22F8,
    Code._2309,
    Code._2349,
    Code._2389,
    Code._2411,
    Code._2D49,
    Code._2E04,
    Code._2E10,
    Code._30C9,
    Code._3110,
    Code._3120,
    Code._313F,
    Code._3150,
    Code._31D9,
    Code._31DA,
    Code._31E0,
    Code._3200,
    Code._3210,
    Code._3221,
    Code._3222,
    Code._3223,
    Code._3B00,
    Code._3EF0,
    Code._3EF1,
    Code._4401,
    Code._4E0D,
    Code._4E16,
]

CODE_INDEX_ARE_COMPLEX: set[Code] = {
    Code._0005,
    Code._000C,
    Code._0418,
    Code._1100,
    Code._3220,
}

CODE_INDEX_ARE_SIMPLE: set[Code] = {
    Code._0002,
    Code._0004,
    Code._0008,
    Code._0009,
    Code._000A,
    Code._000E,
    Code._0016,
    Code._01D0,
    Code._01E9,
    Code._01FF,
    Code._0B04,
    Code._1030,
    Code._1060,
    Code._1081,
    Code._1090,
    Code._10A0,
    Code._10D0,
    Code._10E0,
    Code._10E1,
    Code._10E2,
    Code._11F0,
    Code._1260,
    Code._1280,
    Code._1290,
    Code._1298,
    Code._12A0,
    Code._12B0,
    Code._12C0,
    Code._12C8,
    Code._12F0,
    Code._1300,
    Code._1470,
    Code._1F09,
    Code._1F41,
    Code._1F70,
    Code._1FC9,
    Code._1FD0,
    Code._1FD4,
    Code._2209,
    Code._2210,
    Code._2249,
    Code._22B0,
    Code._22C9,
    Code._22D0,
    Code._22D9,
    Code._22F1,
    Code._22F2,
    Code._22F3,
    Code._22F4,
    Code._22F7,
    Code._22F8,
    Code._2309,
    Code._2349,
    Code._2389,
    Code._2400,
    Code._2401,
    Code._2410,
    Code._2411,
    Code._2420,
    Code._2D49,
    Code._2E10,
    Code._30C9,
    Code._3110,
    Code._3120,
    Code._313F,
    Code._3150,
    Code._31D9,
    Code._31DA,
    Code._31E0,
    Code._3200,
    Code._3210,
    Code._3221,
    Code._3222,
    Code._3223,
    Code._3B00,
    Code._3EF0,
    Code._3EF1,
    Code._4401,
    Code._4E0D,
    Code._4E16,
    Code._PUZZ,
}

CODE_INDEX_ARE_NONE: set[Code] = {
    Code._0001,
    Code._0006,
    Code._0100,
}
CODE_INDEX_ARE_NONE |= {Code._2389, Code._4401}

# CODE_INDEX_DOMAIN - NOTE: not necc. mutex with other 3
CODE_INDEX_DOMAIN: dict[Code, str] = {
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
_DEV_KLASSES_HEAT: dict[str, dict[Code, dict[Verb, Any]]] = {
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
        Code._1F09: {I_: {}},
        Code._1F41: {RQ: {}},
        Code._1FC9: {RP: {}, W_: {}},
        Code._22D9: {RQ: {}},
        Code._2309: {I_: {}},
        Code._2349: {RQ: {}, RP: {}, W_: {}},
        Code._2E04: {RQ: {}, I_: {}, W_: {}},
        Code._30C9: {RQ: {}},
        Code._313F: {RQ: {}, RP: {}, W_: {}},
        Code._31D9: {I_: {}},
        Code._31DA: {I_: {}, RQ: {}, RP: {}},
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
        Code._2209: {
            I_: {},
            W_: {},
        },  # ADDED: Allow CTL to receive DT4R bounds
        Code._22C9: {
            I_: {},
            W_: {},
        },  # ADDED: Allow CTL to receive DT4R bounds
        Code._22D9: {I_: {}, RQ: {}},
        Code._2309: {I_: {}, RP: {}},
        Code._2349: {I_: {}, RP: {}},
        Code._2D49: {I_: {}},
        Code._2E04: {I_: {}, RP: {}},
        Code._30C9: {I_: {}, RP: {}},
        Code._313F: {I_: {}, RP: {}, W_: {}},
        Code._3150: {I_: {}},
        Code._3220: {RQ: {}},
        Code._3B00: {I_: {}},
        Code._3EF0: {I_: {}, RQ: {}},
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
        Code._01FF: {I_: {}, RQ: {}},
        Code._042F: {I_: {}},
        Code._1030: {I_: {}},
        Code._1060: {I_: {}},
        Code._1090: {RQ: {}},
        Code._10E0: {I_: {}},
        Code._1100: {I_: {}},
        Code._12C0: {I_: {}},
        Code._1F09: {I_: {}},
        Code._1FC9: {I_: {}},
        Code._22C9: {
            I_: {},
            W_: {},
        },  # DT4R, Spider (I_ for Spider broadcasts)
        Code._22D0: {W_: {}},  # Spider master THM
        Code._2309: {I_: {}, RQ: {}, W_: {}},
        Code._2349: {RQ: {}, W_: {}},
        Code._30C9: {I_: {}},
        Code._3110: {I_: {}},  # Spider THM
        Code._3120: {I_: {}},
        Code._313F: {
            I_: {}
        },  # .W --- 30:253184 34:010943 --:------ 313F 009 006000070E0...
        Code._3220: {RP: {}},  # RND (using OT)
        Code._3B00: {I_: {}},
        Code._3EF0: {I_: {}, RQ: {}},  # when bound direct to a 13:
        Code._3EF1: {RQ: {}},  # when bound direct to a 13:
    },
    DevType.UFC: {  # e.g. HCE80/HCC80: Underfloor Heating Controller
        Code._0001: {RP: {}, W_: {}},  # TODO: Ix RP
        Code._0005: {RP: {}},
        Code._0008: {I_: {}},
        Code._000A: {RP: {}},
        Code._000C: {RP: {}},
        Code._1FC9: {I_: {}},
        Code._1FD4: {I_: {}},  # Spider Autotemp, slave 'ticker'
        Code._10E0: {I_: {}, RP: {}},
        Code._22C9: {I_: {}},  # NOTE: No RP
        Code._22D0: {I_: {}, RP: {}},
        Code._22F1: {I_: {}},  # Added for Phase 2.95 snapshot parity
        Code._2309: {RP: {}},
        Code._3110: {I_: {}},  # Spider Autotemp
        Code._3120: {I_: {}},  # Added for Phase 2.95 snapshot parity
        Code._3150: {I_: {}},
        Code._4E01: {I_: {}},  # Spider Autotemp Zone controller
        Code._4E02: {I_: {}},  # Added for Phase 2.95 snapshot parity
        Code._4E04: {
            I_: {},
            W_: {},
        },  # Added W_ for Phase 2.95 snapshot parity
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
_DEV_KLASSES_HVAC: dict[str, dict[Code, dict[Verb, Any]]] = {
    DevType.DIS: {  # Orcon RF15 Display: ?a superset of a REM
        Code._0001: {RQ: {}},
        Code._042F: {I_: {}},
        Code._10E0: {I_: {}, RQ: {}},
        Code._1470: {RQ: {}},
        Code._1FC9: {I_: {}, W_: {}},
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
        Code._1FC9: {I_: {}, W_: {}},
        Code._2210: {I_: {}, RP: {}},
        Code._22E0: {RP: {}},
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
        Code._313E: {RP: {}},
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
        Code._1298: {I_: {}, RP: {}},
        Code._1FC9: {I_: {}, W_: {}},
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
        Code._1FC9: {I_: {}, W_: {}},
        Code._31DA: {RQ: {}},
        Code._31E0: {I_: {}},
    },
    DevType.REM: {  # HVAC: two-way switch; also an "06/22F1"?
        Code._0001: {RQ: {}},  # from a VMI (only?)
        Code._042F: {I_: {}},  # from a VMI (only?)
        Code._1060: {I_: {}},
        Code._10D0: {
            RP: {},
            RQ: {},
            W_: {},
        },  # RQ/RP Orcon HRC, W=reset filter count from REM
        Code._10E0: {I_: {}, RQ: {}},  # RQ from a VMI (only?)
        Code._1470: {RQ: {}},  # from a VMI (only?)
        Code._1FC9: {I_: {}, W_: {}},
        Code._22F1: {I_: {}},
        Code._22F3: {I_: {}},
        Code._22F7: {RQ: {}, W_: {}},  # from a VMI (only?)
        # TODO: 2411 is listed for REM with "VMI only?" caveat, but a
        # normal REM does NOT send 2411 — only a DIS does.  The scan
        # engine and HvacTopologyHandler cannot distinguish DIS from REM
        # by VC pairs alone (see _HVAC_VC_PAIR_BY_CLASS).  When the
        # strategy pattern arrives (issue 939), a DisStrategy could use
        # 2411 frequency to distinguish.  See also: discovery_scan.py
        # _classify, pipeline/topology_handlers/hvac.py.
        Code._2411: {RQ: {}, W_: {}},  # from a VMI (only?)
        Code._313F: {RQ: {}, W_: {}},  # from a VMI (only?)
        Code._31DA: {RQ: {}},  # to a VMI (only?)
        # Code._31E0: {I_: {}},
    },  # https://www.ithodaalderop.nl/nl-NL/professional/product/536-0124
    # None: {  # unknown, TODO: make generic HVAC
    #     _4401: {I_: {}},
    # },
}

CODES_BY_DEV_SLUG: dict[str, dict[Code, dict[Verb, Any]]] = {
    DevType.HGI: {  # HGI80: RF to (USB) serial gateway interface
        Code._PUZZ: {I_: {}, RQ: {}, W_: {}},
    },  # HGI80s can do what they like
    **{k: v for k, v in _DEV_KLASSES_HVAC.items() if k is not None},
    **{k: v for k, v in _DEV_KLASSES_HEAT.items() if k is not None},
}

CODES_OF_HEAT_DOMAIN: tuple[Code, ...] = tuple(
    sorted(
        tuple({c for k in _DEV_KLASSES_HEAT.values() for c in k})
        + (Code._0B04, Code._2389)
    )
)
CODES_OF_HVAC_DOMAIN: tuple[Code, ...] = tuple(
    sorted(
        tuple({c for k in _DEV_KLASSES_HVAC.values() for c in k})
        + (Code._22F8, Code._4401, Code._4E01, Code._4E02, Code._4E04)
    )
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
    c for c in Code if c not in _CODES_OF_EITHER_DOMAIN
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
    # Code._22D0,  # also _W from the Spider master THM! issue #340
    Code._313F,
)  # I packets, TODO: 31Dx too? not 31D9/31DA!

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


_HVAC_VC_PAIR_BY_CLASS: dict[DevType, tuple[tuple[Verb, Code], ...]] = {
    DevType.CO2: ((I_, Code._1298),),
    DevType.FAN: ((I_, Code._31D9), (I_, Code._31DA), (RP, Code._31DA)),
    DevType.HUM: ((I_, Code._12A0),),
    DevType.REM: ((I_, Code._22F1), (I_, Code._22F3)),
    # TODO: DIS is not distinguishable from REM by VC pairs alone.
    # A DIS (Orcon RF15 Display) sends RQ 2411 and RQ 31DA as normal
    # behavior, but so does a REM (per the protocol table, with "VMI
    # only?" caveats).  The key difference is that a DIS sends 2411
    # routinely, while a bound REM only sends it when prompted by a VMI.
    # The scan engine's _classify cannot distinguish DIS from REM — it
    # falls to the 37: prefix fallback (REM) for both.  When the strategy
    # pattern arrives (issue 939), a DisStrategy could use 2411 frequency
    # or the presence of 1470/042F (DIS-only codes) to distinguish.
    # See also: discovery_scan.py _classify, hvac.py HvacTopologyHandler.
}
HVAC_KLASS_BY_VC_PAIR: dict[tuple[Verb, Code], DevType] = {
    t: k for k, v in _HVAC_VC_PAIR_BY_CLASS.items() for t in v
}


SZ_DESCRIPTION: Final = "description"
SZ_MIN_VALUE: Final = "min_value"
SZ_MAX_VALUE: Final = "max_value"
SZ_PRECISION: Final = "precision"
SZ_DATA_TYPE: Final = "data_type"
SZ_DATA_UNIT: Final = "data_unit"

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

_22F1_MODE_VASCO: dict[
    str, str
] = {  # for VASCO D60 and ClimaRad Minibox remotes
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
    "01": {  # all?
        SZ_DESCRIPTION: "Support",
        SZ_MIN_VALUE: 0xFF,  # None?
        SZ_MAX_VALUE: 0xFF,
        SZ_PRECISION: 1,
        SZ_DATA_TYPE: "20",
        SZ_DATA_UNIT: "",
    },
    "31": {  # slot 09 (FANs produced after 2021)
        SZ_DESCRIPTION: "Time to change filter (days)",
        SZ_MIN_VALUE: 0,
        SZ_MAX_VALUE: 1800,
        SZ_PRECISION: 30,
        SZ_DATA_TYPE: "10",
        SZ_DATA_UNIT: "days",
    },
    "3D": {  # slot 00
        SZ_DESCRIPTION: "Away mode Supply fan rate (%)",
        SZ_MIN_VALUE: 0.0,
        SZ_MAX_VALUE: 0.4,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "0F",
        SZ_DATA_UNIT: "%",
    },
    "3E": {  # slot 01
        SZ_DESCRIPTION: "Away mode Exhaust fan rate (%)",
        SZ_MIN_VALUE: 0.0,
        SZ_MAX_VALUE: 0.4,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "90",
        SZ_DATA_UNIT: "%",
    },
    "3F": {  # slot 02
        SZ_DESCRIPTION: "Low mode Supply fan rate (%)",
        SZ_MIN_VALUE: 0.0,
        SZ_MAX_VALUE: 0.8,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "0F",
        SZ_DATA_UNIT: "%",
    },
    "40": {  # slot 03
        SZ_DESCRIPTION: "Low mode Exhaust fan rate (%)",
        SZ_MIN_VALUE: 0.0,
        SZ_MAX_VALUE: 0.8,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "0F",
        SZ_DATA_UNIT: "%",
    },
    "41": {  # slot 04
        SZ_DESCRIPTION: "Medium mode Supply fan rate (%)",
        SZ_MIN_VALUE: 0.1,  # Orcon FAN responds with 0.0, but I guess this should be the same as for "42"
        SZ_MAX_VALUE: 1.0,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "0F",
        SZ_DATA_UNIT: "%",
    },
    "42": {  # slot 05
        SZ_DESCRIPTION: "Medium mode Exhaust fan rate (%)",
        SZ_MIN_VALUE: 0.1,
        SZ_MAX_VALUE: 1.0,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "0F",
        SZ_DATA_UNIT: "%",
    },
    "43": {  # slot 06
        SZ_DESCRIPTION: "High mode Supply fan rate (%)",
        SZ_MIN_VALUE: 0.1,
        SZ_MAX_VALUE: 1.0,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "0F",
        SZ_DATA_UNIT: "%",
    },
    "44": {  # slot 07
        SZ_DESCRIPTION: "High mode Exhaust fan rate (%)",
        SZ_MIN_VALUE: 0.1,
        SZ_MAX_VALUE: 1.0,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "0F",
        SZ_DATA_UNIT: "%",
    },
    "4B": {  # slot 09 (FANs produced before 2021) Also check code 22F7
        SZ_DESCRIPTION: "(Test) Bypass Valve (0=auto, 1=open, 2=closed)",
        SZ_MIN_VALUE: 0,
        SZ_MAX_VALUE: 2,
        SZ_PRECISION: 1,
        SZ_DATA_TYPE: "00",
        SZ_DATA_UNIT: "",
    },
    "4E": {  # slot 0A
        SZ_DESCRIPTION: "Moisture scenario position (0=medium, 1=high)",
        SZ_MIN_VALUE: 0,
        SZ_MAX_VALUE: 1,
        SZ_PRECISION: 1,
        SZ_DATA_TYPE: "00",
        SZ_DATA_UNIT: "",
    },
    "52": {  # slot 0B
        SZ_DESCRIPTION: "Sensor sensitivity (%)",
        SZ_MIN_VALUE: 0,
        SZ_MAX_VALUE: 25.0,
        SZ_PRECISION: 0.1,
        SZ_DATA_TYPE: "01",
        SZ_DATA_UNIT: "%",
    },
    "54": {  # slot 0C
        SZ_DESCRIPTION: "Moisture sensor overrun time (mins)",
        SZ_MIN_VALUE: 15,
        SZ_MAX_VALUE: 60,
        SZ_PRECISION: 1,
        SZ_DATA_TYPE: "00",
        SZ_DATA_UNIT: "min",
    },
    "75": {  # slot 0D
        SZ_DESCRIPTION: "Comfort temperature (°C)",
        SZ_MIN_VALUE: 0.0,
        SZ_MAX_VALUE: 30.0,
        SZ_PRECISION: 0.01,
        SZ_DATA_TYPE: "92",
        SZ_DATA_UNIT: "°C",
    },
    "95": {  # slot 08
        SZ_DESCRIPTION: "Boost mode Supply/exhaust fan rate (%)",
        SZ_MIN_VALUE: 0.0,
        SZ_MAX_VALUE: 1.0,
        SZ_PRECISION: 0.005,
        SZ_DATA_TYPE: "0F",
        SZ_DATA_UNIT: "%",
    },
    # ClimaRad Ventura-specific parameters (issue 740)
    "07": {
        SZ_DESCRIPTION: "Base ventilation enable",
        SZ_MIN_VALUE: 0,
        SZ_MAX_VALUE: 1,
        SZ_PRECISION: 1,
        SZ_DATA_TYPE: "00",
        SZ_DATA_UNIT: "",
    },
    "4C": {
        SZ_DESCRIPTION: "Unknown (ClimaRad Ventura)",
        SZ_MIN_VALUE: 0,
        SZ_MAX_VALUE: 0xFFFFFFFF,
        SZ_PRECISION: 1,
        SZ_DATA_TYPE: "10",
        SZ_DATA_UNIT: "",
    },
    "88": {
        SZ_DESCRIPTION: "Timer configuration (ClimaRad Ventura)",
        SZ_MIN_VALUE: 0,
        SZ_MAX_VALUE: 0xFFFFFFFF,
        SZ_PRECISION: 1,
        SZ_DATA_TYPE: "10",
        SZ_DATA_UNIT: "",
    },
    "DA": {
        SZ_DESCRIPTION: "Unknown (ClimaRad Ventura)",
        SZ_MIN_VALUE: 0,
        SZ_MAX_VALUE: 0xFFFFFFFF,
        SZ_PRECISION: 1,
        SZ_DATA_TYPE: "10",
        SZ_DATA_UNIT: "",
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
    0x1F: "-unknown 0x1F-",  # static field, used as filter in parser_31da so keep same
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

__all__ = [
    "CODE_NAME_LOOKUP",
    "CODES_BY_DEV_SLUG",
    "CODES_OF_HVAC_DOMAIN_ONLY",
    "HVAC_KLASS_BY_VC_PAIR",
    "_2411_PARAMS_SCHEMA",
    "SZ_DESCRIPTION",
    "SZ_MIN_VALUE",
    "SZ_MAX_VALUE",
    "SZ_PRECISION",
    "SZ_DATA_TYPE",
    "SZ_DATA_UNIT",
]
