#!/usr/bin/env python3
"""RAMSES RF - Helper functions for packet transports."""

from __future__ import annotations

import logging
import re
import warnings
from string import printable

from ..const import I_, RP, RQ, W_

_LOGGER = logging.getLogger(__name__)


def _normalise(packet_line: str) -> str:
    """Perform any (transparent) frame-level hacks, as required at (near-)RF layer.

    Goals:
      - ensure an evofw3 provides the same output as a HGI80 (none, presently)
      - handle 'strange' packets (e.g. ``I|08:|0008``)

    :param packet_line: The raw packet string from the hardware.
    :type packet_line: str
    :return: The normalized packet string.
    :rtype: str
    """
    # ramses_esp-specific bugs, see: https://github.com/IndaloTech/ramses_esp/issues/1
    if "\r\r" in packet_line or packet_line.startswith(" 000"):
        warnings.warn(
            "Legacy ramses_esp <0.4.0 frame normalization is deprecated and will be removed in a future release.",
            DeprecationWarning,
            stacklevel=2,
        )
    packet_line = re.sub("\r\r", "\r", packet_line)
    if packet_line[:4] == " 000":
        packet_line = packet_line[1:]
    elif packet_line[:2] in (I_, RQ, RP, W_):
        packet_line = ""

    # evofw3 TX echo: lines starting with "# " followed by a verb
    # (I, W, RQ, RP) are hardware echoes of transmitted packets.
    # The "#" is evofw3's TX marker — it occupies the position where
    # an RSSI value would be in a normal RX frame.  Without this
    # normalisation, _partition() treats "#" as a comment delimiter,
    # leaving an empty packet body → PacketInvalid("Null packet").
    #
    # The canonical frame format uses " I" / " W" (leading space) for
    # 1-char verbs, but "RQ" / "RP" (no leading space) for 2-char verbs.
    # The evofw3 echo uses "# I" / "# W" / "# RQ" / "# RP" (single space
    # after #).  We normalise to the canonical RSSI-prefixed format:
    #   "# I --- ..." → "...  I --- ..."  (extra space for 1-char verb)
    #   "# RQ --- ..." → "... RQ --- ..." (no extra space for 2-char verb)
    # This produces a frame that parses correctly and can be matched by
    # _is_recent_tx() as a hardware echo (issue 1131).
    if packet_line.startswith("# "):
        _rest = packet_line[2:]
        # Check for 1-char verbs (I, W) — need extra space to form " I"
        if _rest.startswith((f"{I_.strip()} ", f"{W_.strip()} ")):
            packet_line = "...  " + _rest
        # Check for 2-char verbs (RQ, RP) — no extra space needed
        elif _rest.startswith((f"{RQ} ", f"{RP} ")):
            packet_line = "... " + _rest

    # pseudo-RAMSES-II packets (encrypted payload?)...
    if (
        packet_line[10:14] in (" 08:", " 31:")
        and packet_line[-16:] == "* Checksum error"
    ):
        packet_line = packet_line[:-17] + " # Checksum error (ignored)"

    # remove any "/r/n" (leading whitespeace is a problem for commands, but not packets)
    return packet_line.strip()


def _str(value: bytes) -> str:
    """Decode bytes to a string, ignoring non-printable characters.

    :param value: The bytes to decode.
    :type value: The bytes to decode.
    :return: The decoded string.
    :rtype: str
    """
    try:
        result = "".join(
            c for c in value.decode("ascii", errors="strict") if c in printable
        )
    except UnicodeDecodeError:
        _LOGGER.warning("%s < Can't decode bytestream (ignoring)", value)
        return ""
    return result


def redact_url(url: str | None) -> str:
    """Redact credentials from a URL for safe logging/display.

    Masks the ``user:pass`` portion of URLs like
    ``mqtt://user:pass@host:port/path`` → ``mqtt://***:***@host:port/path``.

    Handles ``mqtt://``, ``rfc2217://``, ``socket://``, and any other
    scheme that uses ``user:pass@host`` syntax.  URLs without
    credentials are returned unchanged.  ``None`` is returned as an
    empty string.

    :param url: The URL to redact.
    :type url: str | None
    :return: The URL with credentials masked, or the original string
        if it can't be parsed or has no credentials.
    :rtype: str
    """
    if not isinstance(url, str):
        return url or ""
    if "@" not in url:
        return url
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(url)
        if parsed.username:
            netloc = f"***:***@{parsed.hostname}"
            if parsed.port:
                netloc += f":{parsed.port}"
            return urlunparse(parsed._replace(netloc=netloc))
    except (ValueError, AttributeError, TypeError):
        pass
    return url
