#!/usr/bin/env python3
"""RAMSES RF - Expose an 0404 schedule (is a stateful process)."""

# TODO: use schemas from evohome_async

from __future__ import annotations

import asyncio
import logging
import struct
import zlib
from collections.abc import Iterable
from datetime import timedelta as td
from typing import TYPE_CHECKING, Final

import voluptuous as vol

from ramses_rf import exceptions as exc
from ramses_rf.const import (
    SZ_FRAG_NUMBER,
    SZ_FRAGMENT,
    SZ_SCHEDULE,
    SZ_TOTAL_FRAGS,
    SZ_ZONE_IDX,
)
from ramses_rf.messages import Message
from ramses_rf.typing import (
    DayOfWeekT,
    EmptyDictT,
    FragmentSetT,
    FragmentT,
    InnerScheduleT,
    OuterSchedule,
    OuterScheduleT,
    PayloadSetT,
    PayloadT,
    SwitchPointDhw,
    SwitchPointsT,
    SwitchPointT,
    SwitchPointZon,
)
from ramses_tx.exceptions import ProtocolSendFailed
from ramses_tx.packet import Packet

from ..enums import Action
from .helpers import send_system_intent

if TYPE_CHECKING:
    from ramses_rf.systems.zones import DhwZone, Zone


# Constants
FIVE_MINS: Final = td(minutes=5)

SZ_MSG: Final = "msg"
SZ_DAY_OF_WEEK: Final = "day_of_week"
SZ_HEAT_SETPOINT: Final = "heat_setpoint"
SZ_SWITCHPOINTS: Final = "switchpoints"
SZ_TIME_OF_DAY: Final = "time_of_day"
SZ_ENABLED: Final = "enabled"

REGEX_TIME_OF_DAY: Final = r"^([0-1][0-9]|2[0-3]):[0-5][05]$"

SWITCHPOINT_STRUCT_SIZE: Final = 20
FRAGMENT_HEX_LENGTH: Final = 82

# 20-byte Schedule Switchpoint binary layout (Little-Endian):
#   Offset  Format  Len  Description                    Sample Hex
#   --------------------------------------------------------------
#   +0       4x     4B   Padding / Header bytes       : 00 00 00 00
#   +4       B      1B   Zone/Domain index (uint8)    : 01
#   +5       3x     3B   Padding bytes                : 00 00 00
#   +8       B      1B   Day of week (uint8, 1-7)     : 01
#   +9       3x     3B   Padding bytes                : 00 00 00
#   +12      H      2B   Time of day (uint16 mins)    : 68 01
#   +14      2x     2B   Padding bytes                : 00 00
#   +16      H      2B   Setpoint value / state (u16) : D0 07
#   +18      H      2B   Reserved / Trailer bytes     : 00 00
#   --------------------------------------------------------------
#   Field-spaced hex : 00000000 01 000000 01 000000 6801 0000 D007 0000
#   Payload hex      : 00000000010000000100000068010000D0070000
CODE_0404_SCHEDULE_SWITCHPOINT_STRUCT: Final[str] = "<xxxxBxxxBxxxHxxHH"


_LOGGER = logging.getLogger(__name__)


# Voluptuous Schemas
def schema_sched(schema_switchpoint: vol.Schema) -> vol.Schema:
    """Generate a voluptuous schema for a weekly schedule array.

    :param schema_switchpoint: The schema describing an individual
        switchpoint.
    :type schema_switchpoint: vol.Schema
    :returns: A voluptuous Schema object for the 7-day schedule array.
    :rtype: vol.Schema
    """
    schema_sched_day = vol.Schema(
        {
            vol.Required(SZ_DAY_OF_WEEK): int,
            vol.Required(SZ_SWITCHPOINTS): vol.All(
                [schema_switchpoint], vol.Length(min=1)
            ),
        },
        extra=vol.PREVENT_EXTRA,
    )
    return vol.Schema(
        vol.All([schema_sched_day], vol.Length(min=0, max=7)),
        extra=vol.PREVENT_EXTRA,
    )


