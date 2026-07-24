#!/usr/bin/env python3

"""RAMSES RF - Transport layer configuration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Never, TypeVar

import voluptuous as vol

from ramses_tx.config import EngineConfig
from ramses_tx.const import DEV_TYPE_MAP, DEVICE_ID_REGEX, DevType
from ramses_tx.schemas import SZ_BLOCK_LIST, SZ_KNOWN_LIST

from .const import SZ_IS_BATTERY, SZ_POLLING_INTERVAL

_T = TypeVar("_T")


def ConvertNullToDict() -> Callable[[_T | None], _T | dict[Never, Never]]:
    """Return a validator that converts a null node value to an empty dictionary.

    :returns: A callable validator function for voluptuous schemas.
    :rtype: Callable[[_T | None], _T | dict[Never, Never]]
    """

    def convert_null_to_dict(node_value: _T | None) -> _T | dict[Never, Never]:
        if node_value is None:
            return {}
        return node_value

    return convert_null_to_dict


SZ_ALIAS: Final = "alias"
SZ_BOUND_TO: Final = "bound"
SZ_CLASS: Final = "class"
SZ_FAKED: Final = "faked"
SZ_SCHEME: Final = "scheme"

SCH_DEVICE_ID_ANY = vol.Match(DEVICE_ID_REGEX.ANY)
SCH_DEVICE_ID_SEN = vol.Match(DEVICE_ID_REGEX.SEN)
SCH_DEVICE_ID_CTL = vol.Match(DEVICE_ID_REGEX.CTL)
SCH_DEVICE_ID_DHW = vol.Match(DEVICE_ID_REGEX.DHW)
SCH_DEVICE_ID_HGI = vol.Match(DEVICE_ID_REGEX.HGI)
SCH_DEVICE_ID_APP = vol.Match(DEVICE_ID_REGEX.APP)
SCH_DEVICE_ID_BDR = vol.Match(DEVICE_ID_REGEX.BDR)
SCH_DEVICE_ID_UFC = vol.Match(DEVICE_ID_REGEX.UFC)

_SCH_TRAITS_DOMAINS = ("heat", "hvac")
_SCH_TRAITS_HVAC_SCHEMES = ("itho", "nuaire", "orcon", "vasco", "climarad")

SCH_POLLING_INTERVAL = vol.Schema({str: vol.All(int, vol.Range(min=0))})


def sch_global_traits_dict_factory(
    heat_traits: dict[vol.Optional, vol.Any] | None = None,
    hvac_traits: dict[vol.Optional, vol.Any] | None = None,
) -> tuple[dict[vol.Optional, vol.Any], vol.Any]:
    """Return a global traits dict with configurable extra traits.

    :param heat_traits: Optional extra traits dict for the heat domain.
    :type heat_traits: dict[vol.Optional, vol.Any] | None
    :param hvac_traits: Optional extra traits dict for the hvac domain.
    :type hvac_traits: dict[vol.Optional, vol.Any] | None
    :returns: A tuple containing the global traits schema dictionary and
        schema validator.
    :rtype: tuple[dict[vol.Optional, vol.Any], vol.Any]
    """
    heat_traits = heat_traits or {}
    hvac_traits = hvac_traits or {}

    SCH_TRAITS_BASE = vol.Schema(
        {
            vol.Optional(SZ_ALIAS, default=None): vol.Any(None, str),
            vol.Optional(SZ_FAKED, default=None): vol.Any(None, bool),
            vol.Optional(SZ_POLLING_INTERVAL, default=None): vol.Any(
                None, SCH_POLLING_INTERVAL
            ),
            vol.Optional(SZ_IS_BATTERY, default=False): vol.Any(None, bool),
            vol.Optional(vol.Remove("_note")): str,
        },
        extra=vol.PREVENT_EXTRA,
    )

    heat_slugs = list(
        str(s) for s in DEV_TYPE_MAP.slugs() if s not in DEV_TYPE_MAP.HVAC_SLUGS
    )
    SCH_TRAITS_HEAT = SCH_TRAITS_BASE.extend(
        {
            vol.Optional("_domain", default="heat"): "heat",
            vol.Optional(SZ_CLASS): vol.Any(
                None, *heat_slugs, *(str(DEV_TYPE_MAP[s]) for s in heat_slugs)
            ),
        }
    )
    SCH_TRAITS_HEAT = SCH_TRAITS_HEAT.extend(
        heat_traits,
        extra=vol.PREVENT_EXTRA,
    )

    hvac_slugs = list(str(s) for s in DEV_TYPE_MAP.HVAC_SLUGS)
    SCH_TRAITS_HVAC = SCH_TRAITS_BASE.extend(
        {
            vol.Optional("_domain", default="hvac"): "hvac",
            vol.Optional(SZ_CLASS, default="HVC"): vol.Any(
                None, *hvac_slugs, *(str(DEV_TYPE_MAP[s]) for s in hvac_slugs)
            ),
            vol.Optional(SZ_BOUND_TO): vol.Any(
                None,
                vol.Match(DEVICE_ID_REGEX.ANY),
                [vol.Match(DEVICE_ID_REGEX.ANY)],
            ),
        }
    )
    SCH_TRAITS_HVAC = SCH_TRAITS_HVAC.extend(
        {vol.Optional(SZ_SCHEME): vol.Any(*_SCH_TRAITS_HVAC_SCHEMES)}
    )
    SCH_TRAITS_HVAC = SCH_TRAITS_HVAC.extend(
        hvac_traits,
        extra=vol.PREVENT_EXTRA,
    )

    SCH_TRAITS = vol.Any(
        vol.All(None, ConvertNullToDict()),
        vol.Any(SCH_TRAITS_HEAT, SCH_TRAITS_HVAC),
        extra=vol.PREVENT_EXTRA,
    )
    SCH_DEVICE = vol.Schema(
        {vol.Optional(SCH_DEVICE_ID_ANY): SCH_TRAITS},
        extra=vol.PREVENT_EXTRA,
    )

    global_traits_dict = {
        vol.Optional(SZ_KNOWN_LIST, default={}): vol.Any(
            vol.All(None, ConvertNullToDict()),
            vol.All(SCH_DEVICE, vol.Length(min=0)),
        ),
        vol.Optional(SZ_BLOCK_LIST, default={}): vol.Any(
            vol.All(None, ConvertNullToDict()),
            vol.All(SCH_DEVICE, vol.Length(min=0)),
        ),
    }

    return global_traits_dict, SCH_TRAITS


SCH_GLOBAL_TRAITS_DICT, SCH_TRAITS = sch_global_traits_dict_factory()


# Trait key mapping: ramses_cc _-prefixed → ramses_rf native trait.
# Used by strip_and_map_traits() stage 2.
# Keys not in this map are simply stripped (stage 1).
_TRAIT_KEY_MAP: Final[dict[str, str]] = {
    "_bound": SZ_BOUND_TO,
    "_scheme": SZ_SCHEME,
    "_alias": SZ_ALIAS,
    "_faked": SZ_FAKED,
    "_class": SZ_CLASS,
    "_polling_interval": SZ_POLLING_INTERVAL,
    "_is_battery": SZ_IS_BATTERY,
}


def strip_and_map_traits(traits: dict[str, Any]) -> dict[str, Any]:
    """Strip ramses_cc-only ``_``-prefixed keys and map known ones to native traits.

    This is the ramses_rf side of the schema transformation pipeline:
    - **Stage 1 (strip):** remove ``_``-prefixed keys that ramses_rf
      doesn't need (``_commands``, ``_disabled``, ``_name``, ``_note``,
      ``_owner``, ``_comment``, ``_skipped``, …).
    - **Stage 2 (map):** rename ``_``-prefixed keys that ramses_rf
      *does* need to their native names (``_bound``→``bound``,
      ``_scheme``→``scheme``, ``_alias``→``alias``, ``_faked``→``faked``,
      ``_class``→``class``).

    Both the HA integration (ramses_cc) and the CLI (ramses_cli) call
    this function so that ``config.json`` schemas with ``_``-prefixed
    keys work everywhere — no duplicate stripper needed.

    Recurses into nested dicts (e.g. zones within a TCS) so that
    ``_name`` inside a zone is also stripped/mapped.

    Keys without a ``_`` prefix are passed through unchanged.

    :param traits: A device's trait dict, possibly containing
        ``_``-prefixed ramses_cc extension keys.
    :return: A new dict with ``_`` keys stripped or mapped to native names.
    """
    result: dict[str, Any] = {}
    for key, value in traits.items():
        if not isinstance(key, str) or not key.startswith("_"):
            # Non-_ key: recurse if dict, pass through otherwise
            if isinstance(value, dict):
                result[key] = strip_and_map_traits(value)
            else:
                result[key] = value
            continue
        # _-prefixed key: map or strip
        native_key = _TRAIT_KEY_MAP.get(key)
        if native_key is not None and native_key not in result:
            # Recurse into mapped value if it's a dict
            if isinstance(value, dict):
                result[native_key] = strip_and_map_traits(value)
            else:
                result[native_key] = value
        # else: strip (drop the key)
    return result


def strip_traits(traits: dict[str, Any]) -> dict[str, Any]:
    """Recursively strip all ``_``-prefixed keys from a dict.

    Stage 1 only (no mapping).  Used by ramses_cc's
    ``_strip_schema_extensions`` to remove all ``_`` keys before passing
    the schema to ``SCH_GLOBAL_SCHEMAS`` (which does not accept mapped
    trait names like ``bound`` or ``class`` — those go in the
    ``known_list``, not the schema).

    Recurses into nested dicts (e.g. zones within a TCS).

    :param traits: A dict possibly containing ``_``-prefixed keys.
    :return: A new dict with all ``_`` keys removed.
    """
    result: dict[str, Any] = {}
    for key, value in traits.items():
        if isinstance(key, str) and key.startswith("_"):
            continue
        if isinstance(value, dict):
            result[key] = strip_traits(value)
        else:
            result[key] = value
    return result


def strip_and_map_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Apply :func:`strip_and_map_traits` to every device entry in a schema.

    Walks a schema dict and applies the strip+map pipeline to each
    device's trait dict.  Top-level keys (``main_tcs``, ``orphans_heat``,
    ``controller``, etc.) are passed through unchanged.

    :param schema: A schema dict with device IDs as keys and trait
        dicts as values.
    :return: A new schema dict with ``_`` keys stripped/mapped per device.
    """
    result: dict[str, Any] = {}
    for key, value in schema.items():
        if isinstance(value, dict) and isinstance(key, str):
            # Could be a device entry (key is a device ID) or a
            # structural key (main_tcs, orphans_heat, etc.).
            # Device entries have _-prefixed keys in their value dict;
            # structural keys don't.  Apply strip+map either way —
            # non-_ keys pass through unchanged.
            result[key] = strip_and_map_traits(value)
        else:
            result[key] = value
    return result


