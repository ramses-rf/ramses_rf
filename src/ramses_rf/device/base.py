#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Base for all devices.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from ramses_rf.binding_fsm import BindContext, Vendor
from ramses_rf.const import DEV_TYPE_MAP, SZ_OEM_CODE, DevType
from ramses_rf.entity_base import Child, Entity, class_by_attr
from ramses_rf.helpers import shrink
from ramses_rf.schemas import (
    SCH_TRAITS,
    SZ_ALIAS,
    SZ_CLASS,
    SZ_FAKED,
    SZ_KNOWN_LIST,
    SZ_SCHEME,
)
from ramses_tx import Command, Packet, Priority, QosParams
from ramses_tx.ramses import CODES_BY_DEV_SLUG, CODES_ONLY_FROM_CTL

from ramses_rf.const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

if TYPE_CHECKING:
    from typing import Any

    from ramses_rf import Gateway
    from ramses_rf.system import Zone
    from ramses_tx import Address, DeviceIdT, IndexT, Message


BIND_WAITING_TIMEOUT = 300  # how long to wait, listening for an offer
BIND_REQUEST_TIMEOUT = 5  # how long to wait for an accept after sending an offer
BIND_CONFIRM_TIMEOUT = 5  # how long to wait for a confirm after sending an accept


_LOGGER = logging.getLogger(__name__)


class DeviceBase(Entity):
    """The Device base class - can also be used for unknown device types."""

    _SLUG: str = DevType.DEV
    _STATE_ATTR: str = None

    _bind_context: BindContext | None = None

    def __init__(self, gwy: Gateway, dev_addr: Address, **kwargs: Any) -> None:
        super().__init__(gwy)

        # FIXME: ZZZ entities must know their parent device ID and their own idx
        self._z_id = dev_addr.id  # the responsible device is itself
        self._z_idx = None  # depends upon its location in the schema

        self.id: DeviceIdT = dev_addr.id

        # self.tcs = None  # NOTE: Heat (CH/DHW) devices only
        # self.ctl = None
        # self._child_id = None  # also in Child class

        self.addr = dev_addr
        self.type = dev_addr.type  # DEX  # TODO: remove this attr? use SLUG?

        self._scheme: Vendor = None

    def __str__(self) -> str:
        if self._STATE_ATTR:
            return f"{self.id} ({self._SLUG}): {getattr(self, self._STATE_ATTR)}"
        return f"{self.id} ({self._SLUG})"

    def __lt__(self, other: object) -> bool:
        if not hasattr(other, "id"):
            return NotImplemented
        return self.id < other.id  # type: ignore[no-any-return]

    def _update_traits(self, **traits: Any) -> None:
        """Update a device with new schema attrs.

        Raise an exception if the new schema is not a superset of the existing schema.
        """

        traits = shrink(SCH_TRAITS(traits))

        if traits.get(SZ_FAKED):  # class & alias are done elsewhere
            if not isinstance(self, Fakeable):
                raise TypeError(f"Device is not fakeable: {self} (traits={traits})")
            self._make_fake()

        self._scheme = traits.get(SZ_SCHEME)

    @classmethod
    def create_from_schema(
        cls, gwy: Gateway, dev_addr: Address, **schema: Any
    ) -> DeviceBase:
        """Create a device (for a GWY) and set its schema attrs (aka traits).

        All devices have traits, but also controllers (CTL, UFC) have a system schema.

        The appropriate Device class should have been determined by a factory.
        Schema attrs include: class (SLUG), alias & faked.
        """

        dev = cls(gwy, dev_addr)
        dev._update_traits(**schema)  # TODO: split traits/schema
        return dev

    def _setup_discovery_cmds(self) -> None:
        # super()._setup_discovery_cmds()
        # sometimes, battery-powered devices will respond to an RQ (e.g. bind mode)

        # if discover_flag & Discover.TRAITS:
        # self._add_discovery_cmd(cmd(RQ, Code._1FC9, "00", self.id), 60 * 60 * 24)
        # self._add_discovery_cmd(cmd(RQ, Code._0016, "00", self.id), 60 * 60)

        pass

    def _send_cmd(self, cmd: Command, **kwargs: Any) -> None:
        if self.has_battery and not self.is_faked and cmd.dst.id == self.id:
            _LOGGER.info(f"{cmd} < Sending inadvisable for {self} (it has a battery)")

        super()._send_cmd(cmd, **kwargs)

    def _handle_msg(self, msg: Message) -> None:
        # # assert msg.src is self or (
        # #     msg.code == Code._1FC9 and msg.payload[SZ_PHASE] == SZ_OFFER
        # # ), f"msg from {msg.src} inappropriately routed to {self}"

        super()._handle_msg(msg)

        if self._SLUG in DEV_TYPE_MAP.PROMOTABLE_SLUGS:  # HACK: can get precise class?
            from . import best_dev_role

            cls = best_dev_role(
                self.addr, msg=msg, eavesdrop=self._gwy.config.enable_eavesdrop
            )

            if cls._SLUG in (DevType.DEV, self._SLUG):
                return  # either a demotion (DEV), or not promotion (HEA/HVC)

            if self._SLUG == DevType.HEA and cls._SLUG in DEV_TYPE_MAP.HVAC_SLUGS:
                return  # TODO: should raise error if CODES_OF_HVAC_DOMAIN_ONLY?

            if self._SLUG == DevType.HVC and cls._SLUG not in DEV_TYPE_MAP.HVAC_SLUGS:
                return  # TODO: should raise error if CODES_OF_HEAT_DOMAIN_ONLY?

            _LOGGER.warning(
                f"Promoting the device class of {self} to {cls._SLUG}"
                f" - use a {SZ_KNOWN_LIST} to explicitly set this device's"
                f" {SZ_CLASS} to '{DEV_TYPE_MAP[cls._SLUG]}'"
            )
            self.__class__ = cls

    @property
    def has_battery(self) -> None | bool:  # 1060
        """Return True if the device is battery powered (excludes battery-backup)."""

        return isinstance(self, BatteryState) or Code._1060 in self._msgz

    @property
    def is_faked(self) -> bool:
        """Return True if the device is faked."""

        return bool(self._bind_context)  # isinstance(self, Fakeable) and...

    @property
    def _is_binding(self) -> bool:
        """Return True if the (faked) device is actively binding."""

        return self._bind_context and self._bind_context.is_binding

    @property
    def _is_present(self) -> bool:
        """Try to exclude ghost devices (as caused by corrupt packet addresses)."""
        return any(
            m.src == self for m in self._msgs.values() if not m._expired
        )  # TODO: needs addressing

    @property
    def schema(self) -> dict[str, Any]:
        """Return the fixed attributes of the device."""
        return {}  # SZ_CLASS: DEV_TYPE_MAP[self._SLUG]}

    @property
    def params(self) -> dict[str, Any]:
        """Return the configurable attributes of the device."""
        return {}

    @property
    def status(self) -> dict[str, Any]:
        """Return the state attributes of the device."""
        return {}

    @property
    def traits(self) -> dict[str, Any]:
        """Return the traits of the device."""

        result = super().traits

        known_dev = self._gwy._include.get(self.id)

        result.update(
            {
                SZ_CLASS: DEV_TYPE_MAP[self._SLUG],
                SZ_ALIAS: known_dev.get(SZ_ALIAS) if known_dev else None,
                SZ_FAKED: self.is_faked,
            }
        )

        return result | {"_bind": self._msg_value(Code._1FC9)}


