#!/usr/bin/env python3

# TODO: a real mess - needs refactor a la protocol_new/transport_new


"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

A pseudo-mocked serial port used for testing.

Will provide a fixed Tx for a given Rx.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime as dt
from queue import Empty, Full, PriorityQueue
from typing import Callable

from ramses_rf import Gateway
from ramses_rf.const import Code
from ramses_tx import Command, Packet, PacketInvalid
from ramses_tx.transport import (
    PacketProtocolFile,
    PacketProtocolPort,
    SerTransportPoll,
    SerTransportRead,
    _PacketProtocolT,
    _PacketTransportT,
    create_protocol_factory,
)

from .const import GWY_ID, __dev_mode__

DEV_MODE = __dev_mode__

RUNNING = True


_LOGGER = logging.getLogger(__name__)
if DEV_MODE:
    _LOGGER.setLevel(logging.DEBUG)


class MockSerial:  # most of the RF 'mocking' is done in here
    """A mocked serial port used for testing.

    Can periodically Rx a sync_cycle set that will be available via `read()`.
    Can use a response table to provide a known Rx for a given Tx sent via `write()`.
    """

    def __init__(
        self, port: str, loop: asyncio.AbstractEventLoop, **kwargs: Any
    ) -> None:
        self._loop = loop

        self.port = port
        self.portstr = port
        self._rx_buffer = bytes()
        self._out_waiting = 0
        self.is_open: bool = None  # type: ignore[assignment]

        # used in PacketProtocolPort (via serial.tools.list_ports.comports)
        self.name = port
        self.product = "evofw3 mocked"

        self.mock_devices: list = []  # list[MockDeviceBase]

        self._que: PriorityQueue = PriorityQueue(maxsize=24)
        self._rx_bytes_from_ether_task = self._loop.create_task(
            self._rx_bytes_from_ether()
        )

    def close(self, err=None):
        """Close the port."""
        if self.is_open is False:
            return
        self.is_open = False

        for device in self.mock_devices:
            for task in device._tasks:
                task.cancel()
        if task := self._rx_bytes_from_ether_task:
            task.cancel()

    @property
    def in_waiting(self) -> int:
        """Return the number of bytes currently in the input buffer."""
        return len(self._rx_buffer)

    def read_all(self) -> bytes:
        """Read max size bytes from the serial port."""
        return self.read(size=self.in_waiting)

    def read(self, size: int = 1) -> bytes:
        """Read max size bytes from the serial port."""
        data, self._rx_buffer = self._rx_buffer[:size], self._rx_buffer[size:]
        return data  # good place for a breakpoint

    # can log HGI Rx from RF in here...
    async def _rx_bytes_from_ether(self) -> None:
        """Poll the queue and add bytes to the Rx buffer.

        Also pass on the pkt header to any other devices on the ether.
        """

        self.is_open = True

        while RUNNING:
            await asyncio.sleep(0.001)

            try:
                priority, _, cmd = self._que.get_nowait()  # log HGI Rx here
            except Empty:
                continue

            if cmd.code != Code._PUZZ:  # Suggest breakpoint under here?
                pass

            # this is the mocked HGI80 receiving the frame
            if priority == 3:  # only from HGI80
                self._out_waiting -= len(str(cmd)) + 2
            self._rx_buffer += b"000 " + bytes(f"{cmd}\r\n", "ascii")

            # these are the mocked devices (if any) receiving the frame
            for device in self.mock_devices:
                if cmd.src.id == device.id:
                    continue
                if cmd.dst.id != device.id and cmd.code != Code._1FC9:
                    continue

                try:
                    device.rx_frame_as_cmd(cmd)
                except AssertionError as err:
                    _LOGGER.exception(err)

                except (AttributeError, TypeError, ValueError) as err:
                    _LOGGER.exception(err)

                except PacketInvalid as err:
                    _LOGGER.exception(err)

            cmd = None
            self._que.task_done()

    @property
    def out_waiting(self) -> int:
        """Return the number of bytes currently in the output buffer."""
        return self._out_waiting

    def write(self, data: bytes) -> int:
        """Output the given byte string over the serial port."""

        if data[:1] == b"!":  # an evofw3 flag
            return 0
        if data[7:16] == b"18:000730":
            data = data[:7] + bytes(GWY_ID, "ascii") + data[16:]
        try:
            self._tx_bytes_to_ether(data)  # good place for a breakpoint
        except PacketInvalid:
            pass
        return 0

    # can log HGI Tx to RF in here...
    def _tx_bytes_to_ether(self, data: bytes) -> None:
        """Transmit a packet from the gateway to the ether."""

        cmd = Command(data.decode("ascii")[:-2])  # rx_header

        if cmd.code != Code._PUZZ:  # Suggest breakpoint under here?
            pass

        try:
            self._que.put_nowait((3, dt.now(), cmd))  # log HGI Tx here
        except Full:
            return

        self._out_waiting += len(str(cmd)) + 2