SCH_SWITCHPOINT_DHW = vol.Schema(
    {
        vol.Required(SZ_TIME_OF_DAY): vol.Match(REGEX_TIME_OF_DAY),
        vol.Required(SZ_ENABLED): bool,
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_SWITCHPOINT_ZON = vol.Schema(
    {
        vol.Required(SZ_TIME_OF_DAY): vol.Match(REGEX_TIME_OF_DAY),
        vol.Required(SZ_HEAT_SETPOINT): vol.All(
            vol.Coerce(float), vol.Range(min=5, max=35)
        ),
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_SCHEDULE_DHW = schema_sched(SCH_SWITCHPOINT_DHW)
SCH_SCHEDULE_DHW_OUTER = vol.Schema(
    {
        vol.Required(SZ_ZONE_IDX): "HW",
        vol.Required(SZ_SCHEDULE): SCH_SCHEDULE_DHW,
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_SCHEDULE_ZON = schema_sched(SCH_SWITCHPOINT_ZON)
SCH_SCHEDULE_ZON_OUTER = vol.Schema(
    {
        vol.Required(SZ_ZONE_IDX): vol.Match(r"0[0-F]"),
        vol.Required(SZ_SCHEDULE): SCH_SCHEDULE_ZON,
    },
    extra=vol.PREVENT_EXTRA,
)

SCH_FULL_SCHEDULE = vol.Schema(
    vol.Any(SCH_SCHEDULE_DHW_OUTER, SCH_SCHEDULE_ZON_OUTER),
    extra=vol.PREVENT_EXTRA,
)


# Binary Packing & Serialization Helpers
def _struct_pack(
    zone_idx: str,
    week_day: DayOfWeekT,
    switchpoint: SwitchPointT,
) -> bytes:
    """Pack schedule information into bytes layout for transport.

    :param zone_idx: The hexadecimal zone/domain index string.
    :type zone_idx: str
    :param week_day: The specific day dictionary object.
    :type week_day: DayOfWeekT
    :param switchpoint: The specific switchpoint dictionary object.
    :type switchpoint: SwitchPointT
    :returns: A packed 20-byte struct representing this switchpoint.
    :rtype: bytes
    """
    dow: int = week_day[SZ_DAY_OF_WEEK]
    tod_str: str = switchpoint[SZ_TIME_OF_DAY]

    idx: int = int(zone_idx, 16)
    tod: int = int(tod_str[:2]) * 60 + int(tod_str[3:])

    if (enabled := switchpoint.get("enabled")) is not None:
        val = int(bool(enabled))
    elif isinstance(sp_val := switchpoint.get("heat_setpoint"), (int, float)):
        val = int(sp_val * 100)
    else:
        val = 0

    return struct.pack(
        CODE_0404_SCHEDULE_SWITCHPOINT_STRUCT,
        idx,
        dow,
        tod,
        val,
        0,  # Reserved trailer field (0x0000)
    )


def _struct_unpack(raw_schedule: bytes) -> tuple[int, int, int, int]:
    """Unpack a compressed RAMSES binary schedule format.

    :param raw_schedule: Uncompressed 20-byte block.
    :type raw_schedule: bytes
    :returns: A tuple of (zone_idx, day_of_week, time_of_day, value).
    :rtype: tuple[int, int, int, int]
    """
    idx, dow, tod, val, _ = struct.unpack(
        CODE_0404_SCHEDULE_SWITCHPOINT_STRUCT,
        raw_schedule,
    )
    return idx, dow, tod, val


def fragz_to_full_sched(fragments: Iterable[FragmentT]) -> OuterSchedule:
    """Convert a tuple of fragments strs (a blob) into a schedule.

    :param fragments: An iterable of hexadecimal string fragments.
    :type fragments: Iterable[FragmentT]
    :returns: A parsed OuterSchedule dictionary representation.
    :rtype: OuterSchedule
    :raises zlib.error: On invalid payload compression stream.
    """
    raw_schedule = zlib.decompress(bytearray.fromhex("".join(fragments)))

    old_day = 0
    schedule: InnerScheduleT = []
    switchpoints: list[SwitchPointT] = []

    for i in range(0, len(raw_schedule), SWITCHPOINT_STRUCT_SIZE):
        idx, dow, tod, val = _struct_unpack(
            raw_schedule[i : i + SWITCHPOINT_STRUCT_SIZE]
        )

        if dow > old_day:
            schedule.append({SZ_DAY_OF_WEEK: old_day, SZ_SWITCHPOINTS: switchpoints})
            old_day, switchpoints = dow, []

        time_str = f"{tod // 60:02d}:{tod % 60:02d}"
        if val in (0, 1):
            sp_dhw: SwitchPointDhw = {
                SZ_TIME_OF_DAY: time_str,
                SZ_ENABLED: bool(val),
            }
            switchpoints.append(sp_dhw)
        else:
            sp_zon: SwitchPointZon = {
                SZ_TIME_OF_DAY: time_str,
                SZ_HEAT_SETPOINT: val / 100,
            }
            switchpoints.append(sp_zon)

    schedule.append({SZ_DAY_OF_WEEK: old_day, SZ_SWITCHPOINTS: switchpoints})
    return {SZ_ZONE_IDX: f"{idx:02X}", SZ_SCHEDULE: schedule}


def full_sched_to_fragz(full_schedule: OuterSchedule) -> list[FragmentT]:
    """Convert a schedule into a set of fragments (a blob).

    :param full_schedule: The OuterSchedule dictionary representation.
    :type full_schedule: OuterSchedule
    :returns: A list of hexadecimal string fragments.
    :rtype: list[FragmentT]
    :raises KeyError: If expected keys are missing from the structure.
    """
    cobj = zlib.compressobj(level=9, wbits=14)
    frags: list[bytes] = []

    zone_idx: str = full_schedule[SZ_ZONE_IDX]
    days_of_week: InnerScheduleT = full_schedule[SZ_SCHEDULE]
    for week_day in days_of_week:
        switchpoints: SwitchPointsT = week_day[SZ_SWITCHPOINTS]
        for switchpoint in switchpoints:
            frags.append(_struct_pack(zone_idx, week_day, switchpoint))

    blob = (b"".join(cobj.compress(f) for f in frags) + cobj.flush()).hex().upper()

    return [
        blob[i : i + FRAGMENT_HEX_LENGTH]
        for i in range(0, len(blob), FRAGMENT_HEX_LENGTH)
    ]


def _to_protocol_zone_idx(zone_idx: str) -> str:
    """Translate domain zone index string to RAMSES RF protocol index.

    DHW uses domain identifier 'HW' externally, which translates to '00' in protocol.

    :param zone_idx: Domain zone index string ('HW' or '00'-'0F').
    :type zone_idx: str
    :returns: RAMSES RF protocol zone index ('00'-'0F').
    :rtype: str
    """
    return "00" if zone_idx == "HW" else zone_idx


# TODO: make stateful (a la binding)
class Schedule:  # 0404
    """The schedule state manager for a heating or DHW zone."""

    def __init__(self, zone: DhwZone | Zone) -> None:
        """Initialize the Schedule for a specific zone.

        :param zone: The heating or DHW zone this schedule applies to.
        :type zone: DhwZone | Zone
        """
        _LOGGER.debug("Schedule(zon=%s).__init__()", zone)

        self.id = zone.id
        self._zone = zone
        self.idx = zone.idx

        self.ctl = zone.ctl
        self.tcs = zone.tcs
        self._gwy = zone._gwy

        self._full_schedule: OuterScheduleT | EmptyDictT = {}

        self._payload_set: PayloadSetT = [None]  # Rx'd
        self._fragments: FragmentSetT = []  # to Tx

        self._global_ver = 0  # None is a sentinel for 'dont know'
        self._sched_ver = 0  # the global_ver when this schedule was retrieved

    def __str__(self) -> str:
        """Return a human-readable representation of the schedule."""
        return f"{self._zone} (schedule)"

    async def _is_dated(self, *, force_io: bool = False) -> tuple[bool, bool]:
        """Indicate if a more recent schedule might be available.

        If required, retrieve the latest global version (change counter)
        from the TCS.

        There may be a false positive if another zone's schedule is
        changed when this zone's schedule has not. There may be a false
        negative if this zone's schedule was changed only very recently
        and a cached global version was used.

        If `force_io`, then a true negative is guaranteed (it forces an
        RQ|0006 unless self._global_ver > self._sched_ver).

        :param force_io: True to force an I/O request to check versions.
        :type force_io: bool
        :returns: A tuple of (is_dated, did_io).
        :rtype: tuple[bool, bool]
        """
        # this will not cause an I/O...
        if (
            not force_io
            and not self._sched_ver
            or (self._global_ver and self._global_ver > self._sched_ver)
        ):
            return True, False  # is_dated, did_io

        # this may cause an I/O...
        self._global_ver, did_io = await self.tcs._schedule_version()
        if did_io or self._global_ver > self._sched_ver:
            return (
                self._global_ver > self._sched_ver,
                did_io,
            )  # is_dated, did_io

        if force_io:  # this will cause an I/O...
            self._global_ver, did_io = await self.tcs._schedule_version(
                force_io=force_io
            )

        return (
            self._global_ver > self._sched_ver,
            did_io,
        )  # is_dated, did_io

    async def get_schedule(
        self, *, force_io: bool = False, timeout: float = 15
    ) -> InnerScheduleT | None:
        """Retrieve/return the brief schedule of a zone.

        Return the cached schedule (which may have been eavesdropped)
        only if the global change counter has not increased. Otherwise,
        RQ the latest schedule from the controller and return that.

        If `force_io`, then the latest schedule is guaranteed (it forces
        an RQ|0006).

        :param force_io: Set to True to force fetching a new schedule.
        :type force_io: bool
        :param timeout: Maximum time in seconds to wait for the schedule.
        :type timeout: float
        :returns: The schedule details or None if not available.
        :rtype: InnerScheduleT | None
        :raises exc.ScheduleFlowError: If unable to obtain the schedule
            before timeout.
        """
        try:
            await asyncio.wait_for(
                self._get_schedule(force_io=force_io), timeout=timeout
            )
        except TimeoutError as err:
            raise exc.ScheduleFlowError(
                f"Failed to obtain schedule within {timeout} secs"
            ) from err
        except ProtocolSendFailed:
            # Silently drop the background request if the transport is
            # inactive (e.g., during cache restoration prior to gateway
            # startup).
            _LOGGER.debug(f"{self}: Dropped request: gateway transport is inactive.")
            return None

        return self.schedule

    async def _fetch_fragment(self, frag_num: int) -> PayloadT:
        """Fetch a single schedule fragment from the controller.

        :param frag_num: The 1-based index of the fragment to fetch.
        :type frag_num: int
        :returns: The dictionary payload of the fragment response.
        :rtype: PayloadT
        """
        frag_set_size = 0 if frag_num == 1 else len(self._payload_set)
        pkt: Packet = await send_system_intent(
            self,
            Action.GET_SCHEDULE_FRAGMENT,
            data={
                "zone_idx": self.idx,
                "frag_number": frag_num,
                "total_frags": frag_set_size,
            },
            wait_for_reply=True,
        )
        msg = Message._from_pkt(pkt)
        assert isinstance(msg.payload, dict)  # mypy check
        return msg.payload  # may: TimeoutError?

    async def _get_schedule(self, *, force_io: bool = False) -> None:
        """Retrieve/return the schedule of a zone and sets `self._full_schedule`.

        :param force_io: Set to True to force network fetching.
        :type force_io: bool
        """
        is_dated, did_io = await self._is_dated(force_io=force_io)
        if is_dated:
            self._full_schedule = {}  # keep frags, maybe only other scheds have changed
        if self._full_schedule:
            return

        await self.tcs._obtain_lock(self.idx)  # maybe raise TimeOutError

        try:
            if not did_io:  # must know the version of the schedule about to be RQ'd
                self._global_ver, _ = await self.tcs._schedule_version(force_io=True)

            self._payload_set[0] = (
                None  # if 1st frag valid: schedule very likely unchanged
            )
            attempts = 0
            max_attempts = max(len(self._payload_set) * 2, 10)
            while True:
                attempts += 1
                if attempts > max_attempts:
                    _LOGGER.warning(
                        "%s: Exceeded max fragment fetch attempts (%s)",
                        self,
                        max_attempts,
                    )
                    raise exc.ScheduleFlowError(
                        f"Exceeded max fragment fetch attempts for zone {self.idx}"
                    )

                frag_num = next(
                    (i for i, f in enumerate(self._payload_set, 1) if f is None),
                    0,
                )
                if frag_num == 0:
                    break

                fragment = await self._fetch_fragment(frag_num)
                # next line also in self._handle_msg(), so protected there with a lock
                try:
                    self._payload_set = self._update_payload_set(
                        self._payload_set, fragment
                    )
                except exc.ScheduleError as err:
                    _LOGGER.warning(
                        "%s: Dropped corrupted schedule fragments during fetch: %s",
                        self,
                        err,
                    )
                    self._payload_set = [None]
                    break

                if None not in self._payload_set:
                    self._sched_ver = self._global_ver
                    break
        finally:
            self.tcs._release_lock()

    def _proc_payload_set(self, payload_set: PayloadSetT) -> OuterScheduleT | None:
        """Process a payload set and return the full schedule.

        Sets `self._full_schedule`. If the schedule is for DHW, set the
        `zone_idx` key to 'HW' (to avoid confusing with zone '00').

        :param payload_set: The completed array of fragment payloads.
        :type payload_set: PayloadSetT
        :returns: The outer schedule dictionary (not `self.schedule`).
        :rtype: OuterScheduleT | None
        :raises exc.ScheduleError: On failure to decompress fragment string or
            if the fragment set is incomplete.
        """
        if payload_set == [None]:
            self._full_schedule = {SZ_ZONE_IDX: self.idx}
            return self._full_schedule

        if None in payload_set:
            raise exc.ScheduleError(
                "Incomplete schedule fragment payload set provided for decompression"
            )

        try:
            schedule = fragz_to_full_sched(
                str(payload[SZ_FRAGMENT])
                for payload in payload_set
                if payload and SZ_FRAGMENT in payload
            )
        except zlib.error as err:
            raise exc.ScheduleError("Failed to decompress schedule fragments") from err

        if self.idx == "HW":
            schedule[SZ_ZONE_IDX] = "HW"
        self._full_schedule = schedule

        return self._full_schedule  # NOTE: not self.schedule

    @staticmethod
    def _init_payload_set(payload: PayloadT) -> PayloadSetT:
        """Initialise a new payload set from a fragment payload.

        :param payload: A fragment payload dictionary.
        :type payload: PayloadT
        :returns: Initialised array for expected fragments.
        :rtype: PayloadSetT
        """
        total_frags = payload.get(SZ_TOTAL_FRAGS)
        frag_num = payload.get(SZ_FRAG_NUMBER)

        if total_frags is None or frag_num is None:
            return [None]

        new_set: PayloadSetT = [None] * total_frags
        if 0 < frag_num <= total_frags:
            new_set[frag_num - 1] = payload
        return new_set

    def _update_payload_set(
        self, payload_set: PayloadSetT, payload: PayloadT
    ) -> PayloadSetT:
        """Add a fragment to a frag set and process/return the new set.

        If the frag set is complete, check for a schedule (sets
        `self._schedule`). If required, start a new frag set with the
        fragment.

        :param payload_set: Existing fragment collection.
        :type payload_set: PayloadSetT
        :param payload: New payload dictionary to integrate.
        :type payload: PayloadT
        :returns: Updated fragment payload collection.
        :rtype: PayloadSetT
        """
        if payload.get(SZ_TOTAL_FRAGS) is None:  # zone has no schedule
            payload_set = [None]
            self._proc_payload_set(payload_set)
            return payload_set

        if payload.get(SZ_TOTAL_FRAGS) != len(payload_set):  # sched has changed
            return self._init_payload_set(payload)

        frag_num = payload.get(SZ_FRAG_NUMBER)
        if frag_num is not None and 0 < frag_num <= len(payload_set):
            payload_set[frag_num - 1] = payload

        if None in payload_set or self._proc_payload_set(
            payload_set
        ):  # sets self._schedule
            return payload_set

        return self._init_payload_set(payload)

    async def _send_fragment(self, frag_num: int, frag_cnt: int, fragment: str) -> None:
        """Send a schedule fragment to the controller.

        :param frag_num: Current fragment number (1-based).
        :type frag_num: int
        :param frag_cnt: Total fragment count.
        :type frag_cnt: int
        :param fragment: Hexadecimal fragment payload string.
        :type fragment: str
        """
        await send_system_intent(
            self,
            Action.SET_SCHEDULE_FRAGMENT,
            data={
                "zone_idx": self.idx,
                "frag_num": frag_num,
                "frag_cnt": frag_cnt,
                "fragment": fragment,
            },
            wait_for_reply=True,
        )

    def _normalise_and_validate(self, schedule: InnerScheduleT) -> OuterSchedule:
        """Normalise and validate schedule dictionary structure.

        :param schedule: 7-day schedule array to validate.
        :type schedule: InnerScheduleT
        :returns: Validated OuterSchedule payload.
        :rtype: OuterSchedule
        :raises exc.ScheduleError: On validation failure.
        """
        if self.idx == "HW":
            full_schedule: OuterSchedule = {
                SZ_ZONE_IDX: "HW",
                SZ_SCHEDULE: schedule,
            }
            schema = SCH_SCHEDULE_DHW_OUTER
        else:
            full_schedule = {
                SZ_ZONE_IDX: self.idx,
                SZ_SCHEDULE: schedule,
            }
            schema = SCH_SCHEDULE_ZON_OUTER

        try:
            validated: OuterSchedule = schema(full_schedule)
        except vol.MultipleInvalid as err:
            raise exc.ScheduleError(f"failed to set schedule: {err}") from err

        if self.idx == "HW":
            # Translate DHW domain index 'HW' to protocol zone index '00'
            validated[SZ_ZONE_IDX] = _to_protocol_zone_idx(self.idx)

        return validated

    async def set_schedule(
        self, schedule: InnerScheduleT, force_refresh: bool = False
    ) -> InnerScheduleT | None:
        """Set the schedule of a zone.

        :param schedule: The array representing the days of the week
            schedule.
        :type schedule: InnerScheduleT
        :param force_refresh: True to query and retrieve the new
            schedule directly after setting.
        :type force_refresh: bool
        :returns: The updated InnerSchedule array.
        :rtype: InnerScheduleT | None
        :raises exc.ScheduleError: On validation or serialization failure.
        :raises exc.ScheduleFlowError: On transmission timeout.
        """
        full_schedule = self._normalise_and_validate(schedule)
        self._fragments = full_sched_to_fragz(full_schedule)

        await self.tcs._obtain_lock(self.idx)  # maybe raise TimeOutError

        try:
            frag_cnt = len(self._fragments)
            for num, frag in enumerate(self._fragments, 1):
                await self._send_fragment(num, frag_cnt, frag)
        except TimeoutError as err:
            raise exc.ScheduleFlowError(f"failed to set schedule: {err}") from err
        else:
            if not force_refresh:
                self._global_ver, _ = await self.tcs._schedule_version(force_io=True)
                self._sched_ver = self._global_ver
        finally:
            self.tcs._release_lock()

        if force_refresh:
            await self.get_schedule(force_io=True)  # sets self._full_schedule
        else:
            self._full_schedule = full_schedule

        return self.schedule

    @property
    def schedule(self) -> InnerScheduleT | None:
        """Return the current (not full) schedule, if any.

        :returns: The 7-day schedule array or None.
        :rtype: InnerScheduleT | None
        """
        if not self._full_schedule:  # can be {}
            return None
        sched = self._full_schedule.get(SZ_SCHEDULE)
        if isinstance(sched, list):
            return sched
        return None

    @property
    def version(self) -> int | None:
        """Return the version associated with the current schedule, if any.

        :returns: The schedule version counter or None.
        :rtype: int | None
        """
        return self._sched_ver if self._full_schedule else None


# 16:27:56.942 000 RQ --- 18:006402 01:145038 --:------ 0006 001 00
# 16:27:56.958 038 RP --- 01:145038 18:006402 --:------ 0006 004 00050009

# 16:27:57.005 000 RQ --- 18:006402 01:145038 --:------ 0404 007 0120000800-0100
# 16:27:57.068 037 RP --- 01:145038 18:006402 --:------ 0404 048 0120000829-0103-68816DCFCB0980301045D1994C3E624916660956604596600516E1D285094112F566F5B80C072222A2
# 16:27:57.114 000 RQ --- 18:006402 01:145038 --:------ 0404 007 0120000800-0203
# 16:27:57.161 038 RP --- 01:145038 18:006402 --:------ 0404 048 0120000829-0203-52DF92C79CEA7EDA91C7F06997FDEFC620B287D6143C054FC153F01C780E3C079E03CFC033F00C3C03
# 16:27:57.202 000 RQ --- 18:006402 01:145038 --:------ 0404 007 0120000800-0303
# 16:27:57.245 038 RP --- 01:145038 18:006402 --:------ 0404 045 0120000826-0303-CF83E7C1F3E079F0CADC3E5E696BFECC944EED5BF5DEAD7AAD45F0227811BCD87937936E24CF
