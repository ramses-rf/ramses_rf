#!/usr/bin/env python3
"""RAMSES RF - Zigbee transport for ESP32 Zigbee devices."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import parse_qs, urlparse

from .. import exceptions as exc
from ..const import SZ_ACTIVE_HGI, SZ_IS_EVOFW3
from ..helpers import dt_now
from .base import TransportConfig, _FullTransport
from .helpers import _normalise

if TYPE_CHECKING:
    from ..protocol import RamsesProtocolT

_LOGGER = logging.getLogger(__name__)


class _ZigbeeTransportAbstractor:
    """Do the bare minimum to abstract a transport from its underlying Zigbee class."""

    def __init__(
        self,
        zigbee_url: str,
        protocol: RamsesProtocolT,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialize the Zigbee transport abstractor.

        :param zigbee_url: The Zigbee URL (zigbee://ieee/cluster/attr/endpoint).
        :type zigbee_url: str
        :param protocol: The protocol instance.
        :type protocol: RamsesProtocolT
        :param loop: The asyncio event loop, defaults to None.
        :type loop: asyncio.AbstractEventLoop | None, optional
        """
        self._zigbee_url = urlparse(zigbee_url)
        self._protocol = protocol
        self._loop = loop or asyncio.get_event_loop()
        self._hass = None
        self._cluster = None
        self._write_cluster = None


