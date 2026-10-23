#!/usr/bin/env python3
"""RAMSES RF - The strict L2 MAC address module."""

from __future__ import annotations

from functools import lru_cache
from typing import Final, cast

from . import exceptions as exc
from .const import DEVICE_ID_REGEX, HGI_PREFIX
from .typing import DeviceIdT

HGI_DEVICE_ID: DeviceIdT = cast(DeviceIdT, "18:000730")
NON_DEVICE_ID: DeviceIdT = cast(DeviceIdT, "--:------")
ALL_DEVICE_ID: DeviceIdT = cast(DeviceIdT, "63:262142")

# NOTE: All debug flags should be False for deployment to end-users
_DBG_DISABLE_STRICT_CHECKING: Final[bool] = False
_DBG_DISABLE_DEV_HVAC = False


class Address:
    """The device Address class."""

    # Limit instance memory overhead and accelerate attribute access for
    # high-volume objects
    __slots__ = ("id", "type", "_hex_id")

    _SLUG = None

    def __init__(self, device_id: DeviceIdT) -> None:
        """Create an address from a valid device ID.

        :param device_id: The RAMSES II device ID (e.g., '01:123456')
        :type device_id: DeviceIdT
        :raises ValueError: If the device_id is not a valid format.
        """
        self.id = device_id
        self.type = device_id[:2]  # dex, drops 2nd part, incl. ":"
        self._hex_id: str | None = None

        if not self.is_valid(device_id):
            raise ValueError(f"Invalid device_id: {device_id}")

    def __repr__(self) -> str:
        """Return an unambiguous string representation."""
        return str(self.id)

    def __str__(self) -> str:
        """Return a human-readable string representation."""
        return self.id

    def __eq__(self, other: object) -> bool:
        """Return True if both addresses are equal."""
        if not hasattr(other, "id"):  # can compare Address with Device
            return NotImplemented
        other_id: str = other.id
        result: bool = self.id == other_id
        return result

    @property
    def hex_id(self) -> str:
        """Return 6-character hex representation of device ID."""
        if self._hex_id is not None:
            return self._hex_id
        self._hex_id = self.convert_to_hex(self.id)
        return self._hex_id

    @staticmethod
    def is_valid(value: str) -> bool:
        """Return True if value is a valid device ID string.

        :param value: The device ID string to validate.
        :type value: str
        :returns: True if valid, False otherwise.
        :rtype: bool
        """
        return isinstance(value, str) and (
            value == NON_DEVICE_ID
            or DEVICE_ID_REGEX.ANY.match(value) is not None
        )

    @classmethod
    @lru_cache(maxsize=256)
    def convert_from_hex(cls, device_hex: str) -> str:
        """Convert a 6-character hex string to a device ID.

        :param device_hex: The hex string to convert (e.g., '06368E')
        :type device_hex: str
        :return: The formatted device ID string
        :rtype: str
        """
        if device_hex == "FFFFFE":  # aka '63:262142'
            return ALL_DEVICE_ID

        if not device_hex.strip():  # aka '--:------'
            return NON_DEVICE_ID

        _tmp = int(device_hex, 16)
        return cast(
            DeviceIdT, f"{(_tmp & 0xFC0000) >> 18:02d}:{_tmp & 0x03FFFF:06d}"
        )

    @classmethod
    @lru_cache(maxsize=256)
    def convert_to_hex(cls, device_id: DeviceIdT) -> str:
        """Convert '01:145038' to '06368E'."""
        if not cls.is_valid(device_id):
            raise TypeError

        dev_type = device_id[:2]
        return f"{(int(dev_type) << 18) + int(device_id[-6:]):0>6X}"


@lru_cache(maxsize=2048)
def id_to_address(device_id: DeviceIdT) -> Address:
    """Return cached Address instance for a device ID."""
    return Address(device_id=device_id)


