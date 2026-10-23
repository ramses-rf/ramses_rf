#!/usr/bin/env python3
"""Fixtures for testing."""

import logging
import os
from collections.abc import AsyncGenerator
from dataclasses import fields
from pathlib import Path
from typing import Any, Final, NoReturn, TypedDict, cast
from unittest.mock import patch

import pytest
import serialx as ser

from ramses_rf import Gateway
from ramses_rf.devices import HgiGateway
from ramses_rf.gateway import GatewayConfig
from ramses_tx import exceptions as exc
from ramses_tx.address import HGI_DEVICE_ID
from ramses_tx.config import EngineConfig
from ramses_tx.discovery import comports
from ramses_tx.transport.port import PortTransport
from ramses_tx.typing import DeviceIdT
from tests_rf.virtual_rf import HgiFwTypes, VirtualRf

#
type PortStrT = str

# TEST_DIR = f"{os.path.dirname(__file__)}"
TEST_DIR = Path(__file__).resolve().parent

SZ_GWY_CONFIG: Final = "gwy_config"
SZ_GWY_DEV_ID: Final = "gwy_dev_id"


class _ConfigDictT(TypedDict):
    disable_discovery: bool
    disable_qos: bool
    enforce_known_list: bool


class _GwyConfigDictT(TypedDict):
    config: _ConfigDictT
    known_list: dict[DeviceIdT, dict[str, bool]]


_LOGGER = logging.getLogger(__name__)


IN_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"
# set -x GITHUB_ACTIONS true
# set -u GITHUB_ACTIONS

_global_failed_ports: list[str] = []


###############################################################################

# pytestmark = pytest.mark.asyncio(scope="function")  # needed?


@pytest.fixture(autouse=True)
def patches_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ramses_tx.protocol.core._DBG_DISABLE_IMPERSONATION_ALERTS", True
    )
    monkeypatch.setattr(
        "ramses_tx.transport.port._DBG_DISABLE_DUTY_CYCLE_LIMIT", True
    )
    monkeypatch.setattr("ramses_tx.transport.port.MIN_INTER_WRITE_GAP", 0)


# TODO: add teardown to cleanup orphan MessageStore thread
# @pytest.fixture(scope="session", autouse=True)
# def close_timer_threads(request: pytest.FixtureRequest) -> None:
#     import threading

#     def finalize() -> None:
#         running_timer_threads = [
#             thread
#             for thread in threading.enumerate()
#             if isinstance(thread, threading.Timer)
#         ]
#         for timer in running_timer_threads:
#             timer.cancel()

#     request.addfinalizer(finalize)


###############################################################################


@pytest.fixture()
async def rf() -> AsyncGenerator[VirtualRf, None]:
    """Utilize a virtual evofw3-compatible gateway."""

    rf = VirtualRf(2)

    try:
        yield rf
    finally:
        await rf.stop()


@pytest.fixture()
def fake_evofw3_port(
    request: pytest.FixtureRequest, rf: VirtualRf
) -> PortStrT | None:
    """Utilize a virtual evofw3-compatible gateway.

    Requires test to supply the gwy_dev_id fixture.
    """

    gwy_dev_id: DeviceIdT = request.getfixturevalue(SZ_GWY_DEV_ID)

    rf.set_gateway(rf.ports[0], gwy_dev_id, fw_type=HgiFwTypes.EVOFW3)

    return rf.ports[0]


@pytest.fixture()
def fake_ti3410_port(
    request: pytest.FixtureRequest, rf: VirtualRf
) -> PortStrT | None:
    """Utilize a virtual HGI80-compatible gateway.

    Requires test to supply the gwy_dev_id fixture.
    """

    gwy_dev_id: DeviceIdT = request.getfixturevalue(SZ_GWY_DEV_ID)

    rf.set_gateway(rf.ports[0], gwy_dev_id, fw_type=HgiFwTypes.HGI_80)

    return rf.ports[0]


@pytest.fixture()  # TODO: remove HACK, below
async def mqtt_evofw3_port() -> PortStrT:
    """Utilize an actual evofw3-compatible gateway."""

    pytest.skip("This test fixture requires an MQTT broker")

    return "mqtt://mqtt_username:mqtt_passw0rd@127.0.0.1"  # type: ignore[unreachable]


