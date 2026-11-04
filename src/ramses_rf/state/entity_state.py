#!/usr/bin/env python3
"""RAMSES RF - State Storage and Database Query Component.

This module provides the EntityState component, which manages database
interactions and state querying for an entity, bridging the Event-Driven
architecture using native StateHeader dictionary lookups.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime as dt
from typing import TYPE_CHECKING, Any, Literal, overload

from ramses_rf.const import SZ_DHW_INDEX, SZ_DOMAIN_INDEX, SZ_ZONE_INDEX
from ramses_rf.typing import SchemaT
from ramses_tx.address import ALL_DEVICE_ID

# noqa: F401, isort: skip, pylint: disable=unused-import
from ramses_tx.const import F9, FA, FC, I_, RP, RQ, Code, Verb
from ramses_tx.typing import PayDictT

from .. import exceptions as exc
from ..messages import Message
from ..protocol.ramses import CODE_NAME_LOOKUP
from ..routing import RoutingContext, StateHeader

if TYPE_CHECKING:
    from ramses_tx.typing import HeaderT

    from ..interfaces import DeviceInterface, EntityInterface, GatewayInterface

_LOGGER = logging.getLogger(__name__)

# Constants for slicing the device ID
_ID_SLICE = 9


class StateCache:
    """Encapsulates the state cache data collection for an entity.

    Now natively powered by O(1) dictionary lookups via StateHeader DTOs.
    """

    def __init__(self) -> None:
        """Initialize the StateCache."""
        self._cache: dict[StateHeader, Message] = {}

    def add(self, msg: Message) -> None:
        """Add a message to the cache natively via its header."""
        self._cache[msg.state_header] = msg

    def get_message(self, header: StateHeader) -> Message | None:
        """Retrieve a message directly by its StateHeader."""
        return self._cache.get(header)

    def get_by_routing_key(
        self, code: Code | str, verb: Verb | str, context: str | bool | None
    ) -> Message | None:
        """Fallback O(N) lookup when source_id is unknown."""
        context_str = RoutingContext(context).as_string
        for hdr, msg in self._cache.items():
            if (
                hdr.code == code
                and hdr.verb == verb
                and hdr.context.as_string == context_str
            ):
                return msg
        return None

    def get_by_code(self, code: Code | str) -> list[Message]:
        """Retrieve all messages for a specific code."""
        return [msg for hdr, msg in self._cache.items() if hdr.code == code]

    def get_all(self) -> list[Message]:
        """Retrieve all stored messages."""
        return list(self._cache.values())

    def get_records(
        self,
    ) -> list[tuple[Code | str, Verb | str, str | bool | None, Message]]:
        """Retrieve all cache records as tuples of (code, verb, ctx, msg)."""
        return [
            (h.code, h.verb, h.context.value, m)
            for h, m in self._cache.items()
        ]


class EntityState:
    """Manages database interactions and state queries for an entity.

    This class acts as a stateless facade. It delegates all heavy lifting
    to the Gateway's central MessageStore while serving as the SSOT ingestion
    point.
    """

    def __init__(
        self,
        entity: EntityInterface | DeviceInterface,
        gateway: GatewayInterface,
    ) -> None:
        """Initialize the EntityState."""
        self._entity = entity
        self._gateway = gateway
        # Context-Aware O(1) Dictionary natively keyed by StateHeader DTO
        self._current_state: dict[StateHeader, Message] = {}
        self._log_cursor: int = 0  # Tracks cursor position in global log

        # Tracks DB tasks to maintain Message object immutability
        self._pending_deletes: set[StateHeader] = set()
        self._delete_tasks: set[asyncio.Task[object]] = set()

    def _sync_state(self) -> None:
        """Hybrid Push Model: Sync O(1) cache using cursor on global log."""
        if self._gateway.message_store is None:
            return

        log_iterable = self._gateway.message_store.log_by_dtm

        current_len = len(log_iterable)
        if current_len == self._log_cursor:
            return  # No new packets, O(1) instant exit

        if current_len < self._log_cursor:
            # The global log was cleared (e.g. cache reset). Rebuild.
            self._log_cursor = 0
            self._current_state.clear()
            self._pending_deletes.clear()
            for task in self._delete_tasks:
                if not task.done():
                    task.cancel()
            self._delete_tasks.clear()

        # Only process NEW packets appended since the last sync
        new_msgs = log_iterable[self._log_cursor :]
        for msg in new_msgs:
            self.ingest_message(msg)

        self._log_cursor = current_len

    def ingest_message(self, msg: Message) -> None:
        """SSOT async ingestion queue preparation.

        In , this will drop the DecodedMessage into an asyncio.Queue.
        For now, it processes the state synchronously.
        """
        self.update_state(msg)

    def update_state(self, msg: Message) -> None:
        """Push model: Instantly cache relevant messages upon arrival."""
        if not self._is_relevant_msg(msg):
            return

        entity_id = self._entity.id
        is_dhw = entity_id[_ID_SLICE:] == "_HW"
        is_zone = len(entity_id) > 9 and not is_dhw
        context_value = msg.context.value

        if is_zone:
            zone_index = entity_id[_ID_SLICE + 1 :]
            in_dict = isinstance(msg.payload, dict)
            in_list = isinstance(msg.payload, list)
            if not (
                context_value == zone_index
                or (
                    in_dict
                    and (
                        str(msg.payload.get(SZ_ZONE_INDEX)) == zone_index
                        or str(msg.payload.get("zone_index")) == zone_index
                    )
                )
                or (
                    in_list
                    and any(
                        isinstance(d, dict)
                        and (
                            str(d.get(SZ_ZONE_INDEX)) == zone_index
                            or str(d.get("zone_index")) == zone_index
                        )
                        for d in msg.payload
                    )
                )
            ):
                return  # Payload does not belong to this specific zone

        if is_dhw:
            in_dict = isinstance(msg.payload, dict)
            in_list = isinstance(msg.payload, list)
            if not (
                context_value in ("FC", "FA", "F9", "FA")
                or (
                    in_dict
                    and (
                        SZ_DHW_INDEX in msg.payload
                        or "dhw_index" in msg.payload
                    )
                )
                or (
                    in_list
                    and any(
                        isinstance(d, dict)
                        and (SZ_DHW_INDEX in d or "dhw_index" in d)
                        for d in msg.payload
                    )
                )
            ):
                return  # Payload does not belong to DHW

        # Context-aware O(1) Overwrite directly keyed by DTO
        self._current_state[msg.state_header] = msg

    def _is_relevant_msg(self, msg: Message) -> bool:
        """Check if a central MessageStore packet is relevant to entity."""
        return bool(
            msg.src.id == self._entity.id[:_ID_SLICE]
            or (msg.dst.id == self._entity.id[:_ID_SLICE] and msg.verb != RQ)
            or (msg.dst.id == ALL_DEVICE_ID and msg.code == Code._1FC9)
        )

    async def get_all_messages(self) -> list[Message]:
        """Return a flattened list of all messages logged on this device."""
        cache = await self._build_state_cache()
        return cache.get_all()

    def get_cached_value(  # type: ignore[explicit-any]  # heterogeneous payload value
        self,
        code: Code | str,
        key: str | None = "*",
        *,
        src_id: str | None = None,
    ) -> Any:
        """Return the newest cached payload for a code — synchronously.

        Unlike the async :meth:`get_value`, this reads only the
        in-memory state (after an incremental sync from the message
        log) and performs no database queries, so it is safe to call
        from synchronous contexts (e.g. properties).

        :param code: The packet code to look up (e.g. ``Code._10E0``).
        :type code: Code | str
        :param key: Payload key to extract; ``"*"`` (default) returns
            the full payload dict.
        :type key: str | None
        :param src_id: When given, only consider messages *sent by* this
            device id.  Replies addressed to the entity (dst match) are
            relevant to its state cache but describe the sender, not the
            entity — e.g. a gateway's RQ|10E0 poll makes every device's
            RP|10E0 land in the HGI's cache.
        :type src_id: str | None
        :return: The value from the most recent matching I/RP message
            for the code, or ``None`` if none has been seen.
        :rtype: Any
        """
        self._sync_state()
        msgs = [
            m
            for m in self._current_state.values()
            if m.code == code
            and m.verb in (I_, RP)
            and (src_id is None or m.src.id == src_id)
        ]
        msg = max(msgs, key=lambda m: m.dtm) if msgs else None
        return self._msg_value_msg(msg, key=key)

    _msg_list = get_all_messages

    def _add_record(
        self,
        device_id: str,
        code: Code | str | None = None,
        verb: Verb | str = I_,
        payload: str = "00",
    ) -> None:
        """Add a (dummy) record to the central SQLite MessageStore."""
        if self._gateway.message_store:
            self._gateway.message_store.add_record(
                device_id, code=str(code), verb=verb, payload=payload
            )

    async def _delete_msg(self, msg: Message) -> None:
        """Remove the msg from the central state databases."""
        if self._gateway.message_store:
            await self._gateway.message_store.rem(msg)
        self._pending_deletes.discard(msg.state_header)

    async def _get_msg_by_hdr(
        self, hdr: HeaderT | StateHeader
    ) -> Message | None:
        """Return a msg, if any, that matches a given header."""
        if isinstance(hdr, str):
            code_str, verb_str, src_id, *args = hdr.split("|")
            ctx_val: str | bool | None = args[0] if args else None
            if ctx_val == "True":
                ctx_val = True
            elif ctx_val == "False":
                ctx_val = False
            header = StateHeader.create(code_str, verb_str, src_id, ctx_val)
        else:
            header = hdr

        if self._gateway.message_store:
            store = self._gateway.message_store
            msgs = await store.get(hdr=header)
            if msgs:
                if (
                    msgs[0].state_header != header
                    and msgs[0].state_header.legacy_hdr != hdr
                ):
                    raise exc.DatabaseQueryError(
                        f"Header mismatch: {msgs[0].state_header.legacy_hdr} != {hdr}"
                    )
                return msgs[0]
            return None

        cache = await self._build_state_cache()
        msg = cache.get_message(header)

        # Fallbacks for unknown source ID routing
        if msg is None:
            msg = cache.get_by_routing_key(
                header.code, header.verb, header.context.value
            )
        if msg is None:
            msg = cache.get_by_routing_key(header.code, header.verb, False)
        if msg is None:
            msg = cache.get_by_routing_key(header.code, header.verb, None)

        if msg is None:
            return None

        if msg.state_header != header and msg.state_header.legacy_hdr != hdr:
            raise exc.DatabaseQueryError(
                f"Header mismatch: {msg.state_header.legacy_hdr} != {hdr}"
            )
        return msg

    async def get_flag(
        self, code: Code | str, key: str, index: int
    ) -> bool | None:
        """Get the boolean value of a specific flag within a payload."""
        if flags := await self.get_value(code, key=key):
            return bool(flags[index])
        return None

    _msg_flag = get_flag

    @overload
    async def get_value(
        self,
        code: Literal[Code._3150] | Literal[Code._0008],
        *args: object,
        **kwargs: object,
    ) -> float | None: ...
    @overload
    async def get_value(
        self,
        code: Literal[Code._1030],
        *args: object,
        **kwargs: object,
    ) -> PayDictT._1030 | None: ...
    @overload
    async def get_value(  # type: ignore[explicit-any]  # heterogeneous payload value
        self,
        code: Literal[Code._10E0] | Literal[Code._1060],
        *args: object,
        **kwargs: object,
    ) -> Any: ...
    @overload
    async def get_value(
        self,
        code: Literal[Code._0006],
        *args: object,
        **kwargs: object,
    ) -> int | None: ...
    @overload
    async def get_value(
        self,
        code: Literal[Code._1100],
        *args: object,
        **kwargs: object,
    ) -> PayDictT._1100 | None: ...
    @overload
    async def get_value(
        self,
        code: Literal[Code._10A0],
        *args: object,
        **kwargs: object,
    ) -> PayDictT._10A0 | None: ...
    @overload
    async def get_value(
        self,
        code: Literal[Code._1F09],
        *args: object,
        key: str | None = None,
        **kwargs: object,
    ) -> PayDictT._1F09 | int | float | None: ...
    @overload
    async def get_value(  # type: ignore[explicit-any]  # heterogeneous payload value
        self,
        code: Code | str | tuple[Code | str, ...] | Message,
        *args: object,
        **kwargs: object,
    ) -> Any: ...
    async def get_value(  # type: ignore[explicit-any]  # heterogeneous passthrough
        self,
        code: Code | str | tuple[Code | str, ...] | Message,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Get the value for a Code from the database or from a Message."""
        if isinstance(code, (str, tuple)):
            return await self._msg_value_code(code, *args, **kwargs)

        assert isinstance(code, Message), f"Invalid format: get_value({code})"
        return self._msg_value_msg(code, *args, **kwargs)

    _msg_value = get_value

    async def _msg_value_code(  # type: ignore[explicit-any]  # heterogeneous passthrough
        self,
        code: Code | str | tuple[Code | str, ...],
        verb: Verb | str | None = None,
        key: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Query the index for the most recent key: value pairs."""
        assert not isinstance(code, tuple) or verb is None, (
            f"Unsupported: using a tuple ({code}) with a verb ({verb})"
        )

        self._sync_state()
        src_id = kwargs.pop("src_id", None)

        if verb:
            if verb == Verb.RQ:
                assert not isinstance(code, tuple), (
                    f"Unsupported: using a keyword ({key}) with verb RQ"
                )
                key = None
            try:
                cd = await self.find_latest_code(
                    code, key, src_id=src_id, **kwargs, verb=verb
                )
                if cd:
                    cache = await self._build_state_cache()

                    # Retrieve the exact context-aware message
                    entity_id = self._entity.id
                    is_dhw = entity_id[_ID_SLICE:] == "_HW"
                    is_zone = len(entity_id) > 9 and not is_dhw
                    context_value = kwargs.get(
                        "zone_index",
                        entity_id[_ID_SLICE + 1 :] if is_zone else None,
                    )
                    if not context_value and is_dhw:
                        context_value = kwargs.get("dhw_index", "HW")

                    msg = cache.get_by_routing_key(cd, verb, context_value)
                    if msg is None:
                        # Fallbacks for base devices
                        msg = cache.get_by_routing_key(cd, verb, False)
                    if msg is None:
                        msg = cache.get_by_routing_key(cd, verb, None)
                else:
                    msg = None
            except KeyError:
                msg = None
        elif isinstance(code, tuple):
            msgs_dict = await self.get_message_log_flat()
            msgs_list = [
                m
                for m in msgs_dict.values()
                if m.code in code and (src_id is None or m.src.id == src_id)
            ]
            msg = max(msgs_list, key=lambda m: m.dtm) if msgs_list else None
        else:
            msgs_dict = await self.get_message_log_flat()
            msg = msgs_dict.get(code)
            if src_id is not None and msg is not None and msg.src.id != src_id:
                # The newest cached msg for this code is a reply
                # addressed to us (dst-matched), sent by another device.
                msg = None

        return self._msg_value_msg(msg, key=key, **kwargs)

    def _msg_value_msg(  # type: ignore[explicit-any]  # heterogeneous payload value
        self,
        msg: Message | None,
        key: str | None = "*",
        zone_index: str | None = None,
        domain_id: str | None = None,
    ) -> Any:
        """Get all or a specific key with its values from a Message."""
        if msg is None:
            return None

        if getattr(msg, "_expired", False):
            hdr = msg.state_header
            if hdr not in self._pending_deletes:
                loop = getattr(self._gateway, "_loop", None)
                if loop is None:
                    with contextlib.suppress(RuntimeError):
                        loop = asyncio.get_running_loop()

                # Protect against the Mock Trap during testing
                if isinstance(loop, asyncio.AbstractEventLoop):
                    self._pending_deletes.add(hdr)
                    task = loop.create_task(self._delete_msg(msg))
                    if task is not None:
                        self._delete_tasks.add(task)
                        task.add_done_callback(self._delete_tasks.discard)

        payload = msg.payload

        if msg.code == Code._1FC9:
            return [x[1] for x in payload]

        filter_index: str | None = None
        filter_val: str | None = None

        if domain_id:
            filter_index, filter_val = SZ_DOMAIN_INDEX, domain_id
        elif zone_index:
            filter_index, filter_val = SZ_ZONE_INDEX, zone_index

        def _matches_filter(d: SchemaT) -> bool:
            if not filter_index:
                return True
            val = d.get(filter_index)
            if val is None:
                if filter_index == SZ_ZONE_INDEX:
                    val = d.get("zone_index")
                elif filter_index == SZ_DOMAIN_INDEX:
                    val = d.get("domain_id", d.get("domain_index"))
            return val == filter_val

        if isinstance(payload, dict):
            msg_dict = payload
            if (
                filter_index
                and filter_index != SZ_DOMAIN_INDEX
                and not _matches_filter(msg_dict)
            ):
                return None
        elif filter_index:
            msg_dict = {
                k: v
                for d in payload
                for k, v in d.items()
                if _matches_filter(d)
            }
            if not msg_dict:
                return None
        else:
            if not payload:
                return None
            if isinstance(payload, list) and (key == "*" or not key):
                return payload
            msg_dict = payload[0]

        if key == "*" or not key:
            return {
                k: v
                for k, v in msg_dict.items()
                if k
                not in (
                    "dhw_index",
                    "dhw_index",
                    "domain_index",
                    "domain_id",
                    "domain_index",
                    "zone_index",
                    "zone_index",
                    SZ_DHW_INDEX,
                    SZ_DOMAIN_INDEX,
                    SZ_ZONE_INDEX,
                )
                and k[:1] != "_"
            }
        return msg_dict.get(key)

    async def _msg_dev_qry(self) -> list[Code | str] | None:
        """Retrieve a list of Code keys involving this device."""
        result: set[Code | str] = set()
        entity_id = self._entity.id
        is_dhw = entity_id[_ID_SLICE:] == "_HW"
        is_zone = len(entity_id) > 9 and not is_dhw
        zone_index = entity_id[_ID_SLICE + 1 :] if is_zone else None

        cache = await self._build_state_cache()

        for code, verb, context_value, msg in cache.get_records():
            if verb not in (I_, RP):
                continue
            if is_dhw:
                in_dict = isinstance(msg.payload, dict)
                in_list = isinstance(msg.payload, list)
                if (
                    context_value in (FC, FA, F9)
                    or (
                        in_dict
                        and (
                            SZ_DHW_INDEX in msg.payload
                            or "dhw_index" in msg.payload
                        )
                    )
                    or (
                        in_list
                        and any(
                            isinstance(d, dict)
                            and (SZ_DHW_INDEX in d or "dhw_index" in d)
                            for d in msg.payload
                        )
                    )
                ):
                    result.add(code)
            elif is_zone:
                in_dict = isinstance(msg.payload, dict)
                in_list = isinstance(msg.payload, list)
                if (
                    context_value == zone_index
                    or (
                        in_dict
                        and str(
                            msg.payload.get(
                                SZ_ZONE_INDEX, msg.payload.get("zone_index")
                            )
                        )
                        == zone_index
                    )
                    or (
                        in_list
                        and any(
                            isinstance(d, dict)
                            and str(d.get(SZ_ZONE_INDEX, d.get("zone_index")))
                            == zone_index
                            for d in msg.payload
                        )
                    )
                ):
                    result.add(code)
            else:
                result.add(code)
        return list(result)

    async def find_latest_code(
        self,
        code: Code | str | tuple[Code | str, ...] | None = None,
        key: str | None = None,
        **kwargs: object,
    ) -> Code | str | None:
        """Retrieve the most current Code involving this device."""
        latest: dt = dt.min.replace(tzinfo=UTC)
        result: Code | str | None = None

        entity_id = self._entity.id
        is_dhw = entity_id[_ID_SLICE:] == "_HW"
        is_zone = len(entity_id) > 9 and not is_dhw
        zone_index = kwargs.get(
            "zone_index", entity_id[_ID_SLICE + 1 :] if is_zone else None
        )
        dhw_index = kwargs.get("dhw_index")

        allowed_verbs = (
            (kwargs.get("verb"),)
            if kwargs.get("verb") in (I_, RP)
            else (I_, RP)
        )
        src_id = kwargs.get("src_id")

        cache = await self._build_state_cache()

        for cd, verb, context_value, msg in cache.get_records():
            if src_id is not None and msg.src.id != src_id:
                continue

            if code is not None:
                if isinstance(code, tuple) and cd not in code:
                    continue
                elif not isinstance(code, tuple) and cd != code:
                    continue

            if verb not in allowed_verbs:
                continue

            if zone_index is not None:
                in_dict = isinstance(msg.payload, dict)
                in_list = isinstance(msg.payload, list)
                if not (
                    str(context_value) == str(zone_index)
                    or (
                        in_dict
                        and str(msg.payload.get("zone_index"))
                        == str(zone_index)
                    )
                    or (
                        in_list
                        and any(
                            isinstance(d, dict)
                            and str(d.get("zone_index")) == str(zone_index)
                            for d in msg.payload
                        )
                    )
                ):
                    continue

            if dhw_index is not None:
                in_dict = isinstance(msg.payload, dict)
                in_list = isinstance(msg.payload, list)
                if not (
                    str(context_value) == str(dhw_index)
                    or context_value in (FC, FA, F9)
                    or (in_dict and SZ_DHW_INDEX in msg.payload)
                    or (
                        in_list
                        and any(
                            isinstance(d, dict) and SZ_DHW_INDEX in d
                            for d in msg.payload
                        )
                    )
                ):
                    continue

            if key is not None:
                if isinstance(msg.payload, dict):
                    if key not in msg.payload:
                        continue
                elif isinstance(msg.payload, list):
                    if not any(
                        isinstance(d, dict) and key in d for d in msg.payload
                    ):
                        continue
                else:
                    continue

            # Ensure timezone awareness for pre-0.60 naive dt
            msg_dtm = msg.dtm
            if msg_dtm.tzinfo is None:
                msg_dtm = msg_dtm.replace(tzinfo=UTC)

            if msg_dtm > latest:
                latest = msg_dtm
                result = cd
        return result

    _msg_qry_by_code_key = find_latest_code

    async def _msg_qry(self, sql: str) -> list[SchemaT]:
        """Query an entity's stored payloads."""
        _LOGGER.warning("Legacy _msg_qry (SQL) called. Returning empty.")
        return []

    async def get_message_log_flat(self) -> dict[Code | str, Message]:
        """Dynamically build flat dict of all I/RP messages logged."""
        self._sync_state()
        _msg_dict: dict[Code | str, Message] = {}

        # Build from _build_state_cache to guarantee strict zone_index isolation
        cache = await self._build_state_cache()

        for code, verb, _ctx, msg in cache.get_records():
            if verb not in (I_, RP):
                continue
            if code not in _msg_dict or msg.dtm > _msg_dict[code].dtm:
                _msg_dict[code] = msg

        return _msg_dict

    async def _build_state_cache(self) -> StateCache:
        """Dynamically build a flat cache of all messages for this entity."""
        self._sync_state()
        cache = StateCache()

        # Extremely fast O(1) iteration, bypassing global history!
        for _hdr, msg in self._current_state.items():
            cache.add(msg)

        return cache

    async def traits(self) -> SchemaT:
        """Get the codes seen by the entity."""
        msgs_dict = await self.get_message_log_flat()

        # Build traits dictionary safely checking for valid Code enums
        codes: dict[Code | str, str | None] = {}
        for code in sorted(msgs_dict, key=str):
            if msgs_dict[code].src.id == self._entity.id[:9]:
                name = None
                if isinstance(code, Code) and code in CODE_NAME_LOOKUP:
                    name = CODE_NAME_LOOKUP[code]
                codes[code] = name

        return {"_sent": list(codes.keys())}
