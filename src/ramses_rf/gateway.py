#!/usr/bin/env python3
"""RAMSES RF - the gateway facade."""

from __future__ import annotations

import asyncio
import logging
import threading
import warnings
from collections.abc import Awaitable, Callable
from logging.handlers import QueueListener
from typing import TYPE_CHECKING, Any

from ramses_rf.commands.dispatcher import CommandDispatcher
from ramses_tx import I_, RP, CommandDTO, Engine, Packet
from ramses_tx.const import (
    DEFAULT_GAP_DURATION,
    DEFAULT_MAX_RETRIES,
    DEFAULT_NUM_REPEATS,
    DEFAULT_SEND_TIMEOUT,
    SZ_ACTIVE_HGI,
    Priority,
)
from ramses_tx.dtos import PacketDTO
from ramses_tx.exceptions import (
    PacketInvalid,
    ProtocolSendFailed,
    TransportError,
)
from ramses_tx.rssi_tracker import RssiTracker
from ramses_tx.schemas import (
    SZ_BLOCK_LIST,
    SZ_ENFORCE_KNOWN_LIST,
    SZ_KNOWN_LIST,
)
from ramses_tx.typing import PayloadT

from .config import GatewayConfig as GatewayConfig, strip_traits
from .const import SZ_PAYLOAD, SZ_ZONES, Code, Verb
from .devices import (
    DeviceFilter,
    DeviceRegistry,
    HgiGateway,
    HvacVentilator,
    device_factory,
)
from .dispatcher import detect_array_fragment, process_msg
from .interfaces import (
    DeviceFilterInterface,
    DeviceRegistryInterface,
    GatewayInterface,
    MessageStoreInterface,
    SchemaUpdatedCallback,
)
from .lifecycle import GatewayLifecycle
from .messages import ApplicationMessage, Message as rf_msg
from .pipeline.conversation import ConversationManager
from .pipeline.polling import PollingManager
from .pipeline.topology_builder import TopologyBuilder
from .routing import StateHeader
from .schemas import (
    SCH_GLOBAL_SCHEMAS,
    SZ_CONFIG,
    SZ_ENABLE_EAVESDROP,
    SZ_MAIN_TCS,
    SZ_ORPHANS,
)
from .systems.tcs import Evohome
from .typing import DeviceIdT

if TYPE_CHECKING:
    from ramses_tx import RamsesTransportT

_LOGGER = logging.getLogger(__name__)


def _payload_to_serialisable(payload: Any) -> Any:
    """Convert a payload object to a JSON-serialisable form.

    Payload dataclasses may contain ``bytes`` fields (e.g.
    ``PuzzlePayload``) that HA's storage layer cannot JSON-encode.
    If the payload has a ``to_dict()`` method, use it; otherwise
    return the payload as-is (legacy behaviour for plain dicts/strings).

    :param payload: The payload object from an ``ApplicationMessage``.
    :type payload: Any
    :returns: A JSON-serialisable representation of the payload.
    :rtype: Any
    """
    if hasattr(payload, "to_dict"):
        try:
            return payload.to_dict()
        except Exception:  # noqa: BLE001
            return str(payload)
    return payload


