"""RAMSES RF - Eavesdropping Engine.

Isolated component for telemetry-driven heuristic topology inference.
Runs only when `gwy.config.enable_eavesdrop` is True.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta as td
from typing import TYPE_CHECKING, cast

import ramses_rf.exceptions as exc
from ramses_rf.const import (
    SZ_DOMAIN_INDEX,
    SZ_TEMPERATURE,
    SZ_ZONE_INDEX,
    ZON_ROLE_MAP,
    DevType,
)
from ramses_rf.devices import Device, Temperature
from ramses_rf.enums import TopologyAction
from ramses_rf.models import TopologyChangedEvent
from ramses_rf.protocol.ramses import (
    CODES_ONLY_FROM_CTL,
    HVAC_KLASS_BY_VC_PAIR,
)
from ramses_rf.typing import SchemaT
from ramses_tx import Code
from ramses_tx.address import HGI_DEV_ADDR
from ramses_tx.const import I_, RQ
from ramses_tx.typing import DeviceIdT

if TYPE_CHECKING:
    from ramses_rf import Gateway
    from ramses_rf.messages import Message
    from ramses_rf.systems import Evohome


class EavesdropEngine:
    """Heuristic eavesdropping engine for un-bound log replay."""

    def __init__(self, gateway: Gateway) -> None:
        """Initialize the eavesdropping engine.

        :param gateway: The Gateway instance.
        :type gateway: Gateway
        """
        self._gateway: Gateway = gateway
        self._prev_30c9_map: dict[str, Message] = {}

    def _emit(self, event: TopologyChangedEvent) -> None:
        """Emit a heuristic topology event to the DeviceRegistry."""
        registry = getattr(self._gateway, "device_registry", None)
        if registry:
            registry.handle_topology_event(event)

    def _get_payloads(self, msg: Message) -> list[object]:
        """Safely extract the array or standard dictionary payload."""
        raw: object = getattr(msg, "payload", getattr(msg, "data", {}))
        if isinstance(raw, dict):
            result = raw.get("_array", [raw])
            return result if isinstance(result, list) else [result]
        if isinstance(raw, list):
            return raw
        return []

    def eavesdrop_referenced_devices(self, msg: Message) -> None:
        """Instantiate implicitly referenced header devices during eavesdropping.

        :param msg: The incoming message.
        :type msg: Message
        """
        if not getattr(self._gateway.config, "enable_eavesdrop", False):
            return

        registry = getattr(self._gateway, "device_registry", None)
        if not registry:
            return

        hgi_id = self._gateway.hgi.id if self._gateway.hgi else None
        dst_dev = registry.device_by_id.get(msg.dst.id)

        if dst_dev is None and msg.src.id != hgi_id:
            with contextlib.suppress(exc.DeviceNotFoundError):
                registry.get_device(msg.dst.id)

        addrs_to_check = [msg.src.id, msg.dst.id]
        if hasattr(msg, "_dto") and msg._dto:
            addrs_to_check.extend(
                [
                    cast(DeviceIdT, msg._dto.addr1),
                    cast(DeviceIdT, msg._dto.addr2),
                    cast(DeviceIdT, msg._dto.addr3),
                ]
            )

        for addr_id in dict.fromkeys(addrs_to_check):
            if addr_id and addr_id not in ("--:------", "63:262142"):
                if (
                    self._gateway.config.engine.enforce_known_list
                    and addr_id[:2] == "18"
                    and addr_id != HGI_DEV_ADDR.id
                    and addr_id != hgi_id
                ):
                    continue
                with contextlib.suppress(exc.DeviceNotFoundError):
                    registry.get_device(addr_id)

    def _get_tcs(self, msg: Message) -> Evohome | None:
        """Find the active TCS read-model instance."""
        if tcs := getattr(msg.src, "tcs", None):
            return cast("Evohome", tcs)
        if tcs := getattr(msg.dst, "tcs", None):
            return cast("Evohome", tcs)
        if controller := getattr(msg.src, "ctl", None):
            if tcs := getattr(controller, "tcs", None):
                return cast("Evohome", tcs)
        if controller := getattr(msg.dst, "ctl", None):
            if tcs := getattr(controller, "tcs", None):
                return cast("Evohome", tcs)
        if tcs := getattr(self._gateway, "tcs", None):
            return cast("Evohome", tcs)

        registry = getattr(self._gateway, "device_registry", None)
        if registry:
            ctls = [
                d
                for d in list(registry.device_by_id.values())
                if (
                    getattr(d, "_SLUG", "") == "CTL"
                    or getattr(d, "type", None) == "01"
                )
                and hasattr(d, "tcs")
                and d.tcs is not None
            ]
            if len(ctls) == 1:
                return cast("Evohome", ctls[0].tcs)
        return None

    async def process_eavesdrop(self, msg: Message) -> None:
        """Process an incoming message for heuristic eavesdropping.

        :param msg: The incoming message.
        :type msg: Message
        """
        if not getattr(self._gateway.config, "enable_eavesdrop", False):
            return

        tcs = self._get_tcs(msg)

        # 1. Telemetry-driven Sensor/Actuator Linking
        self._evaluate_evohome_rules(msg)
        self._evaluate_directed_telemetry_rules(msg)
        self._evaluate_ufh_communication_rules(msg)
        self._evaluate_hvac_rules(msg)
        self._evaluate_dhw_opentherm_rules(msg)
        self._evaluate_heating_prefix_rules(msg)
        self._evaluate_appliance_control_sync_rules(msg)
        self._evaluate_appliance_eavesdrop_rules(msg)
        self._evaluate_zone_type_eavesdrop_rules(msg)
        self._evaluate_implicit_binding_rules(msg)
        self._evaluate_third_address_broadcast_rules(msg)

        # 2. 30C9 Temperature Broadcast Correlation
        if msg.code == Code._30C9 and tcs:
            ctl_id = str(getattr(tcs.ctl, "id", ""))
            prev = self._prev_30c9_map.get(ctl_id)

            if msg._has_array:
                await self._eavesdrop_from_controller_broadcast(tcs, msg, prev)
                self._prev_30c9_map[ctl_id] = msg
            elif getattr(msg.src, "type", None) in (
                "03",
                "04",
                "12",
                "22",
                "34",
            ):
                await self._eavesdrop_from_trv_broadcast(tcs, msg)

    def _evaluate_evohome_rules(self, msg: Message) -> None:
        """Evaluate rules specific to the Evohome CH/DHW ecosystem."""
        if msg.verb == I_ and msg.code in CODES_ONLY_FROM_CTL:
            self._emit(
                TopologyChangedEvent(
                    action=TopologyAction.CREATE_CONTROLLER,
                    device_id=msg.src.id,
                    causation="Rule_Evohome_Controller_Broadcast",
                )
            )

    def _evaluate_directed_telemetry_rules(self, msg: Message) -> None:
        """Evaluate implicit bindings from directed telemetry broadcasts."""
        ctl_id = None
        if getattr(msg.dst, "type", None) == "01":
            ctl_id = msg.dst.id
        elif getattr(msg.addr3, "type", None) == "01":
            ctl_id = msg.addr3.id

        if msg.verb == I_ and ctl_id and msg.src.id != ctl_id:
            for payload in self._get_payloads(msg):
                if not isinstance(payload, dict):
                    continue

                zone_index = payload.get(
                    SZ_ZONE_INDEX, payload.get("zone_index")
                )
                domain_id = payload.get(
                    SZ_DOMAIN_INDEX,
                    payload.get("domain_id", payload.get("domain_index")),
                )

                if zone_index is None and domain_id is None:
                    continue

                metadata: SchemaT = {}

                is_actuator = getattr(msg.src, "type", None) in (
                    "04",
                    "08",
                    "13",
                    "02",
                )
                is_sensor = getattr(msg.src, "type", None) in (
                    "00",
                    "03",
                    "07",
                    "12",
                    "22",
                    "34",
                )

                if msg.code in (
                    Code._3150,
                    Code._0008,
                    Code._2309,
                    Code._000A,
                ):
                    metadata["device_role"] = "actuator"
                elif msg.code in (
                    Code._30C9,
                    Code._1260,
                    Code._10A0,
                    Code._12B0,
                ):
                    metadata["device_role"] = (
                        "sensor" if is_sensor else "actuator"
                    )
                    if is_sensor:
                        metadata["is_sensor"] = "True"
                else:
                    metadata["device_role"] = (
                        "actuator" if is_actuator else "sensor"
                    )
                    if is_sensor:
                        metadata["is_sensor"] = "True"

                if zone_index is not None:
                    metadata["zone_index"] = str(zone_index)
                    metadata["child_id"] = str(zone_index)
                elif domain_id is not None:
                    if domain_id in ("F9", "FA", "FC"):
                        metadata["domain_id"] = str(domain_id)
                        metadata["child_id"] = str(domain_id)
                    else:
                        metadata["zone_index"] = str(domain_id)
                        metadata["domain_id"] = str(domain_id)
                        metadata["child_id"] = str(domain_id)

                self._emit(
                    TopologyChangedEvent(
                        action=TopologyAction.BIND_DEVICE,
                        parent_id=ctl_id,
                        child_id=msg.src.id,
                        metadata=metadata,
                        causation=f"Rule_Telemetry_Eavesdrop_{msg.code}",
                    )
                )

    def _evaluate_ufh_communication_rules(self, msg: Message) -> None:
        """Evaluate rules specific to Underfloor Heating (UFH) communication eavesdropping."""
        is_ufc_src = getattr(msg.src, "type", None) == "02"
        is_ufc_dst = getattr(msg.dst, "type", None) == "02"

        if not (is_ufc_src or is_ufc_dst):
            return

        ufc_id = msg.src.id if is_ufc_src else msg.dst.id

        ctl_id = None
        if getattr(msg.src, "type", None) == "01":
            ctl_id = msg.src.id
        elif getattr(msg.dst, "type", None) == "01":
            ctl_id = msg.dst.id
        elif getattr(msg.addr3, "type", None) == "01":
            ctl_id = msg.addr3.id

        if ctl_id and ctl_id != ufc_id:
            event_promote = TopologyChangedEvent(
                action=TopologyAction.UPDATE_DEVICE_CLASS,
                device_id=ufc_id,
                metadata={"device_class": DevType.UFC},
                causation="Rule_UFH_Communication_Promotion",
            )
            self._emit(event_promote)

            event_bind = TopologyChangedEvent(
                action=TopologyAction.BIND_DEVICE,
                parent_id=ctl_id,
                child_id=ufc_id,
                metadata={"device_role": "ufc"},
                causation="Rule_UFH_Communication_Binding",
            )
            self._emit(event_bind)

    def _evaluate_hvac_rules(self, msg: Message) -> None:
        """Evaluate rules specific to Ventilation and HVAC eavesdropping."""
        msg_verb = msg.verb
        msg_code = str(msg.code)

        dev_class = None
        for (
            schema_verb,
            schema_code,
        ), dev_class_name in HVAC_KLASS_BY_VC_PAIR.items():
            if (schema_verb is None or schema_verb == msg_verb) and str(
                schema_code
            ) == msg_code:
                dev_class = dev_class_name
                break

        if dev_class:
            if (
                msg.src.id != "--:------"
                and getattr(msg.src, "type", None) != "01"
            ):
                self._emit(
                    TopologyChangedEvent(
                        action=TopologyAction.UPDATE_DEVICE_CLASS,
                        device_id=msg.src.id,
                        metadata={"device_class": dev_class},
                        causation="Rule_HVAC_Signature_Source",
                    )
                )

            if (
                msg.dst.id != "--:------"
                and msg.dst.id != msg.src.id
                and getattr(msg.dst, "type", None) != "01"
            ):
                self._emit(
                    TopologyChangedEvent(
                        action=TopologyAction.UPDATE_DEVICE_CLASS,
                        device_id=msg.dst.id,
                        metadata={"device_class": dev_class},
                        causation="Rule_HVAC_Signature_Target",
                    )
                )

    def _evaluate_dhw_opentherm_rules(self, msg: Message) -> None:
        """Evaluate rules specific to DHW and OpenTherm Bridges."""
        if msg.code == Code._3220 and getattr(msg.src, "type", None) == "10":
            self._emit(
                TopologyChangedEvent(
                    action=TopologyAction.UPDATE_DEVICE_CLASS,
                    device_id=msg.src.id,
                    metadata={"device_class": DevType.OTB},
                    causation="Rule_OTB_3220_Signature",
                )
            )

        elif (
            msg.code in (Code._1260, Code._10A0)
            and getattr(msg.src, "type", None) == "07"
        ):
            self._emit(
                TopologyChangedEvent(
                    action=TopologyAction.UPDATE_DEVICE_CLASS,
                    device_id=msg.src.id,
                    metadata={"device_class": DevType.DHW},
                    causation="Rule_DHW_Signature",
                )
            )

    def _evaluate_heating_prefix_rules(self, msg: Message) -> None:
        """Evaluate passive heuristics based purely on hardware prefixes."""
        prefix_map = {
            "03": DevType.HCW,
            "04": DevType.TRV,
            "12": DevType.THM,
            "13": DevType.BDR,
            "22": DevType.THM,
            "34": DevType.THM,
        }

        addrs = [msg.src]
        if msg.dst.id != "--:------" and msg.dst.id != msg.src.id:
            addrs.append(msg.dst)

        for addr in addrs:
            if getattr(addr, "type", None) in prefix_map:
                self._emit(
                    TopologyChangedEvent(
                        action=TopologyAction.UPDATE_DEVICE_CLASS,
                        device_id=addr.id,
                        metadata={"device_class": prefix_map[addr.type]},
                        causation="Rule_Heating_Prefix_Heuristic",
                    )
                )

    def _evaluate_appliance_control_sync_rules(self, msg: Message) -> None:
        """Evaluate direct configuration syncs to map System Relay."""
        if (
            getattr(msg.src, "type", None) == "01"
            and msg.dst.id != "--:------"
            and getattr(msg.dst, "type", None) == "13"
        ):
            if msg.code in (Code._1100, Code._10E0, Code._1FC9):
                self._emit(
                    TopologyChangedEvent(
                        action=TopologyAction.BIND_DEVICE,
                        parent_id=msg.src.id,
                        child_id=msg.dst.id,
                        metadata={
                            "domain_id": "FC",
                            "child_id": "FC",
                            "device_role": "appliance_control",
                        },
                        causation="Rule_Direct_Relay_Sync",
                    )
                )

    def _evaluate_appliance_eavesdrop_rules(self, msg: Message) -> None:
        """Evaluate legacy passive eavesdropping of System Relay."""
        if msg.code not in (Code._3220, Code._3B00, Code._3EF0):
            return

        app_cntrl_id: DeviceIdT | None = None

        if msg.code == Code._3220 and msg.verb == RQ:
            if (
                getattr(msg.src, "type", None) == "01"
                and msg.dst.id != "--:------"
                and getattr(msg.dst, "type", None) == "10"
            ):
                app_cntrl_id = msg.dst.id

        elif msg.code == Code._3EF0 and msg.verb == RQ:
            if (
                getattr(msg.src, "type", None) == "01"
                and msg.dst.id != "--:------"
                and getattr(msg.dst, "type", None) in ("10", "13")
            ):
                app_cntrl_id = msg.dst.id

        if app_cntrl_id is not None:
            self._emit(
                TopologyChangedEvent(
                    action=TopologyAction.BIND_DEVICE,
                    parent_id=msg.src.id,
                    child_id=app_cntrl_id,
                    metadata={
                        "domain_id": "FC",
                        "child_id": "FC",
                        "device_role": "appliance_control",
                    },
                    causation="Rule_Legacy_Appliance_Eavesdrop",
                )
            )

    def _evaluate_zone_type_eavesdrop_rules(self, msg: Message) -> None:
        """Evaluate legacy passive promotion of zone classes."""
        for payload in self._get_payloads(msg):
            if not isinstance(payload, dict):
                continue

            zone_index = payload.get(SZ_ZONE_INDEX, payload.get("zone_index"))
            if zone_index is None:
                continue

            zone_class: str | None = None

            if msg.code in (Code._0008, Code._0009):
                zone_class = ZON_ROLE_MAP["ELE"]

            if zone_class is not None:
                ctl_id = None
                if getattr(msg.dst, "type", None) == "01":
                    ctl_id = msg.dst.id
                elif getattr(msg.src, "type", None) == "01":
                    ctl_id = msg.src.id
                elif getattr(msg.addr3, "type", None) == "01":
                    ctl_id = msg.addr3.id

                if ctl_id is not None:
                    self._emit(
                        TopologyChangedEvent(
                            action=TopologyAction.UPDATE_TRAITS,
                            device_id=ctl_id,
                            metadata={
                                "zone_index": str(zone_index),
                                "class": zone_class,
                            },
                            causation="Rule_Legacy_Zone_Type_Eavesdrop",
                        )
                    )

    def _evaluate_implicit_binding_rules(self, msg: Message) -> None:
        """Evaluate implicit bindings from directed controller polls."""
        if msg.verb not in (RQ, "W"):
            return

        if getattr(msg.src, "type", None) != "01":
            return

        if msg.dst.id == "--:------":
            return

        dst_type = getattr(msg.dst, "type", None)
        if dst_type not in ("00", "04", "08"):
            return

        self._emit(
            TopologyChangedEvent(
                action=TopologyAction.BIND_DEVICE,
                parent_id=msg.src.id,
                child_id=msg.dst.id,
                metadata={
                    "device_role": "actuator"
                    if dst_type in ("04", "08")
                    else "sensor"
                },
                causation="Rule_Implicit_Poll_Binding",
            )
        )

    def _evaluate_third_address_broadcast_rules(self, msg: Message) -> None:
        """Evaluate bindings from 3rd address field of broadcasts."""
        if msg.verb != I_:
            return

        src_type = getattr(msg.src, "type", None)
        if getattr(msg.addr3, "type", None) == "01" and src_type in (
            "00",
            "04",
            "08",
        ):
            self._emit(
                TopologyChangedEvent(
                    action=TopologyAction.BIND_DEVICE,
                    parent_id=msg.addr3.id,
                    child_id=msg.src.id,
                    metadata={
                        "device_role": "actuator"
                        if src_type in ("04", "08")
                        else "sensor"
                    },
                    causation="Rule_3rd_Address_Declaration",
                )
            )

    async def _eavesdrop_from_controller_broadcast(
        self, tcs: Evohome, msg: Message, prev: Message | None
    ) -> None:
        """Correlate recently heard TRV temperatures against a new zone array."""
        if prev is None:
            return

        secs = await tcs.entity_state.get_value(
            Code._1F09, key="remaining_seconds"
        )
        if not isinstance(secs, (int, float)):
            secs = 300.0
        if msg.dtm > prev.dtm + td(seconds=secs + 5):
            return

        changed_zones: dict[str, float] = {
            (z.get(SZ_ZONE_INDEX) or z.get("zone_index")): z.get(
                SZ_TEMPERATURE
            )
            for z in msg.payload
            if z not in prev.payload and z.get(SZ_TEMPERATURE) is not None
        }
        if not changed_zones:
            return

        def _testable_zones(chg_zones: dict[str, float]) -> dict[float, str]:
            result: dict[float, str] = {}
            for i1, t1 in chg_zones.items():
                zone = tcs.zone_by_index.get(i1) or (
                    tcs.get_htg_zone(i1)
                    if hasattr(tcs, "get_htg_zone")
                    else None
                )
                if (
                    zone is not None
                    and zone.sensor is None
                    and t1
                    not in [t2 for i2, t2 in chg_zones.items() if i2 != i1]
                ):
                    result[t1] = i1
            return result

        testable_zones = _testable_zones(changed_zones)
        if not testable_zones:
            return

        testable_sensors_map: dict[float, list[Device]] = {}
        for device in self._gateway.device_registry.devices:
            if isinstance(device, Temperature) and device.ctl in (
                tcs.ctl,
                None,
            ):
                d_temp = await device.temperature()
                if d_temp is None:
                    continue
                # Only consider devices that broadcast 30C9 after the
                # previous controller array — this ensures temporal
                # correlation within the same polling cycle (issue 1010).
                d_msgs = await device.entity_state.get_message_log_flat()
                if Code._30C9 not in d_msgs:
                    continue
                if d_msgs[Code._30C9].dtm <= prev.dtm:
                    continue
                if d_temp not in testable_sensors_map:
                    testable_sensors_map[d_temp] = []
                testable_sensors_map[d_temp].append(device)

        unique_sensors: dict[float, Device] = {}
        for temp_val, candidate_devices in testable_sensors_map.items():
            if len(candidate_devices) == 1:
                unique_sensors[temp_val] = candidate_devices[0]
            else:
                dedicated = [
                    d
                    for d in candidate_devices
                    if not str(getattr(d, "id", "")).startswith("04:")
                ]
                if len(dedicated) == 1:
                    unique_sensors[temp_val] = dedicated[0]

        if not unique_sensors:
            return

        matched_pairs = {
            sensor: zone_index
            for temp_z, zone_index in testable_zones.items()
            for temp_s, sensor in unique_sensors.items()
            if temp_z == temp_s
        }

        for sensor, zone_index in matched_pairs.items():
            zone = tcs.zone_by_index[zone_index]
            if getattr(sensor, "_parent", None) is not None and getattr(
                sensor._parent, "id", None
            ) != getattr(zone, "id", None):
                continue
            with contextlib.suppress(
                exc.DeviceNotFoundError,
                exc.SchemaInconsistentError,
                exc.SystemSchemaInconsistent,
            ):
                self._gateway.device_registry.get_device(
                    sensor.id, parent=zone, is_sensor=True
                )

        if any(z for z in tcs.zones if z.sensor is tcs.ctl):
            return

        remaining_zones = _testable_zones(changed_zones)
        if len(remaining_zones) != 1:
            return

        temp, zone_index = tuple(remaining_zones.items())[0]

        if not [s for s in unique_sensors if s == temp]:
            zone = tcs.zone_by_index[zone_index]
            with contextlib.suppress(
                exc.DeviceNotFoundError,
                exc.SchemaInconsistentError,
                exc.SystemSchemaInconsistent,
            ):
                self._gateway.device_registry.get_device(
                    tcs.ctl.id, parent=zone, is_sensor=True
                )

    async def _eavesdrop_from_trv_broadcast(
        self, tcs: Evohome, msg: Message
    ) -> None:
        """Correlate a new TRV temperature broadcast against known zones.

        A TRV can be both an actuator and the designated zone sensor —
        Evohome lets the user pick which TRV senses temperature for the
        zone.  Therefore we must NOT skip TRVs that already have a parent
        zone (issue 1010).
        """
        if not isinstance(msg.payload, dict):
            return

        # Skip devices that are already a sensor for another zone.
        device = self._gateway.device_registry.device_by_id.get(msg.src.id)
        if device is not None and getattr(device, "_is_sensor", False):
            return

        trv_temp = msg.payload.get(SZ_TEMPERATURE)
        if trv_temp is None:
            return

        matching_zones = []
        for zone in tcs.zones:
            if zone._sensor is None:
                zone_temp = await zone.temperature()
                if zone_temp == trv_temp:
                    matching_zones.append(zone)

        # COLLISION ABSTENTION: Bind only if exactly one zone matches
        if len(matching_zones) == 1:
            with contextlib.suppress(
                exc.DeviceNotFoundError,
                exc.SchemaInconsistentError,
                exc.SystemSchemaInconsistent,
            ):
                self._gateway.device_registry.get_device(
                    msg.src.id, parent=matching_zones[0], is_sensor=True
                )
