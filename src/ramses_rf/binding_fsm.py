#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Base for all devices.
"""

from __future__ import annotations

import asyncio
import logging
import re
from enum import StrEnum
from typing import TYPE_CHECKING, Final

import probatio as vol

from ramses_rf.address import Address
from ramses_rf.commands.core import Command as Intent
from ramses_rf.enums import Action, DevType
from ramses_tx import (
    ALL_DEVICE_ID,
    NON_DEVICE_ID,
    CommandDTO,
    Priority,
    QosParams,
)
from ramses_tx.typing import DeviceIdT

from . import exceptions as exc
from .messages import Message

from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    SZ_OEM_CODE,
    SZ_PHASE,
    Code,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ramses_tx import IndexT, Packet

    from .interfaces import CommandDispatcher, DeviceInterface

_DBG_MAINTAIN_STATE_CHAIN: Final[bool] = False  # maintain Context._prev_state

_LOGGER = logging.getLogger(__name__)


SZ_RESPONDENT: Final = "respondent"
SZ_SUPPLICANT: Final = "supplicant"
SZ_IS_DORMANT: Final = "is_dormant"


CONFIRM_RETRY_LIMIT: Final[int] = (
    3  # automatically Bound, from Confirming > this # of sends
)
SENDING_RETRY_LIMIT: Final[int] = (
    3  # fail Offering/Accepting if no response > this # of sends
)

CONFIRM_TIMEOUT_SECS: Final[float] = (
    3  # automatically Bound, from BoundAccepted > this # of seconds
)
WAITING_TIMEOUT_SECS: Final[float] = (
    5  # fail Listen/Offer/Accept if no packet rcvd > this # of seconds
)

# raise a BindTimeoutError if expected Pkt is not received before this number of seconds
_TENDER_WAIT_TIME: Final[float] = (
    WAITING_TIMEOUT_SECS  # resp. listening for Offer
)
_ACCEPT_WAIT_TIME: Final[float] = (
    WAITING_TIMEOUT_SECS  # supp. sent Offer, expecting Accept
)
_AFFIRM_WAIT_TIME: Final[float] = (
    CONFIRM_TIMEOUT_SECS  # resp. sent Accept, expecting Confirm
)
_RATIFY_WAIT_TIME: Final[float] = (
    CONFIRM_TIMEOUT_SECS  # resp. rcvd Confirm, expecting Ratify (10E0)
)


BINDING_QOS = QosParams(
    max_retries=SENDING_RETRY_LIMIT,
    timeout=WAITING_TIMEOUT_SECS * 2,
)


class Vendor(StrEnum):
    """Enumeration of recognized hardware vendors."""

    BROFER = "brofer"
    CLIMARAD = "climarad"
    ITHO = "itho"
    NUAIRE = "nuaire"
    ORCON = "orcon"
    VASCO = "vasco"
    DEFAULT = "default"


SZ_CLASS: Final = "class"
SZ_VENDOR: Final = "vendor"
SZ_TENDER: Final = "tender"
SZ_AFFIRM: Final = "affirm"
SZ_RATIFY: Final = "ratify"

# VOL_SUPPLICANT_ID = vol.Match(re.compile(r"^03:[0-9]{6}$"))
VOL_CODE_REGEX = vol.Match(re.compile(r"^[0-9A-F]{4}$"))
VOL_OEM_ID_REGEX = vol.Match(re.compile(r"^[0-9A-F]{2}$"))

VOL_TENDER_CODES = vol.All(
    {vol.Required(VOL_CODE_REGEX, default="00"): VOL_OEM_ID_REGEX},
    vol.Length(min=1),
)

VOL_SUPPLICANT = vol.Schema(
    {
        vol.Required(SZ_CLASS): vol.Any(DevType.THM.value, DevType.DHW.value),
        vol.Optional(SZ_VENDOR, default="honeywell"): vol.Any(
            "honeywell", "resideo", *(m.value for m in Vendor)
        ),
        vol.Optional(SZ_TENDER): VOL_TENDER_CODES,
        vol.Optional(SZ_AFFIRM, default={}): vol.Any({}),
        vol.Optional(SZ_RATIFY, default=None): vol.Any(None),
    },
    extra=vol.PREVENT_EXTRA,
)


class BindPhase(StrEnum):
    """Enumeration representing the phase of the binding process."""

    TENDER = "offer"
    ACCEPT = "accept"
    AFFIRM = "confirm"
    RATIFY = "addenda"  # Code._10E0


class BindRole(StrEnum):
    """Enumeration representing the binding role of a device."""

    RESPONDENT = "respondent"
    SUPPLICANT = "supplicant"
    IS_DORMANT = "is_dormant"
    IS_UNKNOWN = "is_unknown"


SCHEME_LOOKUP = {
    Vendor.ITHO: {SZ_OEM_CODE: "01"},
    Vendor.BROFER: {SZ_OEM_CODE: "6A"},
    Vendor.NUAIRE: {SZ_OEM_CODE: "6C"},
    Vendor.CLIMARAD: {SZ_OEM_CODE: "65"},
    Vendor.VASCO: {SZ_OEM_CODE: "66"},
    Vendor.ORCON: {SZ_OEM_CODE: "67", "offer_to": ALL_DEVICE_ID},
    Vendor.DEFAULT: {SZ_OEM_CODE: None},
}


#


class BindingManagerBase:
    """The manager is the core service. It should be initiated with a default state."""

    _attr_role = BindRole.IS_UNKNOWN

    _is_respondent: (
        bool | None
    )  # if binding, is either: respondent or supplicant
    _state: BindStateBase = None  # type: ignore[assignment]

    def __init__(
        self, device: DeviceInterface, dispatcher: CommandDispatcher
    ) -> None:
        """Initialize the binding manager.

        :param device: The device interface context for this binding service.
        :param dispatcher: The command dispatcher callback.
        """
        self._dev = device
        self._dispatcher = dispatcher
        self._loop = asyncio.get_running_loop()
        self._fut: asyncio.Future[Message] | None = None

        self.set_state(DevIsNotBinding)

    def __repr__(self) -> str:
        """Return an unambiguous string representation."""
        return f"{self._dev.id} ({self.role}): {self.state!r}"

    def __str__(self) -> str:
        """Return a human-readable string representation."""
        return f"{self._dev.id}: {self.state}"

    def cancel(self) -> None:
        """Cancel any pending wait timer on the current state.

        This should be called during teardown (e.g. gateway.stop()) to ensure
        no ``call_later`` timer handles outlive the binding manager.
        """
        timer = (
            getattr(self._state, "_timer_handle", None)
            if self._state
            else None
        )
        if timer:
            timer.cancel()

    def set_state(
        self,
        state: type[BindStateBase],
        result: asyncio.Future[Message] | None = None,
    ) -> None:
        """Transition the State of the Manager, and process the result, if any.

        :param state: The new state class to transition into.
        :param result: The future result carrying the preceding message state, optional.
        """
        # Ensure prev_state is always available, not only during debugging
        prev_state = self._state

        # if False and result:
        #     try:
        #         self._fut.set_result(result.result())
        #     except exc.BindingError as err:
        #         self._fut.set_result(err)

        # Cancel any pending timer from the previous state before transitioning
        timer = (
            getattr(prev_state, "_timer_handle", None) if prev_state else None
        )
        if timer:
            timer.cancel()

        self._state = state(self)
        if not self.is_binding:
            self._is_respondent = None
        elif state is RespIsWaitingForOffer:
            self._is_respondent = True
        elif state is SuppSendOfferWaitForAccept:
            self._is_respondent = False

        # Log binding completion transitions
        if isinstance(
            self._state, (RespHasBoundAsRespondent, SuppHasBoundAsSupplicant)
        ):
            _LOGGER.info(
                f"{self._dev.id}: Binding process completed: "
                f"{type(prev_state).__name__} -> {state.__name__} (role: {self.role})"
            )

        if _DBG_MAINTAIN_STATE_CHAIN:  # HACK for debugging
            setattr(self._state, "_prev_state", prev_state)  # noqa: B010

    @property
    def state(self) -> BindStateBase:
        """Return the State (phase) of the Manager."""
        return self._state

    @property
    def role(self) -> BindRole:
        """Return the current binding role."""
        if self._is_respondent is True:
            return BindRole.RESPONDENT
        if self._is_respondent is False:
            return BindRole.SUPPLICANT
        return BindRole.IS_DORMANT

    # TODO: Should remain is_binding until after 10E0 rcvd (if one expected)?
    @property
    def is_binding(self) -> bool:
        """Return True if currently participating in a binding process."""
        return not isinstance(self.state, _IS_NOT_BINDING_STATES)

    def rcvd_msg(self, msg: Message) -> None:
        """Pass relevant Messages through to the state processor.

        :param msg: The incoming message to process.
        """
        if msg.code in (Code._1FC9, Code._10E0):
            self.state.rcvd_msg(msg)

    def sent_cmd(self, command: CommandDTO) -> None:
        """Pass relevant Commands through to the state processor.

        :param command: The outgoing command to process.
        """
        if command.code in (Code._1FC9, Code._10E0):
            self.state.send_cmd(command)


class BindingManagerRespondent(BindingManagerBase):
    """The binding Manager for a Respondent."""

    _attr_role = BindRole.RESPONDENT

    async def wait_for_binding_request(
        self,
        accept_codes: Iterable[Code],
        /,
        *,
        zone_index: IndexT = "00",
        require_ratify: bool = False,
    ) -> tuple[Message, Message, Message, Message | None]:
        """Device starts binding as a Respondent, by listening for an Offer.

        Returns the Supplicant's Offer or raises an exception if the binding is
        unsuccessful (BindError).

        :param accept_codes: An iterable of codes this device accepts.
        :param zone_index: The index to bind to, defaults to '00'.
        :param require_ratify: If True, require an addenda stage.
        :return: A tuple containing the (tender, accept, affirm, ratify) objects.
        :raises exc.BindingFsmError: If already binding.
        """
        if self.is_binding:
            raise exc.BindingFsmError(
                f"{self}: bad State for bindings as a Respondent (is already binding)"
            )
        self.set_state(RespIsWaitingForOffer)  # self._is_respondent = True

        # Step R1: Respondent expects an Offer
        tender = await self._wait_for_offer()

        # Step R2: Respondent expects a Confirm after sending an Accept (accepts Offer)
        accept = await self._accept_offer(
            tender, accept_codes, zone_index=zone_index
        )
        affirm = await self._wait_for_confirm(accept)

        # Step R3: Respondent expects an Addenda (optional)
        if require_ratify:  # TODO: not recvd as sent to 63:262142
            self.set_state(RespIsWaitingForAddenda)  # HACK: easiest way
            ratify = await self._wait_for_addenda(
                accept
            )  # may: exc.BindingFlowFailed:
        else:
            ratify = None

        # self._set_as_bound(tender, accept, affirm, ratify)
        return tender, accept, affirm, ratify

    async def _wait_for_offer(
        self, timeout: float = _TENDER_WAIT_TIME
    ) -> Message:
        """Resp waits timeout seconds for an Offer to arrive & returns it.

        :param timeout: Time to wait in seconds.
        :return: The offer message.
        """
        return await self.state.wait_for_offer(timeout)

    async def _accept_offer(
        self, tender: Message, codes: Iterable[Code], zone_index: IndexT = "00"
    ) -> Message:
        """Resp sends an Accept on the basis of a rcvd Offer & returns the sent message.

        :param tender: The received offer message.
        :param codes: Iterable of codes accepted.
        :param zone_index: The bound index.
        :return: The sent accept message.
        """
        intent = Intent(
            src=Address(self._dev.id),
            dst=Address(tender.src.id),
            action=Action.PUT_BIND,
            data={"verb": W_, "codes": codes, "index": zone_index},
        )
        msg: Message = await self._dispatcher.send(
            intent, priority=Priority.HIGH
        )

        self.state.cast_accept_offer()
        return msg

    async def _wait_for_confirm(
        self,
        accept: Message | Packet,
        timeout: float = _AFFIRM_WAIT_TIME,
    ) -> Message:
        """Resp waits timeout seconds for a Confirm to arrive & returns it.

        :param accept: The accept message or packet previously sent.
        :param timeout: Time to wait in seconds.
        :return: The confirm message.
        """
        return await self.state.wait_for_confirm(timeout)

    async def _wait_for_addenda(
        self,
        accept: Message | Packet,
        timeout: float = _RATIFY_WAIT_TIME,
    ) -> Message:
        """Resp waits timeout seconds for an Addenda to arrive & returns it.

        :param accept: The accept message or packet previously sent.
        :param timeout: Time to wait in seconds.
        :return: The addenda message.
        """
        return await self.state.wait_for_addenda(timeout)


class BindingManagerSupplicant(BindingManagerBase):
    """The binding Manager for a Supplicant."""

    _attr_role = BindRole.SUPPLICANT

    async def initiate_binding_process(
        self,
        offer_codes: Iterable[Code | tuple[IndexT, Code]],
        /,
        *,
        confirm_code: Code | None = None,
        ratify_command: CommandDTO | None = None,
    ) -> tuple[Message, Message, Message, Packet | None]:
        """Device starts binding as a Supplicant, by sending an Offer.

        Returns the Respondent's Accept, or raises an exception if the binding is
        unsuccessful (BindError).

        :param offer_codes: An iterable of codes to offer.
        :param confirm_code: An optional confirm code override.
        :param ratify_command: An optional ratification command to finalize binding.
        :return: A tuple containing the (tender, accept, affirm, ratify) objects.
        :raises exc.BindingFsmError: If already binding.
        """
        if self.is_binding:
            raise exc.BindingFsmError(
                f"{self}: bad State for binding as a Supplicant (is already binding)"
            )
        self.set_state(
            SuppSendOfferWaitForAccept
        )  # self._is_respondent = False

        vendor_code: str | None = None
        if ratify_command:
            if hasattr(ratify_command.payload, "vendor_code"):
                vendor_code = str(ratify_command.payload.vendor_code)
            elif (
                isinstance(ratify_command.payload, str)
                and len(ratify_command.payload) >= 16
            ):
                vendor_code = ratify_command.payload[14:16]

        # Step S1: Supplicant sends an Offer (makes Offer) and expects an Accept
        tender = await self._make_offer(offer_codes, vendor_code=vendor_code)
        accept = await self._wait_for_accept(tender)

        # Step S2: Supplicant sends a Confirm (confirms Accept)
        affirm = await self._confirm_accept(accept, confirm_code=confirm_code)

        # Step S3: Supplicant sends an Addenda (optional)
        if vendor_code and ratify_command is not None:
            self.set_state(SuppIsReadyToSendAddenda)  # HACK: easiest way
            ratify = await self._cast_addenda(accept, ratify_command)
        else:
            ratify = None

        # self._set_as_bound(tender, accept, affirm, ratify)
        return tender, accept, affirm, ratify

    async def _make_offer(
        self,
        codes: Iterable[Code | tuple[IndexT, Code]],
        vendor_code: str | None = None,
    ) -> Message:
        """Supp sends an Offer & returns the corresponding Message.

        :param codes: Codes to offer.
        :param vendor_code: Optional vendor specific code block.
        :return: The sent offer message.
        """
        # if vendor_code, send an 10E0

        # state = self.state
        intent = Intent(
            src=Address(self._dev.id),
            dst=Address(self._dev.id),
            action=Action.PUT_BIND,
            data={
                "verb": I_,
                "codes": codes,
                "vendor_code": vendor_code,
                SZ_OEM_CODE: vendor_code,
            },
        )
        msg: Message = await self._dispatcher.send(
            intent, priority=Priority.HIGH
        )

        # await state._fut
        self.state.cast_offer()
        return msg

    async def _wait_for_accept(
        self,
        tender: Message | Packet,
        timeout: float = _ACCEPT_WAIT_TIME,
    ) -> Message:
        """Supp waits timeout seconds for an Accept to arrive & returns it.

        :param tender: The previously sent offer message or packet.
        :param timeout: Time to wait in seconds.
        :return: The accept message.
        """
        return await self.state.wait_for_accept(timeout)

    async def _confirm_accept(
        self, accept: Message, confirm_code: Code | None = None
    ) -> Message:
        """Supp casts a Confirm on the basis of a rcvd Accept & returns the Confirm.

        :param accept: The received accept message.
        :param confirm_code: The code to confirm with.
        :return: The sent confirm message.
        """
        # HACK assumes all index same
        if accept.payload and hasattr(accept.payload, "index"):
            index = str(accept.payload.index)
        elif accept._dto.raw_payload:
            index = accept._dto.raw_payload[:2]
        else:
            index = "00"

        target_id = (
            accept.dst.id if accept.src.id == self._dev.id else accept.src.id
        )
        intent = Intent(
            src=Address(self._dev.id),
            dst=Address(target_id),
            action=Action.PUT_BIND,
            data={"verb": I_, "codes": confirm_code, "index": index},
        )
        msg: Message = await self._dispatcher.send(
            intent, priority=Priority.HIGH
        )

        await self.state.cast_confirm_accept()
        return msg

    async def _cast_addenda(
        self, accept: Message, command: CommandDTO
    ) -> Packet:
        """Supp casts an Addenda (the final 10E0 command).

        :param accept: The previously received accept message.
        :param command: The ratify command to cast.
        :return: The sent addenda packet.
        """
        gateway = getattr(self._dev, "_gateway", None)
        if gateway is not None:
            packet: Packet = await gateway._async_send_dto(
                command, priority=Priority.HIGH
            )
        else:
            send_dto = getattr(self._dispatcher, "_async_send_dto", None)
            if send_dto is not None:
                packet = await send_dto(command, priority=Priority.HIGH)
            else:
                raise RuntimeError(
                    "No gateway or dispatcher available to send DTO"
                )

        await self.state.cast_addenda()
        return packet


class BindingManager(BindingManagerRespondent, BindingManagerSupplicant):
    """Aggregate manager handling both Respondent and Supplicant flows."""

    _attr_role = BindRole.IS_UNKNOWN


#


class BindStateBase:
    """Base class for all phases within the Binding Finite State Machine."""

    _attr_role = BindRole.IS_UNKNOWN

    _cmds_sent: int = 0  # num of bind cmds sent
    _packets_rcvd: int = (
        0  # num of bind packets rcvd (incl. any echos of sender's own cmd)
    )

    _has_wait_timer: bool = False
    _retry_limit: int = SENDING_RETRY_LIMIT
    _timer_handle: asyncio.TimerHandle

    _next_ctx_state: type[
        BindStateBase
    ]  # next state, if successful transition

    def __init__(self, context: BindingManagerBase) -> None:
        """Initialize the binding state.

        :param context: The binding manager operating this state.
        """
        self._context = context
        self._loop = context._loop

        # Strong typing on Future ensures .result() correctly returns a Message
        self._fut: asyncio.Future[Message] = self._loop.create_future()
        _LOGGER.debug(
            "%s: Changing state from: %s to: %s",
            self,
            self._context.state,
            self,
        )

        if self._has_wait_timer:
            self._timer_handle = self._loop.call_later(
                WAITING_TIMEOUT_SECS,
                self._handle_wait_timer_expired,
                WAITING_TIMEOUT_SECS,
            )

    def __repr__(self) -> str:
        """Return an unambiguous string representation."""
        return f"{self.__class__.__name__} (tx={self._cmds_sent})"

    def __str__(self) -> str:
        """Return a human-readable string representation."""
        return self.__class__.__name__

    @property
    def context(self) -> BindingManagerBase:
        """Return the associated manager for this state."""
        return self._context

    async def _wait_for_fut_result(self, timeout: float) -> Message:
        """Wait timeout seconds for an expected event to occur.

        The expected event is defined by the State's sent_cmd, rcvd_msg methods.

        :param timeout: The maximum time to wait in seconds.
        :return: The message containing the expected result.
        """
        try:
            # Wrap in shield to prevent asyncio.wait_for from cancelling the future.
            await asyncio.wait_for(asyncio.shield(self._fut), timeout)
        except TimeoutError:
            self._handle_wait_timer_expired(timeout)
        else:
            self._set_context_state(self._next_ctx_state)

        return self._fut.result()  # may raise exception

    def _handle_wait_timer_expired(self, timeout: float) -> None:
        """Process an overrun of the wait timer when waiting for a Message.

        :param timeout: The timeout limit that was exceeded.
        """
        msg = (
            f"{self._context}: Failed to transition to {self._next_ctx_state}: "
            f"expected message not received after {timeout} secs"
        )

        _LOGGER.warning(msg)
        if not self._fut.done():
            self._fut.set_exception(exc.BindingTimeoutError(msg))
        self._set_context_state(DevHasFailedBinding)

    def _set_context_state(self, next_state: type[BindStateBase]) -> None:
        """Transition the parent manager to a new state.

        :param next_state: The class representing the next state.
        :raises exc.BindingFsmError: If the future was not yet completed.
        """
        if (
            not self._fut.done()
        ):  # if not BindRetryError, BindTimeoutError, msg
            raise exc.BindingFsmError  # or: self._fut.set_exception()
        self._context.set_state(next_state, result=self._fut)

    def send_cmd(self, command: CommandDTO) -> None:
        """Abstract method to handle an outgoing command.

        :param command: The command that is being sent.
        """
        raise NotImplementedError

    def rcvd_msg(self, msg: Message) -> None:
        """Abstract method to handle an incoming message.

        :param msg: The message that was received.
        """
        raise NotImplementedError

    @staticmethod
    def is_phase(command: CommandDTO | Message, phase: BindPhase) -> bool:
        """Evaluate if the given command or message corresponds to the specified binding phase.

        :param command: The command or message object.
        :param phase: The binding phase to test against.
        :return: True if the command is aligned with the phase, False otherwise.
        """
        if phase == BindPhase.RATIFY:
            return command.verb == I_ and command.code == Code._10E0
        if command.code != Code._1FC9:
            return False

        if isinstance(command, Message):
            addr1, addr2, addr3 = (
                command._addrs[0].id,
                command._addrs[1].id,
                command._addrs[2].id,
            )
            source = addr1
            # For 1FC9, addr3 is often the actual destination or equal to src
            destination = addr2 if addr2 != NON_DEVICE_ID else addr3
        else:
            addrs = [
                a
                for a in (command.addr1, command.addr2, command.addr3)
                if a != NON_DEVICE_ID
            ]
            source = DeviceIdT(addrs[0] if addrs else NON_DEVICE_ID)
            destination = DeviceIdT(addrs[1] if len(addrs) > 1 else source)

        if phase == BindPhase.TENDER:
            return command.verb == I_ and destination in (
                source,
                ALL_DEVICE_ID,
            )
        if phase == BindPhase.ACCEPT:
            # Historically, this was `dst is not src` on distinct Address objects, which was always True
            return command.verb == W_
        # if phase == BindPhase.AFFIRM:
        return command.verb == I_ and destination not in (
            source,
            ALL_DEVICE_ID,
        )

    # Respondent State APIs...
    async def wait_for_offer(self, timeout: float | None = None) -> Message:
        """Wait for an offer message from a supplicant."""
        raise exc.BindingFsmError(
            f"{self._context!r}: shouldn't wait_for_offer() from this State"
        )

    def cast_accept_offer(self) -> None:
        """Cast accept offer command to supplicant."""
        raise exc.BindingFsmError(
            f"{self._context!r}: shouldn't accept_offer() from this State"
        )

    async def wait_for_confirm(self, timeout: float | None = None) -> Message:
        """Wait for a confirmation message from supplicant."""
        raise exc.BindingFsmError(
            f"{self._context!r}: shouldn't wait_for_confirm() from this State"
        )

    async def wait_for_addenda(self, timeout: float | None = None) -> Message:
        """Wait for an optional addenda message from supplicant."""
        raise exc.BindingFsmError(
            f"{self._context!r}: shouldn't wait_for_addenda() from this State"
        )

    # Supplicant State APIs...
    def cast_offer(self, timeout: float | None = None) -> None:
        """Cast binding offer command to respondent."""
        raise exc.BindingFsmError(
            f"{self._context!r}: shouldn't make_offer() from this State"
        )

    async def wait_for_accept(self, timeout: float | None = None) -> Message:
        """Wait for an accept message from respondent."""
        raise exc.BindingFsmError(
            f"{self._context!r}: shouldn't wait_for_accept() from this State"
        )

    async def cast_confirm_accept(
        self, timeout: float | None = None
    ) -> Message:
        """Send confirmation of accepted offer to respondent."""
        raise exc.BindingFsmError(
            f"{self._context!r}: shouldn't confirm_accept() from this State"
        )

    async def cast_addenda(self, timeout: float | None = None) -> Message:
        """Send optional addenda message to respondent."""
        raise exc.BindingFsmError(
            f"{self._context!r}: shouldn't cast_addenda() from this State"
        )


class _DevIsWaitingForMsg(BindStateBase):
    """Device waits until it receives the anticipated Packet (Offer or Addenda).

    Failure occurs when the timer expires (timeout) before receiving the Packet.
    """

    _expected_packet_phase: BindPhase

    _wait_timer_limit: float = 5.1  # WAITING_TIMEOUT_SECS

    def __init__(self, context: BindingManagerBase) -> None:
        super().__init__(context)

        self._timer_handle = self._loop.call_later(
            self._wait_timer_limit,
            self._handle_wait_timer_expired,
            self._wait_timer_limit,
        )

    def _set_context_state(self, next_state: type[BindStateBase]) -> None:
        if self._timer_handle:
            self._timer_handle.cancel()
        super()._set_context_state(next_state)

    def rcvd_msg(self, msg: Message) -> None:
        """If the msg is the waited-for packet, transition to the next state."""
        if not self._fut.done() and self.is_phase(
            msg, self._expected_packet_phase
        ):
            self._fut.set_result(msg)


class _DevIsReadyToSendCmd(BindStateBase):
    """Device sends a Command (Confirm, Addenda) that wouldn't result in a reply Packet.

    Failure occurs when the retry limit is exceeded before receiving a Command echo.
    """

    _expected_cmd_phase: BindPhase

    _send_retry_limit: int = 0  # retries dont include the first send
    _send_retry_timer: float = 0.8  # retry if no echo received before timeout

    def __init__(self, context: BindingManagerBase) -> None:
        super().__init__(context)

        self._cmd: CommandDTO | None = None
        self._cmds_sent: int = 0

    def _retries_exceeded(self) -> None:
        """Process an overrun of the retry limit when sending a Command."""
        msg = (
            f"{self._context}: Failed to transition to {self._next_ctx_state}: "
            f"{self._expected_cmd_phase} command echo not received after "
            f"{self._retry_limit} retries"
        )

        _LOGGER.warning(msg)
        if not self._fut.done():
            self._fut.set_exception(exc.BindingFlowFailed(msg))
        self._set_context_state(DevHasFailedBinding)

    def send_cmd(self, command: CommandDTO) -> None:
        """If sending a cmd, expect the corresponding echo."""
        if not self.is_phase(command, self._expected_cmd_phase):
            return

        if self._cmds_sent > self._send_retry_limit:
            self._retries_exceeded()
        self._cmds_sent += 1
        self._cmd = self._cmd or command

    def rcvd_msg(self, msg: Message) -> None:
        """If the msg is the echo of the sent cmd, transition to the next state."""
        if self._fut.done():
            return
        if (
            self._cmd
            and msg.verb == self._cmd.verb
            and msg.code == self._cmd.code
            and msg.payload == self._cmd.payload
            and msg.src.id == self._cmd.addr1
        ) or (
            self.is_phase(msg, self._expected_cmd_phase)
            and msg.src.id == self._context._dev.id
        ):
            self._fut.set_result(msg)


class _DevSendCmdUntilReply(_DevIsWaitingForMsg, _DevIsReadyToSendCmd):
    """Device sends a Command (Offer, Accept), until it gets the expected reply Packet.

    Failure occurs when the timer expires (timeout) or the retry limit is exceeded
    before receiving a reply Packet.
    """

    def rcvd_msg(self, msg: Message) -> None:
        """If the msg is the expected reply, transition to the next state."""
        if not self._fut.done() and self.is_phase(
            msg, self._expected_packet_phase
        ):
            self._fut.set_result(msg)


class DevHasFailedBinding(BindStateBase):
    """Device has failed binding."""

    _attr_role = BindRole.IS_UNKNOWN


class DevIsNotBinding(BindStateBase):
    """Device is not binding."""

    _attr_role = BindRole.IS_DORMANT


#


class RespHasBoundAsRespondent(BindStateBase):
    """Respondent has received an Offer (+/- an Addenda) & has nothing more to do."""

    _attr_role = BindRole.IS_DORMANT

    def __init__(self, context: BindingManagerBase) -> None:
        """Initialize the respondent bound state."""
        super().__init__(context)
        _LOGGER.info("%s: Binding completed as respondent", context._dev.id)


class RespIsWaitingForAddenda(_DevIsWaitingForMsg, BindStateBase):
    """Respondent has received a Confirm & is waiting for an Addenda."""

    _attr_role = BindRole.RESPONDENT

    _expected_packet_phase: BindPhase = BindPhase.RATIFY
    _next_ctx_state: type[BindStateBase] = RespHasBoundAsRespondent

    async def wait_for_addenda(self, timeout: float | None = None) -> Message:
        """Wait for addenda message from supplicant."""
        return await self._wait_for_fut_result(timeout or _RATIFY_WAIT_TIME)


class RespSendAcceptWaitForConfirm(_DevSendCmdUntilReply, BindStateBase):
    """Respondent is ready to send an Accept & will expect a Confirm."""

    _attr_role = BindRole.RESPONDENT

    _expected_cmd_phase: BindPhase = BindPhase.ACCEPT
    _expected_packet_phase: BindPhase = BindPhase.AFFIRM
    _next_ctx_state: type[BindStateBase] = (
        RespHasBoundAsRespondent  # or: RespIsWaitingForAddenda
    )

    def cast_accept_offer(self) -> None:
        """Ignore any received Offer, other than the first."""
        pass

    async def wait_for_confirm(self, timeout: float | None = None) -> Message:
        """Wait for confirmation message from supplicant."""
        return await self._wait_for_fut_result(timeout or _AFFIRM_WAIT_TIME)


class RespIsWaitingForOffer(_DevIsWaitingForMsg, BindStateBase):
    """Respondent is waiting for an Offer."""

    _attr_role = BindRole.RESPONDENT

    _expected_packet_phase: BindPhase = BindPhase.TENDER
    _next_ctx_state: type[BindStateBase] = RespSendAcceptWaitForConfirm

    async def wait_for_offer(self, timeout: float | None = None) -> Message:
        """Wait for binding offer message from supplicant."""
        return await self._wait_for_fut_result(timeout or _TENDER_WAIT_TIME)


#


class SuppHasBoundAsSupplicant(BindStateBase):
    """Supplicant has sent a Confirm (+/- an Addenda) & has nothing more to do."""

    _attr_role = BindRole.IS_DORMANT

    def __init__(self, context: BindingManagerBase) -> None:
        """Initialize the supplicant bound state."""
        super().__init__(context)
        _LOGGER.info("%s: Binding completed as supplicant", context._dev.id)


class SuppIsReadyToSendAddenda(
    _DevIsReadyToSendCmd, BindStateBase
):  # send until echo, max_retry=1
    """Supplicant has sent a Confirm & is ready to send an Addenda."""

    _attr_role = BindRole.SUPPLICANT

    _expected_cmd_phase: BindPhase = BindPhase.RATIFY
    _next_ctx_state: type[BindStateBase] = SuppHasBoundAsSupplicant

    async def cast_addenda(self, timeout: float | None = None) -> Message:
        """Transmit addenda command to respondent."""
        return await self._wait_for_fut_result(timeout or _ACCEPT_WAIT_TIME)


class SuppIsReadyToSendConfirm(
    _DevIsReadyToSendCmd, BindStateBase
):  # send until echo, max_retry=1
    """Supplicant has received an Accept & is ready to send a Confirm."""

    _attr_role = BindRole.SUPPLICANT

    _expected_cmd_phase: BindPhase = BindPhase.AFFIRM
    _next_ctx_state: type[BindStateBase] = (
        SuppHasBoundAsSupplicant  # or: SuppIsReadyToSendAddenda
    )

    async def cast_confirm_accept(
        self, timeout: float | None = None
    ) -> Message:
        """Transmit confirmation command to respondent."""
        return await self._wait_for_fut_result(timeout or _ACCEPT_WAIT_TIME)


class SuppSendOfferWaitForAccept(_DevSendCmdUntilReply, BindStateBase):
    """Supplicant is ready to send an Offer & will expect an Accept."""

    _attr_role = BindRole.SUPPLICANT

    _expected_cmd_phase: BindPhase = BindPhase.TENDER
    _expected_packet_phase: BindPhase = BindPhase.ACCEPT
    _next_ctx_state: type[BindStateBase] = SuppIsReadyToSendConfirm

    def cast_offer(self, timeout: float | None = None) -> None:
        """Transmit offer command to respondent."""
        pass

    async def wait_for_accept(self, timeout: float | None = None) -> Message:
        """Wait for accept response from respondent."""
        return await self._wait_for_fut_result(timeout or _ACCEPT_WAIT_TIME)


#


class _BindStates:  # used for test suite
    IS_IDLE_DEVICE = DevIsNotBinding  # may send Offer
    NEEDING_TENDER = RespIsWaitingForOffer  # receives Offer, sends Accept
    NEEDING_ACCEPT = SuppSendOfferWaitForAccept  # receives Accept, sends
    NEEDING_AFFIRM = RespSendAcceptWaitForConfirm
    TO_SEND_AFFIRM = SuppIsReadyToSendConfirm
    NEEDING_RATIFY = RespIsWaitingForAddenda  # Optional: has sent Confirm
    TO_SEND_RATIFY = SuppIsReadyToSendAddenda  # Optional
    HAS_BOUND_RESP = RespHasBoundAsRespondent
    HAS_BOUND_SUPP = SuppHasBoundAsSupplicant
    IS_FAILED_RESP = DevHasFailedBinding
    IS_FAILED_SUPP = DevHasFailedBinding


_IS_NOT_BINDING_STATES = (
    DevHasFailedBinding,
    DevIsNotBinding,
    RespHasBoundAsRespondent,
    SuppHasBoundAsSupplicant,
)
