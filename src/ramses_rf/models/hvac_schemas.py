from dataclasses import dataclass


@dataclass
class FanParamInfo:
    description: str
    min_val: float
    max_val: float
    precision: float
    data_type: str
    data_unit: str


_22F1_MODE_MAX: dict[str, str | None] = {
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
    "itho": _22F1_MODE_ITHO,
    "nuaire": _22F1_MODE_NUAIRE,
    "orcon": _22F1_MODE_ORCON,
    "vasco": _22F1_MODE_VASCO,
}

_2411_PARAMS_SCHEMA: dict[str, FanParamInfo] = {
    "01": FanParamInfo("Support", 255, 255, 1, "20", ""),
    "31": FanParamInfo("Time to change filter (days)", 0, 1800, 30, "10", "days"),
    "3D": FanParamInfo("Away mode Supply fan rate (%)", 0.0, 0.4, 0.005, "0F", "%"),
    "3E": FanParamInfo("Away mode Exhaust fan rate (%)", 0.0, 0.4, 0.005, "0F", "%"),
    "3F": FanParamInfo("Low mode Supply fan rate (%)", 0.0, 0.75, 0.005, "0F", "%"),
    "40": FanParamInfo("Low mode Exhaust fan rate (%)", 0.0, 0.75, 0.005, "0F", "%"),
    "41": FanParamInfo("Medium mode Supply fan rate (%)", 0.0, 0.75, 0.005, "0F", "%"),
    "42": FanParamInfo("Medium mode Exhaust fan rate (%)", 0.0, 0.75, 0.005, "0F", "%"),
    "43": FanParamInfo("High mode Supply fan rate (%)", 0.0, 1.0, 0.005, "0F", "%"),
    "44": FanParamInfo("High mode Exhaust fan rate (%)", 0.0, 1.0, 0.005, "0F", "%"),
    "4B": FanParamInfo("Night mode timer (minutes)", 0, 180, 10, "00", "minutes"),
    "4C": FanParamInfo("Away mode timer (minutes)", 0, 180, 10, "00", "minutes"),
    "4E": FanParamInfo("High mode timer (minutes)", 0, 180, 10, "00", "minutes"),
    "50": FanParamInfo("Low mode timer (minutes)", 0, 180, 10, "00", "minutes"),
    "52": FanParamInfo("Trickle mode timer (minutes)", 0, 180, 10, "00", "minutes"),
    "64": FanParamInfo("Exhaust temperature limit (°C)", 5, 25, 1, "92", "°C"),
    "65": FanParamInfo("Supply temperature limit (°C)", 5, 25, 1, "92", "°C"),
    "C8": FanParamInfo("Bypass mode (auto/manual)", 255, 255, 1, "20", ""),
    "CA": FanParamInfo("Bypass override timer (minutes)", 0, 180, 10, "00", "minutes"),
    "CB": FanParamInfo("Summer mode limit (°C)", 15, 25, 1, "92", "°C"),
    "CE": FanParamInfo("Winter mode limit (°C)", 5, 15, 1, "92", "°C"),
    "CF": FanParamInfo("Bypass hysteresis (°C)", 0.5, 5, 0.5, "92", "°C"),
    "F5": FanParamInfo("Pre-heater limit (°C)", -15, -5, 1, "92", "°C"),
    "F6": FanParamInfo("Pre-heater hysteresis (°C)", 0.5, 5, 0.5, "92", "°C"),
}
