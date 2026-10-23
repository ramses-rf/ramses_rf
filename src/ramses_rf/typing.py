"""RAMSES RF - Domain-specific type definitions."""

from datetime import timedelta as td
from typing import Any, NotRequired, TypedDict

from ramses_tx.typing import DeviceIdT as TxDeviceIdT, DevIndexT as TxIndexT

type DeviceIdT = TxDeviceIdT
type DevIndexT = TxIndexT
type IndexT = TxIndexT

type DeviceTraitsT = dict[  # type: ignore[explicit-any]  # heterogeneous traits
    str, Any
]
type DeviceListT = dict[str, DeviceTraitsT]
type PollingIntervalsT = dict[str, int]

# Domain Payload Type Aliases
type HvacPayloadT = dict[  # type: ignore[explicit-any]  # heterogeneous payload
    str, Any
]
type OpenThermPayloadT = dict[  # type: ignore[explicit-any]  # heterogeneous payload
    str, Any
]
type HeatingPayloadT = dict[  # type: ignore[explicit-any]  # heterogeneous payload
    str, Any
]


# For fingerprints.py
class DeviceFingerprint(TypedDict):
    """Device fingerprint metadata for entity identification."""

    slug: str
    dev_type: str
    date: str
    desc: str


# CODES_SCHEMA entries
CodeSchemaEntry = TypedDict(
    "CodeSchemaEntry",
    {
        "name": str,
        " I": str,  # Regex
        "RQ": str,  # Regex
        "RP": str,  # Regex
        " W": str,  # Regex
        "lifespan": bool | td | None,
    },
    total=False,
)


# Schedule domain types
class EmptyDictT(TypedDict):
    """An empty typed dictionary used as a sentinel."""

    pass


class SwitchPointDhw(TypedDict):
    """A dictionary representing a DHW switchpoint."""

    time_of_day: str
    enabled: bool


class SwitchPointZon(TypedDict):
    """A dictionary representing a zone heating switchpoint."""

    time_of_day: str
    heat_setpoint: float


type SwitchPointT = SwitchPointDhw | SwitchPointZon
type SwitchPointsT = list[SwitchPointT]


class DayOfWeek(TypedDict):
    """A dictionary representing a schedule for a single day."""

    day_of_week: int
    switchpoints: SwitchPointsT


type DayOfWeekT = DayOfWeek
type WeeklySchedule = list[DayOfWeek]
type InnerScheduleT = WeeklySchedule


class WeeklyScheduleDict(TypedDict, total=False):
    """A dictionary representing a full schedule payload."""

    zone_index: str
    schedule: WeeklySchedule


class EmptySchedule(TypedDict, total=False):
    """A dictionary representing an empty schedule payload."""

    zone_index: str
    schedule: NotRequired[EmptyDictT | None]


# Legacy aliases for backward compatibility
type OuterSchedule = WeeklyScheduleDict
type OuterScheduleT = WeeklyScheduleDict | EmptySchedule


type PayloadT = dict[  # type: ignore[explicit-any]  # heterogeneous protocol payload
    str, Any
]
type SchemaT = dict[  # type: ignore[explicit-any]  # heterogeneous schema tree
    str, Any
]
type PayloadSetT = list[PayloadT | None]

type FragmentT = str
type FragmentSetT = list[FragmentT]