@dataclass
class GatewayConfig:
    """Configuration parameters for the Ramses Gateway.

    :param disable_polling: Disable device polling, defaults to False.
    :type disable_polling: bool
    :param enable_eavesdrop: Enable eavesdropping mode, defaults to False.
    :type enable_eavesdrop: bool
    :param reduce_processing: Level of reduced processing, defaults to 0.
    :type reduce_processing: int
    :param max_zones: Maximum number of zones allowed, defaults to 12.
    :type max_zones: int
    :param use_aliases: Mapping of aliases for device IDs.
    :type use_aliases: dict[str, str]
    :param enforce_strict_handling: Enforce strict handling of packets.
    :type enforce_strict_handling: bool
    :param use_native_ot: Preference for using native OpenTherm.
    :type use_native_ot: Literal["always", "prefer", "avoid", "never"] | None
    :param schema: Dictionary representing the schema.
    :type schema: dict[str, Any]
    :param debug_mode: If True, set the logger to debug mode.
    :type debug_mode: bool
    :param gateway_timeout: Custom timeout threshold in minutes.
    :type gateway_timeout: int | None
    :param database_path: Target disk path for the SQLite DB.
    :type database_path: str | None
    :param known_list: A list of known device IDs and their traits.
    :type known_list: dict[str, Any]
    :param block_list: A list of blocked device IDs.
    :type block_list: dict[str, Any]
    :param engine: Typed configuration object for the Transport layer.
    :type engine: EngineConfig
    :param hgi_id: The explicit Device ID of the active HGI hardware.
    :type hgi_id: str | None
    """

    disable_polling: bool = False
    disable_discovery: bool | None = None
    enable_eavesdrop: bool = False
    reduce_processing: int = 0
    max_zones: int = 12
    use_aliases: dict[str, str] = field(default_factory=dict)
    enforce_strict_handling: bool = False
    use_native_ot: Literal["always", "prefer", "avoid", "never"] | None = None

    schema: dict[str, Any] = field(default_factory=dict)
    debug_mode: bool = False
    gateway_timeout: int | None = None
    database_path: str | None = "ramses.db"

    known_list: dict[str, Any] = field(default_factory=dict)
    block_list: dict[str, Any] = field(default_factory=dict)

    # Transport layer configuration encapsulated perfectly
    engine: EngineConfig = field(default_factory=EngineConfig)

    hgi_id: str | None = None

    def __setattr__(self, name: str, value: Any) -> None:
        """Keep disable_polling and disable_discovery synchronized natively."""
        super().__setattr__(name, value)
        if value is not None:
            if name == "disable_discovery":
                super().__setattr__("disable_polling", value)
            elif name == "disable_polling":
                super().__setattr__("disable_discovery", value)

    def __post_init__(self) -> None:
        """Initialize computed properties natively on startup."""
        if self.disable_discovery is None:
            self.disable_discovery = self.disable_polling
        elif self.disable_discovery:
            self.disable_polling = True
        elif self.disable_polling:
            self.disable_discovery = True
        if not self.hgi_id:
            explicit_hgis = [
                k
                for k, v in self.known_list.items()
                if v.get(SZ_CLASS) in (DevType.HGI, DEV_TYPE_MAP[DevType.HGI])
            ]
            implicit_hgis = [
                k
                for k, v in self.known_list.items()
                if not v.get(SZ_CLASS) and k[:2] == DEV_TYPE_MAP._hex(DevType.HGI)
            ]

            if explicit_hgis:
                self.hgi_id = explicit_hgis[0]
            elif implicit_hgis:
                self.hgi_id = implicit_hgis[0]

    @property
    def mac_filter_list(self) -> list[str]:
        """Return a flattened list of MAC addresses from the known_list.

        :returns: A list of MAC address strings from known_list.
        :rtype: list[str]
        """
        return list(self.known_list.keys())
