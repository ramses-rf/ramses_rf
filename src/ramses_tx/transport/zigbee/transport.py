#!/usr/bin/env python3
"""RAMSES RF - Zigbee transport for ESP32 Zigbee devices."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable
from typing import Any, Final
from urllib.parse import parse_qs, urlparse

from ... import exceptions as exc
from ...const import SZ_ACTIVE_HGI, SZ_IS_EVOFW3
from ...helpers import dt_now
from ...typing import DeviceIdT, RamsesProtocolT
from ..base import TransportConfig, _FullTransport
from ..helpers import _normalise
from .connection import ZigbeeConnectionManager
from .framing import ZigbeeFramingHandler

_LOGGER = logging.getLogger(__name__)


def _hgi_id_from_ieee(ieee: str) -> str | None:
    """Derive the ramses_esp gateway ID from a Zigbee IEEE address.

    ramses_esp derives its gateway ID from the last three bytes of the
    base MAC address::

        DevId = (mac[3]<<16 | mac[4]<<8 | mac[5]) & 0x3FFFF

    The device's IEEE (EUI-64) address is the 48-bit base MAC expanded
    by inserting ``ff:fe`` in the middle, so ``mac[3:6] == ieee[5:8]``
    and the derivation is reversible.

    :param ieee: The IEEE address, e.g. ``10:bd:a3:ff:fe:a7:e0:dc``.
    :returns: The HGI device ID (e.g. ``18:254172``), or None if the
        IEEE is not a standard EUI-64 expansion of the base MAC.
    """
    parts = ieee.split(":")
    if len(parts) != 8:
        return None
    try:
        octets = [int(p, 16) for p in parts]
    except ValueError:
        return None
    if octets[3] != 0xFF or octets[4] != 0xFE:
        return None  # not a MAC-with-ff:fe EUI-64 — can't derive safely
    dev_id = ((octets[5] << 16) | (octets[6] << 8) | octets[7]) & 0x3FFFF
    return f"18:{dev_id:06d}"


class _ZigbeeTransportAbstractor:
    """Do the bare minimum to abstract a transport from its underlying Zigbee class."""

    def __init__(
        self,
        zigbee_url: str,
        protocol: RamsesProtocolT,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialise the Zigbee transport abstractor.

        :param zigbee_url: The Zigbee URL (zigbee://ieee/cluster/attr/endpoint).
        :type zigbee_url: str
        :param protocol: The protocol instance.
        :type protocol: RamsesProtocolT
        :param loop: The asyncio event loop, defaults to None.
        :type loop: asyncio.AbstractEventLoop | None
        """
        self._zigbee_url = urlparse(zigbee_url)
        self._protocol = protocol
        self._loop = loop or asyncio.get_event_loop()
        self._hass: Any | None = None


