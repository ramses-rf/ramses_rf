"""RAMSES RF - Outbound Command Dispatcher."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ramses_rf.commands.builders import build_dto
from ramses_rf.commands.core import Command
from ramses_rf.enums import Action
from ramses_rf.interfaces import GatewayInterface
from ramses_rf.messages import Message
from ramses_rf.models.hvac_schemas import _2411_PARAMS_SCHEMA, FanParamInfo
from ramses_rf.payloads.hvac import HvacFanParamPayload
from ramses_tx import Priority
from ramses_tx.const import DEFAULT_WAIT_FOR_REPLY, RP, Code
from ramses_tx.dtos import CommandDTO
from ramses_tx.exceptions import ProtocolSendFailed

if TYPE_CHECKING:
    from ramses_tx import Packet

_QOS_TX_LIMIT = 12
# Data types the 2411 set_fan_param encoder understands (see build_set_fan_param)
_KNOWN_2411_DATA_TYPES: frozenset[str] = frozenset(
    {"00", "01", "0F", "10", "20", "90", "92"}
)
_LOGGER = logging.getLogger(__name__)


class CommandDispatcher:
    """Dispatches L7 Command intents to the L3 modem.

    Implements CQRS pattern by separating intent generation from payload
    construction and modem dispatch.
    """

    def __init__(self, gateway: GatewayInterface) -> None:
        """Initialize the dispatcher with a reference to the Gateway.

        :param gateway: The main gateway instance for sending L3 payloads.
        :type gateway: GatewayInterface
        """
        self._gateway = gateway

    def _check_transmission_policy(self, intent: Command) -> None:
        """Validate intent against destination device policies before transmission.

        :param intent: The command intent to evaluate.
        :type intent: Command
        :raises ProtocolSendFailed: If destination device is unreachable/deprecated.
        """
        registry = getattr(self._gateway, "device_registry", None)
        if registry is None:
            return

        target_dev = getattr(registry, "device_by_id", {}).get(intent.dst.id)
        if target_dev is None:
            return

        # 1. Check if unreachable device has exceeded transmission limit
        qos_count = getattr(target_dev, "_qos_tx_count", 0)
        if isinstance(qos_count, int) and qos_count > _QOS_TX_LIMIT:
            _LOGGER.warning(
                "%s < Sending was deprecated for %s", intent, target_dev
            )
            raise ProtocolSendFailed(
                f"Sending deprecated for {target_dev} (exceeded tx limit)"
            )

        # 2. Check if destination is a real battery-powered device
        is_faked = getattr(target_dev, "is_faked", False)
        has_battery = getattr(target_dev, "has_battery", None)
        is_battery_state = False
        if callable(has_battery):
            # Synchronous check if BatteryState is inherited
            from ramses_rf.devices.dev_base import BatteryState

            is_battery_state = isinstance(target_dev, BatteryState)

        if is_battery_state and not is_faked:
            _LOGGER.info(
                "%s < Sending inadvisable for %s (it has a battery)",
                intent,
                target_dev,
            )

    async def send(
        self,
        intent: Command,
        *,
        priority: Priority | None = None,
        wait_for_reply: bool | None = DEFAULT_WAIT_FOR_REPLY,
    ) -> Message:
        """Translate and send a high-level intent over the RF network.

        :param intent: The high-level intent to execute.
        :type intent: Command
        :param priority: Priority override for transmission.
        :type priority: Priority | None
        :param wait_for_reply: True if the L7 FSM should await a reply.
        :type wait_for_reply: bool | None
        :returns: The resulting Message domain object.
        :rtype: Message
        """
        self._check_transmission_policy(intent)
        if intent.action == Action.SET_FAN_PARAM:
            intent = await self._resolve_reported_param_schema(intent)
        dto: CommandDTO = build_dto(intent)
        conv_mgr = self._gateway.conversation_manager

        if wait_for_reply and conv_mgr is not None:
            # Coalesce identical in-flight RQ conversations: a second
            # byte-identical frame's echo and reply would be dropped by
            # pool dedup, stalling the send queue
            # (ramses-rf/ramses_cc#1336).
            pending_fut = conv_mgr.find_pending_reply(dto)
            if pending_fut is not None:
                return await asyncio.shield(pending_fut)
            rply_fut = await conv_mgr.track_intent(intent, dto)
            await self._gateway._async_send_dto(
                dto,
                priority=priority
                if priority is not None
                else Priority(dto.priority),
            )
            return await rply_fut

        packet: Packet = await self._gateway._async_send_dto(
            dto,
            priority=priority
            if priority is not None
            else Priority(dto.priority),
        )
        msg = Message._from_packet(packet)
        msg._pkt = packet  # type: ignore[attr-defined]
        return msg

    async def _resolve_reported_param_schema(self, intent: Command) -> Command:
        """Prefer the device-reported 2411 parameter definition for a write.

        Fans state each parameter's data_type/min/max/precision in their
        RP/2411 responses. When a retained response from the destination
        fan defines the parameter being written, that definition wins over
        the static schema — the tables can disagree per model (e.g. param
        52 is a trickle timer on some models and sensor sensitivity on
        others; issue 1332). Anything incomplete or unparsable falls
        back to the static schema.

        :param intent: The SET_FAN_PARAM intent to enrich.
        :type intent: Command
        :returns: The intent, with fan_param_schema set when a usable
                  device-reported definition was found.
        :rtype: Command
        """
        param_id = str(intent.get("param_id", "")).strip().upper()
        store = self._gateway.message_store
        if len(param_id) != 2 or store is None:
            return intent
        try:
            param_id_int = int(param_id, 16)
        except ValueError:
            return intent

        messages = await store.get(
            source=intent.dst.id, code=Code._2411, verb=RP
        )
        for msg in sorted(messages, key=lambda m: m.dtm, reverse=True):
            try:
                definition = HvacFanParamPayload.from_bytes(
                    bytes.fromhex(msg.raw_payload)
                )
            except ValueError:
                continue
            if definition.parameter_id != param_id_int:
                continue
            if reported := self._reported_param_schema(param_id, definition):
                _LOGGER.debug(
                    "%s < 2411 param %s: using device-reported schema "
                    "(data_type %s)",
                    intent.dst.id,
                    param_id,
                    reported.data_type,
                )
                intent = intent.with_data(fan_param_schema=reported)
            break
        return intent

    @staticmethod
    def _reported_param_schema(
        param_id: str, definition: HvacFanParamPayload
    ) -> FanParamInfo | None:
        """Build a FanParamInfo from a device-reported 2411 definition.

        :param param_id: The 2-digit hex parameter ID (for the description).
        :type param_id: str
        :param definition: The parsed RP/2411 parameter definition.
        :type definition: HvacFanParamPayload
        :returns: A FanParamInfo, or None if the reported definition is
                  incomplete or uses a data type the encoder cannot handle.
        :rtype: FanParamInfo | None
        """
        data_type = f"{definition.data_type:02X}"
        if (
            definition._raw_3b is not None  # short/N-A reply, not a schema
            or data_type not in _KNOWN_2411_DATA_TYPES
            or definition.min_value_scaled is None
            or definition.max_value_scaled is None
            or definition.precision_scaled is None
        ):
            return None
        static = _2411_PARAMS_SCHEMA.get(param_id)
        return FanParamInfo(
            description=(
                static.description if static else f"Parameter {param_id}"
            ),
            min_value=float(definition.min_value_scaled),
            max_value=float(definition.max_value_scaled),
            precision=float(definition.precision_scaled) or 1.0,
            data_type=data_type,
            data_unit=static.data_unit if static else "",
        )

    def send_background(
        self,
        intent: Command,
        *,
        priority: Priority | None = None,
        wait_for_reply: bool | None = DEFAULT_WAIT_FOR_REPLY,
    ) -> asyncio.Task[Message]:
        """Schedule command intent transmission as a background task.

        :param intent: The high-level intent to execute.
        :type intent: Command
        :param priority: Priority override for transmission.
        :type priority: Priority | None
        :param wait_for_reply: True if the L7 FSM should await a reply.
        :type wait_for_reply: bool | None
        :returns: An asyncio Task resolving to the resulting Message.
        :rtype: asyncio.Task[Message]
        """
        coro = self.send(
            intent,
            priority=priority,
            wait_for_reply=wait_for_reply,
        )
        loop = (
            getattr(self._gateway, "_loop", None) or asyncio.get_running_loop()
        )
        task = loop.create_task(coro)

        def _clear_exc(fut: asyncio.Task[object]) -> None:
            if not fut.cancelled() and fut.exception():
                _LOGGER.debug(
                    "Background intent task failed: %s", fut.exception()
                )

        task.add_done_callback(_clear_exc)
        add_task = getattr(self._gateway, "add_task", None)
        if callable(add_task):
            add_task(task)
        return task