class ZigbeeTransport(_FullTransport, _ZigbeeTransportAbstractor):
    """Send/receive packets to/from ESP32 Zigbee device.

    Zigbee URL format: zigbee://ieee/cluster/attr/endpoint/write_cluster/write_attr/write_endpoint
    """

    _GATEWAY_POLL_INTERVAL: Final[float] = 1.0
    _GATEWAY_POLL_ATTEMPTS: Final[int] = 30
    _DEVICE_READY_TIMEOUT: Final[float] = 60.0
    _MAX_CHAR_STRING_LEN: Final[int] = 63
    _CHUNK_BODY_LEN: Final[int] = (
        32  # Reduced to prevent APS fragmentation & buffer exhaustion
    )
    _MAX_CHAR_STRING_LEN_CMD: Final[int] = 63
    _CHUNK_BODY_LEN_CMD: Final[int] = (
        32  # Reduced to prevent APS fragmentation & buffer exhaustion
    )

    def __init__(
        self,
        zigbee_url: str,
        protocol: RamsesProtocolT,
        *,
        config: TransportConfig,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        # _FullTransport and _ReadTransport break the cooperative MRO chain by
        # calling their parent classes directly rather than via super().  We
        # therefore initialise both halves of the diamond explicitly.
        _ZigbeeTransportAbstractor.__init__(self, zigbee_url, protocol, loop=loop)
        _FullTransport.__init__(self, config=config, loop=loop)

        self._ieee = self._zigbee_url.netloc
        path_parts = [p for p in self._zigbee_url.path.strip("/").split("/") if p]

        if not self._ieee or len(path_parts) < 6:
            raise exc.TransportSourceInvalid(
                "Invalid Zigbee URL format. Expected zigbee://ieee/cluster/attr/endpoint/write_cluster/write_attr/write_endpoint"
            )

        self._cluster_id = int(
            path_parts[0], 16 if path_parts[0].startswith("0x") else 10
        )
        self._attr_id = int(path_parts[1], 16 if path_parts[1].startswith("0x") else 10)
        self._endpoint_id = int(float(path_parts[2]))
        self._write_cluster_id = int(
            path_parts[3], 16 if path_parts[3].startswith("0x") else 10
        )
        self._write_attr_id = int(
            path_parts[4], 16 if path_parts[4].startswith("0x") else 10
        )
        self._write_endpoint_id = int(float(path_parts[5]))

        query = parse_qs(self._zigbee_url.query)
        cmd = query.get("cmd", ["0x00"])[0] or "0x00"
        # For this deployment we use custom ZCL commands for all payloads
        # (ESP <-> HA uses commands only). Force command mode regardless of
        # URL query; this removes the attribute-path fallback and keeps
        # handling simple and consistent.
        self._use_command_mode = True
        self._cmd_id = int(cmd, 16 if cmd.startswith("0x") else 10)
        # For custom commands, we listen on client-side cluster where Zigbee stack
        # delivers incoming commands from the ESP's client cluster
        self._read_direction = "out" if self._use_command_mode else "in"
        self._write_direction = "in"
        self._max_char_len = (
            self._MAX_CHAR_STRING_LEN_CMD
            if self._use_command_mode
            else self._MAX_CHAR_STRING_LEN
        )
        self._chunk_body_len = self._CHUNK_BODY_LEN_CMD

        self._extra[SZ_IS_EVOFW3] = True
        self._hass = config.app_context
        self._device: Any | None = None
        self._zha_gateway: Any | None = None
        self._cluster: Any | None = None
        self._write_cluster: Any | None = None
        self._device_ready_unsub: Callable[[], None] | None = None

        self._loop.create_task(self._async_init(), name="ZigbeeTransport._async_init()")
        # buffers for assembling incoming chunked messages per device
        self._chunk_buffers: dict[str, dict[str, Any]] = {}

    async def _async_init(self) -> None:
        try:
            from zigpy.types import EUI64

            if not self._hass:
                raise exc.TransportError("Home Assistant instance not available")

            gateway = await self._wait_for_gateway()
            ieee = EUI64.convert(self._ieee)

            device = None
            zha_devices = getattr(gateway, "devices", None)
            if zha_devices and ieee in zha_devices:
                device = zha_devices[ieee]
            elif getattr(gateway, "application_controller", None):
                device = gateway.application_controller.devices.get(ieee)

            if not device:
                raise exc.TransportError(f"Zigbee device {self._ieee} not found")

            self._zha_gateway = gateway
            self._device = device

            await self._wait_for_device_ready(device, ieee)
            self._attach_clusters(device)
            await self._bind_and_configure()

            self._extra[SZ_ACTIVE_HGI] = self._ieee
            self._make_connection(gwy_id=self._ieee)
            _LOGGER.info(
                "Zigbee transport ready: ieee=%s cluster=0x%04x attr=0x%04x",
                self._ieee,
                self._cluster_id,
                self._attr_id,
            )
        except Exception as err:
            _LOGGER.exception("Failed to initialize Zigbee transport: %s", err)
            self._close(exc.TransportError(str(err)))

    def attribute_updated(self, attrid: int, value: Any) -> None:
        self._ensure_read_cluster_bound()
        if attrid != self._attr_id or not isinstance(value, str):
            return

        payload = value.strip()
        if not payload:
            return
        # Fast-path: ignore application ACKs here so they are not treated
        # as normal RAMSES frames by the parser.
        if payload.startswith("ACK "):
            return

        # If this is a chunk header, assemble; otherwise pass through
        try:
            if self._maybe_handle_incoming_chunk(payload):
                return
        except Exception:
            _LOGGER.exception("Error handling incoming chunk")
        self._frame_read(dt_now().isoformat(), _normalise(payload))
        # If this payload looks like a chunk header, schedule an application ACK
        try:
            m = re.match(r"^(\d{1,3})/(\d{1,3})\|", payload)
            if m:
                seq = int(m.group(1))
                total = int(m.group(2))
                ack = f"ACK {seq}/{total}"
                # fire-and-forget ACK send on the cluster that delivered this payload
                _LOGGER.debug("Scheduling application ACK: %s", ack)
                try:
                    target_cluster = self._cluster
                except Exception:
                    target_cluster = None
                self._loop.create_task(
                    self._send_unacked(ack, target_cluster=target_cluster)
                )
        except Exception:
            pass

    def cluster_command(
        self, tsn: int, command_id: int, args: Any, *_args: Any, **_kwargs: Any
    ) -> None:
        # Attempt to decode command payload as a ZCL char-string. Previously
        # we ignored incoming commands unless in explicit command mode; this
        # prevented handling ESP custom-command chunked payloads when the
        # read/write clusters differed (common with Ramses ESP). Decode the
        # payload and only return when decoding yields nothing relevant.
        payload = self._decode_command_payload(args)
        if not payload:
            return

        # Fast-path: ignore incoming application ACKs to avoid feeding them
        # into the RAMSES frame parser (they are control-plane only).
        if isinstance(payload, str) and payload.startswith("ACK "):
            return
        # If chunked, assemble and only call frame_read when complete
        try:
            if self._maybe_handle_incoming_chunk(payload):
                return
        except Exception:
            _LOGGER.exception("Error handling incoming chunk")
        self._frame_read(dt_now().isoformat(), _normalise(payload))
        # If payload looks like a chunk header, schedule an ACK
        try:
            m = re.match(r"^(\d{1,3})/(\d{1,3})\|", payload)
            if m:
                seq = int(m.group(1))
                total = int(m.group(2))
                ack = f"ACK {seq}/{total}"
                _LOGGER.debug("Scheduling application ACK (cmd): %s", ack)
                try:
                    target_cluster = self._cluster
                except Exception:
                    target_cluster = None
                self._loop.create_task(
                    self._send_unacked(ack, target_cluster=target_cluster)
                )
        except Exception:
            pass

    async def _write_frame(self, frame: str) -> None:
        if self._closing:
            raise exc.TransportError("Zigbee transport is closing")

        _LOGGER.debug("Zigbee write requested frame: %s", frame)

        payload = frame.strip()
        if not payload:
            return

        # Manual chunking required - ZCL commands have size limits (~60-80 bytes)
        # before APS fragmentation. Each chunk must fit within ZCL command size.
        if self._use_command_mode:
            chunks = list(self._chunk_payload(payload))
            for seq, total, chunk in chunks:
                try:
                    await self._send_command(chunk, seq, total)
                    # Delay between chunks to prevent ZBOSS buffer pool exhaustion
                    if seq < total:
                        await asyncio.sleep(0.025)
                except Exception as err:
                    _LOGGER.warning(
                        "Zigbee chunk %s/%s failed: %s - continuing", seq, total, err
                    )
            # Real echo will come from ESP via cluster_command callback
            return

        chunks = list(self._chunk_payload(payload))
        for seq, total, chunk in chunks:
            try:
                await self._send_chunk(chunk, seq, total)
                # Delay between chunks to prevent ZBOSS buffer pool exhaustion
                if seq < total:
                    await asyncio.sleep(0.025)
            except Exception as err:
                _LOGGER.warning(
                    "Zigbee chunk %s/%s failed: %s - continuing", seq, total, err
                )

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._cluster:
            with contextlib.suppress(Exception):
                self._cluster.remove_listener(self)
        if self._device_ready_unsub:
            with contextlib.suppress(Exception):
                self._device_ready_unsub()
            self._device_ready_unsub = None
        super().close()

    async def _wait_for_gateway(self) -> Any:
        for _attempt in range(self._GATEWAY_POLL_ATTEMPTS):
            zha_data = self._hass.data.get("zha") if self._hass else None
            gateway_proxy = (
                getattr(zha_data, "gateway_proxy", None) if zha_data else None
            )
            gateway = getattr(gateway_proxy, "gateway", None) if gateway_proxy else None
            if gateway:
                return gateway
            await asyncio.sleep(self._GATEWAY_POLL_INTERVAL)
        raise exc.TransportError("ZHA gateway proxy not found")

    async def _wait_for_device_ready(self, device: Any, ieee: Any) -> None:
        if getattr(device, "is_initialized", True):
            return

        from homeassistant.helpers.dispatcher import async_dispatcher_connect

        ready_event = asyncio.Event()

        def _mark_ready(*_: Any) -> None:
            if not ready_event.is_set():
                ready_event.set()

        signal = f"zha_device_initialized_{ieee}"
        self._device_ready_unsub = async_dispatcher_connect(
            self._hass, signal, _mark_ready
        )

        try:
            await asyncio.wait_for(
                ready_event.wait(), timeout=self._DEVICE_READY_TIMEOUT
            )
        except TimeoutError as err:  # pragma: no cover - defensive
            raise exc.TransportError(
                f"Zigbee device {ieee} did not finish initializing"
            ) from err
        finally:
            if getattr(self, "_device_ready_unsub", None):
                self._device_ready_unsub()
                self._device_ready_unsub = None

    def _parse_chunk(self, payload: str) -> tuple[int, int, str] | None:
        """Parse a chunk header of the form 'seq/total|body'. Returns (seq,total,body) or None."""
        try:
            m = re.match(r"^(\d{1,3})/(\d{1,3})\|(.*)$", payload, re.DOTALL)
            if not m:
                return None
            seq = int(m.group(1))
            total = int(m.group(2))
            body = m.group(3)
            if seq < 1 or total < 1 or seq > total:
                return None
            return (seq, total, body)
        except Exception:
            return None

    def _maybe_handle_incoming_chunk(self, payload: str) -> bool:
        """Handle incoming chunked payloads. If payload is chunked, buffer and
        assemble; call _frame_read when complete. Returns True if chunk handled
        (and original should NOT be passed to _frame_read)."""
        parsed = self._parse_chunk(payload)
        if not parsed:
            return False
        seq, total, body = parsed
        key = str(self._ieee)
        buf = self._chunk_buffers.get(key)
        if not buf or buf.get("total") != total:
            # start new assembly
            buf = {"total": total, "parts": [None] * total, "received": 0}
            self._chunk_buffers[key] = buf

        parts = buf["parts"]
        if parts[seq - 1] is None:
            parts[seq - 1] = body
            buf["received"] += 1
            try:
                ack = f"ACK {seq}/{total}"
                _LOGGER.info("Scheduling application ACK (part): %s", ack)
                try:
                    target_cluster = self._cluster
                except Exception:
                    target_cluster = None
                # fire-and-forget ACK send on the cluster that delivered this payload
                self._loop.create_task(
                    self._send_unacked(ack, target_cluster=target_cluster)
                )
            except Exception:
                _LOGGER.exception("Failed to schedule application ACK")

        if buf["received"] < total:
            # Not complete yet
            return True

        # All parts received; assemble and clear buffer
        assembled = "".join(p if p is not None else "" for p in parts)
        try:
            # deliver assembled payload to frame reader
            self._frame_read(dt_now().isoformat(), _normalise(assembled))
        except Exception as err:
            _LOGGER.exception("Error delivering assembled chunk: %s", err)
        # cleanup
        with contextlib.suppress(Exception):
            del self._chunk_buffers[key]
        return True

    def _get_cluster(
        self, device: Any, endpoint_id: int, cluster_id: int, direction: str = "in"
    ) -> Any:
        getter = getattr(device, "async_get_cluster", None)
        if callable(getter):
            try:
                cluster = getter(endpoint_id, cluster_id, direction)
            except Exception as err:
                # Some ZHA implementations raise KeyError (or other exceptions)
                # when the cluster is not present; normalize to TransportError
                raise exc.TransportError(
                    f"Cluster lookup failed for 0x{cluster_id:04x} on endpoint {endpoint_id}: {err}"
                ) from err
            if cluster is None:
                raise exc.TransportError(
                    f"Cluster 0x{cluster_id:04x} not found on endpoint {endpoint_id}"
                )
            return cluster

        if not hasattr(device, "endpoints"):
            raise exc.TransportError("Zigbee device has no endpoints map")

        endpoint = device.endpoints.get(endpoint_id)
        if endpoint is None:
            raise exc.TransportError(
                f"Endpoint {endpoint_id} not found on Zigbee device {self._ieee}"
            )

        clusters_attr = "in_clusters" if direction == "in" else "out_clusters"
        clusters = getattr(endpoint, clusters_attr, None)
        if clusters is None:
            raise exc.TransportError(
                f"Endpoint {endpoint_id} has no {direction} clusters map"
            )
        cluster = clusters.get(cluster_id)
        if cluster is None:
            raise exc.TransportError(
                f"Cluster 0x{cluster_id:04x} not found on endpoint {endpoint_id}"
            )
        return cluster

    def _attach_clusters(self, device: Any) -> None:
        try:
            read_cluster = self._get_cluster(
                device, self._endpoint_id, self._cluster_id, self._read_direction
            )
        except exc.TransportError:
            # Fallback: search all endpoints and both cluster directions
            # for the requested cluster id, and bind to the first matching
            # endpoint/direction. This helps when the user supplied an
            # endpoint that doesn't expose the custom cluster in the
            # expected direction (in vs out).
            _LOGGER.debug(
                "Read cluster 0x%04x not found on endpoint %s; searching other endpoints/directions",
                self._cluster_id,
                self._endpoint_id,
            )
            # Dump device endpoints and their clusters to help diagnose role/direction mismatches
            try:
                ep_map = {}
                for ep_id, ep_obj in getattr(device, "endpoints", {}).items():
                    try:
                        in_clusters = list(getattr(ep_obj, "in_clusters", {}).keys())
                    except Exception:
                        in_clusters = (
                            list(getattr(ep_obj, "in_clusters", {}).keys())
                            if hasattr(ep_obj, "in_clusters")
                            else []
                        )
                    try:
                        out_clusters = list(getattr(ep_obj, "out_clusters", {}).keys())
                    except Exception:
                        out_clusters = (
                            list(getattr(ep_obj, "out_clusters", {}).keys())
                            if hasattr(ep_obj, "out_clusters")
                            else []
                        )
                    ep_map[int(ep_id)] = {"in": in_clusters, "out": out_clusters}
                _LOGGER.debug("ZHA device endpoints map: %s", ep_map)
            except Exception:
                _LOGGER.exception("Failed to dump device endpoints for debugging")
            found = False
            for ep_id, _ep in getattr(device, "endpoints", {}).items():
                for dir_try in ("in", "out"):
                    try:
                        candidate = self._get_cluster(
                            device, int(ep_id), self._cluster_id, dir_try
                        )
                        _LOGGER.info(
                            "Auto-selected endpoint %s (direction=%s) for read cluster 0x%04x",
                            ep_id,
                            dir_try,
                            self._cluster_id,
                        )
                        self._endpoint_id = int(ep_id)
                        self._read_direction = dir_try
                        read_cluster = candidate
                        found = True
                        break
                    except Exception:
                        continue
                if found:
                    break
            if not found:
                raise

        if (self._write_cluster_id, self._write_endpoint_id) == (
            self._cluster_id,
            self._endpoint_id,
        ):
            # Write cluster is the same as the read cluster - reuse handle
            write_cluster = read_cluster
        else:
            _LOGGER.debug(
                "Write cluster 0x%04x not found on endpoint %s; searching other endpoints/directions",
                self._write_cluster_id,
                self._write_endpoint_id,
            )
            # Dump device endpoints and clusters for debugging
            try:
                ep_map = {}
                for ep_id, ep_obj in getattr(device, "endpoints", {}).items():
                    try:
                        in_clusters = list(getattr(ep_obj, "in_clusters", {}).keys())
                    except Exception:
                        in_clusters = (
                            list(getattr(ep_obj, "in_clusters", {}).keys())
                            if hasattr(ep_obj, "in_clusters")
                            else []
                        )
                    try:
                        out_clusters = list(getattr(ep_obj, "out_clusters", {}).keys())
                    except Exception:
                        out_clusters = (
                            list(getattr(ep_obj, "out_clusters", {}).keys())
                            if hasattr(ep_obj, "out_clusters")
                            else []
                        )
                    ep_map[int(ep_id)] = {"in": in_clusters, "out": out_clusters}
                _LOGGER.debug("ZHA device endpoints map: %s", ep_map)
            except Exception:
                _LOGGER.exception("Failed to dump device endpoints for debugging")
            found = False
            for ep_id, _ep in getattr(device, "endpoints", {}).items():
                for dir_try in ("in", "out"):
                    try:
                        candidate = self._get_cluster(
                            device, int(ep_id), self._write_cluster_id, dir_try
                        )
                        _LOGGER.info(
                            "Auto-selected endpoint %s (direction=%s) for write cluster 0x%04x",
                            ep_id,
                            dir_try,
                            self._write_cluster_id,
                        )
                        self._write_endpoint_id = int(ep_id)
                        self._write_direction = dir_try
                        write_cluster = candidate
                        found = True
                        break
                    except Exception:
                        continue
                if found:
                    break
            if not found:
                raise exc.TransportError(
                    f"Write cluster 0x{self._write_cluster_id:04x} not found on device {self._ieee}"
                )

        if self._cluster and hasattr(self._cluster, "remove_listener"):
            with contextlib.suppress(Exception):
                self._cluster.remove_listener(self)

        self._cluster = read_cluster
        self._write_cluster = write_cluster

        if hasattr(self._cluster, "add_listener"):
            self._cluster.add_listener(self)

    async def _bind_and_configure(self) -> None:
        if not self._cluster:
            raise exc.TransportError("Read cluster handle not available")

        if self._use_command_mode:
            return

        with contextlib.suppress(Exception):
            await self._cluster.bind()

        configure = getattr(self._cluster, "configure_reporting", None)
        if not callable(configure):
            return

        with contextlib.suppress(Exception):
            await configure(self._attr_id, 0, 0xFFFE, None)

    def _refresh_write_cluster(self) -> Any | None:
        if not self._device:
            return self._write_cluster

        try:
            cluster = self._get_cluster(
                self._device,
                self._write_endpoint_id,
                self._write_cluster_id,
                self._write_direction,
            )
        except exc.TransportError:
            return None

        self._write_cluster = cluster
        return cluster

    def _get_active_write_cluster(self, force_refresh: bool = False) -> Any | None:
        if force_refresh or self._write_cluster is None:
            return self._refresh_write_cluster()
        return self._write_cluster

    def _ensure_read_cluster_bound(self) -> None:
        if not self._device:
            return

        try:
            cluster = self._get_cluster(
                self._device, self._endpoint_id, self._cluster_id, self._read_direction
            )
        except exc.TransportError:
            return

        if cluster is self._cluster:
            return

        if self._cluster and hasattr(self._cluster, "remove_listener"):
            with contextlib.suppress(Exception):
                self._cluster.remove_listener(self)

        self._cluster = cluster
        if hasattr(self._cluster, "add_listener"):
            self._cluster.add_listener(self)

    def _chunk_payload(self, payload: str) -> list[tuple[int, int, str]]:
        if len(payload) <= self._max_char_len:
            return [(1, 1, payload)]

        total = math.ceil(len(payload) / self._chunk_body_len)
        chunks: list[tuple[int, int, str]] = []
        for idx in range(total):
            start = idx * self._chunk_body_len
            body = payload[start : start + self._chunk_body_len]
            header = f"{idx + 1}/{total}|"
            allowed = self._max_char_len - len(header)
            if allowed <= 0:
                raise exc.TransportError(
                    "Chunk header exceeds Zigbee char-string limit"
                )
            body = body[:allowed]
            chunk = header + body
            chunks.append((idx + 1, total, chunk))

        return chunks

    def _decode_command_payload(self, args: Any) -> str | None:
        if isinstance(args, str):
            return args

        if isinstance(args, (bytes, bytearray)):
            raw = bytes(args)
        elif isinstance(args, list) and args and all(isinstance(x, int) for x in args):
            raw = bytes(args)
        elif isinstance(args, (list, tuple)) and args:
            return self._decode_command_payload(args[0])
        else:
            return None

        if not raw:
            return None

        # Check if this is a valid ZCL char-string (length prefix + data)
        # where the first byte indicates the string length
        if len(raw) >= 2 and raw[0] > 0 and raw[0] <= len(raw) - 1:
            string_data = raw[1 : 1 + raw[0]]

            # Check if the string data looks like a chunk header (e.g., "1/2|..." or "2/2|...")
            try:
                data_str = string_data.decode("ascii", errors="strict")
                if len(data_str) >= 4 and data_str[0].isdigit():
                    slash_pos = data_str.find("/")
                    if 0 < slash_pos < 3:
                        pipe_pos = data_str.find("|", slash_pos)
                        if slash_pos < pipe_pos < 6:
                            return data_str  # Return chunk as-is
            except (UnicodeDecodeError, AttributeError):
                pass

            return string_data.decode("ascii", errors="ignore")

        return raw.decode("ascii", errors="ignore")

    async def _send_command(
        self, chunk: str, seq: int, total: int, cmd_override: int | None = None
    ) -> None:
        cluster = self._get_active_write_cluster()
        if not cluster:
            raise exc.TransportError("Zigbee write cluster not ready")

        _LOGGER.debug("Zigbee TX %s/%s: %s", seq, total, chunk)

        last_err: Exception | None = None
        # If a command override is requested (e.g., ACK=0x01) and we have
        # an active read cluster (self._cluster), try sending the command on
        # that cluster first. This helps hit the server/client direction
        # mapping that the device expects for ACK responses.
        tried_clusters: list[Any] = []
        candidate_clusters: list[Any] = []
        if cmd_override is not None and getattr(self, "_cluster", None) is not None:
            candidate_clusters.append(self._cluster)
        candidate_clusters.append(cluster)

        for attempt in (1, 2):
            for candidate in candidate_clusters:
                if candidate in tried_clusters:
                    continue
                tried_clusters.append(candidate)
                try:
                    use_cmd = cmd_override if cmd_override is not None else self._cmd_id
                    # Prefer explicit client_command API when available (client->server)
                    if hasattr(candidate, "client_command"):
                        try:
                            await candidate.client_command(
                                use_cmd, chunk, expect_reply=False
                            )
                            return
                        except KeyError as ke:
                            # Missing client command mapping for this id — try server-side command
                            _LOGGER.debug(
                                "client_command KeyError (cmd=0x%02x) on cluster 0x%04x, will try server_command: %s",
                                use_cmd,
                                getattr(candidate, "cluster_id", 0),
                                ke,
                            )
                        except Exception as err:  # pragma: no cover - defensive
                            last_err = err
                            _LOGGER.warning(
                                "Zigbee write cmd %s/%s attempt %s failed (endpoint=%s cluster=0x%04x cmd=0x%02x): %s (%s)",
                                seq,
                                total,
                                attempt,
                                self._write_endpoint_id,
                                self._write_cluster_id,
                                use_cmd,
                                err,
                                type(err).__name__,
                            )

                    # If client_command not available or failed with KeyError, try server_command (server->client)
                    if hasattr(candidate, "server_command"):
                        try:
                            await candidate.server_command(
                                use_cmd, chunk, expect_reply=False
                            )
                            return
                        except Exception as err:  # pragma: no cover - defensive
                            last_err = err
                            _LOGGER.warning(
                                "Zigbee write server cmd %s/%s attempt %s failed (endpoint=%s cluster=0x%04x cmd=0x%02x): %s (%s)",
                                seq,
                                total,
                                attempt,
                                self._write_endpoint_id,
                                self._write_cluster_id,
                                use_cmd,
                                err,
                                type(err).__name__,
                            )

                    # Fallback to generic command API if present
                    if hasattr(candidate, "command"):
                        try:
                            await candidate.command(use_cmd, chunk, expect_reply=False)
                            return
                        except Exception as err:  # pragma: no cover - defensive
                            last_err = err
                            _LOGGER.warning(
                                "Zigbee write generic cmd %s/%s attempt %s failed (endpoint=%s cluster=0x%04x cmd=0x%02x): %s (%s)",
                                seq,
                                total,
                                attempt,
                                self._write_endpoint_id,
                                self._write_cluster_id,
                                use_cmd,
                                err,
                                type(err).__name__,
                            )

                    # If we reach here, nothing succeeded — dump available mappings for debugging
                    try:
                        client_map = getattr(
                            candidate, "client_commands", None
                        ) or getattr(candidate, "client_command_names", None)
                        server_map = getattr(
                            candidate, "server_commands", None
                        ) or getattr(candidate, "server_command_names", None)
                        _LOGGER.debug(
                            "Cluster 0x%04x available commands: client=%r server=%r",
                            getattr(candidate, "cluster_id", 0),
                            client_map,
                            server_map,
                        )
                    except Exception:
                        pass
                    # fall through to outer retry/refresh logic
                except Exception as err:  # pragma: no cover - defensive
                    last_err = err
                    _LOGGER.warning(
                        "Zigbee write cmd %s/%s attempt %s unexpected failure (endpoint=%s cluster=0x%04x cmd=0x%02x): %s (%s)",
                        seq,
                        total,
                        attempt,
                        self._write_endpoint_id,
                        self._write_cluster_id,
                        cmd_override if cmd_override is not None else self._cmd_id,
                        err,
                        type(err).__name__,
                    )
            # refresh and retry once
            if attempt == 1:
                refreshed = self._get_active_write_cluster(force_refresh=True)
                if refreshed and refreshed is not cluster:
                    cluster = refreshed
                    candidate_clusters = [
                        c for c in candidate_clusters if c is not cluster
                    ]
                    candidate_clusters.append(cluster)
                    continue
            break

        if last_err is None:
            raise exc.TransportError("Failed to send Zigbee command")

        raise exc.TransportError("Failed to send Zigbee command") from last_err

    async def _send_chunk(self, chunk: str, seq: int, total: int) -> None:
        cluster = self._get_active_write_cluster()
        if not cluster:
            raise exc.TransportError("Zigbee write cluster not ready")

        _LOGGER.debug(
            "Zigbee write chunk %s/%s (len=%s endpoint=%s cluster=0x%04x): %s",
            seq,
            total,
            len(chunk),
            self._write_endpoint_id,
            self._write_cluster_id,
            chunk,
        )

        last_err: Exception | None = None
        for attempt in (1, 2):
            try:
                from zigpy import types as t

                value = t.CharacterString(chunk)
                await cluster.write_attributes(
                    {self._write_attr_id: value}, manufacturer=None
                )
                return
            except Exception as err:  # pragma: no cover - defensive
                last_err = err
                _LOGGER.warning(
                    "Zigbee write chunk %s/%s attempt %s failed (endpoint=%s cluster=0x%04x): %s",
                    seq,
                    total,
                    attempt,
                    self._write_endpoint_id,
                    self._write_cluster_id,
                    err,
                )
                if attempt == 1:
                    refreshed = self._get_active_write_cluster(force_refresh=True)
                    if refreshed and refreshed is not cluster:
                        cluster = refreshed
                        continue
                break

        if last_err is None:
            raise exc.TransportError("Failed to send Zigbee chunk")

        raise exc.TransportError("Failed to send Zigbee chunk") from last_err

    async def _send_unacked(self, text: str, target_cluster: Any | None = None) -> None:
        """Send a small ZCL payload back to the device without expecting an app-level ACK.

        When `target_cluster` is provided, the send will use that cluster object and
        the command path implied by that cluster (server_command for server->client
        sends, client_command for client->server sends). This avoids probing multiple
        clusters dynamically and enforces deterministic behavior according to the
        quirk definitions.
        """
        try:
            chunks = list(self._chunk_payload(text))
            for seq, total, chunk in chunks:
                # If a target_cluster was provided, send on that cluster deterministically
                if target_cluster is not None:
                    use_cmd = (
                        0x01
                        if isinstance(chunk, str) and chunk.startswith("ACK ")
                        else self._cmd_id
                    )
                    # Use the generic cluster.command API which ZHA cluster objects provide
                    # This respects the cluster role (server/client) under the hood and
                    # avoids relying on presence of `server_command`/`client_command`
                    try:
                        await target_cluster.command(use_cmd, chunk, expect_reply=False)
                    except Exception as err:  # pragma: no cover - defensive
                        raise exc.TransportError(
                            f"Target cluster command failed (cluster=0x{getattr(target_cluster, 'cluster_id', 0):04x} cmd=0x{use_cmd:02x}): {err}"
                        ) from err
                else:
                    # No explicit cluster provided: fall back to the configured write cluster
                    await self._send_command(chunk, seq, total)
                await asyncio.sleep(0.01)
        except Exception as err:  # pragma: no cover - defensive
            _LOGGER.warning("Zigbee unacked send failed: %s", err)
