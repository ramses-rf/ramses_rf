"""RAMSES RF - Dataclass to Pre-0.60 Dictionary Adapter.

This module provides serialization utilities to convert typed PayloadBase instances
into pre-0.60 dictionary formats for parity testing and backward compatibility.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ramses_rf.typing import PayloadT

    from .base import PayloadBase


# Map internal Pythonic dataclass field names to pre-0.60 dictionary schema keys
_FIELD_TO_LEGACY_KEY: dict[str, str] = {
    "dhw_index": "dhw_index",
    "zone_index": "zone_index",
    "zone_index_raw": "zone_index_raw",
    "sub_index": "sub_index",
    "domain_index": "domain_index",
    "domain_or_zone_index": "domain_or_zone_index",
    "ot_index": "ot_index",
    "opentherm_index": "ot_index",
    "mod_index": "mod_index",
    "modulation_index": "mod_index",
    "capacity_index": "capacity_index",
    "capacity_value": "capacity_val",
    "fault_index": "fault_index",
    "mode_index": "mode_index",
    "day_index": "day_index",
    "setpoint_index": "setpoint_index",
    "log_index": "log_index",
    "config_index": "config_index",
    "config_value": "config_val",
    "flag_value": "flag_val",
    "mode_value": "mode_val",
    "parameter_index": "param_index",
    "parameter_value": "param_val",
    "parameter_id": "param_id",
    "min_value_scaled": "min_val_scaled",
    "max_value_scaled": "max_val_scaled",
    "request_reason": "req_reason",
    "is_daylight_saving": "is_dst",
    "_hvac_index": "_hvac_index",
    "ufh_index": "ufh_index",
}


def payload_to_dict(payload: PayloadBase, *, legacy: bool = False) -> PayloadT:
    """Convert a PayloadBase instance into a dictionary structure.

    :param payload: The payload dataclass instance to convert.
    :type payload: PayloadBase
    :param pre-0.60: If True, translate field names to pre-0.60 abbreviated keys.
    :type pre-0.60: bool
    :returns: Dictionary representation of the payload attributes.
    :rtype: dict[str, Any]
    :raises TypeError: If the input is not a dataclass instance.
    """
    if not is_dataclass(type(payload)):
        err_msg = f"Expected dataclass instance, got {type(payload).__name__}"
        raise TypeError(err_msg)
    if legacy:
        return {
            _FIELD_TO_LEGACY_KEY.get(k, k): v
            for k, v in asdict(payload).items()
            if not k.startswith("_")
        }
    return {k: v for k, v in asdict(payload).items() if not k.startswith("_")}
