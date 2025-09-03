#!/usr/bin/env python3
"""RAMSES RF - RAMSES-II compatible packet protocol finite state machine."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime as dt
from queue import Empty, Full, PriorityQueue
from threading import Lock
from typing import TYPE_CHECKING, Any, Final, TypeAlias

from . import exceptions as exc
from .address import HGI_DEVICE_ID
from .command import Command
from .const import (
    DEFAULT_BUFFER_SIZE,
    DEFAULT_ECHO_TIMEOUT,
    DEFAULT_RPLY_TIMEOUT,
    MAX_RETRY_LIMIT,
    MAX_SEND_TIMEOUT,
    Code,
    Priority,
)
from .packet import Packet
from .typing import QosParams

if TYPE_CHECKING:
    from .protocol import RamsesProtocolT
    from .transport import RamsesTransportT
    from .typing import ExceptionT

#
# NOTE: All debug flags should be False for deployment to end-users
_DBG_MAINTAIN_STATE_CHAIN: Final[bool] = False  # maintain Context._prev_state
_DBG_USE_STRICT_TRANSITIONS: Final[bool] = False

_LOGGER = logging.getLogger(__name__)


#######################################################################################

_FutureT: TypeAlias = asyncio.Future[Packet]
_QueueEntryT: TypeAlias = tuple[Priority, dt, Command, QosParams, _FutureT]


class ProtocolContext:
    SEND_TIMEOUT_LIMIT = MAX_SEND_TIMEOUT

    def __init__(
        self,
        protocol: RamsesProtocolT,
        /,
        *,
        echo_timeout: float = DEFAULT_ECHO_TIMEOUT,
        reply_timeout: float = DEFAULT_RPLY_TIMEOUT,
        max_retry_limit: int = MAX_RETRY_LIMIT,
        max_buffer_size: int = DEFAULT_BUFFER_SIZE,
    ) -> None:
        self._protocol = protocol
        self.echo_timeout = echo_timeout
        self.reply_timeout = reply_timeout
        self.max_retry_limit = min(max_retry_limit, MAX_RETRY_LIMIT)
        self.max_buffer_size = min(max_buffer_size, DEFAULT_BUFFER_SIZE)

        self._loop = protocol._loop
        self._lock = Lock()  # FIXME: threading lock, or asyncio lock?
        self._fut: _FutureT | None = None
        self._que: PriorityQueue[_QueueEntryT] = PriorityQueue(
            maxsize=self.max_buffer_size
        )

        self._expiry_timer: asyncio.Task[None] | None = None
        self._multiplier = 0
        self._state: _ProtocolStateT = None  # type: ignore[assignment]

        # TODO: pass this over as an instance parameter
        self._send_fnc: Callable[[Command], Coroutine[Any, Any, None]] = None  # type: ignore[assignment]

        self._cmd: Command | None = None
        self._qos: QosParams | None = None
        self._cmd_tx_count: int = 0  # was: None
        self._cmd_tx_limit: int = 0

        self.set_state(Inactive)

    def __repr__(self) -> str:
        msg = f"<ProtocolContext state={repr(self._state)[21:-1]}"
        if self._cmd is None:
            return msg + ">"
        if self._cmd_tx_count == 0:  # was: is None
            return msg + ", tx_count=0/0>"
        return msg + f", tx_count={self._cmd_tx_count}/{self._cmd_tx_limit}>"

    @property
    def state(self) -> _ProtocolStateT:
        return self._state

    @property
    def is_sending(self) -> bool:  # TODO: remove asserts
        if isinstance(self._state, WantEcho | WantRply):
            assert self._cmd is not None, f"{self}: Coding error"  # mypy hint
            assert self._qos is not None, f"{self}: Coding error"  # mypy hint
            assert self._fut is not None, f"{self}: Coding error"  # mypy hint
            return True

        assert self._cmd is None, f"{self}: Coding error"  # mypy hint
        assert self._qos is None, f"{self}: Coding error"  # mypy hint
        assert self._fut is None or self._fut.done(), (
            f"{self}: Coding error"
        )  # mypy hint
        return False

    def set_state(
        self,
        state_class: _ProtocolStateClassT,
        expired: bool = False,
        timed_out: bool = False,
        exception: Exception | None = None,
        result: Packet | None = None,
    ) -> None:
        async def expire_state_on_timeout() -> None:
            # a separate coro, so can be spawned off with create_task()

            assert self._cmd is not None  # mypy

            assert isinstance(self.is_sending, bool), (
                f"{self}: Coding error"
            )  # TODO: remove
            assert self._cmd_tx_count > 0, f"{self}: Coding error"  # TODO: remove

            if isinstance(self._state, WantEcho):  # otherwise is WantRply
                delay = self.echo_timeout * (2**self._multiplier)
            # elif self._cmd.code == Code._0404:
            #     delay = self.reply_timeout * (2**self._multiplier) * 2
            else:  # isinstance(self._state, WantRply):
                delay = self.reply_timeout * (2**self._multiplier)

            # assuming success, multiplier can be decremented...
            self._multiplier, old_val = max(0, self._multiplier - 1), self._multiplier

            await asyncio.sleep(delay)  # ideally, will be interrupted by wait_for()

            # nope, was not successful, so multiplier should be incremented...
            self._multiplier = min(3, old_val + 1)

            if isinstance(self._state, WantEcho):
                _LOGGER.warning("TOUT.. = %s: echo_timeout=%s", self, delay)
            else:  # isinstance(self._state, WantRply):
                _LOGGER.warning("TOUT.. = %s: rply_timeout=%s", self, delay)

            assert isinstance(self.is_sending, bool), (
                f"{self}: Coding error"
            )  # TODO: remove

            # Timer has expired, can we retry or are we done?
            assert isinstance(self._cmd_tx_count, int)

            if self._cmd_tx_count < self._cmd_tx_limit:
                self.set_state(WantEcho, timed_out=True)
            else:
                self.set_state(IsInIdle, expired=True)

            assert isinstance(self.is_sending, bool), (
                f"{self}: Coding error"
            )  # TODO: remove

        def effect_state(timed_out: bool) -> None:
            """Take any actions indicated by state, and optionally set expiry timer."""
            # a separate function, so can be spawned off with call_soon()

            assert isinstance(self.is_sending, bool), (
                f"{self}: Coding error"
            )  # TODO: remove

            if timed_out:
                assert self._cmd is not None, f"{self}: Coding error"  # mypy hint
                self._send_cmd(self._cmd, is_retry=True)

            if isinstance(self._state, IsInIdle):
                self._loop.call_soon_threadsafe(self._check_buffer_for_cmd)

            elif isinstance(self._state, WantRply) and not self._qos.wait_for_reply:  # type: ignore[union-attr]
                self.set_state(IsInIdle, result=self._state._echo_pkt)

            elif isinstance(self._state, WantEcho | WantRply):
                self._expiry_timer = self._loop.create_task(expire_state_on_timeout())

        if self._expiry_timer is not None:
            self._expiry_timer.cancel("Changing state")
            self._expiry_timer = None

        # when _fut.done(), three possibilities:
        #  _fut.set_result()
        #  _fut.set_exception()
        #  _fut.cancel() (incl. via a send_cmd(qos.timeout) -> wait_for(timeout))

        # Changing the order of the following is fraught with danger
        if self._fut is None:  # logging only - IsInIdle, Inactive
            _LOGGER.debug("BEFORE = %s", self)
            assert self._cmd is None, f"{self}: Coding error"  # mypy hint
            assert isinstance(self._state, IsInIdle | Inactive | None), (
                f"{self}: Coding error"
            )  # mypy hint

        elif self._fut.cancelled() and not isinstance(self._state, IsInIdle):
            # cancelled by wait_for(timeout), cancel("buffer overflow"), or other?
            # was for previous send_cmd if currently IsInIdle (+/- Inactive?)
            _LOGGER.debug("BEFORE = %s: expired=%s (global)", self, expired)
            assert self._cmd is not None, f"{self}: Coding error"  # mypy hint
            assert isinstance(self._state, WantEcho | WantRply), (
                f"{self}: Coding error"
            )  # mypy hint

        elif exception:
            _LOGGER.debug("BEFORE = %s: exception=%s", self, exception)
            assert not self._fut.done(), (
                f"{self}: Coding error ({self._fut})"
            )  # mypy hint
            assert isinstance(self._state, WantEcho | WantRply), (
                f"{self}: Coding error"
            )  # mypy hint
            self._fut.set_exception(exception)  # apologise to the sender

        elif result:
            _LOGGER.debug("BEFORE = %s: result=%s", self, result._hdr)
            assert not self._fut.done(), (
                f"{self}: Coding error ({self._fut})"
            )  # mypy hint
            assert isinstance(self._state, WantEcho | WantRply), (
                f"{self}: Coding error"
            )  # mypy hint
            self._fut.set_result(result)

        elif expired:  # by expire_state_on_timeout(echo_timeout/reply_timeout)
            _LOGGER.debug("BEFORE = %s: expired=%s", self, expired)
            assert not self._fut.done(), (
                f"{self}: Coding error ({self._fut})"
            )  # mypy hint
            assert isinstance(self._state, WantEcho | WantRply), (
                f"{self}: Coding error"
            )  # mypy hint
            self._fut.set_exception(
                exc.ProtocolSendFailed(f"{self}: Exceeded maximum retries")
            )

        else:  # logging only - WantEcho, WantRply
            _LOGGER.debug("BEFORE = %s", self)
            assert self._fut is None or self._fut.cancelled() or not self._fut.done(), (
                f"{self}: Coding error ({self._fut})"
            )  # mypy hint
            # sert isinstance(self._state, WantEcho | WantRply), f"{self}: Coding error"  # mypy hint

        prev_state = self._state  # for _DBG_MAINTAIN_STATE_CHAIN

        self._state = state_class(self)  # keep atomic with tx_count / tx_limit calcs

        if _DBG_MAINTAIN_STATE_CHAIN:  # for debugging
            # tattr(prev_state, "_next_state", self._state)
            setattr(self._state, "_prev_state", prev_state)  # noqa: B010

        if timed_out:  # isinstance(self._state, WantEcho):
            assert isinstance(self._cmd_tx_count, int), (
                f"{self}: Coding error"
            )  # mypy hint
            self._cmd_tx_count += 1

        elif isinstance(self._state, WantEcho):
            assert self._qos is not None, f"{self}: Coding error"  # mypy hint
            # self._cmd_tx_limit = min(self._qos.max_retries, self.max_retry_limit) + 1
            self._cmd_tx_count = 1

        elif not isinstance(self._state, WantRply):  # IsInIdle, IsInactive
            self._cmd = self._qos = None
            self._cmd_tx_count = 0  # was: = None

        assert isinstance(self.is_sending, bool)  # TODO: remove

        # remaining code spawned off with a call_soon(), so early return to caller
        self._loop.call_soon_threadsafe(effect_state, timed_out)  # calls expire_state

        if not isinstance(self._state, WantRply):
            _LOGGER.debug("AFTER. = %s", self)
            return

        assert self._qos is not None, f"{self}: Coding error"  # mypy hint
        _LOGGER.debug("AFTER. = %s: wait_for_reply=%s", self, self._qos.wait_for_reply)

    def connection_made(self, transport: RamsesTransportT) -> None:
        # may want to set some instance variables, according to type of transport
        self._state.connection_made()

    # TODO: Should we clear the buffer if connection is lost (and apoligise to senders?
    def connection_lost(self, err: ExceptionT | None) -> None:
        self._state.connection_lost()

    def pkt_received(self, pkt: Packet) -> Any:
        self._state.pkt_rcvd(pkt)

    def pause_writing(self) -> None:
        self._state.writing_paused()

    def resume_writing(self) -> None:
        self._state.writing_resumed()

    async def send_cmd(
        self,
        send_fnc: Callable[[Command], Coroutine[Any, Any, None]],  # TODO: remove
        cmd: Command,
        priority: Priority,
        qos: QosParams,
    ) -> Packet:
        self._send_fnc = send_fnc  # TODO: REMOVE: make per Context, not per Command

        if isinstance(self._state, Inactive):
            raise exc.ProtocolSendFailed(f"{self}: Send failed (no active transport?)")

        assert self._loop is asyncio.get_running_loop()  # BUG is here

        fut: _FutureT = self._loop.create_future()
        try:
            self._que.put_nowait((priority, dt.now(), cmd, qos, fut))
        except Full as err:
            fut.cancel("Send buffer overflow")
            raise exc.ProtocolSendFailed(f"{self}: Send buffer overflow") from err

        if isinstance(self._state, IsInIdle):
            self._loop.call_soon_threadsafe(self._check_buffer_for_cmd)

        timeout = min(  # needs to be greater than worse-case via set_state engine
            qos.timeout, self.SEND_TIMEOUT_LIMIT
        )  # incl. time queued in buffer
        try:
            await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError as err:  # incl. fut.cancel()
            msg = f"{self}: Expired global timer after {timeout} sec"
            _LOGGER.warning(
                "TOUT.. = %s: send_timeout=%s (%s)", self, timeout, self._cmd is cmd
            )
            if self._cmd is cmd:  # NOTE: # this cmd may not yet be self._cmd
                self.set_state(
                    IsInIdle, expired=True
                )  # set_exception() will cause InvalidStateError
            raise exc.ProtocolSendFailed(msg) from err  # make msg *before* state reset

        try:
            return fut.result()
        except exc.ProtocolSendFailed:
            raise
        except (exc.ProtocolError, exc.TransportError) as err:  # incl. ProtocolFsmError
            raise exc.ProtocolSendFailed(f"{self}: Send failed: {err}") from err

    def _check_buffer_for_cmd(self) -> None:
        self._lock.acquire()
        assert isinstance(self.is_sending, bool), f"{self}: Coding error"  # mypy hint

        if self._fut is not None and not self._fut.done():
            self._lock.release()
            return

        while True:
            try:
                *_, self._cmd, self._qos, self._fut = self._que.get_nowait()
            except Empty:
                self._cmd = self._qos = self._fut = None
                self._lock.release()
                return

            self._cmd_tx_count = 0
            self._cmd_tx_limit = min(self._qos.max_retries, self.max_retry_limit) + 1

            assert isinstance(self._fut, asyncio.Future)  # mypy hint
            if self._fut.done():  # e.g. TimeoutError
                self._que.task_done()
                continue

            break

        self._lock.release()

        try:
            assert self._cmd is not None, f"{self}: Coding error"  # mypy hint
            self._send_cmd(self._cmd)
        finally:
            self._que.task_done()

    def _send_cmd(self, cmd: Command, is_retry: bool = False) -> None:
        """Wrapper to send a command with retries, until success or exception."""

        async def send_fnc_wrapper(cmd: Command) -> None:
            try:  # the wrapped function (actual Tx.write)
                await self._send_fnc(cmd)
            except exc.TransportError as err:
                self.set_state(IsInIdle, exception=err)

        # TODO: check what happens when exception here - why does it hang?
        assert cmd is not None, f"{self}: Coding error"

        try:  # the wrapped function (actual Tx.write)
            self._state.cmd_sent(cmd, is_retry=is_retry)
        except exc.ProtocolFsmError as err:
            self.set_state(IsInIdle, exception=err)
        else:
            self._loop.create_task(send_fnc_wrapper(cmd))


# With wait_for_reply=False
# AFTER. = <ProtocolContext state=IsInIdle>
# BEFORE = <ProtocolContext state=IsInIdle cmd_=2349|RQ|01:145038|08, tx_count=0/4>
# AFTER. = <ProtocolContext state=WantEcho cmd_=2349|RQ|01:145038|08, tx_count=1/4>
# BEFORE = <ProtocolContext state=WantEcho echo=2349|RQ|01:145038|08, tx_count=1/4>
# AFTER. = <ProtocolContext state=WantRply echo=2349|RQ|01:145038|08, tx_count=1/4>: wait_for_reply=False
#
# BEFORE = <ProtocolContext state=WantRply echo=2349|RQ|01:145038|08, tx_count=1/4>: result=2349|RQ|01:145038|08
# AFTER. = <ProtocolContext state=IsInIdle>

# With wait_for_reply=True
# AFTER. = <ProtocolContext state=IsInIdle>
# BEFORE = <ProtocolContext state=IsInIdle cmd_=0004|RQ|01:145038|05, tx_count=0/4>
# AFTER. = <ProtocolContext state=WantEcho cmd_=0004|RQ|01:145038|05, tx_count=1/4>
# BEFORE = <ProtocolContext state=WantEcho echo=0004|RQ|01:145038|05, tx_count=1/4>
# AFTER. = <ProtocolContext state=WantRply echo=0004|RQ|01:145038|05, tx_count=1/4>: wait_for_reply=True
#
# BEFORE = <ProtocolContext state=WantRply rply=0004|RP|01:145038|05, tx_count=1/4>: result=0004|RP|01:145038|05
# AFTER. = <ProtocolContext state=IsInIdle>

#######################################################################################

# NOTE: Because .dst / .src may switch from Address to Device from one pkt to the next:
#  - use: pkt.dst.id == self._echo_pkt.src.id
#  - not: pkt.dst    is self._echo_pkt.src


class ProtocolStateBase:
    def __init__(self, context: ProtocolContext) -> None:
        self._context = context

        self._sent_cmd: Command | None = None
        self._echo_pkt: Packet | None = None
        self._rply_pkt: Packet | None = None

    def __repr__(self) -> str:
        msg = f"<ProtocolState state={self.__class__.__name__}"
        if self._rply_pkt:
            return msg + f" rply={self._rply_pkt._hdr}>"
        if self._echo_pkt:
            return msg + f" echo={self._echo_pkt._hdr}>"
        if self._sent_cmd:
            return msg + f" cmd_={self._sent_cmd._hdr}>"
        return msg + ">"

    def connection_made(self) -> None:  # For all states except Inactive
        """Do nothing, as (except for InActive) we're already connected."""
        pass

    def connection_lost(self) -> None:  # Varies by states (not needed if Inactive)
        """Transition to Inactive, regardless of current state."""

        if isinstance(self._context._state, Inactive):
            return

        if isinstance(self._context._state, IsInIdle):
            self._context.set_state(Inactive)
            return

        self._context.set_state(
            Inactive, exception=exc.TransportError("Connection lost")
        )

    def pkt_rcvd(self, pkt: Packet) -> None:  # Different for each state
        """Raise a NotImplementedError."""
        raise NotImplementedError("Invalid state to receive a packet")

    def writing_paused(self) -> None:  # Currently same for all states (TBD)
        """Do nothing."""
        pass

    def writing_resumed(self) -> None:  # Currently same for all states (TBD)
        """Do nothing."""
        pass

    def cmd_sent(  # For all except IsInIdle, WantEcho
        self, cmd: Command, is_retry: bool | None = None
    ) -> None:
        raise exc.ProtocolFsmError(f"Invalid state to send a command: {self._context}")


