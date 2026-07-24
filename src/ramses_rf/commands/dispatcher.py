"""RAMSES RF - Outbound Command Dispatcher."""

from typing import cast

from ramses_rf.commands.builders import build_dto
from ramses_rf.commands.core import Command
from ramses_rf.interfaces import GatewayInterface
from ramses_tx import Packet, Priority
from ramses_tx.const import DEFAULT_WAIT_FOR_REPLY
from ramses_tx.dtos import CommandDTO


class CommandDispatcher:
    """Dispatches L7 Command intents to the L3 modem.

    Implements CQRS pattern by separating intent generation from payload
    construction and modem dispatch.
    """

    def __init__(self, gwy: GatewayInterface) -> None:
        """Initialize the dispatcher with a reference to the Gateway.

        :param gwy: The main gateway instance for sending L3 payloads.
        """
        self._gwy = gwy

    async def send(
        self,
        intent: Command,
        *,
        priority: Priority | None = None,
        wait_for_reply: bool | None = DEFAULT_WAIT_FOR_REPLY,
    ) -> Packet:
        """Translate and send a high-level intent over the RF network.

        :param intent: The high-level intent to execute.
        :type intent: Command
        :param priority: Priority override for transmission.
        :type priority: Priority | None
        :param wait_for_reply: True if the L7 FSM should await a reply.
        :type wait_for_reply: bool | None
        :returns: The resulting Packet from the modem (or RP packet).
        :rtype: Packet
        """
        dto: CommandDTO = build_dto(intent)
        conv_mgr = getattr(self._gwy, "conversation_manager", None)

        if wait_for_reply and conv_mgr is not None:
            rply_fut = await conv_mgr.track_intent(intent, dto)
            await self._gwy.async_send_cmd(
                dto,
                priority=priority if priority is not None else Priority(dto.priority),
            )
            msg = await rply_fut
            return cast(Packet, msg._pkt)

        return await self._gwy.async_send_cmd(
            dto,
            priority=priority if priority is not None else Priority(dto.priority),
        )
