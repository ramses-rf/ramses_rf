#!/usr/bin/env python3
"""
Constants and type definitions for the virtual RF network.
"""

from enum import Enum
from typing import Any, Final, NamedTuple

# Shared Constants
MAX_NUM_PORTS: Final = 6


class HardwareProfile(NamedTuple):
    """
    Metadata for a specific hardware gateway profile.

    :param manufacturer: USB manufacturer name.
    :param product: USB product name string.
    :param vid: Vendor ID (e.g., 0x10AC).
    :param pid: Product ID (e.g., 0x0102).
    :param description: Human-readable device description.
    :param serial_number: Unique hardware serial, if any.
    :param interface: Specific interface name.
    :param subsystem: The system subsystem (e.g., 'usb').
    :param dev_path: Default system device path.
    :param dev_by_id: The persistent 'by-id' system path.
    """

    manufacturer: str
    product: str
    vid: int
    pid: int
    #
    description: str
    serial_number: str | None
    interface: str | None
    #
    subsystem: str
    dev_path: str
    dev_by_id: str


class HgiFwTypes(Enum):
    """
    Supported firmware/hardware combinations for gateway emulation.
    """

    EVOFW3 = HardwareProfile(  # 8/16 MHz atmega32u4 (HW Uart)
        manufacturer="SparkFun",
        product="evofw3 atmega32u4",
        vid=0x1B4F,  # aka SparkFun Electronics
        pid=0x9206,
        #
        description="evofw3 atmega32u4",
        serial_number=None,
        interface=None,
        #
        subsystem="usb-serial",
        dev_path="/dev/ttyACM0",  # is not a fixed value
        dev_by_id="/dev/serial/by-id/usb-SparkFun_evofw3_atmega32u4-if00",
    )
    """Standard SparkFun hardware (Atmega32u4), values are from real devices."""

    EVOFW3_FTDI = HardwareProfile(  # 16MHZ atmega328 (SW Uart)
        manufacturer="FTDI",
        product="FT232R USB UART",
        vid=0x0403,  # aka Future Technology Devices International Ltd.
        pid=0x6001,
        description="FT232R USB UART - FT232R USB UART",
        serial_number="A50285BI",
        interface="FT232R USB UART",
        subsystem="usb-serial",
        dev_path="/dev/ttyUSB0",  # is not a fixed value
        dev_by_id="/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0",
    )
    """Alternative hardware using an FTDI chip (Atmega328P), values are from real devices."""

    HGI_80 = HardwareProfile(  # Honeywell HGI80 (partially contrived)
        manufacturer="Texas Instruments",
        product="TUSB3410 Boot Device",
        vid=0x10AC,  # aka Honeywell, Inc.
        pid=0x0102,
        description="TUSB3410 Boot Device",  # contrived
        serial_number="TUSB3410",
        interface=None,  # assumed
        subsystem="usb",
        dev_path="/dev/ttyUSB0",  # is not a fixed value
        dev_by_id="/dev/serial/by-id/usb-Texas_Instruments_TUSB3410_Boot_Device_TUSB3410-if00-port0",
    )
    """Original Honeywell HGI80 hardware, partially contrived values."""


# Schema constants for testing
SCHEMA_1: Final[dict[str, Any]] = {
    "orphans_hvac": ["41:111111"],
    "known_list": {
        "18:111111": {"class": "HGI", "fw_version": "EVOFW3"},
        "41:111111": {"class": "REM"},
    },
}

SCHEMA_2: Final[dict[str, Any]] = {
    "orphans_hvac": ["42:222222"],
    "known_list": {
        "18:222222": {"class": "HGI", "fw_version": "HGI_80"},
        "42:222222": {"class": "FAN"},
    },
}

SCHEMA_3: Final[dict[str, Any]] = {
    "orphans_hvac": ["42:333333"],
    "known_list": {"18:333333": {"class": "HGI"}, "42:333333": {"class": "FAN"}},
}
"""Schema added for specific HVAC functionality testing."""
