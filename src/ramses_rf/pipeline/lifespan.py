"""RAMSES RF - Packet lifespan (TTL) heuristics."""

from datetime import timedelta as td
from typing import Final

from ramses_tx.const import I_, RP, RQ, W_, Code
from ramses_tx.packet import Packet

# Pre-allocated timedelta objects for high-performance lifespan evaluations
TD_SECS_000: Final[td] = td(seconds=0)
TD_SECS_360: Final[td] = td(seconds=360)
TD_MINS_060: Final[td] = td(minutes=60)
TD_DAYS_001: Final[td] = td(minutes=60 * 24)


def pkt_lifespan(pkt: Packet) -> td:
    """Return the duration before packet state payload data expires.

    :param pkt: Packet instance to evaluate
    :type pkt: Packet
    :returns: Timedelta duration before packet expires
    :rtype: td
    """
    if pkt.verb in (RQ, W_):
        return TD_SECS_000

    match pkt.code:
        case Code._0005 | Code._000C | Code._0404 | Code._10E0:
            return TD_DAYS_001

        case Code._0006:
            return TD_MINS_060

        # pkt._len > 3 checks if the packet has an array
        case Code._000A if pkt._len > 3:
            return TD_MINS_060  # sends I /1h

        case Code._1F09:
            # can't do better than 300s with reading the payload
            return TD_SECS_360 if pkt.verb == I_ else TD_SECS_000

        case Code._1FC9 if pkt.verb == RP:
            return TD_DAYS_001  # TODO: check other verbs, they seem variable

        # pkt._len > 3 checks if the packet has an array
        case Code._2309 | Code._30C9 if pkt._len > 3:
            return TD_SECS_360  # sends I /sync_cycle

        case _:
            return TD_MINS_060  # applies to lots of HVAC packets
