"""RAMSES RF - Heat command intent to L3 payload translation."""

from ramses_rf.commands.builders.helpers import resolve_addrs
from ramses_rf.commands.core import Command
from ramses_tx.const import DEFAULT_NUM_REPEATS, I_, Code, Priority
from ramses_tx.dtos import CommandDTO
from ramses_tx.helpers import hex_from_temp


def build_put_outdoor_temp(intent: Command) -> CommandDTO:
    """Translate a PUT_OUTDOOR_TEMP intent into a CommandDTO."""
    temperature = intent.get("temperature")
    payload = f"00{hex_from_temp(temperature)}"
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=I_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._0002,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_put_dhw_temp(intent: Command) -> CommandDTO:
    """Translate a PUT_DHW_TEMP intent into a CommandDTO."""
    temperature = intent.get("temperature")
    payload = f"00{hex_from_temp(temperature)}"
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=I_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._1260,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )


def build_put_sensor_temp(intent: Command) -> CommandDTO:
    """Translate a PUT_SENSOR_TEMP intent into a CommandDTO."""
    temperature = intent.get("temperature")
    zone_idx = intent.get("zone_idx", "00")
    payload = f"{zone_idx}{hex_from_temp(temperature)}"
    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=I_,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._30C9,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )
