#!/usr/bin/env python3
"""RAMSES RF - Typing for RamsesProtocol & RamsesTransport."""

from collections.abc import Awaitable, Callable
from datetime import datetime as dt
from enum import EnumCheck, StrEnum, verify
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    NewType,
    NotRequired,
    TypedDict,
    TypeVar,
)

from .const import (
    DEFAULT_GAP_DURATION,
    DEFAULT_MAX_RETRIES,
    DEFAULT_NUM_REPEATS,
    DEFAULT_SEND_TIMEOUT,
    FaultDeviceClass,
    FaultState,
    FaultType,
    Priority,
)

if TYPE_CHECKING:
    from .dtos import PacketDTO
    from .packet import Packet


# Core Types
DeviceIdT = NewType("DeviceIdT", str)
DevIndexT = NewType("DevIndexT", str)
SerPortNameT = NewType("SerPortNameT", str)
ExceptionT = TypeVar("ExceptionT", bound=Exception)
HeaderT = NewType("HeaderT", str)
PayloadT = NewType("PayloadT", str)


# Device Traits
class DeviceTraitsT(TypedDict):
    """Schema representing device traits and metadata."""

    alias: str | None
    faked: bool | None
    class_: str | None  # "class" is a reserved keyword


type DeviceListT = dict[DeviceIdT, DeviceTraitsT]


if TYPE_CHECKING:
    from .interfaces import ProtocolInterface

    MsgFilterT = Callable[["PacketDTO"], bool]
    MsgHandlerT = Callable[["PacketDTO"], Awaitable[None]]
    type RamsesProtocolT = ProtocolInterface
else:
    MsgFilterT = Callable[[Any], bool]
    MsgHandlerT = Callable[[Any], Awaitable[None]]
    RamsesProtocolT = Any


# QoS & Send Parameters
class QosParams:
    """A container for QoS attributes and state."""

    def __init__(
        self,
        *,
        max_retries: int | None = DEFAULT_MAX_RETRIES,
        timeout: float | None = DEFAULT_SEND_TIMEOUT,
    ) -> None:
        """Create a QosParams instance."""
        if max_retries is None:
            self._max_retries = DEFAULT_MAX_RETRIES
        else:
            self._max_retries = max_retries

        self._timeout = timeout or DEFAULT_SEND_TIMEOUT

        self._echo_packet: Packet | None = None
        self._rply_packet: Packet | None = None

        self._dt_cmd_sent: dt | None = None
        self._dt_echo_rcvd: dt | None = None
        self._dt_rply_rcvd: dt | None = None

    @property
    def max_retries(self) -> int:
        """Return the maximum retry count."""
        return self._max_retries

    @property
    def timeout(self) -> float:
        """Return the QoS timeout duration in seconds."""
        return self._timeout


class SendParams:
    """A container for Send attributes and state."""

    def __init__(
        self,
        *,
        gap_duration: float | None = DEFAULT_GAP_DURATION,
        num_repeats: int | None = DEFAULT_NUM_REPEATS,
        priority: Priority | None = Priority.DEFAULT,
    ) -> None:
        """Create a SendParams instance."""
        self._gap_duration = gap_duration or DEFAULT_GAP_DURATION
        self._num_repeats = num_repeats or DEFAULT_NUM_REPEATS
        self._priority = priority or Priority.DEFAULT

        self._dt_cmd_arrived: dt | None = None
        self._dt_cmd_queued: dt | None = None
        self._dt_cmd_sent: dt | None = None

    @property
    def gap_duration(self) -> float:
        """Return the gap duration between transmissions in seconds."""
        return self._gap_duration

    @property
    def num_repeats(self) -> int:
        """Return the repeat count for transmission."""
        return self._num_repeats

    @property
    def priority(self) -> Priority:
        """Return the transmission priority."""
        return self._priority


# TypedDicts (formerly in typed_dicts.py)
type _HexToTempT = float | None


