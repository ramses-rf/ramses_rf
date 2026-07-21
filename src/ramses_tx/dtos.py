"""
Data Transfer Objects for ramses_tx.

This module defines the strict boundaries for OSI layer decoupling
between the RF modem (L1-L3) and the Domain Model (L4-L7).
"""

from dataclasses import dataclass
from datetime import datetime as dt


@dataclass(frozen=True, slots=True)
class PacketDTO:
    """
    Pure data object bridging the ramses_tx modem and ramses_rf.

    :param timestamp: Time the frame was received.
    :type timestamp: datetime
    :param rssi: Received Signal Strength Indicator (e.g., "-72").
    :type rssi: str
    :param verb: The action verb (e.g., "RQ", "I", "W", "RP").
    :type verb: str
    :param seq: The sequence number (e.g., "003").
    :type seq: str
    :param addr1: Positional L2 Address 1 (e.g., "01:145038").
    :type addr1: str
    :param addr2: Positional L2 Address 2.
    :type addr2: str
    :param addr3: Positional L2 Address 3.
    :type addr3: str
    :param code: The packet code (e.g., "30C9").
    :type code: str
    :param length: The payload length.
    :type length: str
    :param payload: Raw hex payload string (e.g., "0001C8").
    :type payload: str
    """

    timestamp: dt
    rssi: str
    verb: str
    seq: str
    addr1: str
    addr2: str
    addr3: str
    code: str
    length: str
    payload: str


@dataclass(frozen=True, slots=True)
class CommandDTO:
    """
    Instructions strictly for L2/L3 transmission over the radio.

    :param verb: The action verb.
    :type verb: str
    :param addr1: Positional L2 Address 1.
    :type addr1: str
    :param addr2: Positional L2 Address 2.
    :type addr2: str
    :param addr3: Positional L2 Address 3.
    :type addr3: str
    :param code: The command code.
    :type code: str
    :param payload: Raw hex payload string.
    :type payload: str
    :param priority: Hardware queue priority (e.g., 1 High).
    :type priority: int
    :param num_repeats: Hardware repeat blasts to beat RF noise.
    :type num_repeats: int
    """

    verb: str
    addr1: str
    addr2: str
    addr3: str
    code: str
    payload: str
    priority: int = 1
    num_repeats: int = 1

    def __str__(self) -> str:
        """Return the string representation of the frame to be transmitted.

        The verb is stripped and right-justified to 2 characters to match the
        RF protocol format expected by the HGI80 (e.g. ``" W"`` not ``"W"``).
        Without this normalisation, verbs passed as plain strings (e.g. from
        the ramses_cc send_packet service) produce malformed frames that the
        HGI80 silently drops — no echo, causing a 20s QoS timeout.
        """
        verb = f"{str(self.verb).strip():>2}"
        return (
            f"{verb} --- {self.addr1} {self.addr2} {self.addr3} {self.code} "
            f"{int(len(self.payload) / 2):03d} {self.payload}"
        )

    @classmethod
    def from_cli(cls, cli_str: str) -> "CommandDTO":
        """Parse a CLI string into a CommandDTO."""
        verb = cli_str[:2]
        parts = cli_str[2:].split()
        if len(parts) > 0 and parts[0] == "---":
            parts.pop(0)

        addr1, addr2, addr3, code = parts[:4]
        if len(parts) == 5:
            payload = parts[4]
        elif len(parts) >= 6:
            payload = parts[5]
        else:
            payload = ""

        return cls(
            verb=verb,
            addr1=addr1,
            addr2=addr2,
            addr3=addr3,
            code=code,
            payload=payload,
        )

    @property
    def tx_header(self) -> str:
        """Return the QoS header of this (request) packet."""
        from .frame import pkt_header
        from .packet import Packet

        pkt = Packet._from_cmd(self)
        return str(pkt_header(pkt))

    @property
    def rx_header(self) -> str | None:
        """Return the QoS header of the expected Rx packet."""
        from .frame import pkt_header
        from .packet import Packet

        pkt = Packet._from_cmd(self)
        hdr = pkt_header(pkt, rx_header=True)
        return str(hdr) if hdr else None
