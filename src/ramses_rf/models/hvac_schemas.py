"""HVAC parameter schema definitions and mode lookup tables."""

from dataclasses import dataclass


@dataclass
class FanParamInfo:
    """Metadata descriptor for an HVAC fan configuration parameter."""

    description: str
    min_value: float
    max_value: float
    precision: float
    data_type: str
    data_unit: str


_22F1_MODE_MAX: dict[str, str | None] = {
    "climarad": "06",
    "itho": "04",
    "nuaire": "0A",
    "vasco": "06",
    "orcon": "07",
}

_22F1_MODE_ITHO: dict[str, str] = {
    "00": "off",
    "01": "trickle",
    "02": "low",
    "03": "medium",
    "04": "high",
}
_22F1_MODE_NUAIRE: dict[str, str] = {
    "02": "normal",
    "03": "boost",
    "09": "heater_off",
    "0A": "heater_auto",
}
_22F1_MODE_ORCON: dict[str, str] = {
    "00": "away",
    "01": "low",
    "02": "medium",
    "03": "high",
    "04": "auto",
    "05": "auto_alt",
    "06": "boost",
    "07": "off",
}
_22F1_MODE_VASCO: dict[str, str] = {
    "00": "off",
    "01": "away",
    "02": "low",
    "03": "medium",
    "04": "high",
    "05": "auto",
}

_22F1_SCHEMES: dict[str, dict[str, str]] = {
    "climarad": _22F1_MODE_VASCO,
    "itho": _22F1_MODE_ITHO,
    "nuaire": _22F1_MODE_NUAIRE,
    "orcon": _22F1_MODE_ORCON,
    "vasco": _22F1_MODE_VASCO,
}

# Canonical HVAC parameter schema: the single source of truth for what each
# 2411 parameter means. Exposed as a legacy dict-shaped view at
# protocol.ramses._2411_PARAMS_SCHEMA (payload decode + ramses_cc entity
# metadata) and consumed directly here by the command builders.
#
# Note: semantics of a given param_id can differ per device family (e.g.
# ClimaRad Ventura vs Orcon); where the two former tables conflicted, the
# legacy protocol.ramses entries won as they were validated against real
# traffic for years (and restore the pre-0.59.1 write behaviour).
_2411_PARAMS_SCHEMA: dict[str, FanParamInfo] = {
    "01": FanParamInfo("Support", 255, 255, 1, "20", ""),
    "07": FanParamInfo(  # ClimaRad Ventura (issue 740)
        "Base ventilation enable", 0, 1, 1, "00", ""
    ),
    "31": FanParamInfo(  # slot 09 (FANs produced after 2021)
        "Time to change filter (days)", 0, 1800, 30, "10", "days"
    ),
    "3D": FanParamInfo(
        "Away mode Supply fan rate (%)", 0.0, 0.4, 0.005, "0F", "%"
    ),
    "3E": FanParamInfo(  # slot 01; dtype 90 per issues 342/317
        "Away mode Exhaust fan rate (%)", 0.0, 0.4, 0.005, "90", "%"
    ),
    "3F": FanParamInfo(  # slot 02
        "Low mode Supply fan rate (%)", 0.0, 0.8, 0.005, "0F", "%"
    ),
    "40": FanParamInfo(  # slot 03
        "Low mode Exhaust fan rate (%)", 0.0, 0.8, 0.005, "0F", "%"
    ),
    "41": FanParamInfo(  # slot 04; Orcon FAN responds with 0.0
        "Medium mode Supply fan rate (%)", 0.1, 1.0, 0.005, "0F", "%"
    ),
    "42": FanParamInfo(  # slot 05
        "Medium mode Exhaust fan rate (%)", 0.1, 1.0, 0.005, "0F", "%"
    ),
    "43": FanParamInfo(  # slot 06
        "High mode Supply fan rate (%)", 0.1, 1.0, 0.005, "0F", "%"
    ),
    "44": FanParamInfo(  # slot 07
        "High mode Exhaust fan rate (%)", 0.1, 1.0, 0.005, "0F", "%"
    ),
    "4B": FanParamInfo(  # slot 09 (FANs produced before 2021); also check 22F7
        "(Test) Bypass Valve (0=auto, 1=open, 2=closed)", 0, 2, 1, "00", ""
    ),
    "4C": FanParamInfo(  # ClimaRad Ventura
        "Unknown parameter 4C (ClimaRad Ventura)", 0, 0xFFFFFFFF, 1, "10", ""
    ),
    "4E": FanParamInfo(  # slot 0A
        "Moisture scenario position (0=medium, 1=high)", 0, 1, 1, "00", ""
    ),
    "50": FanParamInfo(  # Orcon mode timer
        "Low mode timer (minutes)", 0, 180, 10, "00", "minutes"
    ),
    "52": FanParamInfo(  # slot 0B
        "Sensor sensitivity (%)", 0, 25.0, 0.1, "01", "%"
    ),
    "54": FanParamInfo(  # slot 0C
        "Moisture sensor overrun time (mins)", 15, 60, 1, "00", "min"
    ),
    "64": FanParamInfo("Exhaust temperature limit (°C)", 5, 25, 1, "92", "°C"),
    "65": FanParamInfo("Supply temperature limit (°C)", 5, 25, 1, "92", "°C"),
    "75": FanParamInfo(  # slot 0D
        "Comfort temperature (°C)", 0.0, 30.0, 0.01, "92", "°C"
    ),
    "88": FanParamInfo(  # ClimaRad Ventura
        "Timer configuration (ClimaRad Ventura)", 0, 0xFFFFFFFF, 1, "10", ""
    ),
    "95": FanParamInfo(  # slot 08
        "Boost mode Supply/exhaust fan rate (%)", 0.0, 1.0, 0.005, "0F", "%"
    ),
    "C8": FanParamInfo("Bypass mode (auto/manual)", 255, 255, 1, "20", ""),
    "CA": FanParamInfo(
        "Bypass override timer (minutes)", 0, 180, 10, "00", "minutes"
    ),
    "CB": FanParamInfo("Summer mode limit (°C)", 15, 25, 1, "92", "°C"),
    "CE": FanParamInfo("Winter mode limit (°C)", 5, 15, 1, "92", "°C"),
    "CF": FanParamInfo("Bypass hysteresis (°C)", 0.5, 5, 0.5, "92", "°C"),
    "DA": FanParamInfo(  # ClimaRad Ventura
        "Unknown parameter DA (ClimaRad Ventura)", 0, 0xFFFFFFFF, 1, "10", ""
    ),
    "F5": FanParamInfo("Pre-heater limit (°C)", -15, -5, 1, "92", "°C"),
    "F6": FanParamInfo("Pre-heater hysteresis (°C)", 0.5, 5, 0.5, "92", "°C"),
}
