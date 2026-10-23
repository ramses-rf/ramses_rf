"""RAMSES RF - CQRS Underfloor Heating (UFH) Topology Handler."""

from __future__ import annotations

import logging
from typing import cast

from ramses_rf.const import (
    DEV_ROLE_MAP,
    DEV_TYPE_MAP,
    SZ_CHILD_ID,
    SZ_DEVICE_ROLE,
    SZ_DEVICES,
    SZ_UFH_INDEX,
    SZ_ZONE_INDEX,
    SZ_ZONE_TYPE,
    ZON_ROLE_MAP,
    DevType,
)
from ramses_rf.enums import TopologyAction
from ramses_rf.messages.core import Message
from ramses_rf.models import TopologyChangedEvent
from ramses_rf.pipeline.topology_handlers.base import TopologyHandler
from ramses_rf.typing import PayloadT
from ramses_tx.const import Code
from ramses_tx.typing import DeviceIdT

_LOGGER = logging.getLogger(__name__)


class UfhTopologyHandler(TopologyHandler):
    """CQRS Topology Handler for Underfloor Heating Controllers (UFC / 02:)."""

    def consume(self, msg: Message) -> None:
        """Evaluate rules specific to Underfloor Heating (UFH).

        UFCs broadcast their circuit mappings via 000C messages.
        We intercept these to bind the UFC to the Controller and map
        the individual circuits to their corresponding zones.
        Note: This is explicit configuration data, not a heuristic,
        so it is processed regardless of the enable_eavesdrop flag.

        :param msg: The immutable Message L7 envelope to evaluate.
        :type msg: Message
        :returns: None
        :rtype: None
        """
        is_ufc_src = getattr(msg.src, "type", None) in (
            DEV_TYPE_MAP.UFC,
            DevType.UFC,
            "02",
        )
        is_ufc_dst = getattr(msg.dst, "type", None) in (
            DEV_TYPE_MAP.UFC,
            DevType.UFC,
            "02",
        )

        ufc_id: str | None = (
            msg.src.id if is_ufc_src else (msg.dst.id if is_ufc_dst else None)
        )

        # Identify the Controller ID if present in the conversation
        ctl_id: str | None = None
        if getattr(msg.src, "type", None) in (
            DEV_TYPE_MAP.CTL,
            DevType.CTL,
            "01",
        ):
            ctl_id = msg.src.id
        elif getattr(msg.dst, "type", None) in (
            DEV_TYPE_MAP.CTL,
            DevType.CTL,
            "01",
        ):
            ctl_id = msg.dst.id
        elif getattr(msg.addr3, "type", None) in (
            DEV_TYPE_MAP.CTL,
            DevType.CTL,
            "01",
        ):
            ctl_id = msg.addr3.id

        # 1. Conversational Binding: Promote and bind if communicating with a Controller
        if ctl_id and ufc_id and ctl_id != ufc_id:
            # Explicitly promote to UFC to prevent strict parser dropping
            event_promote = TopologyChangedEvent(
                action=TopologyAction.UPDATE_DEVICE_CLASS,
                device_id=cast(DeviceIdT, ufc_id),
                metadata={"device_class": DevType.UFC},
                causation="Rule_UFH_Communication_Promotion",
            )
            self._emit(event_promote)

            # Bind the UFC to the parent Controller
            event_bind = TopologyChangedEvent(
                action=TopologyAction.BIND_DEVICE,
                parent_id=cast(DeviceIdT, ctl_id),
                child_id=cast(DeviceIdT, ufc_id),
                metadata={SZ_DEVICE_ROLE: DEV_ROLE_MAP.UFH},
                causation="Rule_UFH_Communication_Binding",
            )
            self._emit(event_bind)

        # 2. Extract circuit topology from 000C configuration packets
        if msg.header.code == Code._000C:
            raw_data: PayloadT = msg.data
            zone_type = (
                raw_data.get(SZ_ZONE_TYPE)
                if isinstance(raw_data, dict)
                else None
            )
            if zone_type and zone_type not in (
                ZON_ROLE_MAP.ACT,
                ZON_ROLE_MAP.UFH,
                "09",
            ):
                return

            for payload in self._get_payloads(msg):
                if not isinstance(payload, dict):
                    continue

                target_ufc_id = ufc_id
                if not target_ufc_id:
                    for device_id in payload.get(SZ_DEVICES, []):
                        device_str = str(device_id)
                        if (
                            device_str.startswith(f"{DEV_TYPE_MAP.UFC}:")
                            or device_str.startswith(f"{DevType.UFC}:")
                            or getattr(device_id, "type", None)
                            in (DEV_TYPE_MAP.UFC, DevType.UFC, "02")
                        ):
                            target_ufc_id = device_str
                            break

                ufh_index = payload.get(SZ_UFH_INDEX)
                zone_index = payload.get(SZ_ZONE_INDEX)

                if target_ufc_id and ufh_index is not None:
                    zone_index_str = (
                        str(zone_index) if zone_index is not None else "None"
                    )
                    event_circuit = TopologyChangedEvent(
                        action=TopologyAction.CREATE_CIRCUIT,
                        device_id=cast(DeviceIdT, target_ufc_id),
                        metadata={
                            SZ_UFH_INDEX: str(ufh_index),
                            SZ_ZONE_INDEX: zone_index_str,
                            SZ_CHILD_ID: zone_index_str,
                            "ufh_index": str(ufh_index),
                            "zone_index": zone_index_str,
                            "child_id": zone_index_str,
                            "tcs_id": str(ctl_id) if ctl_id else "None",
                        },
                        causation="Rule_UFH_000C_Circuit",
                    )
                    self._emit(event_circuit)