@pytest.fixture()  # TODO: remove HACK, below
async def real_evofw3_port() -> PortStrT | NoReturn:
    """Utilize an actual evofw3-compatible gateway."""

    if IN_GITHUB_ACTIONS:  # replace with your condition
        pytest.skip("This test fixture requires physical hardware")

    port_names: list[PortStrT] = [
        p.device for p in comports() if p.product and "evofw3" in p.product
    ]

    if port_names:
        return port_names[0]

    if port_names := [
        p.device
        for p in comports()
        if os.path.basename(p.device)[:6] == "ttyACM"
    ]:  # HACK: evofw3-esp
        _LOGGER.warning("Assuming %s is evofw3-compatible", port_names[0])
        return port_names[0]

    pytest.skip("No evofw3-based gateway device found")


@pytest.fixture()
async def real_ti3410_port() -> PortStrT | NoReturn:
    """Utilize an actual HGI80-compatible gateway."""

    if IN_GITHUB_ACTIONS:  # replace with your condition
        pytest.skip("This test fixture requires physical hardware")

    port_names: list[PortStrT] = [
        p.device for p in comports() if p.product and "TUSB3410" in p.product
    ]

    if port_names:
        return port_names[0]

    pytest.skip("No ti3410-based gateway device found")


###############################################################################


async def _gateway(gwy_port: PortStrT, gwy_config: _GwyConfigDictT) -> Gateway:
    """Instantiate a gateway."""

    kwargs = cast(dict[str, Any], dict(gwy_config))

    # Flatten gwy_config to handle nested 'config' dicts or GatewayConfig
    # objects
    if "config" in kwargs:
        cfg = kwargs.pop("config")
        cfg_dict = cast(
            dict[str, Any], cfg if isinstance(cfg, dict) else vars(cfg)
        )
        # Outer kwargs take precedence over nested config values
        kwargs = {**cfg_dict, **kwargs}

    valid_gwy_keys = {f.name for f in fields(GatewayConfig)}
    valid_eng_keys = {f.name for f in fields(EngineConfig)}

    gwy_kwargs: dict[str, Any] = {}
    eng_kwargs: dict[str, Any] = {}

    for k, v in kwargs.items():
        if k in valid_gwy_keys:
            gwy_kwargs[k] = v
        elif k in valid_eng_keys:
            eng_kwargs[k] = v
        else:
            gwy_kwargs[k] = v

    if eng_kwargs:
        gwy_kwargs["engine"] = EngineConfig(**eng_kwargs)

    config = GatewayConfig(**gwy_kwargs)

    gwy = Gateway(gwy_port, config=config)

    assert gwy.hgi is None and gwy.device_registry.devices == []

    await gwy.start()
    return gwy


async def _fake_gateway(
    gwy_port: PortStrT, gwy_config: _GwyConfigDictT, rf: VirtualRf
) -> Gateway:
    """Wrapper to instantiate a virtual gateway."""

    with (
        patch("ramses_tx.discovery.comports", rf.comports),
        patch("serialx.async_list_serial_ports", rf.async_comports),
    ):
        gwy = await _gateway(gwy_port, gwy_config)

    assert gwy._engine._transport  # mypy
    # Cast to PortTransport to access concrete implementation details
    transport = cast(PortTransport, gwy._engine._transport)
    transport._extra["virtual_rf"] = rf

    return gwy


async def _real_gateway(
    gwy_port: PortStrT, gwy_config: _GwyConfigDictT
) -> Gateway:
    """Wrapper to instantiate a physical gateway."""

    global _global_failed_ports

    if gwy_port in _global_failed_ports:
        pytest.skip(f"Port {gwy_port} previously failed")

    try:
        return await _gateway(gwy_port, gwy_config)
    except (ser.SerialException, exc.TransportError) as err:
        _global_failed_ports.append(gwy_port)
        # not skip, as we had determined port exists elsewhere
        pytest.xfail(str(err))