class Inactive(ProtocolStateBase):
    """The Protocol is not connected to the transport layer."""

    def connection_made(self) -> None:
        """Transition to IsInIdle."""
        self._context.set_state(IsInIdle)

    def pkt_rcvd(self, pkt: Packet) -> None:  # raise ProtocolFsmError
        """Raise an exception, as a packet is not expected in this state."""

        assert self._sent_cmd is None, f"{self}: Coding error"

        if pkt.code != Code._PUZZ:
            _LOGGER.warning("%s: Invalid state to receive a packet", self._context)


class IsInIdle(ProtocolStateBase):
    """The Protocol is not in the process of sending a Command."""

    def pkt_rcvd(self, pkt: Packet) -> None:  # Do nothing
        """Do nothing as we're not expecting an echo, nor a reply."""

        assert self._sent_cmd is None, f"{self}: Coding error"

        pass

    def cmd_sent(  # Will expect an Echo
        self, cmd: Command, is_retry: bool | None = None
    ) -> None:
        """Transition to WantEcho."""

        assert self._sent_cmd is None and is_retry is False, f"{self}: Coding error"

        self._sent_cmd = cmd

        # HACK for headers with sentinel values:
        #  I --- 18:000730 18:222222 --:------ 30C9 003 000333  # 30C9| I|18:000730,    *but* will be: 30C9| I|18:222222
        #  I --- --:------ --:------ 18:000730 0008 002 00BB    # 0008| I|18:000730|00, *and* will be unchanged

        if HGI_DEVICE_ID in cmd.tx_header:  # HACK: what do I do about this
            cmd._hdr_ = cmd._hdr_.replace(HGI_DEVICE_ID, self._context._protocol.hgi_id)
        self._context.set_state(WantEcho)


