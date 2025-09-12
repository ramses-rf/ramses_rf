#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

This module provides the `Command` class for constructing and managing RAMSES-II protocol
commands (packets) that are to be sent to HVAC devices. It includes methods for creating
commands to control various aspects of the heating system including zones, DHW, and fan controls.

"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from datetime import datetime as dt, timedelta as td
from typing import TYPE_CHECKING, Any, TypeVar

from . import exceptions as exc
from .address import (
    ALL_DEV_ADDR,
    HGI_DEV_ADDR,
    NON_DEV_ADDR,
    Address,
    dev_id_to_hex_id,
    pkt_addrs,
)
from .const import (
    DEV_TYPE_MAP,
    DEVICE_ID_REGEX,
    FAULT_DEVICE_CLASS,
    FAULT_STATE,
    FAULT_TYPE,
    SYS_MODE_MAP,
    SZ_DHW_IDX,
    SZ_MAX_RETRIES,
    SZ_PRIORITY,
    SZ_TIMEOUT,
    ZON_MODE_MAP,
    FaultDeviceClass,
    FaultState,
    FaultType,
    Priority,
)
from .frame import Frame, pkt_header
from .helpers import (
    air_quality_code,
    capability_bits,
    fan_info_flags,
    fan_info_to_byte,
    hex_from_bool,
    hex_from_double,
    hex_from_dtm,
    hex_from_dts,
    hex_from_percent,
    hex_from_str,
    hex_from_temp,
    timestamp,
)
from .opentherm import parity
from .parsers import LOOKUP_PUZZ
from .ramses import (
    _2411_PARAMS_SCHEMA,
    SZ_DATA_TYPE,
    SZ_MAX_VALUE,
    SZ_MIN_VALUE,
    SZ_PRECISION,
)
from .version import VERSION

from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)
from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    F9,
    FA,
    FC,
    FF,
)


if TYPE_CHECKING:
    from .const import VerbT
    from .frame import HeaderT, PayloadT
    from .schemas import DeviceIdT


COMMAND_FORMAT = "{:<2} {} {} {} {} {} {:03d} {}"


DEV_MODE = False

_LOGGER = logging.getLogger(__name__)
if DEV_MODE:
    _LOGGER.setLevel(logging.DEBUG)


_ZoneIdxT = TypeVar("_ZoneIdxT", int, str)


class Qos:
    """The QoS class - this is a mess - it is the first step in cleaning up QoS."""

    # TODO: this needs work

    POLL_INTERVAL = 0.002

    TX_PRIORITY_DEFAULT = Priority.DEFAULT

    # tx (from sent to gwy, to get back from gwy) seems to takes approx. 0.025s
    TX_RETRIES_DEFAULT = 2
    TX_RETRIES_MAX = 5
    TX_TIMEOUT_DEFAULT = td(seconds=0.2)  # 0.20 OK, but too high?

    RX_TIMEOUT_DEFAULT = td(seconds=0.50)  # 0.20 seems OK, 0.10 too low sometimes

    TX_BACKOFFS_MAX = 2  # i.e. tx_timeout 2 ** MAX_BACKOFF

    QOS_KEYS = (SZ_PRIORITY, SZ_MAX_RETRIES, SZ_TIMEOUT)
    # priority, max_retries, rx_timeout, backoff
    DEFAULT_QOS = (Priority.DEFAULT, TX_RETRIES_DEFAULT, TX_TIMEOUT_DEFAULT, True)
    DEFAULT_QOS_TABLE = {
        f"{RQ}|{Code._0016}": (Priority.HIGH, 5, None, True),
        f"{RQ}|{Code._0006}": (Priority.HIGH, 5, None, True),
        f"{I_}|{Code._0404}": (Priority.HIGH, 3, td(seconds=0.30), True),
        f"{RQ}|{Code._0404}": (Priority.HIGH, 3, td(seconds=1.00), True),
        f"{W_}|{Code._0404}": (Priority.HIGH, 3, td(seconds=1.00), True),
        f"{RQ}|{Code._0418}": (Priority.LOW, 3, None, None),
        f"{RQ}|{Code._1F09}": (Priority.HIGH, 5, None, True),
        f"{I_}|{Code._1FC9}": (Priority.HIGH, 2, td(seconds=1), False),
        f"{RQ}|{Code._3220}": (Priority.DEFAULT, 1, td(seconds=1.2), False),
        f"{W_}|{Code._3220}": (Priority.HIGH, 3, td(seconds=1.2), False),
    }  # The long timeout for the OTB is for total RTT to slave (boiler)

    def __init__(
        self,
        *,
        priority: Priority | None = None,  # TODO: deprecate
        max_retries: int | None = None,  # TODO:   deprecate
        timeout: td | None = None,  # TODO:        deprecate
        backoff: bool | None = None,  # TODO:      deprecate
    ) -> None:
        self.priority = self.DEFAULT_QOS[0] if priority is None else priority
        self.retry_limit = self.DEFAULT_QOS[1] if max_retries is None else max_retries
        self.tx_timeout = self.TX_TIMEOUT_DEFAULT
        self.rx_timeout = self.DEFAULT_QOS[2] if timeout is None else timeout
        self.disable_backoff = not (self.DEFAULT_QOS[3] if backoff is None else backoff)

        self.retry_limit = min(self.retry_limit, Qos.TX_RETRIES_MAX)

    @classmethod  # constructor from verb|code pair
    def verb_code(cls, verb: VerbT, code: str | Code, **kwargs: Any) -> Qos:
        """Constructor to create a QoS based upon the defaults for a verb|code pair."""

        default_qos = cls.DEFAULT_QOS_TABLE.get(f"{verb}|{code}", cls.DEFAULT_QOS)
        return cls(
            **{k: kwargs.get(k, default_qos[i]) for i, k in enumerate(cls.QOS_KEYS)}
        )


def _check_idx(zone_idx: int | str) -> str:
    """Validate and normalize a zone index or DHW index.

    This helper function validates that a zone index is within the valid range
    and converts it to a consistent string format.

    :param zone_idx: The zone index to validate. Can be:
        - int: 0-15 for zones, 0xFA for DHW
        - str: String representation of the index (hex or 'HW' for DHW)
    :type zone_idx: int | str
    :return: The normalized zone index as a 2-character hex string
    :rtype: str
    :raises CommandInvalid: If the zone index is invalid

    .. note::
        - For DHW (Domestic Hot Water), use 0xFA or 'HW'
        - For zones, use 0-15 (or '00'-'0F' as hex strings)
    """
    # if zone_idx is None:
    #     return "00"
    if not isinstance(zone_idx, int | str):
        raise exc.CommandInvalid(f"Invalid value for zone_idx: {zone_idx}")
    if isinstance(zone_idx, str):
        zone_idx = FA if zone_idx == "HW" else zone_idx
    result: int = zone_idx if isinstance(zone_idx, int) else int(zone_idx, 16)
    if 0 > result > 15 and result != 0xFA:
        raise exc.CommandInvalid(f"Invalid value for zone_idx: {result}")
    return f"{result:02X}"


def _normalise_mode(
    mode: int | str | None,
    target: bool | float | None,
    until: dt | str | None,
    duration: int | None,
) -> str:
    """Validate and normalize a heating mode for zone or DHW control.

    This helper function ensures the operating mode is valid and consistent
    with the provided target and timing parameters.

    :param mode: The operating mode. Can be:
        - None: Auto-determined from other parameters
        - int/str: Mode code (see ZON_MODE_MAP for valid values)
    :type mode: int | str | None
    :param target: The target value for the mode:
        - For zone modes: The temperature setpoint
        - For DHW modes: Active state (True/False)
    :type target: bool | float | None
    :param until: The end time for temporary modes
    :type until: datetime | str | None
    :param duration: The duration in minutes for countdown modes
    :type duration: int | None
    :return: Normalized 2-character hex mode string
    :rtype: str
    :raises CommandInvalid: If the parameters are inconsistent or invalid

    .. note::
        - If mode is None, it will be determined based on other parameters:
            - If until is set: TEMPORARY mode
            - If duration is set: COUNTDOWN mode
            - Otherwise: PERMANENT mode
        - The target parameter must be provided for all modes except FOLLOW
    """

    if mode is None and target is None:
        raise exc.CommandInvalid(
            "Invalid args: One of mode or setpoint/active can't be None"
        )
    if until and duration:
        raise exc.CommandInvalid(
            "Invalid args: At least one of until or duration must be None"
        )

    if mode is None:
        if until:
            mode = ZON_MODE_MAP.TEMPORARY
        elif duration:
            mode = ZON_MODE_MAP.COUNTDOWN
        else:
            mode = ZON_MODE_MAP.PERMANENT  # TODO: advanced_override?
    elif isinstance(mode, int):
        mode = f"{mode:02X}"
    if mode not in ZON_MODE_MAP:
        mode = ZON_MODE_MAP._hex(mode)  # type: ignore[arg-type]  # may raise KeyError

    assert isinstance(mode, str)  # mypy check

    if mode != ZON_MODE_MAP.FOLLOW and target is None:
        raise exc.CommandInvalid(
            f"Invalid args: For {ZON_MODE_MAP[mode]}, setpoint/active can't be None"
        )

    return mode


def _normalise_until(
    mode: int | str | None,
    _: Any,
    until: dt | str | None,
    duration: int | None,
) -> tuple[Any, Any]:
    """Validate and normalize timing parameters for zone/DHW mode changes.

    This helper function ensures that the timing parameters (until/duration)
    are consistent with the specified mode.

    :param mode: The operating mode (from ZON_MODE_MAP)
    :type mode: int | str | None
    :param _: Unused parameter (kept for compatibility with call signatures)
    :type _: Any
    :param until: The end time for temporary modes
    :type until: datetime | str | None
    :param duration: The duration in minutes for countdown modes
    :type duration: int | None
    :return: A tuple of (until, duration) with validated values
    :rtype: tuple[Any, Any]
    :raises CommandInvalid: If the timing parameters are inconsistent with the mode

    .. note::
        - For TEMPORARY mode: 'until' must be provided, 'duration' must be None
        - For COUNTDOWN mode: 'duration' must be provided, 'until' must be None
        - For other modes: Both 'until' and 'duration' must be None
        - If mode is TEMPORARY and until is None, it will be changed to ADVANCED mode
    """
    if mode == ZON_MODE_MAP.TEMPORARY:
        if duration is not None:
            raise exc.CommandInvalid(
                f"Invalid args: For mode={mode}, duration must be None"
            )
        if until is None:
            mode = ZON_MODE_MAP.ADVANCED  # or: until = dt.now() + td(hour=1)

    elif mode in ZON_MODE_MAP.COUNTDOWN:
        if duration is None:
            raise exc.CommandInvalid(
                f"Invalid args: For mode={mode}, duration can't be None"
            )
        if until is not None:
            raise exc.CommandInvalid(
                f"Invalid args: For mode={mode}, until must be None"
            )

    elif until is not None or duration is not None:
        raise exc.CommandInvalid(
            f"Invalid args: For mode={mode}, until and duration must both be None"
        )

    return until, duration  # TODO return updated mode for ZON_MODE_MAP.TEMPORARY ?