class ZigbeeTransport(_FullTransport, _ZigbeeTransportAbstractor):
    """Send/receive packets to/from ESP32 Zigbee device."""

    _DEVICE_READY_TIMEOUT: Final[float] = 60.0
    _MAX_CHAR_STRING_LEN: Final[int] = 63
    _CHUNK_BODY_LEN: Final[int] = 32
    _MAX_CHAR_STRING_LEN_CMD: Final[int] = 63
    _CHUNK_BODY_LEN_CMD: Final[int] = 32
    _CHUNK_TIMEOUT: Final[float] = ZigbeeFramingHandler._CHUNK_TIMEOUT

    def __init__(
        self,
        zigbee_url: str,
        protocol: RamsesProtocolT,
        *,
        config: TransportConfig,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialise the Zigbee transport.

        :param zigbee_url: The connection URL.
        :type zigbee_url: str
        :param protocol: The active protocol instance.
        :type protocol: RamsesProtocolT
        :param config: Transport configuration object.
        :type config: TransportConfig
        :param loop: The asyncio event loop.
        :type loop: asyncio.AbstractEventLoop | None
        :raises TransportSourceInvalid: If URL is malformed.
        """
        _ZigbeeTransportAbstractor.__init__(
            self, zigbee_url, protocol, loop=loop
        )
        _FullTransport.__init__(self, config=config, loop=loop)

        self._ieee = self._zigbee_url.netloc
        path_parts = [
            p for p in self._zigbee_url.path.strip("/").split("/") if p
        ]

        if not self._ieee or len(path_parts) < 6:
            raise exc.TransportSourceInvalid(
                "Invalid Zigbee URL format. Expected "
                "zigbee://ieee/cluster/attr/endpoint/write_cluster/"
                "write_attr/write_endpoint"
            )

        self._cluster_id = int(
            path_parts[0], 16 if path_parts[0].startswith("0x") else 10
        )
        self._attr_id = int(
            path_parts[1], 16 if path_parts[1].startswith("0x") else 10
        )
        self._endpoint_id = int(float(path_parts[2]))
        self._write_cluster_id = int(
            path_parts[3], 16 if path_parts[3].startswith("0x") else 10
        )
        self._write_attr_id = int(
            path_parts[4], 16 if path_parts[4].startswith("0x") else 10
        )
        self._write_endpoint_id = int(float(path_parts[5]))

        query = parse_qs(self._zigbee_url.query)
        command_str = query.get("cmd", ["0x00"])[0] or "0x00"
        self._use_command_mode = True
        self._cmd_id = int(
            command_str, 16 if command_str.startswith("0x") else 10
        )

        max_char_len = (
            self._MAX_CHAR_STRING_LEN_CMD
            if self._use_command_mode
            else self._MAX_CHAR_STRING_LEN
        )
        chunk_body_len = self._CHUNK_BODY_LEN_CMD

        self._extra[SZ_IS_EVOFW3] = True
        self._hass = config.app_context
        self._tasks: set[asyncio.Task[Any]] = set()

        self._framing = ZigbeeFramingHandler(
            max_char_len=max_char_len,
            chunk_body_len=chunk_body_len,
        )
        self._connection_mgr = ZigbeeConnectionManager(
            hass=self._hass,
            ieee=self._ieee,
            cluster_id=self._cluster_id,
            attr_id=self._attr_id,
            endpoint_id=self._endpoint_id,
            write_cluster_id=self._write_cluster_id,
            write_attr_id=self._write_attr_id,
            write_endpoint_id=self._write_endpoint_id,
            cmd_id=self._cmd_id,
            use_command_mode=self._use_command_mode,
            listener=self,
        )

        self._track_task(
            self._loop.create_task(
                self._async_init(), name="ZigbeeTransport._async_init()"
            )
        )

    @property
    def _hass(self) -> Any | None:
        return (
            self._connection_mgr._hass
            if hasattr(self, "_connection_mgr")
            else self.__dict__.get("_hass")
        )

    @_hass.setter
    def _hass(self, value: Any | None) -> None:
        self.__dict__["_hass"] = value
        if hasattr(self, "_connection_mgr"):
            self._connection_mgr._hass = value

    @property
    def _max_char_len(self) -> int:
        return self._framing._max_char_len

    @_max_char_len.setter
    def _max_char_len(self, value: int) -> None:
        self._framing._max_char_len = value

    @property
    def _chunk_body_len(self) -> int:
        return self._framing._chunk_body_len

    @_chunk_body_len.setter
    def _chunk_body_len(self, value: int) -> None:
        self._framing._chunk_body_len = value

    @property
    def _chunk_buffers(self) -> dict[str, dict[str, Any]]:
        return self._framing._chunk_buffers

    @_chunk_buffers.setter
    def _chunk_buffers(self, value: dict[str, dict[str, Any]]) -> None:
        self._framing._chunk_buffers = value

    @property
    def _device(self) -> Any | None:
        return self._connection_mgr._device

    @_device.setter
    def _device(self, value: Any | None) -> None:
        self._connection_mgr._device = value

    @property
    def _zha_gateway(self) -> Any | None:
        return self._connection_mgr._zha_gateway

    @_zha_gateway.setter
    def _zha_gateway(self, value: Any | None) -> None:
        self._connection_mgr._zha_gateway = value

    @property
    def _cluster(self) -> Any | None:
        return self._connection_mgr._cluster

    @_cluster.setter
    def _cluster(self, value: Any | None) -> None:
        self._connection_mgr._cluster = value

    @property
    def _write_cluster(self) -> Any | None:
        return self._connection_mgr._write_cluster

    @_write_cluster.setter
    def _write_cluster(self, value: Any | None) -> None:
        self._connection_mgr._write_cluster = value

    @property
    def _device_ready_unsub(self) -> Callable[[], None] | None:
        return self._connection_mgr._device_ready_unsub

    @_device_ready_unsub.setter
    def _device_ready_unsub(self, value: Callable[[], None] | None) -> None:
        self._connection_mgr._device_ready_unsub = value

    @property
    def _read_direction(self) -> str:
        return self._connection_mgr._read_direction

    @_read_direction.setter
    def _read_direction(self, value: str) -> None:
        self._connection_mgr._read_direction = value

    @property
    def _write_direction(self) -> str:
        return self._connection_mgr._write_direction

    @_write_direction.setter
    def _write_direction(self, value: str) -> None:
        self._connection_mgr._write_direction = value

    @property
    def _GATEWAY_POLL_ATTEMPTS(self) -> int:
        return self._connection_mgr._GATEWAY_POLL_ATTEMPTS

    @_GATEWAY_POLL_ATTEMPTS.setter
    def _GATEWAY_POLL_ATTEMPTS(self, value: int) -> None:
        self._connection_mgr._GATEWAY_POLL_ATTEMPTS = value

    @property
    def _GATEWAY_POLL_INTERVAL(self) -> float:
        return self._connection_mgr._GATEWAY_POLL_INTERVAL

    @_GATEWAY_POLL_INTERVAL.setter
    def _GATEWAY_POLL_INTERVAL(self, value: float) -> None:
        self._connection_mgr._GATEWAY_POLL_INTERVAL = value

    def _cleanup_chunk_buffers(self) -> None:
        self._framing.cleanup_chunk_buffers()

    def _parse_chunk(self, payload: str) -> tuple[int, int, str] | None:
        return self._framing.parse_chunk(payload)

    def _chunk_payload(self, payload: str) -> list[tuple[int, int, str]]:
        return self._framing.chunk_payload(payload)

    def _decode_command_payload(self, args: Any) -> str | None:
        return self._framing.decode_command_payload(args)

    def _maybe_handle_incoming_chunk(self, payload: str) -> bool:
        self._cleanup_chunk_buffers()

        parsed = self._parse_chunk(payload)
        if not parsed:
            return False

        sequence_number, total, body = parsed
        key = str(self._ieee)
        chunk_buffer = self._chunk_buffers.get(key)

        if not chunk_buffer or chunk_buffer.get("total") != total:
            chunk_buffer = {
                "total": total,
                "parts": [None] * total,
                "received": 0,
                "timestamp": dt_now(),
            }
            self._chunk_buffers[key] = chunk_buffer
        else:
            chunk_buffer["timestamp"] = dt_now()

        parts = chunk_buffer["parts"]
        if parts[sequence_number - 1] is None:
            parts[sequence_number - 1] = body
            chunk_buffer["received"] += 1
            try:
                ack = f"ACK {sequence_number}/{total}"
                _LOGGER.info("Scheduling application ACK (part): %s", ack)
                target_cluster = getattr(self, "_cluster", None)
                self._track_task(
                    self._loop.create_task(
                        self._send_unacked(ack, target_cluster=target_cluster)
                    )
                )
            except asyncio.CancelledError:
                raise
            except (
                KeyError,
                ValueError,
                TypeError,
                AttributeError,
                RuntimeError,
                exc.RamsesException,
            ) as err:
                _LOGGER.exception(
                    "Failed to schedule application ACK: %s", err
                )

        if chunk_buffer["received"] < total:
            return True

        assembled = "".join(p if p is not None else "" for p in parts)
        try:
            self._frame_read(dt_now().isoformat(), _normalise(assembled))
        except asyncio.CancelledError:
            raise
        except (
            KeyError,
            ValueError,
            TypeError,
            AttributeError,
            RuntimeError,
            exc.RamsesException,
        ) as err:
            _LOGGER.exception("Error delivering assembled chunk: %s", err)

        with contextlib.suppress(KeyError):
            del self._chunk_buffers[key]

        return True

    async def _wait_for_gateway(self) -> Any:
        return await self._connection_mgr.wait_for_gateway()

    async def _wait_for_device_ready(self, device: Any, ieee: Any) -> None:
        await self._connection_mgr.wait_for_device_ready(device, ieee)

    def _resolve_hgi_id(self) -> str:
        """Resolve the RAMSES HGI device ID for this gateway.

        The IEEE address is NOT a RAMSES HGI ID and must never be used
        as one — it would cause device_id filter exceptions and
        incorrect outbound packet patching. Precedence:

        1. ``configured_hgi_id`` (explicit override) wins.
        2. Derive from the IEEE: ramses_esp derives its gateway ID from
           the last three bytes of the base MAC, and the IEEE address is
           the base MAC expanded to EUI-64 via an ``ff:fe`` insertion —
           so the derivation is reversible (see ``_hgi_id_from_ieee``).
        3. Fall back to the unknown-HGI sentinel (``18:000730``); the
           child is then receive-only until RF traffic reveals the real
           ID.
        """
        from ...address import HGI_DEV_ADDR

        return (
            self._configured_hgi_id
            or _hgi_id_from_ieee(self._ieee)
            or HGI_DEV_ADDR.id
        )

    def _get_cluster(
        self,
        device: Any,
        endpoint_id: int,
        cluster_id: int,
        direction: str = "in",
    ) -> Any:
        return self._connection_mgr.get_cluster(
            device, endpoint_id, cluster_id, direction
        )

    def _attach_clusters(self, device: Any) -> None:
        try:
            read_cluster = self._get_cluster(
                device,
                self._endpoint_id,
                self._cluster_id,
                self._read_direction,
            )
        except exc.TransportZigbeeError:
            _LOGGER.debug(
                "Read cluster 0x%04x not found on endpoint %s; searching other endpoints/directions",
                self._cluster_id,
                self._endpoint_id,
            )
            found = False
            for ep_id in getattr(device, "endpoints", {}):
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
                    except exc.TransportZigbeeError:
                        continue
                if found:
                    break
            if not found:
                raise

        if (self._write_cluster_id, self._write_endpoint_id) == (
            self._cluster_id,
            self._endpoint_id,
        ):
            write_cluster = read_cluster
        else:
            _LOGGER.debug(
                "Write cluster 0x%04x not found on endpoint %s; searching other endpoints/directions",
                self._write_cluster_id,
                self._write_endpoint_id,
            )
            found = False
            for ep_id in getattr(device, "endpoints", {}):
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
                    except exc.TransportZigbeeError:
                        continue
                if found:
                    break
            if not found:
                raise exc.TransportZigbeeError(
                    f"Write cluster 0x{self._write_cluster_id:04x} not found on device {self._ieee}"
                )

        cluster = getattr(self, "_cluster", None)
        if cluster is not None:
            try:
                cluster.remove_listener(self)
            except Exception as err:
                _LOGGER.exception("Failed to remove listener: %s", err)

        self._cluster = read_cluster
        self._write_cluster = write_cluster

        if hasattr(self._cluster, "add_listener"):
            self._cluster.add_listener(self)

    async def _bind_and_configure(self) -> None:
        if not self._cluster:
            raise exc.TransportZigbeeError("Read cluster handle not available")

        if self._use_command_mode:
            return

        try:
            await self._cluster.bind()
        except (TimeoutError, ValueError, AttributeError) as err:
            _LOGGER.exception("Failed to bind cluster: %s", err)

        configure = getattr(self._cluster, "configure_reporting", None)
        if callable(configure):
            try:
                await configure(self._attr_id, 0, 0xFFFE, None)
            except (TimeoutError, ValueError, AttributeError) as err:
                _LOGGER.exception("Failed to configure reporting: %s", err)

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
        except exc.TransportZigbeeError:
            return None

        self._write_cluster = cluster
        return cluster

    def _get_active_write_cluster(
        self, force_refresh: bool = False
    ) -> Any | None:
        if force_refresh or self._write_cluster is None:
            return self._refresh_write_cluster()
        return self._write_cluster

    def _ensure_read_cluster_bound(self) -> None:
        if not self._device:
            return

        try:
            cluster = self._get_cluster(
                self._device,
                self._endpoint_id,
                self._cluster_id,
                self._read_direction,
            )
        except exc.TransportZigbeeError:
            return

        if cluster is self._cluster:
            return

        old_cluster = getattr(self, "_cluster", None)
        if old_cluster is not None:
            try:
                old_cluster.remove_listener(self)
            except Exception as err:
                _LOGGER.exception("Failed to remove listener: %s", err)

        self._cluster = cluster
        if hasattr(self._cluster, "add_listener"):
            self._cluster.add_listener(self)

    async def _send_command(
        self,
        chunk: str,
        sequence_number: int,
        total: int,
        command_override: int | None = None,
    ) -> None:
        cluster = self._get_active_write_cluster()
        if not cluster:
            raise exc.TransportZigbeeError("Zigbee write cluster not ready")

        _LOGGER.debug("Zigbee TX %s/%s: %s", sequence_number, total, chunk)
        last_err: Exception | None = None

        tried_clusters: list[Any] = []
        candidate_clusters: list[Any] = []

        if (
            command_override is not None
            and getattr(self, "_cluster", None) is not None
        ):
            candidate_clusters.append(self._cluster)
        candidate_clusters.append(cluster)

        for attempt in (1, 2):
            for candidate in candidate_clusters:
                if candidate in tried_clusters:
                    continue
                tried_clusters.append(candidate)
                try:
                    use_cmd = (
                        command_override
                        if command_override is not None
                        else self._cmd_id
                    )

                    if hasattr(candidate, "client_command"):
                        try:
                            await candidate.client_command(
                                use_cmd, chunk, expect_reply=False
                            )
                            return
                        except KeyError as ke:
                            _LOGGER.debug(
                                "client_command KeyError on 0x%04x: %s",
                                getattr(candidate, "cluster_id", 0),
                                ke,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as err:
                            last_err = err

                    if hasattr(candidate, "server_command"):
                        try:
                            await candidate.server_command(
                                use_cmd, chunk, expect_reply=False
                            )
                            return
                        except asyncio.CancelledError:
                            raise
                        except Exception as err:
                            last_err = err

                    if hasattr(candidate, "command"):
                        try:
                            await candidate.command(
                                use_cmd, chunk, expect_reply=False
                            )
                            return
                        except asyncio.CancelledError:
                            raise
                        except Exception as err:
                            last_err = err
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    last_err = err

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
            raise exc.TransportZigbeeError("Failed to send Zigbee command")
        raise exc.TransportZigbeeError(
            "Failed to send Zigbee command"
        ) from last_err

    async def _send_chunk(
        self, chunk: str, sequence_number: int, total: int
    ) -> None:
        cluster = self._get_active_write_cluster()
        if not cluster:
            raise exc.TransportZigbeeError("Zigbee write cluster not ready")

        _LOGGER.debug(
            "Zigbee write chunk %s/%s (len=%s endpoint=%s cluster=0x%04x): %s",
            sequence_number,
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
            except asyncio.CancelledError:
                raise
            except Exception as err:
                last_err = err
                if attempt == 1:
                    refreshed = self._get_active_write_cluster(
                        force_refresh=True
                    )
                    if refreshed and refreshed is not cluster:
                        cluster = refreshed
                        continue
                break

        if last_err is None:
            raise exc.TransportZigbeeError("Failed to send Zigbee chunk")
        raise exc.TransportZigbeeError(
            "Failed to send Zigbee chunk"
        ) from last_err

    async def _send_unacked(
        self, text: str, target_cluster: Any | None = None
    ) -> None:
        try:
            chunks = list(self._chunk_payload(text))
            for sequence_number, total, chunk in chunks:
                if target_cluster is not None:
                    use_cmd = (
                        0x01
                        if isinstance(chunk, str) and chunk.startswith("ACK ")
                        else self._cmd_id
                    )
                    try:
                        await target_cluster.command(
                            use_cmd, chunk, expect_reply=False
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as err:
                        _LOGGER.exception(
                            "Target cluster command failed: %s", err
                        )
                        raise exc.TransportZigbeeError(
                            f"Target cluster command failed: {err}"
                        ) from err
                else:
                    await self._send_command(chunk, sequence_number, total)
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.exception("Zigbee unacked send failed: %s", err)

    def _track_task(self, task: asyncio.Task[Any]) -> None:
        """Add a task to the registry to prevent garbage collection."""
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _async_init(self) -> None:
        """Initialise ZHA dependencies and wait for gateway connection."""
        try:
            from zigpy.types import EUI64

            if not self._hass:
                raise exc.TransportZigbeeError(
                    "Home Assistant instance not available"
                )

            gateway = await self._wait_for_gateway()
            self._zha_gateway = gateway
            ieee = EUI64.convert(self._ieee)

            device = None
            zha_devices = getattr(gateway, "devices", None)
            if zha_devices and ieee in zha_devices:
                device = zha_devices[ieee]
            elif getattr(gateway, "application_controller", None):
                device = gateway.application_controller.devices.get(ieee)

            if not device:
                raise exc.TransportZigbeeError(
                    f"Zigbee device {self._ieee} not found"
                )

            self._device = device
            await self._wait_for_device_ready(device, ieee)
            self._attach_clusters(device)
            await self._bind_and_configure()

            hgi_id = self._resolve_hgi_id()
            self._extra[SZ_ACTIVE_HGI] = hgi_id
            self._make_connection(gateway_id=DeviceIdT(hgi_id))
            _LOGGER.info(
                "Zigbee transport ready: ieee=%s cluster=0x%04x attr=0x%04x "
                "(hgi_id=%s)",
                self._ieee,
                self._cluster_id,
                self._attr_id,
                hgi_id,
            )
        except asyncio.CancelledError:
            raise
        except ImportError as err:
            _LOGGER.error(
                "zigpy is required for Zigbee transport but is not "
                "installed: %s. Install zigpy (e.g. via the ZHA "
                "integration) or use a different transport.",
                err,
            )
            self._close(
                exc.TransportZigbeeError(
                    f"zigpy is required for Zigbee transport: {err}. "
                    "Install it or use a different transport."
                )
            )
        except Exception as err:
            _LOGGER.exception("Failed to initialize Zigbee transport: %s", err)
            self._close(exc.TransportZigbeeError(str(err)))

    def attribute_updated(self, attrid: int, value: Any) -> None:
        """Handle updates to a bound cluster attribute."""
        self._ensure_read_cluster_bound()
        if attrid != self._attr_id or not isinstance(value, str):
            return

        payload = value.strip()
        if not payload or payload.startswith("ACK "):
            return

        try:
            if self._maybe_handle_incoming_chunk(payload):
                return
        except asyncio.CancelledError:
            raise
        except (
            KeyError,
            ValueError,
            TypeError,
            AttributeError,
            RuntimeError,
            exc.RamsesException,
        ) as err:
            _LOGGER.exception("Error handling incoming chunk: %s", err)

        self._frame_read(dt_now().isoformat(), _normalise(payload))

        try:
            m = re.match(r"^(\d{1,3})/(\d{1,3})\|", payload)
            if m:
                ack = f"ACK {int(m.group(1))}/{int(m.group(2))}"
                self._track_task(
                    self._loop.create_task(
                        self._send_unacked(ack, target_cluster=self._cluster)
                    )
                )
        except asyncio.CancelledError:
            raise
        except (
            KeyError,
            ValueError,
            TypeError,
            AttributeError,
            RuntimeError,
            exc.RamsesException,
        ) as err:
            _LOGGER.exception("Failed to schedule application ACK: %s", err)

    def cluster_command(
        self,
        tsn: int,
        command_id: int,
        args: Any,
        *_args: Any,
        **_kwargs: Any,
    ) -> None:
        """Handle incoming ZCL command on the bound cluster."""
        payload = self._decode_command_payload(args)
        if not payload or (
            isinstance(payload, str) and payload.startswith("ACK ")
        ):
            return

        try:
            if self._maybe_handle_incoming_chunk(payload):
                return
        except asyncio.CancelledError:
            raise
        except (
            KeyError,
            ValueError,
            TypeError,
            AttributeError,
            RuntimeError,
            exc.RamsesException,
        ) as err:
            _LOGGER.exception("Error handling incoming chunk: %s", err)

        self._frame_read(dt_now().isoformat(), _normalise(payload))

        try:
            m = re.match(r"^(\d{1,3})/(\d{1,3})\|", payload)
            if m:
                ack = f"ACK {int(m.group(1))}/{int(m.group(2))}"
                self._track_task(
                    self._loop.create_task(
                        self._send_unacked(ack, target_cluster=self._cluster)
                    )
                )
        except asyncio.CancelledError:
            raise
        except (
            KeyError,
            ValueError,
            TypeError,
            AttributeError,
            RuntimeError,
            exc.RamsesException,
        ) as err:
            _LOGGER.exception("Failed to schedule application ACK: %s", err)

    async def _write_frame(self, frame: str) -> None:
        """Write a frame to the Zigbee device."""
        if self._closing:
            raise exc.TransportZigbeeError("Zigbee transport is closing")

        _LOGGER.debug("Zigbee write requested frame: %s", frame)
        payload = frame.strip()
        if not payload:
            return

        chunks = self._chunk_payload(payload)
        for sequence_number, total, chunk in chunks:
            try:
                if self._use_command_mode:
                    await self._send_command(chunk, sequence_number, total)
                else:
                    await self._send_chunk(chunk, sequence_number, total)
                if sequence_number < total:
                    await asyncio.sleep(0.025)
            except asyncio.CancelledError:
                raise
            except (
                KeyError,
                ValueError,
                TypeError,
                AttributeError,
                RuntimeError,
                exc.RamsesException,
            ) as err:
                _LOGGER.exception(
                    "Zigbee chunk %s/%s failed: %s - continuing",
                    sequence_number,
                    total,
                    err,
                )

    def close(self) -> None:
        """Close transport cleanly, unbinding clusters and cancelling tasks."""
        if self._closing:
            return
        self._closing = True

        for task in self._tasks:
            if not task.done():
                task.cancel()

        cluster = getattr(self, "_cluster", None)
        if cluster is not None:
            try:
                cluster.remove_listener(self)
            except (
                KeyError,
                ValueError,
                TypeError,
                AttributeError,
                RuntimeError,
                exc.RamsesException,
            ) as err:
                _LOGGER.exception("Failed to remove listener: %s", err)

        unsub = getattr(self, "_device_ready_unsub", None)
        if unsub is not None:
            try:
                unsub()
            except (
                KeyError,
                ValueError,
                TypeError,
                AttributeError,
                RuntimeError,
                exc.RamsesException,
            ) as err:
                _LOGGER.exception("Failed to unsubscribe: %s", err)
            self._device_ready_unsub = None

        super().close()
