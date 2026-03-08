#!/usr/bin/env python3

# TODO:
# - self._tasks is not ThreadSafe


"""RAMSES RF - The serial to RF gateway (HGI80, not RFG100)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime as dt
from typing import TYPE_CHECKING, Any, Never

from .address import ALL_DEV_ADDR, HGI_DEV_ADDR, NON_DEV_ADDR
from .command import Command
from .const import (
    DEFAULT_DISABLE_QOS,
    DEFAULT_GAP_DURATION,
    DEFAULT_MAX_RETRIES,
    DEFAULT_NUM_REPEATS,
    DEFAULT_SEND_TIMEOUT,
    DEFAULT_WAIT_FOR_REPLY,
    SZ_ACTIVE_HGI,
    Priority,
)
from .message import Message
from .packet import Packet
from .protocol import protocol_factory
from .schemas import (
    SZ_PACKET_LOG,
    SZ_PORT_CONFIG,
    SZ_PORT_NAME,
    select_device_filter_mode,
)
from .transport import TransportConfig, transport_factory
from .typing import PktLogConfigT, PortConfigT, QosParams

from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    Code,
    I_,
    RP,
    RQ,
    W_,
)

if TYPE_CHECKING:
    from .const import VerbT
    from .protocol import RamsesProtocolT
    from .transport import RamsesTransportT
    from .typing import DeviceIdT, DeviceListT, MsgHandlerT, PayloadT


DEV_MODE = False

_LOGGER = logging.getLogger(__name__)


class Engine:
    """The engine class."""

    def __init__(
        self,
        port_name: str | None,
        input_file: str | None = None,
        port_config: PortConfigT | None = None,
        packet_log: PktLogConfigT | None = None,
        block_list: DeviceListT | None = None,
        known_list: DeviceListT | None = None,
        hgi_id: str | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        *,
        disable_sending: bool = False,
        disable_qos: bool | None = None,
        enforce_known_list: bool = False,
        sqlite_index: bool = False,
        log_all_mqtt: bool = False,
        evofw_flag: str | None = None,
        use_regex: dict[str, dict[str, str]] | None = None,
        transport_constructor: Callable[..., Awaitable[RamsesTransportT]] | None = None,
        app_context: Any | None = None,
    ) -> None:
        if port_name and input_file:
            _LOGGER.warning(
                "Port (%s) specified, so file (%s) ignored", port_name, input_file
            )
            input_file = None

        self._disable_sending = disable_sending
        if input_file:
            self._disable_sending = True
        elif not port_name:
            raise TypeError("Either a port_name or an input_file must be specified")

        self.ser_name = port_name
        self._input_file = input_file

        self._port_config: PortConfigT | dict[Never, Never] = port_config or {}
        self._packet_log: PktLogConfigT | dict[Never, Never] = packet_log or {}
        self._loop = loop or asyncio.get_running_loop()

        self._exclude: DeviceListT = block_list or {}
        self._include: DeviceListT = known_list or {}
        self._unwanted: list[DeviceIdT] = [
            NON_DEV_ADDR.id,
            ALL_DEV_ADDR.id,
            "01:000001",  # type: ignore[list-item]  # why this one?
        ]
        self._enforce_known_list = select_device_filter_mode(
            enforce_known_list,
            self._include,
            self._exclude,
        )
        self._sqlite_index = sqlite_index  # TODO Q1 2026: default True
        self._log_all_mqtt = log_all_mqtt
        self._evofw_flag = evofw_flag
        self._use_regex = use_regex or {}
        self._disable_qos = (
            disable_qos if disable_qos is not None else DEFAULT_DISABLE_QOS
        )

        self._transport_constructor = transport_constructor
        self._app_context = app_context

        self._hgi_id = hgi_id

        self._engine_lock = asyncio.Lock()
        self._engine_state: (
            tuple[MsgHandlerT | None, bool | None, *tuple[Any, ...]] | None
        ) = None

        self._protocol: RamsesProtocolT = None  # type: ignore[assignment]
        self._transport: RamsesTransportT | None = None  # None until self.start()

        self._prev_msg: Message | None = None
        self._this_msg: Message | None = None

        self._tasks: list[asyncio.Task] = []  # type: ignore[type-arg]

        self._set_msg_handler(self._msg_handler)  # sets self._protocol

    def __str__(self) -> str:
        if self._hgi_id:
            return f"{self._hgi_id} ({self.ser_name})"

        if not self._transport:
            return f"{HGI_DEV_ADDR.id} ({self.ser_name})"

        device_id = self._transport.get_extra_info(
            SZ_ACTIVE_HGI, default=HGI_DEV_ADDR.id
        )
        return f"{device_id} ({self.ser_name})"

    def _dt_now(self) -> dt:
        timesource: Callable[[], dt] = getattr(self._transport, "_dt_now", dt.now)
        return timesource()

    def _set_msg_handler(self, msg_handler: MsgHandlerT) -> None:
        """Create an appropriate protocol for the packet source (transport).

        The corresponding transport will be created later.
        """

        self._protocol = protocol_factory(
            msg_handler,
            disable_sending=self._disable_sending,
            disable_qos=self._disable_qos,
            enforce_include_list=self._enforce_known_list,
            exclude_list=self._exclude,
            include_list=self._include,
        )

    def add_msg_handler(
        self,
        msg_handler: Callable[[Message], None],
        /,
        *,
        msg_filter: Callable[[Message], bool] | None = None,
    ) -> Callable[[], None]:
        """Add a Message handler to the underlying Protocol.

        The optional filter will return True if the message is to be handled.
        Returns a callable that can be used to subsequently remove the handler.
        """
        return self._protocol.add_handler(msg_handler, msg_filter=msg_filter)

    async def start(self) -> None:
        """Create a suitable transport for the specified packet source.

        Initiate receiving (Messages) and sending (Commands).
        """

        pkt_source: dict[str, Any] = {}  # [str, dict | str | TextIO]
        if self.ser_name:
            pkt_source[SZ_PORT_NAME] = self.ser_name
            pkt_source[SZ_PORT_CONFIG] = self._port_config
        else:  # if self._input_file:
            pkt_source[SZ_PACKET_LOG] = self._input_file  # filename as string

        transport_config = TransportConfig(
            disable_sending=bool(self._disable_sending),
            log_all=bool(self._log_all_mqtt),
            evofw_flag=self._evofw_flag,
            use_regex=self._use_regex,
            app_context=self._app_context,
        )

        extra_info: dict[str, Any] = {}
        if self._hgi_id:
            extra_info[SZ_ACTIVE_HGI] = self._hgi_id

        # incl. await protocol.wait_for_connection_made(timeout=5)
        self._transport = await transport_factory(
            self._protocol,
            config=transport_config,
            loop=self._loop,
            transport_constructor=self._transport_constructor,
            extra=extra_info if extra_info else None,
            **pkt_source,
        )

        await self._protocol.wait_for_connection_made()

        if self._input_file:
            await self._protocol.wait_for_connection_lost(timeout=86400)
            # timeout set to timeout=86400, to stop type checker complaint if sent to None

    async def stop(self) -> None:
        """Close the transport (will stop the protocol)."""

        # Shutdown Safety - wait for tasks to clean up
        tasks = [t for t in self._tasks if not t.done()]
        for t in tasks:
            t.cancel()

        if tasks:
            await asyncio.wait(tasks)

        # Clear any unretrieved exceptions from background tasks
        for task in self._tasks:
            if task.done() and not task.cancelled():
                if exc := task.exception():
                    _LOGGER.debug("Background task %s failed: %s", task.get_name(), exc)

        if self._transport:
            self._transport.close()
            await self._protocol.wait_for_connection_lost()

        return None

    async def _pause(self, *args: Any) -> None:
        """Pause the (active) engine or raise a RuntimeError."""
        # Async lock handling
        if self._engine_lock.locked():
            raise RuntimeError("Unable to pause engine, failed to acquire lock")

        await self._engine_lock.acquire()

        if self._engine_state is not None:
            self._engine_lock.release()
            raise RuntimeError("Unable to pause engine, it is already paused")

        self._engine_state = (None, None, tuple())  # aka not None
        self._engine_lock.release()  # is ok to release now

        self._protocol.pause_writing()  # TODO: call_soon()?
        if self._transport:
            pause_reading = getattr(self._transport, "pause_reading", None)
            if pause_reading:
                pause_reading()  # TODO: call_soon()?

        self._protocol._msg_handler, handler = None, self._protocol._msg_handler  # type: ignore[assignment]
        self._disable_sending, read_only = True, self._disable_sending

        self._engine_state = (handler, read_only, *args)

    async def _resume(self) -> tuple[Any]:  # FIXME: not atomic
        """Resume the (paused) engine or raise a RuntimeError."""

        args: tuple[Any]  # mypy

        # Async lock with timeout
        try:
            await asyncio.wait_for(self._engine_lock.acquire(), timeout=0.1)
        except TimeoutError as err:
            raise RuntimeError(
                "Unable to resume engine, failed to acquire lock"
            ) from err

        if self._engine_state is None:
            self._engine_lock.release()
            raise RuntimeError("Unable to resume engine, it was not paused")

        self._protocol._msg_handler, self._disable_sending, *args = self._engine_state  # type: ignore[assignment]
        self._engine_lock.release()

        if self._transport:
            resume_reading = getattr(self._transport, "resume_reading", None)
            if resume_reading:
                resume_reading()
        if not self._disable_sending:
            self._protocol.resume_writing()

        self._engine_state = None

        return args

    def add_task(self, task: asyncio.Task[Any]) -> None:  # TODO: needs a lock?
        # keep a track of tasks, so we can tidy-up
        self._tasks = [t for t in self._tasks if not t.done()]
        self._tasks.append(task)

    @staticmethod
    def create_cmd(
        verb: VerbT, device_id: DeviceIdT, code: Code, payload: PayloadT, **kwargs: Any
    ) -> Command:
        """Make a command addressed to device_id."""

        if [
            k for k in kwargs if k not in ("from_id", "seqn")
        ]:  # FIXME: deprecate QoS in kwargs
            raise RuntimeError("Deprecated kwargs: %s", kwargs)

        return Command.from_attrs(verb, device_id, code, payload, **kwargs)

    async def async_send_cmd(
        self,
        cmd: Command,
        /,
        *,
        gap_duration: float = DEFAULT_GAP_DURATION,
        num_repeats: int = DEFAULT_NUM_REPEATS,
        priority: Priority = Priority.DEFAULT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = DEFAULT_SEND_TIMEOUT,
        wait_for_reply: bool | None = DEFAULT_WAIT_FOR_REPLY,
    ) -> Packet:
        """Send a Command and return the corresponding Packet.

        If wait_for_reply is True (*and* the Command has a rx_header), return the
        reply Packet. Otherwise, simply return the echo Packet.

        If the expected Packet can't be returned, raise:
            ProtocolSendFailed: tried to Tx Command, but didn't get echo/reply
            ProtocolError:      didn't attempt to Tx Command for some reason
        """

        qos = QosParams(
            max_retries=max_retries,
            timeout=timeout,
            wait_for_reply=wait_for_reply,
        )

        # adjust priority, WFR here?
        # if cmd.code in (Code._0005, Code._000C) and qos.wait_for_reply is None:
        #     qos.wait_for_reply = True

        return await self._protocol.send_cmd(
            cmd,
            gap_duration=gap_duration,
            num_repeats=num_repeats,
            priority=priority,
            qos=qos,
        )  # may: raise ProtocolError/ProtocolSendFailed

    def _msg_handler(self, msg: Message) -> None:
        """Process incoming messages from the protocol."""
        # HACK: This is one consequence of an unpleasant anachronism
        msg.__class__ = Message

        # MUST be set so ramses_rf properties (e.g. msg.src) can instantiate orphans
        # Using setattr bypasses Mypy type assignment checks, keeping decoupling clean
        setattr(msg, "_gwy", self)  # noqa: B010

        self._this_msg, self._prev_msg = msg, self._this_msg

        # Safely pass execution to Gateway's extended handling logic if defined
        handler = getattr(self, "_handle_msg", None)
        if handler:
            handler(msg)