class SerTransportMock(SerTransportPoll):  # can breakpoint in write()
    def write(self, cmd) -> None:
        if cmd[:1] == b"!" or b" 7FFF " in cmd:
            return

        super().write(cmd)  # good place for a breakpoint

    def close(self) -> None:
        """Gracefully close the mocked serial port."""
        super().close()

        if self.serial:
            self.serial.close()


class PacketProtocolMock(PacketProtocolPort):  # can breakpoint in _pkt_received()
    def _pkt_received(self, pkt: Packet) -> None:
        super()._pkt_received(pkt)  # good place for a breakpoint

    async def _send_impersonation_alert(self, cmd: Command) -> None:
        """Stifle impersonation alerts when mocking."""
        pass


def create_pkt_stack_new(  # to use a mocked Serial port (and a sympathetic Transport)
    gwy: Gateway, *args, **kwargs
) -> tuple[_PacketProtocolT, _PacketTransportT]:
    from protocol.protocol import create_stack

    # with patch(
    #     "ramses_tx.transport.serial_for_url",
    #     return_value=MockSerial(gwy.ser_name, loop=gwy._loop),
    # ):
    return create_stack(gwy, *args, **kwargs)


def create_pkt_stack(  # to use a mocked Serial port (and a sympathetic Transport)
    gwy,
    pkt_callback: Callable[[Packet], None],
    /,
    *,
    protocol_factory: Callable[[], _PacketProtocolT] = None,
    port_name: str = None,
    port_config: dict = None,
    packet_log: str = None,  # just the file path, must open
    packet_dict: dict = None,
) -> tuple[_PacketProtocolT, _PacketTransportT]:
    """Return a mocked packet stack.

    Must use SerTransportPoll and not SerTransportAsync.
    """

    def get_serial_instance(
        ser_name: str, loop: asyncio.AbstractEventLoop
    ) -> MockSerial:
        return MockSerial(ser_name, loop)

    def protocol_factory_() -> _PacketProtocolT:
        if packet_log or packet_dict is not None:
            return create_protocol_factory(PacketProtocolFile, gwy, pkt_callback)()
        return create_protocol_factory(PacketProtocolMock, gwy, pkt_callback)()

    if len([x for x in (packet_dict, packet_log, port_name) if x is not None]) != 1:
        raise TypeError("serial port, log file & dict should be mutually exclusive")

    pkt_protocol = protocol_factory_()

    if (pkt_source := packet_log or packet_dict) is not None:  # {} is a processable log
        return pkt_protocol, SerTransportRead(gwy._loop, pkt_protocol, pkt_source)

    assert port_name is not None  # instead of: type: ignore[arg-type]
    ser_instance = get_serial_instance(port_name, gwy._loop)

    return pkt_protocol, SerTransportMock(gwy._loop, pkt_protocol, ser_instance)
