#!/usr/bin/env python3
"""RAMSES RF - Gateway Lifecycle Management."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime as dt, timedelta as td
from logging.handlers import QueueListener
from typing import TYPE_CHECKING, cast

from ramses_rf.typing import SchemaT
from ramses_tx import Packet, protocol_factory, set_packet_logging_config
from ramses_tx.const import SZ_ACTIVE_HGI
from ramses_tx.logger import flush_packet_log
from ramses_tx.typing import PktLogConfigT

from .const import (
    DONT_CREATE_MESSAGES,
    HIGH_VOLUME_STATUS_CODES,
    I_,
    RP,
    RQ,
    W_,
    Code,
)
from .exceptions import DeviceNotFoundError
from .messages import Message
from .pipeline.ingestion import StateProjector
from .schemas import load_schema
from .state import MessageStore
from .typing import DeviceIdT

if TYPE_CHECKING:
    from ramses_tx import Engine
    from ramses_tx.dtos import PacketDTO

    from .config import GatewayConfig
    from .gateway import Gateway
    from .interfaces import DeviceRegistryInterface, MessageStoreInterface
    from .systems.tcs import Evohome

_LOGGER = logging.getLogger(__name__)


class GatewayLifecycle:
    """Gateway lifecycle management and orchestration."""

    if TYPE_CHECKING:

        @property
        def config(self) -> GatewayConfig:
            """Return the active gateway configuration."""
            ...

        @property
        def device_registry(self) -> DeviceRegistryInterface:
            """Return the active device registry interface."""
            ...

        _engine: Engine
        _message_store: MessageStoreInterface | None
        _packet_log_listener: QueueListener | None
        _schema: SchemaT
        _tcs: Evohome | None
        state_projector: StateProjector | None

        def add_task(self, task: asyncio.Task[object]) -> None:
            """Register a tracked task for clean cancellation."""
            ...

        def clear_message_history(self) -> None:
            """Clear cached message envelopes and packets."""
            ...

        async def schema(self) -> SchemaT:
            """Return the current system topology schema."""
            ...

        async def _msg_handler(self, dto: PacketDTO) -> None: ...

    def create_sqlite_message_index(self) -> None:
        """Initialize the SQLite MessageStore."""
        self._message_store = MessageStore(disk_path=self.config.database_path)

    async def start(
        self,
        /,
        *,
        start_discovery: bool = True,
        cached_packets: dict[str, SchemaT | str] | None = None,
    ) -> None:
        """Start the Gateway."""
        _, self._packet_log_listener = await set_packet_logging_config(
            cc_console=(self.config.reduce_processing >= DONT_CREATE_MESSAGES),
            **cast(PktLogConfigT, self._engine.packet_log),
        )

        if self._packet_log_listener:
            self._packet_log_listener.start()

            packet_log_config: SchemaT = dict(self._engine.packet_log)
            if flush_interval := packet_log_config.get("flush_interval", 0):

                async def _periodic_flush() -> None:
                    try:
                        while True:
                            await asyncio.sleep(flush_interval)
                            await self._engine._loop.run_in_executor(
                                None,
                                flush_packet_log,
                                self._packet_log_listener,
                            )
                    except asyncio.CancelledError:
                        pass

                self.add_task(
                    self._engine._loop.create_task(_periodic_flush())
                )

        _LOGGER.info("Ramses RF starts central MessageStore")
        self.create_sqlite_message_index()

        # Initialize the CQRS State Projector with a dummy queue.
        # The background worker is not started; the projector is fed
        # synchronously via the gateway._msg_handler bridge.
        if self.state_projector is None:
            self.state_projector = StateProjector(
                cast("Gateway", self), asyncio.Queue()
            )

        self.config.disable_discovery, disable_discovery = (
            True,
            self.config.disable_discovery,
        )

        load_schema(
            cast("Gateway", self),
            known_list=self.config.known_list,
            **self._schema,
        )

        # Bootstrap the local hardware adapter (HGI) into the registry early if configured.
        # Because the adapter has no topological bonds to a heating controller,
        # the TopologyBuilder pipeline will never emit a BIND_DEVICE event for it.
        if self.config.hgi_id:
            with contextlib.suppress(DeviceNotFoundError):
                self.device_registry.get_device(
                    cast(DeviceIdT, self.config.hgi_id)
                )

        if cached_packets:
            await self._restore_cached_packets(cached_packets)

        await self._engine.start()

        # If the underlying transport dynamically discovered the hardware adapter
        # (e.g. from parsing the first frames of a log file stream), sync the
        # configuration state and explicitly register it into the graph.
        if self._engine.transport:
            if hgi_id := self._engine.transport.get_extra_info(SZ_ACTIVE_HGI):
                hgi_str = str(hgi_id)
                if not self.config.hgi_id:
                    self.config.hgi_id = hgi_str
                    if hasattr(self.config.engine, "hgi_id"):
                        self.config.engine.hgi_id = hgi_str
                with contextlib.suppress(DeviceNotFoundError):
                    self.device_registry.get_device(cast(DeviceIdT, hgi_str))

        self.config.disable_discovery = disable_discovery

        if (
            not self._engine._disable_sending
            and not self.config.disable_discovery
            and start_discovery
        ):
            if pm := getattr(self, "polling_manager", None):
                pm.start()

    async def stop(self) -> None:
        """Stop the Gateway and tidy up."""
        self.config.disable_discovery = True

        if pm := getattr(self, "polling_manager", None):
            await pm.stop()

        if cm := getattr(self, "conversation_manager", None):
            cm.cancel_all()

        # Cancel binding managers before stopping engine
        for device in self.device_registry.devices:
            bm = getattr(device, "_binding_manager", None)
            if bm:
                try:
                    bm.cancel()
                except (AttributeError, RuntimeError) as err:
                    _LOGGER.debug(
                        "Error cancelling binding manager for device %s: %s",
                        getattr(device, "id", device),
                        err,
                    )

        await self._engine.stop()

        # The StateProjector background worker isn't running, but we call stop
        # just to be architecturally safe and clean up any dangling references.
        if self.state_projector is not None:
            await self.state_projector.stop()

        if self._packet_log_listener:

            def _stop_listener(listener: QueueListener) -> None:
                listener.stop()
                for handler in listener.handlers:
                    handler.close()

            await self._engine._loop.run_in_executor(
                None, _stop_listener, self._packet_log_listener
            )
            self._packet_log_listener = None

        if self._message_store:
            self._message_store.stop()

    async def _pause(self, *args: object) -> None:
        """Pause the (unpaused) gateway (disables sending/discovery)."""
        _LOGGER.debug("Gateway: Pausing engine...")

        self.config.disable_discovery, disc_flag = (
            True,
            self.config.disable_discovery,
        )

        try:
            await self._engine._pause(disc_flag, *args)
        except RuntimeError:
            self.config.disable_discovery = disc_flag
            raise

    async def _resume(self) -> tuple[object, ...]:
        """Resume the (paused) gateway."""
        args: tuple[object, ...]
        _LOGGER.debug("Gateway: Resuming engine...")

        self.config.disable_discovery, *args = (
            await self._engine._resume()  # type: ignore[assignment]
        )

        return tuple(args)

    async def get_state(
        self, include_expired: bool = False
    ) -> tuple[SchemaT, SchemaT]:
        """Return the current global schema and state.

        Forces a complete state projection sync across all pending packets
        before extracting the schema and message log.

        :param include_expired: If True, include expired messages in the state.
        :type include_expired: bool
        :returns: A tuple of (schema dictionary, sorted packet dictionary).
        :rtype: tuple[SchemaT, SchemaT]
        """
        # Drain all pending asynchronous packet ingestion tasks before taking state snapshot
        await self._pause()

        def wanted_msg(msg: Message, include_expired: bool = False) -> bool:
            if msg.code == Code._0005 and msg.verb in (I_, RP):
                return True
            if msg.code in (Code._0004, Code._000C, Code._0418):
                return msg.verb in (I_, W_) and msg.len > 7
            if msg.verb in (W_, RQ):
                return False
            return include_expired or not getattr(msg, "_expired", False)

        packets_dict: SchemaT = {}
        if self._message_store:
            all_msgs = await self._message_store.all(include_expired=True)
            for i, msg in enumerate(all_msgs):
                if wanted_msg(msg, include_expired=include_expired):
                    dtm_str = msg.dtm.isoformat(timespec="microseconds")
                    packets_dict[dtm_str] = msg.dto.source_packets[0].__dict__
                if i > 0 and i % 100 == 0:
                    await asyncio.sleep(0)

        await self._resume()
        return await self.schema(), dict(sorted(packets_dict.items()))

    async def _restore_cached_packets(
        self,
        packets: dict[str, SchemaT | str],
        _clear_state: bool = False,
    ) -> None:
        """Restore cached packets (may include expired packets)."""

        def clear_state() -> None:
            _LOGGER.info("Gateway: Clearing existing schema/state...")
            self._tcs = None
            self.device_registry.devices.clear()
            self.device_registry.device_by_id.clear()
            self.clear_message_history()

        _LOGGER.debug("Gateway: Restoring a cached packet log...")
        await self._pause()

        if _clear_state:
            clear_state()

        enforce_include_list = bool(
            self._engine.enforce_known_list and self.config.hgi_id
        )

        tmp_protocol = protocol_factory(
            self._msg_handler,
            disable_sending=True,
            enforce_include_list=enforce_include_list,
            exclude_list=self._engine._exclude,
            include_list=self._engine.include_list,
        )

        cutoff_dtm = dt.now(tz=UTC) - td(hours=1)

        for i, (dtm, state) in enumerate(packets.items()):
            if i > 0 and i % 100 == 0:
                await asyncio.sleep(0)

            # Never restore a cached 2E04 (system mode): a stale cached
            # mode is presented as authoritative while a real refresh can
            # be up to a day away.  The true mode is fetched by an early
            # startup poll of the controller instead (issue:
            # https://github.com/ramses-rf/ramses_cc/issues/1242).
            if isinstance(state, dict):
                frame_str = str(
                    state.get("frame") or state.get("raw_packet") or ""
                )
                is_system_mode = state.get("code") == Code._2E04 or (
                    f" {Code._2E04} " in frame_str
                )
            else:
                is_system_mode = f" {Code._2E04} " in str(state)
            if is_system_mode:
                continue

            try:
                clean_dtm = dtm.replace("Z", "+00:00")
                packet_dtm = dt.fromisoformat(clean_dtm)
                if packet_dtm.tzinfo is None:
                    packet_dtm = packet_dtm.replace(tzinfo=UTC)
                is_old = packet_dtm < cutoff_dtm
            except (TypeError, ValueError):
                is_old = False

            if is_old:
                is_match = False
                if isinstance(state, dict) and "code" in state:
                    is_match = state["code"] in HIGH_VOLUME_STATUS_CODES
                else:
                    frame_str = (
                        state.get("frame", "")
                        if isinstance(state, dict)
                        else str(state)
                    )
                    is_match = any(
                        f" {c} " in frame_str for c in HIGH_VOLUME_STATUS_CODES
                    )

                if is_match:
                    continue

            try:
                packet = Packet.from_dict(dtm, state)
                tmp_protocol.packet_received(packet)
            except Exception as err:
                _LOGGER.debug(
                    "Gateway: Failed to restore packet %s: %s", dtm, err
                )

        # Drain all pending handler tasks so that restored packets are fully
        # processed (including CQRS state projection, e.g. power_state updates
        # from 1060 battery packets) before we resume and entities are created.
        # Without this, entity properties may query power_state before the
        # async handler tasks have completed, causing flaky 'unknown' states.
        if hasattr(tmp_protocol, "_handler_tasks"):
            pending = list(tmp_protocol._handler_tasks)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        _LOGGER.debug("Gateway: Restored, resuming")
        await self._resume()