LogIdxT = Literal[
    "00",
    "01",
    "02",
    "03",
    "04",
    "05",
    "06",
    "07",
    "08",
    "09",
    "0A",
    "0B",
    "0C",
    "0D",
    "0E",
    "0F",
    "10",
    "11",
    "12",
    "13",
    "14",
    "15",
    "16",
    "17",
    "18",
    "19",
    "1A",
    "1B",
    "1C",
    "1D",
    "1E",
    "1F",
    "20",
    "21",
    "22",
    "23",
    "24",
    "25",
    "26",
    "27",
    "28",
    "29",
    "2A",
    "2B",
    "2C",
    "2D",
    "2E",
    "2F",
    "30",
    "31",
    "32",
    "33",
    "34",
    "35",
    "36",
    "37",
    "38",
    "39",
    "3A",
    "3B",
    "3C",
    "3D",
    "3E",
    "3F",
]


class _FlowRate(TypedDict):
    dhw_flow_rate: _HexToTempT


class _Pressure(TypedDict):
    pressure: _HexToTempT


class _Setpoint(TypedDict):
    setpoint: _HexToTempT


class _Temperature(TypedDict):
    temperature: _HexToTempT


class FaultLogEntryNull(TypedDict):
    """Empty fault log entry payload schema."""

    _log_index: LogIdxT


class FaultLogEntry(TypedDict):
    """Fault log entry payload schema."""

    _log_index: LogIdxT
    timestamp: str
    fault_state: FaultState
    fault_type: FaultType
    domain_index: str
    device_class: FaultDeviceClass
    device_id: DeviceIdT | None
    _unknown_3: str
    _unknown_7: str
    _unknown_15: str


class AirQuality(TypedDict):
    """Air quality measurement payload schema."""

    air_quality: float | None
    air_quality_basis: NotRequired[str]


class Co2Level(TypedDict):
    """CO2 level measurement payload schema."""

    co2_level: float | None


class RelativeHumidity(TypedDict):
    """Relative humidity measurement payload schema."""

    relative_humidity: _HexToTempT
    temperature: NotRequired[float | None]
    dewpoint_temp: NotRequired[float | None]


class IndoorHumidity(TypedDict):
    """Indoor relative humidity measurement payload schema."""

    indoor_humidity: _HexToTempT
    temperature: NotRequired[float | None]
    dewpoint_temp: NotRequired[float | None]


class OutdoorHumidity(TypedDict):
    """Outdoor relative humidity measurement payload schema."""

    outdoor_humidity: _HexToTempT
    temperature: NotRequired[float | None]
    dewpoint_temp: NotRequired[float | None]


class ExhaustTemp(TypedDict):
    """Exhaust air temperature payload schema."""

    exhaust_temp: _HexToTempT


class SupplyTemp(TypedDict):
    """Supply air temperature payload schema."""

    supply_temp: _HexToTempT


class IndoorTemp(TypedDict):
    """Indoor air temperature payload schema."""

    indoor_temp: _HexToTempT


class OutdoorTemp(TypedDict):
    """Outdoor air temperature payload schema."""

    outdoor_temp: _HexToTempT


class Capabilities(TypedDict):
    """Fan speed capabilities payload schema."""

    speed_capabilities: list[str] | None


class BypassPosition(TypedDict):
    """Ventilator bypass damper position payload schema."""

    bypass_position: float | None


class FanInfo(TypedDict):
    """Ventilator status information payload schema."""

    fan_info: str | None
    _unknown_fan_info_flags: list[int]


class ExhaustFanSpeed(TypedDict):
    """Exhaust fan speed payload schema."""

    exhaust_fan: float | None


class SupplyFanSpeed(TypedDict):
    """Supply fan speed payload schema."""

    supply_fan: float | None


class RemainingMins(TypedDict):
    """Remaining boost minutes payload schema."""

    remaining_mins: int | None


class PostHeater(TypedDict):
    """Post-heater modulation level payload schema."""

    post_heater: float | None


class PreHeater(TypedDict):
    """Pre-heater modulation level payload schema."""

    pre_heater: float | None


class SupplyFlow(TypedDict):
    """Supply air flow rate payload schema."""

    supply_flow: float | None


class ExhaustFlow(TypedDict):
    """Exhaust air flow rate payload schema."""

    exhaust_flow: float | None


class _VentilationState(
    ExhaustFanSpeed,
    FanInfo,
    AirQuality,
    Co2Level,
    ExhaustTemp,
    SupplyTemp,
    IndoorTemp,
    OutdoorTemp,
    Capabilities,
    BypassPosition,
    SupplyFanSpeed,
    RemainingMins,
    PostHeater,
    PreHeater,
    SupplyFlow,
    ExhaustFlow,
):
    indoor_humidity: _HexToTempT
    outdoor_humidity: _HexToTempT
    extra: NotRequired[str | None]


