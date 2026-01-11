#!/usr/bin/env python3
"""RAMSES RF - A pseudo-mocked serial port used for testing."""

from typing import Any, Final
from unittest.mock import patch

from ramses_rf import Gateway
from ramses_rf.const import DEV_TYPE_MAP, DevType
from ramses_rf.schemas import SZ_CLASS, SZ_KNOWN_LIST

from .const import MAX_NUM_PORTS, HgiFwTypes
from .virtual_rf import VirtualRf

__all__ = ["HgiFwTypes", "VirtualRf", "rf_factory"]

# patched constants
# _DBG_DISABLE_IMPERSONATION_ALERTS = True  # # ramses_tx.protocol
# _DBG_DISABLE_QOS = False  # #                 ramses_tx.protocol
MIN_INTER_WRITE_GAP = 0  # #                    ramses_tx.protocol

# other constants
GWY_ID_0: Final = "18:000000"
GWY_ID_1: Final = "18:111111"

_DEFAULT_GWY_CONFIG = {
    "config": {
        "disable_discovery": True,
        "enforce_known_list": False,
    }
}


def _get_hgi_id_for_schema(
    schema: dict[str, Any], port_idx: int
) -> tuple[str, HgiFwTypes]:
    """Return the Gateway's device_id for a schema (if required, construct an id).

    Does not modify the schema.

    Checks that only one Gateway is defined and ensures all 18: type devices
    have an explicit HGI class defined.

    If a Gateway (18:) device is present in the schema, it must have a defined class
    of "HGI". If it does, its device_id is returned, along with its FW type (if
    specified, or EVOFW3 is assumed).

    If no Gateway device is present, one is created (18:000000), and its
    details returned.

    :param schema: The configuration schema.
    :param port_idx: Index used to construct a default ID if none found.
    :raises TypeError: If multiple gateways exist or an HGI device lacks a class.
    :return: A tuple of (device_id, firmware_type).
    """

    known_list: dict[str, Any] = schema.get(SZ_KNOWN_LIST, {})

    # 1. Collect HGI IDs for validation
    hgi_ids = [
        device_id
        for device_id, v in known_list.items()
        if v.get(SZ_CLASS) == DevType.HGI
    ]

    # 2. Validation: Multiple Gateways
    if len(hgi_ids) > 1:
        raise TypeError("Multiple Gateways per schema are not supported")

    # 3. Validation: Orphaned 18: devices (Gateways without a class)
    if any(
        k
        for k, v in known_list.items()
        if k.startswith(DEV_TYPE_MAP[DevType.HGI]) and not v.get(SZ_CLASS)
    ):
        raise TypeError("Any Gateway (18:) must have its class defined explicitly")

    # 4. Logic: Return existing
    if len(hgi_ids) == 1:
        hgi_id = hgi_ids[0]
        fw_type_name = known_list[hgi_id].get("fw_version", HgiFwTypes.EVOFW3.name)
        return hgi_id, HgiFwTypes[fw_type_name]

    # 5. Logic: Create default if none present (18:000000 for idx 0, 18:111111 for idx 1)
    if port_idx == 0:
        return GWY_ID_0, HgiFwTypes.EVOFW3
    if port_idx == 1:
        return GWY_ID_1, HgiFwTypes.EVOFW3
    return f"18:{port_idx:06d}", HgiFwTypes.EVOFW3


@patch("ramses_tx.transport.MIN_INTER_WRITE_GAP", MIN_INTER_WRITE_GAP)
async def rf_factory(
    schemas: list[dict[str, Any] | None], start_gwys: bool = True
) -> tuple[VirtualRf, list[Gateway]]:
    """Return the virtual network corresponding to a list of gateway schema/configs.

    Each dict entry will consist of a standard gateway config/schema (or None). Any
    serial port configs are ignored, and are instead allocated sequentially from the
    virtual RF pool.
    """

    if len(schemas) > MAX_NUM_PORTS:
        raise TypeError(f"Only a maximum of {MAX_NUM_PORTS} ports is supported")

    gwys = []

    rf = VirtualRf(len(schemas))

    for idx, schema in enumerate(schemas):
        if schema is None:  # assume no gateway device
            # rf._create_port(idx)  # REMOVED: Redundant and causes race condition
            continue

        hgi_id, fw_type = _get_hgi_id_for_schema(schema, idx)

        # rf._create_port(idx)  # REMOVED: Redundant and causes race condition
        rf.set_gateway(rf.ports[idx], hgi_id, fw_type=fw_type)

        with patch("ramses_tx.transport.comports", rf.comports):
            gwy = Gateway(rf.ports[idx], **schema)
            # gwy._engine.ptcl.qos.disable_qos = False  # Hack for testing

            if start_gwys:
                await gwy.start()
            gwy.get_device(hgi_id)

        gwys.append(gwy)

    return rf, gwys
