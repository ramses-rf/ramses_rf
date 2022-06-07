#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
"""RAMSES RF - RAMSES-II compatible Packet processor."""

from typing import Dict, List, Tuple

__all__ = ["check_signature"]

# incl. date_1. NB: date_2 can vary (firmware date), and _unknown_1 can vary for R8810A
# fmt: off
__DEVICE_INFO_DB: Dict[str, Tuple[str, str, str, str]] = {
    # Heating (device type implies a slug only for these)...
    "0002FF0119FFFFFFFF": ("CTL", "01", "2014-01-16", "EvoTouch Colour"),  # .              ATC928-G3-0xx Evo Mk3 - EvoTouch Colour (WiFi, 12 zones)
    "0002FF0163FFFFFFFF": ("CTL", "01", "2013-08-01", "Evo Color"),  # .                    ATP928-G2-080 Evo Mk2 - Color (no WiFi)
    "0002FFFF17FFFFFFFF": ("CTL", "01", "2012-05-11", "IONA RAI Prototype"),  # .           ATC928-G1-000 Evo Mk1 - Monochrone (?prototype, 8 zones)
    "0003FF0203FFFF0001": ("UFC", "02", "2017-11-06", "HCE80 V3.10 061117"),
    "0002FF0412FFFFFFFF": ("TRV", "04", "2014-03-13", "HR92 Radiator Ctrl."),
    "0002FF050BFFFFFFFF": ("TRV", "04", "2017-03-07", "HR91 Radiator Ctrl."),
    "0001C8810B0700FEFF": ("OTB", "10", "2019-08-20", "R8820"),
    "0002FF0A0CFFFFFFFF": ("OTB", "10", "2014-07-31", "R8810A Bridge"),
    "0002FF1E01FFFFFFFF": ("RFG", "30", "2013-12-04", "Internet Gateway"),
    "0002FF1E03FFFFFFFF": ("RFG", "30", "2017-04-21", "Internet Gateway"),
    "0001C8380A0100F1FF": ("RND", "34", "2014-11-03", "T87RF2025"),  # .                    Round
    "0001C8380F0100F1FF": ("RND", "34", "2017-05-03", "T87RF2025"),  # .                    Round
    # Odd - Jasper kit (device type implies a slug here too)
    "0002FF0802FFFFFFFE": ("JIM", "08", "2017-11-10", "Jasper EIM"),
    "0002FF1F02FFFFFFFF": ("JST", "31", "2016-08-04", "Jasper Stat TXXX"),
    # FAN - some are HRUs, others extraction only
    "000100140C06010000": ("FAN", "20", "0000-00-00", ""),  # .                             31D9
    "000100140D06130000": ("FAN", "20", "0000-00-00", ""),  # .                             31D9
    "0001001B190B010000": ("FAN", "20", "0000-00-00", ""),  # .                             31D9
    "0001001B221201FEFF": ("FAN", "20", "2015-05-12", "CVE-RF"),  # .                       31D9, 31DA
    "0001001B271501FEFF": ("FAN", "20", "2016-11-03", "CVE-RF"),  # .                       31D9, 31DA (RP|12A0, RP|3120, both N/A)
    "0001001B281501FEFF": ("FAN", "20", "2016-11-11", "CVE-RF"),  # .                       31D9, 31DA
    "0001001B2E1901FEFF": ("FAN", "37", "2017-11-29", "CVE-RF"),  # .                       31D9, 31DA
    "0001001B311901FEFF": ("FAN", "37", "2018-05-14", "CVE-RF"),  # .                       31D9, 31DA
    "0001001B361B01FEFF": ("FAN", "37", "2019-04-11", "CVE-RF"),  # .                       31D9, 31DA, and 12C8
    "0001001B371B01FEFF": ("FAN", "37", "2019-08-29", "CVE-RF"),  # .                       31D9, 31DA
    "0001001B381B01FEFF": ("FAN", "37", "2020-02-14", "CVE-RF"),  # .                       31D9, 31DA (and I|042F, I|3120)
    "0001C8260A0367FFFF": ("FAN", "29", "0000-00-00", "VMC-15RP01"),
    "0001C8260D0467FFFF": ("FAN", "29", "0000-00-00", "VMC-15RP01"),  # .                   31D9
    "0001C83A0F0866FFFF": ("FAN", "32", "0000-00-00", "VMD-17RPS01"),  # .                  31D9, 31DA
    "0001C87D140D67FEFF": ("FAN", "32", "2019-12-23", "VMD-15RMS64"),  # .                  31D9, 31DA (and I|042F)
    "0001C895050567FEFF": ("FAN", "32", "2020-07-01", "VMD-15RMS86"),  # .                  31DA, 12A0, 22F7, 2411 (and I|042F, I|313F, I|3120)
    "0001C8950B0A67FEFF": ("FAN", "32", "2021-01-21", "VMD-15RMS86"),  # .                  31D9, 31DA, 12A0, 313F (and I|042F, I|3120)
    "0001C90011006CFEFF": ("FAN", "30", "2016-09-09", "BRDG-02JAS01"),  # .      NOTE: 30:  31D9, 31DA, 1F09 (a PIV)
    # CO2 - some have PIR
    "00010028080101FEFF": ("CO2", "37", "2019-04-29", "VMS-12C39"),  # .                    1298, 31E0, 2E10, 3120, and I|22F1!
    "00010028090101FEFF": ("CO2", "37", "2021-01-20", "VMS-12C39"),  # .                    1298, 31E0, 2E10, 3120 (and I|042F)
    "0001C822060166FEFF": ("CO2", "37", "2016-12-22", "VMS-17C01"),  # .                    1298, 31E0
    "0001C85701016CFFFF": ("CO2", "32", "2016-06-17", "VMS-23C33"),  # .                    1298, 31E0 (and I|042F)
    # HUM
    "0001C825050266FFFF": ("HUM", "29", "2017-04-19", "VMS-17HB01"),  # .                   12A0, 31E0, 1060
    "0001C85802016CFFFF": ("HUM", "32", "2016-07-12", "VMS-23HB33"),  # .                   12A0, 31E0, 1060 (and I|042F)
    "0001C85803016CFFFF": ("HUM", "32", "2016-09-12", "VMS-23HB33"),  # .                   12A0, 31E0, 1060 (and I|042F)
    # SWI
    "0001C827050167FFFF": ("SWI", "29", "0000-00-00", "VMN-15LF01"),  # .                   22F1, 22F3
    "0001C827070167FFFF": ("SWI", "29", "0000-00-00", "VMN-15LF01"),  # .                   22F1, 22F3
    "0001C827090167FFFF": ("SWI", "29", "2019-02-13", "VMN-15LF01"),  # .                   22F1, 22F3 (and I|042F)
    "0001C85A01016CFFFF": ("SWI", "32", "2016-06-01", "VMN-23LMH23"),  # .        zxdavb    22F1, 1060, 4-way?
    # SWI (display)
    "0001C894030167FFFF": ("SWI", "37", "2020-08-27", "VMI-15WSJ53"),  # .                  22F1, ?22F3
    # RFS...
    "000100222B0001FEFF": ("RFS", "21", "2019-07-10", "CCU-12T20"),  # .           spIDer   1060, 12C0, 22C9,             2E10, 30C9, 3110, 3120, 3EF0
    "00010022340001FEFF": ("RFS", "21", "2020-08-05", "CCU-12T20"),  # .           spIDer   1060, 12C0, 22C9, 22F1, 22F3, 2E10, 30C9, 3110, 3120, 3EF0
    # TBA - broken as 18:...
    "0001FA100A0001FEFE": ("FAN", "18", "2019-04-11", "BRDG-02A55"),  # .        NOTE: 18:  31D9, 31DA, 1F09
    "0001FA100B0001FEFE": ("FAN", "18", "2019-07-22", "BRDG-02A55"),  # .        NOTE: 18:  31D9, 31DA, 1F09
    "0001C8820C006AFEFF": ("FAN", "18", "2019-08-20", "HRA82"),  # .             NOTE: 18:  (only I|042F, I|10E0)
}
# fmt: on

