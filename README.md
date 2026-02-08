![Linting](https://github.com/ramses-rf/ramses_rf/actions/workflows/check-lint.yml/badge.svg)
![Typing](https://github.com/ramses-rf/ramses_rf/actions/workflows/check-type.yml/badge.svg)
![Testing](https://github.com/ramses-rf/ramses_rf/actions/workflows/check-test.yml/badge.svg)
[![Coverage](https://github.com/ramses-rf/ramses_rf/actions/workflows/check-cov.yml/badge.svg?event=push)](https://github.com/ramses-rf/ramses_rf/actions/workflows/check-cov.yml)

## Overview

**ramses_rf** is a Python client library/CLI utility used to interface with some Honeywell-compatible HVAC & CH/DHW systems that use 868MHz RF, such as:
 - (Heat) **evohome**, **Sundial**, **Hometronic**, **Chronotherm**
 - (HVAC) **Itho**, **Orcon**, **Nuaire**, **Vasco**, **ClimaRad**

Ramses RF can **not** interpret the new Honeywell Ramses-III (R3) messages used after a firmware upgrade since 2025 and (some) new devices.

It requires a USB-to-RF device, either a Honeywell HGI80 (somewhat rare, expensive) or a USB/MQTT dongle running the [ramses_esp](https://github.com/IndaloTech/ramses_esp) or [evofw3](https://github.com/ghoti57/evofw3) firmware, such as the one from [here](https://indalo-tech.onlineweb.shop/) or your own ESP32-S3-WROOM-1 N16R8 with a CC1100 transponder.

It does four things:
 - decodes RAMSES II-compatible packets and converts them into useful JSON
 - builds a picture (schema, config & state) of evohome-compatible CH/DHW systems - either passively (by eavesdropping), or actively (probing)
 - allows you to send commands to CH/DHW and HVAC systems, or monitor them for state changes
 - allows you to emulate some hardware devices (remotes)

> [!WARNING]
> This library is not affiliated with Honeywell, Airios nor any final manufacturer. The developers take no responsibility for anything that may happen to your devices because of this library.

For CH/DHW, the simplest way to know if it will work with your system is to identify the box connected to your boiler/HVAC appliance as one of:
 - **R8810A**: OpenTherm Bridge
 - **BDR91A**: Wireless Relay (also BDR91T)
 - **HC60NG**: Wireless Relay (older hardware)

Other systems may well work, such as some Itho Daalderop HVAC systems, use this protocol. YMMV.

This library includes a CLI and can be used as a standalone tool, but also is used as a client library by:
 - [ramses_cc](https://github.com/ramses-rf/ramses_cc), a Home Assistant integration
 - [evohome-Listener](https://github.com/smar000/evohome-Listener), an MQTT gateway

## Installation

To use the `ramses_cc` Integration in Home Assistant, just install `Ramses RF` from HACS. It will take care of installing this library. See the [`Ramses_cc wiki`](https://github.com/ramses-rf/ramses_cc/wiki/1.-Installation) for details.

### Ramses_rf CLI

To install the `ramses_rf` command line client:
```
git clone https://github.com/ramses-rf/ramses_rf
cd ramses_rf
pip install -r requirements/requirements.txt
pip install -e .
```

The CLI is called `client.py` and is included in the code root.
It has options to monitor and parse Ramses-II traffic to screen or a log file, and to parse a file containing Ramses-II messages to the screen.
See the [client.py CLI wiki page](https://github.com/ramses-rf/ramses_rf/wiki/2.-The-client.py-command-line) for instructions.

For code development, some more setup is required. Please follow the steps in our [Developer's Resource](README-developers.md)
