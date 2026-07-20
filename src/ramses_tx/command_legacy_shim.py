"""RAMSES RF - Legacy Command Shim Adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ramses_tx.command import Command

if TYPE_CHECKING:
    from ramses_tx.dtos import CommandDTO


class LegacyCommandShim:
    """Temporary adapter bridging L3 CommandDTO back to legacy ramses_tx.Command."""

    @staticmethod
    def from_dto(dto: CommandDTO) -> Command:
        """Synthesize a legacy Command from a CommandDTO for discovery and protocol layers."""
        frame = (
            f"{dto.verb} --- {dto.addr1} {dto.addr2} {dto.addr3} {dto.code} "
            f"{int(len(dto.payload) / 2):03d} {dto.payload}"
        )
        return Command(frame)

    @staticmethod
    def _puzzle(msg_type: str | None = None, message: str = "") -> Command:
        from ramses_tx.address import ALL_DEV_ADDR, NON_DEV_ADDR
        from ramses_tx.const import I_, LOOKUP_PUZZ, Code
        from ramses_tx.dtos import CommandDTO
        from ramses_tx.helpers import hex_from_str, timestamp
        from ramses_tx.version import VERSION

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

        from ramses_tx.address import HGI_DEV_ADDR

        return LegacyCommandShim.from_dto(
            CommandDTO(
                verb=I_,
                addr1=HGI_DEV_ADDR.id,
                addr2=ALL_DEV_ADDR.id,
                addr3=NON_DEV_ADDR.id,
                code=Code._PUZZ,
                payload=payload[:48],
            )
        )