class _empty(TypedDict):
    pass


class _0004(TypedDict):
    zone_index: NotRequired[str | None]
    name: NotRequired[str | None]


class _0006(TypedDict):
    change_counter: NotRequired[int | None]


class _0008(TypedDict):
    relay_demand: float | None


class _000a(TypedDict):
    zone_index: NotRequired[str]
    min_temp: float | None
    max_temp: float | None
    local_override: bool
    openwindow_function: bool
    multiroom_mode: bool
    _unknown_bitmap: str


class _0100(TypedDict):
    language: str
    _unknown_0: str


class _0404(TypedDict):
    frag_number: int
    total_frags: int | None
    frag_length: NotRequired[int | None]
    fragment: NotRequired[str]


class _0418_NULL(TypedDict):
    log_index: NotRequired[LogIdxT]
    log_entry: None


class _0418(TypedDict):
    log_index: LogIdxT
    log_entry: tuple[str, ...]


class _1060(TypedDict):
    battery_low: bool
    battery_level: float | None


class _1030(TypedDict):
    max_flow_setpoint: float
    min_flow_setpoint: float
    valve_run_time: int
    pump_run_time: int
    boolean_cc: bool


class _1090(TypedDict):
    temperature_0: float | None
    temperature_1: float | None


class _10a0(TypedDict):
    setpoint: _HexToTempT | None
    overrun: NotRequired[int]
    differential: NotRequired[_HexToTempT]


class _10d0(TypedDict):
    days_remaining: int | None
    days_lifetime: NotRequired[int | None]
    percent_remaining: NotRequired[float | None]


class _10e1(TypedDict):
    device_id: DeviceIdT


class _1100(TypedDict):
    domain_id: NotRequired[str]
    cycle_rate: int
    min_on_time: float
    min_off_time: float
    _unknown_0: NotRequired[str]
    proportional_band_width: NotRequired[float | None]
    _unknown_1: NotRequired[str | None]


class _1100_INDEX(TypedDict):
    domain_id: str


class _12a0(TypedDict):
    hvac_index: str
    indoor_humidity: NotRequired[_HexToTempT | None]
    outdoor_humidity: NotRequired[_HexToTempT | None]
    relative_humidity: NotRequired[_HexToTempT | None]
    temperature: NotRequired[float | None]
    dewpoint_temp: NotRequired[float | None]
    _unknown_12: NotRequired[str]


class _12b0(TypedDict):
    window_open: bool | None


class _12c0(TypedDict):
    temperature: float | None
    units: Literal["Fahrenheit", "Celsius"]
    _unknown_6: NotRequired[str]


class _1f09(TypedDict):
    remaining_seconds: float
    _next_sync: str


class _1f41(TypedDict):
    mode: str
    active: NotRequired[bool | None]
    until: NotRequired[str | None]


class _1fd4(TypedDict):
    ticker: int


@verify(EnumCheck.UNIQUE)
class _BindPhase(StrEnum):
    OFFER = "offer"
    ACCEPT = "accept"
    CONFIRM = "confirm"


class _1fc9(TypedDict):
    phase: str | None
    bindings: list[list[str]]


class _22b0(TypedDict):
    enabled: bool


class _22f4(TypedDict):
    fan_mode: str | None
    fan_rate: str | None


class _2309(TypedDict):
    zone_index: NotRequired[str]
    setpoint: float | None


@verify(EnumCheck.UNIQUE)
class _ZoneMode(StrEnum):
    FOLLOW = "follow_schedule"
    ADVANCED = "advanced_override"
    PERMANENT = "permanent_override"
    COUNTDOWN = "countdown_override"
    TEMPORARY = "temporary_override"


class _2349(TypedDict):
    mode: _ZoneMode
    setpoint: float | None
    duration: NotRequired[int | None]
    until: NotRequired[str | None]


class _2d49(TypedDict):
    state: bool | None


class _2e04(TypedDict):
    system_mode: str
    until: NotRequired[str | None]


class _3110(TypedDict):
    mode: str
    demand: NotRequired[float | None]


class _313f(TypedDict):
    datetime: str | None
    is_dst: bool | None
    _unknown_0: str


class _3220(TypedDict):
    msg_id: int
    msg_type: str
    msg_name: str
    description: str