class Command(Frame):
    """The Command class (packets to be transmitted).

    They have QoS and/or callbacks (but no RSSI).
    """

    def __init__(self, frame: str) -> None:
        """Create a command from a string (and its meta-attrs)."""

        try:
            super().__init__(frame)
        except exc.PacketInvalid as err:
            raise exc.CommandInvalid(err.message) from err

        try:
            self._validate(strict_checking=False)
        except exc.PacketInvalid as err:
            raise exc.CommandInvalid(err.message) from err

        try:
            self._validate(strict_checking=True)
        except exc.PacketInvalid as err:
            _LOGGER.warning(f"{self} < Command is potentially invalid: {err}")

        self._rx_header: str | None = None
        # self._source_entity: Entity | None = None  # TODO: is needed?

    @classmethod  # convenience constructor
    def from_attrs(
        cls,
        verb: VerbT,
        dest_id: DeviceIdT | str,
        code: Code,
        payload: PayloadT,
        *,
        from_id: DeviceIdT | str | None = None,
        seqn: int | str | None = None,
    ) -> Command:
        """Create a command from its attrs using a destination device_id."""

        from_id = from_id or HGI_DEV_ADDR.id

        addrs: tuple[DeviceIdT | str, DeviceIdT | str, DeviceIdT | str]

        # if dest_id == NUL_DEV_ADDR.id:
        #     addrs = (from_id, dest_id, NON_DEV_ADDR.id)
        if dest_id == from_id:
            addrs = (from_id, NON_DEV_ADDR.id, dest_id)
        else:
            addrs = (from_id, dest_id, NON_DEV_ADDR.id)

        return cls._from_attrs(
            verb,
            code,
            payload,
            addr0=addrs[0],
            addr1=addrs[1],
            addr2=addrs[2],
            seqn=seqn,
        )

    @classmethod  # generic constructor
    def _from_attrs(
        cls,
        verb: str | VerbT,
        code: str | Code,
        payload: PayloadT,
        *,
        addr0: DeviceIdT | str | None = None,
        addr1: DeviceIdT | str | None = None,
        addr2: DeviceIdT | str | None = None,
        seqn: int | str | None = None,
    ) -> Command:
        """Create a command from its attrs using an address set."""

        verb = I_ if verb == "I" else W_ if verb == "W" else verb

        addr0 = addr0 or NON_DEV_ADDR.id
        addr1 = addr1 or NON_DEV_ADDR.id
        addr2 = addr2 or NON_DEV_ADDR.id

        _, _, *addrs = pkt_addrs(" ".join((addr0, addr1, addr2)))
        # print(pkt_addrs(" ".join((addr0, addr1, addr2))))

        if seqn is None or seqn in ("", "-", "--", "---"):
            seqn = "---"
        elif isinstance(seqn, int):
            seqn = f"{int(seqn):03d}"

        frame = " ".join(
            (
                verb,
                seqn,
                *(a.id for a in addrs),
                code,
                f"{int(len(payload) / 2):03d}",
                payload,
            )
        )

        return cls(frame)

    @classmethod  # used by CLI for -x switch (NB: no len field)
    def from_cli(cls, cmd_str: str) -> Command:
        """Create a command from a CLI string (the -x switch).

        Examples include (whitespace for readability):
            'RQ     01:123456               1F09 00'
            'RQ     01:123456     13:123456 3EF0 00'
            'RQ     07:045960     01:054173 10A0 00137400031C'
            ' W 123 30:045960 -:- 32:054173 22F1 001374'
        """

        parts = cmd_str.upper().split()
        if len(parts) < 4:
            raise exc.CommandInvalid(
                f"Command string is not parseable: '{cmd_str}'"
                ", format is: verb [seqn] addr0 [addr1 [addr2]] code payload"
            )

        verb = parts.pop(0)
        seqn = "---" if DEVICE_ID_REGEX.ANY.match(parts[0]) else parts.pop(0)
        payload = parts.pop()[:48]
        code = parts.pop()

        addrs: tuple[DeviceIdT | str, DeviceIdT | str, DeviceIdT | str]

        if not 0 < len(parts) < 4:
            raise exc.CommandInvalid(f"Command is invalid: '{cmd_str}'")
        elif len(parts) == 1 and verb == I_:
            # drs = (cmd[0],          NON_DEV_ADDR.id, cmd[0])
            addrs = (NON_DEV_ADDR.id, NON_DEV_ADDR.id, parts[0])
        elif len(parts) == 1:
            addrs = (HGI_DEV_ADDR.id, parts[0], NON_DEV_ADDR.id)
        elif len(parts) == 2 and parts[0] == parts[1]:
            addrs = (parts[0], NON_DEV_ADDR.id, parts[1])
        elif len(parts) == 2:
            addrs = (parts[0], parts[1], NON_DEV_ADDR.id)
        else:
            addrs = (parts[0], parts[1], parts[2])

        return cls._from_attrs(
            verb,
            code,
            payload,
            **{f"addr{k}": v for k, v in enumerate(addrs)},
            seqn=seqn,
        )

    def __repr__(self) -> str:
        """Return an unambiguous string representation of this object."""
        # e.g.: RQ --- 18:000730 01:145038 --:------ 000A 002 0800  # 000A|RQ|01:145038|08
        comment = f" # {self._hdr}{f' ({self._ctx})' if self._ctx else ''}"
        return f"... {self}{comment}"

    def __str__(self) -> str:
        """Return a brief readable string representation of this object."""
        # e.g.: 000A|RQ|01:145038|08
        return super().__repr__()  # TODO: self._hdr

    @property
    def tx_header(self) -> HeaderT:
        """Return the QoS header of this (request) packet."""

        return self._hdr

    @property
    def rx_header(self) -> HeaderT | None:
        """Return the QoS header of a corresponding response packet (if any)."""

        if self.tx_header and self._rx_header is None:
            self._rx_header = pkt_header(self, rx_header=True)
        return self._rx_header

    @classmethod  # constructor for I|0002  # TODO: trap corrupt temps?
    def put_weather_temp(cls, dev_id: DeviceIdT | str, temperature: float) -> Command:
        """Constructor to announce the current temperature of a weather sensor (0002).

        This is for use by a faked HB85 or similar.
        """

        if dev_id[:2] != DEV_TYPE_MAP.OUT:
            raise exc.CommandInvalid(
                f"Faked device {dev_id} has an unsupported device type: "
                f"device_id should be like {DEV_TYPE_MAP.OUT}:xxxxxx"
            )

        payload = f"00{hex_from_temp(temperature)}01"
        return cls._from_attrs(I_, Code._0002, payload, addr0=dev_id, addr2=dev_id)

    @classmethod  # constructor for RQ|0004
    def get_zone_name(cls, ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT) -> Command:
        """Get the name of a zone. (c.f. parser_0004)

        This method constructs a command to request the name of a specific zone
        from the controller.

        :param ctl_id: The device ID of the controller
        :type ctl_id: DeviceIdT | str
        :param zone_idx: The index of the zone (00-31)
        :type zone_idx: _ZoneIdxT
        :return: A Command object for the RQ|0004 message
        :rtype: Command

        .. note::
            The zone name is typically a user-assigned identifier for the zone,
            such as "Living Room" or "Bedroom 1".
        """
        return cls.from_attrs(RQ, ctl_id, Code._0004, f"{_check_idx(zone_idx)}00")

    @classmethod  # constructor for W|0004
    def set_zone_name(
        cls, ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT, name: str
    ) -> Command:
        """Set the name of a zone. (c.f. parser_0004)

        This method constructs a command to set the name of a specific zone
        on the controller. The name will be truncated to 20 characters (40 hex digits).

        :param ctl_id: The device ID of the controller
        :type ctl_id: DeviceIdT | str
        :param zone_idx: The index of the zone (00-31)
        :type zone_idx: _ZoneIdxT
        :param name: The new name for the zone (max 20 characters)
        :type name: str
        :return: A Command object for the W|0004 message
        :rtype: Command

        .. note::
            The name will be converted to uppercase and non-ASCII characters
            will be replaced with '?'. The name is limited to 20 characters.
        """
        payload = f"{_check_idx(zone_idx)}00{hex_from_str(name)[:40]:0<40}"
        return cls.from_attrs(W_, ctl_id, Code._0004, payload)

    @classmethod  # constructor for RQ|0006
    def get_schedule_version(cls, ctl_id: DeviceIdT | str) -> Command:
        """Get the current version (change counter) of the schedules.

        This method retrieves a version number that is incremented whenever any zone's
        schedule (including the DHW zone) is modified. This allows clients to efficiently
        check if schedules have changed before downloading them.

        :param ctl_id: The device ID of the controller
        :type ctl_id: DeviceIdT | str
        :return: A Command object for the RQ|0006 message
        :rtype: Command

        .. note::
            The version number is a simple counter that increments with each schedule
            change. It has no inherent meaning beyond indicating that a change has
            occurred. The actual value should be compared with a previously stored
            version to detect changes.
        """
        return cls.from_attrs(RQ, ctl_id, Code._0006, "00")

    @classmethod  # constructor for RQ|0008
    def get_relay_demand(
        cls, dev_id: DeviceIdT | str, zone_idx: _ZoneIdxT | None = None
    ) -> Command:
        """Get the current demand value for a relay or zone. (c.f. parser_0008)

        This method constructs a command to request the current demand value for a
        specific relay or zone. The demand value typically represents the requested
        output level (0-100%) for the relay or zone.

        :param dev_id: The device ID of the relay or controller
        :type dev_id: DeviceIdT | str
        :param zone_idx: The index of the zone (00-31), or None for the relay itself
        :type zone_idx: _ZoneIdxT | None
        :return: A Command object for the RQ|0008 message
        :rtype: Command

        .. note::
            - If zone_idx is None, the command requests the relay's overall demand.
            - If zone_idx is specified, the command requests the demand for that specific zone.
            - The response will contain the current demand value as a percentage (0-100%).
        """
        payload = "00" if zone_idx is None else _check_idx(zone_idx)
        return cls.from_attrs(RQ, dev_id, Code._0008, payload)

    @classmethod  # constructor for RQ|000A
    def get_zone_config(cls, ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT) -> Command:
        """Get the configuration of a specific zone. (c.f. parser_000a)

        This method constructs a command to request the configuration parameters
        for a specific zone from the controller. The configuration includes
        settings related to the zone's operation, such as temperature setpoints,
        mode, and other zone-specific parameters.

        :param ctl_id: The device ID of the controller
        :type ctl_id: DeviceIdT | str
        :param zone_idx: The index of the zone (00-31)
        :type zone_idx: _ZoneIdxT
        :return: A Command object for the RQ|000A message
        :rtype: Command

        .. note::
            The response to this command will include various configuration parameters
            for the specified zone, such as:
            - Zone type (radiator, underfloor heating, etc.)
            - Temperature setpoints
            - Mode (heating/cooling)
            - Other zone-specific settings
        """
        zon_idx = _check_idx(zone_idx)
        return cls.from_attrs(RQ, ctl_id, Code._000A, zon_idx)

    @classmethod  # constructor for W|000A
    def set_zone_config(
        cls,
        ctl_id: DeviceIdT | str,
        zone_idx: _ZoneIdxT,
        *,
        min_temp: float = 5,
        max_temp: float = 35,
        local_override: bool = False,
        openwindow_function: bool = False,
        multiroom_mode: bool = False,
    ) -> Command:
        """Set the configuration parameters for a specific zone. (c.f. parser_000a)

        This method constructs a command to configure various parameters for a zone,
        including temperature limits and operational modes.

        :param ctl_id: The device ID of the controller
        :type ctl_id: DeviceIdT | str
        :param zone_idx: The index of the zone (00-31)
        :type zone_idx: _ZoneIdxT
        :param min_temp: Minimum allowed temperature for the zone (5-21°C)
        :type min_temp: float
        :param max_temp: Maximum allowed temperature for the zone (21-35°C)
        :type max_temp: float
        :param local_override: If True, allows local temperature override at the device
        :type local_override: bool
        :param openwindow_function: If True, enables open window detection function
        :type openwindow_function: bool
        :param multiroom_mode: If True, enables multi-room mode for this zone
        :type multiroom_mode: bool
        :return: A Command object for the W|000A message
        :rtype: Command
        :raises CommandInvalid: If any parameter is out of range or of incorrect type

        .. note::
            - The minimum temperature must be between 5°C and 21°C
            - The maximum temperature must be between 21°C and 35°C
            - The minimum temperature cannot be higher than the maximum temperature
            - These settings affect how the zone behaves in different operating modes
        """
        zon_idx = _check_idx(zone_idx)

        if not (5 <= min_temp <= 21):
            raise exc.CommandInvalid(f"Out of range, min_temp: {min_temp}")
        if not (21 <= max_temp <= 35):
            raise exc.CommandInvalid(f"Out of range, max_temp: {max_temp}")
        if not isinstance(local_override, bool):
            raise exc.CommandInvalid(f"Invalid arg, local_override: {local_override}")
        if not isinstance(openwindow_function, bool):
            raise exc.CommandInvalid(
                f"Invalid arg, openwindow_function: {openwindow_function}"
            )
        if not isinstance(multiroom_mode, bool):
            raise exc.CommandInvalid(f"Invalid arg, multiroom_mode: {multiroom_mode}")

        bitmap = 0 if local_override else 1
        bitmap |= 0 if openwindow_function else 2
        bitmap |= 0 if multiroom_mode else 16

        payload = "".join(
            (zon_idx, f"{bitmap:02X}", hex_from_temp(min_temp), hex_from_temp(max_temp))
        )

        return cls.from_attrs(W_, ctl_id, Code._000A, payload)

    @classmethod  # constructor for RQ|0100
    def get_system_language(cls, ctl_id: DeviceIdT | str, **kwargs: Any) -> Command:
        """Get the configured language of the system. (c.f. parser_0100)

        This method constructs a command to request the current language setting
        from the system controller.

        :param ctl_id: The device ID of the controller
        :type ctl_id: DeviceIdT | str
        :param kwargs: Additional keyword arguments (not used, for compatibility only)
        :return: A Command object for the RQ|0100 message
        :rtype: Command

        .. note::
            The response will contain a language code that corresponds to the
            system's configured language setting.
        """
        assert not kwargs, kwargs
        return cls.from_attrs(RQ, ctl_id, Code._0100, "00", **kwargs)

    @classmethod  # constructor for RQ|0404
    def get_schedule_fragment(
        cls,
        ctl_id: DeviceIdT | str,
        zone_idx: _ZoneIdxT,
        frag_number: int,
        total_frags: int | None,
        **kwargs: Any,
    ) -> Command:
        """Get a specific fragment of a schedule. (c.f. parser_0404)

        This method constructs a command to request a specific fragment of a schedule
        from the controller. Schedules are typically broken into multiple fragments
        for efficient transmission.

        :param ctl_id: The device ID of the controller
        :type ctl_id: DeviceIdT | str
        :param zone_idx: The index of the zone (00-31), or 0xFA/'FA'/'HW' for DHW schedule
        :type zone_idx: _ZoneIdxT
        :param frag_number: The fragment number to retrieve (0-based)
        :type frag_number: int
        :param total_frags: Total number of fragments (optional)
        :type total_frags: int | None
        :param kwargs: Additional keyword arguments
        :return: A Command object for the RQ|0404 message
        :rtype: Command

        .. note::
            - For zone schedules, use a zone index between 00-31
            - For DHW (Domestic Hot Water) schedule, use 0xFA, 'FA', or 'HW' as zone_idx
            - The schedule is typically retrieved in multiple fragments to handle
              the potentially large amount of data
        """

        assert not kwargs, kwargs
        zon_idx = _check_idx(zone_idx)

        if total_frags is None:
            total_frags = 0

        kwargs.pop("frag_length", None)  # for pytests?
        frag_length = "00"

        # TODO: check the following rules
        if frag_number == 0:
            raise exc.CommandInvalid(f"frag_number={frag_number}, but it is 1-indexed")
        elif frag_number == 1 and total_frags != 0:
            raise exc.CommandInvalid(
                f"total_frags={total_frags}, but must be 0 when frag_number=1"
            )
        elif frag_number > total_frags and total_frags != 0:
            raise exc.CommandInvalid(
                f"frag_number={frag_number}, but must be <= total_frags={total_frags}"
            )

        header = "00230008" if zon_idx == FA else f"{zon_idx}200008"

        payload = f"{header}{frag_length}{frag_number:02X}{total_frags:02X}"
        return cls.from_attrs(RQ, ctl_id, Code._0404, payload, **kwargs)

    @classmethod  # constructor for W|0404
    def set_schedule_fragment(
        cls,
        ctl_id: DeviceIdT | str,
        zone_idx: _ZoneIdxT,
        frag_num: int,
        frag_cnt: int,
        fragment: str,
    ) -> Command:
        """Set a specific fragment of a schedule. (c.f. parser_0404)

        This method constructs a command to set a specific fragment of a schedule
        on the controller. Schedules are typically set in multiple fragments
        due to their potentially large size.

        :param ctl_id: The device ID of the controller
        :type ctl_id: DeviceIdT | str
        :param zone_idx: The index of the zone (00-31), or 0xFA/'FA'/'HW' for DHW schedule
        :type zone_idx: _ZoneIdxT
        :param frag_num: The fragment number being set (1-based index)
        :type frag_num: int
        :param frag_cnt: Total number of fragments in the schedule
        :type frag_cnt: int
        :param fragment: The schedule fragment data as a hex string
        :type fragment: str
        :return: A Command object for the W|0404 message
        :rtype: Command
        :raises CommandInvalid: If fragment number is invalid or out of range

        .. note::
            - For zone schedules, use a zone index between 00-31
            - For DHW (Domestic Hot Water) schedule, use 0xFA, 'FA', or 'HW' as zone_idx
            - The first fragment (frag_num=1) typically contains schedule metadata
            - Fragment numbers are 1-based (1 to frag_cnt)
        """

        zon_idx = _check_idx(zone_idx)

        # TODO: check the following rules
        if frag_num == 0:
            raise exc.CommandInvalid(f"frag_num={frag_num}, but it is 1-indexed")
        elif frag_num > frag_cnt:
            raise exc.CommandInvalid(
                f"frag_num={frag_num}, but must be <= frag_cnt={frag_cnt}"
            )

        header = "00230008" if zon_idx == FA else f"{zon_idx}200008"
        frag_length = int(len(fragment) / 2)

        payload = f"{header}{frag_length:02X}{frag_num:02X}{frag_cnt:02X}{fragment}"
        return cls.from_attrs(W_, ctl_id, Code._0404, payload)

    @classmethod  # constructor for RQ|0418
    def get_system_log_entry(
        cls, ctl_id: DeviceIdT | str, log_idx: int | str
    ) -> Command:
        """Retrieve a specific log entry from the system log. (c.f. parser_0418)

        This method constructs a command to request a specific log entry from the
        system's event log. The log contains historical events and fault records.

        :param ctl_id: The device ID of the controller
        :type ctl_id: DeviceIdT | str
        :param log_idx: The index of the log entry to retrieve (0-based)
        :type log_idx: int | str (hex string)
        :return: A Command object for the RQ|0418 message
        :rtype: Command

        .. note::
            - The log index is 0-based, where 0 is the most recent entry
            - The log typically contains system events, faults, and warnings
            - The response will include details about the log entry
        """
        log_idx = log_idx if isinstance(log_idx, int) else int(log_idx, 16)
        return cls.from_attrs(RQ, ctl_id, Code._0418, f"{log_idx:06X}")

    @classmethod  # constructor for I|0418 (used for testing only)
    def _put_system_log_entry(
        cls,
        ctl_id: DeviceIdT | str,
        fault_state: FaultState | str,
        fault_type: FaultType | str,
        device_class: FaultDeviceClass | str,
        device_id: DeviceIdT | str | None = None,
        domain_idx: int | str = "00",
        _log_idx: int | str | None = None,
        timestamp: dt | str | None = None,
        **kwargs: Any,
    ) -> Command:
        """Create a log entry in the system log. (c.f. parser_0418)

        This internal method constructs a command to create a log entry in the system's
        event log. It's primarily used for testing purposes to simulate log entries.

        :param ctl_id: The device ID of the controller
        :type ctl_id: DeviceIdT | str
        :param fault_state: The state of the fault (e.g., 'on', 'off', 'unknown')
        :type fault_state: FaultState | str
        :param fault_type: The type of fault being logged
        :type fault_type: FaultType | str
        :param device_class: The class of device associated with the fault
        :type device_class: FaultDeviceClass | str
        :param device_id: The ID of the device associated with the fault (optional)
        :type device_id: DeviceIdT | str | None
        :param domain_idx: The domain index (default: '00')
        :type domain_idx: int | str
        :param _log_idx: The log index (for internal use, optional)
        :type _log_idx: int | str | None
        :param timestamp: The timestamp of the log entry (default: current time)
        :type timestamp: dt | str | None
        :param kwargs: Additional keyword arguments
        :return: A Command object for the I|0418 message
        :rtype: Command
        :raises AssertionError: If device_class is invalid

        .. note::
            - This is an internal method primarily used for testing
            - The log entry will appear in the system's event log
            - The fault_state and fault_type should match the expected enums
            - If timestamp is not provided, the current time will be used
        """
        if isinstance(device_class, FaultDeviceClass):
            device_class = {v: k for k, v in FAULT_DEVICE_CLASS.items()}[device_class]
        assert device_class in FAULT_DEVICE_CLASS

        if isinstance(fault_state, FaultState):
            fault_state = {v: k for k, v in FAULT_STATE.items()}[fault_state]
        assert fault_state in FAULT_STATE

        if isinstance(fault_type, FaultType):
            fault_type = {v: k for k, v in FAULT_TYPE.items()}[fault_type]
        assert fault_type in FAULT_TYPE

        assert isinstance(domain_idx, str) and len(domain_idx) == 2

        if _log_idx is None:
            _log_idx = 0
        if not isinstance(_log_idx, str):
            _log_idx = f"{_log_idx:02X}"
        assert 0 <= int(_log_idx, 16) <= 0x3F  # TODO: is it 0x3E or 0x3F?

        if timestamp is None:
            timestamp = dt.now()  #
        timestamp = hex_from_dts(timestamp)

        dev_id = dev_id_to_hex_id(device_id) if device_id else "000000"  # type: ignore[arg-type]

        payload = "".join(
            (
                "00",
                fault_state,
                _log_idx,
                "B0",
                fault_type,
                domain_idx,
                device_class,
                "0000",
                timestamp,
                "FFFF7000",
                dev_id,
            )
        )

        return cls.from_attrs(I_, ctl_id, Code._0418, payload)

    @classmethod  # constructor for RQ|1030
    def get_mix_valve_params(
        cls, ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT
    ) -> Command:
        """Retrieve the mixing valve parameters for a specific zone. (c.f. parser_1030)

        This method constructs a command to request the current mixing valve parameters
        for a specific zone from the controller. These parameters control how the
        mixing valve operates for the specified zone.

        :param ctl_id: The device ID of the controller
        :type ctl_id: DeviceIdT | str
        :param zone_idx: The index of the zone (00-31)
        :type zone_idx: _ZoneIdxT
        :return: A Command object for the RQ|1030 message
        :rtype: Command

        .. note::
            - The mixing valve controls the temperature of the water in the heating circuit
              by mixing hot water from the boiler with cooler return water
            - The parameters include settings like the minimum and maximum flow temperatures
              and the proportional band for the valve control
        """
        zon_idx = _check_idx(zone_idx)

        return cls.from_attrs(RQ, ctl_id, Code._1030, zon_idx)

    @classmethod  # constructor for W|1030 - TODO: sort out kwargs for HVAC
    def set_mix_valve_params(
        cls,
        ctl_id: DeviceIdT | str,
        zone_idx: _ZoneIdxT,
        *,
        max_flow_setpoint: int = 55,
        min_flow_setpoint: int = 15,
        valve_run_time: int = 150,
        pump_run_time: int = 15,
        **kwargs: Any,
    ) -> Command:
        """Set the mixing valve parameters for a specific zone. (c.f. parser_1030)

        This method constructs a command to configure the mixing valve parameters
        for a specific zone. These parameters control how the mixing valve operates
        to regulate the temperature of the water in the heating circuit.

        :param ctl_id: The device ID of the controller
        :type ctl_id: DeviceIdT | str
        :param zone_idx: The index of the zone (00-31)
        :type zone_idx: _ZoneIdxT
        :param max_flow_setpoint: Maximum flow temperature setpoint in °C (0-99)
        :type max_flow_setpoint: int
        :param min_flow_setpoint: Minimum flow temperature setpoint in °C (0-50)
        :type min_flow_setpoint: int
        :param valve_run_time: Valve run time in seconds (0-240)
        :type valve_run_time: int
        :param pump_run_time: Pump overrun time in seconds after valve closes (0-99)
        :type pump_run_time: int
        :param kwargs: Additional keyword arguments (e.g., boolean_cc)
        :return: A Command object for the W|1030 message
        :rtype: Command
        :raises CommandInvalid: If any parameter is out of valid range

        .. note::
            - The mixing valve controls the temperature by mixing hot water from the boiler
              with cooler return water
            - The pump overrun time allows the pump to continue running after the valve
              closes to dissipate residual heat
            - The valve run time determines how long the valve takes to move between
              fully open and fully closed positions
        """
        boolean_cc = kwargs.pop("boolean_cc", 1)
        assert not kwargs, kwargs

        zon_idx = _check_idx(zone_idx)

        if not (0 <= max_flow_setpoint <= 99):
            raise exc.CommandInvalid(
                f"Out of range, max_flow_setpoint: {max_flow_setpoint}"
            )
        if not (0 <= min_flow_setpoint <= 50):
            raise exc.CommandInvalid(
                f"Out of range, min_flow_setpoint: {min_flow_setpoint}"
            )
        if not (0 <= valve_run_time <= 240):
            raise exc.CommandInvalid(f"Out of range, valve_run_time: {valve_run_time}")
        if not (0 <= pump_run_time <= 99):
            raise exc.CommandInvalid(f"Out of range, pump_run_time: {pump_run_time}")

        payload = "".join(
            (
                zon_idx,
                f"C801{max_flow_setpoint:02X}",
                f"C901{min_flow_setpoint:02X}",
                f"CA01{valve_run_time:02X}",
                f"CB01{pump_run_time:02X}",
                f"CC01{boolean_cc:02X}",
            )
        )

        return cls.from_attrs(W_, ctl_id, Code._1030, payload, **kwargs)

    @classmethod  # constructor for RQ|10A0
    def get_dhw_params(cls, ctl_id: DeviceIdT | str, **kwargs: Any) -> Command:
        """Get the parameters of the Domestic Hot Water (DHW) system. (c.f. parser_10a0)

        This method constructs a command to retrieve the current parameters
        of the DHW system, including setpoint, overrun, and differential settings.

        :param ctl_id: The device ID of the controller
        :type ctl_id: DeviceIdT | str
        :param kwargs: Additional keyword arguments
            - dhw_idx: Index of the DHW circuit (0 or 1), defaults to 0
            - Other arguments will raise an exception
        :return: A Command object for the RQ|10A0 message
        :rtype: Command
        :raises AssertionError: If unexpected keyword arguments are provided

        .. note::
            - Most systems only have one DHW circuit (index 0)
            - The response includes current setpoint, overrun, and differential values
            - The actual values are parsed by parser_10a0
        """
        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, f"Unexpected arguments: {kwargs}"

        return cls.from_attrs(RQ, ctl_id, Code._10A0, dhw_idx)

    @classmethod  # constructor for W|10A0
    def set_dhw_params(
        cls,
        ctl_id: DeviceIdT | str,
        *,
        setpoint: float | None = 50.0,
        overrun: int | None = 5,
        differential: float | None = 1,
        **kwargs: Any,  # only expect "dhw_idx"
    ) -> Command:
        """Set the parameters of the Domestic Hot Water (DHW) system. (c.f. parser_10a0)

        This method constructs a command to configure the parameters of the DHW system,
        including temperature setpoint, pump overrun time, and temperature differential.

        :param ctl_id: The device ID of the controller
        :type ctl_id: DeviceIdT | str
        :param setpoint: Target temperature for DHW in °C (30.0-85.0), defaults to 50.0
        :type setpoint: float | None
        :param overrun: Pump overrun time in minutes (0-10), defaults to 5
        :type overrun: int | None
        :param differential: Temperature differential in °C (1.0-10.0), defaults to 1.0
        :type differential: float | None
        :param kwargs: Additional keyword arguments
            - dhw_idx: Index of the DHW circuit (0 or 1), defaults to 0
        :return: A Command object for the W|10A0 message
        :rtype: Command
        :raises CommandInvalid: If any parameter is out of valid range
        :raises AssertionError: If unexpected keyword arguments are provided

        .. note::
            - The setpoint is the target temperature for the hot water
            - Overrun keeps the pump running after heating stops to dissipate residual heat
            - Differential prevents rapid cycling by requiring this much temperature drop
              before reheating
            - Most systems only have one DHW circuit (index 0)
        """
        # Defaults for newer evohome colour:
        # Defaults for older evohome colour: ?? (30-85) C, ? (0-10) min, ? (1-10) C
        # Defaults for evohome monochrome:

        # 14:34:26.734 022  W --- 18:013393 01:145038 --:------ 10A0 006 000F6E050064
        # 14:34:26.751 073  I --- 01:145038 --:------ 01:145038 10A0 006 000F6E0003E8
        # 14:34:26.764 074  I --- 01:145038 18:013393 --:------ 10A0 006 000F6E0003E8

        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, f"Unexpected arguments: {kwargs}"

        setpoint = 50.0 if setpoint is None else setpoint
        overrun = 5 if overrun is None else overrun
        differential = 1.0 if differential is None else differential

        if not (30.0 <= setpoint <= 85.0):
            raise exc.CommandInvalid(f"Out of range, setpoint: {setpoint}")
        if not (0 <= overrun <= 10):
            raise exc.CommandInvalid(f"Out of range, overrun: {overrun}")
        if not (1 <= differential <= 10):
            raise exc.CommandInvalid(f"Out of range, differential: {differential}")

        payload = f"{dhw_idx}{hex_from_temp(setpoint)}{overrun:02X}{hex_from_temp(differential)}"

        return cls.from_attrs(W_, ctl_id, Code._10A0, payload)

    @classmethod  # constructor for RQ|1100
    def get_tpi_params(
        cls, dev_id: DeviceIdT | str, *, domain_id: int | str | None = None
    ) -> Command:
        """Get the Time Proportional and Integral (TPI) parameters of a system. (c.f. parser_1100)

        This method constructs a command to retrieve the TPI parameters for a specific domain.
        TPI is a control algorithm used to maintain temperature by cycling the boiler on/off.

        :param dev_id: The device ID of the controller or BDR91 relay
        :type dev_id: DeviceIdT | str
        :param domain_id: The domain ID to get parameters for, or None for default
                          (00 for BDR devices, FC for controllers)
        :type domain_id: int | str | None
        :return: A Command object for the RQ|1100 message
        :rtype: Command

        .. note::
            - TPI parameters control how the system maintains temperature by cycling the boiler
            - Different domains can have different TPI settings
            - The response will include cycle rate, minimum on/off times, and other parameters
        """
        if domain_id is None:
            domain_id = "00" if dev_id[:2] == DEV_TYPE_MAP.BDR else FC

        return cls.from_attrs(RQ, dev_id, Code._1100, _check_idx(domain_id))

    @classmethod  # constructor for W|1100
    def set_tpi_params(
        cls,
        ctl_id: DeviceIdT | str,
        domain_id: int | str | None,
        *,
        cycle_rate: int = 3,  # TODO: check
        min_on_time: int = 5,  # TODO: check
        min_off_time: int = 5,  # TODO: check
        proportional_band_width: float | None = None,  # TODO: check
    ) -> Command:
        """Set the Time Proportional and Integral (TPI) parameters of a system. (c.f. parser_1100)

        This method constructs a command to configure the TPI parameters for a specific domain.
        TPI is a control algorithm that maintains temperature by cycling the boiler on/off.

        :param ctl_id: The device ID of the controller
        :type ctl_id: DeviceIdT | str
        :param domain_id: The domain ID to configure, or None for default domain (00)
        :type domain_id: int | str | None
        :param cycle_rate: Number of on/off cycles per hour (TODO: validate range, typically 3,6,9,12)
        :type cycle_rate: int
        :param min_on_time: Minimum time in minutes the boiler stays on (TODO: validate range, typically 1-5)
        :type min_on_time: int
        :param min_off_time: Minimum time in minutes the boiler stays off (TODO: validate range, typically 1-5)
        :type min_off_time: int
        :param proportional_band_width: Width of the proportional band in °C (TODO: validate range, typically 1.5-3.0)
        :type proportional_band_width: float | None
        :return: A Command object for the W|1100 message
        :rtype: Command
        :raises AssertionError: If any parameter is out of valid range

        .. note::
            - TPI parameters control how the system maintains temperature by cycling the boiler
            - Different domains can have different TPI settings
            - The proportional band determines how much the temperature can vary before the
              boiler cycles on/off
            - The cycle rate affects how frequently the boiler cycles when maintaining temperature
            - Parameters are converted to appropriate hex values in the payload (e.g., minutes * 4)
        """
        if domain_id is None:
            domain_id = "00"

        # TODO: Uncomment and fix these validations once ranges are confirmed
        # assert cycle_rate is None or cycle_rate in (3, 6, 9, 12), cycle_rate
        # assert min_on_time is None or 1 <= min_on_time <= 5, min_on_time
        # assert min_off_time is None or 1 <= min_off_time <= 5, min_off_time
        # assert (
        #     proportional_band_width is None or 1.5 <= proportional_band_width <= 3.0
        # ), proportional_band_width

        payload = "".join(
            (
                _check_idx(domain_id),
                f"{cycle_rate * 4:02X}",  # Convert cycles/hour to internal format
                f"{int(min_on_time * 4):02X}",  # Convert minutes to internal format
                f"{int(min_off_time * 4):02X}00",  # Convert minutes to internal format (or: ...FF)
                f"{hex_from_temp(proportional_band_width)}01",  # Convert temperature to hex
            )
        )

        return cls.from_attrs(W_, ctl_id, Code._1100, payload)

    @classmethod  # constructor for RQ|1260
    def get_dhw_temp(cls, ctl_id: DeviceIdT | str, **kwargs: Any) -> Command:
        """Get the current temperature from a Domestic Hot Water (DHW) sensor. (c.f. parser_10a0)

        This method constructs a command to request the current temperature reading from
        a DHW temperature sensor. The sensor is typically located in the hot water tank.

        :param ctl_id: The device ID of the controller
        :type ctl_id: DeviceIdT | str
        :param kwargs: Additional keyword arguments
            - dhw_idx: Index of the DHW sensor (0 or 1), defaults to 0
            - Other arguments will raise an exception
        :return: A Command object for the RQ|1260 message
        :rtype: Command
        :raises AssertionError: If unexpected keyword arguments are provided

        .. note::
            - Most systems only have one DHW sensor (index 0)
            - The response will include the current temperature in degrees Celsius
            - The actual temperature is parsed by parser_10a0
        """
        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, f"Unexpected arguments: {kwargs}"

        return cls.from_attrs(RQ, ctl_id, Code._1260, dhw_idx)

    @classmethod  # constructor for I|1260  # TODO: trap corrupt temps?
    def put_dhw_temp(
        cls, dev_id: DeviceIdT | str, temperature: float | None, **kwargs: Any
    ) -> Command:
        """Announce the current temperature of a Domestic Hot Water (DHW) sensor. (1260)

        This method constructs a command to announce/simulate a temperature reading from
        a DHW temperature sensor. This is primarily intended for use with simulated or
        emulated devices like a faked CS92A sensor.

        :param dev_id: The device ID of the DHW sensor (must start with DHW type code)
        :type dev_id: DeviceIdT | str
        :param temperature: The temperature to report in °C, or None for no reading
        :type temperature: float | None
        :param kwargs: Additional keyword arguments
            - dhw_idx: Index of the DHW sensor (0 or 1), defaults to 0
            - Other arguments will raise an exception
        :return: A Command object for the I|1260 message
        :rtype: Command
        :raises CommandInvalid: If the device type is not a DHW sensor
        :raises AssertionError: If unexpected keyword arguments are provided

        .. note::
            - This is typically used for testing or simulation purposes
            - The temperature is converted to the appropriate hex format
            - The device ID must be a valid DHW sensor type (starts with DHW code)
            - Most systems only have one DHW sensor (index 0)
            - The message is sent as an I-type (unsolicited) message
        """
        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, f"Unexpected arguments: {kwargs}"

        if dev_id[:2] != DEV_TYPE_MAP.DHW:
            raise exc.CommandInvalid(
                f"Faked device {dev_id} has an unsupported device type: "
                f"device_id should be like {DEV_TYPE_MAP.DHW}:xxxxxx"
            )

        payload = f"{dhw_idx}{hex_from_temp(temperature)}"
        return cls._from_attrs(I_, Code._1260, payload, addr0=dev_id, addr2=dev_id)

    @classmethod  # constructor for I|1290  # TODO: trap corrupt temps?
    def put_outdoor_temp(
        cls, dev_id: DeviceIdT | str, temperature: float | None
    ) -> Command:
        """Announce the current outdoor temperature from a sensor. (1290)

        This method constructs a command to announce/simulate an outdoor temperature reading.
        This is for use by a faked HVAC sensor, or similar.

        :param dev_id: The device ID of the outdoor temperature sensor
        :type dev_id: DeviceIdT | str
        :param temperature: The temperature to report in °C, or None for no reading
        :type temperature: float | None
        :return: A Command object for the I|1290 message
        :rtype: Command

        .. note::
            - This is typically used for testing or simulation purposes
            - The temperature is converted to the appropriate hex format
            - The message is sent as an I-type (unsolicited) message
            - The sensor index is hardcoded to 00 (most systems have only one outdoor sensor)
            - The device ID should match the expected format for an outdoor temperature sensor
        """
        payload = f"00{hex_from_temp(temperature)}"
        return cls._from_attrs(I_, Code._1290, payload, addr0=dev_id, addr2=dev_id)

    @classmethod  # constructor for I|1298
    def put_co2_level(cls, dev_id: DeviceIdT | str, co2_level: float | None) -> Command:
        """Announce the current CO₂ level from a sensor. (1298)
        .I --- 37:039266 --:------ 37:039266 1298 003 000316

        This method constructs a command to announce/simulate a CO₂ level reading from
        an indoor air quality sensor. The message is typically sent by devices that
        monitor indoor air quality.

        :param dev_id: The device ID of the CO₂ sensor
        :type dev_id: DeviceIdT | str
        :param co2_level: The CO₂ level to report in ppm (parts per million), or None for no reading
        :type co2_level: float | None
        :return: A Command object for the I|1298 message
        :rtype: Command

        .. note::
            - This is typically used for testing or simulation purposes
            - The CO₂ level is converted to the appropriate hex format using double precision
            - The message is sent as an I-type (unsolicited) message
            - The sensor index is hardcoded to 00 (most systems have only one CO₂ sensor)
            - The device ID should match the expected format for a CO₂ sensor
            - Example message format: ``.I --- 37:039266 --:------ 37:039266 1298 003 000316``
        """
        payload = f"00{hex_from_double(co2_level)}"
        return cls._from_attrs(I_, Code._1298, payload, addr0=dev_id, addr2=dev_id)

    @classmethod  # constructor for I|12A0
    def put_indoor_humidity(
        cls, dev_id: DeviceIdT | str, indoor_humidity: float | None
    ) -> Command:
        """Announce the current indoor humidity from a sensor or fan. (12A0)
        .I --- 37:039266 --:------ 37:039266 1298 003 000316

        This method constructs a command to announce/simulate an indoor humidity reading.
        The message is typically sent by devices that monitor indoor air quality,
        such as humidity sensors or ventilation systems with humidity sensing capabilities.

        :param dev_id: The device ID of the humidity sensor or fan
        :type dev_id: DeviceIdT | str
        :param indoor_humidity: The relative humidity to report (0-100%), or None for no reading
        :type indoor_humidity: float | None
        :return: A Command object for the I|12A0 message
        :rtype: Command

        .. note::
            - This is typically used for testing or simulation purposes
            - The humidity is converted to the appropriate hex format using standard precision
            - The message is sent as an I-type (unsolicited) message
            - The sensor index is hardcoded to 00 (most systems have only one humidity sensor)
            - The device ID should match the expected format for a humidity sensor or fan
            - The humidity value is expected to be in the range 0-100%
            - Example message format: ``.I --- 37:039266 --:------ 37:039266 12A0 003 0032`` (for 50%)
        """
        payload = "00" + hex_from_percent(indoor_humidity, high_res=False)
        return cls._from_attrs(I_, Code._12A0, payload, addr0=dev_id, addr2=dev_id)

    @classmethod  # constructor for RQ|12B0
    def get_zone_window_state(
        cls, ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT
    ) -> Command:
        """Request the open window state of a zone (c.f. parser 12B0).

        This method constructs a command to query whether a particular zone has an open window.
        The response will indicate if the window in the specified zone is open or closed.

        :param ctl_id: The device ID of the controller managing the zone
        :type ctl_id: DeviceIdT | str
        :param zone_idx: The zone index (0-based) to query
        :type zone_idx: _ZoneIdxT
        :return: A Command object for the RQ|12B0 message
        :rtype: Command

        .. note::
            - The zone index is 0-based (0 = Zone 1, 1 = Zone 2, etc.)
            - The controller will respond with a message indicating the window state
            - This is typically used by thermostats to enable/disable heating when windows are open
            - The actual window state detection is usually done by a separate sensor
        """
        return cls.from_attrs(RQ, ctl_id, Code._12B0, _check_idx(zone_idx))

    @classmethod  # constructor for RQ|1F41
    def get_dhw_mode(cls, ctl_id: DeviceIdT | str, **kwargs: Any) -> Command:
        """Request the current mode of the Domestic Hot Water (DHW) system. (c.f. parser 1F41)

        This method constructs a command to query the operating mode of the DHW system.
        The response will indicate whether the DHW is in automatic, manual, or other modes.

        :param ctl_id: The device ID of the DHW controller
        :type ctl_id: DeviceIdT | str
        :param **kwargs: Additional parameters (currently only 'dhw_idx' is supported)
        :key dhw_idx: The DHW circuit index (0 or 1, defaults to 0 for single-DHW systems)
        :type dhw_idx: int, optional
        :return: A Command object for the RQ|1F41 message
        :rtype: Command
        :raises AssertionError: If unexpected keyword arguments are provided

        .. note::
            - Most systems have a single DHW circuit (index 0)
            - The response will indicate the current DHW mode (e.g., auto, manual, off)
            - This is typically used by heating controllers to monitor DHW state
            - The actual mode values are defined in the response parser (parser_1f41)
        """
        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, f"Unexpected arguments: {kwargs}"

        return cls.from_attrs(RQ, ctl_id, Code._1F41, dhw_idx)

    @classmethod  # constructor for W|1F41
    def set_dhw_mode(
        cls,
        ctl_id: DeviceIdT | str,
        *,
        mode: int | str | None = None,
        active: bool | None = None,
        until: dt | str | None = None,
        duration: int | None = None,  # never supplied by DhwZone.set_mode()
        **kwargs: Any,
    ) -> Command:
        """Set or reset the mode of the Domestic Hot Water (DHW) system. (c.f. parser 1F41)

        This method constructs a command to change the operating mode of the DHW system.
        It can set the DHW to automatic, manual on/off, or scheduled modes with specific durations.

        :param ctl_id: The device ID of the DHW controller
        :type ctl_id: DeviceIdT | str
        :param mode: The desired DHW mode (None, "auto", "heat", "off", or numeric values)
        :type mode: int | str | None
        :param active: If specified, sets the DHW on/off state (alternative to mode)
        :type active: bool | None
        :param until: End time for temporary mode (datetime or "YYYY-MM-DD HH:MM" string)
        :type until: datetime | str | None
        :param duration: Duration in seconds for temporary mode (alternative to 'until')
        :type duration: int | None
        :param **kwargs: Additional parameters (currently only 'dhw_idx' is supported)
        :key dhw_idx: The DHW circuit index (0 or 1, defaults to 0 for single-DHW systems)
        :type dhw_idx: int, optional
        :return: A Command object for the W|1F41 message
        :rtype: Command
        :raises AssertionError: If unexpected keyword arguments are provided
        :raises CommandInvalid: If invalid parameters are provided

        .. note::
            - Mode takes precedence over 'active' if both are specified
            - When using 'active' with 'until' or 'duration', the mode will be temporary
            - Supported mode values are defined in ZON_MODE_MAP
            - Most systems have a single DHW circuit (index 0)
            - The actual mode values are defined in the response parser (parser_1f41)
        """
        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, f"Unexpected arguments: {kwargs}"

        mode = _normalise_mode(mode, active, until, duration)

        if mode == ZON_MODE_MAP.FOLLOW:
            active = None
        if active is not None and not isinstance(active, bool | int):
            raise exc.CommandInvalid(
                f"Invalid args: active={active}, but must be a bool"
            )

        until, duration = _normalise_until(mode, active, until, duration)

        payload = "".join(
            (
                dhw_idx,
                "FF" if active is None else "01" if bool(active) else "00",
                mode,
                "FFFFFF" if duration is None else f"{duration:06X}",
                "" if until is None else hex_from_dtm(until),
            )
        )

        return cls.from_attrs(W_, ctl_id, Code._1F41, payload)

    @classmethod  # constructor for 1FC9 (rf_bind) 3-way handshake
    def put_bind(
        cls,
        verb: VerbT,
        src_id: DeviceIdT | str,
        codes: Code | Iterable[Code] | None,
        dst_id: DeviceIdT | str | None = None,
        **kwargs: Any,
    ) -> Command:
        """Create an RF bind command (1FC9) for device binding operations.

        This method constructs commands used in the 3-way handshake process for binding
        devices in the Ramses RF protocol. It's primarily used by faked/test devices.

        :param verb: The verb for the command (I, RQ, RP, W, etc.)
        :type verb: VerbT
        :param src_id: Source device ID initiating the bind
        :type src_id: DeviceIdT | str
        :param codes: Single code or list of codes to bind
        :type codes: Code | Iterable[Code] | None
        :param dst_id: Optional destination device ID (defaults to broadcast)
        :type dst_id: DeviceIdT | str | None
        :param **kwargs: Additional parameters
        :key oem_code: OEM code for bind offers (only used with I-type messages)
        :type oem_code: str, optional
        :return: A Command object for the bind operation
        :rtype: Command
        :raises CommandInvalid: If invalid codes are provided for binding

        .. note::
            - Common use cases include:
              - FAN binding to CO2 (1298), HUM (12A0), PER (2E10), or SWI (22F1, 22F3)
              - CTL binding to DHW (1260), RND/THM (30C9)
            - More complex bindings (e.g., TRV to CTL) may require custom constructors
            - The binding process typically involves a 3-way handshake
            - For I-type messages with no specific destination, this creates a bind offer
        """
        kodes: list[Code]

        if not codes:  # None, "", or []
            kodes = []  # used by confirm
        elif len(codes[0]) == len(Code._1FC9):  # type: ignore[index]  # if iterable: list, tuple, or dict.keys()
            kodes = list(codes)  # type: ignore[arg-type]
        elif len(codes[0]) == len(Code._1FC9[0]):  # type: ignore[index]
            kodes = [codes]  # type: ignore[list-item]
        else:
            raise exc.CommandInvalid(f"Invalid codes for a bind command: {codes}")

        if verb == I_ and dst_id in (None, src_id, ALL_DEV_ADDR.id):
            oem_code = kwargs.pop("oem_code", None)
            assert not kwargs, f"Unexpected arguments: {kwargs}"
            return cls._put_bind_offer(src_id, dst_id, kodes, oem_code=oem_code)

        elif verb == W_ and dst_id not in (None, src_id):
            idx = kwargs.pop("idx", None)
            assert not kwargs, kwargs
            return cls._put_bind_accept(src_id, dst_id, kodes, idx=idx)  # type: ignore[arg-type]

        elif verb == I_:
            idx = kwargs.pop("idx", None)
            assert not kwargs, kwargs
            return cls._put_bind_confirm(src_id, dst_id, kodes, idx=idx)  # type: ignore[arg-type]

        raise exc.CommandInvalid(
            f"Invalid verb|dst_id for a bind command: {verb}|{dst_id}"
        )

    @classmethod  # constructor for 1FC9 (rf_bind) offer
    def _put_bind_offer(
        cls,
        src_id: DeviceIdT | str,
        dst_id: DeviceIdT | str | None,
        codes: list[Code],
        *,
        oem_code: str | None = None,
    ) -> Command:
        """Create a bind offer message (I-type) for device binding.

        # TODO: should preserve order of codes, else tests may fail

        This internal method constructs the initial bind offer message in the 3-way
        binding handshake. It's typically called by `put_bind()` and not used directly.

        :param src_id: Source device ID making the offer
        :type src_id: DeviceIdT | str
        :param dst_id: Optional destination device ID (broadcast if None)
        :type dst_id: DeviceIdT | str | None
        :param codes: List of codes to include in the bind offer
        :type codes: list[Code]
        :param oem_code: Optional OEM-specific code for the binding
        :type oem_code: str | None
        :return: A Command object for the bind offer message
        :rtype: Command
        :raises CommandInvalid: If no valid codes are provided for the offer

        .. note::
            - This creates an I-type (unsolicited) bind offer message
            - The message includes the source device's ID and the requested bind codes
            - OEM-specific bindings can include an additional OEM code
            - The actual binding codes are filtered to exclude 1FC9 and 10E0
            - The order of codes is preserved in the output message
        """
        # Filter out 1FC9 and 10E0 from the codes list
        kodes = [c for c in codes if c not in (Code._1FC9, Code._10E0)]
        if not kodes:  # might be []
            raise exc.CommandInvalid(f"Invalid codes for a bind offer: {codes}")

        hex_id = Address.convert_to_hex(src_id)  # type: ignore[arg-type]
        payload = "".join(f"00{c}{hex_id}" for c in kodes)

        if oem_code:  # 01, 67, 6C
            payload += f"{oem_code}{Code._10E0}{hex_id}"
        payload += f"00{Code._1FC9}{hex_id}"

        return cls.from_attrs(  # NOTE: .from_attrs, not ._from_attrs
            I_, dst_id or src_id, Code._1FC9, payload, from_id=src_id
        )  # as dst_id could be NUL_DEV_ID

    @classmethod  # constructor for 1FC9 (rf_bind) accept - mainly used for test suite
    def _put_bind_accept(
        cls,
        src_id: DeviceIdT | str,
        dst_id: DeviceIdT | str,
        codes: list[Code],
        *,
        idx: str | None = "00",
    ) -> Command:
        """Create a bind accept message (W-type) for device binding.

        This internal method constructs the bind accept message in the 3-way binding
        handshake. It's typically called by `put_bind()` and is mainly used for testing.

        :param src_id: Source device ID accepting the bind
        :type src_id: DeviceIdT | str
        :param dst_id: Destination device ID that sent the bind offer
        :type dst_id: DeviceIdT | str
        :param codes: List of codes to include in the bind accept
        :type codes: list[Code]
        :param idx: Optional index for the binding (defaults to "00")
        :type idx: str | None
        :return: A Command object for the bind accept message
        :rtype: Command
        :raises CommandInvalid: If no valid codes are provided for the accept

        .. note::
            - This creates a W-type (write) bind accept message
            - The message includes the source device's ID and the accepted bind codes
            - The index parameter allows for multiple bindings between the same devices
            - Primarily used in test suites to simulate device binding
            - The actual binding codes should match those in the original offer
        """
        if not codes:  # might be empty list
            raise exc.CommandInvalid(f"Invalid codes for a bind accept: {codes}")

        hex_id = Address.convert_to_hex(src_id)  # type: ignore[arg-type]
        payload = "".join(f"{idx or '00'}{c}{hex_id}" for c in codes)

        return cls.from_attrs(W_, dst_id, Code._1FC9, payload, from_id=src_id)

    @classmethod  # constructor for 1FC9 (rf_bind) confirm
    def _put_bind_confirm(
        cls,
        src_id: DeviceIdT | str,
        dst_id: DeviceIdT | str,
        codes: list[Code],
        *,
        idx: str | None = "00",
    ) -> Command:
        """Create a bind confirmation message (I-type) to complete device binding.

        This internal method constructs the final confirmation message in the 3-way
        binding handshake. It's typically called by `put_bind()` to confirm that
        the binding process has been completed successfully.

        :param src_id: Source device ID confirming the bind
        :type src_id: DeviceIdT | str
        :param dst_id: Destination device ID that needs confirmation
        :type dst_id: DeviceIdT | str
        :param codes: List of codes that were bound (only first code is used)
        :type codes: list[Code]
        :param idx: Optional index for the binding (defaults to "00")
        :type idx: str | None
        :return: A Command object for the bind confirmation message
        :rtype: Command

        .. note::
            - This creates an I-type (unsolicited) bind confirmation message
            - The message includes the source device's ID and the first bound code
            - If no codes are provided, only the index is used as payload
            - The index is important (e.g., Nuaire 4-way switch uses "21")
            - This is the final step in the 3-way binding handshake
            - The binding is considered complete after this message is received
        """
        if not codes:  # if not payload
            payload = idx or "00"  # e.g. Nuaire 4-way switch uses 21!
        else:
            hex_id = Address.convert_to_hex(src_id)  # type: ignore[arg-type]
            payload = f"{idx or '00'}{codes[0]}{hex_id}"

        return cls.from_attrs(I_, dst_id, Code._1FC9, payload, from_id=src_id)

    @classmethod  # constructor for I|22F1
    def set_fan_mode(
        cls,
        fan_id: DeviceIdT | str,
        fan_mode: int | str | None,
        *,
        seqn: int | str | None = None,
        src_id: DeviceIdT | str | None = None,
        idx: str = "00",  # could be e.g. "63"
    ) -> Command:
        """Set the operating mode of a ventilation fan.

        This method constructs a command to control the speed and operating mode of a
        ventilation fan. The command can be sent with either a sequence number or a
        source device ID, depending on the system configuration.

        There are two types of this packet observed:
        - With sequence number: ``I 018 --:------ --:------ 39:159057 22F1 003 000x04``
        - With source ID: ``I --- 21:039407 28:126495 --:------ 22F1 003 000x07``

        :param fan_id: The device ID of the target fan (e.g., '39:159057')
        :type fan_id: DeviceIdT | str
        :param fan_mode: The desired fan mode, which can be specified as:
            - Integer: 0-9 for different speed levels
            - String: Descriptive mode like 'auto', 'low', 'medium', 'high'
            - None: Default mode (typically auto)
        :type fan_mode: int | str | None
        :param seqn: Optional sequence number (0-255), mutually exclusive with src_id
        :type seqn: int | str | None
        :param src_id: Optional source device ID, mutually exclusive with seqn
        :type src_id: DeviceIdT | str | None
        :param idx: Index identifier, typically '00' but can be other values like '63'
        :type idx: str
        :return: A configured Command object ready to be sent to the device.
        :rtype: Command
        :raises CommandInvalid: If both seqn and src_id are provided, or if fan_mode is invalid.

        .. note::
            This command is typically sent as part of a triplet with 0.1s intervals
            when using sequence numbers. The sequence number should increase
            monotonically modulo 256 after each triplet.

            **Scheme 1 (with sequence number):**
            - Sent as a triplet, 0.1s apart
            - Uses a sequence number (000-255)
            - Example: ``I 218 --:------ --:------ 39:159057 22F1 003 000204`` (low speed)

            **Scheme 2 (with source ID):**
            - Sent as a triplet, 0.085s apart
            - Uses source device ID instead of sequence number
            - Example: ``I --- 21:039407 28:126495 --:------ 22F1 003 000507``
        """
        # NOTE: WIP: rate can be int or str

        # Scheme 1: I 218 --:------ --:------ 39:159057
        #  - are cast as a triplet, 0.1s apart?, with a seqn (000-255) and no src_id
        #  - triplet has same seqn, increased monotonically mod 256 after every triplet
        #  - only payloads seen: '(00|63)0[234]04', may accept '000.'
        # .I 218 --:------ --:------ 39:159057 22F1 003 000204  # low

        # Scheme 1a: I --- --:------ --:------ 21:038634 (less common)
        #  - some systems that accept scheme 2 will accept this scheme

        # Scheme 2: I --- 21:038634 18:126620 --:------ (less common)
        #  - are cast as a triplet, 0.085s apart, without a seqn (i.e. is ---)
        #  - only payloads seen: '000[0-9A]0[5-7A]', may accept '000.'
        # .I --- 21:038634 18:126620 --:------ 22F1 003 000507

        from .ramses import _22F1_MODE_ORCON

        _22F1_MODE_ORCON_MAP = {v: k for k, v in _22F1_MODE_ORCON.items()}

        if fan_mode is None:
            mode = "00"
        elif isinstance(fan_mode, int):
            mode = f"{fan_mode:02X}"
        else:
            mode = fan_mode

        if mode in _22F1_MODE_ORCON:
            payload = f"{idx}{mode}"
        elif mode in _22F1_MODE_ORCON_MAP:
            payload = f"{idx}{_22F1_MODE_ORCON_MAP[mode]}"
        else:
            raise exc.CommandInvalid(f"fan_mode is not valid: {fan_mode}")

        if src_id and seqn:
            raise exc.CommandInvalid(
                "seqn and src_id are mutually exclusive (you can have neither)"
            )

        if seqn:
            return cls._from_attrs(I_, Code._22F1, payload, addr2=fan_id, seqn=seqn)
        return cls._from_attrs(I_, Code._22F1, payload, addr0=src_id, addr1=fan_id)

    @classmethod  # constructor for I|22F7
    def set_bypass_position(
        cls,
        fan_id: DeviceIdT | str,
        *,
        bypass_position: float | None = None,
        src_id: DeviceIdT | str | None = None,
        **kwargs: Any,
    ) -> Command:
        """Set the position or mode of a bypass valve in a ventilation system.

        This method constructs a command to control the bypass valve position or mode
        for a ventilation system. The bypass valve regulates the flow of air between
        the supply and exhaust air streams, typically for heat recovery.

        The method supports two ways to control the bypass:
        - Direct position control using `bypass_position` (0.0 to 1.0)
        - Predefined modes using `bypass_mode` ('auto', 'on', 'off')

        :param fan_id: The device ID of the target fan/ventilation unit (e.g., '01:123456')
        :type fan_id: DeviceIdT | str
        :param bypass_position: The desired position as a float between 0.0 (fully closed)
            and 1.0 (fully open). If None, the system will use auto mode.
        :type bypass_position: float | None
        :param src_id: The source device ID sending the command. If None, defaults to fan_id.
        :type src_id: DeviceIdT | str | None
        :keyword bypass_mode: Alternative to bypass_position, accepts:
            - 'auto': Let the system control the bypass automatically
            - 'on': Force bypass fully open
            - 'off': Force bypass fully closed
        :type bypass_mode: str | None
        :return: A configured Command object ready to be sent to the device.
        :rtype: Command
        :raises CommandInvalid: If both bypass_position and bypass_mode are provided,
            or if an invalid bypass_mode is specified.

        .. note::
            The bypass valve position affects heat recovery efficiency and indoor air quality.
            Use with caution as incorrect settings may impact system performance.
        """

        # RQ --- 37:155617 32:155617 --:------ 22F7 002 0064  # officially: 00C8EF
        # RP --- 32:155617 37:155617 --:------ 22F7 003 00C8C8

        bypass_mode = kwargs.pop("bypass_mode", None)
        assert not kwargs, kwargs

        src_id = src_id or fan_id  # TODO: src_id should be an arg?

        if bypass_mode and bypass_position is not None:
            raise exc.CommandInvalid(
                "bypass_mode and bypass_position are mutually exclusive, "
                "both cannot be provided, and neither is OK"
            )
        elif bypass_position is not None:
            pos = f"{int(bypass_position * 200):02X}"
        elif bypass_mode:
            pos = {"auto": "FF", "off": "00", "on": "C8"}[bypass_mode]
        else:
            pos = "FF"  # auto

        return cls._from_attrs(
            W_, Code._22F7, f"00{pos}", addr0=src_id, addr1=fan_id
        )  # trailing EF not required

    @classmethod  # constructor for RQ|2309
    def get_zone_setpoint(cls, ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT) -> Command:
        """Get the current temperature setpoint for a specific zone.

        This method constructs a command to request the current temperature setpoint
        for a specified zone from the controller. The response will contain the current
        target temperature for the zone.

        :param ctl_id: The device ID of the controller (e.g., '01:123456')
        :type ctl_id: DeviceIdT | str
        :param zone_idx: The index of the zone (0-31 or '00'-'1F')
        :type zone_idx: _ZoneIdxT
        :return: A configured Command object that can be sent to the device.
        :rtype: Command
        :raises ValueError: If the zone index is out of valid range (0-31)

        .. note::
            The zone index is 0-based, where:
            - 0 = Zone 1 (typically main living area)
            - 1 = Zone 2 (e.g., bedrooms)
            - And so on up to zone 32

            The actual number of available zones depends on the controller configuration.
            Requesting a non-existent zone will typically result in no response.
        """
        return cls.from_attrs(W_, ctl_id, Code._2309, _check_idx(zone_idx))

    @classmethod  # constructor for W|2309
    def set_zone_setpoint(
        cls, ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT, setpoint: float
    ) -> Command:
        """Set the temperature setpoint for a specific zone.

        This method constructs a command to set the target temperature for a specified
        zone. The setpoint is specified in degrees Celsius with a resolution of 0.1°C.

        :param ctl_id: The device ID of the controller (e.g., '01:123456')
        :type ctl_id: DeviceIdT | str
        :param zone_idx: The index of the zone (0-31 or '00'-'1F')
        :type zone_idx: _ZoneIdxT
        :param setpoint: The desired temperature in °C (typically 5.0-35.0)
        :type setpoint: float
        :return: A configured Command object ready to be sent to the device.
        :rtype: Command
        :raises ValueError: If the setpoint is outside the valid range or if the
            zone index is invalid.

        .. note::
            The controller will typically round the setpoint to the nearest 0.5°C.
            The actual temperature range may be further limited by:
            - System-wide minimum/maximum limits
            - Zone-specific overrides
            - Current operating mode (heating/cooling)

            When setting a new setpoint, the system may take some time to acknowledge
            the change. Use `get_zone_setpoint` to verify the new setting.

            Some systems may have additional restrictions on when setpoints can be
            modified, such as during specific operating modes or schedules.
        """
        # Example: .W --- 34:092243 01:145038 --:------ 2309 003 0107D0
        payload = f"{_check_idx(zone_idx)}{hex_from_temp(setpoint)}"
        return cls.from_attrs(W_, ctl_id, Code._2309, payload)

    @classmethod  # constructor for RQ|2349
    def get_zone_mode(cls, ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT) -> Command:
        """Get the current operating mode of a zone.

        This method constructs a command to request the current operating mode
        and setpoint information for a specific zone from the controller.

        :param ctl_id: The device ID of the controller (e.g., '01:123456')
        :type ctl_id: DeviceIdT | str
        :param zone_idx: The index of the zone (0-31 or '00'-'1F')
        :type zone_idx: _ZoneIdxT
        :return: A configured Command object that can be sent to the device.
        :rtype: Command

        :Example:
            >>> # Get mode for zone 0
            >>> cmd = Command.get_zone_mode('01:123456', '00')
        """

        return cls.from_attrs(RQ, ctl_id, Code._2349, _check_idx(zone_idx))

    @classmethod  # constructor for W|2349
    def set_zone_mode(
        cls,
        ctl_id: DeviceIdT | str,
        zone_idx: _ZoneIdxT,
        *,
        mode: int | str | None = None,
        setpoint: float | None = None,
        until: dt | str | None = None,
        duration: int | None = None,  # never supplied by Zone.set_mode()
    ) -> Command:
        """Set or reset the operating mode of a zone.

        This method constructs a command to configure the operating mode and setpoint
        for a specific zone. The command can set the zone to various modes including
        follow schedule, temporary override, or permanent override.

        :param ctl_id: The device ID of the controller (e.g., '01:123456')
        :type ctl_id: DeviceIdT | str
        :param zone_idx: The index of the zone (0-31 or '00'-'1F')
        :type zone_idx: _ZoneIdxT
        :keyword mode: The desired operating mode. Can be an integer, string, or None.
            Common values include 'follow_schedule', 'temporary', 'permanent_override'.
        :type mode: int | str | None
        :keyword setpoint: The target temperature in °C (resolution 0.1°C). Required for
            some modes. If None, the system will use the maximum possible value.
        :type setpoint: float | None
        :keyword until: The end time for a temporary override. Required for 'temporary' mode.
            Can be a datetime object or ISO 8601 formatted string.
        :type until: datetime | str | None
        :keyword duration: Duration in minutes for the override. Mutually exclusive with 'until'.
        :type duration: int | None
        :return: A configured Command object ready to be sent to the device.
        :rtype: Command
        :raises CommandInvalid: If invalid arguments are provided.

        .. note::
            Incompatible combinations:
            - mode == 'follow_schedule' & setpoint is not None (setpoint will be ignored)
            - mode == 'temporary' & until is None (until is required)
            - until and duration are mutually exclusive (use only one)
        """

        # .W --- 18:013393 01:145038 --:------ 2349 013 0004E201FFFFFF330B1A0607E4
        # .W --- 22:017139 01:140959 --:------ 2349 007 0801F400FFFFFF

        mode = _normalise_mode(mode, setpoint, until, duration)

        if setpoint is not None and not isinstance(setpoint, float | int):
            raise exc.CommandInvalid(
                f"Invalid args: setpoint={setpoint}, but must be a float"
            )

        until, duration = _normalise_until(mode, setpoint, until, duration)

        payload = "".join(
            (
                _check_idx(zone_idx),
                hex_from_temp(setpoint),  # None means max, if a temp is required
                mode,
                "FFFFFF" if duration is None else f"{duration:06X}",
                "" if until is None else hex_from_dtm(until),
            )
        )

        return cls.from_attrs(W_, ctl_id, Code._2349, payload)

    @classmethod  # constructor for W|2411
    def set_fan_param(
        cls,
        fan_id: DeviceIdT | str,
        param_id: str,
        value: str | int | float | bool,
        *,
        src_id: DeviceIdT | str | None = None,
    ) -> Command:
        """Set a configuration parameter for a fan/ventilation device.

        This method constructs a command to configure various parameters of a
        fan or ventilation device using the RAMSES-II protocol.

        :param fan_id: The device ID of the fan/ventilation unit
        :type fan_id: DeviceIdT | str
        :param param_id: The parameter ID to set (e.g., 'bypass_position' or hex code '00')
        :type param_id: str
        :param value: The value to set for the parameter. Type depends on the parameter.
        :type value: str | int | float | bool
        :param src_id: Optional source device ID. If not provided, fan_id will be used.
        :type src_id: DeviceIdT | str | None
        :return: A configured Command object ready to be sent to the device.
        :rtype: Command
        :raises CommandInvalid: If the parameter ID is unknown or value is invalid.

        .. note::
            The parameter ID must be a valid 2-character hexadecimal string (00-FF) that
            exists in the _2411_PARAMS_SCHEMA. The payload format follows the pattern:
            ^(00|01|15|16|17|21)00[0-9A-F]{6}[0-9A-F]{8}(([0-9A-F]{8}){3}[0-9A-F]{4})?$
            --- Ramses-II 2411 payload: 23 bytes, 46 hex digits ---

        Raises:
            CommandInvalid: For invalid parameters or values
        """
        # Validate and normalize parameter ID
        try:
            param_id = param_id.strip().upper()
            if len(param_id) != 2:
                raise ValueError(
                    "Parameter ID must be exactly 2 hexadecimal characters"
                )
            int(param_id, 16)  # Validate hex
        except ValueError as err:
            raise exc.CommandInvalid(
                f"Invalid parameter ID: '{param_id}'. Must be a 2-digit hexadecimal value (00-FF)"
            ) from err

        # Get parameter schema
        if (param_schema := _2411_PARAMS_SCHEMA.get(param_id)) is None:
            raise exc.CommandInvalid(
                f"Unknown parameter ID: '{param_id}'. This parameter is not defined in the device schema"
            )

        # Get value constraints with defaults
        min_val = param_schema[SZ_MIN_VALUE]
        max_val = param_schema[SZ_MAX_VALUE]
        precision = param_schema.get(SZ_PRECISION, 1.0)
        data_type = param_schema.get(SZ_DATA_TYPE, "00")

        try:
            # Check for special float values first
            if isinstance(value, float) and not math.isfinite(value):
                raise exc.CommandInvalid(
                    f"Parameter {param_id}: Invalid value '{value}'. Must be a finite number"
                )

            # Scaling
            if str(data_type) == "01":  # %
                # Special handling for parameter 52 (Sensor sensitivity)
                value_scaled = int(round(float(value) / precision))
                min_val_scaled = int(round(float(min_val) / precision))
                max_val_scaled = int(round(float(max_val) / precision))
                precision_scaled = int(round(float(precision) * 10))
                trailer = "0032"  # Trailer for percentage parameters

                # For percentage values, validate input is in range
                if not min_val_scaled <= value_scaled <= max_val_scaled:
                    raise exc.CommandInvalid(
                        f"Parameter {param_id}: Value {value_scaled / 10}% is out of allowed range ({min_val_scaled / 10}% to {max_val_scaled / 10}%)"
                    )
            elif str(data_type) == "0F":  # %
                # For other percentage parameters, use the standard scaling
                value_scaled = int(round((float(value) / 100.0) / float(precision)))
                min_val_scaled = int(round(float(min_val) / float(precision)))
                max_val_scaled = int(round(float(max_val) / float(precision)))
                precision_scaled = int(round(float(precision) * 200))
                trailer = "0032"  # Trailer for percentage parameters

                # For percentage values, validate input is in range
                if not min_val_scaled <= value_scaled <= max_val_scaled:
                    raise exc.CommandInvalid(
                        f"Parameter {param_id}: Value {value_scaled / 2}% is out of allowed range ({min_val_scaled / 2}% to {max_val_scaled / 2}%)"
                    )
            elif str(data_type) == "92":  # °C
                # Scale temperature values by 100 (21.5°C -> 2150 = 0x0866)
                # Round to 0.1°C precision first, then scale
                value_rounded = (
                    round(float(value) * 10) / 10
                )  # Round to 1 decimal place
                value_scaled = int(
                    value_rounded * 100
                )  # Convert to integer (e.g., 21.5 -> 2150)
                min_val_scaled = int(float(min_val) * 100)
                max_val_scaled = int(float(max_val) * 100)
                precision_scaled = int(float(precision) * 100)
                trailer = (
                    "0001"  # always 4 hex not sure about the value, but seems to work.
                )
                # For temperature values, validate input is within allowed range
                if not min_val_scaled <= value_scaled <= max_val_scaled:
                    raise exc.CommandInvalid(
                        f"Parameter {param_id}: Temperature {value_scaled / 100:.1f}°C is out of allowed range ({min_val_scaled / 100:.1f}°C to {max_val_scaled / 100:.1f}°C)"
                    )
            elif (str(data_type) == "00") or (
                str(data_type) == "10"
            ):  # numeric (minutes, medium(0)/high(1) or days)
                value_scaled = int(float(value))
                min_val_scaled = int(float(min_val))
                max_val_scaled = int(float(max_val))
                precision = 1
                precision_scaled = int(precision)
                trailer = (
                    "0001"  # always 4 hex not sure about the value, but seems to work.
                )
                # For numeric values, validate input is between min and max
                if not min_val_scaled <= value_scaled <= max_val_scaled:
                    unit = "minutes" if data_type == "00" else ""
                    raise exc.CommandInvalid(
                        f"Parameter {param_id}: Value {value_scaled}{' ' + unit if unit else ''} is out of allowed range ({min_val_scaled} to {max_val_scaled}{' ' + unit if unit else ''})"
                    )
            else:
                # Validate value against min/max
                raise exc.CommandInvalid(
                    f"Parameter {param_id}: Invalid data type '{data_type}'. Must be one of '00', '01', '0F', '10', or '92'"
                    f"Invalid Data_type {data_type} for parameter {param_id}"
                )

            # Assemble payload fields
            leading = "00"  # always 2 hex
            param_id_hex = f"{int(param_id, 16):04X}"  # 4 hex, upper, zero-padded

            # data_type (6 hex): always from schema, zero-padded to 6 hex
            data_type_hex = f"00{data_type}"
            value_hex = f"{value_scaled:08X}"
            min_hex = f"{min_val_scaled:08X}"
            max_hex = f"{max_val_scaled:08X}"
            precision_hex = f"{precision_scaled:08X}"

            _LOGGER.debug(
                f"set_fan_param: value={value}, min={min_val}, max={max_val}, precision={precision}"
                f"\n  Scaled: value={value_scaled} (0x{value_hex}), min={min_val_scaled} (0x{min_hex}), "
                f"max={max_val_scaled} (0x{max_hex}), precision={precision_scaled} (0x{precision_hex})"
            )

            # Final field order: 2+4+4+8+8+8+8+4 = 46 hex -> 23 bytes
            payload = (
                f"{leading}"
                f"{param_id_hex}"
                f"{data_type_hex}"
                f"{value_hex}"
                f"{min_hex}"
                f"{max_hex}"
                f"{precision_hex}"
                f"{trailer}"
            )
            payload = "".join(payload)
            _LOGGER.debug(
                f"set_fan_param: Final frame: {W_} --- {src_id} {fan_id} --:------ 2411 {len(payload):03d} {payload}"
            )

            # Create the command with exactly 2 addresses: from_id and fan_id
            return cls._from_attrs(
                W_,
                Code._2411,
                payload,
                addr0=src_id,
                addr1=fan_id,
                addr2=NON_DEV_ADDR.id,
            )

        except (ValueError, TypeError) as err:
            raise exc.CommandInvalid(f"Invalid value: {value}") from err

    @classmethod  # constructor for RQ|2411
    def get_fan_param(
        cls,
        fan_id: DeviceIdT | str,
        param_id: str,
        *,
        src_id: DeviceIdT | str,
    ) -> Command:
        """Create a command to get a fan parameter value.

        This method constructs a command to read a specific parameter from a fan device
        using the RAMSES-II 2411 command. The parameter ID must be a valid 2-character
        hexadecimal string (00-FF).

        :param fan_id: The device ID of the target fan (e.g., '01:123456')
        :type fan_id: DeviceIdT | str
        :param param_id: The parameter ID to read (2-character hex string, e.g., '4E')
        :type param_id: str
        :param src_id: The source device ID that will send the command
        :type src_id: DeviceIdT | str
        :return: A Command object for the RQ|2411 message
        :rtype: Command
        :raises CommandInvalid: If the parameter ID is invalid (None, wrong type, wrong format)

        .. note::
            For a complete working example, see the `test_get_fan_param.py` test file
            which demonstrates:
            - Setting up the gateway
            - Sending the command
            - Handling the response
            - Proper error handling

        .. warning::
            The parameter ID must be a valid 2-character hexadecimal string (00-FF).
            The following will raise CommandInvalid:
            - None value
            - Non-string types
            - Leading/trailing whitespace
            - Incorrect length (not 2 characters)
            - Non-hexadecimal characters
        """
        if param_id is None:
            raise exc.CommandInvalid("Parameter ID cannot be None")

        if not isinstance(param_id, str):
            raise exc.CommandInvalid(
                f"Parameter ID must be a string, got {type(param_id).__name__}"
            )

        param_id_stripped = param_id.strip()
        if param_id != param_id_stripped:
            raise exc.CommandInvalid(
                f"Parameter ID cannot have leading or trailing whitespace: '{param_id}'"
            )

        # validate the string format
        try:
            if len(param_id) != 2:
                raise ValueError("Invalid length")
            int(param_id, 16)  # Will raise ValueError if not valid hex
        except ValueError as err:
            raise exc.CommandInvalid(
                f"Invalid parameter ID: '{param_id}'. Must be a 2-character hex string (00-FF)."
            ) from err

        payload = f"0000{param_id.upper()}"  # Convert to uppercase for consistency
        _LOGGER.debug(
            "Created get_fan_param command for %s from %s to %s",
            param_id,
            src_id,
            fan_id,
        )

        return cls._from_attrs(RQ, Code._2411, payload, addr0=src_id, addr1=fan_id)

    @classmethod  # constructor for RQ|2E04
    def get_system_mode(cls, ctl_id: DeviceIdT | str) -> Command:
        """Constructor to get the mode of a system (c.f. parser_2e04)."""

        return cls.from_attrs(RQ, ctl_id, Code._2E04, FF)

    @classmethod  # constructor for W|2E04
    def set_system_mode(
        cls,
        ctl_id: DeviceIdT | str,
        system_mode: int | str | None,
        *,
        until: dt | str | None = None,
    ) -> Command:
        """Set or reset the operating mode of the HVAC system. (c.f. parser_2e04)

        This method constructs a command to change the system-wide operating mode,
        such as switching between heating modes or setting a temporary override.

        :param ctl_id: The device ID of the controller (e.g., '01:123456')
        :type ctl_id: DeviceIdT | str
        :param system_mode: The desired system mode. Can be specified as:
            - Integer: Numeric mode code (0-5)
            - String: Mode name (e.g., 'auto', 'heat_eco')
            - Hex string: Two-character hex code (e.g., '00' for auto)
            If None, defaults to 'auto' mode.
        :type system_mode: int | str | None
        :param until: Optional timestamp when the mode should revert.
            Required for temporary modes like 'eco' or 'advanced'.
            Not allowed for 'auto' or 'heat_off' modes.
        :type until: datetime | str | None
        :return: A configured Command object ready to be sent to the device.
        :rtype: Command
        :raises CommandInvalid: If the combination of mode and until is invalid.
        :raises KeyError: If an invalid mode is specified.

        .. note::
            Available modes are defined in SYS_MODE_MAP and typically include:
            - 'auto': System follows the schedule (code '00')
            - 'heat_off': Heating disabled (code '04')
            - 'eco': Reduced temperature mode (code '01')
            - 'advanced': Custom temperature mode (code '02')
            - 'holiday': Away mode (code '03')
            - 'custom': Custom mode (code '05')

            When using temporary modes (eco/advanced), the 'until' parameter
            must be provided. The system will automatically revert to the
            schedule when the time elapses.
        """

        if system_mode is None:
            system_mode = SYS_MODE_MAP.AUTO
        if isinstance(system_mode, int):
            system_mode = f"{system_mode:02X}"
        if system_mode not in SYS_MODE_MAP:
            system_mode = SYS_MODE_MAP._hex(system_mode)  # may raise KeyError

        if until is not None and system_mode in (
            SYS_MODE_MAP.AUTO,
            SYS_MODE_MAP.AUTO_WITH_RESET,
            SYS_MODE_MAP.HEAT_OFF,
        ):
            raise exc.CommandInvalid(
                f"Invalid args: For system_mode={SYS_MODE_MAP[system_mode]},"
                " until must be None"
            )

        assert isinstance(system_mode, str)  # mypy hint

        payload = "".join(
            (
                system_mode,
                hex_from_dtm(until),
                "00" if until is None else "01",
            )
        )

        return cls.from_attrs(W_, ctl_id, Code._2E04, payload)

    @classmethod  # constructor for I|2E10
    def put_presence_detected(
        cls, dev_id: DeviceIdT | str, presence_detected: bool | None
    ) -> Command:
        """Announce the current presence detection state from a sensor. (c.f. parser_2e10)
        # .I --- ...

        This method constructs an I-type (unsolicited) command to report the
        presence detection state from a presence sensor to the system.

        :param dev_id: The device ID of the presence sensor (e.g., '01:123456')
        :type dev_id: DeviceIdT | str
        :param presence_detected: The current presence state:
            - True: Presence detected
            - False: No presence detected
            - None: Sensor state unknown/error
        :type presence_detected: bool | None
        :return: A configured Command object ready to be sent to the system.
        :rtype: Command

        .. note::
            This is typically used by presence sensors to report their state
            to the HVAC system. The system may use this information for
            occupancy-based control strategies.

            The command uses the 2E10 code, which is specifically designed
            for presence/occupancy reporting in the RAMSES-II protocol.
        """
        payload = f"00{hex_from_bool(presence_detected)}"
        return cls._from_attrs(I_, Code._2E10, payload, addr0=dev_id, addr2=dev_id)

    @classmethod  # constructor for RQ|30C9
    def get_zone_temp(cls, ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT) -> Command:
        """Request the current temperature reading for a specific zone. (c.f. parser_30c9)

        This method constructs a command to request the current temperature
        from a zone's temperature sensor. The response will include the current
        temperature in degrees Celsius with 0.1°C resolution.

        :param ctl_id: The device ID of the controller managing the zone (e.g., '01:123456')
        :type ctl_id: DeviceIdT | str
        :param zone_idx: The index of the zone to query. Can be specified as:
            - Integer (0-31)
            - Hex string ('00'-'1F')
            - String representation of integer ('0'-'31')
        :type zone_idx: _ZoneIdxT
        :return: A configured Command object that can be sent to the device.
        :rtype: Command
        :raises ValueError: If the zone index is out of valid range (0-31)

        .. note::
            The zone index is 0-based. For example:
            - 0 = Zone 1 (typically main living area)
            - 1 = Zone 2 (e.g., bedrooms)
            - And so on up to zone 32

            The actual number of available zones depends on the controller configuration.
            Requesting a non-existent zone will typically result in no response.
        """
        return cls.from_attrs(RQ, ctl_id, Code._30C9, _check_idx(zone_idx))

    @classmethod  # constructor for I|30C9  # TODO: trap corrupt temps?
    def put_sensor_temp(
        cls, dev_id: DeviceIdT | str, temperature: float | None
    ) -> Command:
        """Announce the current temperature reading from a thermostat. (c.f. parser_30c9)
        This is for use by a faked DTS92(E) or similar.

        This method constructs an I-type (unsolicited) command to report the current
        temperature from a thermostat or temperature sensor to the system. This is
        typically used to simulate a physical thermostat's temperature reporting.

        :param dev_id: The device ID of the thermostat or sensor (e.g., '01:123456')
        :type dev_id: DeviceIdT | str
        :param temperature: The current temperature in degrees Celsius.
            Use None to indicate a sensor error or invalid reading.
            The valid range is typically 0-40°C, but this may vary by device.
        :type temperature: float | None
        :return: A configured Command object ready to be sent to the system.
        :rtype: Command

        .. note::
            This is primarily used for testing or simulating thermostats like the DTS92(E).
            The temperature is transmitted with 0.1°C resolution.

            The command uses the 30C9 code, which is used by thermostats to report
            their current temperature reading to the controller.

            When temperature is None, it typically indicates a sensor fault or
            invalid reading, which the system may interpret as a maintenance alert.
        """
        # .I --- 34:021943 --:------ 34:021943 30C9 003 000C0D

        if dev_id[:2] not in (
            DEV_TYPE_MAP.TR0,  # 00
            DEV_TYPE_MAP.HCW,  # 03
            DEV_TYPE_MAP.TRV,  # 04
            DEV_TYPE_MAP.DTS,  # 12
            DEV_TYPE_MAP.DT2,  # 22
            DEV_TYPE_MAP.RND,  # 34
        ):
            raise exc.CommandInvalid(
                f"Faked device {dev_id} has an unsupported device type: "
                f"device_id should be like {DEV_TYPE_MAP.HCW}:xxxxxx"
            )

        payload = f"00{hex_from_temp(temperature)}"
        return cls._from_attrs(I_, Code._30C9, payload, addr0=dev_id, addr2=dev_id)

    @classmethod  # constructor for RQ|313F
    def get_system_time(cls, ctl_id: DeviceIdT | str) -> Command:
        """Constructor to get the datetime of a system (c.f. parser_313f)."""

        return cls.from_attrs(RQ, ctl_id, Code._313F, "00")

    @classmethod  # constructor for W|313F
    def set_system_time(
        cls,
        ctl_id: DeviceIdT | str,
        datetime: dt | str,
        is_dst: bool = False,
    ) -> Command:
        """Constructor to set the datetime of a system (c.f. parser_313f)."""
        # .W --- 30:185469 01:037519 --:------ 313F 009 0060003A0C1B0107E5

        dt_str = hex_from_dtm(datetime, is_dst=is_dst, incl_seconds=True)
        return cls.from_attrs(W_, ctl_id, Code._313F, f"0060{dt_str}")

    @classmethod  # constructor for I|31DA
    def get_hvac_fan_31da(
        cls,
        dev_id: DeviceIdT | str,
        hvac_id: str,
        bypass_position: float | None,
        air_quality: int | None,
        co2_level: int | None,
        indoor_humidity: float | None,
        outdoor_humidity: float | None,
        exhaust_temp: float | None,
        supply_temp: float | None,
        indoor_temp: float | None,
        outdoor_temp: float | None,
        speed_capabilities: list[str],
        fan_info: str,
        _unknown_fan_info_flags: list[int],  # skip? as starts with _
        exhaust_fan_speed: float | None,
        supply_fan_speed: float | None,
        remaining_mins: int | None,
        post_heat: int | None,
        pre_heat: int | None,
        supply_flow: float | None,
        exhaust_flow: float | None,
        **kwargs: Any,  # option: air_quality_basis: str | None,
    ) -> Command:
        """Construct an I|31DA command for HVAC fan status updates.

        This method creates an unsolicited status update command for HVAC fan systems,
        reporting various sensor readings and system states.

        :param dev_id: The device ID of the HVAC controller
        :type dev_id: DeviceIdT | str
        :param hvac_id: The ID of the HVAC unit
        :type hvac_id: str
        :param bypass_position: Current bypass damper position (0.0-1.0)
        :type bypass_position: float | None
        :param air_quality: Current air quality reading
        :type air_quality: int | None
        :param co2_level: Current CO₂ level in ppm
        :type co2_level: int | None
        :param indoor_humidity: Current indoor relative humidity (0.0-1.0)
        :type indoor_humidity: float | None
        :param outdoor_humidity: Current outdoor relative humidity (0.0-1.0)
        :type outdoor_humidity: float | None
        :param exhaust_temp: Current exhaust air temperature in °C
        :type exhaust_temp: float | None
        :param supply_temp: Current supply air temperature in °C
        :type supply_temp: float | None
        :param indoor_temp: Current indoor temperature in °C
        :type indoor_temp: float | None
        :param outdoor_temp: Current outdoor temperature in °C
        :type outdoor_temp: float | None
        :param speed_capabilities: List of supported fan speed settings
        :type speed_capabilities: list[str]
        :param fan_info: Current fan mode/status information
        :type fan_info: str
        :param _unknown_fan_info_flags: Internal flags (reserved for future use)
        :type _unknown_fan_info_flags: list[int]
        :param exhaust_fan_speed: Current exhaust fan speed (0.0-1.0)
        :type exhaust_fan_speed: float | None
        :param supply_fan_speed: Current supply fan speed (0.0-1.0)
        :type supply_fan_speed: float | None
        :param remaining_mins: Remaining time in current mode (minutes)
        :type remaining_mins: int | None
        :param post_heat: Post-heat status/level
        :type post_heat: int | None
        :param pre_heat: Pre-heat status/level
        :type pre_heat: int | None
        :param supply_flow: Current supply air flow rate (if available)
        :type supply_flow: float | None
        :param exhaust_flow: Current exhaust air flow rate (if available)
        :type exhaust_flow: float | None
        :param **kwargs: Additional parameters (reserved for future use)
        :return: A configured Command object for the HVAC fan status update
        :rtype: Command

        .. note::
            This command is typically sent periodically by the HVAC controller to report
            current system status. All parameters are optional, but providing complete
            information will result in more accurate system monitoring and control.
        """
        # 00 EF00 7FFF 34 33 0898 0898 088A 0882 F800 00 15 14 14 0000 EF EF 05F5 0613:
        # {"hvac_id": '00', 'bypass_position': 0.000, 'air_quality': None,
        # 'co2_level': None, 'indoor_humidity': 0.52, 'outdoor_humidity': 0.51,
        # 'exhaust_temp': 22.0, 'supply_temp': 22.0, 'indoor_temp': 21.86,
        # 'outdoor_temp': 21.78, 'speed_capabilities': ['off', 'low_med_high',
        # 'timer', 'boost', 'auto'], 'fan_info': 'away',
        # '_unknown_fan_info_flags': [0, 0, 0], 'exhaust_fan_speed': 0.1,
        # 'supply_fan_speed': 0.1, 'remaining_mins': 0, 'post_heat': None,
        # 'pre_heat': None, 'supply_flow': 15.25, 'exhaust_flow': 15.55},

        air_quality_basis: str = kwargs.pop("air_quality_basis", "00")
        extra: str = kwargs.pop("_extra", "")
        assert not kwargs, kwargs

        payload = hvac_id
        payload += (
            f"{(int(air_quality * 200)):02X}" if air_quality is not None else "EF"
        )
        payload += (
            f"{air_quality_code(air_quality_basis)}"
            if air_quality_basis is not None
            else "00"
        )
        payload += f"{co2_level:04X}" if co2_level is not None else "7FFF"
        payload += (
            hex_from_percent(indoor_humidity, high_res=False)
            if indoor_humidity is not None
            else "EF"
        )
        payload += (
            hex_from_percent(outdoor_humidity, high_res=False)
            if outdoor_humidity is not None
            else "EF"
        )
        payload += hex_from_temp(exhaust_temp) if exhaust_temp is not None else "7FFF"
        payload += hex_from_temp(supply_temp) if supply_temp is not None else "7FFF"
        payload += hex_from_temp(indoor_temp) if indoor_temp is not None else "7FFF"
        payload += hex_from_temp(outdoor_temp) if outdoor_temp is not None else "7FFF"
        payload += (
            f"{capability_bits(speed_capabilities):04X}"
            if speed_capabilities is not None
            else "7FFF"
        )
        payload += (
            hex_from_percent(bypass_position, high_res=True)
            if bypass_position is not None
            else "EF"
        )
        payload += (
            f"{(fan_info_to_byte(fan_info) | fan_info_flags(_unknown_fan_info_flags)):02X}"
            if fan_info is not None
            else "EF"
        )
        payload += (
            hex_from_percent(exhaust_fan_speed, high_res=True)
            if exhaust_fan_speed is not None
            else "FF"
        )
        payload += (
            hex_from_percent(supply_fan_speed, high_res=True)
            if supply_fan_speed is not None
            else "FF"
        )
        payload += f"{remaining_mins:04X}" if remaining_mins is not None else "7FFF"
        payload += f"{int(post_heat * 200):02X}" if post_heat is not None else "EF"
        payload += f"{int(pre_heat * 200):02X}" if pre_heat is not None else "EF"
        payload += (
            f"{(int(supply_flow * 100)):04X}" if supply_flow is not None else "7FFF"
        )
        payload += (
            f"{(int(exhaust_flow * 100)):04X}" if exhaust_flow is not None else "7FFF"
        )
        payload += extra

        return cls._from_attrs(I_, Code._31DA, payload, addr0=dev_id, addr2=dev_id)

    @classmethod  # constructor for RQ|3220
    def get_opentherm_data(cls, otb_id: DeviceIdT | str, msg_id: int | str) -> Command:
        """Request OpenTherm protocol data from a device. (c.f. parser_3220)

        This method constructs a command to request data from an OpenTherm compatible
        device using the OpenTherm protocol. It sends a Read-Data request for a
        specific data ID to the target device.

        :param otb_id: The device ID of the OpenTherm bridge/controller
        :type otb_id: DeviceIdT | str
        :param msg_id: The OpenTherm message ID to request. Can be specified as:
            - Integer (e.g., 0 for Status)
            - Hex string (e.g., '00' for Status)
            See OpenTherm specification for valid message IDs.
        :type msg_id: int | str
        :return: A configured Command object ready to be sent to the device.
        :rtype: Command

        .. note::
            The OpenTherm protocol is used for communication between heating systems
            and thermostats. Common message IDs include:
            - 0x00: Status (0x00)
            - 0x01: Control setpoint (0x01)
            - 0x11: Relative modulation level (0x11)
            - 0x12: CH water pressure (0x12)
            - 0x19: Boiler water temperature (0x19)
            - 0x1A: DHW temperature (0x1A)
            - 0x71: DHW setpoint (0x71)

            The response will contain the requested data in the OpenTherm format,
            which includes status flags and the data value.

            The command automatically handles the parity bit required by the
            OpenTherm protocol.
        """
        msg_id = msg_id if isinstance(msg_id, int) else int(msg_id, 16)
        payload = f"0080{msg_id:02X}0000" if parity(msg_id) else f"0000{msg_id:02X}0000"
        return cls.from_attrs(RQ, otb_id, Code._3220, payload)

    @classmethod  # constructor for I|3EF0  # TODO: trap corrupt states?
    def put_actuator_state(
        cls, dev_id: DeviceIdT | str, modulation_level: float
    ) -> Command:
        """Announce the current modulation level of a heating actuator. (c.f. parser_3ef0)
        This is for use by a faked BDR91A or similar.

        This method constructs an I-type (unsolicited) command to report the current
        modulation level of a heating actuator, such as a BDR91A relay. The modulation
        level represents the current output state of the actuator as a percentage.

        :param dev_id: The device ID of the actuator (e.g., '13:123456').
            Must be a device type compatible with BDR91A.
        :type dev_id: DeviceIdT | str
        :param modulation_level: The current modulation level as a float between 0.0 and 1.0.
            - 0.0: Actuator is fully off
            - 1.0: Actuator is fully on
            - Values in between represent partial modulation (if supported)
            - None: Indicates an error or unknown state
        :type modulation_level: float | None
        :return: A configured Command object ready to be sent to the system.
        :rtype: Command
        :raises CommandInvalid: If the device ID is not a valid BDR-type device.

        .. note::
            This is primarily used for testing or simulating BDR91A relay modules.
            The modulation level is converted to a percentage (0-100%) with 0.5% resolution.

            The command uses the 3EF0 code, which is specifically designed for
            reporting actuator states in the RAMSES-II protocol.
        """
        # .I --- 13:049798 --:------ 13:049798 3EF0 003 00C8FF
        # .I --- 13:106039 --:------ 13:106039 3EF0 003 0000FF

        if dev_id[:2] != DEV_TYPE_MAP.BDR:
            raise exc.CommandInvalid(
                f"Faked device {dev_id} has an unsupported device type: "
                f"device_id should be like {DEV_TYPE_MAP.BDR}:xxxxxx"
            )

        payload = (
            "007FFF"
            if modulation_level is None
            else f"00{int(modulation_level * 200):02X}FF"
        )
        return cls._from_attrs(I_, Code._3EF0, payload, addr0=dev_id, addr2=dev_id)

    @classmethod  # constructor for RP|3EF1 (I|3EF1?)  # TODO: trap corrupt values?
    def put_actuator_cycle(
        cls,
        src_id: DeviceIdT | str,
        dst_id: DeviceIdT | str,
        modulation_level: float,
        actuator_countdown: int,
        *,
        cycle_countdown: int | None = None,
    ) -> Command:
        """Announce the internal cycling state of a heating actuator. (c.f. parser_3ef1)
        This is for use by a faked BDR91A or similar.

        This method constructs an RP-type (request/response) command to report the
        internal cycling state of a heating actuator, such as a BDR91A relay. It provides
        detailed timing information about the actuator's modulation cycle.

        :param src_id: The device ID of the actuator sending the report (e.g., '13:123456').
            Must be a device type compatible with BDR91A.
        :type src_id: DeviceIdT | str
        :param dst_id: The device ID of the intended recipient of this report.
        :type dst_id: DeviceIdT | str
        :param modulation_level: The current modulation level as a float between 0.0 and 1.0.
            - 0.0: Actuator is fully off
            - 1.0: Actuator is fully on
            - Values in between represent partial modulation (if supported)
        :type modulation_level: float
        :param actuator_countdown: Time in seconds until the next actuator cycle state change.
            This is used for PWM (Pulse Width Modulation) control.
        :type actuator_countdown: int
        :param cycle_countdown: Optional time in seconds until the next complete cycle.
            If None, indicates the cycle is not currently active.
        :type cycle_countdown: int | None
        :return: A configured Command object ready to be sent to the system.
        :rtype: Command
        :raises CommandInvalid: If the source device ID is not a valid BDR-type device.

        .. note::
            This is primarily used for testing or simulating BDR91A relay modules.
            The method automatically handles the conversion of timing values to the
            appropriate hexadecimal format required by the RAMSES-II protocol.

            The command uses the 3EF1 code, which is specifically designed for
            reporting detailed actuator cycling information.
        """
        # RP --- 13:049798 18:006402 --:------ 3EF1 007 00-0126-0126-00-FF

        if src_id[:2] != DEV_TYPE_MAP.BDR:
            raise exc.CommandInvalid(
                f"Faked device {src_id} has an unsupported device type: "
                f"device_id should be like {DEV_TYPE_MAP.BDR}:xxxxxx"
            )

        payload = "00"
        payload += f"{cycle_countdown:04X}" if cycle_countdown is not None else "7FFF"
        payload += f"{actuator_countdown:04X}"
        payload += hex_from_percent(modulation_level)
        payload += "FF"
        return cls._from_attrs(RP, Code._3EF1, payload, addr0=src_id, addr1=dst_id)

    @classmethod  # constructor for internal use only
    def _puzzle(cls, msg_type: str | None = None, message: str = "") -> Command:
        """Construct a puzzle command used for device discovery and version reporting.

        This internal method creates a special 'puzzle' command used during device
        discovery and version reporting. The command format varies based on the
        message type and content.

        :param msg_type: The type of puzzle message to create. If None, it will be
            automatically determined based on the presence of a message:
            - '10': Version request (empty message)
            - '12': Version response (with message)
            Other valid types include '11' and '13' for specific message formats,
            and '20' and above for timestamp-based messages.
        :type msg_type: str | None
        :param message: The message content to include in the puzzle.
            Format depends on msg_type:
            - For type '10': Should be empty (version request)
            - For type '11': Should be a 10-character string (MAC address)
            - For type '12': Version string (e.g., 'v0.20.0')
            - For other types: Arbitrary message content
        :type message: str
        :return: A configured Command object with the puzzle message.
        :rtype: Command
        :raises AssertionError: If msg_type is not in LOOKUP_PUZZ.

        .. note::
            This is an internal method used by the RAMSES-II protocol for device
            discovery and version reporting. The message format varies:

            - Type '10': Version request (empty message)
            - Type '11': MAC address report (special format)
            - Type '12': Version response (includes version string)
            - Type '13': Basic message (no timestamp)
            - Type '20+': Timestamped message (high precision)

            The method automatically handles timestamp generation and message
            formatting based on the message type.
        """
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

        return cls.from_attrs(I_, ALL_DEV_ADDR.id, Code._PUZZ, payload[:48])


