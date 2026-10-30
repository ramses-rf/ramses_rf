#!/usr/bin/env python3
"""RAMSES RF - A pseudo-mocked serial port used for testing."""

from dataclasses import fields
from typing import Any, Final
from unittest.mock import patch

from ramses_rf import Gateway
from ramses_rf.config import SZ_CLASS
from ramses_rf.gateway import GatewayConfig
from ramses_tx.config import EngineConfig
from ramses_tx.schemas import SZ_KNOWN_LIST

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
    schema: dict[str, Any] | GatewayConfig, port_index: int
) -> tuple[str, HgiFwTypes]:
    """Return the Gateway's device_id for a schema (if required, construct
    an id).

    Does not modify the schema.

    Checks that only one Gateway is defined and ensures all 18: type
    devices have an explicit HGI class trait.
    """

    if isinstance(schema, GatewayConfig):
        known_list_dict = getattr(schema, "known_list", {}) or {}
    else:
        known_list_dict = schema.get(SZ_KNOWN_LIST, {}) or {}

    gwy_ids = [
        k
        for k, v in known_list_dict.items()
        if k[:2] == "18" and v.get(SZ_CLASS) == "HGI"
    ]

    if not gwy_ids:  # check for an 18: without a class: HGI
        gwy_ids = [k for k in known_list_dict if k[:2] == "18"]

    if gwy_ids:
        if len(gwy_ids) > 1:
            raise ValueError(f"Schema contains more than one HGI: {gwy_ids}")
        return gwy_ids[0], HgiFwTypes.EVOFW3

    # Fallback assignment mirroring the original behavior
    if port_index == 0:
        return GWY_ID_0, HgiFwTypes.EVOFW3
    elif port_index == 1:
        return GWY_ID_1, HgiFwTypes.EVOFW3

    return f"18:{port_index:06d}", HgiFwTypes.EVOFW3


@patch("ramses_tx.transport.port.MIN_INTER_WRITE_GAP", MIN_INTER_WRITE_GAP)
async def rf_factory(
    schemas: list[dict[str, Any] | GatewayConfig | None],
    start_gwys: bool = True,
) -> tuple[VirtualRf, list[Gateway]]:
    """Return the virtual network corresponding to a list of gateway
    schema/configs.

    Each dict entry will consist of a standard gateway config/schema (or
    None). Any serial port configs are ignored, and are instead allocated
    sequentially from the virtual RF pool.
    """

    if len(schemas) > MAX_NUM_PORTS:
        raise TypeError(
            f"Only a maximum of {MAX_NUM_PORTS} ports is supported"
        )

    gwys = []

    rf = VirtualRf(len(schemas))

    for index, schema in enumerate(schemas):
        if schema is None:  # assume no gateway device
            continue

        hgi_id, fw_type = _get_hgi_id_for_schema(schema, index)

        rf.set_gateway(rf.ports[index], hgi_id, fw_type=fw_type)

        with (
            patch("ramses_tx.discovery.comports", rf.comports),
            patch("serialx.async_list_serial_ports", rf.async_comports),
        ):
            if isinstance(schema, GatewayConfig):
                schema.engine.hgi_id = hgi_id
                gwy_config = schema
            else:
                config_kwargs: dict[str, Any] = {}
                schema_copy = dict(schema)
                config_dict = schema_copy.pop("config", {})

                if isinstance(config_dict, GatewayConfig):
                    config_kwargs.update(
                        {
                            f.name: getattr(config_dict, f.name)
                            for f in fields(GatewayConfig)
                        }
                    )
                else:
                    config_kwargs.update(config_dict)

                config_kwargs.update(schema_copy)

                valid_gwy_keys = {f.name for f in fields(GatewayConfig)}
                valid_eng_keys = {f.name for f in fields(EngineConfig)}

                gwy_kwargs: dict[str, Any] = {}
                eng_kwargs: dict[str, Any] = {}

                for k, v in config_kwargs.items():
                    if k in valid_gwy_keys:
                        gwy_kwargs[k] = v
                    elif k in valid_eng_keys:
                        eng_kwargs[k] = v

                eng_kwargs["hgi_id"] = hgi_id

                if eng_kwargs:
                    gwy_kwargs["engine"] = EngineConfig(**eng_kwargs)

                gwy_config = GatewayConfig(**gwy_kwargs)

            gwy = Gateway(rf.ports[index], config=gwy_config)

            if start_gwys:
                await gwy.start()
                # allows Virtual RF to capture/reply
                gwy._engine._disable_sending = False

            gwys.append(gwy)

    return rf, gwys
