#!/usr/bin/env python3
"""RAMSES RF - System-wide command constructors."""

from __future__ import annotations

from typing import TypeVar

from ..address import ALL_DEV_ADDR
from ..const import I_, LOOKUP_PUZZ, Code
from ..helpers import hex_from_str, timestamp
from ..typing import PayloadT
from ..version import VERSION
from .base import CommandBase

_T = TypeVar("_T", bound="SystemMixins")


class SystemMixins(CommandBase):
    """Mixins for System-wide commands."""

    @classmethod
    def _puzzle(cls: type[_T], msg_type: str | None = None, message: str = "") -> _T:
        if msg_type is None:
            msg_type = "12" if message else "10"

        assert msg_type in LOOKUP_PUZZ, f"Invalid/deprecated Puzzle type: {msg_type}"

        payload = f"00{msg_type}"

        if int(msg_type, 16) >= int("20", 16):
            payload += f"{int(timestamp() * 1e7):012X}"
        elif msg_type != "13":
            payload += f"{int(timestamp() * 1000):012X}"

        if msg_type == "10":
            payload += hex_from_str(f"v{VERSION}")
        elif msg_type == "11":
            payload += hex_from_str(message[:4] + message[5:7] + message[8:])
        else:
            payload += hex_from_str(message)

        return cls.from_attrs(I_, ALL_DEV_ADDR.id, Code._PUZZ, PayloadT(payload[:48]))