class _3222(TypedDict):
    start: int | None
    length: int
    data: NotRequired[str]


class _3b00(TypedDict):
    domain_id: NotRequired[Literal["FC"]]
    actuator_sync: bool | None


class _3ef0_3(TypedDict):
    modulation_level: float | None
    _flags_2: str


class _3ef0_6(_3ef0_3):
    _flags_3: list[int]
    ch_active: bool
    dhw_active: bool
    cool_active: bool
    flame_on: bool
    _unknown_4: str
    _unknown_5: str


class _3ef0_9(_3ef0_6):
    _flags_6: list[int]
    ch_enabled: bool
    ch_setpoint: int
    max_rel_modulation: float


class _3ef1(TypedDict):
    modulation_level: float | None
    actuator_countdown: int | None
    cycle_countdown: int | None
    _unknown_0: str


class _JASPER(TypedDict):
    ordinal: str
    blob: str


class PayDictT:
    """Payload dict types."""

    type EMPTY = _empty

    type _0004 = _0004
    type _0006 = _0006
    type _0008 = _0008
    type _000A = _000a
    type _0100 = _0100
    type _0404 = _0404
    type _0418 = _0418
    type _0418_NULL = _0418_NULL
    type _1030 = _1030
    type _1060 = _1060
    type _1081 = _Setpoint
    type _1090 = _1090
    type _10A0 = _10a0
    type _10D0 = _10d0
    type _10E1 = _10e1
    type _1100 = _1100
    type _1100_INDEX = _1100_INDEX
    type _1260 = _Temperature
    type _1280 = OutdoorHumidity
    type _1290 = OutdoorTemp
    type _1298 = Co2Level
    type _12A0 = _12a0
    type _12B0 = _12b0
    type _12C0 = _12c0
    type _12C8 = AirQuality
    type _12F0 = _FlowRate
    type _1300 = _Pressure
    type _1F09 = _1f09
    type _1F41 = _1f41
    type _1FC9 = _1fc9
    type _1FD4 = _1fd4
    type _22B0 = _22b0
    type _22F4 = _22f4
    type _2309 = _2309
    type _2349 = _2349
    type _22D9 = _Setpoint
    type _2D49 = _2d49
    type _2E04 = _2e04
    type _3110 = _3110
    type _313F = _313f
    type _31DA = _VentilationState
    type _3200 = _Temperature
    type _3210 = _Temperature
    type _3B00 = _3b00
    type _3EF0 = _3ef0_3 | _3ef0_6 | _3ef0_9
    type _3EF1 = _3ef1

    type _JASPER = _JASPER

    type FAULT_LOG_ENTRY = FaultLogEntry
    type FAULT_LOG_ENTRY_NULL = FaultLogEntryNull
    type TEMPERATURE = _Temperature

    type RELATIVE_HUMIDITY = RelativeHumidity

    type AIR_QUALITY = AirQuality
    type CO2_LEVEL = Co2Level
    type EXHAUST_TEMP = ExhaustTemp
    type SUPPLY_TEMP = SupplyTemp
    type INDOOR_HUMIDITY = IndoorHumidity
    type OUTDOOR_HUMIDITY = OutdoorHumidity
    type INDOOR_TEMP = IndoorTemp
    type OUTDOOR_TEMP = OutdoorTemp
    type CAPABILITIES = Capabilities
    type BYPASS_POSITION = BypassPosition
    type FAN_INFO = FanInfo
    type EXHAUST_FAN_SPEED = ExhaustFanSpeed
    type SUPPLY_FAN_SPEED = SupplyFanSpeed
    type REMAINING_MINUTES = RemainingMins
    type POST_HEATER = PostHeater
    type PRE_HEATER = PreHeater
    type SUPPLY_FLOW = SupplyFlow
    type EXHAUST_FLOW = ExhaustFlow


class PortConfigT(TypedDict):
    """Serial port communication configuration schema."""

    baudrate: int  # 57600, 115200
    dsrdtr: bool
    rtscts: bool
    timeout: int
    xonxoff: bool


class PktLogConfigT(TypedDict):
    """Packet logger configuration schema."""

    packet_log_path: str
    packet_log_prefix: str
    packet_log_retention_days: int | None
    rotate_bytes: int | None
    buffer_capacity: NotRequired[int]
    flush_level: NotRequired[int | str]
    flush_interval: NotRequired[float | int]
