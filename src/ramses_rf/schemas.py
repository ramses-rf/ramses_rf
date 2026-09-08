#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

:term:`Schema` processor for upper layer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

import probatio as vol

from ramses_rf.const import (
    SZ_ACTUATORS as SZ_ACTUATORS,
    SZ_CONFIG as SZ_CONFIG,
    SZ_DEVICES as SZ_DEVICES,
    SZ_NAME,
    SZ_SENSOR as SZ_SENSOR,
    SZ_ZONE_INDEX,
    SZ_ZONE_TYPE,
    SZ_ZONES,
)
from ramses_rf.typing import DeviceIdT, DeviceListT

# TODO: deprecate re-exporting (via as) in favour of direct imports
# TODO: deprecate re-exporting (via as) in favour of direct imports
from ramses_tx.const import (
    DEFAULT_MAX_ZONES as DEFAULT_MAX_ZONES,
    DEVICE_ID_REGEX as DEVICE_ID_REGEX,
)
from ramses_tx.schemas import (  # noqa: F401
    SCH_ENGINE_DICT,
    SZ_BLOCK_LIST,
    SZ_DISABLE_SENDING,
    SZ_ENFORCE_KNOWN_LIST,
    SZ_KNOWN_LIST as SZ_KNOWN_LIST,
    SZ_PACKET_LOG,
    SZ_SCHEMA as SZ_SCHEMA,
    sch_packet_log_dict_factory,
    select_device_filter_mode,
)

from . import exceptions as exc

# Import elevated L7 domain concepts from ramses_rf.config instead of ramses_tx.schemas
from .config import (
    SCH_DEVICE_ID_ANY,
    SCH_DEVICE_ID_APP,
    SCH_DEVICE_ID_BDR,
    SCH_DEVICE_ID_CTL,
    SCH_DEVICE_ID_DHW,
    SCH_DEVICE_ID_SEN,
    SCH_DEVICE_ID_UFC,
    SCH_GLOBAL_TRAITS_DICT,
    SCH_TRAITS as SCH_TRAITS,
    SZ_ALIAS as SZ_ALIAS,
    SZ_BOUND_TO as SZ_BOUND_TO,
    SZ_CLASS as SZ_CLASS,
    SZ_FAKED as SZ_FAKED,
    SZ_SCHEME as SZ_SCHEME,
    strip_and_map_schema as strip_and_map_schema,
    strip_and_map_traits as strip_and_map_traits,
    strip_traits as strip_traits,
)
from .const import (
    DEV_ROLE_MAP,
    DEV_TYPE_MAP,
    DONT_CREATE_MESSAGES,
    SZ_DISABLE_DISCOVERY as SZ_DISABLE_DISCOVERY,
    SZ_DISABLE_POLLING as SZ_DISABLE_POLLING,
    SZ_IS_BATTERY as SZ_IS_BATTERY,
    SZ_POLLING_INTERVAL as SZ_POLLING_INTERVAL,
    ZON_ROLE_MAP,
    DevRole,
    DevType,
    SystemType,
)

if TYPE_CHECKING:
    from .devices import Device
    from .gateway import Gateway
    from .systems.tcs import SystemBase


_LOGGER = logging.getLogger(__name__)


#
# 0/5: Schema strings
SZ_MAIN_TCS: Final = "main_tcs"

SZ_CONTROLLER = DEV_TYPE_MAP[DevType.CTL]
SZ_SYSTEM: Final = "system"
SZ_APPLIANCE_CONTROL = DEV_ROLE_MAP[DevRole.APP]
SZ_ORPHANS: Final = "orphans"
SZ_ORPHANS_HEAT: Final = "orphans_heat"
SZ_ORPHANS_HVAC: Final = "orphans_hvac"

SZ_DHW_SYSTEM: Final = "stored_hotwater"
SZ_DHW_SENSOR = DEV_ROLE_MAP[DevRole.DHW]
SZ_DHW_VALVE = DEV_ROLE_MAP[DevRole.HTG]
SZ_HTG_VALVE = DEV_ROLE_MAP[DevRole.HT1]

SZ_SENSOR_FAKED: Final = "sensor_faked"

SZ_UFH_SYSTEM: Final = "underfloor_heating"
SZ_UFH_CTL = DEV_TYPE_MAP[DevType.UFC]  # ufh_controller
SZ_CIRCUITS: Final = "circuits"

HEAT_ZONES_STRS = tuple(ZON_ROLE_MAP[t] for t in ZON_ROLE_MAP.HEAT_ZONES)