class WantEcho(ProtocolStateBase):
    """The Protocol is waiting to receive an echo Packet."""

    # NOTE: unfortunately, the cmd's src / echo's src can be different:
    # RQ --- 18:000730 10:052644 --:------ 3220 005 0000050000  # RQ|10:048122|3220|05
    # RQ --- 18:198151 10:052644 --:------ 3220 005 0000050000  # RQ|10:048122|3220|05

    def __init__(self, context: ProtocolContext) -> None:
        super().__init__(context)

        self._sent_cmd = context._state._sent_cmd
        # if isinstance(context._state, WantEcho | WantRply):
        #     self._echo_pkt = context._state._echo_pkt
        # else:
        #     self._echo_pkt = None

    def pkt_rcvd(self, pkt: Packet) -> None:  # Check if pkt is expected Echo
        """If the pkt is the expected Echo, transition to IsInIdle, or WantRply."""

        # RQ --- 18:002563 01:078710 --:------ 2349 002 0200                # 2349|RQ|01:078710|02
        # RP --- 01:078710 18:002563 --:------ 2349 007 0201F400FFFFFF      # 2349|RP|01:078710|02
        #  W --- 30:257306 01:096339 --:------ 313F 009 0060002916050B07E7  # 313F| W|01:096339
        #  I --- 01:096339 30:257306 --:------ 313F 009 00FC0029D6050B07E7  # 313F| I|01:096339

        assert self._sent_cmd, f"{self}: Coding error"  # mypy hint

        # if self._sent_cmd.rx_header and pkt._hdr == self._sent_cmd.rx_header:
        #     _LOGGER.error("hdr=%s", self._sent_cmd.rx_header)
        #     _LOGGER.error("src=%s", self._sent_cmd.src.id)
        #     _LOGGER.error("dst=%s", pkt.dst.id)

        if (
            self._sent_cmd.rx_header
            and pkt._hdr == self._sent_cmd.rx_header
            and (
                pkt.dst.id == self._sent_cmd.src.id
                or (  # handle: 18:146440 == 18:000730
                    self._sent_cmd.src.id == HGI_DEVICE_ID
                    and pkt.dst.id == self._context._protocol.hgi_id
                )
            )
        ):
            _LOGGER.warning(
                "%s: Invalid state to receive a reply (expecting echo)", self._context
            )

            self._rply_pkt = pkt
            self._context.set_state(IsInIdle, result=pkt)
            return

        # HACK for packets with addr sets like (issue is only with sentinel values?):
        #  I --- --:------ --:------ 18:000730 0008 002 00BB

        if HGI_DEVICE_ID in pkt._hdr:  # HACK: what do I do about this?
            pkt__hdr = pkt._hdr_.replace(HGI_DEVICE_ID, self._context._protocol.hgi_id)
        else:
            pkt__hdr = pkt._hdr

        if pkt__hdr != self._sent_cmd.tx_header:
            return

        # # HACK: for testing - drop some packets
        # import random
        # if random.random() < 0.2:
        #     return

        self._echo_pkt = pkt
        if self._sent_cmd.rx_header:
            self._context.set_state(WantRply)
        else:
            self._context.set_state(IsInIdle, result=pkt)

    def cmd_sent(self, cmd: Command, is_retry: bool | None = None) -> None:
        """Transition to WantEcho (i.e. a retransmit)."""

        assert self._sent_cmd is not None and is_retry is True, f"{self}: Coding error"

        # NOTE: don't self._context.set_state(WantEcho) here - may cause endless loop


