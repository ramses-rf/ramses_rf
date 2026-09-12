#!/usr/bin/env python3
"""RAMSES RF - Hardware discovery and identification."""

from __future__ import annotations

import asyncio
import logging
import os

import serialx
from serialx import SerialException, serial_for_url

from . import exceptions as exc
from .typing import SerPortNameT

_LOGGER = logging.getLogger(__name__)

__all__ = ["comports", "is_hgi80"]


def comports(
    include_links: bool = False,
    _hide_subsystems: list[str] | None = None,
) -> list[serialx.SerialPortInfo]:
    """Return a list of available serial port info objects.

    :param include_links: Ignored, retained for backwards compatibility.
    :type include_links: bool
    :param _hide_subsystems: Ignored, retained for backwards compatibility.
    :type _hide_subsystems: list[str] | None
    :returns: List of serial port information objects.
    :rtype: list[serialx.SerialPortInfo]
    """
    del include_links, _hide_subsystems
    return serialx.list_serial_ports()


async def is_hgi80(serial_port: SerPortNameT) -> bool | None:
    """Return True if device has attributes of a Honeywell HGI80.

    :param serial_port: Name or URL of the serial port to inspect.
    :type serial_port: SerPortNameT
    :returns: True if HGI80, False if evofw3/FTDI, None if unknown.
    :rtype: bool | None
    :raises exc.TransportSerialError: If port does not exist or URL invalid.
    """
    if serial_port[:7] == "mqtt://":
        return False  # ramses_esp

    if "://" in serial_port:  # e.g. "rfc2217://localhost:5001"
        try:
            serial_for_url(serial_port, do_not_open=True)
        except (SerialException, ValueError) as err:
            from .transport.helpers import redact_url

            raise exc.TransportSerialError(
                f"Unable to find {redact_url(serial_port)}: {err}"
            ) from err
        return None

    loop = asyncio.get_running_loop()
    if not await loop.run_in_executor(None, os.path.exists, serial_port):
        raise exc.TransportSerialError(f"Unable to find {serial_port}")

    if "by-id" in serial_port:
        if "TUSB3410" in serial_port:
            return True
        if any(x in serial_port for x in ("evofw3", "FT232R", "NANO")):
            return False

    try:
        komports = await serialx.async_list_serial_ports()
    except (SerialException, OSError) as err:
        raise exc.TransportSerialError(
            f"Unable to find {serial_port}: {err}"
        ) from err

    vid = {x.device: x.vid for x in komports}.get(serial_port)

    if not vid:
        pass
    elif vid == 0x10AC:  # Honeywell
        return True
    elif vid in (0x0403, 0x1B4F):  # FTDI, SparkFun
        return False

    product = {x.device: x.product for x in komports}.get(serial_port)

    if not product:
        pass
    elif "TUSB3410" in product:
        return True
    elif any(x in product for x in ("evofw3", "FT232R", "NANO")):
        return False

    _LOGGER.warning(
        "%s: the gateway type is not determinable, will assume evofw3%s",
        serial_port,
        (
            ", TIP: specify the serial port by-id (i.e. /dev/serial/by-id/usb-...)"
            if "by-id" not in serial_port
            else ""
        ),
    )
    return None