@pytest.fixture()
async def fake_evofw3(
    fake_evofw3_port: PortStrT, request: pytest.FixtureRequest, rf: VirtualRf
) -> AsyncGenerator[Gateway, None]:
    """Utilize a virtual evofw3-compatible gateway (discovered by
    fake_evofw3_port).

    Requires test to supply gwy_config & gwy_dev_id (used by fake_evofw3_port)
    fixtures.
    """

    gwy_config: _GwyConfigDictT = request.getfixturevalue(SZ_GWY_CONFIG)
    gwy_dev_id: DeviceIdT = request.getfixturevalue(SZ_GWY_DEV_ID)

    gwy = await _fake_gateway(fake_evofw3_port, gwy_config, rf)

    assert isinstance(gwy.hgi, HgiGateway) and gwy.hgi.id == gwy_dev_id
    assert gwy._engine._protocol._is_evofw3 is True

    try:
        yield gwy
    finally:
        await gwy.stop()


@pytest.fixture()
async def fake_ti3410(
    fake_ti3410_port: PortStrT, request: pytest.FixtureRequest, rf: VirtualRf
) -> AsyncGenerator[Gateway, None]:
    """Utilize a virtual HGI80-compatible gateway (discovered by
    fake_ti3410_port).

    Requires test to supply gwy_config & gwy_dev_id (used by fake_ti3410_port)
    fixtures.
    """

    gwy_config: _GwyConfigDictT = request.getfixturevalue(SZ_GWY_CONFIG)
    gwy_dev_id: DeviceIdT = request.getfixturevalue(SZ_GWY_DEV_ID)

    gwy = await _fake_gateway(fake_ti3410_port, gwy_config, rf)

    assert isinstance(gwy.hgi, HgiGateway) and gwy.hgi.id == gwy_dev_id
    assert gwy._engine._protocol._is_evofw3 is False

    try:
        yield gwy
    finally:
        await gwy.stop()


@pytest.fixture()
async def mqtt_evofw3(
    mqtt_evofw3_port: PortStrT, request: pytest.FixtureRequest
) -> AsyncGenerator[Gateway, None]:
    """Utilize an actual evofw3-compatible gateway (discovered by
    mqtt_evofw3_port).

    Requires test to supply corresponding gwy_config fixture.
    """

    # TODO: pytest.skip() if the MQTT broker is available
    gwy_config: _GwyConfigDictT = request.getfixturevalue(SZ_GWY_CONFIG)
    gwy = await _gateway(mqtt_evofw3_port, gwy_config)

    # HACK: not instantiated: no puzzle packets sent
    gwy.device_registry.get_device(gwy._engine._protocol.hgi_id)

    assert isinstance(gwy.hgi, HgiGateway) and gwy.hgi.id not in (
        None,
        HGI_DEVICE_ID,
    )
    assert gwy._engine._protocol._is_evofw3 is True

    try:
        yield gwy
    finally:
        await gwy.stop()


@pytest.fixture()
async def real_evofw3(
    real_evofw3_port: PortStrT, request: pytest.FixtureRequest
) -> AsyncGenerator[Gateway, None]:
    """Utilize an actual evofw3-compatible gateway (discovered by
    real_evofw3_port).

    Requires test to supply corresponding gwy_config fixture.
    """

    gwy_config: _GwyConfigDictT = request.getfixturevalue(SZ_GWY_CONFIG)

    gwy = await _real_gateway(real_evofw3_port, gwy_config)

    assert isinstance(gwy.hgi, HgiGateway) and gwy.hgi.id not in (
        None,
        HGI_DEVICE_ID,
    )
    assert gwy._engine._protocol._is_evofw3 is True

    try:
        yield gwy
    finally:
        await gwy.stop()


@pytest.fixture()
async def real_ti3410(
    real_ti3410_port: PortStrT, request: pytest.FixtureRequest
) -> AsyncGenerator[Gateway, None]:
    """Utilize an actual HGI80-compatible gateway (discovered by
    real_ti3410_port).

    Requires test to supply corresponding gwy_config fixture.
    """

    gwy_config: _GwyConfigDictT = request.getfixturevalue(SZ_GWY_CONFIG)

    gwy = await _real_gateway(real_ti3410_port, gwy_config)

    assert isinstance(gwy.hgi, HgiGateway) and gwy.hgi.id not in (
        None,
        HGI_DEVICE_ID,
    )
    assert gwy._engine._protocol._is_evofw3 is False

    try:
        yield gwy
    finally:
        await gwy.stop()