class WantRply(ProtocolStateBase):
    """The Protocol is waiting to receive an reply Packet."""

    # NOTE: is possible get a false rply (same rx_header), e.g.:
    # RP --- 10:048122 18:198151 --:------ 3220 005 00C0050000  # 3220|RP|10:048122|05
    # RP --- 10:048122 01:145038 --:------ 3220 005 00C0050000  # 3220|RP|10:048122|05

    # NOTE: unfortunately, the cmd's src / rply's dst can still be different:
    # RQ --- 18:000730 10:052644 --:------ 3220 005 0000050000  # 3220|RQ|10:048122|05
    # RP --- 10:048122 18:198151 --:------ 3220 005 00C0050000  # 3220|RP|10:048122|05

    def __init__(self, context: ProtocolContext) -> None:
        super().__init__(context)

        self._sent_cmd = context._state._sent_cmd
        self._echo_pkt = context._state._echo_pkt

    def pkt_rcvd(self, pkt: Packet) -> None:  # Check if pkt is expected Reply
        """If the pkt is the expected reply, transition to IsInIdle."""

        assert self._sent_cmd, f"{self}: Coding error"  # mypy hint
        assert self._echo_pkt, f"{self}: Coding error"  # mypy hint

        # NOTE: beware collisions: same header, but is not reply (must check RSSI or src)
        # 2024-04-16 08:28:33.895 000 RQ --- 18:146440 10:048122 --:------ 3220 005 0000110000  # 3220|RQ|10:048122|11
        # 2024-04-16 08:28:33.910 052 RQ --- 01:145038 10:048122 --:------ 3220 005 0000110000  # 3220|RQ|10:048122|11

        if pkt._hdr == self._sent_cmd.tx_header and pkt.src == self._echo_pkt.src:
            _LOGGER.warning(
                "%s: Invalid state to receive an echo (expecting reply)", self._context
            )
            return  # do not transition, wait until existing timer expires

        # HACK: special case: if null log entry for log_idx=nn, then
        # HACK: rx_hdr will be 0418|RP|01:145038|00, and not 0418|RP|01:145038|nn
        # HACK: wait_for_reply must be true for RQ|0418 commands
        if (
            self._sent_cmd.rx_header[:8] == "0418|RP|"  # type: ignore[index]
            and self._sent_cmd.rx_header[:-2] == pkt._hdr[:-2]  # type: ignore[index]
            and pkt.payload == "000000B0000000000000000000007FFFFF7000000000"
        ):
            self._rply_pkt = pkt

        elif pkt._hdr != self._sent_cmd.rx_header:
            return

        else:
            self._rply_pkt = pkt

        self._context.set_state(IsInIdle, result=pkt)


#######################################################################################


_ProtocolStateT: TypeAlias = Inactive | IsInIdle | WantEcho | WantRply

_ProtocolStateClassT: TypeAlias = (
    type[Inactive] | type[IsInIdle] | type[WantEcho] | type[WantRply]
)
