"""RAMSES RF - Outbound Command Dispatcher."""

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
        :return: The resulting Packet from the modem (or an RP if requested).
        """
        dto: CommandDTO = build_dto(intent)

        if (
            wait_for_reply
            and getattr(self._gwy, "conversation_manager", None) is not None
        ):
            await self._gwy.conversation_manager.track_intent(intent, dto)

        return await self._gwy.async_send_cmd(
            dto,
            priority=priority if priority is not None else Priority(dto.priority),
            wait_for_reply=wait_for_reply,
        )
