#!/usr/bin/env python3
"""RAMSES RF - Factory for RAMSES-II compatible packet transports."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Final, TypeAlias

from .. import exceptions as exc
from ..interfaces import TransportInterface
from ..schemas import SCH_SERIAL_PORT_CONFIG
from ..typing import PortConfigT, RamsesProtocolT, SerPortNameT
from .base import TransportConfig
from .file import FileTransport
from .mqtt import MqttTransport
from .pooled import PooledTransport, _ChildProtocolProxy
from .port import PortTransport

_LOGGER = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_PORT: Final[float] = 3.0
_DEFAULT_TIMEOUT_MQTT: Final[float] = 60.0
_DEFAULT_TIMEOUT_ZIGBEE: Final[float] = 60.0
_DEFAULT_TIMEOUT_POOL: Final[float] = 10.0

RamsesTransportT: TypeAlias = TransportInterface


async def transport_factory(
    protocol: RamsesProtocolT,
    /,
    *,
    config: TransportConfig,
    port_name: SerPortNameT | None = None,
    port_config: PortConfigT | None = None,
    packet_log: str | None = None,
    packet_dict: dict[str, str] | None = None,
    transport_constructor: Callable[..., Awaitable[RamsesTransportT]]
    | None = None,
    extra: dict[str, object] | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> RamsesTransportT:
    """Create and return a Ramses-specific async packet Transport.

    :param protocol: The protocol instance that will use this transport.
    :type protocol: RamsesProtocolT
    :param config: Extracted setup configuration for transports.
    :type config: TransportConfig
    :param port_name: Serial port name or MQTT URL, defaults to None.
    :type port_name: SerPortNameT | None, optional
    :param port_config: Configuration dictionary for serial port, defaults to None.
    :type port_config: PortConfigT | None, optional
    :param packet_log: Path to a file containing packet logs for playback/parsing, defaults to None.
    :type packet_log: str | None, optional
    :param packet_dict: Dictionary of packets for playback, defaults to None.
    :type packet_dict: dict[str, str] | None, optional
    :param transport_constructor: Custom async callable to create a transport, defaults to None.
    :type transport_constructor: Callable[..., Awaitable[RamsesTransportT]] | None, optional
    :param extra: Extra configuration options, defaults to None.
    :type extra: dict[str, Any] | None, optional
    :param loop: Asyncio event loop, defaults to None.
    :type loop: asyncio.AbstractEventLoop | None, optional
    :return: An instantiated RamsesTransportT object.
    :rtype: RamsesTransportT
    :raises exc.TransportSourceInvalid: If the packet source is invalid or multiple sources are specified.
    """
    # Apply regex rules to the Protocol before binding the Transport
    if config.use_regex:
        protocol.set_regex_rules(config.use_regex)

    # If a constructor is provided, delegate entirely to it.
    if transport_constructor:
        _LOGGER.debug(
            "transport_factory: Delegating to external transport_constructor"
        )
        return await transport_constructor(
            protocol,
            config=config,
            extra=extra,
            loop=loop,
        )

    def issue_warning() -> None:
        """Warn of the perils of semi-supported configurations."""
        _LOGGER.warning(
            "%s is not fully supported by this library: "
            "please don't report any Transport/Protocol errors/warnings, "
            "unless they are reproducible with a standard configuration "
            "(e.g. linux with a local serial port)",
            "Windows" if os.name == "nt" else "This type of serial interface",
        )

    if (
        len([x for x in (packet_dict, packet_log, port_name) if x is not None])
        != 1
    ):
        _LOGGER.warning(
            "Input: packet_dict: %s, packet_log: %s, port_name: %s",
            packet_dict,
            packet_log,
            port_name,
        )
        raise exc.TransportSourceInvalid(
            "Packet source must be exactly one of: packet_dict, packet_log, port_name"
        )

    # File
    if (packet_source := packet_log or packet_dict) is not None:
        return FileTransport(
            packet_source, protocol, config=config, extra=extra, loop=loop
        )

    assert port_name is not None  # mypy check

    # Zigbee
    if port_name[:6] == "zigbee":
        from .zigbee import ZigbeeTransport

        transport = ZigbeeTransport(
            port_name,
            protocol,
            config=config,
            loop=loop,
        )
        try:
            await protocol.wait_for_connection_made(
                timeout=_DEFAULT_TIMEOUT_ZIGBEE
            )
        except Exception:
            transport.close()
            raise
        return transport

    assert port_config is not None  # mypy check

    # MQTT
    if port_name[:4] == "mqtt":
        # Check for custom timeout in config, fallback to constant
        mqtt_timeout = config.timeout or _DEFAULT_TIMEOUT_MQTT

        transport = MqttTransport(
            port_name,
            protocol,
            config=config,
            extra=extra,
            loop=loop,
        )

        try:
            # Wait with timeout, handle failure gracefully
            await protocol.wait_for_connection_made(timeout=mqtt_timeout)
        except Exception:
            # Close the transport if setup fails to prevent "Zombie" callbacks
            transport.close()
            raise

        return transport

    # Serial
    ser_config = SCH_SERIAL_PORT_CONFIG(port_config)

    if os.name == "nt" or port_name.startswith(("rfc2217://", "socket://")):
        issue_warning()

    transport_port = PortTransport(
        port_name,
        protocol,
        port_config=ser_config,
        config=config,
        extra=extra,
        loop=loop,
    )

    try:
        await protocol.wait_for_connection_made(
            timeout=config.timeout or _DEFAULT_TIMEOUT_PORT
        )
    except exc.TransportSerialError as err:
        transport_port.close()
        raise exc.TransportSourceInvalid(
            f"Unable to open the serial port {port_name}: {err}"
        ) from err
    except Exception:
        transport_port.close()
        raise

    return transport_port


async def pooled_transport_factory(
    protocol: RamsesProtocolT,
    /,
    *,
    config: TransportConfig,
    port_names: list[SerPortNameT],
    port_configs: list[PortConfigT] | None = None,
    extra: dict[str, object] | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    dedup_window: float = 0.5,
) -> RamsesTransportT:
    """Create a :class:`PooledTransport` from multiple port names.

    Each port name gets its own child transport (serial, MQTT, or
    Zigbee) created with a :class:`_ChildProtocolProxy` that routes
    inbound packets through the pool's deduplication filter.

    :param protocol: The real protocol that receives deduplicated
        packets.
    :type protocol: RamsesProtocolT
    :param config: Transport configuration shared by all children.
    :type config: TransportConfig
    :param port_names: List of port names (serial, MQTT URLs, etc.).
    :type port_names: list[SerPortNameT]
    :param port_configs: Optional per-child port configurations for
        serial ports.  If provided, must be the same length as
        ``port_names``.  If ``None``, serial children will use a
        default config.
    :type port_configs: list[PortConfigT] | None
    :param extra: Extra configuration options shared by all children.
    :type extra: dict[str, object] | None
    :param loop: Asyncio event loop.
    :type loop: asyncio.AbstractEventLoop | None
    :param dedup_window: Deduplication window in seconds.
    :type dedup_window: float
    :returns: A :class:`PooledTransport` wrapping all child transports.
    :rtype: PooledTransport
    :raises ValueError: If ``port_names`` is empty or ``port_configs``
        length doesn't match.
    """
    if not port_names:
        raise ValueError(
            "pooled_transport_factory requires at least one port_name"
        )
    if port_configs is not None and len(port_configs) != len(port_names):
        raise ValueError("port_configs must be the same length as port_names")

    # Apply regex rules to the Protocol before binding any Transport.
    if config.use_regex:
        protocol.set_regex_rules(config.use_regex)

    # Create the pool first so we can create child proxies.
    pool = PooledTransport(
        protocol,
        [],  # filled in below
        config=config,
        loop=loop,
        dedup_window=dedup_window,
    )

    child_transports: list[TransportInterface] = []

    for i, pname in enumerate(port_names):
        proxy = _ChildProtocolProxy(pool, i)
        pconfig = port_configs[i] if port_configs else None

        # Create the child transport via the standard factory, but
        # with the proxy protocol instead of the real one.
        child = await _create_single_child(
            proxy,
            config=config,
            port_name=pname,
            port_config=pconfig,
            extra=extra,
            loop=loop,
        )
        child_transports.append(child)

    # Inject the child transports into the pool.
    pool._transports = child_transports
    pool._child_connected = [False] * len(child_transports)
    pool._child_hgi = [None] * len(child_transports)
    pool._child_transport_objs = [None] * len(child_transports)
    pool._pkts_received = [0] * len(child_transports)

    # Wait for at least one child to connect.
    await pool._wait_for_any_connection(
        timeout=config.timeout or _DEFAULT_TIMEOUT_POOL
    )

    return pool


async def _create_single_child(
    protocol: RamsesProtocolT,
    /,
    *,
    config: TransportConfig,
    port_name: SerPortNameT,
    port_config: PortConfigT | None,
    extra: dict[str, object] | None,
    loop: asyncio.AbstractEventLoop | None,
) -> TransportInterface:
    """Create a single child transport for the pool.

    This is a simplified version of :func:`transport_factory` that
    does not support packet_log/packet_dict (pools only work with
    live transports) and uses a shorter default timeout.
    """
    # Zigbee
    if port_name[:6] == "zigbee":
        from .zigbee import ZigbeeTransport

        transport = ZigbeeTransport(
            port_name,
            protocol,
            config=config,
            loop=loop,
        )
        try:
            await protocol.wait_for_connection_made(
                timeout=_DEFAULT_TIMEOUT_ZIGBEE
            )
        except Exception:
            transport.close()
            raise
        return transport

    # MQTT
    if port_name[:4] == "mqtt":
        mqtt_timeout = config.timeout or _DEFAULT_TIMEOUT_MQTT
        transport = MqttTransport(
            port_name,
            protocol,
            config=config,
            extra=extra,
            loop=loop,
        )
        try:
            await protocol.wait_for_connection_made(timeout=mqtt_timeout)
        except Exception:
            transport.close()
            raise
        return transport

    # Serial
    assert port_config is not None  # serial requires port_config
    ser_config = SCH_SERIAL_PORT_CONFIG(port_config)

    transport_port = PortTransport(
        port_name,
        protocol,
        port_config=ser_config,
        config=config,
        extra=extra,
        loop=loop,
    )

    try:
        await protocol.wait_for_connection_made(
            timeout=config.timeout or _DEFAULT_TIMEOUT_PORT
        )
    except exc.TransportSerialError as err:
        transport_port.close()
        raise exc.TransportSourceInvalid(
            f"Unable to open the serial port {port_name}: {err}"
        ) from err
    except Exception:
        transport_port.close()
        raise

    return transport_port