class BatteryState(DeviceBase):  # 1060
    BATTERY_LOW = "battery_low"  # boolean
    BATTERY_STATE = "battery_state"  # percentage (0.0-1.0)

    @property
    def battery_low(self) -> None | bool:  # 1060
        if self.is_faked:
            return False
        return self._msg_value(Code._1060, key=self.BATTERY_LOW)

    @property
    def battery_state(self) -> dict[str, Any] | None:  # 1060
        if self.is_faked:
            return None
        return self._msg_value(Code._1060)

    @property
    def status(self) -> dict[str, Any]:
        return {
            **super().status,
            self.BATTERY_STATE: self.battery_state,
        }


class DeviceInfo(DeviceBase):  # 10E0
    def _setup_discovery_cmds(self) -> None:
        super()._setup_discovery_cmds()

        # if discover_flag & Discover.SCHEMA:
        if self._SLUG not in CODES_BY_DEV_SLUG or RP in CODES_BY_DEV_SLUG[
            self._SLUG
        ].get(Code._10E0, {}):
            cmd = Command.from_attrs(RQ, self.id, Code._10E0, "00")
            self._add_discovery_cmd(cmd, 60 * 60 * 24)

    @property
    def device_info(self) -> dict | None:  # 10E0
        return self._msg_value(Code._10E0)

    @property
    def traits(self) -> dict[str, Any]:
        """Return the traits of the device."""

        result = super().traits

        if Code._10E0 in self._msgs or Code._10E0 in CODES_BY_DEV_SLUG.get(
            self._SLUG, []
        ):
            result.update({"_info": self.device_info})

        return result


