#!/usr/bin/env python3

# TODO:
# - self._tasks is not ThreadSafe


"""RAMSES RF - The serial to RF gateway (HGI80, not RFG100)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime as dt
from threading import Lock
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
    SZ_DISABLE_QOS,
    SZ_DISABLE_SENDING,
    SZ_ENFORCE_KNOWN_LIST,
    SZ_PACKET_LOG,
    SZ_PORT_CONFIG,
    SZ_PORT_NAME,
    SZ_SQLITE_INDEX,
    PktLogConfigT,
    PortConfigT,
    select_device_filter_mode,
)
from .transport import transport_factory
from .typing import QosParams

from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    Code,
)

if TYPE_CHECKING:
    from .const import VerbT
    from .frame import PayloadT
    from .protocol import RamsesProtocolT
    from .schemas import DeviceIdT, DeviceListT
    from .transport import RamsesTransportT

_MsgHandlerT = Callable[[Message], None]


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
        loop: asyncio.AbstractEventLoop | None = None,
        **kwargs: Any,
    ) -> None:
        if port_name and input_file:
            _LOGGER.warning(
                "Port (%s) specified, so file (%s) ignored", port_name, input_file
            )
            input_file = None

        self._disable_sending = kwargs.pop(SZ_DISABLE_SENDING, None)
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
            kwargs.pop(SZ_ENFORCE_KNOWN_LIST, None),
            self._include,
            self._exclude,
        )
        self._sqlite_index = kwargs.pop(SZ_SQLITE_INDEX, False)  # default True?
        self._kwargs: dict[str, Any] = kwargs  # HACK

        self._engine_lock = Lock()  # FIXME: threading lock, or asyncio lock?
        self._engine_state: (
            tuple[_MsgHandlerT | None, bool | None, *tuple[Any, ...]] | None
        ) = None

        self._protocol: RamsesProtocolT = None  # type: ignore[assignment]
        self._transport: RamsesTransportT | None = None  # None until self.start()

        self._prev_msg: Message | None = None
        self._this_msg: Message | None = None

        self._tasks: list[asyncio.Task] = []  # type: ignore[type-arg]

        self._set_msg_handler(self._msg_handler)  # sets self._protocol

    def __str__(self) -> str:
        if not self._transport:
            return f"{HGI_DEV_ADDR.id} ({self.ser_name})"

        device_id = self._transport.get_extra_info(
            SZ_ACTIVE_HGI, default=HGI_DEV_ADDR.id
        )
        return f"{device_id} ({self.ser_name})"

    def _dt_now(self) -> dt:
        return self._transport._dt_now() if self._transport else dt.now()

    def _set_msg_handler(self, msg_handler: _MsgHandlerT) -> None:
        """Create an appropriate protocol for the packet source (transport).

        The corresponding transport will be created later.
        """

        self._protocol = protocol_factory(
            msg_handler,
            disable_sending=self._disable_sending,
            disable_qos=self._kwargs.pop(SZ_DISABLE_QOS, DEFAULT_DISABLE_QOS),
            enforce_include_list=self._enforce_known_list,
            exclude_list=self._exclude,
            include_list=self._include,
        )

    def add_msg_handler(
        self,
        msg_handler: Callable[[Message], None],
        /,
        msg_filter: Callable[[Message], bool] | None = None,
    ) -> None:
        """Create a client protocol for the RAMSES-II message transport.

        The optional filter will return True if the message is to be handled.
        """

        # if msg_filter is not None and not is_callback(msg_filter):
        #     raise TypeError(f"Msg filter {msg_filter} is not a callback")

        if not msg_filter:
            msg_filter = lambda _: True  # noqa: E731
        else:
            raise NotImplementedError

        self._protocol.add_handler(msg_handler, msg_filter=msg_filter)

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

        # incl. await protocol.wait_for_connection_made(timeout=5)
        self._transport = await transport_factory(
            self._protocol,
            disable_sending=self._disable_sending,
            loop=self._loop,
            **pkt_source,
            **self._kwargs,  # HACK: odd/misc params, e.g. comms_params
        )

        self._kwargs = {}  # HACK

        await self._protocol.wait_for_connection_made()

        # TODO: should this be removed (if so, pytest all before committing)
        if self._input_file:
            await self._protocol.wait_for_connection_lost()

    async def stop(self) -> None:
        """Close the transport (will stop the protocol)."""

        async def cancel_all_tasks() -> None:  # TODO: needs a lock?
            _ = [t.cancel() for t in self._tasks if not t.done()]
            try:  # FIXME: this is broken
                if tasks := (t for t in self._tasks if not t.done()):
                    await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                pass

        await cancel_all_tasks()

        if self._transport:
            self._transport.close()
            await self._protocol.wait_for_connection_lost()

        return None

    def _pause(self, *args: Any) -> None:
        """Pause the (active) engine or raise a RuntimeError."""

        if not self._engine_lock.acquire(blocking=False):
            raise RuntimeError("Unable to pause engine, failed to acquire lock")

        if self._engine_state is not None:
            self._engine_lock.release()
            raise RuntimeError("Unable to pause engine, it is already paused")

        self._engine_state = (None, None, tuple())  # aka not None
        self._engine_lock.release()  # is ok to release now

        self._protocol.pause_writing()  # TODO: call_soon()?
        if self._transport:
            self._transport.pause_reading()  # TODO: call_soon()?

        self._protocol._msg_handler, handler = None, self._protocol._msg_handler  # type: ignore[assignment]
        self._disable_sending, read_only = True, self._disable_sending

        self._engine_state = (handler, read_only, *args)

    def _resume(self) -> tuple[Any]:  # FIXME: not atomic
        """Resume the (paused) engine or raise a RuntimeError."""

        args: tuple[Any]  # mypy

        if not self._engine_lock.acquire(timeout=0.1):
            raise RuntimeError("Unable to resume engine, failed to acquire lock")

        if self._engine_state is None:
            self._engine_lock.release()
            raise RuntimeError("Unable to resume engine, it was not paused")

        self._protocol._msg_handler, self._disable_sending, *args = self._engine_state  # type: ignore[assignment]
        self._engine_lock.release()

        if self._transport:
            self._transport.resume_reading()
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
        # HACK: This is one consequence of an unpleasant anachronism
        msg.__class__ = Message  # HACK (next line too)
        msg._gwy = self  # type: ignore[assignment]

        self._this_msg, self._prev_msg = msg, self._this_msg