class Gateway(GatewayLifecycle, GatewayInterface):
    """The gateway class.

    This class serves as the primary interface for the RAMSES RF network.
    It manages the serial connection (via ``Engine``), device discovery,
    schema maintenance, and message dispatching.
    """

    def __init__(
        self,
        port_name: str | None = None,
        *,
        config: GatewayConfig | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        transport_constructor: Callable[..., Awaitable[RamsesTransportT]]
        | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the Gateway instance."""
        self._gwy_config = config or GatewayConfig()

        if port_name is not None:
            self._gwy_config.engine.port_name = port_name

        if kwargs:
            keys = list(kwargs.keys())
            _LOGGER.warning(
                "Gateway received legacy kwargs: %s. Please migrate "
                "ramses_cc to use the GatewayConfig object.",
                keys,
            )
            warnings.warn(
                f"Initializing Gateway with **kwargs {keys} is deprecated "
                "and will be removed in a future release. Please use "
                "GatewayConfig.",
                DeprecationWarning,
                stacklevel=2,
            )

            def _apply_kwargs(cfg_dict: dict[str, Any]) -> None:
                """Recursively unpack nested dictionaries to apply configs."""
                for key, value in cfg_dict.items():
                    if hasattr(self._gwy_config.engine, key):
                        setattr(self._gwy_config.engine, key, value)
                    elif hasattr(self._gwy_config, key):
                        setattr(self._gwy_config, key, value)
                    elif isinstance(value, dict):
                        _apply_kwargs(value)
                    else:
                        _LOGGER.error(
                            "Gateway received unsupported kwarg: %s. "
                            "This argument is ignored.",
                            key,
                        )

            _apply_kwargs(kwargs)

        if self._gwy_config.debug_mode:
            _LOGGER.setLevel(logging.DEBUG)

        # Override EngineConfig with the stripped-down L7 properties
        self._gwy_config.engine.hgi_id = self._gwy_config.hgi_id
        self._gwy_config.engine.known_list = list(
            self._gwy_config.known_list.keys()
        )
        self._gwy_config.engine.block_list = list(
            self._gwy_config.block_list.keys()
        )

        self._engine = Engine(
            self._gwy_config.engine,
            loop=loop,
            transport_constructor=transport_constructor,
        )

        # Force the engine's protocol to use Gateway's message handler
        self._engine._set_msg_handler(self._msg_handler)

        if self._engine._disable_sending:
            self._gwy_config.disable_discovery = True

        if self._gwy_config.enable_eavesdrop:
            _LOGGER.warning(
                f"{SZ_ENABLE_EAVESDROP}=True: this is strongly discouraged "
                "for routine use (there be dragons here)"
            )

        schema_in = self._gwy_config.schema or {}
        # Use strip_traits (stage 1 only) to remove _-prefixed keys before
        # validation.  strip_and_map_schema (stage 1+2) would map _class→class,
        # but SCH_GLOBAL_SCHEMAS rejects mapped trait names — they are only
        # valid for the known_list, not the schema validator (issue 1120).
        stripped = strip_traits(schema_in)

        # Preserve _name in zone entries (ramses-rf/ramses_cc#919: zone names
        # lost after 24h when the MessageStore prunes 0004 packets — the
        # schema is the only persistent source of the name).  strip_traits
        # removes _name, so re-add it from the original schema for
        # SCH_TCS_ZONES_ZON to accept and Zone._update_schema to hydrate.
        for ctl_id, ctl_entry in schema_in.items():
            if not isinstance(ctl_entry, dict) or not isinstance(
                ctl_entry.get(SZ_ZONES), dict
            ):
                continue
            stripped_ctl = stripped.get(ctl_id)
            if not isinstance(stripped_ctl, dict) or not isinstance(
                stripped_ctl.get(SZ_ZONES), dict
            ):
                continue
            for z_idx, z_entry in ctl_entry[SZ_ZONES].items():
                if (
                    isinstance(z_entry, dict)
                    and z_entry.get("_name")
                    and isinstance(stripped_ctl[SZ_ZONES].get(z_idx), dict)
                ):
                    stripped_ctl[SZ_ZONES][z_idx]["_name"] = z_entry["_name"]

        self._schema: dict[str, Any] = SCH_GLOBAL_SCHEMAS(stripped)

        self._tcs: Evohome | None = None
        self._eavesdrop_engine: Any = None

        self._device_filter: DeviceFilterInterface = DeviceFilter(
            include=[DeviceIdT(k) for k in self._gwy_config.known_list],
            exclude=[DeviceIdT(k) for k in self._gwy_config.block_list],
            unwanted=self._engine._unwanted,
            enforce_known_list=self._gwy_config.engine.enforce_known_list,
            hgi_id_provider=lambda: (
                getattr(self.hgi, "id", None) or self._gwy_config.hgi_id
            ),
        )

        self._device_registry: DeviceRegistryInterface = DeviceRegistry(
            device_filter=self._device_filter,
            config=self._gwy_config,
            device_factory_cb=lambda addr, msg, traits: device_factory(
                gateway=self, device_address=addr, msg=msg, traits=traits
            ),
            on_topology_changed_cb=self._on_topology_changed,
        )

        # RSSI tracker for this HGI (transport layer, per issue 1047).
        # One tracker per gateway/HGI; multi-HGI support will add more.
        self._rssi_tracker: RssiTracker = RssiTracker()

        # Instantiate the new asynchronous Topology Builder engine
        self._topology_builder = TopologyBuilder(
            emit_event_cb=self._device_registry.handle_topology_event,
            device_class_lookup_cb=self._lookup_device_class,
        )

        self._message_store: MessageStoreInterface | None = None
        self._packet_log_listener: QueueListener | None = None
        self._schema_updated_callback: SchemaUpdatedCallback | None = None

        # Initialize placeholder for the CQRS StateProjector
        self.state_projector = None

        self._prev_msg: ApplicationMessage | None = None
        self._this_msg: ApplicationMessage | None = None
        self._state_cache: dict[StateHeader, rf_msg] = {}
        self._history_lock = threading.Lock()

        # 1. Controller Knowledge Bridge
        def is_controller(device_id: str) -> bool:
            device = self._device_registry.device_by_id.get(
                DeviceIdT(device_id)
            )
            if device:
                return getattr(device, "_is_controller", True)
            return True

        rf_msg._IS_CONTROLLER_CB = is_controller

        # 2. Instantiate L7 Command Dispatcher, ConversationManager, and PollingManager
        self._dispatcher = CommandDispatcher(self)
        self._conversation_manager = ConversationManager(
            loop=loop,
            send_func=self._async_send_dto,
        )
        self._polling_manager = PollingManager(self, shadow_mode=False)

    def _lookup_device_class(self, device_id: str) -> dict[str, Any] | None:
        """Look up the current device traits for a device_id.

        Used by topology handlers to detect contradictions between
        the known_list class and observed message patterns.

        :param device_id: The device ID to look up.
        :type device_id: str
        :returns: A dict with keys "class" (device slug), "locked"
            (whether the user has locked the class), and "faked", or None.
        :rtype: dict[str, Any] | None
        """
        device = self._device_registry.device_by_id.get(DeviceIdT(device_id))
        if not device:
            return None
        slug = getattr(device, "_SLUG", None)
        # Check known_list for _locked trait (user override)
        known = self._gwy_config.known_list.get(device_id, {})
        return {
            "class": slug,
            "locked": bool(known.get("_locked", False)),
            "faked": bool(getattr(device, "is_faked", False)),
        }

    def __repr__(self) -> str:
        """Return an unambiguous string representation."""
        if not self._engine.ser_name:
            return f"Gateway(input_file={self._engine._input_file})"
        return (
            f"Gateway(port_name={self._engine.ser_name}, "
            f"port_config={self._engine._port_config})"
        )

    @property
    def device_registry(self) -> DeviceRegistryInterface:
        """Return the active device registry instance."""
        return self._device_registry

    @property
    def rssi_tracker(self) -> RssiTracker:
        """Return the RSSI tracker for this HGI (transport layer).

        Tracks the last N RSSI readings per device heard by this
        gateway's receiver (issue 1047).
        """
        return self._rssi_tracker

    @property
    def conversation_manager(self) -> ConversationManager:
        """Return the L7 ConversationManager instance."""
        return self._conversation_manager

    @property
    def polling_manager(self) -> PollingManager:
        """Return the L7 PollingManager instance."""
        return self._polling_manager

    @property
    def dispatcher(self) -> CommandDispatcher:
        """Return the command dispatcher instance."""
        return self._dispatcher

    @property
    def config(self) -> GatewayConfig:
        """Return the gateway configuration."""
        return self._gwy_config

    @property
    def message_store(self) -> MessageStoreInterface | None:
        """Return the SQLite message store instance or None."""
        return self._message_store

    @message_store.setter
    def message_store(self, value: MessageStoreInterface | None) -> None:
        self._message_store = value

    @property
    def hgi(self) -> HgiGateway | None:
        """Return the HGI gateway device interface or None."""
        if not self._engine._transport:
            return None
        if device_id := self._engine._transport.get_extra_info(SZ_ACTIVE_HGI):
            return self.device_registry.device_by_id.get(device_id)
        return None

    def update_message_history(self, msg: ApplicationMessage) -> None:
        """Update current and previous message tracking references."""
        with self._history_lock:
            self._prev_msg = self._this_msg
            self._this_msg = msg

    def clear_message_history(self) -> None:
        """Clear the tracked message history references."""
        with self._history_lock:
            self._prev_msg = None
            self._this_msg = None
            self._state_cache.clear()

    @property
    def tcs(self) -> Evohome | None:
        """Return the primary Evohome system or None."""
        if self._tcs is None and self.device_registry.systems:
            self._tcs = self.device_registry.systems[0]
        return self._tcs

    async def _config(self) -> dict[str, Any]:
        return {
            "_gateway_id": self.hgi.id if self.hgi else None,
            SZ_MAIN_TCS: self.tcs.id if self.tcs else None,
            SZ_CONFIG: {
                SZ_ENFORCE_KNOWN_LIST: self.config.engine.enforce_known_list
            },
            SZ_KNOWN_LIST: await self.device_registry.known_list(),
            SZ_BLOCK_LIST: self.config.engine.block_list or [],
            "_unwanted": sorted(self._engine._unwanted),
        }

    @property
    def schema_updated_callback(self) -> SchemaUpdatedCallback | None:
        """Return the async callback invoked when system topology/schema updates.

        :returns: Registered callback or None.
        :rtype: SchemaUpdatedCallback | None
        """
        return self._schema_updated_callback

    def set_schema_updated_callback(
        self, callback: SchemaUpdatedCallback | None
    ) -> None:
        """Set the async callback invoked when system topology/schema updates.

        :param callback: Async or sync callback accepting schema dict.
        :type callback: SchemaUpdatedCallback | None
        """
        self._schema_updated_callback = callback

    def _on_topology_changed(self) -> None:
        """Handle topology change notification from DeviceRegistry."""
        task = asyncio.create_task(self._notify_schema_updated())
        self.add_task(task)

    async def _notify_schema_updated(self) -> None:
        """Invoke registered schema updated callback safely.

        Dispatches the latest system schema dictionary to the registered
        consumer callback (e.g., ramses_cc warm-restart ingestion).
        """
        if self._schema_updated_callback is None:
            return
        schema_dict = await self.schema()
        result = self._schema_updated_callback(schema_dict)
        if asyncio.iscoroutine(result):
            await result

    async def schema(self) -> dict[str, Any]:
        """Return the entire gateway and device topology schema."""
        schema: dict[str, Any] = {
            SZ_MAIN_TCS: self.tcs.ctl.id if self.tcs else None
        }
        for tcs in self.device_registry.systems:
            schema[tcs.ctl.id] = await tcs.schema()
        # Include FAN/VCS topology (remotes/sensors membership) so that
        # HVAC structure round-trips across restarts via load_fan()
        for device in self.device_registry.devices:
            if isinstance(device, HvacVentilator) and (
                device._remote_ids or device._sensor_ids
            ):
                schema[device.id] = await device.schema()
        schema[
            f"{SZ_ORPHANS}_heat"
        ] = await self.device_registry.get_heat_orphans()
        schema[
            f"{SZ_ORPHANS}_hvac"
        ] = await self.device_registry.get_hvac_orphans()
        return schema

    async def params(self) -> dict[str, Any]:
        """Return parameters across all registered devices."""
        return await self.device_registry.params()

    async def status(self) -> dict[str, Any]:
        """Return operational status across all registered devices."""
        status_dict = await self.device_registry.status()
        tx_rate = (
            self._engine._transport.get_extra_info("tx_rate")
            if self._engine._transport
            else None
        )
        status_dict["_tx_rate"] = tx_rate
        return status_dict

    async def get_state(
        self, include_expired: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return the current system state for warm restarts (e.g., for HA ramses_cc).

        This returns a tuple of (schema, packets). The packets payload is sourced
        directly from the OSI L7 MessageStore, containing the single most recent
        packet for every unique StateHeader. It strictly maps ``dtm_str`` to a
        dictionary containing ``code``, ``verb``, and ``payload`` to maintain
        backward compatibility with downstream consumers.

        :param include_expired: Included for backward compatibility, unused.
        :type include_expired: bool
        :return: A tuple containing the schema dictionary and the state dictionary.
        :rtype: tuple[dict[str, Any], dict[str, Any]]
        """
        state_dict: dict[str, Any] = {}
        with self._history_lock:
            cached_messages = list(self._state_cache.values())

        if not cached_messages and self.message_store is not None:
            # Use getattr fallback in case interface strictly lacks state_cache mapping
            state_cache_map = getattr(self.message_store, "state_cache", {})
            cached_messages = list(state_cache_map.values())

        for msg in cached_messages:
            if msg.verb not in (I_, RP):
                continue

            dtm_str = msg.dtm.isoformat(timespec="microseconds")
            state_dict[dtm_str] = {
                "verb": msg.verb,
                "src": msg.src.id,  # Keep for downstream legacy parsers
                "dst": msg.dst.id,  # Keep for downstream legacy parsers
                "addr1": msg._addrs[0].id,  # <-- Exact raw addr1
                "addr2": msg._addrs[1].id,  # <-- Exact raw addr2
                "addr3": msg._addrs[2].id,  # <-- Exact raw addr3
                "code": str(msg.code),
                SZ_PAYLOAD: _payload_to_serialisable(msg.payload),
                # Frame string is required by _restore_cached_packets /
                # Packet.from_dict to reconstruct the Packet on warm restart.
                # Without it, from_dict gets an empty frame body and raises
                # "Bad frame: Invalid structure: >>><<<" (issue 812).
                "frame": getattr(msg, "raw_frame", ""),
            }

        schema_dict = await self.schema()
        return schema_dict, state_dict

    async def _msg_handler(self, dto: PacketDTO) -> None:
        try:
            app_msg = ApplicationMessage.from_dto(dto)
        except PacketInvalid:
            return

        app_msg.set_gateway(self._engine)
        app_msg.bind_context(self)  # noqa: B010
        self.update_message_history(app_msg)

        if (
            self._this_msg
            and self._prev_msg
            and detect_array_fragment(
                self._this_msg,
                self._prev_msg,
            )
        ):
            app_msg._force_has_array()
            app_msg._payload = self._prev_msg.payload + (
                app_msg.payload
                if isinstance(app_msg.payload, list)
                else [app_msg.payload]
            )

        # NEW: Feed the async TopologyBuilder so it can structurally map the
        # graph *before* the message state is ingested by the Read-Models.
        payload_data = getattr(app_msg, "data", None) or getattr(
            app_msg, SZ_PAYLOAD, None
        )

        if payload_data is not None:
            # Bridge the payload to satisfy core.Message strict dict typing
            if isinstance(payload_data, dict):
                core_data = payload_data
            elif isinstance(payload_data, list):
                core_data = {"_array": payload_data}
            else:
                core_data = {"_payload": payload_data}

            # Temporary Phase 2.8 Strangler Fig Translation
            from ramses_rf.enums import Topic
            from ramses_rf.messages.core import Message as CoreMessage

            core_msg = CoreMessage(
                topic=Topic.TOPOLOGY_DISCOVERY,
                header=app_msg.state_header,
                src=app_msg.src,
                dst=app_msg.dst,
                data=core_data,
                packets=(),  # L3 packets dropped for legacy bridging
                timestamp=app_msg.dtm,
            )
            await self._topology_builder.consume(core_msg)

        await process_msg(self, app_msg)

        # CQRS Strangler Bridge: feed the StateProjector synchronously
        # so the Read-Models get properly hydrated in production.
        if self.state_projector is not None:
            self.state_projector.process_message_state(app_msg)

    def add_msg_handler(
        self,
        msg_handler: Callable[[PacketDTO], Awaitable[None]],
        /,
        *,
        msg_filter: Callable[[PacketDTO], bool] | None = None,
    ) -> Callable[[], None]:
        """Register an asynchronous packet message handler callback."""
        return self._engine.add_msg_handler(msg_handler, msg_filter=msg_filter)

    def add_raw_packet_handler(
        self,
        msg_handler: Callable[[PacketDTO], Awaitable[None]],
        /,
    ) -> Callable[[], None]:
        """Add a raw packet handler that fires before the device ID filter.

        Used by the passive scan engine to see packets from unknown devices
        even when ``enforce_known_list=True``.
        """
        return self._engine.add_raw_packet_handler(msg_handler)

    def add_task(self, task: asyncio.Task[Any]) -> None:
        """Register a tracked asyncio task on the transport engine."""
        self._engine.add_task(task)

    @staticmethod
    def create_cmd(
        verb: Verb,
        device_id: DeviceIdT,
        code: Code,
        payload: PayloadT,
        **kwargs: Any,
    ) -> CommandDTO:
        """Create a standardized CommandDTO packet command."""
        return Engine.create_cmd(
            verb,
            device_id,
            code,
            payload,
            **kwargs,
        )

    async def async_send_raw_command(
        self,
        command: CommandDTO,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        timeout: float = DEFAULT_SEND_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> Packet:
        """Transmit a raw command DTO for diagnostics and developer tools.

        :param command: The compiled CommandDTO packet to transmit.
        :type command: CommandDTO
        :param gap_duration: Inter-frame gap in seconds.
        :type gap_duration: float
        :param num_repeats: Number of transmission retries.
        :type num_repeats: int
        :param priority: QoS transmission priority.
        :type priority: Priority
        :param timeout: Maximum await duration in seconds.
        :type timeout: float
        :param max_retries: Maximum protocol retransmissions.
        :type max_retries: int
        :returns: The acknowledged echo packet.
        :rtype: Packet
        :raises ProtocolSendFailed: If transmission fails or is cancelled.
        """
        return await self._async_send_dto(
            command,
            gap_duration=gap_duration,
            num_repeats=num_repeats,
            priority=priority,
            timeout=timeout,
            max_retries=max_retries,
        )

    async def _async_send_dto(
        self,
        command: CommandDTO,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        timeout: float = DEFAULT_SEND_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> Packet:
        """Transmit a command DTO over the L3 engine and return the echo packet."""
        try:
            return await self._engine.async_send_cmd(
                command,
                gap_duration=gap_duration,
                num_repeats=num_repeats,
                priority=priority,
                max_retries=max_retries,
                timeout=timeout,
            )
        except (
            ProtocolSendFailed,
            NotImplementedError,
            TransportError,
        ) as err:
            if (
                self.config.disable_discovery
                or self._engine._disable_sending
                or "Inactive" in str(err)
                or "Read-Only" in str(err)
            ):
                raise asyncio.CancelledError(
                    f"Gateway shutting down, suppressed teardown leak: {err}"
                ) from err
            raise
