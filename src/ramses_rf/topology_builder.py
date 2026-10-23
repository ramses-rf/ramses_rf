#!/usr/bin/env python3
"""RAMSES RF - Topology discovery and schema entity initialization."""

from __future__ import annotations

import contextlib
import dataclasses
import logging
from typing import TYPE_CHECKING, cast

from ramses_rf.config import SZ_CLASS
from ramses_rf.typing import SchemaT
from ramses_tx.const import Code
from ramses_tx.typing import DeviceIdT

from . import exceptions as exc
from .const import (
    DEV_TYPE_MAP,
    SZ_CIRCUIT_INDEX,
    SZ_DEVICES,
    SZ_DOMAIN_INDEX,
    SZ_UFH_INDEX,
    SZ_ZONE_INDEX,
    SZ_ZONE_MASK,
    SZ_ZONE_TYPE,
    ZON_ROLE_MAP,
    DevType,
)
from .eavesdropper import EavesdropEngine
from .messages import Message

if TYPE_CHECKING:
    from .devices.dev_base import Device
    from .gateway import Gateway

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "TopologyBuilder",
    "update_topology_schema_state",
]


class TopologyBuilder:
    """Builder for discovering and building system topology read-models."""

    def __init__(self, gateway: Gateway) -> None:
        """Initialize the topology builder.

        :param gateway: The gateway handling topology state.
        :type gateway: Gateway
        """
        self._gateway = gateway

    async def update_topology(self, payload: SchemaT, msg: Message) -> None:
        """Process a decoded payload and update topology models.

        :param payload: The payload dictionary.
        :type payload: SchemaT
        :param msg: The raw message object.
        :type msg: Message
        """
        await update_topology_schema_state(self._gateway, payload, msg)