__DEVICE_INFO: Dict[str, List[str]] = {
    t: [k for k, v in __DEVICE_INFO_DB.items() if v[1] == t]
    for t in sorted(dict.fromkeys(v[1] for v in __DEVICE_INFO_DB.values()))
}  # convert to {dev_type: [signature, ...]}


def check_signature(dev_type: str, signature: str) -> None:
    """Raise ValueError if the device type is not known to have the signature.

    e.g. '01' can imply '0002FF0119FFFFFFFF', but not '0001C8820C006AFEFF'
    """
    if not (sigs := __DEVICE_INFO.get(dev_type)) or signature not in sigs:
        raise ValueError(
            f"device type {dev_type} not known to have signature: {signature}"
        )


########################################################################################
# from: https://www.airios.eu/products

# BRDG - RF interface to RS485/Ethernet: for heating and ventilation.
# VMD - Heat recovery unit
# VMC - Mechanical extraction: To integrate in a single fan system
# VMI - User interface with display
# VMN -
# VMS - Sensors platform: CO2, humidity and temperature (and PIR?)

# BRDG-02JAS01 - PIV - Nuaire DriMaster PIV (input)
# CCU-12T20    - RFS - RF gateway (spIDer, Fifthplay Home Area Manager)
# CVE-RF       - FAN -
# HRA82        -
# VMC-15RP01   - Orcon unit (senseair.com)
# VMD-15RMS64  - FAN - Orcon HRC-350 (Ventiline)
# VMD-15RMS86  -
# VMD-17RPS01  -
# VMN-15LF01   -
# VMN-23LMH23  - SWI - 4 button RF Switch
# VMS-02MC05   - CO2 -
# VMS-12C39    - CO2 - CO2 sensor, incl. integrated control, PIR
# VMS-15CM17   - CO2 - CO2 Sensor
# VMS-17C01    -
# VMS-17HB01   -
# VMS-23C33    - CO2 - CO2 Sensor
# VMS-23HB33   - HUM - RH/Temp Sensor
# MVS-15RHB    - FAN - Orcon Smartline FAN (incl. Moisture sensor and transmitter)

# CVD ???
# CVE coupled ventilation system (equipment)
# DCV demand controlled ventilation
# IAQ indoor air quality
# HRA
# RFT - RF
# HRU heat recovery unit (MVHR), aka WTW (in. dutch)
