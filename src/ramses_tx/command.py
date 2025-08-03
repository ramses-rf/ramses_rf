#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Construct a command (packet that is to be sent).
"""

from __future__ import annotations

import logging
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
from .ramses import _2411_PARAMS_SCHEMA
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
    """Validate the zone_mode, and return a it as a normalised 2-byte code.

    Used by set_dhw_mode (target=active) and set_zone_mode (target=setpoint).
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
    """Validate until and duration, and return a normalised xxx.

    Used by set_dhw_mode and set_zone_mode.
    """
    # if until and duration:
    #     raise exc.CommandInvalid(
    #         "Invalid args: Only one of until or duration can be set"
    #     )

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

    return until, duration


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
        """Constructor to get the name of a zone (c.f. parser_0004)."""

        return cls.from_attrs(RQ, ctl_id, Code._0004, f"{_check_idx(zone_idx)}00")

    @classmethod  # constructor for W|0004
    def set_zone_name(
        cls, ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT, name: str
    ) -> Command:
        """Constructor to set the name of a zone (c.f. parser_0004)."""

        payload = f"{_check_idx(zone_idx)}00{hex_from_str(name)[:40]:0<40}"
        return cls.from_attrs(W_, ctl_id, Code._0004, payload)

    @classmethod  # constructor for RQ|0006
    def get_schedule_version(cls, ctl_id: DeviceIdT | str) -> Command:
        """Constructor to get the current version (change counter) of the schedules.

        This number is increased whenever any zone's schedule is changed (incl. the DHW
        zone), and is used to avoid the relatively large expense of downloading a
        schedule, only to see that it hasn't changed.
        """

        return cls.from_attrs(RQ, ctl_id, Code._0006, "00")

    @classmethod  # constructor for RQ|0008
    def get_relay_demand(
        cls, dev_id: DeviceIdT | str, zone_idx: _ZoneIdxT | None = None
    ) -> Command:
        """Constructor to get the demand of a relay/zone (c.f. parser_0008)."""

        payload = "00" if zone_idx is None else _check_idx(zone_idx)
        return cls.from_attrs(RQ, dev_id, Code._0008, payload)

    @classmethod  # constructor for RQ|000A
    def get_zone_config(cls, ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT) -> Command:
        """Constructor to get the config of a zone (c.f. parser_000a)."""

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
        """Constructor to set the config of a zone (c.f. parser_000a)."""

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
        """Constructor to get the language of a system (c.f. parser_0100)."""

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
        """Constructor to get a schedule fragment (c.f. parser_0404).

        Usually a zone, but will be the DHW schedule if zone_idx == 0xFA, 'FA', or 'HW'.
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
        """Constructor to set a zone schedule fragment (c.f. parser_0404).

        Usually a zone, but will be the DHW schedule if zone_idx == 0xFA, 'FA', or 'HW'.
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
        """Constructor to get a log entry from a system (c.f. parser_0418)."""

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
        """Constructor to get a log entry from a system (c.f. parser_0418)."""

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
        """Constructor to get the mix valve params of a zone (c.f. parser_1030)."""

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
        """Constructor to set the mix valve params of a zone (c.f. parser_1030)."""

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
        """Constructor to get the params of the DHW (c.f. parser_10a0)."""

        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, kwargs

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
        """Constructor to set the params of the DHW (c.f. parser_10a0)."""
        # Defaults for newer evohome colour:
        # Defaults for older evohome colour: ?? (30-85) C, ? (0-10) min, ? (1-10) C
        # Defaults for evohome monochrome:

        # 14:34:26.734 022  W --- 18:013393 01:145038 --:------ 10A0 006 000F6E050064
        # 14:34:26.751 073  I --- 01:145038 --:------ 01:145038 10A0 006 000F6E0003E8
        # 14:34:26.764 074  I --- 01:145038 18:013393 --:------ 10A0 006 000F6E0003E8

        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, kwargs

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
        """Constructor to get the TPI params of a system (c.f. parser_1100)."""

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
        """Constructor to set the TPI params of a system (c.f. parser_1100)."""

        if domain_id is None:
            domain_id = "00"

        # assert cycle_rate is None or cycle_rate in (3, 6, 9, 12), cycle_rate
        # assert min_on_time is None or 1 <= min_on_time <= 5, min_on_time
        # assert min_off_time is None or 1 <= min_off_time <= 5, min_off_time
        # assert (
        #     proportional_band_width is None or 1.5 <= proportional_band_width <= 3.0
        # ), proportional_band_width

        payload = "".join(
            (
                _check_idx(domain_id),
                f"{cycle_rate * 4:02X}",
                f"{int(min_on_time * 4):02X}",
                f"{int(min_off_time * 4):02X}00",  # or: ...FF",
                f"{hex_from_temp(proportional_band_width)}01",
            )
        )

        return cls.from_attrs(W_, ctl_id, Code._1100, payload)

    @classmethod  # constructor for RQ|1260
    def get_dhw_temp(cls, ctl_id: DeviceIdT | str, **kwargs: Any) -> Command:
        """Constructor to get the temperature of the DHW sensor (c.f. parser_10a0)."""

        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, kwargs

        return cls.from_attrs(RQ, ctl_id, Code._1260, dhw_idx)

    @classmethod  # constructor for I|1260  # TODO: trap corrupt temps?
    def put_dhw_temp(
        cls, dev_id: DeviceIdT | str, temperature: float | None, **kwargs: Any
    ) -> Command:
        """Constructor to announce the current temperature of an DHW sensor (1260).

        This is for use by a faked CS92A or similar.
        """

        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, kwargs

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
        """Constructor to announce the current outdoor temperature (1290).

        This is for use by a faked HVAC sensor or similar.
        """

        payload = f"00{hex_from_temp(temperature)}"
        return cls._from_attrs(I_, Code._1290, payload, addr0=dev_id, addr2=dev_id)

    @classmethod  # constructor for I|1298
    def put_co2_level(cls, dev_id: DeviceIdT | str, co2_level: float | None) -> Command:
        """Constructor to announce the current co2 level of a sensor (1298)."""
        # .I --- 37:039266 --:------ 37:039266 1298 003 000316

        payload = f"00{hex_from_double(co2_level)}"
        return cls._from_attrs(I_, Code._1298, payload, addr0=dev_id, addr2=dev_id)

    @classmethod  # constructor for I|12A0
    def put_indoor_humidity(
        cls, dev_id: DeviceIdT | str, indoor_humidity: float | None
    ) -> Command:
        """Constructor to announce the current humidity of a sensor (12A0)."""
        # .I --- 37:039266 --:------ 37:039266 1298 003 000316

        payload = "00" + hex_from_percent(indoor_humidity, high_res=False)
        return cls._from_attrs(I_, Code._12A0, payload, addr0=dev_id, addr2=dev_id)

    @classmethod  # constructor for RQ|12B0
    def get_zone_window_state(
        cls, ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT
    ) -> Command:
        """Constructor to get the openwindow state of a zone (c.f. parser_12b0)."""

        return cls.from_attrs(RQ, ctl_id, Code._12B0, _check_idx(zone_idx))

    @classmethod  # constructor for RQ|1F41
    def get_dhw_mode(cls, ctl_id: DeviceIdT | str, **kwargs: Any) -> Command:
        """Constructor to get the mode of the DHW (c.f. parser_1f41)."""

        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, kwargs

        return cls.from_attrs(RQ, ctl_id, Code._1F41, dhw_idx)

    @classmethod  # constructor for W|1F41
    def set_dhw_mode(
        cls,
        ctl_id: DeviceIdT | str,
        *,
        mode: int | str | None = None,
        active: bool | None = None,
        until: dt | str | None = None,
        duration: int | None = None,
        **kwargs: Any,
    ) -> Command:
        """Constructor to set/reset the mode of the DHW (c.f. parser_1f41)."""

        dhw_idx = _check_idx(kwargs.pop(SZ_DHW_IDX, 0))  # 00 or 01 (rare)
        assert not kwargs, kwargs

        mode = _normalise_mode(mode, active, until, duration)

        if mode == ZON_MODE_MAP.FOLLOW:
            active = None
        if active is not None and not isinstance(active, bool | int):
            raise exc.CommandInvalid(
                f"Invalid args: active={active}, but must be an bool"
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
        """Constructor for RF bind commands (1FC9), for use by faked devices.

        Expected use-cases:
          FAN bound by CO2 (1298), HUM (12A0), PER (2E10), SWI (22F1, 22F3)
          CTL bound by DHW (1260), RND/THM (30C9)

        Many other bindings are much more complicated than the above, and may require
        bespoke constructors (e.g. TRV binding to a CTL).
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
            assert not kwargs, kwargs
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
        # TODO: should preserve order of codes, else tests may fail
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
        if not codes:  # might be
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
        """Constructor to get the fan speed (and heater?) (c.f. parser_22f1).

        There are two types of this packet seen (with seqn, or with src_id):
         - I 018 --:------ --:------ 39:159057 22F1 003 000204
         - I --- 21:039407 28:126495 --:------ 22F1 003 000407
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
        #  - only payloads seen: '000.0[47A]', may accept '000.'
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
        """Constructor to set the position of the bypass valve (c.f. parser_22f7).

        bypass_position: a % from fully open (1.0) to fully closed (0.0).
        None is a sentinel value for auto.

        bypass_mode: is a proxy for bypass_position (they should be mutex)
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
        """Constructor to get the setpoint of a zone (c.f. parser_2309)."""

        return cls.from_attrs(W_, ctl_id, Code._2309, _check_idx(zone_idx))

    @classmethod  # constructor for W|2309  # TODO: check if setpoint can be None
    def set_zone_setpoint(
        cls, ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT, setpoint: float
    ) -> Command:
        """Constructor to set the setpoint of a zone (c.f. parser_2309)."""
        # .W --- 34:092243 01:145038 --:------ 2309 003 0107D0

        payload = f"{_check_idx(zone_idx)}{hex_from_temp(setpoint)}"
        return cls.from_attrs(W_, ctl_id, Code._2309, payload)

    @classmethod  # constructor for RQ|2349
    def get_zone_mode(cls, ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT) -> Command:
        """Constructor to get the mode of a zone (c.f. parser_2349)."""

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
        duration: int | None = None,
    ) -> Command:
        """Constructor to set/reset the mode of a zone (c.f. parser_2349).

        The setpoint has a resolution of 0.1 C. If a setpoint temperature is required,
        but none is provided, evohome will use the maximum possible value.

        The until has a resolution of 1 min.

        Incompatible combinations:
        - mode == Follow & setpoint not None (will silently ignore setpoint)
        - mode == Temporary & until is None (will silently ignore ???)
        - until and duration are mutually exclusive
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
        value: str,
        *,
        src_id: DeviceIdT | str | None = None,
    ) -> Command:
        """Constructor to set a configurable fan parameter (c.f. parser_2411)."""

        src_id = src_id or fan_id  # TODO: src_id should be an arg?

        if not _2411_PARAMS_SCHEMA.get(param_id):  # TODO: not exclude unknowns?
            raise exc.CommandInvalid(f"Unknown parameter: {param_id}")

        payload = f"0000{param_id}0000{value:08X}"  # TODO: needs work

        return cls._from_attrs(W_, Code._2411, payload, addr0=src_id, addr1=fan_id)

    @classmethod  # constructor for RQ|2411
    def get_fan_param(
        cls,
        fan_id: DeviceIdT | str,
        param_id: str,
        *,
        src_id: DeviceIdT | str,
    ) -> Command:
        """Create a command to get a fan parameter.

        This method constructs a command to read a specific parameter from a fan device using the 2411 command.
        The parameter ID must be a valid 2-character hexadecimal string (00-FF).

        For a complete example of how to use this method in a real application, see the
        `test_get_fan_param.py` test file in the tests directory. The test demonstrates:
        - Setting up the gateway
        - Sending the command
        - Handling the response
        - Proper error handling

        Example:
            # Basic usage:
            cmd = Command.get_fan_param(
                fan_id='01:123456',  # Target fan device ID
                param_id='4E',       # Parameter to read (2-char hex)
                src_id='12:345678'   # Source device ID (e.g., remote)
            )

            # For a complete working example, see:
            # tests/test_get_fan_param.py

        Args:
            fan_id: The device ID of the target fan (e.g., '01:123456')
            param_id: The parameter ID to read (2-character hex string, e.g., '4E')
            src_id: The source device ID that will send the command (e.g., a remote or DIS device ID)

        Returns:
            Command: A Command object for the RQ|2411 message that can be sent to the gateway

        Raises:
            CommandInvalid: If the parameter ID is invalid, including:
                - None value
                - Non-string types
                - Leading/trailing whitespace
                - Incorrect length (not 2 characters)
                - Non-hexadecimal characters
        """
        # Check for None first
        if param_id is None:
            raise exc.CommandInvalid("Parameter ID cannot be None")

        # Check if param_id is a string
        if not isinstance(param_id, str):
            raise exc.CommandInvalid(
                f"Parameter ID must be a string, got {type(param_id).__name__}"
            )

        # Check for leading/trailing whitespace by comparing with stripped version
        param_id_stripped = param_id.strip()
        if param_id != param_id_stripped:
            raise exc.CommandInvalid(
                f"Parameter ID cannot have leading or trailing whitespace: '{param_id}'"
            )

        # Then validate the string format
        try:
            if len(param_id) != 2:
                raise ValueError("Invalid length")
            int(param_id, 16)  # Will raise ValueError if not valid hex
        except ValueError as err:
            raise exc.CommandInvalid(
                f"Invalid parameter ID: '{param_id}'. Must be a 2-character hex string (00-FF)."
            ) from err

        # For RQ, the payload is just the parameter ID with 0000 prefix
        payload = f"0000{param_id.upper()}"  # Convert to uppercase for consistency

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
        """Constructor to set/reset the mode of a system (c.f. parser_2e04)."""

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
        """Constructor to announce the current presence state of a sensor (2E10)."""
        # .I --- ...

        payload = f"00{hex_from_bool(presence_detected)}"
        return cls._from_attrs(I_, Code._2E10, payload, addr0=dev_id, addr2=dev_id)

    @classmethod  # constructor for RQ|30C9
    def get_zone_temp(cls, ctl_id: DeviceIdT | str, zone_idx: _ZoneIdxT) -> Command:
        """Constructor to get the current temperature of a zone (c.f. parser_30c9)."""

        return cls.from_attrs(RQ, ctl_id, Code._30C9, _check_idx(zone_idx))

    @classmethod  # constructor for I|30C9  # TODO: trap corrupt temps?
    def put_sensor_temp(
        cls, dev_id: DeviceIdT | str, temperature: float | None
    ) -> Command:
        """Constructor to announce the current temperature of a thermostat (3C09).

        This is for use by a faked DTS92(E) or similar.
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

    @classmethod  # constructor for RQ|3220
    def get_opentherm_data(cls, otb_id: DeviceIdT | str, msg_id: int | str) -> Command:
        """Constructor to get (Read-Data) opentherm msg value (c.f. parser_3220)."""

        msg_id = msg_id if isinstance(msg_id, int) else int(msg_id, 16)
        payload = f"0080{msg_id:02X}0000" if parity(msg_id) else f"0000{msg_id:02X}0000"
        return cls.from_attrs(RQ, otb_id, Code._3220, payload)

    @classmethod  # constructor for I|3EF0  # TODO: trap corrupt states?
    def put_actuator_state(
        cls, dev_id: DeviceIdT | str, modulation_level: float
    ) -> Command:
        """Constructor to announce the modulation level of an actuator (3EF0).

        This is for use by a faked BDR91A or similar.
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
        """Constructor to announce the internal state of an actuator (3EF1).

        This is for use by a faked BDR91A or similar.
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
    f"{I_}|{Code._1298}": Command.put_co2_level,
    f"{RQ}|{Code._1F41}": Command.get_dhw_mode,
    f"{W_}|{Code._1F41}": Command.set_dhw_mode,  # .          has a test
    f"{RQ}|{Code._10A0}": Command.get_dhw_params,
    f"{W_}|{Code._10A0}": Command.set_dhw_params,  # .        has a test
    f"{RQ}|{Code._1260}": Command.get_dhw_temp,
    f"{I_}|{Code._1260}": Command.put_dhw_temp,  # .          has a test (empty)
    f"{I_}|{Code._22F1}": Command.set_fan_mode,
    f"{W_}|{Code._2411}": Command.set_fan_param,
    f"{RQ}|{Code._2411}": Command.get_fan_param,  # .         has a test
    f"{I_}|{Code._12A0}": Command.put_indoor_humidity,
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
}  # TODO: RQ|0404 (Zone & DHW)