async def update_topology_schema_state(
    gateway: Gateway, p: SchemaT, msg: Message
) -> None:
    """Discover and instantiate schema entities (TCS, zones, DHW, UFH) from packets.

    :param gateway: The gateway handling entity instantiation.
    :type gateway: Gateway
    :param p: Decoded payload dictionary.
    :type p: SchemaT
    :param msg: Message object containing headers and routing addrs.
    :type msg: Message
    """
    registry = getattr(gateway, "device_registry", None)
    tcs = None
    if registry:
        if getattr(msg.src, "type", None) in ("01", DevType.CTL):
            ctl_id = str(msg.src.id)
            if ctl_dev := registry.device_by_id.get(ctl_id):
                tcs = getattr(ctl_dev, "tcs", None)
        elif getattr(msg.dst, "type", None) in ("01", DevType.CTL):
            ctl_id = str(msg.dst.id)
            if ctl_dev := registry.device_by_id.get(ctl_id):
                tcs = getattr(ctl_dev, "tcs", None)
        else:
            if (devices := p.get(SZ_DEVICES, p.get("devices"))) and isinstance(
                devices, list
            ):
                for candidate_device in devices:
                    candidate_type = getattr(
                        candidate_device, "type", str(candidate_device)[:2]
                    )
                    if candidate_type in ("01", DevType.CTL):
                        ctl_id = str(candidate_device)
                        if ctl_dev := (
                            registry.device_by_id.get(ctl_id)
                            or registry.device_by_id.get(
                                cast(DeviceIdT, ctl_id)
                            )
                        ):
                            tcs = getattr(ctl_dev, "tcs", None)
                        break

            if tcs is None and hasattr(msg.src, "id"):
                if src_dev := (
                    registry.device_by_id.get(str(msg.src.id))
                    or registry.device_by_id.get(
                        cast(DeviceIdT, str(msg.src.id))
                    )
                ):
                    tcs = getattr(src_dev, "tcs", None)
            if tcs is None and hasattr(msg.dst, "id"):
                if dst_dev := (
                    registry.device_by_id.get(str(msg.dst.id))
                    or registry.device_by_id.get(
                        cast(DeviceIdT, str(msg.dst.id))
                    )
                ):
                    tcs = getattr(dst_dev, "tcs", None)

    if tcs is None and registry:
        controllers = [
            d
            for d in list(registry.device_by_id.values())
            if (
                getattr(d, "_SLUG", "") == "CTL"
                or getattr(d, "type", None) in ("01", DevType.CTL)
            )
            and hasattr(d, "tcs")
            and d.tcs is not None
        ]
        if len(controllers) == 1:
            tcs = controllers[0].tcs

    match msg.code:
        # 1. Code 0005: System Zone Structure Discovery & Initialization
        case Code._0005:
            if (
                getattr(msg.src, "type", None) in ("01", DevType.CTL)
                and tcs
                and hasattr(tcs, "get_htg_zone")
            ):
                z_type = p.get("zone_type")
                if (
                    isinstance(z_type, str)
                    and z_type in ZON_ROLE_MAP.HEAT_ZONES
                ):
                    schema: SchemaT = {"class": ZON_ROLE_MAP[z_type]}
                    if zone_mask := p.get("zone_mask"):
                        for bit_index, active in enumerate(zone_mask):
                            if active:
                                with contextlib.suppress(
                                    exc.DeviceNotFoundError,
                                    exc.SchemaInconsistentError,
                                ):
                                    z_str = f"{bit_index:02X}"
                                    ez = getattr(tcs, "zone_by_index", {}).get(
                                        z_str
                                    )
                                    if (
                                        ez is not None
                                        and getattr(ez, "_heating_type", None)
                                        is not None
                                    ):
                                        tcs.get_htg_zone(z_str)
                                    else:
                                        tcs.get_htg_zone(z_str, **schema)
                    elif (
                        zone_index := (
                            p.get(SZ_ZONE_INDEX)
                            or p.get("zone_index")
                            or p.get("child_id")
                        )
                    ) is not None:
                        with contextlib.suppress(
                            exc.DeviceNotFoundError,
                            exc.SchemaInconsistentError,
                        ):
                            z_str = str(zone_index)
                            ez = getattr(tcs, "zone_by_index", {}).get(z_str)
                            if (
                                ez is not None
                                and getattr(ez, "_heating_type", None)
                                is not None
                            ):
                                tcs.get_htg_zone(z_str)
                            else:
                                tcs.get_htg_zone(z_str, **schema)

            elif tcs:
                for bit_index, flag in enumerate(p.get(SZ_ZONE_MASK, [])):
                    if flag == 1:
                        z_id = f"{bit_index:02X}"
                        if z_id not in tcs.zone_by_index:
                            tcs.get_htg_zone(z_id)

        # 2. Code 000C: Device Role Bindings, Zone Types & UFH Circuit Mappings
        case Code._000C:
            zone_index = p.get(SZ_ZONE_INDEX) or p.get("zone_index")
            domain_id = (
                p.get(SZ_DOMAIN_INDEX)
                or p.get("domain_id")
                or p.get("domain_index")
            )
            devices = p.get(SZ_DEVICES, p.get("devices", [])) or []
            if "device_id" in p and not devices:
                devices = [p["device_id"]]

            zone_type = p.get(SZ_ZONE_TYPE, p.get("zone_type"))
            ufh_index = (
                p.get(SZ_UFH_INDEX)
                or p.get(SZ_CIRCUIT_INDEX)
                or p.get("ufh_index")
                or p.get("cct_index")
            )

            # Instantiate any 02: UFH Controller devices and link as children of TCS
            ufc_devs: list[Device] = []
            if registry and tcs:
                for d_id in devices:
                    if getattr(d_id, "type", str(d_id)[:2]) in (
                        "02",
                        DevType.UFC,
                    ):
                        with contextlib.suppress(exc.DeviceNotFoundError):
                            ufc = registry.get_device(str(d_id), parent=tcs)
                            if ufc not in ufc_devs:
                                ufc_devs.append(ufc)

            if not ufc_devs and registry:
                src_type = getattr(msg.src, "type", None)
                dst_type = getattr(msg.dst, "type", None)
                if src_type in ("02", DevType.UFC, DEV_TYPE_MAP.UFC):
                    ufc_instance = (
                        (
                            registry.device_by_id.get(str(msg.src.id))
                            or registry.device_by_id.get(
                                cast(DeviceIdT, str(msg.src.id))
                            )
                        )
                        if hasattr(registry, "device_by_id")
                        else None
                    )
                    if not ufc_instance and hasattr(registry, "get_device"):
                        with contextlib.suppress(exc.DeviceNotFoundError):
                            ufc_instance = registry.get_device(str(msg.src.id))
                    if ufc_instance and ufc_instance not in ufc_devs:
                        ufc_devs.append(ufc_instance)
                elif dst_type in ("02", DevType.UFC, DEV_TYPE_MAP.UFC):
                    ufc_instance = (
                        (
                            registry.device_by_id.get(str(msg.dst.id))
                            or registry.device_by_id.get(
                                cast(DeviceIdT, str(msg.dst.id))
                            )
                        )
                        if hasattr(registry, "device_by_id")
                        else None
                    )
                    if not ufc_instance and hasattr(registry, "get_device"):
                        with contextlib.suppress(exc.DeviceNotFoundError):
                            ufc_instance = registry.get_device(str(msg.dst.id))
                    if ufc_instance and ufc_instance not in ufc_devs:
                        ufc_devs.append(ufc_instance)

            if not ufc_devs and tcs:
                ufc_devs = [
                    d
                    for d in getattr(tcs, "childs", [])
                    if getattr(d, "_SLUG", "") == "UFC"
                    or getattr(d, "type", None) in ("02", DevType.UFC)
                ]

            # Route UFH circuit mappings to UFH controllers
            if ufc_devs and ufh_index is not None:
                ufh_z_str: str | None = (
                    str(zone_index) if zone_index is not None else None
                )
                ufh_str = (
                    f"{int(str(ufh_index), 16):02X}"
                    if isinstance(ufh_index, (int, str))
                    and str(ufh_index).isalnum()
                    else str(ufh_index)
                )
                for ufc in ufc_devs:
                    if hasattr(ufc, "circuit_by_id"):
                        ufc.circuit_by_id[ufh_str] = {SZ_ZONE_INDEX: ufh_z_str}
                    if hasattr(ufc, "get_circuit"):
                        circuit = ufc.get_circuit(ufh_str)
                        if ufh_z_str and tcs and hasattr(circuit, "set_zone"):
                            target_zone = getattr(
                                tcs, "zone_by_index", {}
                            ).get(ufh_z_str)
                            if target_zone:
                                circuit.set_zone(target_zone)

            # Map Zone Bindings & Classes
            if (
                zone_index is not None
                and tcs
                and hasattr(tcs, "get_htg_zone")
                and getattr(msg.src, "type", None) not in ("02", DevType.UFC)
            ):
                _non_zone_prefixes = (
                    f"{DevType.CTL}:",
                    f"{DevType.UFC}:",
                    f"{DevType.HGI}:",
                    "7F:",
                )
                valid_devs = [
                    d
                    for d in devices
                    if not str(d).startswith(_non_zone_prefixes)
                    and str(d) != "7FFFFFFF"
                ]
                zone_cls: str | None = None
                if (
                    valid_devs
                    and zone_type is not None
                    and zone_type in ZON_ROLE_MAP
                ):
                    candidate_cls = ZON_ROLE_MAP[zone_type]
                    if candidate_cls in (
                        "radiator_valve",
                        "underfloor_heating",
                        "zone_valve",
                        "electric_zone",
                        "mix_valve",
                    ):
                        zone_cls = candidate_cls

                existing_zone = tcs.zone_by_index.get(str(zone_index))
                if (
                    existing_zone
                    and getattr(existing_zone, "_heating_type", None)
                    is not None
                ):
                    schema = {}
                else:
                    schema = {"class": zone_cls} if zone_cls else {}

                zone = tcs.get_htg_zone(str(zone_index), **schema)
                if zone and valid_devs:
                    has_trv = any(
                        str(d).startswith(f"{DevType.TRV}:")
                        for d in valid_devs
                    )
                    if (
                        has_trv
                        and getattr(zone, "_heating_type", None)
                        != "radiator_valve"
                    ):
                        with contextlib.suppress(exc.RamsesException):
                            zone._update_schema(**{SZ_CLASS: "radiator_valve"})

                if zone and valid_devs and registry:
                    dev_role = p.get("device_role")
                    z_type_code = p.get("zone_type")
                    is_sen = dev_role in (
                        "zone_sensor",
                        "dhw_sensor",
                    ) or z_type_code in (
                        "04",
                        "07",
                        "0D",
                    )
                    _non_actuator_prefixes = (
                        f"{DevType.CTL}:",
                        f"{DevType.UFC}:",
                        f"{DevType.HGI}:",
                        "63:",
                    )
                    for d_id in valid_devs:
                        d_str = str(d_id)
                        if d_str.startswith(_non_actuator_prefixes):
                            continue
                        with contextlib.suppress(
                            exc.DeviceNotFoundError,
                            exc.SchemaInconsistentError,
                        ):
                            if is_sen:
                                registry.device_by_id.get(d_str)
                            else:
                                registry.device_by_id.get(d_str)

            # Map Domain Bindings (Appliance Control / DHW)
            elif domain_id == "FC" and devices and tcs and registry:
                for d_id in devices:
                    with contextlib.suppress(
                        exc.DeviceNotFoundError, exc.SchemaInconsistentError
                    ):
                        registry.device_by_id.get(str(d_id))

            elif (
                domain_id in ("FA", "F9")
                and devices
                and tcs
                and hasattr(tcs, "get_dhw_zone")
            ):
                dhw = tcs.get_dhw_zone()
                if dhw and registry:
                    for d_id in devices:
                        is_sen = (
                            getattr(d_id, "type", str(d_id)[:2]) == "07"
                            or p.get("device_role") == "dhw_sensor"
                        )
                        with contextlib.suppress(
                            exc.DeviceNotFoundError,
                            exc.SchemaInconsistentError,
                        ):
                            registry.get_device(
                                str(d_id),
                                parent=dhw,
                                child_id=domain_id,
                                is_sensor=is_sen,
                            )

        # 3. Code 0004: Zone Naming & Creation
        case Code._0004:
            if tcs:
                zone_index = p.get(SZ_ZONE_INDEX) or p.get("zone_index")
                name = p.get("name")
                if zone_index is not None and name:
                    zone = tcs.get_htg_zone(str(zone_index))
                    if zone:
                        zone.zone_state = dataclasses.replace(
                            zone.zone_state, name=name
                        )

        # 4. Code 0204: Underfloor Heating (UFH) Controller Circuits
        case Code._0204:
            ufc_list: list[Device] = []
            if registry:
                if getattr(msg.src, "type", None) in ("02", DevType.UFC):
                    with contextlib.suppress(exc.DeviceNotFoundError):
                        ufc_list.append(registry.get_device(str(msg.src.id)))
                elif getattr(msg.dst, "type", None) in ("02", DevType.UFC):
                    with contextlib.suppress(exc.DeviceNotFoundError):
                        ufc_list.append(registry.get_device(str(msg.dst.id)))
            if not ufc_list and tcs and hasattr(tcs, "ufh_controllers"):
                ufc_list = list(getattr(tcs, "ufh_controllers", {}).values())

            cct_index = (
                p.get(SZ_UFH_INDEX)
                or p.get(SZ_CIRCUIT_INDEX)
                or p.get("cct_index")
            )
            z_index = p.get(SZ_ZONE_INDEX) or p.get("zone_index")
            if cct_index is not None and ufc_list:
                cct_str = (
                    f"{int(str(cct_index), 16):02X}"
                    if isinstance(cct_index, (int, str))
                    and str(cct_index).isalnum()
                    else str(cct_index)
                )
                for ufc in ufc_list:
                    if hasattr(ufc, "circuit_by_id"):
                        ufc.circuit_by_id[cct_str] = {
                            "zone_index": str(z_index)
                            if z_index is not None
                            else None
                        }

        # 5. Code 000A: Zone Parameters & Configuration
        case Code._000A:
            pass
        case _:
            pass

    if getattr(gateway.config, "enable_eavesdrop", False):
        engine = getattr(gateway, "_eavesdrop_engine", None)
        if engine is None:
            engine = EavesdropEngine(gateway)
            with contextlib.suppress(AttributeError):
                gateway._eavesdrop_engine = engine
        await engine.process_eavesdrop(msg)