HGI_DEV_ADDR = Address(HGI_DEVICE_ID)  # 18:000730
NON_DEV_ADDR = Address(NON_DEVICE_ID)  # --:------
ALL_DEV_ADDR = Address(ALL_DEVICE_ID)  # 63:262142


def dev_id_to_hex_id(device_id: DeviceIdT) -> str:
    """Convert '01:145038' to '06368E'."""
    dev_type = device_id[:2]
    return f"{(int(dev_type) << 18) + int(device_id[-6:]):0>6X}"


def hex_id_to_dev_id(device_hex: str) -> DeviceIdT:
    """Convert '06368E' to '01:145038'."""
    if device_hex == "FFFFFE":  # aka '63:262142'
        return ALL_DEVICE_ID

    if not device_hex.strip():  # aka '--:------'
        return NON_DEVICE_ID

    _tmp = int(device_hex, 16)
    dev_type = f"{(_tmp & 0xFC0000) >> 18:02d}"
    return cast(DeviceIdT, f"{dev_type}:{_tmp & 0x03FFFF:06d}")


@lru_cache(maxsize=2048)
def is_valid_dev_id(value: str, device_class: None | str = None) -> bool:
    """Return True if a device_id is valid."""
    return (
        isinstance(value, str) and DEVICE_ID_REGEX.ANY.match(value) is not None
    )


def is_hgi_id(device_id: str) -> bool:
    """Return True if the device_id is an HGI gateway interface (``18:`` prefix).

    HGI gateways are RF receivers, not remote devices — downstream packages
    (e.g. ramses_cc discovery) use this instead of hardcoding the prefix.

    :param device_id: The device ID to check (e.g. '18:000730').
    :return: True if the id is an HGI device id.
    """
    return isinstance(device_id, str) and device_id.startswith(HGI_PREFIX)


@lru_cache(maxsize=2048)
def packet_addrs(
    address_fragment: str,
) -> tuple[Address, Address, Address, Address, Address]:
    """Parse address fields from a 30-character address fragment.

    :param address_fragment: The 30-char fragment
    :type address_fragment: str
    :return: A tuple of (src_addr, dst_addr, addr_0, addr_1, addr_2)
    :rtype: tuple[Address, Address, Address, Address, Address]
    :raises PacketAddrSetInvalid: If the address fields are not valid.
    """
    try:
        addrs = tuple(
            id_to_address(address_fragment[i : i + 9])
            for i in range(0, 30, 10)
        )
    except ValueError as err:
        raise exc.PacketAddrSetInvalid(
            f"Invalid address set: {address_fragment}: {err}"
        ) from None

    if not _DBG_DISABLE_STRICT_CHECKING and (
        not (
            addrs[0] not in (NON_DEV_ADDR, ALL_DEV_ADDR)
            and addrs[1] == NON_DEV_ADDR
            and addrs[2] != NON_DEV_ADDR
        )
        and not (
            addrs[0] not in (NON_DEV_ADDR, ALL_DEV_ADDR)
            and addrs[1] not in (NON_DEV_ADDR, addrs[0])
            and addrs[2] == NON_DEV_ADDR
        )
        and not (
            addrs[2] not in (NON_DEV_ADDR, ALL_DEV_ADDR)
            and addrs[0] == NON_DEV_ADDR
            and addrs[1] == NON_DEV_ADDR
        )
    ):
        raise exc.PacketAddrSetInvalid(
            f"Invalid address set: {address_fragment}"
        )

    device_addrs = list(filter(lambda a: a.type != "--", addrs))  # dex
    src_addr = device_addrs[0]
    dst_addr = device_addrs[1] if len(device_addrs) > 1 else NON_DEV_ADDR

    if src_addr.id == dst_addr.id:  # incl. HGI_DEV_ADDR == HGI_DEV_ADDR
        src_addr = dst_addr

    return src_addr, dst_addr, addrs[0], addrs[1], addrs[2]
