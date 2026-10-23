#!/usr/bin/env python3
"""RAMSES RF - Domain Routing and Addressing.

This module provides the strictly-typed, immutable Data Transfer Objects
responsible for L7 domain routing, replacing legacy L3 string parsing.
It acts as the foundation for the asynchronous event-driven SSOT.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from ramses_tx.const import Code, Verb
from ramses_tx.typing import DeviceIdT

if TYPE_CHECKING:
    pass


class EventTopic(StrEnum):
    """Strict enumeration of valid topics for the SSOT event bus."""

    INFORMATION = "information"  # Maps to ' I'
    TOPOLOGY_DISCOVERY = "topology_discovery"  # Maps to ' I' for 1FC9
    REQUEST = "request"  # Maps to 'RQ'
    WRITE = "write"  # Maps to ' W'
    RESPONSE = "response"  # Maps to 'RP'


@dataclass(frozen=True, slots=True)
class RoutingContext:
    """Represents the sub-payload context for routing within a device.

    This acts as a Secondary Routing Key, guaranteeing isolation between
    different sub-entities operating on the same physical device.
    """

    value: str | bool | None

    @property
    def as_string(self) -> str:
        """Format the context safely for legacy string interpolation.

        Examples:
            RoutingContext(True) -> "True"
            RoutingContext("00") -> "00"
            RoutingContext(None) -> "None"

        :return: The context value as a string.
        :rtype: str

        """
        if self.value is True:
            return "True"
        if self.value is False:
            return "False"
        return str(self.value)


def extract_context_value(  # type: ignore[explicit-any]  # duck-typed payload/str/None
    payload: Any, raw_payload: str = "", code: str | Code | None = None
) -> str | None:
    """Extract the primary routing context discriminator from a payload or string.

    Inspects typed PayloadBase objects or raw ASCII hex strings to derive the
    2-character routing context discriminator (e.g. zone index, domain ID,
    OpenTherm Data ID, or generic packet index) used to calculate
    RoutingContext and StateHeader instances.

    :param payload: Strongly-typed PayloadBase object, string, or None.
    :type payload: Any
    :param raw_payload: Unparsed wire ASCII hex string fallback, defaults to "".
    :type raw_payload: str
    :param code: Command opcode (e.g. '3220' or Code._3220), defaults to None.
    :type code: str | Code | None
    :returns: Uppercase 2-character context discriminator string, or None if
        non-contextual.
    :rtype: str | None
    """
    if payload is None:
        if code == Code._3220 and len(raw_payload) >= 6:
            return raw_payload[4:6]
        return raw_payload[:2] if len(raw_payload) >= 2 else None

    if isinstance(payload, str):
        if code == Code._3220 and len(payload) >= 6:
            return payload[4:6]
        return payload[:2] if len(payload) >= 2 else None

    if getattr(payload, "msg_id", None) is not None:
        msg_id_val = payload.msg_id
        return (
            f"{msg_id_val:02X}"
            if isinstance(msg_id_val, int)
            else str(msg_id_val)
        )

    if getattr(payload, "data_id", None) is not None:
        return str(payload.data_id)

    if getattr(payload, "domain_index", None) is not None:
        return str(payload.domain_index)

    if getattr(payload, "domain_id", None) is not None:
        return str(payload.domain_id)

    if getattr(payload, "index", None) is not None:
        return str(payload.index)

    if getattr(payload, "zone_index", None) is not None:
        zone_index_val = payload.zone_index
        return (
            f"{zone_index_val:02X}"
            if isinstance(zone_index_val, int)
            else str(zone_index_val)
        )

    if getattr(payload, "zone_index", None) is not None:
        zone_index_val = payload.zone_index
        return (
            f"{zone_index_val:02X}"
            if isinstance(zone_index_val, int)
            else str(zone_index_val)
        )

    if getattr(payload, "dhw_index", None) is not None:
        dhw_index_val = payload.dhw_index
        return (
            f"{dhw_index_val:02X}"
            if isinstance(dhw_index_val, int)
            else str(dhw_index_val)
        )

    if getattr(payload, "dhw_index", None) is not None:
        dhw_index_val = payload.dhw_index
        return (
            f"{dhw_index_val:02X}"
            if isinstance(dhw_index_val, int)
            else str(dhw_index_val)
        )

    if getattr(payload, "ufh_index", None) is not None:
        ufh_index_val = payload.ufh_index
        return (
            f"{ufh_index_val:02X}"
            if isinstance(ufh_index_val, int)
            else str(ufh_index_val)
        )

    if getattr(payload, "ufh_index", None) is not None:
        ufh_index_val = payload.ufh_index
        return (
            f"{ufh_index_val:02X}"
            if isinstance(ufh_index_val, int)
            else str(ufh_index_val)
        )

    if isinstance(payload, dict):
        z_index = payload.get("zone_index", payload.get("zone_index"))
        if z_index is not None:
            return (
                f"{z_index:02X}" if isinstance(z_index, int) else str(z_index)
            )
        d_index = payload.get(
            "domain_index",
            payload.get("domain_id", payload.get("domain_index")),
        )
        if d_index is not None:
            return str(d_index)
        dhw_index = payload.get("dhw_index", payload.get("dhw_index"))
        if dhw_index is not None:
            return (
                f"{dhw_index:02X}"
                if isinstance(dhw_index, int)
                else str(dhw_index)
            )
        ufh_index = payload.get("ufh_index", payload.get("ufh_index"))
        if ufh_index is not None:
            return (
                f"{ufh_index:02X}"
                if isinstance(ufh_index, int)
                else str(ufh_index)
            )

    return None


@dataclass(frozen=True, slots=True)
class StateHeader:
    """The primary immutable routing key for state caching and CQRS.

    Provides O(1) dictionary hashing and serves as the bridge toward
    the event-driven Master Plan (routing into topics and dest_ids).
    """

    code: Code | str
    verb: Verb | str
    source_id: DeviceIdT | str
    context: RoutingContext

    @classmethod
    def create(
        cls,
        code: Code | str,
        verb: Verb | str,
        source_id: DeviceIdT | str,
        context_value: str | bool | None,
    ) -> StateHeader:
        """Generate a StateHeader from primitive or rich variables.

        :param code: The message command code (e.g., '3220' or Code._3220).
        :type code: Code | str
        :param verb: The message verb (e.g., ' I', 'RP' or Verb.I_).
        :type verb: Verb | str
        :param source_id: The source L2 device ID (e.g., '01:123456').
        :type source_id: DeviceIdT | str
        :param context_value: The sub-payload context key.
        :type context_value: str | bool | None
        :return: The immutable StateHeader instance.
        :rtype: StateHeader
        """
        # Safely promote strings to rich types, gracefully falling back to
        # strings for unregistered OEM/Debug hardware codes.
        try:
            safe_code: Code | str = (
                Code(code) if isinstance(code, str) else code
            )
        except ValueError:
            safe_code = code

        try:
            safe_verb: Verb | str = (
                Verb(verb) if isinstance(verb, str) else verb
            )
        except ValueError:
            safe_verb = verb

        safe_src = (
            cast(DeviceIdT, source_id)
            if isinstance(source_id, str)
            else source_id
        )

        return cls(
            code=safe_code,
            verb=safe_verb,
            source_id=safe_src,
            context=RoutingContext(context_value),
        )

    @property
    def legacy_hdr(self) -> str:
        """Calculate the legacy state routing header natively.

        Format: '{code}|{verb}|{src_id}|{ctx_str}'
        Example: '3220|RP|01:123456|00'

        :return: The legacy pipe-separated routing string.
        :rtype: str
        """
        return f"{self.code}|{self.verb}|{self.source_id}|{self.context.as_string}"

    @property
    def topic(self) -> EventTopic:
        """Master Plan alignment: Generate the SSOT event topic.

        This provides the standardized envelope topic for the future
        DecodedMessage event stream based on the verb/code.

        :return: The strictly-typed event topic enum.
        :rtype: EventTopic
        """
        if self.code == Code._1FC9:
            return EventTopic.TOPOLOGY_DISCOVERY

        if self.verb == Verb.I_:
            return EventTopic.INFORMATION
        if self.verb == Verb.RQ:
            return EventTopic.REQUEST
        if self.verb == Verb.W_:
            return EventTopic.WRITE

        return EventTopic.RESPONSE