# A convenience dict
CODE_API_MAP = {
    f"{RP}|{Code._3EF1}": Command.put_actuator_cycle,  # .   has a test (RP, not I)
    f"{I_}|{Code._3EF0}": Command.put_actuator_state,
    f"{I_}|{Code._1FC9}": Command.put_bind,
    f"{W_}|{Code._1FC9}": Command.put_bind,  # NOTE: same class method as I|1FC9
    f"{W_}|{Code._22F7}": Command.set_bypass_position,
    f"{I_}|{Code._1298}": Command.put_co2_level,  # .         has a test
    f"{RQ}|{Code._1F41}": Command.get_dhw_mode,
    f"{W_}|{Code._1F41}": Command.set_dhw_mode,  # .          has a test
    f"{RQ}|{Code._10A0}": Command.get_dhw_params,
    f"{W_}|{Code._10A0}": Command.set_dhw_params,  # .        has a test
    f"{RQ}|{Code._1260}": Command.get_dhw_temp,
    f"{I_}|{Code._1260}": Command.put_dhw_temp,  # .          has a test (empty)
    f"{I_}|{Code._22F1}": Command.set_fan_mode,
    f"{W_}|{Code._2411}": Command.set_fan_param,
    f"{I_}|{Code._12A0}": Command.put_indoor_humidity,  # .   has a test
    f"{RQ}|{Code._1030}": Command.get_mix_valve_params,
    f"{W_}|{Code._1030}": Command.set_mix_valve_params,  # .  has a test
    f"{RQ}|{Code._3220}": Command.get_opentherm_data,
    f"{I_}|{Code._1290}": Command.put_outdoor_temp,
    f"{I_}|{Code._2E10}": Command.put_presence_detected,
    f"{RQ}|{Code._0008}": Command.get_relay_demand,
    f"{RQ}|{Code._0404}": Command.get_schedule_fragment,  # . has a test
    f"{W_}|{Code._0404}": Command.set_schedule_fragment,
    f"{RQ}|{Code._0006}": Command.get_schedule_version,
    f"{I_}|{Code._30C9}": Command.put_sensor_temp,  # .       has a test
    f"{RQ}|{Code._0100}": Command.get_system_language,
    f"{RQ}|{Code._0418}": Command.get_system_log_entry,
    f"{RQ}|{Code._2E04}": Command.get_system_mode,  # .       has a test
    f"{W_}|{Code._2E04}": Command.set_system_mode,
    f"{RQ}|{Code._313F}": Command.get_system_time,
    f"{W_}|{Code._313F}": Command.set_system_time,  # .       has a test
    f"{RQ}|{Code._1100}": Command.get_tpi_params,
    f"{W_}|{Code._1100}": Command.set_tpi_params,  # .        has a test
    f"{I_}|{Code._0002}": Command.put_weather_temp,
    f"{RQ}|{Code._000A}": Command.get_zone_config,
    f"{W_}|{Code._000A}": Command.set_zone_config,  # .       has a test
    f"{RQ}|{Code._2349}": Command.get_zone_mode,
    f"{W_}|{Code._2349}": Command.set_zone_mode,  # .         has a test
    f"{RQ}|{Code._0004}": Command.get_zone_name,
    f"{W_}|{Code._0004}": Command.set_zone_name,  # .         has a test
    f"{RQ}|{Code._2309}": Command.get_zone_setpoint,
    f"{W_}|{Code._2309}": Command.set_zone_setpoint,  # .     has a test
    f"{RQ}|{Code._30C9}": Command.get_zone_temp,
    f"{RQ}|{Code._12B0}": Command.get_zone_window_state,
    f"{I_}|{Code._31DA}": Command.get_hvac_fan_31da,  # .     has a test
}  # TODO: RQ|0404 (Zone & DHW)