SCH_DOM_ID = vol.Match(r"^[0-9A-F]{2}$")
SCH_UFH_INDEX = vol.Match(r"^0[0-8]$")
SCH_ZON_INDEX = vol.Match(
    r"^0[0-9AB]$"
)  # TODO: what if > 12 zones? (e.g. hometronics)


def error_renamed_key(new_key: str) -> Callable[[Any], None]:
    """Return a probatio validator function raising an invalid key error.

    :param new_key: The new key name to instruct the user to rename to.
    :type new_key: str
    :returns: A probatio validator function.
    :rtype: Callable[[Any], None]
    """

    def renamed_key(node_value: Any) -> None:
        raise vol.Invalid(
            f"the key name has changed: rename it to '{new_key}'"
        )

    return renamed_key


#
# 1/7: Schemas for CH/DHW systems, aka Heat/TCS (temp control systems)
SCH_TCS_SYS_CLASS = (
    SystemType.EVOHOME,
    SystemType.HOMETRONICS,
    SystemType.SUNDIAL,
)
SCH_TCS_SYS = vol.Schema(
    {
        vol.Required(SZ_APPLIANCE_CONTROL, default=None): vol.Any(
            None, SCH_DEVICE_ID_APP
        ),
        vol.Optional("heating_control"): error_renamed_key(
            SZ_APPLIANCE_CONTROL
        ),
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_TCS_DHW = vol.Schema(
    {
        vol.Optional(SZ_SENSOR, default=None): vol.Any(
            None, SCH_DEVICE_ID_DHW
        ),
        vol.Optional(SZ_DHW_VALVE, default=None): vol.Any(
            None, SCH_DEVICE_ID_BDR
        ),
        vol.Optional(SZ_HTG_VALVE, default=None): vol.Any(
            None, SCH_DEVICE_ID_BDR
        ),
        vol.Optional(SZ_DHW_SENSOR): error_renamed_key(SZ_SENSOR),
    },
    extra=vol.PREVENT_EXTRA,
)

_CH_TCS_UFH_CIRCUIT = vol.Schema(
    {
        vol.Required(SCH_UFH_INDEX): vol.Schema(
            {
                vol.Optional(SZ_ZONE_INDEX): SCH_ZON_INDEX,
            },
        ),
    },
    extra=vol.PREVENT_EXTRA,
)
SCH_TCS_UFH = vol.All(
    vol.Schema(
        {
            vol.Required(SCH_DEVICE_ID_UFC): vol.Any(
                None, {vol.Optional(SZ_CIRCUITS): vol.Any(None, dict)}
            )
        }
    ),
    vol.Length(min=1, max=3),
    extra=vol.PREVENT_EXTRA,
)

SCH_TCS_ZONES_ZON = vol.Schema(
    {
        vol.Optional(SZ_CLASS, default=None): vol.Any(None, *HEAT_ZONES_STRS),
        vol.Optional(SZ_SENSOR, default=None): vol.Any(
            None, SCH_DEVICE_ID_SEN
        ),
        vol.Optional(SZ_DEVICES): error_renamed_key(SZ_ACTUATORS),
        vol.Optional(SZ_ACTUATORS, default=[]): vol.All(
            [SCH_DEVICE_ID_ANY], vol.Length(min=0)
        ),
        vol.Optional(SZ_ZONE_TYPE): error_renamed_key(SZ_CLASS),
        vol.Optional("zone_sensor"): error_renamed_key(SZ_SENSOR),
        # vol.Optional(SZ_SENSOR_FAKED): bool,
        vol.Optional(f"_{SZ_NAME}"): vol.Any(None, str),
    },
    extra=vol.PREVENT_EXTRA,
)
SCH_TCS_ZONES = vol.All(
    vol.Schema({vol.Required(SCH_ZON_INDEX): SCH_TCS_ZONES_ZON}),
    vol.Length(min=1, max=12),
    extra=vol.PREVENT_EXTRA,
)

SCH_TCS = vol.Schema(
    {
        vol.Optional(SZ_SYSTEM, default={}): vol.Any({}, SCH_TCS_SYS),
        vol.Optional(SZ_DHW_SYSTEM, default={}): vol.Any({}, SCH_TCS_DHW),
        vol.Optional(SZ_UFH_SYSTEM, default={}): vol.Any({}, SCH_TCS_UFH),
        vol.Optional(SZ_ORPHANS, default=[]): vol.All(
            [SCH_DEVICE_ID_ANY], vol.Unique()
        ),
        vol.Optional(SZ_ZONES, default={}): vol.Any({}, SCH_TCS_ZONES),
        vol.Remove("is_tcs"): vol.Coerce(bool),
    },
    extra=vol.PREVENT_EXTRA,
)


#
# 2/7: Schemas for Ventilation control systems, aka HVAC/VCS
SZ_REMOTES: Final = "remotes"
SZ_SENSORS: Final = "sensors"

SCH_VCS_DATA = vol.Schema(
    {
        vol.Optional(SZ_REMOTES, default=[]): vol.All(
            [SCH_DEVICE_ID_ANY],
            vol.Unique(),  # vol.Length(min=1)
        ),
        vol.Optional(SZ_SENSORS, default=[]): vol.All(
            [SCH_DEVICE_ID_ANY],
            vol.Unique(),  # vol.Length(min=1)
        ),
        vol.Remove("is_vcs"): vol.Coerce(bool),
    },
    extra=vol.PREVENT_EXTRA,
)
SCH_VCS_KEYS = vol.Schema(
    {
        vol.Required(
            vol.Any(SZ_REMOTES, SZ_SENSORS),
            msg=(
                "The ventilation control system schema must include at least "
                f"one of [{SZ_REMOTES}, {SZ_SENSORS}]"
            ),
        ): object
    },
    extra=vol.ALLOW_EXTRA,  # must be ALLOW_EXTRA, as is a subset of SCH_VCS_DATA
)
SCH_VCS = vol.All(SCH_VCS_KEYS, SCH_VCS_DATA)


#
# 3/7: Global Schema for Heat/HVAC systems
SCH_GLOBAL_SCHEMAS_DICT = {  # System schemas - can be 0-many Heat/HVAC schemas
    # orphans are devices to create that won't be in a (cached) schema...
    vol.Optional(SZ_MAIN_TCS): vol.Any(None, SCH_DEVICE_ID_CTL),
    vol.Remove("main_controller"): vol.Any(None, SCH_DEVICE_ID_CTL),
    vol.Optional(SCH_DEVICE_ID_CTL): vol.Any(SCH_TCS, SCH_VCS),
    vol.Optional(
        SCH_DEVICE_ID_ANY
    ): SCH_VCS,  # must be after SCH_DEVICE_ID_CTL
    vol.Optional(SZ_ORPHANS_HEAT): vol.All([SCH_DEVICE_ID_ANY], vol.Unique()),
    vol.Optional(SZ_ORPHANS_HVAC): vol.All([SCH_DEVICE_ID_ANY], vol.Unique()),
    vol.Optional("transport_constructor"): vol.Any(callable, None),
}
SCH_GLOBAL_SCHEMAS = vol.Schema(
    SCH_GLOBAL_SCHEMAS_DICT, extra=vol.PREVENT_EXTRA
)

#
# 4/7: Gateway (parser/state) configuration
SZ_ENABLE_EAVESDROP: Final = "enable_eavesdrop"
SZ_ENFORCE_STRICT_HANDLING: Final = "enforce_strict_handling"
SZ_MAX_ZONES: Final = "max_zones"  # TODO: move to TCS-attr from GWY-layer
SZ_REDUCE_PROCESSING: Final = "reduce_processing"
SZ_USE_ALIASES: Final = (
    "use_aliases"  # use friendly device names from known_list
)
SZ_USE_NATIVE_OT: Final = "use_native_ot"  # favour OT (3220s) over RAMSES

SCH_GATEWAY_DICT = {
    vol.Optional(SZ_DISABLE_POLLING, default=False): bool,
    vol.Optional(SZ_DISABLE_DISCOVERY): bool,
    vol.Optional(SZ_ENABLE_EAVESDROP, default=False): bool,
    vol.Optional(SZ_ENFORCE_STRICT_HANDLING, default=False): bool,
    vol.Optional(SZ_MAX_ZONES, default=DEFAULT_MAX_ZONES): vol.All(
        int, vol.Range(min=1, max=16)
    ),  # NOTE: no default
    vol.Optional(SZ_REDUCE_PROCESSING, default=0): vol.All(
        int, vol.Range(min=0, max=DONT_CREATE_MESSAGES)
    ),
    vol.Optional(SZ_USE_ALIASES, default=False): bool,
    vol.Optional(SZ_USE_NATIVE_OT, default="prefer"): vol.Any(
        "always", "prefer", "avoid", "never"
    ),
}
SCH_GATEWAY_CONFIG = vol.Schema(SCH_GATEWAY_DICT, extra=vol.REMOVE_EXTRA)


#
# 5/7: the Global (gateway) Schema
SCH_GLOBAL_CONFIG = (
    vol.Schema(
        {
            # Gateway/engine Configuration, incl. packet_log, serial_port params...
            vol.Optional(SZ_CONFIG, default={}): SCH_GATEWAY_DICT
            | SCH_ENGINE_DICT
        },
        extra=vol.PREVENT_EXTRA,
    )
    .extend(SCH_GLOBAL_SCHEMAS_DICT)
    .extend(SCH_GLOBAL_TRAITS_DICT)
    .extend(sch_packet_log_dict_factory(default_backups=0))
)


#
# 6/7: External Schemas, to be used by clients of this library
def normalise_restore_cache() -> Callable[
    [bool | dict[str, bool]], dict[str, bool]
]:
    """Convert a shorthand restore_cache bool to a dict.

    restore_cache: bool ->  restore_cache:
                              restore_schema: bool
                              restore_state: bool

    :returns: A callable validator function converting boolean to dict.
    :rtype: Callable[[bool | dict[str, bool]], dict[str, bool]]
    """

    def _normalise(
        node_value: bool | dict[str, bool],
    ) -> dict[str, bool]:
        if isinstance(node_value, dict):
            return node_value
        return {SZ_RESTORE_SCHEMA: node_value, SZ_RESTORE_STATE: node_value}

    return _normalise


SZ_RESTORE_CACHE: Final = "restore_cache"
SZ_RESTORE_SCHEMA: Final = "restore_schema"
SZ_RESTORE_STATE: Final = "restore_state"

SCH_RESTORE_CACHE_DICT = {
    vol.Optional(SZ_RESTORE_CACHE, default=True): vol.Any(
        vol.All(bool, normalise_restore_cache()),
        vol.Schema(
            {
                vol.Optional(SZ_RESTORE_SCHEMA, default=True): bool,
                vol.Optional(SZ_RESTORE_STATE, default=True): bool,
            }
        ),
    )
}


#
# 7/7: Other stuff
def _get_device(
    gateway: Gateway, device_id: DeviceIdT, **kwargs: Any
) -> Device:  # , **traits
    """Get a device from the gateway.

    Raise a DeviceNotFoundError if a device_id is filtered out by the known or block list.

    The underlying method is wrapped only to provide a better error message.
    """

    def check_filter_lists(device_id: DeviceIdT) -> None:
        """Raise a DeviceNotFoundError if a device_id is filtered out by a list."""
        err_msg = None
        if (
            gateway._engine._enforce_known_list
            and device_id not in gateway._engine._include
        ):
            err_msg = (
                f"it is in the {SZ_SCHEMA}, but not in the {SZ_KNOWN_LIST}"
            )
        # issue ramses_cc #296: if enforce_known_list is turned on, error on any "unknown" dev_id
        # fix: delete from schema?
        if device_id in gateway._engine._exclude:
            err_msg = (
                f"it is in the {SZ_SCHEMA}, but also in the {SZ_BLOCK_LIST}"
            )

        if err_msg:
            raise exc.DeviceNotFoundError(
                f"Can't create {device_id}: {err_msg} (check configuration.yaml)"
            )

    check_filter_lists(device_id)

    device: Device = gateway.device_registry.get_device(device_id, **kwargs)
    return device


def load_schema(
    gateway: Any,
    known_list: DeviceListT | dict[str, Any] | None = None,
    **schema: Any,
) -> None:
    """Instantiate all entities in the schema, and faked devices in the known_list.

    :param gateway: The Gateway instance to attach devices and systems to.
    :type gateway: Gateway
    :param known_list: Optional dictionary of known device IDs and traits.
    :type known_list: dict[DeviceIdT, Any] | None
    :param schema: Keyword arguments representing the global schema.
    :type schema: Any
    :rtype: None
    """
    from .devices import Fakeable  # circular import

    known_list = known_list or {}

    # schema: dict = SCH_GLOBAL_SCHEMAS_DICT(schema)

    [
        load_tcs(gateway, ctl_id, schema)  # type: ignore[arg-type]
        for ctl_id, schema in schema.items()
        if re.match(DEVICE_ID_REGEX.ANY, ctl_id) and SZ_REMOTES not in schema
    ]
    if schema.get(SZ_MAIN_TCS):
        sys_by_id = gateway.device_registry.system_by_id
        gateway._tcs = sys_by_id.get(schema[SZ_MAIN_TCS])
    [
        load_fan(gateway, fan_id, schema)  # type: ignore[arg-type]
        for fan_id, schema in schema.items()
        if re.match(DEVICE_ID_REGEX.ANY, fan_id) and SZ_REMOTES in schema
    ]
    [  # NOTE: class favoured, domain ignored
        _get_device(gateway, device_id)  # domain=key[-4:])
        for key in (SZ_ORPHANS_HEAT, SZ_ORPHANS_HVAC)
        for device_id in schema.get(key, [])
    ]  # TODO: pass domain (Heat/HVAC), or generalise to SZ_ORPHANS

    # create any devices in the known list that are faked, or fake those already created
    for device_id, traits in known_list.items():
        if traits.get(SZ_FAKED):
            device = _get_device(gateway, DeviceIdT(device_id))  # , **traits)
            if not isinstance(device, Fakeable):
                raise exc.DeviceNotFaked(f"Device is not fakeable: {device}")
            if not device.is_faked:
                device._make_fake()


def load_fan(
    gateway: Gateway, fan_id: DeviceIdT, schema: dict[str, Any]
) -> Device:
    """Create a FAN using its schema (i.e. with remotes, sensors).

    :param gateway: The Gateway instance managing the device.
    :type gateway: Gateway
    :param fan_id: The device ID of the FAN entity.
    :type fan_id: DeviceIdT
    :param schema: The schema dictionary for the FAN entity.
    :type schema: dict[str, Any]
    :returns: The created or retrieved FAN device instance.
    :rtype: Device
    """
    fan = _get_device(gateway, fan_id)
    if hasattr(fan, "_update_schema"):
        fan._update_schema(**schema)

    return fan


def _load_ufh_system(
    gateway: Gateway, tcs: SystemBase, ufh_system: Any
) -> None:
    """Hydrate UFH controllers and circuits from system schema.

    :param gateway: The Gateway instance managing the system.
    :param tcs: The Temperature Control System instance.
    :param ufh_system: The underfloor heating schema dictionary.
    """
    if not isinstance(ufh_system, dict):
        return

    zones_map: dict[str, Any] = getattr(tcs, "zone_by_index", {})
    device_registry = getattr(gateway, "device_registry", None)
    cqrs_actuators: dict[str, set[str]] = getattr(
        device_registry, "_cqrs_actuators", {}
    )

    for device_id, ufc_schema in ufh_system.items():
        if not isinstance(ufc_schema, dict):
            continue

        ufc = _get_device(gateway, device_id, parent=tcs)
        if not hasattr(ufc, "get_circuit"):
            continue

        circuits_dict = ufc_schema.get(SZ_CIRCUITS)
        if not isinstance(circuits_dict, dict):
            continue

        for ufh_index, circuit_schema in circuits_dict.items():
            if not isinstance(circuit_schema, dict):
                continue

            zone_index = circuit_schema.get(SZ_ZONE_INDEX)
            if zone_index is None:
                continue

            circuit = ufc.get_circuit(str(ufh_index))
            zone_index_str = str(zone_index)
            if zone := zones_map.get(zone_index_str):
                circuit.set_zone(zone)

            zone_key = f"{tcs.id}_{zone_index_str}"
            cqrs_actuators.setdefault(zone_key, set()).add(circuit.id)


def load_tcs(
    gateway: Gateway, controller_id: DeviceIdT, schema: dict[str, Any]
) -> SystemBase:
    """Create a TCS using its schema.

    :param gateway: The Gateway instance managing the TCS.
    :type gateway: Gateway
    :param controller_id: The controller device ID for the TCS.
    :type controller_id: DeviceIdT
    :param schema: The schema dictionary for the TCS.
    :type schema: dict[str, Any]
    :returns: The created or retrieved TCS instance.
    :rtype: SystemBase
    """
    # print(schema)
    # schema = SCH_TCS_ZONES_ZON(schema)

    controller = _get_device(gateway, controller_id)
    if controller.tcs is None:
        raise exc.SchemaInconsistentError(
            f"No TCS assigned to controller {controller.id}"
        )
    if hasattr(controller.tcs, "_update_schema"):
        controller.tcs._update_schema(**schema)

    _load_ufh_system(gateway, controller.tcs, schema.get(SZ_UFH_SYSTEM))

    for dev_id in schema.get(SZ_ORPHANS, []):
        _get_device(gateway, dev_id, parent=controller)

    # if DEV_MODE:
    #     import json
    #
    #     src = json.dumps(shrink(schema), sort_keys=True)
    #     dst = json.dumps(shrink(gateway.device_registry.system_by_id[ctl.id].schema), sort_keys=True)
    #     # assert dst == src, "They don't match!"
    #     print(src)
    #     print(dst)

    return controller.tcs
