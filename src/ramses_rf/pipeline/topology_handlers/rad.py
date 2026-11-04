"""RAMSES RF - CQRS Radiator & Heating Zone Topology Handler."""

from __future__ import annotations

from ramses_rf.const import SZ_DOMAIN_INDEX, SZ_ZONE_INDEX, DevType
from ramses_rf.enums import TopologyAction
from ramses_rf.messages.core import Message
from ramses_rf.models import TopologyChangedEvent
from ramses_rf.pipeline.topology_handlers.base import TopologyHandler
from ramses_rf.typing import PayloadT, SchemaT
from ramses_tx.const import I_, Code


class RadTopologyHandler(TopologyHandler):
    """CQRS Topology Handler for Radiator and standard Heating Zone devices."""

    def consume(self, msg: Message) -> None:
        """Evaluate Radiator and Heating Zone topology rules.

        :param msg: The incoming message envelope.
        :type msg: Message
        """
        self._evaluate_directed_telemetry_rules(msg)
        self._evaluate_heating_prefix_rules(msg)
        self._evaluate_eavesdrop_rules(msg)

    def _evaluate_directed_telemetry_rules(self, msg: Message) -> None:
        """Evaluate implicit bindings from directed telemetry broadcasts.

        Devices (TRVs, Thermostats, DHW sensors) explicitly declare their
        topological relationships by broadcasting telemetry (e.g., 30C9,
        3150, 1060, 1260) directly to their parent Controller (01).
        """
        if not self._enable_eavesdrop:
            return

        # Broaden the net: Intercept ANY directed telemetry to a Controller,
        # but strictly prevent the Controller from binding to itself.
        # Identify the Controller ID whether it is a directed target or a
        # broadcast addr3 target.
        ctl_id = None
        if getattr(msg.dst, "type", None) == "01":
            ctl_id = msg.dst.id
        elif getattr(msg.addr3, "type", None) == "01":
            ctl_id = msg.addr3.id

        if msg.header.verb == I_ and ctl_id and msg.src.id != ctl_id:
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
                if getattr(msg.src, "type", None) in ("04", "08", DevType.TRV):
                    metadata["class"] = "radiator_valve"

                # Determine Device Role (Fallback to hardware prefix inference)
                is_actuator = getattr(msg.src, "type", None) in (
                    "04",
                    "08",
                    "13",
                    "02",
                )
                is_sensor = getattr(msg.src, "type", None) in (
                    "00",
                    "03",
                    "07",  # DHW Sensor
                    "12",
                    "22",
                    "34",
                )

                if msg.header.code in (
                    Code._3150,
                    Code._0008,
                    Code._2309,
                    Code._000A,
                ):
                    metadata["device_role"] = "actuator"
                elif msg.header.code in (
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
                        causation=f"Rule_Telemetry_Eavesdrop_{msg.header.code}",
                    )
                )

    def _evaluate_heating_prefix_rules(self, msg: Message) -> None:
        """Evaluate passive heuristics based purely on hardware prefixes.

        Automatically promotes generic devices into specific heating domain
        subtypes (e.g., TRV, UFC, BDR) when their address is observed anywhere
        in the packet (src, dst, or embedded in payload).
        """
        if not self._enable_eavesdrop:
            return

        prefix_map = {
            # REMOVED: DevType.UFC / "02" - This greedy assumption
            # breaks HVAC validation
            DevType.HCW: DevType.HCW,
            DevType.TRV: DevType.TRV,
            DevType.BDR: DevType.BDR,
            DevType.THM: DevType.THM,
            "03": DevType.HCW,
            "04": DevType.TRV,
            "12": DevType.THM,
            "13": DevType.BDR,
            "22": DevType.THM,
            "34": DevType.THM,
        }

        # Safe L7 extraction (dropping the pre-0.60 _packet._addrs shim).
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

    def _evaluate_eavesdrop_rules(self, msg: Message) -> None:
        """Evaluate broadcast telemetry for heuristic sensor correlation."""
        if not self._enable_eavesdrop:
            return

        # Break Mypy strict typing explicitly
        raw_payload: PayloadT = msg.data

        # Catch Controller Sync Array (30C9 from 01 to --:------ or specific)
        if (
            msg.header.verb == I_
            and msg.header.code == Code._30C9
            and getattr(msg.src, "type", None) == "01"
        ):
            self._emit(
                TopologyChangedEvent(
                    action=TopologyAction.UPDATE_TRAITS,
                    device_id=msg.src.id,
                    metadata={
                        "eavesdrop": "controller_sync",
                        "payload": raw_payload,
                    },
                    causation="Rule_30C9_Controller_Sync",
                )
            )

        # Catch Orphan Sensor Broadcast (30C9 from sensors to themselves)
        elif (
            msg.header.verb == I_
            and msg.header.code == Code._30C9
            and (msg.dst.id == "--:------" or msg.dst.id == msg.src.id)
        ):
            self._emit(
                TopologyChangedEvent(
                    action=TopologyAction.UPDATE_TRAITS,
                    device_id=msg.src.id,
                    metadata={
                        "eavesdrop": "orphan_broadcast",
                        "payload": raw_payload,
                    },
                    causation="Rule_30C9_Orphan_Broadcast",
                )
            )
