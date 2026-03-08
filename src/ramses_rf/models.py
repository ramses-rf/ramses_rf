#!/usr/bin/env python3
"""RAMSES RF - Data models and configuration objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class DeviceTraits:
    """Strictly typed traits for device instantiation."""

    device_class: str | None = None
    alias: str | None = None
    faked: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceTraits:
        """Construct DeviceTraits safely from a dynamically parsed dictionary."""
        return cls(
            device_class=data.get("class"),
            alias=data.get("alias"),
            faked=data.get("faked"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize back to a dictionary.

        Useful for bridging the boundary into legacy methods expecting **kwargs.
        """
        result: dict[str, Any] = {}
        if self.device_class is not None:
            result["class"] = self.device_class
        if self.alias is not None:
            result["alias"] = self.alias
        if self.faked is not None:
            result["faked"] = self.faked
        return result
