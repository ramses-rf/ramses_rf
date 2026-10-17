#!/usr/bin/env python3
"""RAMSES RF - Zigbee cluster and device connection management."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ... import exceptions as exc

if TYPE_CHECKING:
    from zigpy.types import EUI64 as ZigbeeIEEE

_LOGGER = logging.getLogger(__name__)


class ZigbeeConnectionManager:
    """Manages ZHA / zigpy cluster endpoints, readiness, and command I/O."""

    _GATEWAY_POLL_INTERVAL: float = 1.0
    _GATEWAY_POLL_ATTEMPTS: int = 30
    _DEVICE_READY_TIMEOUT: float = 60.0
    _PING_TIMEOUT: float = 10.0

    def __init__(
        self,
        *,
        hass: Any,
        ieee: str,
        cluster_id: int,
        attr_id: int,
        endpoint_id: int,
        write_cluster_id: int,
        write_attr_id: int,
        write_endpoint_id: int,
        cmd_id: int = 0x00,
        use_command_mode: bool = True,
        listener: Any | None = None,
    ) -> None:
        """Initialise the Zigbee connection manager."""
        self._hass = hass
        self._ieee = ieee
        self._cluster_id = cluster_id
        self._attr_id = attr_id
        self._endpoint_id = endpoint_id
        self._write_cluster_id = write_cluster_id
        self._write_attr_id = write_attr_id
        self._write_endpoint_id = write_endpoint_id
        self._cmd_id = cmd_id
        self._use_command_mode = use_command_mode
        self._listener = listener

        self._read_direction = "out" if use_command_mode else "in"
        self._write_direction = "in"

        self._device: Any | None = None
        self._zha_gateway: Any | None = None
        self._cluster: Any | None = None
        self._write_cluster: Any | None = None
        self._device_ready_unsub: Callable[[], None] | None = None

    @property
    def read_cluster(self) -> Any | None:
        """Return the active read cluster handle."""
        return self._cluster

    @property
    def write_cluster(self) -> Any | None:
        """Return the active write cluster handle."""
        return self._write_cluster

    async def wait_for_gateway(self) -> Any:
        """Poll the Home Assistant environment for the ZHA gateway.

        Fails fast when no ZHA config entry could produce a gateway this
        boot (no entry at all, or every entry in a failed/retrying
        state) — waiting the full poll is pointless while ZHA backs off
        for minutes.  A pending watcher (ramses_cc ``_schedule_zigbee_
        rejoin``) reloads the entry once ZHA does come up.
        """
        for _attempt in range(self._GATEWAY_POLL_ATTEMPTS):
            gateway = self._current_gateway()
            if gateway:
                return gateway
            if not self._zha_may_come_up():
                raise exc.TransportZigbeeError(
                    "ZHA gateway proxy not found "
                    "(no ZHA config entry pending load)"
                )
            await asyncio.sleep(self._GATEWAY_POLL_INTERVAL)
        raise exc.TransportZigbeeError("ZHA gateway proxy not found")

    def _zha_may_come_up(self) -> bool:
        """Return True while a ZHA config entry may still yield a gateway.

        Entries in ``not_loaded``/``setup_in_progress``/``loaded`` may
        still initialise the gateway proxy; entries in retry/error
        states will not within this poll window.  Unknown hass mocks
        keep the historical poll-and-wait behaviour.
        """
        config_entries = getattr(self._hass, "config_entries", None)
        async_entries = getattr(config_entries, "async_entries", None)
        if async_entries is None:
            return True
        try:
            entries = async_entries("zha")
            if not entries:
                return False
            pending = {"not_loaded", "setup_in_progress", "loaded"}
            return any(
                getattr(getattr(e, "state", None), "value", None) in pending
                for e in entries
            )
        except Exception:  # noqa: BLE001 — mocked hass in tests
            return True

    def _current_gateway(self) -> Any | None:
        """Return the live ZHA gateway object, or None if ZHA is down."""
        zha_data = (
            self._hass.data.get("zha")
            if self._hass and hasattr(self._hass, "data")
            else None
        )
        gateway_proxy = (
            getattr(zha_data, "gateway_proxy", None) if zha_data else None
        )
        return (
            getattr(gateway_proxy, "gateway", None) if gateway_proxy else None
        )

    def gateway_present(self) -> bool:
        """Return True if the ZHA gateway is reachable; refresh the handle."""
        gateway = self._current_gateway()
        if gateway is None:
            return False
        self._zha_gateway = gateway
        return True

    def resolve_device(self) -> Any | None:
        """Re-resolve the ZHA device for our IEEE (survives ZHA reloads)."""
        gateway = self._zha_gateway or self._current_gateway()
        if gateway is None:
            return None
        ieee: ZigbeeIEEE | str
        try:
            from zigpy.types import EUI64

            ieee = EUI64.convert(self._ieee)
        except ImportError:
            ieee = self._ieee

        devices = getattr(gateway, "devices", None)
        if devices and ieee in devices:
            return devices[ieee]
        controller = getattr(gateway, "application_controller", None)
        if controller is not None:
            return getattr(controller, "devices", {}).get(ieee)
        return None

    def device_available(self) -> bool:
        """Return the ZHA-reported availability of the bound device."""
        device = self._device
        if device is None:
            return False
        return bool(
            getattr(device, "available", True)
            and getattr(device, "on_network", True)
        )

    def last_seen_age(self) -> float | None:
        """Seconds since the Zigbee device last reported, or None if unknown."""
        import time

        device = self._device
        last_seen = getattr(device, "last_seen", None)
        if last_seen is None:
            zigpy_device = getattr(device, "_zigpy_device", None) or getattr(
                device, "zigpy_device", None
            )
            last_seen = getattr(zigpy_device, "last_seen", None)
        if last_seen is None:
            return None
        return float(time.time() - last_seen)

    async def ping_device(self) -> bool | None:
        """Actively verify device liveness via the Basic cluster.

        Mirrors ZHA's own availability check: read the manufacturer
        attribute.  Returns None when no usable Basic cluster exists
        (liveness cannot be verified actively).
        """
        device = self._device
        basic = getattr(device, "basic_cluster", None)
        read = getattr(basic, "read_attributes", None)
        if basic is None or not callable(read):
            return None
        try:
            result = await asyncio.wait_for(
                read(["manufacturer"], allow_cache=False),
                timeout=self._PING_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        success = result[0] if isinstance(result, (tuple, list)) else result
        return bool(success)

    def subscribe_device_offline(
        self, callback: Callable[[], None]
    ) -> Callable[[], None] | None:
        """Subscribe to the device's zha_event stream for offline events.

        ZHA emits ``{"device_event_type": "device_offline"}`` when it
        marks a device unavailable — an early signal that does not wait
        for the next availability poll.
        """
        on_event = getattr(self._device, "on_event", None)
        if not callable(on_event):
            return None

        def _listener(event: Any = None, *_args: Any, **_kwargs: Any) -> None:
            data = getattr(event, "data", event)
            if (
                isinstance(data, dict)
                and data.get("device_event_type") == "device_offline"
            ):
                callback()

        try:
            unsub = on_event("zha_event", _listener)
        except Exception as err:
            _LOGGER.debug("Failed to subscribe to device events: %s", err)
            return None
        return unsub if callable(unsub) else None

    async def wait_for_device_ready(self, device: Any, ieee: Any) -> None:
        """Wait for target Zigbee device to be fully initialised."""
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
        except TimeoutError as err:
            raise exc.TransportZigbeeError(
                f"Zigbee device {ieee} did not finish initializing"
            ) from err
        finally:
            if self._device_ready_unsub:
                self._device_ready_unsub()
                self._device_ready_unsub = None

    def get_cluster(
        self,
        device: Any,
        endpoint_id: int,
        cluster_id: int,
        direction: str = "in",
    ) -> Any:
        """Retrieve a cluster object from a ZHA device endpoint."""
        getter = getattr(device, "async_get_cluster", None)
        if callable(getter):
            try:
                cluster = getter(endpoint_id, cluster_id, direction)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                raise exc.TransportZigbeeError(
                    f"Cluster lookup failed for 0x{cluster_id:04x} on "
                    f"endpoint {endpoint_id}: {err}"
                ) from err
            if cluster is None:
                raise exc.TransportZigbeeError(
                    f"Cluster 0x{cluster_id:04x} not found on endpoint {endpoint_id}"
                )
            return cluster

        if not hasattr(device, "endpoints"):
            raise exc.TransportZigbeeError(
                "Zigbee device has no endpoints map"
            )

        endpoint = device.endpoints.get(endpoint_id)
        if endpoint is None:
            raise exc.TransportZigbeeError(
                f"Endpoint {endpoint_id} not found on Zigbee device {self._ieee}"
            )

        clusters_attr = "in_clusters" if direction == "in" else "out_clusters"
        clusters = getattr(endpoint, clusters_attr, None)
        if clusters is None:
            raise exc.TransportZigbeeError(
                f"Endpoint {endpoint_id} has no {direction} clusters map"
            )

        cluster = clusters.get(cluster_id)
        if cluster is None:
            raise exc.TransportZigbeeError(
                f"Cluster 0x{cluster_id:04x} not found on endpoint {endpoint_id}"
            )

        return cluster

    def attach_clusters(self, device: Any) -> None:
        """Locate and attach read/write clusters for device."""
        self._device = device
        try:
            read_cluster = self.get_cluster(
                device,
                self._endpoint_id,
                self._cluster_id,
                self._read_direction,
            )
        except exc.TransportZigbeeError:
            _LOGGER.debug(
                "Read cluster 0x%04x not found on endpoint %s; "
                "searching other endpoints/directions",
                self._cluster_id,
                self._endpoint_id,
            )
            found = False
            for ep_id in getattr(device, "endpoints", {}):
                for dir_try in ("in", "out"):
                    try:
                        candidate = self.get_cluster(
                            device, int(ep_id), self._cluster_id, dir_try
                        )
                        _LOGGER.info(
                            "Auto-selected endpoint %s (direction=%s) for "
                            "read cluster 0x%04x",
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
                "Write cluster 0x%04x not found on endpoint %s; "
                "searching other endpoints/directions",
                self._write_cluster_id,
                self._write_endpoint_id,
            )
            found = False
            for ep_id in getattr(device, "endpoints", {}):
                for dir_try in ("in", "out"):
                    try:
                        candidate = self.get_cluster(
                            device, int(ep_id), self._write_cluster_id, dir_try
                        )
                        _LOGGER.info(
                            "Auto-selected endpoint %s (direction=%s) for "
                            "write cluster 0x%04x",
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
                    f"Write cluster 0x{self._write_cluster_id:04x} not found "
                    f"on device {self._ieee}"
                )

        if self._cluster is not None and self._listener is not None:
            try:
                self._cluster.remove_listener(self._listener)
            except Exception as err:
                _LOGGER.exception("Failed to remove listener: %s", err)

        self._cluster = read_cluster
        self._write_cluster = write_cluster

        if self._listener is not None and hasattr(
            self._cluster, "add_listener"
        ):
            self._cluster.add_listener(self._listener)

    async def bind_and_configure(self) -> None:
        """Bind read clusters and configure attribute reporting."""
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

    def refresh_write_cluster(self) -> Any | None:
        """Re-acquire write cluster reference."""
        if not self._device:
            return self._write_cluster

        try:
            cluster = self.get_cluster(
                self._device,
                self._write_endpoint_id,
                self._write_cluster_id,
                self._write_direction,
            )
        except exc.TransportZigbeeError:
            return None

        self._write_cluster = cluster
        return cluster

    def get_active_write_cluster(
        self, force_refresh: bool = False
    ) -> Any | None:
        """Return write cluster, refreshing if requested or missing."""
        if force_refresh or self._write_cluster is None:
            return self.refresh_write_cluster()
        return self._write_cluster

    def ensure_read_cluster_bound(self) -> None:
        """Re-bind read listener if cluster references became stale."""
        if not self._device:
            return

        try:
            cluster = self.get_cluster(
                self._device,
                self._endpoint_id,
                self._cluster_id,
                self._read_direction,
            )
        except exc.TransportZigbeeError:
            return

        if cluster is self._cluster:
            return

        if self._cluster is not None and self._listener is not None:
            try:
                self._cluster.remove_listener(self._listener)
            except Exception as err:
                _LOGGER.exception("Failed to remove listener: %s", err)

        self._cluster = cluster
        if self._listener is not None and hasattr(
            self._cluster, "add_listener"
        ):
            self._cluster.add_listener(self._listener)

    async def send_command(
        self,
        chunk: str,
        sequence_number: int,
        total: int,
        command_override: int | None = None,
    ) -> None:
        """Send a string payload as ZCL command to write cluster."""
        cluster = self.get_active_write_cluster()
        if not cluster:
            raise exc.TransportZigbeeError("Zigbee write cluster not ready")

        _LOGGER.debug("Zigbee TX %s/%s: %s", sequence_number, total, chunk)
        last_err: Exception | None = None

        tried_clusters: list[Any] = []
        candidate_clusters: list[Any] = []

        if command_override is not None and self._cluster is not None:
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
                refreshed = self.get_active_write_cluster(force_refresh=True)
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

    async def send_chunk(
        self, chunk: str, sequence_number: int, total: int
    ) -> None:
        """Write a string chunk to a Zigbee attribute."""
        cluster = self.get_active_write_cluster()
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
                    refreshed = self.get_active_write_cluster(
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

    def close(self) -> None:
        """Close connections and remove event listeners."""
        if self._cluster is not None and self._listener is not None:
            try:
                self._cluster.remove_listener(self._listener)
            except Exception as err:
                _LOGGER.exception("Failed to remove listener: %s", err)

        if self._device_ready_unsub is not None:
            try:
                self._device_ready_unsub()
            except Exception as err:
                _LOGGER.exception("Failed to unsubscribe: %s", err)
            self._device_ready_unsub = None
