#!/usr/bin/env python3
"""RAMSES RF - Callback-based packet transport."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime as dt
from typing import TYPE_CHECKING, Any

from .. import exceptions as exc
from ..helpers import dt_now
from .base import TransportConfig, _FullTransport

if TYPE_CHECKING:
    from ..protocol import RamsesProtocolT

_LOGGER = logging.getLogger(__name__)


class _CallbackTransportAbstractor:
    """Do the bare minimum to abstract a transport from its underlying class."""

    def __init__(self, /, *, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Initialize the callback transport abstractor."""
        self._loop = loop or asyncio.get_event_loop()
        super().__init__()


class CallbackTransport(_FullTransport, _CallbackTransportAbstractor):
    """A virtual transport that delegates I/O to external callbacks.

    This transport provides an Inversion of Control (IoC) interface
    designed for integrations such as Home Assistant, allowing external
    services to inject inbound frames and handle outbound transmission.
    """

    def __init__(
        self,
        protocol: RamsesProtocolT,
        io_writer: Callable[[str], Awaitable[None]],
        /,
        *,
        config: TransportConfig,
        extra: dict[str, Any] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Initialize the callback transport.

        :param protocol: The RAMSES protocol bound to this transport.
        :type protocol: RamsesProtocolT
        :param io_writer: Async callback used to transmit outbound frames.
        :type io_writer: Callable[[str], Awaitable[None]]
        :param config: Extracted setup configuration for transports.
        :type config: TransportConfig
        :param extra: Additional configuration state dictionary, if any.
        :type extra: dict[str, Any] | None
        :param loop: Asyncio event loop instance, defaults to None.
        :type loop: asyncio.AbstractEventLoop | None
        """
        _CallbackTransportAbstractor.__init__(self, loop=loop)
        _FullTransport.__init__(self, config=config, extra=extra, loop=loop)

        self._protocol = protocol
        self._io_writer: Callable[[str], Awaitable[None]] | None = io_writer

        self._reading = False

        _LOGGER.info("CallbackTransport created with io_writer=%s", io_writer)

        self._protocol.connection_made(self, ramses=True)

        if config.autostart:
            self.resume_reading()

    async def write_frame(self, frame: str, disable_tx_limits: bool = False) -> None:
        """Process a frame for transmission by passing it to external writer.

        :param frame: The raw string frame to be transmitted.
        :type frame: str
        :param disable_tx_limits: Flag to bypass transmit rate limits.
        :type disable_tx_limits: bool
        :returns: None
        :rtype: None
        :raises exc.TransportError: If sending disabled or io_writer fails.
        :raises asyncio.CancelledError: If the write task is cancelled.
        """
        if self._disable_sending:
            raise exc.TransportError("Sending has been disabled")

        if self._io_writer is None:
            raise exc.TransportError("Transport has been closed")

        _LOGGER.debug("Sending frame via external writer: %s", frame)

        try:
            await self._io_writer(frame)
        except asyncio.CancelledError:
            _LOGGER.debug("External writer task cancelled while sending frame")
            raise
        except Exception as err:
            _LOGGER.error("External writer failed to send frame: %s", err)
            raise exc.TransportError(f"External writer failed: {err}") from err

    async def _write_frame(self, frame: str) -> None:
        """Wait for the frame to be written by the external writer."""
        await self.write_frame(frame)

    def receive_frame(self, frame: str, dtm: str | None = None) -> None:
        """Ingest a frame from the external source (Read Path).

        :param frame: The raw string frame received from external source.
        :type frame: str
        :param dtm: Optional ISO timestamp string for the frame.
        :type dtm: str | None
        :returns: None
        :rtype: None
        """
        _LOGGER.debug(
            "Received frame from external source: frame=%r, timestamp=%s",
            frame,
            dtm,
        )

        if not self._reading:
            _LOGGER.debug("Dropping received frame (transport paused): %r", frame)
            return

        if dtm:
            try:
                dt.fromisoformat(dtm)
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Invalid ISO timestamp format (%r), using current time",
                    dtm,
                )
                dtm = dt_now().isoformat()
        else:
            dtm = dt_now().isoformat()

        _LOGGER.debug(
            "Ingesting frame into transport: frame=%r, timestamp=%s",
            frame,
            dtm,
        )

        try:
            self._frame_read(dtm, frame.rstrip())
        except exc.TransportError as err:
            _LOGGER.warning("Transport error processing received frame: %s", err)
        except Exception as err:
            _LOGGER.warning("Error processing received frame (%r): %s", frame, err)

    def _close(self, exc: exc.RamsesException | None = None) -> None:
        """Close the transport and unbind callbacks."""
        super()._close(exc)
        self._reading = False
        self._io_writer = None
