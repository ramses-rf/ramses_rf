"""Tests for task/timer cleanup during gateway shutdown and connection loss.

These tests prove that fire-and-forget tasks, binding manager timers, and
discovery pollers are properly cleaned up when the gateway stops or the
transport connection is lost, preventing lingering task errors.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

from ramses_rf import Gateway
from ramses_rf.gateway import GatewayConfig
from ramses_tx.const import I_, Code


async def test_handler_tasks_cancelled_on_connection_lost() -> None:
    """Handler dispatch tasks are cancelled when connection_lost is called.

    This proves the fix for the fire-and-forget loop.create_task(res) calls
    in protocol/base.py that were never tracked or cancelled.
    """
    import tempfile

    with tempfile.NamedTemporaryFile() as tmp:
        config = GatewayConfig()
        config.disable_discovery = True
        config.engine.input_file = tmp.name

        gwy = Gateway(config=config)
        with contextlib.suppress(Exception):
            await gwy.start(start_discovery=False)

        # Get the protocol from the engine
        protocol = gwy._engine._protocol

        # Reset the connection_lost future so connection_lost doesn't early-return
        protocol._wait_connection_lost = protocol._loop.create_future()

        # Simulate a pending handler task by creating one directly
        async def _dummy_handler() -> None:
            await asyncio.sleep(100)

        protocol._create_handler_task(_dummy_handler())

        # Verify the task is tracked
        assert len(protocol._handler_tasks) == 1
        task = next(iter(protocol._handler_tasks))
        assert not task.done()

        # Trigger connection_lost
        protocol.connection_lost(None)

        # Yield to let cancellation propagate
        await asyncio.sleep(0)

        # The task should be cancelled and the set cleared
        assert len(protocol._handler_tasks) == 0
        assert task.cancelled() or task.done()

        with contextlib.suppress(asyncio.CancelledError):
            await task

        await gwy.stop()


async def test_entity_state_delete_tasks_cancelled_on_log_reset() -> None:
    """EntityState delete tasks are cancelled when the log is reset.

    This proves the fix for untracked loop.create_task(self._delete_msg(msg))
    calls in entity_state.py that would linger if the log was cleared.
    """
    from ramses_rf.routing import RoutingContext, StateHeader
    from ramses_rf.state import EntityState

    class DummyMsg:
        def __init__(self) -> None:
            self.src = MagicMock()
            self.src.id = "04:123456"
            self.dst = MagicMock()
            self.dst.id = "01:000000"
            self.verb = I_
            self.code = Code._30C9
            self.dtm = dt.now()
            self._pkt = MagicMock()
            self._pkt._ctx = False
            self._expired = False
            self.payload = {"temperature": 21.0, "zone_idx": "00"}

            # state_header property
            ctx = RoutingContext("00")
            self.state_header = StateHeader(
                code=Code._30C9, verb=I_, source_id="04:123456", context=ctx
            )

    gwy = MagicMock()
    gwy._loop = asyncio.get_running_loop()
    gwy.message_store = None

    entity = EntityState(MagicMock(), gwy)

    # Simulate a delete task
    async def _dummy_delete() -> None:
        await asyncio.sleep(100)

    task = asyncio.create_task(_dummy_delete())
    entity._delete_tasks.add(task)
    task.add_done_callback(entity._delete_tasks.discard)

    assert len(entity._delete_tasks) == 1

    # Simulate a log reset (cursor goes backwards)
    entity._log_cursor = 10
    # Need to set up message_store with a shorter log to trigger reset
    gwy.message_store = MagicMock()
    gwy.message_store.log_by_dtm = []  # empty list, len=0 < cursor=10

    entity._sync_state()

    # Yield to let cancellation propagate
    await asyncio.sleep(0)

    # The delete task should have been cancelled
    assert len(entity._delete_tasks) == 0
    assert task.cancelled() or task.done()

    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_gateway_stop_cancels_binding_managers() -> None:
    """Gateway.stop() cancels all binding manager timers.

    This proves the fix for binding_fsm.py timer handles that were never
    cancelled during gateway shutdown.
    """
    import tempfile

    with tempfile.NamedTemporaryFile() as tmp:
        config = GatewayConfig()
        config.disable_discovery = True
        config.engine.input_file = tmp.name

        gwy = Gateway(config=config)
        with contextlib.suppress(Exception):
            await gwy.start(start_discovery=False)

        # Create a mock binding manager on a device
        dev = MagicMock()
        dev._binding_manager = MagicMock()

        # Inject the mock device into the registry
        gwy.device_registry.devices.append(dev)

        await gwy.stop()

        # Binding manager cancel was called
        dev._binding_manager.cancel.assert_called_once()


async def test_gateway_stop_stops_polling_manager() -> None:
    """Gateway.stop() stops the PollingManager.

    This proves that the L7 PollingManager background loop is cleanly stopped
    during gateway shutdown.
    """
    import tempfile

    with tempfile.NamedTemporaryFile() as tmp:
        config = GatewayConfig()
        config.disable_discovery = True
        config.engine.input_file = tmp.name

        gwy = Gateway(config=config)
        with contextlib.suppress(Exception):
            await gwy.start(start_discovery=False)

        with patch.object(
            gwy.polling_manager, "stop", new_callable=AsyncMock
        ) as mock_stop:
            await gwy.stop()
            mock_stop.assert_awaited_once()


async def test_handler_tasks_auto_cleanup_on_completion() -> None:
    """Completed handler tasks are automatically removed from the tracking set.

    This proves the done_callback mechanism works correctly to prevent
    the _handler_tasks set from growing unbounded.
    """
    import tempfile

    with tempfile.NamedTemporaryFile() as tmp:
        config = GatewayConfig()
        config.disable_discovery = True
        config.engine.input_file = tmp.name

        gwy = Gateway(config=config)
        with contextlib.suppress(Exception):
            await gwy.start(start_discovery=False)

        protocol = gwy._engine._protocol

        async def _quick_handler() -> None:
            pass

        protocol._create_handler_task(_quick_handler())

        # Let the task complete
        await asyncio.sleep(0.1)

        # The completed task should have been auto-removed from the set
        assert len(protocol._handler_tasks) == 0

        await gwy.stop()
