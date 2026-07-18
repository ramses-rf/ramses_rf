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
