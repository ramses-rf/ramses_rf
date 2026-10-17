"""Native APS acknowledgements must pace writes without ASCII control traffic."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from zigpy import types
from zigpy.zcl import Cluster, foundation

from ramses_tx import exceptions as exc

from .test_transport_zigbee import _VALID_URL, _make_transport


class NativeTXCluster(Cluster):
    """Use the same command schema as the current ELECRAM ZHA quirk."""

    cluster_id = 0xFC01

    class ClientCommandDefs(foundation.BaseCommandDefs):
        send_chunk = foundation.ZCLCommandDef(
            id=0x02,
            schema={
                "message_id": types.uint32_t,
                "sequence": types.uint8_t,
                "total": types.uint8_t,
                "text": types.CharacterString,
            },
        )
        native_mode = foundation.ZCLCommandDef(
            id=0x03, schema={"enabled": types.Bool}
        )


def test_existing_urls_keep_legacy_acknowledgements() -> None:
    transport = _make_transport()
    assert not transport._aps_ack
    with pytest.raises(exc.TransportSourceInvalid):
        _make_transport(f"{_VALID_URL}?ack=unknown")


@pytest.mark.parametrize("callback", ["cluster_command", "attribute_updated"])
def test_aps_reassembly_never_schedules_ascii_ack(callback: str) -> None:
    transport = _make_transport(f"{_VALID_URL}?ack=aps")
    transport._frame_read = MagicMock()
    for payload in ("1/2|first", "2/2|second"):
        if callback == "cluster_command":
            transport.cluster_command(1, 0, payload)
        else:
            transport.attribute_updated(transport._attr_id, payload)
    transport._frame_read.assert_called_once()
    assert transport._frame_read.call_args.args[1] == "firstsecond"
    transport._loop.create_task.assert_not_called()


@pytest.mark.asyncio
async def test_real_zigpy_serialization_requests_aps_ack_without_zcl_reply() -> (
    None
):
    transport = _make_transport(f"{_VALID_URL}?ack=aps")
    endpoint = MagicMock()
    endpoint.reply = AsyncMock()
    endpoint.device.get_sequence.return_value = 7
    cluster = NativeTXCluster(endpoint, is_server=True)
    transport._get_active_write_cluster = MagicMock(return_value=cluster)
    transport._native_message_id = 0x12345678
    await transport._send_command("hello", 1, 2)
    kwargs = endpoint.reply.call_args.kwargs
    assert kwargs["ask_for_ack"] is True
    assert kwargs["expect_reply"] is False
    header, body = foundation.ZCLHeader.deserialize(kwargs["data"])
    assert header.frame_control.disable_default_response
    assert (
        header.frame_control.direction == foundation.Direction.Client_to_Server
    )
    assert header.command_id == 0x02
    assert body == b"\x78\x56\x34\x12\x01\x02\x05hello"


@pytest.mark.asyncio
async def test_failed_delivery_aborts_remaining_chunks_without_api_fallback() -> (
    None
):
    transport = _make_transport(f"{_VALID_URL}?ack=aps")
    cluster = MagicMock()
    cluster.client_commands = {2: object()}
    cluster.client_command = AsyncMock(side_effect=TimeoutError)
    cluster.command = AsyncMock()
    cluster.server_command = AsyncMock()
    transport._get_active_write_cluster = MagicMock(return_value=cluster)
    with pytest.raises(exc.TransportZigbeeError, match="Aborting"):
        await transport._write_frame("x" * 100)
    cluster.client_command.assert_awaited_once()
    cluster.command.assert_not_called()
    cluster.server_command.assert_not_called()


@pytest.mark.asyncio
async def test_next_chunk_waits_for_delivery_and_frames_do_not_interleave() -> (
    None
):
    transport = _make_transport(f"{_VALID_URL}?ack=aps")
    first_started = asyncio.Event()
    confirm = asyncio.Event()
    sent: list[str] = []

    async def send(chunk: str, sequence: int, total: int) -> None:
        sent.append(chunk)
        if len(sent) == 1:
            first_started.set()
            await confirm.wait()

    transport._send_command = send
    first = asyncio.create_task(transport._write_frame("a" * 70))
    await first_started.wait()
    second = asyncio.create_task(transport._write_frame("b" * 70))
    await asyncio.sleep(0)
    assert len(sent) == 1
    confirm.set()
    await asyncio.gather(first, second)
    assert len(sent) == 6
    assert all("a" in chunk for chunk in sent[:3])
    assert all("b" in chunk for chunk in sent[3:])


@pytest.mark.asyncio
async def test_aps_payloads_stay_within_conservative_wire_budget() -> None:
    transport = _make_transport(f"{_VALID_URL}?ack=aps")
    sent: list[str] = []

    async def send(chunk: str, sequence: int, total: int) -> None:
        sent.append(chunk)

    transport._send_command = send
    for length in range(1, 256):
        # Avoid intentionally added pacing sleeps in this payload-boundary test.
        with patch(
            "ramses_tx.transport.zigbee.transport.asyncio.sleep",
            new=AsyncMock(),
        ):
            await transport._write_frame("x" * length)
    assert all(len(chunk.encode("ascii")) + 10 <= 40 for chunk in sent)


def test_new_message_replaces_incomplete_message_with_same_chunk_count() -> (
    None
):
    transport = _make_transport(f"{_VALID_URL}?ack=aps")
    transport._frame_read = MagicMock()
    for payload in ("1/3|old", "2/3|obsolete", "1/3|new", "3/3|end"):
        transport.cluster_command(1, 0, payload)
    transport._frame_read.assert_not_called()
    transport.cluster_command(1, 0, "2/3|middle")
    assert transport._frame_read.call_args.args[1] == "newmiddleend"


def test_malformed_chunk_and_legacy_ack_never_schedule_control_traffic() -> (
    None
):
    transport = _make_transport(f"{_VALID_URL}?ack=aps")
    transport._frame_read = MagicMock()
    transport.cluster_command(1, 0, "ACK 1/3")
    transport._frame_read.assert_not_called()
    transport.cluster_command(1, 0, "0/3|invalid")
    transport._loop.create_task.assert_not_called()


def test_native_late_chunks_cannot_join_a_different_message() -> None:
    transport = _make_transport(f"{_VALID_URL}?ack=aps")
    transport._frame_read = MagicMock()
    for args in ((10, 1, 2, "old"), (11, 1, 2, "new"), (10, 2, 2, "late")):
        transport.cluster_command(1, 2, args)
    transport._frame_read.assert_not_called()
    transport.cluster_command(1, 2, (11, 2, 2, "end"))
    assert transport._frame_read.call_args.args[1] == "newend"
    transport._loop.create_task.assert_not_called()


def test_native_gap_drops_message_and_next_message_recovers() -> None:
    transport = _make_transport(f"{_VALID_URL}?ack=aps")
    transport._frame_read = MagicMock()
    for args in ((10, 1, 3, "first"), (10, 3, 3, "gap"), (10, 2, 3, "late")):
        transport.cluster_command(1, 2, args)
    transport._frame_read.assert_not_called()
    transport.cluster_command(1, 2, (11, 1, 1, "complete"))
    assert transport._frame_read.call_args.args[1] == "complete"


def test_native_duplicate_chunks_do_not_deliver_twice() -> None:
    transport = _make_transport(f"{_VALID_URL}?ack=aps")
    transport._frame_read = MagicMock()
    for args in ((10, 1, 2, "one"), (10, 1, 2, "one"), (10, 2, 2, "two")) * 2:
        transport.cluster_command(1, 2, args)
    transport._frame_read.assert_called_once()
    assert transport._frame_read.call_args.args[1] == "onetwo"


@pytest.mark.asyncio
async def test_native_timeout_releases_write_lock_for_next_frame() -> None:
    transport = _make_transport(f"{_VALID_URL}?ack=aps")
    transport._APS_TIMEOUT = 0.01
    cluster = MagicMock()
    cluster.client_commands = {2: object()}

    async def never_confirms(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    cluster.client_command = AsyncMock(side_effect=never_confirms)
    transport._get_active_write_cluster = MagicMock(return_value=cluster)
    with pytest.raises(exc.TransportZigbeeError):
        await transport._write_frame("first")
    assert not transport._write_lock.locked()
    cluster.client_command.side_effect = None
    await transport._write_frame("next")
    assert cluster.client_command.await_count == 2


@pytest.mark.asyncio
async def test_native_mode_selection_is_a_single_aps_command() -> None:
    transport = _make_transport(f"{_VALID_URL}?ack=aps")
    cluster = MagicMock()
    cluster.client_commands = {3: object()}
    cluster.client_command = AsyncMock()
    transport._get_active_write_cluster = MagicMock(return_value=cluster)
    await transport._enable_native_mode()
    cluster.client_command.assert_awaited_once_with(
        3,
        True,
        expect_reply=False,
        ask_for_ack=True,
        disable_default_response=True,
    )
