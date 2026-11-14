"""RAMSES RF - Opentherm command intent to L3 payload translation."""

from ramses_rf.commands.builders.helpers import resolve_addrs
from ramses_rf.commands.core import Command
from ramses_rf.payloads.opentherm import OpenThermMsgPayload
from ramses_rf.protocol.opentherm import parity
from ramses_tx.const import DEFAULT_NUM_REPEATS, RQ, Code, Priority
from ramses_tx.dtos import CommandDTO


def build_get_opentherm_data(intent: Command) -> CommandDTO:
    """Translate a GET_OPENTHERM_DATA intent into a CommandDTO.

    :param intent: The GET_OPENTHERM_DATA intent. It is expected to
        contain the `msg_id` key (int | str) in its data dictionary.
    :return: A populated CommandDTO.
    """
    msg_id = intent.get("msg_id")

    if msg_id is None:
        raise ValueError("Missing 'msg_id' in intent data")

    msg_id_int = msg_id if isinstance(msg_id, int) else int(msg_id, 16)

    # Data-ID 0 (Master/Slave Status) carries the master's CH/DHW enable
    # flags in the request's high data byte. ramses cannot know the
    # controller's real flags, so a zeroed request reads as "CH disabled,
    # DHW disabled" and R8810 bridges stop the boiler (issue 1372).
    if msg_id_int == 0:
        raise ValueError(
            "OpenTherm Data-ID 0 (Status) must not be requested: the "
            "request's high data byte carries master CH/DHW enable "
            "flags that cannot be supplied safely (issue 1372)"
        )
    msg_type = 8 if parity(msg_id_int) else 0
    payload = OpenThermMsgPayload.create(
        opentherm_index=0,
        msg_id=msg_id_int,
        msg_type=msg_type,
        raw_value=b"\x00\x00",
    ).hex()

    addr1, addr2, addr3 = resolve_addrs(intent.src, intent.dst)

    return CommandDTO(
        verb=RQ,
        addr1=addr1,
        addr2=addr2,
        addr3=addr3,
        code=Code._3220,
        payload=payload,
        priority=Priority.DEFAULT,
        num_repeats=DEFAULT_NUM_REPEATS,
    )