# NOTE: devices (Thermostat) not attrs (Temperature) are faked
class Fakeable(DeviceBase):
    """There are two types of Faking: impersonation (of real devices) and full-faking.

    Impersonation of physical devices simply means sending packets on their behalf. This
    is straight-forward for sensors & remotes (they do not usually receive pkts).

    Faked (virtual) devices must have any packet addressed to them sent to their
    handle_msg() method by the dispatcher. Impersonated devices will simply pick up
    such packets via RF.
    """

    def __init__(self, gwy: Gateway, *args: Any, **kwargs: Any) -> None:
        super().__init__(gwy, *args, **kwargs)

        self._bind_context: BindContext | None = None

        # TOD: this is messy - device schema vs device traits
        if self.id in gwy._include and gwy._include[self.id].get(SZ_FAKED):
            self._make_fake()

        if kwargs.get(SZ_FAKED):
            self._make_fake()

    def _make_fake(self) -> None:
        if self._bind_context:
            return

        self._bind_context = BindContext(self)
        self._gwy._include[self.id][SZ_FAKED] = True  # TODO: remove this
        _LOGGER.info(f"Faking now enabled for: {self}")

    async def _async_send_cmd(
        self,
        cmd: Command,
        priority: Priority | None = None,
        qos: QosParams | None = None,
    ) -> Packet | None:
        """Wrapper to CC: any relevant Commands to the binding Context."""

        if self._bind_context and self._bind_context.is_binding:
            # cmd.code in (Code._1FC9, Code._10E0)
            self._bind_context.sent_cmd(cmd)  # other codes needed for edge cases

        return await super()._async_send_cmd(cmd, priority=priority, qos=qos)

    def _handle_msg(self, msg: Message) -> None:
        """Wrapper to CC: any relevant Packets to the binding Context."""

        super()._handle_msg(msg)

        if self._bind_context and self._bind_context.is_binding:
            # msg.code in (Code._1FC9, Code._10E0)
            self._bind_context.rcvd_msg(msg)  # maybe other codes needed for edge cases

    async def _wait_for_binding_request(
        self,
        accept_codes: Iterable[Code],
        /,
        *,
        idx: IndexT = "00",
        require_ratify: bool = False,
    ) -> tuple[Packet, Packet, Packet, Packet | None]:
        """Listen for a binding and return the Offer, or raise an exception."""

        if not self._bind_context:
            raise TypeError(f"{self}: Faking not enabled")

        msgs = await self._bind_context.wait_for_binding_request(
            accept_codes, idx=idx, require_ratify=require_ratify
        )
        return msgs

    async def wait_for_binding_request(
        self,
        accept_codes: Iterable[Code],
        /,
        *,
        idx: IndexT = "00",
        require_ratify: bool = False,
    ) -> tuple[Packet, Packet, Packet, Packet | None]:
        raise NotImplementedError

    async def _initiate_binding_process(
        self,
        offer_codes: Code | Iterable[Code],
        /,
        *,
        confirm_code: Code | None = None,
        ratify_cmd: Command | None = None,
    ) -> tuple[Packet, Packet, Packet, Packet | None]:
        """Start a binding and return the Accept, or raise an exception."""
        # confirm_code can be FFFF.

        if not self._bind_context:
            raise TypeError(f"{self}: Faking not enabled")

        if isinstance(offer_codes, Iterable):
            codes: tuple[Code] = offer_codes
        else:
            codes = tuple([offer_codes])

        msgs = await self._bind_context.initiate_binding_process(
            codes, confirm_code=confirm_code, ratify_cmd=ratify_cmd
        )  # TODO: if successful, re-discover schema?
        return msgs

    async def initiate_binding_process(self) -> Packet:
        raise NotImplementedError

    @property
    def oem_code(self) -> str | None:
        """Return the OEM code (a 2-char ascii str) for this device, if there is one."""
        # raise NotImplementedError  # self.traits is a @property
        if not self.traits.get(SZ_OEM_CODE):
            self.traits[SZ_OEM_CODE] = self._msg_value(Code._10E0, key=SZ_OEM_CODE)
        return self.traits.get(SZ_OEM_CODE)


class Device(Child, DeviceBase):
    """The base class for all devices."""

    def __init__(self, gwy: Gateway, dev_addr: Address, **kwargs: Any) -> None:
        _LOGGER.debug("Creating a Device: %s (%s)", dev_addr.id, self.__class__)
        super().__init__(gwy, dev_addr)

        gwy._add_device(self)


class HgiGateway(Device):  # HGI (18:)
    """The HGI80 base class."""

    _SLUG: str = DevType.HGI

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self.ctl = None  # FIXME: a mess
        self._child_id = "gw"  # TODO
        self.tcs = None

    @property
    def schema(self) -> dict[str, Any]:
        return {}


class DeviceHeat(Device):  # Heat domain: Honeywell CH/DHW or compatible
    """The base class for the heat domain (Honeywell CH/DHW-compatible devices).

    Includes UFH and heatpumps (which can also cool).
    """

    _SLUG: str = DevType.HEA  # shouldn't be any of these instantiated

    def __init__(self, gwy: Gateway, dev_addr: Address, **kwargs: Any) -> None:
        super().__init__(gwy, dev_addr, **kwargs)

        self.ctl = None
        self.tcs = None
        self._child_id = None  # domain_id, or zone_idx

        self._iz_controller: None | bool | Message = None

    def _handle_msg(self, msg: Message) -> None:
        super()._handle_msg(msg)

        if msg.verb != I_ or self._iz_controller is not None:
            return

        if not self._iz_controller and msg.code in CODES_ONLY_FROM_CTL:
            if self._iz_controller is None:
                _LOGGER.info(f"{msg!r} # IS_CONTROLLER (00): is TRUE")
                self._make_tcs_controller(msg=msg)
            elif self._iz_controller is False:  # TODO: raise CorruptStateError
                _LOGGER.error(f"{msg!r} # IS_CONTROLLER (01): was FALSE, now True")

    def _make_tcs_controller(
        self, *, msg: Message | None = None, **schema: Any
    ) -> None:  # CH/DHW
        """Attach a TCS (create/update as required) after passing it any msg."""

        if self.type not in DEV_TYPE_MAP.CONTROLLERS:  # potentially can be controllers
            raise TypeError(f"Invalid device type to be a controller: {self}")

        self._iz_controller = self._iz_controller or msg or True

    # @property
    # def controller(self):  # -> Optional[Controller]:
    #     """Return the entity's controller, if known."""

    #     return self.ctl  # TODO: if the controller is not known, try to find it?

    @property
    def _is_controller(self) -> None | bool:
        if self._iz_controller is not None:
            return bool(self._iz_controller)  # True, False, or msg

        if self.ctl is not None:  # TODO: messy
            return self.ctl is self

        return False

    @property
    def zone(self) -> Zone | None:
        """Return the device's parent zone, if known."""

        return self._parent


class DeviceHvac(Device):  # HVAC domain: ventilation, PIV, MV/HR
    """The Device base class for the HVAC domain (ventilation, PIV, MV/HR)."""

    _SLUG: str = DevType.HVC  # these may be instantiated, and promoted later on

    def __init__(self, gwy: Gateway, dev_addr: Address, **kwargs: Any) -> None:
        super().__init__(gwy, dev_addr, **kwargs)

        self._child_id = "hv"  # TODO: domain_id/deprecate

    # def _handle_msg(self, msg: Message) -> None:
    #     super()._handle_msg(msg)

    #     # if type(self) is DeviceHvac:
    #     #     if self.type == DEV_TYPE_MAP.RFG:  # self.__class__ is Device, DEX
    #     #         # TODO: the RFG codes need checking
    #     #         if msg.code in (Code._31D9, Code._31DA) and msg.verb in (I_, RP):
    #     #             self.__class__ = HvacVentilator
    #     #         elif msg.code in (Code._0006, Code._0418, Code._3220) and msg.verb == RQ:
    #     #             self.__class__ = RfgGateway
    #     #         elif msg.code in (Code._313F,) and msg.verb == W_:
    #     #             self.__class__ = RfgGateway
    #     #     if type(self) is not Device:
    #     #         _LOGGER.warning(f"Promoted a device type for: {self}")

    #     if msg.code in (Code._1298, Code._12A0, Code._22F1, Code._22F3):
    #         self._hvac_trick()


# e.g. {"HGI": HgiGateway}
BASE_CLASS_BY_SLUG: dict[str, type[Device]] = class_by_attr(__name__, "_SLUG")
