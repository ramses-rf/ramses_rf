"""RAMSES RF - Base state models and events."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime as dt

from ramses_rf.enums import TopologyAction
from ramses_rf.typing import DeviceIdT, PayloadT, PollingIntervalsT, SchemaT


@dataclass
class DeviceTraits:
    """Strictly typed traits for device instantiation."""

    device_class: str | None = None
    alias: str | None = None
    faked: bool | None = None
    scheme: str | None = None
    polling_interval: PollingIntervalsT | None = None
    heartbeat_timeout: int | None = None
    is_battery: bool | None = None

    @classmethod
    def from_dict(cls, data: SchemaT) -> DeviceTraits:
        """Construct DeviceTraits safely from a dynamically parsed dictionary.

        :param data: The raw dictionary containing device trait key-values.
        :type data: SchemaT
        :returns: A new DeviceTraits instance populated with trait values.
        :rtype: DeviceTraits
        """
        return cls(
            device_class=data.get("class"),
            alias=data.get("alias"),
            faked=data.get("faked"),
            scheme=data.get("scheme"),
            polling_interval=data.get("polling_interval"),
            heartbeat_timeout=data.get("heartbeat_timeout"),
            is_battery=data.get("is_battery"),
        )

    def to_dict(self) -> SchemaT:
        """Serialize back to a dictionary.

        Useful for bridging the boundary into pre-0.60 methods expecting
        **kwargs.

        :returns: A dictionary representing the device traits.
        :rtype: SchemaT
        """
        result: SchemaT = {}
        if self.device_class is not None:
            result["class"] = getattr(
                self.device_class, "value", self.device_class
            )
        if self.alias is not None:
            result["alias"] = self.alias
        if self.faked is not None:
            result["faked"] = self.faked
        if self.scheme is not None:
            result["scheme"] = getattr(self.scheme, "value", self.scheme)
        if self.polling_interval is not None:
            result["polling_interval"] = self.polling_interval
        if self.heartbeat_timeout is not None:
            result["heartbeat_timeout"] = self.heartbeat_timeout
        if self.is_battery is not None:
            result["is_battery"] = self.is_battery
        return result


def _now_utc() -> dt:
    """Return the current timezone-aware UTC datetime.

    This helper function is required for dataclass default factories.
    If `default=dt.now(UTC)` is used directly in a dataclass field,
    Python evaluates the function exactly once when the module is
    imported. As a result, every event instantiated during the
    runtime would share the exact same timestamp. By providing this
    zero-argument callable to `default_factory`, we ensure a fresh,
    accurate timestamp is generated every time an object is created.
    """
    return dt.now(UTC)


@dataclass(frozen=True, slots=True)
class TopologyChangedEvent:
    """Immutable event for a structural network graph change."""

    # The structural action to perform
    action: TopologyAction

    # -- Entity Identifiers (Populated based on the Action) --

    # Used for single-device actions (e.g., UPDATE_DEVICE_CLASS, UPDATE_TRAITS)
    device_id: DeviceIdT | None = None

    # Used together for structural relationship actions (e.g., BIND_DEVICE)
    parent_id: DeviceIdT | None = None
    child_id: DeviceIdT | None = None

    # -- Context & Observability --

    # Flexible domain-specific metadata (e.g., {"zone_index": "01",
    # "is_sensor": True})
    metadata: dict[str, str | int | float | bool | PayloadT] = field(
        default_factory=dict
    )

    # The Tracing Triad (Observability & Debugging)
    event_id: uuid.UUID = field(default_factory=uuid.uuid4)
    correlation_id: uuid.UUID = field(default_factory=uuid.uuid4)
    # causation identifies the rule/engine that generated this guess
    # (e.g., "Rule_000C")
    causation: str = "TopologyBuilder"

    timestamp: dt = field(default_factory=_now_utc)

    @property
    def is_single_device(self) -> bool:
        """Return True if this event target is a single device.

        :returns: True if device_id is present.
        :rtype: bool
        """
        return self.device_id is not None

    @property
    def is_relationship(self) -> bool:
        """Return True if this event defines a parent-child relationship.

        :returns: True if parent_id or child_id is present.
        :rtype: bool
        """
        return self.parent_id is not None or self.child_id is not None

    @property
    def target_device_id(self) -> DeviceIdT | None:
        """Return primary device ID associated with this topology event.

        :returns: Primary device ID (device_id, child_id, or parent_id).
        :rtype: DeviceIdT | None
        """
        return self.device_id or self.child_id or self.parent_id


# --- CQRS Domain Read-Models and Events ---


@dataclass(frozen=True, slots=True)
class StateUpdatedEvent:
    """An immutable event representing a state update for an entity.

    Includes the OpenTelemetry tracing triad to guarantee perfect
    observability of the event lineage.
    """

    entity_id: str
    state: object
    event_id: uuid.UUID = field(default_factory=uuid.uuid4)
    correlation_id: uuid.UUID = field(default_factory=uuid.uuid4)
    causation_id: uuid.UUID = field(default_factory=uuid.uuid4)
    timestamp: dt = field(default_factory=_now_utc)
