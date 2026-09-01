#!/usr/bin/env python3
"""RAMSES RF - MQTT connection and reconnection management."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from paho.mqtt import MQTTException, client as mqtt

try:
    from paho.mqtt.enums import CallbackAPIVersion
except ImportError:
    # Fallback for Paho MQTT < 2.0.0 (Home Assistant compatibility)
    CallbackAPIVersion = None  # type: ignore[assignment, misc]

from ... import exceptions as exc
from ...typing import DeviceIdT
from .framing import (
    TOPIC_SUFFIX_RX,
    TOPIC_SUFFIX_TX,
    TOPIC_WILDCARD_RX,
    validate_topic_path,
)

_LOGGER = logging.getLogger(__name__)


class MqttConnectionManager:
    """Manages the Paho MQTT client lifecycle, callbacks, and reconnection."""

    def __init__(
        self,
        broker_url: str,
        loop: asyncio.AbstractEventLoop,
        *,
        on_connection_established: Callable[[DeviceIdT | None], None],
        on_connection_lost: Callable[[Exception | None], None],
        on_message_received: Callable[[mqtt.MQTTMessage], None],
        on_pause_writing: Callable[[], None],
        on_resume_writing: Callable[[], None],
        on_schedule_reconnect: Callable[[], None] | None = None,
        on_attempt_connection: Callable[[], None] | None = None,
    ) -> None:
        """Initialise the MQTT connection manager."""
        self._broker_url = urlparse(broker_url)
        self._loop = loop
        self._on_connection_established = on_connection_established
        self._on_connection_lost = on_connection_lost
        self._on_message_received = on_message_received
        self._on_pause_writing = on_pause_writing
        self._on_resume_writing = on_resume_writing
        self._on_schedule_reconnect_cb = on_schedule_reconnect
        self._on_attempt_connection_cb = on_attempt_connection

        self._username = unquote(self._broker_url.username or "")
        self._password = unquote(self._broker_url.password or "")

        self._topic_base = validate_topic_path(self._broker_url.path)
        self._topic_pub = ""
        self._topic_sub = ""
        self._data_wildcard_topic = ""

        qos_val = parse_qs(self._broker_url.query).get("qos", ["0"])[0]
        self._mqtt_qos = int(qos_val)

        self._connected = False
        self._connecting = False
        self._connection_established = False
        self._closing = False

        # Track all online ESP IDs seen via the status topic (wildcard).
        # The primary ESP is used for _topic_pub/_topic_sub; additional
        # ESPs are tracked so the coordinator can register them in the
        # discovery scan (issue 1119: multi-HGI via single MQTT transport).
        self._online_esp_ids: set[DeviceIdT] = set()
        # The primary ESP ID (the one whose /rx topic is _topic_sub).
        # Set when the first ESP comes online and _topic_sub is assigned.
        self._primary_esp_id: DeviceIdT | None = None

        # Reconnection settings
        self._reconnect_interval = 5.0
        self._max_reconnect_interval = 300.0
        self._reconnect_backoff = 1.5
        self._current_reconnect_interval = self._reconnect_interval
        self._reconnect_task: asyncio.Task[None] | None = None

        client_kwargs: dict[str, Any] = {"protocol": mqtt.MQTTv5}
        if CallbackAPIVersion is not None:
            client_kwargs["callback_api_version"] = CallbackAPIVersion.VERSION2

        self.client = mqtt.Client(**client_kwargs)
        self.client.on_connect = self._on_connect
        self.client.on_connect_fail = self._on_connect_fail
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.username_pw_set(self._username, self._password)

    @property
    def is_connected(self) -> bool:
        """Return True if currently connected to the MQTT broker."""
        return self._connected

    @property
    def online_esp_ids(self) -> frozenset[DeviceIdT]:
        """Return all ESP IDs seen online via the status topic."""
        return frozenset(self._online_esp_ids)

    @property
    def topic_pub(self) -> str:
        """Return the active publish topic."""
        return self._topic_pub

    @property
    def topic_sub(self) -> str:
        """Return the active subscribe topic."""
        return self._topic_sub

    def start(self) -> None:
        """Begin connecting to the MQTT broker."""
        self._attempt_connection()

    def _attempt_connection(self) -> None:
        """Attempt to connect to the MQTT broker."""
        if self._connecting or self._connected:
            return

        self._connecting = True
        try:
            self.client.connect_async(
                str(self._broker_url.hostname or "localhost"),
                self._broker_url.port or 1883,
                60,
            )
            self.client.loop_start()
        except (ValueError, OSError, MQTTException) as err:
            _LOGGER.exception("Failed to initiate MQTT connection: %s", err)
            self._connecting = False
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Schedule a reconnection attempt with exponential backoff."""
        if self._on_schedule_reconnect_cb is not None:
            self._on_schedule_reconnect_cb()
            return

        self._do_schedule_reconnect()

    def _do_schedule_reconnect(self) -> None:
        """Execute scheduling the reconnect task."""
        if self._closing or self._reconnect_task:
            return

        _LOGGER.info(
            "Scheduling MQTT reconnect in %s seconds",
            self._current_reconnect_interval,
        )
        self._reconnect_task = self._loop.create_task(
            self._reconnect_after_delay(),
            name="MqttConnectionManager._reconnect_after_delay()",
        )

    async def _reconnect_after_delay(self) -> None:
        """Wait and then attempt to reconnect."""
        try:
            await asyncio.sleep(self._current_reconnect_interval)
            self._current_reconnect_interval = min(
                self._current_reconnect_interval * self._reconnect_backoff,
                self._max_reconnect_interval,
            )
            _LOGGER.info("Attempting MQTT reconnection...")
            if self._on_attempt_connection_cb is not None:
                self._on_attempt_connection_cb()
            else:
                self._attempt_connection()
        except asyncio.CancelledError:
            pass
        finally:
            self._reconnect_task = None

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: dict[str, Any],
        reason_code: Any,
        properties: Any | None,
    ) -> None:
        """Handle MQTT connection success."""
        self._connecting = False

        if reason_code.is_failure:
            _LOGGER.error("MQTT connection failed: %s", reason_code.getName())
            self._schedule_reconnect()
            return

        _LOGGER.info("MQTT connected: %s", reason_code.getName())
        self._current_reconnect_interval = self._reconnect_interval

        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None

        self.client.subscribe(self._topic_base)
        _LOGGER.info("Subscribed to status topic: %s", self._topic_base)

        # If the topic_base is a specific HGI (not wildcard), also
        # subscribe to the parent wildcard status topic so we can
        # discover additional ESPs on the same broker (issue 1119).
        if not self._topic_base.endswith("/+"):
            parent_status = "/".join(self._topic_base.split("/")[:-1] + ["+"])
            self.client.subscribe(parent_status)
            _LOGGER.info(
                "Subscribed to wildcard status topic: %s", parent_status
            )

        if self._topic_base.endswith("/+") and not self._topic_sub:
            data_wildcard = self._topic_base.replace("/+", TOPIC_WILDCARD_RX)
            self.client.subscribe(data_wildcard, qos=self._mqtt_qos)
            self._data_wildcard_topic = data_wildcard
            _LOGGER.info("Subscribed to data wildcard: %s", data_wildcard)

        if self._topic_sub:
            self.client.subscribe(self._topic_sub, qos=self._mqtt_qos)
            _LOGGER.debug(
                "Re-subscribed to specific topic: %s", self._topic_sub
            )
            if self._data_wildcard_topic:
                try:
                    self.client.unsubscribe(self._data_wildcard_topic)
                except (ValueError, MQTTException) as err:
                    _LOGGER.exception(
                        "Error unsubscribing data wildcard: %s", err
                    )
                finally:
                    self._data_wildcard_topic = ""

        if not self._connection_established:
            gwy_id: DeviceIdT | None = None
            if not self._topic_base.endswith("/+"):
                parts = self._topic_base.split("/")
                if len(parts) == 3:
                    gwy_id = DeviceIdT(parts[-1])
                    if not self._topic_pub:
                        self._primary_esp_id = gwy_id
                        self._topic_pub = self._topic_base + TOPIC_SUFFIX_TX
                        self._topic_sub = self._topic_base + TOPIC_SUFFIX_RX
                        self.client.subscribe(
                            self._topic_sub, qos=self._mqtt_qos
                        )
                        _LOGGER.debug(
                            "Pre-set topic_pub/sub from topic_base: %s, %s",
                            self._topic_pub,
                            self._topic_sub,
                        )
            self._establish_connection(gwy_id)

    def _on_connect_fail(
        self,
        client: mqtt.Client,
        userdata: Any,
    ) -> None:
        """Handle MQTT connection failure."""
        _LOGGER.error("MQTT connection failed")
        self._connecting = False
        self._connected = False
        if not self._closing:
            self._schedule_reconnect()

    def _establish_connection(self, gateway_id: DeviceIdT | None) -> None:
        """Establish the protocol connection callback."""
        if self._connection_established:
            if gateway_id is not None:
                self._on_connection_established(gateway_id)
            return

        self._connected = True
        self._connection_established = True
        self._on_connection_established(gateway_id)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Handle MQTT disconnection."""
        reason_code = args[0] if len(args) >= 1 else None
        reason_name = (
            reason_code.getName()
            if reason_code is not None and hasattr(reason_code, "getName")
            else str(reason_code)
        )
        _LOGGER.warning("MQTT disconnected: %s", reason_name)

        was_connected = self._connected
        self._connected = False

        if was_connected and self._topic_sub:
            device_topic = self._topic_sub[:-3]
            _LOGGER.warning("The MQTT device is offline: %s", device_topic)
            self._on_pause_writing()

        if not self._closing:
            self._schedule_reconnect()

    def _on_message(
        self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage
    ) -> None:
        """Dispatch received MQTT message."""
        if self._closing:
            return

        if msg.topic[-3:] != TOPIC_SUFFIX_RX:
            if msg.payload == b"offline":
                # Remove from online ESP set
                esp_id = DeviceIdT(msg.topic.rsplit("/", 1)[-1])
                self._online_esp_ids.discard(esp_id)
                if (
                    self._topic_sub and msg.topic == self._topic_sub[:-3]
                ) or not self._topic_sub:
                    _LOGGER.warning(
                        "The ESP device is offline (via LWT): %s",
                        msg.topic,
                    )
                    self._on_pause_writing()
            elif msg.payload == b"online":
                _LOGGER.info(
                    "The ESP device is online (via status): %s",
                    msg.topic,
                )
                self._handle_device_online(msg)
            return

        if msg.topic.endswith(TOPIC_SUFFIX_RX):
            topic_parts = msg.topic.split("/")
            if len(topic_parts) >= 3 and topic_parts[-2] not in ("+", "*"):
                gateway_id = topic_parts[-2]
                if not self._topic_sub:
                    self._topic_pub = f"{'/'.join(topic_parts[:-1])}/tx"
                    self._topic_sub = msg.topic
                    _LOGGER.info(
                        "Inferring gateway connection from data topic: %s",
                        gateway_id,
                    )
                    try:
                        self.client.subscribe(
                            self._topic_sub, qos=self._mqtt_qos
                        )
                    except (ValueError, MQTTException) as err:
                        _LOGGER.exception(
                            "Error subscribing specific topic: %s", err
                        )
                    if self._data_wildcard_topic:
                        try:
                            self.client.unsubscribe(self._data_wildcard_topic)
                        except (ValueError, MQTTException) as err:
                            _LOGGER.exception(
                                "Error unsubscribing wildcard: %s", err
                            )
                        finally:
                            self._data_wildcard_topic = ""
                if not self._connection_established:
                    self._establish_connection(DeviceIdT(gateway_id))

        self._on_message_received(msg)

    def _handle_device_online(self, msg: mqtt.MQTTMessage) -> None:
        """Handle online status message from ramses_esp device."""
        # Extract the ESP device ID from the topic (last segment).
        esp_id = DeviceIdT(msg.topic.rsplit("/", 1)[-1])
        self._online_esp_ids.add(esp_id)

        if self._connected:
            if not self._topic_sub:
                _LOGGER.info(
                    "MQTT device online — subscribing to /rx "
                    "(deferred from broker connect)"
                )
                self._primary_esp_id = esp_id
                self._topic_pub = msg.topic + TOPIC_SUFFIX_TX
                self._topic_sub = msg.topic + TOPIC_SUFFIX_RX
                self.client.subscribe(self._topic_sub, qos=self._mqtt_qos)
                if self._data_wildcard_topic:
                    try:
                        self.client.unsubscribe(self._data_wildcard_topic)
                    except (ValueError, MQTTException) as err:
                        _LOGGER.exception(
                            "Error unsubscribing data wildcard: %s", err
                        )
                    finally:
                        self._data_wildcard_topic = ""
                self._establish_connection(esp_id)
            elif esp_id == self._primary_esp_id:
                _LOGGER.info("MQTT device came back online - resuming writing")
                self._on_resume_writing()
            else:
                # A different ESP came online — subscribe to its /rx topic
                # so we can receive packets from it too (issue 1119).
                # The primary ESP's topic_sub is already set; this adds
                # the new ESP's RX topic as an additional subscription.
                new_rx_topic = msg.topic + TOPIC_SUFFIX_RX
                if new_rx_topic != self._topic_sub:
                    try:
                        self.client.subscribe(new_rx_topic, qos=self._mqtt_qos)
                        _LOGGER.info(
                            "Subscribed to additional ESP /rx topic: %s",
                            new_rx_topic,
                        )
                    except (ValueError, MQTTException) as err:
                        _LOGGER.exception(
                            "Error subscribing to additional ESP /rx: %s",
                            err,
                        )
                else:
                    _LOGGER.info(
                        "MQTT device came back online - resuming writing"
                    )
                    self._on_resume_writing()
            return

        _LOGGER.info("MQTT device is online - establishing connection")
        self._connected = True
        self._primary_esp_id = esp_id
        self._topic_pub = msg.topic + TOPIC_SUFFIX_TX
        self._topic_sub = msg.topic + TOPIC_SUFFIX_RX
        self.client.subscribe(self._topic_sub, qos=self._mqtt_qos)

        if self._data_wildcard_topic:
            try:
                self.client.unsubscribe(self._data_wildcard_topic)
            except (ValueError, MQTTException) as err:
                _LOGGER.exception("Error unsubscribing data wildcard: %s", err)
            finally:
                self._data_wildcard_topic = ""

        if not self._connection_established:
            self._establish_connection(DeviceIdT(msg.topic[-9:]))
        else:
            self._on_connection_established(DeviceIdT(msg.topic[-9:]))

    def publish(self, payload: str) -> None:
        """Publish payload string to active MQTT topic."""
        if not self._connected:
            raise exc.TransportStateError("Cannot publish: MQTT not connected")
        if not self._topic_pub:
            _LOGGER.warning("Cannot publish: _topic_pub not yet set")
            return

        info: mqtt.MQTTMessageInfo = self.client.publish(
            self._topic_pub, payload=payload, qos=self._mqtt_qos
        )
        if not info:
            _LOGGER.warning("MQTT publish returned no info")
        elif info.rc != mqtt.MQTT_ERR_SUCCESS:
            _LOGGER.warning("MQTT publish failed with code: %s", info.rc)
            if info.rc in (mqtt.MQTT_ERR_NO_CONN, mqtt.MQTT_ERR_CONN_LOST):
                self._connected = False
                if not self._closing:
                    self._schedule_reconnect()

    def close(self) -> None:
        """Close connection and stop network loop thread."""
        self._closing = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None

        try:
            if self._topic_sub and self._connected:
                self.client.unsubscribe(self._topic_sub)
            self.client.disconnect()
            threading.Thread(target=self.client.loop_stop, daemon=True).start()
        except (ValueError, MQTTException) as err:
            _LOGGER.exception("Error during MQTT cleanup: %s", err)

        self._connected = False
