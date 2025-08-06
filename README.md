![Linting](https://github.com/ramses-rf/ramses_rf/actions/workflows/check-lint.yml/badge.svg)
![Typing](https://github.com/ramses-rf/ramses_rf/actions/workflows/check-type.yml/badge.svg)
![Testing](https://github.com/ramses-rf/ramses_rf/actions/workflows/check-test.yml/badge.svg)

## Overview

**ramses_rf** is a Python client library/CLI utility used to interface with some Honeywell-compatible HVAC & CH/DHW systems that use 868MHz RF, such as:
 - (Heat) **evohome**, **Sundial**, **Hometronic**, **Chronotherm**
 - (HVAC) **Itho**, **Orcon**, **Nuaire**, **Vasco**, **ClimaRad**

It requires a USB-to-RF device, either a Honeywell HGI80 (somewhat rare, expensive) or something running the [evofw3](https://github.com/ghoti57/evofw3) firmware, such as the one from [here](https://indalo-tech.onlineweb.shop/) or your own ESP32-S3-WROOM-1 N16R8 with a CC1100 transponder.

It does four things:
 - decodes RAMSES II-compatible packets and converts them into useful JSON
 - builds a picture (schema, config & state) of evohome-compatible CH/DHW systems - either passively (by eavesdropping), or actively (probing)
 - allows you to send commands to CH/DHW and HVAC systems, or monitor for state changes
 - allows you to emulate some hardware devices

> [!WARNING]
> This integration is not affiliated in any way with Honeywell, Airios nor any final manufacturer. The developers take no responsibility for anything that may happen to your devices because of this library.

For CH/DHW, the simplest way to know if it will work with your system is to identify the box connected to your boiler/HVAC appliance as one of:
 - **R8810A**: OpenTherm Bridge
 - **BDR91A**: Wireless Relay (also BDR91T)
 - **HC60NG**: Wireless Relay (older hardware)

Other systems may well work, such as some Itho Daalderop HVAC systems, use this protocol, YMMV.

It includes a CLI and can be used as a standalone tool, but also is used as a client library by:
 - [ramses_cc](https://github.com/ramses-rf/ramses_cc), a Home Assistant integration
 - [evohome-Listener](https://github.com/smar000/evohome-Listener), an MQTT gateway

## Installation

To use the `ramses_rf` Integration in Home Assistant, just install `Ramses RF` from HACS. It will take care of installing this library. See the [`Ramses_cc wiki`](https://github.com/ramses-rf/ramses_cc/wiki/1.-Installation) for details.

To run the `ramses_rf` client:
```
git clone https://github.com/ramses-rf/ramses_rf
cd ramses_rf
pip install -r requirements.txt
```

For development, more setup is required. Please follow the steps in our [Developer's Resource](README-developers.md)

## Ramses_rf CLI

The CLI is called ``client.py`` and is included in the code root.

For example, to monitor ramses_rf messages picked up by a dongle connected to port USB0, and log them in `packet.log`:
```
python client.py monitor /dev/ttyUSB0 -o packet.log
```
To view the `client.py` help:
```
python client.py --help
```

To send a command to a device, type:
```
python client.py execute /dev/ttyUSB0 -x "_verb [seqn] addr0 [addr1 [addr2]] code payload"
```
Notes:
- Before the `I` verb, add a whitespace inside the opening double quote: [RP]|[RQ]|[ I]
- Skip empty device addresses;
- Don't enter the packet length.

Send command example:
```
python3 client.py execute /dev/cu.usbmodemFD131 -x " I 29:091138 32:022222 22F1 000406"
```
See the [client.py Configuration wiki page](https://github.com/ramses-rf/ramses_rf/wiki/client.py-configuration-file) for more.
