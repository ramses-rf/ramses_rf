#!/usr/bin/env python3
"""RAMSES RF - The modular Command package."""

from __future__ import annotations

from collections.abc import Callable

from .base import CommandBase
from .system import SystemMixins


class Command(SystemMixins, CommandBase):
    """The Command class (packets to be transmitted).

    They have QoS and/or callbacks (but no RSSI).
    """


# A convenience dict
CODE_API_MAP: dict[str, Callable[..., Command]] = {}
