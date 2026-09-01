#!/usr/bin/env python3
"""RAMSES RF - MQTT-based packet transport."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime as dt, timedelta as td
from typing import Any, Final
from urllib.parse import urlparse

from paho.mqtt import MQTTException, client as mqtt

from ... import exceptions as exc
from ...const import (
    DUTY_CYCLE_DURATION,
    MAX_TRANSMIT_RATE_TOKENS,
    SZ_ACTIVE_HGI,
    SZ_IS_EVOFW3,
)
from ...typing import DeviceIdT, RamsesProtocolT
from ..base import TransportConfig, _FullTransport
from ..helpers import _normalise
from .connection import MqttConnectionManager
from .framing import MqttFramingHandler

_LOGGER = logging.getLogger(__name__)

_DBG_FORCE_FRAME_LOGGING: Final[bool] = False


class _MqttTransportAbstractor:
    """Do the bare minimum to abstract a transport from its underlying class."""

    def __init__(
        self,
        broker_url: str,
        protocol: RamsesProtocolT,
        /,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialise the MQTT transport abstractor."""
        self._broker_url = urlparse(broker_url)
        self._protocol = protocol
        self._loop = loop or asyncio.get_event_loop()


class MqttTransport(_FullTransport, _MqttTransportAbstractor):
    """Send/receive packets to/from ramses_esp via MQTT."""

    _MAX_TOKENS: Final[int] = MAX_TRANSMIT_RATE_TOKENS
    _TIME_WINDOW: Final[int] = DUTY_CYCLE_DURATION
    _TOKEN_RATE: Final[float] = _MAX_TOKENS / _TIME_WINDOW

    def __init__(
        self,
        broker_url: str,
        protocol: RamsesProtocolT,
        /,
        *,
        config: TransportConfig,
        extra: dict[str, Any] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialise the MQTT transport."""
        _MqttTransportAbstractor.__init__(
            self, broker_url, protocol, loop=loop
        )
        _FullTransport.__init__(self, config=config, extra=extra, loop=loop)

        self._extra[SZ_IS_EVOFW3] = True
        self._log_all = config.log_all

        self._framing = MqttFramingHandler()
        self._connection_mgr = MqttConnectionManager(
            broker_url,
            self._loop,
            on_connection_established=self._handle_connection_established,
            on_connection_lost=self._handle_connection_lost,
            on_message_received=self._handle_message_received,
            on_pause_writing=self._handle_pause_writing,
            on_resume_writing=self._handle_resume_writing,
            on_schedule_reconnect=lambda: self._schedule_reconnect(),
            on_attempt_connection=lambda: self._attempt_connection(),
        )

        self._connection_mgr.start()

    @property
    def client(self) -> mqtt.Client:
        """Return the underlying Paho MQTT client."""
        return self._connection_mgr.client

    @client.setter
    def client(self, value: mqtt.Client) -> None:
        self._connection_mgr.client = value

    @property
    def _connected(self) -> bool:
        """Return connection state."""
        return self._connection_mgr._connected

    @_connected.setter
    def _connected(self, value: bool) -> None:
        self._connection_mgr._connected = value

    @property
    def _connecting(self) -> bool:
        """Return connecting state."""
        return self._connection_mgr._connecting

    @_connecting.setter
    def _connecting(self, value: bool) -> None:
        self._connection_mgr._connecting = value

    @property
    def _connection_established(self) -> bool:
        """Return connection established state."""
        return self._connection_mgr._connection_established

    @_connection_established.setter
    def _connection_established(self, value: bool) -> None:
        self._connection_mgr._connection_established = value

    @property
    def _reconnect_task(self) -> asyncio.Task[None] | None:
        """Return reconnect task."""
        return self._connection_mgr._reconnect_task

    @_reconnect_task.setter
    def _reconnect_task(self, value: asyncio.Task[None] | None) -> None:
        self._connection_mgr._reconnect_task = value

    @property
    def _reconnect_interval(self) -> float:
        return self._connection_mgr._reconnect_interval

    @property
    def _max_reconnect_interval(self) -> float:
        return self._connection_mgr._max_reconnect_interval

    @property
    def _reconnect_backoff(self) -> float:
        return self._connection_mgr._reconnect_backoff

    @property
    def _current_reconnect_interval(self) -> float:
        return self._connection_mgr._current_reconnect_interval

    @_current_reconnect_interval.setter
    def _current_reconnect_interval(self, value: float) -> None:
        self._connection_mgr._current_reconnect_interval = value

    @property
    def _topic_base(self) -> str:
        return self._connection_mgr._topic_base

    @_topic_base.setter
    def _topic_base(self, value: str) -> None:
        self._connection_mgr._topic_base = value

    @property
    def _topic_pub(self) -> str:
        return self._connection_mgr._topic_pub

    @_topic_pub.setter
    def _topic_pub(self, value: str) -> None:
        self._connection_mgr._topic_pub = value

    @property
    def _topic_sub(self) -> str:
        return self._connection_mgr._topic_sub

    @_topic_sub.setter
    def _topic_sub(self, value: str) -> None:
        self._connection_mgr._topic_sub = value

    @property
    def _data_wildcard_topic(self) -> str:
        return self._connection_mgr._data_wildcard_topic

    @_data_wildcard_topic.setter
    def _data_wildcard_topic(self, value: str) -> None:
        self._connection_mgr._data_wildcard_topic = value

    @property
    def _mqtt_qos(self) -> int:
        return self._connection_mgr._mqtt_qos

    @property
    def _num_tokens(self) -> float:
        return self._framing._num_tokens

    @_num_tokens.setter
    def _num_tokens(self, value: float) -> None:
        self._framing._num_tokens = value

    @property
    def _max_tokens(self) -> float:
        return self._framing._max_tokens

    @_max_tokens.setter
    def _max_tokens(self, value: float) -> None:
        self._framing._max_tokens = value

    @property
    def _timestamp(self) -> float:
        return self._framing._timestamp

    @_timestamp.setter
    def _timestamp(self, value: float) -> None:
        self._framing._timestamp = value

    def _attempt_connection(self) -> None:
        self._connection_mgr._attempt_connection()

    def _schedule_reconnect(self) -> None:
        self._connection_mgr._do_schedule_reconnect()

    async def _reconnect_after_delay(self) -> None:
        await self._connection_mgr._reconnect_after_delay()

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: dict[str, Any],
        reason_code: Any,
        properties: Any | None,
    ) -> None:
        self._connection_mgr._on_connect(
            client, userdata, flags, reason_code, properties
        )

    def _on_connect_fail(
        self,
        client: mqtt.Client,
        userdata: Any,
    ) -> None:
        self._connection_mgr._on_connect_fail(client, userdata)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self._connection_mgr._on_disconnect(client, userdata, *args, **kwargs)

    def _on_message(
        self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage
    ) -> None:
        if self._closing:
            return
        self._connection_mgr._on_message(client, userdata, msg)

    def _create_connection(self, msg: mqtt.MQTTMessage) -> None:
        self._connection_mgr._handle_device_online(msg)

    def _establish_connection(self, gateway_id: DeviceIdT | None) -> None:
        self._connection_mgr._establish_connection(gateway_id)

    def _publish(self, payload: str) -> None:
        self._connection_mgr.publish(payload)

    def _handle_connection_established(
        self, gateway_id: DeviceIdT | None
    ) -> None:
        """Handle active connection from connection manager."""
        if gateway_id is not None:
            self._extra[SZ_ACTIVE_HGI] = gateway_id

        if not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(
                    self._make_connection, gateway_id
                )
            except RuntimeError:
                _LOGGER.debug("Event loop closed, cannot establish connection")

    def _handle_connection_lost(self, exc_val: Exception | None) -> None:
        """Handle lost connection from connection manager."""
        self._close(exc.TransportError(str(exc_val)) if exc_val else None)

    def _handle_pause_writing(self) -> None:
        """Handle request to pause protocol writing."""
        if hasattr(self, "_protocol") and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._protocol.pause_writing)
            except RuntimeError:
                _LOGGER.debug("Event loop closed, cannot pause writing")

    def _handle_resume_writing(self) -> None:
        """Handle request to resume protocol writing."""
        if hasattr(self, "_protocol") and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._protocol.resume_writing)
            except RuntimeError:
                _LOGGER.debug("Event loop closed, cannot resume writing")

    def _handle_message_received(self, msg: mqtt.MQTTMessage) -> None:
        """Process incoming decoded MQTT data message."""
        if _DBG_FORCE_FRAME_LOGGING:
            _LOGGER.warning("Rx: %s", msg.payload)
        elif self._log_all and _LOGGER.getEffectiveLevel() == logging.INFO:
            _LOGGER.info("mq Rx: %s", msg.payload)

        decoded = self._framing.decode_payload(msg.payload)
        if decoded is None:
            return

        ts_str, raw_msg = decoded
        dtm = dt.fromisoformat(ts_str)
        if dtm.tzinfo is not None:
            dtm = dtm.astimezone().replace(tzinfo=None)
        if dtm < dt.now() - td(days=90):
            _LOGGER.warning(
                "%s: Have you configured the SNTP settings on the ESP?",
                self,
            )

        if not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(
                    self._frame_read,
                    dtm.isoformat(),
                    _normalise(raw_msg),
                )
            except RuntimeError:
                _LOGGER.debug("Event loop closed, cannot read frame")

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        """Get extra information about the transport.

        Extends the base class to expose ``pool_hgi_ids`` — the set of
        all ESP IDs seen online via the MQTT status topic (wildcard).
        This allows the coordinator to register MQTT-discovered HGIs
        in the discovery scan, even without a PooledTransport.
        """
        if name == "pool_hgi_ids":
            ids = self._connection_mgr.online_esp_ids
            return list(ids) if ids else default
        return super().get_extra_info(name, default)

    async def write_frame(
        self, frame: str, disable_tx_limits: bool = False
    ) -> None:
        """Transmit a frame with token-bucket rate limiting."""
        if not self._connection_mgr.is_connected:
            raise exc.TransportStateError(
                "Cannot write frame: MQTT not connected"
            )

        allowed = await self._framing.throttle_tx(
            disable_tx_limits=disable_tx_limits
        )
        if not allowed:
            return

        await super().write_frame(frame)

    async def _write_frame(self, frame: str) -> None:
        """Write frame payload to the MQTT broker."""
        data = self._framing.encode_frame(frame)

        if _DBG_FORCE_FRAME_LOGGING:
            _LOGGER.warning("Tx: %s", data)
        elif _LOGGER.getEffectiveLevel() == logging.INFO or self._log_all:
            _LOGGER.info("mq Tx: %s", data)

        try:
            self._connection_mgr.publish(data)
        except (ValueError, MQTTException) as err:
            _LOGGER.exception("MQTT publish failed: %s", err)
            self._connection_mgr._connected = False
            if not self._connection_mgr._closing:
                self._schedule_reconnect()

    def _close(self, exc_val: exc.RamsesException | None = None) -> None:
        """Close the transport and teardown MQTT connections."""
        super()._close(exc_val)
        self._connection_mgr.close()
