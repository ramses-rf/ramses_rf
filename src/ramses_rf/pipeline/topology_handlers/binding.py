"""RAMSES RF - CQRS Binding & Protocol Topology Handler."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from ramses_rf.const import (
    SZ_ACCEPT,
    SZ_CONFIRM,
    SZ_OFFER,
    ZON_ROLE_MAP,
    DevType,
)
from ramses_rf.enums import TopologyAction
from ramses_rf.messages.core import Message
from ramses_rf.models import TopologyChangedEvent
from ramses_rf.pipeline.topology_handlers.base import TopologyHandler
from ramses_rf.protocol.ramses import CODES_ONLY_FROM_CTL
from ramses_rf.typing import SchemaT
from ramses_tx import ALL_DEV_ADDR, DeviceIdT
from ramses_tx.address import hex_id_to_dev_id
from ramses_tx.const import I_, RQ, W_, Code

if TYPE_CHECKING:
    from ramses_rf.payloads.heating import (
        ZoneDevices5BPayload,
        ZoneDevices6BPayload,
    )

_LOGGER = logging.getLogger(__name__)


class BindTopologyHandler(TopologyHandler):
    """CQRS Topology Handler for explicit binding and protocol envelope exchanges."""

    def consume(self, msg: Message) -> None:
        """Evaluate binding and protocol exchange rules.

        :param msg: The incoming message envelope.
        :type msg: Message
        """
        self._evaluate_evohome_rules(msg)
        self._evaluate_rf_bind_rules(msg)
        self._evaluate_zone_binding_rules(msg)
        self._evaluate_appliance_control_sync_rules(msg)
        self._evaluate_implicit_binding_rules(msg)
        self._evaluate_third_address_broadcast_rules(msg)

    def _evaluate_evohome_rules(self, msg: Message) -> None:
        """Evaluate Evohome controller broadcast rules.

        Historically, entities intercepted CODES_ONLY_FROM_CTL to
        dynamically promote themselves to Controllers. We now extract
        that logic into this explicit, trackable rule.
        """
        if not self._enable_eavesdrop:
            return

        if msg.header.verb == I_ and msg.header.code in CODES_ONLY_FROM_CTL:
            self._emit(
                TopologyChangedEvent(
                    action=TopologyAction.CREATE_CONTROLLER,
                    device_id=msg.src.id,
                    causation="Rule_Evohome_Controller_Broadcast",
                )
            )

    def _evaluate_rf_bind_rules(self, msg: Message) -> None:
        """Evaluate 1FC9 (rf_bind) frames to extract device bindings.

        Intercepts 1FC9 offers, accepts, and confirms, decoding
        positional binding payload chunks to emit BIND_DEVICE and
        CREATE_CONTROLLER topology events.
        """
        if msg.header.code != Code._1FC9:
            return

        dto = getattr(msg, "_dto", None)
        payload_hex: str = getattr(dto, "payload", "") if dto else ""
        if (
            not payload_hex
            or len(payload_hex) in (2, 4)
            or len(payload_hex) % 12 != 0
        ):
            return

        if msg.header.verb == I_ and msg.dst.id in (
            msg.src.id,
            ALL_DEV_ADDR.id,
        ):
            bind_phase = SZ_OFFER
        elif msg.header.verb == W_ and msg.src is not msg.dst:
            bind_phase = SZ_ACCEPT
        elif msg.header.verb == I_:
            bind_phase = SZ_CONFIRM
        else:
            bind_phase = None

        if bind_phase is None:
            return

        # Always trigger controller creation if source or destination is a controller
        if getattr(msg.src, "type", None) in ("01", DevType.CTL):
            self._emit(
                TopologyChangedEvent(
                    action=TopologyAction.CREATE_CONTROLLER,
                    device_id=msg.src.id,
                    causation="Rule_1FC9_Controller_Source",
                )
            )

        if getattr(msg.dst, "type", None) in ("01", DevType.CTL):
            self._emit(
                TopologyChangedEvent(
                    action=TopologyAction.CREATE_CONTROLLER,
                    device_id=msg.dst.id,
                    causation="Rule_1FC9_Controller_Target",
                )
            )

        for i in range(0, len(payload_hex), 12):
            chunk = payload_hex[i : i + 12]
            domain_id_hex = chunk[:2]
            opcode_hex = chunk[2:6]
            bound_dev_id = hex_id_to_dev_id(chunk[6:12])

            if bound_dev_id.startswith(
                f"{DevType.CTL}:"
            ) or bound_dev_id.startswith("01:"):
                self._emit(
                    TopologyChangedEvent(
                        action=TopologyAction.CREATE_CONTROLLER,
                        device_id=bound_dev_id,
                        causation="Rule_1FC9_Controller_Payload",
                    )
                )

            parent_id: DeviceIdT
            child_id: DeviceIdT

            if bind_phase == SZ_ACCEPT:
                parent_id = msg.dst.id
                child_id = msg.src.id
            elif bind_phase == SZ_CONFIRM:
                parent_id = msg.src.id
                child_id = (
                    msg.dst.id
                    if msg.dst.id not in ("--:------", ALL_DEV_ADDR.id)
                    else bound_dev_id
                )
            else:  # SZ_OFFER
                parent_id = msg.src.id
                child_id = bound_dev_id

            self._emit(
                TopologyChangedEvent(
                    action=TopologyAction.BIND_DEVICE,
                    parent_id=parent_id,
                    child_id=child_id,
                    metadata={
                        "domain_id": domain_id_hex,
                        "opcode": opcode_hex,
                        "phase": bind_phase,
                    },
                    causation=f"Rule_1FC9_Binding_{bind_phase.capitalize()}",
                )
            )

    def _evaluate_zone_binding_rules(self, msg: Message) -> None:
        """Evaluate 000C and heuristic packets to bind actuators to zones.

        Processes explicit 000C configuration broadcasts from
        Controllers (01) to extract device roles and bind child devices to
        specific zone indices or domain IDs.
        """
        # EXPLICIT BINDING: Controllers (01) broadcasting 000C device maps
        if (
            getattr(msg, "code", None) == Code._000C
            or getattr(msg.header, "code", None) == Code._000C
        ) and (
            msg.src.id.startswith("01:")
            or getattr(msg.src, "type", None) == "01"
        ):
            for payload in self._get_payloads(msg):
                zone_index: str | None = None
                domain_id: str | None = None
                device_role: str | None = None
                zone_type: str | None = None
                devices: list[str] = []

                if hasattr(payload, "device_id_str"):
                    zi = getattr(payload, "zone_index", None)
                    zone_index = (
                        f"{zi:02X}"
                        if isinstance(zi, int)
                        else (str(zi) if zi is not None else None)
                    )
                    domain_id = getattr(payload, "domain_id", None)
                    zone_devices = cast(
                        "ZoneDevices5BPayload | ZoneDevices6BPayload", payload
                    )
                    device_role = f"{zone_devices.device_role_id:02X}"
                    devices = [zone_devices.device_id_str]
                elif isinstance(payload, dict):
                    val_zone = payload.get("zone_index") or payload.get(
                        "zone_index"
                    )
                    zone_index = (
                        str(val_zone) if val_zone is not None else None
                    )

                    val_domain = (
                        payload.get("domain_index")
                        or payload.get("domain_id")
                        or payload.get("domain_index")
                    )
                    domain_id = (
                        str(val_domain) if val_domain is not None else None
                    )

                    val_role = payload.get("device_role")
                    device_role = (
                        str(val_role) if val_role is not None else None
                    )

                    val_type = payload.get("zone_type")
                    zone_type = str(val_type) if val_type is not None else None

                    devices = [str(d) for d in payload.get("devices", [])]

                if not devices:
                    continue

                # Prepare the base metadata dict, correctly flagging
                # all types of sensors (e.g., 'sensor', 'dhw_sensor')
                metadata: SchemaT = {}
                device_role_str = (
                    str(device_role) if device_role is not None else ""
                )

                if device_role is not None:
                    # 04 is sensor, 08 is actuator, dhw_valve, etc.
                    metadata["is_sensor"] = (
                        "sensor" in device_role_str or device_role_str == "04"
                    )
                    metadata["device_role"] = (
                        device_role_str  # Explicit DHW preservation
                    )

                if (
                    zone_type is not None
                    and zone_type in ZON_ROLE_MAP.HEAT_ZONES
                ):
                    metadata["class"] = ZON_ROLE_MAP[zone_type]

                # Implicit Zone Class Inference: If we bind an actuator, its type implies the zone class
                if device_role_str in (
                    "08",
                    "rad_actuator",
                ) and not metadata.get("class"):
                    metadata["class"] = ZON_ROLE_MAP["08"]  # radiator_valve

                if domain_id is not None:
                    event_meta = dict(metadata)
                    event_meta["domain_id"] = str(domain_id)
                    event_meta["child_id"] = str(domain_id)
                    for child_id in devices:
                        self._emit(
                            TopologyChangedEvent(
                                action=TopologyAction.BIND_DEVICE,
                                parent_id=msg.src.id,  # The Controller
                                child_id=cast(
                                    DeviceIdT, child_id
                                ),  # The Device
                                metadata=event_meta,
                                causation="Rule_000C_Domain_Binding",
                            )
                        )
                elif zone_index is not None:
                    # Clone metadata to avoid cross-iteration pollution
                    event_meta = dict(metadata)
                    event_meta["zone_index"] = str(zone_index)
                    # Bridging quirk: DeviceRegistry expects domain index under child_id
                    event_meta["child_id"] = str(zone_index)
                    for child_id in devices:
                        if (
                            child_id.startswith(f"{DevType.CTL}:")
                            or child_id.startswith(f"{DevType.UFC}:")
                            or child_id.startswith(f"{DevType.HGI}:")
                            or child_id.startswith("63:")
                        ):
                            continue
                        self._emit(
                            TopologyChangedEvent(
                                action=TopologyAction.BIND_DEVICE,
                                parent_id=msg.src.id,  # The Controller
                                child_id=cast(
                                    DeviceIdT, child_id
                                ),  # The Device
                                metadata=event_meta,
                                causation="Rule_000C_Zone_Binding",
                            )
                        )

    def _evaluate_appliance_control_sync_rules(self, msg: Message) -> None:
        """Evaluate direct configuration syncs to map System Relay."""
        if not self._enable_eavesdrop:
            return

        # Guard: Catch direct commands from the Controller (01) to a Relay (13)
        if (
            getattr(msg.src, "type", None) == "01"
            and msg.dst.id != "--:------"
            and getattr(msg.dst, "type", None) == "13"
        ):
            # 1100 (Boiler Params) or 10E0/1FC9 (Binding) are direct links
            if msg.header.code in (Code._1100, Code._10E0, Code._1FC9):
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

    def _evaluate_implicit_binding_rules(self, msg: Message) -> None:
        """Evaluate implicit bindings from directed controller polls.

        If a Controller (01:) explicitly sends a direct command (RQ, W)
        to a heating device (e.g., 04: TRV, 00: Zone Sensor, 08: Relay),
        it implies the controller believes that device belongs to its
        network.
        """
        if not self._enable_eavesdrop:
            return

        # 1. We only care about explicit, directed requests/writes
        if msg.header.verb not in (RQ, W_):
            return

        # 2. The source MUST be a Controller
        if getattr(msg.src, "type", None) != "01":
            return

        # 3. The target MUST be a valid Heating Domain device
        # (00 = Zone Sensor, 04 = TRV, 08 = Relay/BDR91)
        if msg.dst.id == "--:------":
            return

        dst_type = getattr(msg.dst, "type", None)
        if dst_type not in ("00", "04", "08"):
            return

        # Emit the topology mutation event. The downstream Registry
        # will safely process this and ignore it if already bound.
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
        """Evaluate bindings from 3rd address field of broadcasts.

        Many heating devices broadcast telemetry (I ---) to no particular
        address (--:------), but explicitly declare their parent
        Controller in the third address slot of the RF frame.
        """
        if not self._enable_eavesdrop:
            return

        if msg.header.verb != I_:
            return

        # Pure L7 architectural access using the new Domain property
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
