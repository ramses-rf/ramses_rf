"""An incomplete native frame must never contaminate a subsequent RF frame."""

from datetime import timedelta as td
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ramses_tx.helpers import dt_now

from .test_transport_zigbee import (
    _VALID_URL,
    _make_transport,
    _transport_with_device,
)
from .test_zigbee_aps import NativeTXCluster


def test_expired_native_assembly_discards_late_tail() -> None:
    transport = _make_transport(f"{_VALID_URL}?ack=aps")
    transport._frame_read = MagicMock()
    transport.cluster_command(1, 2, (100, 1, 2, "old"))
    future = dt_now() + td(seconds=36)
    with patch(
        "ramses_tx.transport.zigbee.transport.dt_now", return_value=future
    ):
        transport.cluster_command(1, 2, (100, 2, 2, "late"))
        transport._frame_read.assert_not_called()
        transport.cluster_command(1, 2, (101, 1, 1, "new"))
    assert transport._frame_read.call_args.args[1] == "new"


@pytest.mark.parametrize(
    "malformed",
    [
        (1, 0, 2, "bad"),
        (1, 1, 10, "bad"),
        (1, 1, 1, "x" * 31),
        (1, 1, 1, "\x00"),
        (1, 1, 1, ""),
        (1, 1, 1, "é"),
    ],
)
def test_malformed_native_chunk_is_not_delivered(
    malformed: tuple[int, int, int, str],
) -> None:
    transport = _make_transport(f"{_VALID_URL}?ack=aps")
    transport._frame_read = MagicMock()
    transport.cluster_command(1, 2, malformed)
    transport._frame_read.assert_not_called()


def test_changed_total_aborts_current_native_message() -> None:
    transport = _make_transport(f"{_VALID_URL}?ack=aps")
    transport._frame_read = MagicMock()
    for args in (
        (100, 1, 3, "first"),
        (100, 2, 2, "bad"),
        (100, 3, 3, "tail"),
    ):
        transport.cluster_command(1, 2, args)
    transport._frame_read.assert_not_called()


def test_schema_object_from_quirk_is_accepted() -> None:
    transport = _make_transport(f"{_VALID_URL}?ack=aps")
    transport._frame_read = MagicMock()
    schema = NativeTXCluster.ClientCommandDefs.send_chunk.schema
    transport.cluster_command(
        1, 2, schema(message_id=100, sequence=1, total=1, text="frame")
    )
    assert transport._frame_read.call_args.args[1] == "frame"


@pytest.mark.asyncio
async def test_reconnect_selects_native_mode_before_becoming_ready() -> None:
    transport = _transport_with_device(last_seen_age=0)
    transport._aps_ack = True
    transport._device_online = False
    transport._attach_clusters = MagicMock()
    transport._bind_and_configure = AsyncMock()
    transport._enable_native_mode = AsyncMock()
    transport._make_connection = MagicMock()
    await transport._try_reconnect()
    transport._enable_native_mode.assert_awaited_once()
    transport._make_connection.assert_called_once()
    assert transport._device_online
