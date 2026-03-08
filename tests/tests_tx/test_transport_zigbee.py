#!/usr/bin/env python3
"""Unit tests for ZigbeeTransport (>90% coverage of transport/zigbee.py)."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from ramses_tx import exceptions as exc
from ramses_tx.transport import TransportConfig
from ramses_tx.transport.zigbee import ZigbeeTransport

# ---------------------------------------------------------------------------
# Valid test URL  (zigbee://ieee/cluster/attr/endpoint/write_cluster/write_attr/write_endpoint)
# ---------------------------------------------------------------------------

_IEEE = "00:11:22:33:44:55:66:77"
_VALID_URL = f"zigbee://{_IEEE}/0xFC00/0x0000/1/0xFC00/0x0001/1"


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------


def _mock_create_task() -> MagicMock:
    """Return a MagicMock for ``loop.create_task`` that closes coroutines immediately.

    Without this, async coroutines passed to the mock are silently leaked and Python
    emits ``RuntimeWarning: coroutine '...' was never awaited``.
    """

    def _close_coro(coro: Any, **_kwargs: Any) -> MagicMock:
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()

    return MagicMock(side_effect=_close_coro)


def _make_transport(
    url: str = _VALID_URL,
    app_context: Any = "DEFAULT_MOCK",
) -> ZigbeeTransport:
    """Create a ZigbeeTransport with *_async_init* suppressed.

    Passes a mock event loop to avoid touching the real one, and closes the
    coroutine created by ``create_task`` so Python does not emit a
    "coroutine was never awaited" warning.
    """
    mock_protocol = MagicMock()

    if isinstance(app_context, str) and app_context == "DEFAULT_MOCK":
        app_context = MagicMock()

    mock_loop = MagicMock(spec=asyncio.AbstractEventLoop)

    def _noop_create_task(coro: Any, **_kwargs: Any) -> MagicMock:
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()

    mock_loop.create_task.side_effect = _noop_create_task
    # After construction also install the coro-closing mock so any subsequent
    # direct calls on t._loop.create_task (e.g. from attribute_updated) are
    # handled cleanly.

    transport = ZigbeeTransport(
        url,
        mock_protocol,
        config=TransportConfig(app_context=app_context),
        loop=mock_loop,
    )
    # Re-install the coro-closing mock so that any subsequent create_task calls
    # (e.g. from attribute_updated / cluster_command ACK scheduling) also close
    # the coroutine immediately rather than leaking it.
    transport._loop.create_task = _mock_create_task()
    return transport


# ---------------------------------------------------------------------------
# 1. URL parsing
# ---------------------------------------------------------------------------


class TestZigbeeTransportUrlParsing(unittest.TestCase):
    """Tests for Zigbee URL parsing in ``__init__``."""

    def test_hex_ids_parsed_correctly(self) -> None:
        t = _make_transport()
        self.assertEqual(t._ieee, _IEEE)
        self.assertEqual(t._cluster_id, 0xFC00)
        self.assertEqual(t._attr_id, 0x0000)
        self.assertEqual(t._endpoint_id, 1)
        self.assertEqual(t._write_cluster_id, 0xFC00)
        self.assertEqual(t._write_attr_id, 0x0001)
        self.assertEqual(t._write_endpoint_id, 1)

    def test_decimal_cluster_ids_parsed(self) -> None:
        url = f"zigbee://{_IEEE}/64512/0/1/64512/1/1"
        t = _make_transport(url)
        self.assertEqual(t._cluster_id, 64512)
        self.assertEqual(t._attr_id, 0)

    def test_command_mode_always_enabled(self) -> None:
        """_use_command_mode is forced True regardless of the ?cmd query."""
        t = _make_transport(f"{_VALID_URL}?cmd=0x03")
        self.assertTrue(t._use_command_mode)

    def test_cmd_id_from_query(self) -> None:
        """Custom cmd id is parsed from the query string."""
        t = _make_transport(f"{_VALID_URL}?cmd=0x05")
        self.assertEqual(t._cmd_id, 0x05)

    def test_missing_url_parts_raises(self) -> None:
        bad_url = f"zigbee://{_IEEE}/0xFC00/0x0000/1/0xFC00"  # only 4 path parts
        with self.assertRaises(exc.TransportSourceInvalid):
            _make_transport(bad_url)

    def test_missing_ieee_raises(self) -> None:
        with self.assertRaises(exc.TransportSourceInvalid):
            _make_transport("zigbee:///0xFC00/0x0000/1/0xFC00/0x0001/1")

    def test_hass_extracted_from_config(self) -> None:
        mock_hass = MagicMock()
        t = _make_transport(app_context=mock_hass)
        self.assertIs(t._hass, mock_hass)

    def test_hass_none_when_missing_from_config(self) -> None:
        t = _make_transport(app_context=None)
        self.assertIsNone(t._hass)

    def test_is_evofw3_set_true(self) -> None:
        from ramses_tx.const import SZ_IS_EVOFW3

        t = _make_transport()
        self.assertTrue(t._extra.get(SZ_IS_EVOFW3))

    def test_chunk_buffers_initially_empty(self) -> None:
        t = _make_transport()
        self.assertEqual(t._chunk_buffers, {})


# ---------------------------------------------------------------------------
# 2. _parse_chunk
# ---------------------------------------------------------------------------


class TestParseChunk(unittest.TestCase):
    """Tests for ``_parse_chunk``."""

    def setUp(self) -> None:
        self.t = _make_transport()

    def test_valid_middle_chunk(self) -> None:
        self.assertEqual(self.t._parse_chunk("2/5|body"), (2, 5, "body"))

    def test_valid_single_chunk(self) -> None:
        self.assertEqual(self.t._parse_chunk("1/1|payload"), (1, 1, "payload"))

    def test_valid_last_chunk(self) -> None:
        self.assertEqual(self.t._parse_chunk("3/3|end"), (3, 3, "end"))

    def test_empty_body_allowed(self) -> None:
        self.assertEqual(self.t._parse_chunk("1/2|"), (1, 2, ""))

    def test_no_pipe_returns_none(self) -> None:
        self.assertIsNone(self.t._parse_chunk("plain frame text"))

    def test_seq_greater_than_total_returns_none(self) -> None:
        self.assertIsNone(self.t._parse_chunk("5/3|body"))

    def test_zero_seq_returns_none(self) -> None:
        self.assertIsNone(self.t._parse_chunk("0/3|body"))

    def test_zero_total_returns_none(self) -> None:
        self.assertIsNone(self.t._parse_chunk("1/0|body"))

    def test_multiline_body_returned(self) -> None:
        result = self.t._parse_chunk("1/1|line1\nline2")
        self.assertIsNotNone(result)
        self.assertIn("line1", result[2])

    def test_plain_ack_returns_none(self) -> None:
        self.assertIsNone(self.t._parse_chunk("ACK 1/3"))


# ---------------------------------------------------------------------------
# 3. _chunk_payload
# ---------------------------------------------------------------------------


class TestChunkPayload(unittest.TestCase):
    """Tests for ``_chunk_payload``."""

    def setUp(self) -> None:
        self.t = _make_transport()

    def test_short_payload_single_chunk(self) -> None:
        chunks = self.t._chunk_payload("hello")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], (1, 1, "hello"))

    def test_exactly_max_length_is_single_chunk(self) -> None:
        payload = "x" * self.t._max_char_len
        chunks = self.t._chunk_payload(payload)
        self.assertEqual(len(chunks), 1)

    def test_long_payload_splits_into_multiple(self) -> None:
        payload = "A" * 200
        chunks = self.t._chunk_payload(payload)
        self.assertGreater(len(chunks), 1)

    def test_chunk_sequence_numbers_are_correct(self) -> None:
        payload = "B" * 200
        chunks = self.t._chunk_payload(payload)
        for idx, (seq, total, _) in enumerate(chunks):
            self.assertEqual(seq, idx + 1)
            self.assertEqual(total, len(chunks))

    def test_chunk_strings_within_max_length(self) -> None:
        payload = "C" * 500
        for _, _, chunk_str in self.t._chunk_payload(payload):
            self.assertLessEqual(len(chunk_str), self.t._max_char_len)

    def test_reassembly_recovers_original_payload(self) -> None:
        payload = "D" * 150
        chunks = self.t._chunk_payload(payload)
        assembled = "".join(
            c[chunk_str.index("|") + 1 :]
            for _, _, chunk_str in chunks
            for c in [chunk_str]
        )
        self.assertEqual(assembled, payload)

    def test_empty_payload_single_chunk(self) -> None:
        chunks = self.t._chunk_payload("")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], (1, 1, ""))


# ---------------------------------------------------------------------------
# 4. _maybe_handle_incoming_chunk
# ---------------------------------------------------------------------------


class TestMaybeHandleIncomingChunk(unittest.TestCase):
    """Tests for ``_maybe_handle_incoming_chunk``."""

    def setUp(self) -> None:
        self.t = _make_transport()
        self.t._frame_read = MagicMock()
        self.t._loop.create_task = _mock_create_task()

    def test_non_chunk_returns_false(self) -> None:
        self.assertFalse(self.t._maybe_handle_incoming_chunk("plain frame"))
        self.t._frame_read.assert_not_called()

    def test_single_chunk_1of1_delivers_immediately(self) -> None:
        result = self.t._maybe_handle_incoming_chunk("1/1|hello")
        self.assertTrue(result)
        self.t._frame_read.assert_called_once()
        # Assembled payload "hello" should appear in the call
        _, frame_arg = self.t._frame_read.call_args[0]
        self.assertIn("hello", frame_arg)

    def test_partial_chunk_returns_true_no_delivery(self) -> None:
        self.assertTrue(self.t._maybe_handle_incoming_chunk("1/3|part1"))
        self.t._frame_read.assert_not_called()

    def test_multi_chunk_completes_on_last_part(self) -> None:
        self.t._maybe_handle_incoming_chunk("1/2|hello ")
        self.t._frame_read.assert_not_called()
        self.t._maybe_handle_incoming_chunk("2/2|world")
        self.t._frame_read.assert_called_once()
        _, frame_arg = self.t._frame_read.call_args[0]
        self.assertIn("hello world", frame_arg)

    def test_duplicate_chunk_does_not_double_count(self) -> None:
        self.t._maybe_handle_incoming_chunk("1/2|part1")
        self.t._maybe_handle_incoming_chunk("1/2|part1")  # duplicate
        self.t._frame_read.assert_not_called()
        self.t._maybe_handle_incoming_chunk("2/2|part2")
        self.assertEqual(self.t._frame_read.call_count, 1)

    def test_new_total_resets_buffer(self) -> None:
        """Starting a new transfer with a different total discards the old buffer."""
        self.t._maybe_handle_incoming_chunk("1/2|old")
        # New transfer with total=3
        self.t._maybe_handle_incoming_chunk("1/3|new1")
        self.t._maybe_handle_incoming_chunk("2/3|new2")
        self.t._frame_read.assert_not_called()
        self.t._maybe_handle_incoming_chunk("3/3|new3")
        self.t._frame_read.assert_called_once()

    def test_buffer_cleared_after_completion(self) -> None:
        self.t._maybe_handle_incoming_chunk("1/1|data")
        self.assertNotIn(self.t._ieee, self.t._chunk_buffers)

    def test_per_chunk_ack_scheduled(self) -> None:
        """An ACK task should be scheduled for each received chunk."""
        self.t._maybe_handle_incoming_chunk("1/3|part")
        self.t._loop.create_task.assert_called()


# ---------------------------------------------------------------------------
# 5. _decode_command_payload
# ---------------------------------------------------------------------------


class TestDecodeCommandPayload(unittest.TestCase):
    """Tests for ``_decode_command_payload``."""

    def setUp(self) -> None:
        self.t = _make_transport()

    def test_string_passthrough(self) -> None:
        self.assertEqual(self.t._decode_command_payload("hello"), "hello")

    def test_bytes_with_valid_length_prefix(self) -> None:
        data = b"hello"
        raw = bytes([len(data)]) + data
        self.assertEqual(self.t._decode_command_payload(raw), "hello")

    def test_bytearray_with_length_prefix(self) -> None:
        data = b"test"
        raw = bytearray([len(data)]) + bytearray(data)
        self.assertEqual(self.t._decode_command_payload(raw), "test")

    def test_bytes_chunk_header_returned_as_is(self) -> None:
        payload = "1/2|data"
        raw = bytes([len(payload)]) + payload.encode()
        result = self.t._decode_command_payload(raw)
        self.assertEqual(result, payload)

    def test_bytes_without_valid_prefix_fallback_decode(self) -> None:
        # First byte (0xFF) > remaining bytes (2) → raw ASCII decode
        raw = bytes([0xFF, 0x41, 0x42])
        result = self.t._decode_command_payload(raw)
        self.assertIsInstance(result, str)

    def test_list_of_ints_with_prefix(self) -> None:
        data = b"test"
        raw = [len(data), *list(data)]
        self.assertEqual(self.t._decode_command_payload(raw), "test")

    def test_list_with_single_string_delegates(self) -> None:
        self.assertEqual(self.t._decode_command_payload(["hello"]), "hello")

    def test_tuple_with_single_string_delegates(self) -> None:
        self.assertEqual(self.t._decode_command_payload(("hello",)), "hello")

    def test_empty_bytes_returns_none(self) -> None:
        self.assertIsNone(self.t._decode_command_payload(b""))

    def test_empty_list_returns_none(self) -> None:
        self.assertIsNone(self.t._decode_command_payload([]))

    def test_none_returns_none(self) -> None:
        self.assertIsNone(self.t._decode_command_payload(None))

    def test_integer_returns_none(self) -> None:
        self.assertIsNone(self.t._decode_command_payload(42))


# ---------------------------------------------------------------------------
# 6. attribute_updated
# ---------------------------------------------------------------------------


class TestAttributeUpdated(unittest.TestCase):
    """Tests for ``attribute_updated``."""

    def setUp(self) -> None:
        self.t = _make_transport()
        self.t._frame_read = MagicMock()
        self.t._loop.create_task = _mock_create_task()
        self.t._ensure_read_cluster_bound = MagicMock()

    def test_valid_frame_delivered_to_protocol(self) -> None:
        frame = "059  I --- 01:000730 --:------ 01:000730 1FC9 000 00"
        self.t.attribute_updated(self.t._attr_id, frame)
        self.t._frame_read.assert_called_once()

    def test_wrong_attr_id_ignored(self) -> None:
        self.t.attribute_updated(self.t._attr_id + 99, "some data")
        self.t._frame_read.assert_not_called()

    def test_non_string_value_ignored(self) -> None:
        self.t.attribute_updated(self.t._attr_id, 42)
        self.t._frame_read.assert_not_called()

    def test_ack_payload_ignored(self) -> None:
        self.t.attribute_updated(self.t._attr_id, "ACK 1/3")
        self.t._frame_read.assert_not_called()

    def test_whitespace_only_ignored(self) -> None:
        self.t.attribute_updated(self.t._attr_id, "   ")
        self.t._frame_read.assert_not_called()

    def test_chunk_schedules_ack_and_buffers(self) -> None:
        self.t.attribute_updated(self.t._attr_id, "1/2|part1")
        # Partial chunk → not yet delivered
        self.t._frame_read.assert_not_called()
        # ACK should have been scheduled on the loop
        self.t._loop.create_task.assert_called()

    def test_chunk_complete_delivers_assembled_frame(self) -> None:
        self.t.attribute_updated(self.t._attr_id, "1/2|hello ")
        self.t._frame_read.assert_not_called()
        self.t.attribute_updated(self.t._attr_id, "2/2|world")
        self.t._frame_read.assert_called_once()

    def test_ensure_read_cluster_bound_called(self) -> None:
        self.t.attribute_updated(self.t._attr_id, "data")
        self.t._ensure_read_cluster_bound.assert_called_once()


# ---------------------------------------------------------------------------
# 7. cluster_command
# ---------------------------------------------------------------------------


class TestClusterCommand(unittest.TestCase):
    """Tests for ``cluster_command``."""

    def setUp(self) -> None:
        self.t = _make_transport()
        self.t._frame_read = MagicMock()
        self.t._loop.create_task = _mock_create_task()

    def test_string_payload_delivered(self) -> None:
        frame = "059  I --- 01:000730 --:------ 01:000730 1FC9 000 00"
        self.t.cluster_command(0, 0, frame)
        self.t._frame_read.assert_called_once()

    def test_ack_payload_not_delivered(self) -> None:
        self.t.cluster_command(0, 0, "ACK 1/3")
        self.t._frame_read.assert_not_called()

    def test_none_payload_ignored(self) -> None:
        self.t.cluster_command(0, 0, None)
        self.t._frame_read.assert_not_called()

    def test_bytes_payload_decoded_and_delivered(self) -> None:
        frame = "059  I --- 01:000730 --:------ 01:000730 1FC9 000 00"
        raw = bytes([len(frame)]) + frame.encode()
        self.t.cluster_command(0, 0, raw)
        self.t._frame_read.assert_called_once()

    def test_partial_chunk_buffers_and_schedules_ack(self) -> None:
        self.t.cluster_command(0, 0, "1/3|part1")
        self.t._frame_read.assert_not_called()
        self.t._loop.create_task.assert_called()

    def test_complete_chunked_sequence_delivered(self) -> None:
        self.t.cluster_command(0, 0, "1/2|hello ")
        self.t._frame_read.assert_not_called()
        self.t.cluster_command(0, 0, "2/2|world")
        self.t._frame_read.assert_called_once()

    def test_empty_decoded_payload_ignored(self) -> None:
        self.t.cluster_command(0, 0, [])
        self.t._frame_read.assert_not_called()


# ---------------------------------------------------------------------------
# 8. _get_cluster
# ---------------------------------------------------------------------------


class TestGetCluster(unittest.TestCase):
    """Tests for ``_get_cluster``."""

    def setUp(self) -> None:
        self.t = _make_transport()

    # --- via async_get_cluster API ---

    def test_async_get_cluster_returns_cluster(self) -> None:
        mock_cluster = MagicMock()
        device = MagicMock()
        device.async_get_cluster.return_value = mock_cluster
        result = self.t._get_cluster(device, 1, 0xFC00, "out")
        self.assertIs(result, mock_cluster)

    def test_async_get_cluster_returns_none_raises(self) -> None:
        device = MagicMock()
        device.async_get_cluster.return_value = None
        with self.assertRaises(exc.TransportError):
            self.t._get_cluster(device, 1, 0xFC00, "out")

    def test_async_get_cluster_key_error_raises_transport_error(self) -> None:
        device = MagicMock()
        device.async_get_cluster.side_effect = KeyError("not found")
        with self.assertRaises(exc.TransportError):
            self.t._get_cluster(device, 1, 0xFC00, "out")

    # --- via endpoints map ---

    def _device_with_clusters(
        self, ep_id: int, cluster_id: int, direction: str, cluster: Any
    ) -> MagicMock:
        endpoint = MagicMock()
        if direction == "in":
            endpoint.in_clusters = {cluster_id: cluster}
            endpoint.out_clusters = {}
        else:
            endpoint.out_clusters = {cluster_id: cluster}
            endpoint.in_clusters = {}
        device = MagicMock(spec=["endpoints"])
        device.endpoints = {ep_id: endpoint}
        return device

    def test_endpoints_map_in_cluster(self) -> None:
        mock_cluster = MagicMock()
        device = self._device_with_clusters(1, 0xFC00, "in", mock_cluster)
        result = self.t._get_cluster(device, 1, 0xFC00, "in")
        self.assertIs(result, mock_cluster)

    def test_endpoints_map_out_cluster(self) -> None:
        mock_cluster = MagicMock()
        device = self._device_with_clusters(1, 0xFC00, "out", mock_cluster)
        result = self.t._get_cluster(device, 1, 0xFC00, "out")
        self.assertIs(result, mock_cluster)

    def test_missing_endpoint_raises(self) -> None:
        device = MagicMock(spec=["endpoints"])
        device.endpoints = {}
        with self.assertRaises(exc.TransportError):
            self.t._get_cluster(device, 1, 0xFC00, "in")

    def test_missing_cluster_raises(self) -> None:
        endpoint = MagicMock()
        endpoint.in_clusters = {}
        device = MagicMock(spec=["endpoints"])
        device.endpoints = {1: endpoint}
        with self.assertRaises(exc.TransportError):
            self.t._get_cluster(device, 1, 0xFC00, "in")

    def test_device_without_endpoints_attr_raises(self) -> None:
        device = MagicMock(spec=[])  # no endpoints, no async_get_cluster
        with self.assertRaises(exc.TransportError):
            self.t._get_cluster(device, 1, 0xFC00, "in")


# ---------------------------------------------------------------------------
# 9. _attach_clusters
# ---------------------------------------------------------------------------


class TestAttachClusters(unittest.TestCase):
    """Tests for ``_attach_clusters``."""

    def _mock_device(
        self, read_cluster: Any, write_cluster: Any | None = None
    ) -> MagicMock:
        """Create a mock device that hands back clusters via endpoints map."""
        read_ep = MagicMock()
        read_ep.out_clusters = {0xFC00: read_cluster}  # command-mode reads "out"
        read_ep.in_clusters = {}
        device = MagicMock(spec=["endpoints"])

        if write_cluster is None or write_cluster is read_cluster:
            # Same endpoint / cluster for both read and write
            device.endpoints = {1: read_ep}
        else:
            write_ep = MagicMock()
            write_ep.in_clusters = {0xFC00: write_cluster}
            write_ep.out_clusters = {}
            device.endpoints = {1: read_ep}  # simplified — same ep for test

        return device

    def test_attach_sets_cluster_and_adds_listener(self) -> None:
        t = _make_transport()
        mock_cluster = MagicMock()
        device = self._mock_device(mock_cluster)
        t._attach_clusters(device)
        mock_cluster.add_listener.assert_called_once_with(t)
        self.assertIs(t._cluster, mock_cluster)

    def test_same_cluster_reused_for_write(self) -> None:
        """When read and write clusters share the same ID and endpoint the handle is reused."""
        t = _make_transport()
        mock_cluster = MagicMock()
        device = self._mock_device(mock_cluster)
        t._attach_clusters(device)
        self.assertIs(t._cluster, t._write_cluster)

    def test_old_listener_removed_before_reattach(self) -> None:
        t = _make_transport()
        old_cluster = MagicMock()
        t._cluster = old_cluster  # simulate pre-existing cluster
        mock_cluster = MagicMock()
        device = self._mock_device(mock_cluster)
        t._attach_clusters(device)
        old_cluster.remove_listener.assert_called_once_with(t)


# ---------------------------------------------------------------------------
# 10. close
# ---------------------------------------------------------------------------


class TestClose(unittest.TestCase):
    """Tests for ``close``."""

    def setUp(self) -> None:
        self.t = _make_transport()

    def test_close_removes_cluster_listener(self) -> None:
        mock_cluster = MagicMock()
        self.t._cluster = mock_cluster
        self.t.close()
        mock_cluster.remove_listener.assert_called_once_with(self.t)

    def test_close_calls_device_ready_unsub(self) -> None:
        mock_unsub = MagicMock()
        self.t._device_ready_unsub = mock_unsub
        self.t.close()
        mock_unsub.assert_called_once()

    def test_close_sets_closing_flag(self) -> None:
        self.t.close()
        self.assertTrue(self.t._closing)

    def test_close_idempotent(self) -> None:
        self.t.close()
        self.t.close()  # must not raise

    def test_close_tolerates_cluster_remove_listener_exception(self) -> None:
        mock_cluster = MagicMock()
        mock_cluster.remove_listener.side_effect = RuntimeError("boom")
        self.t._cluster = mock_cluster
        self.t.close()  # must not propagate

    def test_close_tolerates_missing_cluster(self) -> None:
        self.t._cluster = None
        self.t.close()  # must not raise


# ---------------------------------------------------------------------------
# 11. _write_frame  (async)
# ---------------------------------------------------------------------------


class TestWriteFrame(unittest.IsolatedAsyncioTestCase):
    """Tests for ``_write_frame``."""

    async def _make(self) -> ZigbeeTransport:
        t = _make_transport()
        mock_cluster = AsyncMock()
        mock_cluster.client_command = AsyncMock()
        t._cluster = mock_cluster
        t._write_cluster = mock_cluster
        return t

    async def test_empty_payload_no_send(self) -> None:
        t = await self._make()
        await t._write_frame("   ")
        t._write_cluster.client_command.assert_not_called()

    async def test_closing_transport_raises(self) -> None:
        t = await self._make()
        t._closing = True
        with self.assertRaises(exc.TransportError):
            await t._write_frame("some frame")

    async def test_short_frame_sends_exactly_one_command(self) -> None:
        t = await self._make()
        await t._write_frame("short frame")
        self.assertEqual(t._write_cluster.client_command.await_count, 1)

    async def test_long_frame_sends_multiple_commands(self) -> None:
        t = await self._make()
        await t._write_frame("X" * 200)
        self.assertGreater(t._write_cluster.client_command.await_count, 1)

    async def test_write_cluster_not_ready_logs_warning_and_continues(self) -> None:
        """A missing write cluster should not raise but log a warning."""
        t = await self._make()
        t._write_cluster = None
        t._device = None
        # Should complete without raising (failed chunk is warned, not raised)
        await t._write_frame("test frame")


# ---------------------------------------------------------------------------
# 12. _wait_for_gateway  (async)
# ---------------------------------------------------------------------------


class TestWaitForGateway(unittest.IsolatedAsyncioTestCase):
    """Tests for ``_wait_for_gateway``."""

    async def _make(self) -> ZigbeeTransport:
        return _make_transport()

    async def test_gateway_found_on_first_attempt(self) -> None:
        t = await self._make()
        mock_gateway = MagicMock()
        mock_proxy = MagicMock()
        mock_proxy.gateway = mock_gateway
        mock_zha = MagicMock()
        mock_zha.gateway_proxy = mock_proxy
        mock_hass = MagicMock()
        mock_hass.data = {"zha": mock_zha}
        t._hass = mock_hass

        result = await t._wait_for_gateway()
        self.assertIs(result, mock_gateway)

    async def test_gateway_not_found_raises_transport_error(self) -> None:
        t = await self._make()
        mock_hass = MagicMock()
        mock_hass.data = {}
        t._hass = mock_hass
        t._GATEWAY_POLL_ATTEMPTS = 1
        t._GATEWAY_POLL_INTERVAL = 0.001

        with self.assertRaises(exc.TransportError):
            await t._wait_for_gateway()

    async def test_no_hass_raises_transport_error(self) -> None:
        t = await self._make()
        t._hass = None
        t._GATEWAY_POLL_ATTEMPTS = 1
        t._GATEWAY_POLL_INTERVAL = 0.001

        with self.assertRaises(exc.TransportError):
            await t._wait_for_gateway()


# ---------------------------------------------------------------------------
# 13. _send_command  (async)
# ---------------------------------------------------------------------------


class TestSendCommand(unittest.IsolatedAsyncioTestCase):
    """Tests for ``_send_command``."""

    async def _make_with_cluster(self) -> tuple[ZigbeeTransport, AsyncMock]:
        t = _make_transport()
        mock_cluster = AsyncMock()
        mock_cluster.client_command = AsyncMock()
        t._write_cluster = mock_cluster
        return t, mock_cluster

    async def test_client_command_called(self) -> None:
        t, cluster = await self._make_with_cluster()
        await t._send_command("test", 1, 1)
        cluster.client_command.assert_awaited_once()

    async def test_no_cluster_raises(self) -> None:
        t = _make_transport()
        t._write_cluster = None
        t._device = None
        with self.assertRaises(exc.TransportError):
            await t._send_command("test", 1, 1)

    async def test_key_error_falls_back_to_server_command(self) -> None:
        t, cluster = await self._make_with_cluster()
        cluster.client_command.side_effect = KeyError("0x00")
        cluster.server_command = AsyncMock()
        await t._send_command("test", 1, 1)
        cluster.server_command.assert_awaited()

    async def test_all_commands_fail_raises_transport_error(self) -> None:
        t, cluster = await self._make_with_cluster()
        cluster.client_command.side_effect = RuntimeError("fail")
        if hasattr(cluster, "server_command"):
            cluster.server_command = AsyncMock(side_effect=RuntimeError("fail"))
        if hasattr(cluster, "command"):
            cluster.command = AsyncMock(side_effect=RuntimeError("fail"))
        # Should raise TransportError after retries exhausted
        with self.assertRaises(exc.TransportError):
            await t._send_command("test", 1, 1)


# ---------------------------------------------------------------------------
# 14. _send_unacked  (async)
# ---------------------------------------------------------------------------


class TestSendUnacked(unittest.IsolatedAsyncioTestCase):
    """Tests for ``_send_unacked``."""

    async def test_with_target_cluster_uses_command(self) -> None:
        t = _make_transport()
        mock_cluster = AsyncMock()
        mock_cluster.command = AsyncMock()
        await t._send_unacked("ACK 1/3", target_cluster=mock_cluster)
        mock_cluster.command.assert_awaited()
        # An ACK payload should use cmd 0x01
        cmd_arg = mock_cluster.command.call_args[0][0]
        self.assertEqual(cmd_arg, 0x01)

    async def test_non_ack_with_target_cluster_uses_cmd_id(self) -> None:
        t = _make_transport()
        mock_cluster = AsyncMock()
        mock_cluster.command = AsyncMock()
        await t._send_unacked("regular payload", target_cluster=mock_cluster)
        cmd_arg = mock_cluster.command.call_args[0][0]
        self.assertEqual(cmd_arg, t._cmd_id)

    async def test_without_target_cluster_calls_send_command(self) -> None:
        t = _make_transport()
        t._send_command = AsyncMock()
        await t._send_unacked("hello")
        t._send_command.assert_awaited_once()

    async def test_target_cluster_failure_logged_not_raised(self) -> None:
        """Errors in _send_unacked are caught and logged, not propagated."""
        t = _make_transport()
        mock_cluster = AsyncMock()
        mock_cluster.command = AsyncMock(side_effect=RuntimeError("fail"))
        # _send_unacked swallows exceptions
        await t._send_unacked("ACK 1/1", target_cluster=mock_cluster)


# ---------------------------------------------------------------------------
# 15. _ensure_read_cluster_bound & _refresh_write_cluster  (sync logic)
# ---------------------------------------------------------------------------


class TestClusterBinding(unittest.TestCase):
    """Tests for ``_ensure_read_cluster_bound`` and ``_refresh_write_cluster``."""

    def setUp(self) -> None:
        self.t = _make_transport()

    def test_ensure_read_cluster_bound_no_device_returns_silently(self) -> None:
        self.t._device = None
        self.t._ensure_read_cluster_bound()  # must not raise

    def test_ensure_read_cluster_bound_same_cluster_no_change(self) -> None:
        mock_cluster = MagicMock()
        self.t._device = MagicMock()
        self.t._get_cluster = MagicMock(return_value=mock_cluster)
        self.t._cluster = mock_cluster
        self.t._ensure_read_cluster_bound()
        mock_cluster.remove_listener.assert_not_called()

    def test_ensure_read_cluster_bound_updates_on_mismatch(self) -> None:
        old_cluster = MagicMock()
        new_cluster = MagicMock()
        self.t._cluster = old_cluster
        self.t._device = MagicMock()
        self.t._get_cluster = MagicMock(return_value=new_cluster)
        self.t._ensure_read_cluster_bound()
        old_cluster.remove_listener.assert_called_once_with(self.t)
        new_cluster.add_listener.assert_called_once_with(self.t)
        self.assertIs(self.t._cluster, new_cluster)

    def test_refresh_write_cluster_no_device_returns_existing(self) -> None:
        self.t._device = None
        self.t._write_cluster = MagicMock()
        result = self.t._refresh_write_cluster()
        self.assertIs(result, self.t._write_cluster)

    def test_refresh_write_cluster_get_cluster_failure_returns_none(self) -> None:
        self.t._device = MagicMock()
        self.t._get_cluster = MagicMock(side_effect=exc.TransportError("missing"))
        result = self.t._refresh_write_cluster()
        self.assertIsNone(result)

    def test_get_active_write_cluster_returns_existing_without_force(self) -> None:
        mock_cluster = MagicMock()
        self.t._write_cluster = mock_cluster
        result = self.t._get_active_write_cluster(force_refresh=False)
        self.assertIs(result, mock_cluster)

    def test_get_active_write_cluster_force_refresh_calls_refresh(self) -> None:
        new_cluster = MagicMock()
        self.t._device = MagicMock()
        self.t._get_cluster = MagicMock(return_value=new_cluster)
        result = self.t._get_active_write_cluster(force_refresh=True)
        self.assertIs(result, new_cluster)


# ---------------------------------------------------------------------------
# 16. _bind_and_configure  (async)
# ---------------------------------------------------------------------------


class TestBindAndConfigure(unittest.IsolatedAsyncioTestCase):
    """Tests for ``_bind_and_configure``."""

    async def test_no_cluster_raises(self) -> None:
        t = _make_transport()
        t._cluster = None
        with self.assertRaises(exc.TransportError):
            await t._bind_and_configure()

    async def test_command_mode_skips_bind_and_configure(self) -> None:
        t = _make_transport()
        t._use_command_mode = True
        mock_cluster = AsyncMock()
        t._cluster = mock_cluster
        await t._bind_and_configure()
        mock_cluster.bind.assert_not_called()

    async def test_non_command_mode_calls_bind_and_configure_reporting(self) -> None:
        t = _make_transport()
        t._use_command_mode = False
        mock_cluster = AsyncMock()
        mock_cluster.bind = AsyncMock()
        mock_cluster.configure_reporting = AsyncMock()
        t._cluster = mock_cluster
        await t._bind_and_configure()
        mock_cluster.bind.assert_awaited_once()
        mock_cluster.configure_reporting.assert_awaited_once()


# ---------------------------------------------------------------------------
# 17. _async_init  (async)
# ---------------------------------------------------------------------------


class TestAsyncInit(unittest.IsolatedAsyncioTestCase):
    """Tests for ``_async_init``."""

    # sys.modules mock context for zigpy (not a real dependency in the venv)
    _MOCK_MODULES: dict[str, Any] = {}

    @staticmethod
    def _zigpy_patch(mock_ieee: Any) -> dict[str, Any]:
        mock_types = MagicMock()
        mock_types.EUI64.convert.return_value = mock_ieee
        return {"zigpy": MagicMock(), "zigpy.types": mock_types}

    async def test_success_device_in_gateway_devices(self) -> None:
        from unittest.mock import patch

        t = _make_transport()
        mock_ieee = MagicMock()
        mock_device = MagicMock()
        mock_gateway = MagicMock()
        mock_gateway.devices = {mock_ieee: mock_device}
        mock_gateway.application_controller = None

        t._wait_for_gateway = AsyncMock(return_value=mock_gateway)
        t._wait_for_device_ready = AsyncMock()
        t._attach_clusters = MagicMock()
        t._bind_and_configure = AsyncMock()
        t._make_connection = MagicMock()
        t._close = MagicMock()

        with patch.dict("sys.modules", self._zigpy_patch(mock_ieee)):
            await t._async_init()

        t._make_connection.assert_called_once()
        t._close.assert_not_called()
        self.assertIs(t._device, mock_device)
        self.assertIs(t._zha_gateway, mock_gateway)

    async def test_device_found_via_application_controller(self) -> None:
        from unittest.mock import patch

        t = _make_transport()
        mock_ieee = MagicMock()
        mock_device = MagicMock()
        mock_controller = MagicMock()
        mock_controller.devices = {mock_ieee: mock_device}
        mock_gateway = MagicMock()
        mock_gateway.devices = {}
        mock_gateway.application_controller = mock_controller

        t._wait_for_gateway = AsyncMock(return_value=mock_gateway)
        t._wait_for_device_ready = AsyncMock()
        t._attach_clusters = MagicMock()
        t._bind_and_configure = AsyncMock()
        t._make_connection = MagicMock()
        t._close = MagicMock()

        with patch.dict("sys.modules", self._zigpy_patch(mock_ieee)):
            await t._async_init()

        t._make_connection.assert_called_once()
        self.assertIs(t._device, mock_device)

    async def test_device_not_found_calls_close(self) -> None:
        from unittest.mock import patch

        t = _make_transport()
        mock_ieee = MagicMock()
        mock_gateway = MagicMock()
        mock_gateway.devices = {}
        mock_gateway.application_controller = None

        t._wait_for_gateway = AsyncMock(return_value=mock_gateway)
        t._close = MagicMock()

        with patch.dict("sys.modules", self._zigpy_patch(mock_ieee)):
            await t._async_init()

        t._close.assert_called_once()

    async def test_no_hass_calls_close(self) -> None:
        from unittest.mock import patch

        t = _make_transport(app_context=None)  # no hass
        t._close = MagicMock()

        with patch.dict(
            "sys.modules", {"zigpy": MagicMock(), "zigpy.types": MagicMock()}
        ):
            await t._async_init()

        t._close.assert_called_once()

    async def test_exception_in_wait_for_gateway_calls_close(self) -> None:
        from unittest.mock import patch

        t = _make_transport()
        t._wait_for_gateway = AsyncMock(side_effect=RuntimeError("unexpected"))
        t._close = MagicMock()

        with patch.dict(
            "sys.modules", {"zigpy": MagicMock(), "zigpy.types": MagicMock()}
        ):
            await t._async_init()

        t._close.assert_called_once()


# ---------------------------------------------------------------------------
# 18. _wait_for_device_ready  (async)
# ---------------------------------------------------------------------------


class TestWaitForDeviceReady(unittest.IsolatedAsyncioTestCase):
    """Tests for ``_wait_for_device_ready``."""

    async def test_initialized_device_returns_immediately(self) -> None:
        t = _make_transport()
        device = MagicMock()
        device.is_initialized = True
        await t._wait_for_device_ready(device, _IEEE)  # must not raise

    async def test_uninitialized_device_waits_for_signal(self) -> None:
        from unittest.mock import patch

        t = _make_transport()
        t._hass = MagicMock()
        device = MagicMock()
        device.is_initialized = False

        # fake async_dispatcher_connect that fires the callback immediately
        def _fake_connect(hass: Any, signal: Any, callback: Any) -> MagicMock:
            callback()
            return MagicMock()

        mock_dispatcher = MagicMock()
        mock_dispatcher.async_dispatcher_connect.side_effect = _fake_connect

        with patch.dict(
            "sys.modules",
            {
                "homeassistant": MagicMock(),
                "homeassistant.helpers": MagicMock(),
                "homeassistant.helpers.dispatcher": mock_dispatcher,
            },
        ):
            await t._wait_for_device_ready(device, _IEEE)

        mock_dispatcher.async_dispatcher_connect.assert_called_once()


# ---------------------------------------------------------------------------
# 19. _attach_clusters fallback paths
# ---------------------------------------------------------------------------


_DIFF_WRITE_URL = f"zigbee://{_IEEE}/0xFC00/0x0000/1/0xFC01/0x0001/2"


class TestAttachClustersFallback(unittest.TestCase):
    """Fallback-search tests for ``_attach_clusters``."""

    def _device_no_cluster_on_ep1_cluster_on_ep2(self) -> tuple[MagicMock, MagicMock]:
        mock_cluster = MagicMock()
        ep1 = MagicMock()
        ep1.in_clusters = {}
        ep1.out_clusters = {}
        ep2 = MagicMock()
        ep2.in_clusters = {0xFC00: mock_cluster}
        ep2.out_clusters = {}
        device = MagicMock(spec=["endpoints"])
        device.endpoints = {1: ep1, 2: ep2}
        return device, mock_cluster

    def test_read_cluster_fallback_finds_on_different_endpoint(self) -> None:
        t = _make_transport()
        device, mock_cluster = self._device_no_cluster_on_ep1_cluster_on_ep2()
        t._attach_clusters(device)
        self.assertIs(t._cluster, mock_cluster)
        mock_cluster.add_listener.assert_called_once_with(t)

    def test_read_cluster_not_found_anywhere_raises(self) -> None:
        t = _make_transport()
        ep = MagicMock()
        ep.in_clusters = {}
        ep.out_clusters = {}
        device = MagicMock(spec=["endpoints"])
        device.endpoints = {1: ep}
        with self.assertRaises(exc.TransportError):
            t._attach_clusters(device)

    def test_write_cluster_fallback_finds_on_different_endpoint(self) -> None:
        """When write cluster ID differs from read cluster, the fallback is searched."""
        t = _make_transport(_DIFF_WRITE_URL)

        read_cluster = MagicMock()
        write_cluster = MagicMock()

        ep1 = MagicMock()
        ep1.out_clusters = {0xFC00: read_cluster}  # read: out of ep1
        ep1.in_clusters = {}

        ep2 = MagicMock()
        ep2.in_clusters = {0xFC01: write_cluster}  # write: in of ep2
        ep2.out_clusters = {}

        device = MagicMock(spec=["endpoints"])
        device.endpoints = {1: ep1, 2: ep2}

        t._attach_clusters(device)

        self.assertIs(t._cluster, read_cluster)
        self.assertIs(t._write_cluster, write_cluster)

    def test_write_cluster_not_found_raises(self) -> None:
        t = _make_transport(_DIFF_WRITE_URL)

        read_cluster = MagicMock()
        ep1 = MagicMock()
        ep1.out_clusters = {0xFC00: read_cluster}
        ep1.in_clusters = {}

        device = MagicMock(spec=["endpoints"])
        device.endpoints = {1: ep1}  # no ep2 → write cluster 0xFC01 not found

        with self.assertRaises(exc.TransportError):
            t._attach_clusters(device)


# ---------------------------------------------------------------------------
# 20. _write_frame in attribute-write mode  (async)
# ---------------------------------------------------------------------------


class TestWriteFrameAttributeMode(unittest.IsolatedAsyncioTestCase):
    """Tests for ``_write_frame`` when *_use_command_mode* is False."""

    async def _make(self) -> ZigbeeTransport:
        t = _make_transport()
        t._use_command_mode = False
        t._send_chunk = AsyncMock()
        return t

    async def test_short_frame_single_send_chunk(self) -> None:
        t = await self._make()
        await t._write_frame("short frame")
        t._send_chunk.assert_awaited_once()

    async def test_long_frame_multiple_send_chunks(self) -> None:
        t = await self._make()
        await t._write_frame("X" * 200)
        self.assertGreater(t._send_chunk.await_count, 1)

    async def test_send_chunk_failure_continues(self) -> None:
        t = await self._make()
        t._send_chunk = AsyncMock(side_effect=RuntimeError("fail"))
        await t._write_frame("a frame")  # must not raise


# ---------------------------------------------------------------------------
# 21. _send_chunk  (async)
# ---------------------------------------------------------------------------


class TestSendChunk(unittest.IsolatedAsyncioTestCase):
    """Tests for ``_send_chunk``."""

    async def test_success_via_write_attributes(self) -> None:
        from unittest.mock import patch

        t = _make_transport()
        mock_cluster = AsyncMock()
        mock_cluster.write_attributes = AsyncMock()
        t._write_cluster = mock_cluster

        mock_zigpy = MagicMock()
        mock_zigpy.types.CharacterString.return_value = MagicMock()

        with patch.dict("sys.modules", {"zigpy": mock_zigpy}):
            await t._send_chunk("payload", 1, 1)

        mock_cluster.write_attributes.assert_awaited_once()

    async def test_no_cluster_raises(self) -> None:
        from unittest.mock import patch

        t = _make_transport()
        t._write_cluster = None
        t._device = None

        with (
            patch.dict("sys.modules", {"zigpy": MagicMock()}),
            self.assertRaises(exc.TransportError),
        ):
            await t._send_chunk("payload", 1, 1)

    async def test_write_attributes_fails_raises_transport_error(self) -> None:
        from unittest.mock import patch

        t = _make_transport()
        mock_cluster = AsyncMock()
        mock_cluster.write_attributes = AsyncMock(side_effect=RuntimeError("IO"))
        t._write_cluster = mock_cluster
        t._device = None  # prevent write-cluster refresh on retry

        mock_zigpy = MagicMock()
        mock_zigpy.types.CharacterString.return_value = MagicMock()

        with (
            patch.dict("sys.modules", {"zigpy": mock_zigpy}),
            self.assertRaises(exc.TransportError),
        ):
            await t._send_chunk("payload", 1, 1)


# ---------------------------------------------------------------------------
# 22. Edge cases: invalid chunk header triggers ACK scheduling
# ---------------------------------------------------------------------------


class TestAttributeUpdatedEdgeCases(unittest.TestCase):
    """Edge cases for ``attribute_updated``."""

    def setUp(self) -> None:
        self.t = _make_transport()
        self.t._frame_read = MagicMock()
        self.t._loop.create_task = _mock_create_task()
        self.t._ensure_read_cluster_bound = MagicMock()

    def test_invalid_chunk_header_seq_gt_total_delivered_and_ack_scheduled(
        self,
    ) -> None:
        """Payload '5/3|body': _parse_chunk returns None → _maybe_handle returns False
        → _frame_read is called → ACK scheduling regex still matches → ACK scheduled."""
        self.t.attribute_updated(self.t._attr_id, "5/3|body")
        self.t._frame_read.assert_called_once()
        self.t._loop.create_task.assert_called()

    def test_maybe_handle_raises_exception_fallthrough(self) -> None:
        """If _maybe_handle_incoming_chunk raises, the exception is caught and
        the payload is forwarded to _frame_read."""
        self.t._maybe_handle_incoming_chunk = MagicMock(
            side_effect=RuntimeError("boom")
        )
        self.t.attribute_updated(self.t._attr_id, "plain frame")
        self.t._frame_read.assert_called_once()


class TestClusterCommandEdgeCases(unittest.TestCase):
    """Edge cases for ``cluster_command``."""

    def setUp(self) -> None:
        self.t = _make_transport()
        self.t._frame_read = MagicMock()
        self.t._loop.create_task = _mock_create_task()

    def test_invalid_chunk_header_delivered_and_ack_scheduled(self) -> None:
        self.t.cluster_command(0, 0, "5/3|body")
        self.t._frame_read.assert_called_once()
        self.t._loop.create_task.assert_called()

    def test_maybe_handle_raises_exception_fallthrough(self) -> None:
        self.t._maybe_handle_incoming_chunk = MagicMock(
            side_effect=RuntimeError("boom")
        )
        self.t.cluster_command(0, 0, "plain text")
        self.t._frame_read.assert_called_once()


# ---------------------------------------------------------------------------
# 23. _maybe_handle_incoming_chunk: error delivery path
# ---------------------------------------------------------------------------


class TestMaybeHandleChunkErrorDelivery(unittest.TestCase):
    """Test that exceptions during assembled-chunk delivery are caught."""

    def setUp(self) -> None:
        self.t = _make_transport()
        self.t._loop.create_task = _mock_create_task()

    def test_frame_read_exception_during_assembly_does_not_propagate(self) -> None:
        self.t._frame_read = MagicMock(side_effect=RuntimeError("protocol error"))
        # 1/1 triggers immediate delivery
        result = self.t._maybe_handle_incoming_chunk("1/1|hello")
        self.assertTrue(result)  # must still return True (chunk was handled)


if __name__ == "__main__":
    unittest.main()
