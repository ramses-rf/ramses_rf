#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser."""

import asyncio
import json
import re
import warnings
from collections.abc import AsyncGenerator, Callable
from dataclasses import fields
from pathlib import Path
from random import shuffle
from typing import Any

import pytest
import voluptuous as vol

from ramses_rf import Gateway
from ramses_rf.config import SCH_GLOBAL_TRAITS_DICT
from ramses_rf.gateway import GatewayConfig
from ramses_rf.helpers import shrink
from ramses_rf.schemas import SCH_GLOBAL_CONFIG, SCH_GLOBAL_SCHEMAS
from ramses_rf.state import MessageStore
from ramses_tx.config import EngineConfig

SCH_GLOBAL_TRAITS = vol.Schema(SCH_GLOBAL_TRAITS_DICT, extra=vol.PREVENT_EXTRA)

# import tracemalloc
# tracemalloc.start()

warnings.filterwarnings("ignore", category=DeprecationWarning)

# usu. WARNING  # TODO: Verify original intent. Commented out as it breaks
# isolated logging logic in PR #413.
# logging.disable(logging.WARNING)


# TEST_DIR = f"{os.path.dirname(__file__)}"
TEST_DIR = Path(__file__).resolve().parent


def shuffle_dict(old_dict: dict[str, Any]) -> dict[str, Any]:
    """Return a dictionary with its keys shuffled.

    :param old_dict: The dictionary to shuffle.
    :type old_dict: dict
    :return: A new dictionary with shuffled keys.
    :rtype: dict
    """
    keys = list(old_dict.keys())
    shuffle(keys)
    new_dict: dict[str, Any] = dict()
    for key in keys:
        new_dict.update({key: old_dict[key]})
    return new_dict


@pytest.fixture
# NOTE: async to get running loop
async def gwy() -> AsyncGenerator[Gateway, None]:
    """Return a vanilla system (with a known, minimal state)."""
    gwy = Gateway("/dev/null", config=GatewayConfig())
    gwy._engine._disable_sending = True
    gwy.message_store = MessageStore()  # required to add heat dummy 3220 msg
    try:
        yield gwy
    finally:
        await gwy.stop()  # close sqlite3 connection


def assert_expected(
    actual: dict[str, Any], expected: dict[str, Any] | None = None
) -> None:
    """Compare an actual system state dict against the corresponding
    expected state.

    :param actual: The actual state dictionary.
    :type actual: dict[str, Any]
    :param expected: The expected state dictionary, defaults to None.
    :type expected: dict[str, Any] | None, optional
    :return: None
    :rtype: None
    """

    def _assert_expected(actual_: dict[str, Any], expect_: dict[str, Any]) -> None:
        if actual_ != expect_:
            print("\n--- DIAGNOSTIC OUTPUT ---")
            for k, v in actual_.items():
                if k not in expect_:
                    print(f"EXTRA: {k} = {v}")
                elif v != expect_[k]:
                    print(f"MISMATCH {k}: Act={v}, Exp={expect_[k]}")

            for k in expect_:
                if k not in actual_:
                    print(f"MISSING {k}: Exp={expect_[k]}")
            print("-------------------------\n")

        assert actual_ == expect_

    if expected:
        _assert_expected(shrink(actual), shrink(expected))


async def assert_expected_set(gwy: Gateway, expected: dict[str, Any]) -> None:
    """Compare the actual system state against the expected system
    state.

    :param gwy: The gateway instance to check.
    :type gwy: Gateway
    :param expected: The expected state dictionary.
    :type expected: dict
    :return: None
    :rtype: None
    """

    assert_expected(await gwy.schema(), expected.get("schema"))
    assert_expected(await gwy.params(), expected.get("params"))
    assert_expected(await gwy.status(), expected.get("status"))
    assert_expected(await gwy.device_registry.known_list(), expected.get("known_list"))


def assert_raises(
    exception: type[Exception], fnc: Callable[..., Any], *args: Any
) -> None:
    """Assert that a function raises a specific exception.

    :param exception: The exception class expected to be raised.
    :type exception: type[Exception]
    :param fnc: The function to call.
    :type fnc: Callable[..., Any]
    :param args: Arguments to pass to the function.
    :type args: Any
    :return: None
    :rtype: None
    """
    try:
        fnc(*args)
    except exception:  # as err:
        pass  # or: assert True
    else:
        assert False


def shrink_dict(old_dict: Any, keys: tuple[str, ...]) -> Any:
    """Return a dictionary recursively containing only certain keys.

    :param old_dict: The dictionary to filter.
    :type old_dict: Any
    :param keys: The keys to keep.
    :type keys: tuple[str, ...]
    :return: The filtered dictionary.
    :rtype: Any
    """

    if not isinstance(old_dict, dict):
        return old_dict

    new_dict: dict[str, Any] = {}
    for k, v in old_dict.items():
        if k in keys:
            if isinstance(v, dict):
                new_dict[k] = shrink_dict(v, keys)
            elif isinstance(v, list):
                new_dict[k] = [shrink_dict(i, keys) for i in v]
            else:
                new_dict[k] = v
    return new_dict


async def test_ports() -> dict[str, Any]:
    """Test the comports."""
    import serial.tools.list_ports

    return {
        p.device: p.description
        for p in serial.tools.list_ports.comports()
        if p.description != "n/a"
    }


def find_test_tcs(gwy: Gateway | tuple[Gateway, ...]) -> Any:
    """Return the controller of the main TCS.

    :param gwy: The gateway instance.
    :type gwy: Gateway | tuple[Gateway, ...]
    :return: The system object.
    :rtype: Any
    """
    if isinstance(gwy, tuple):
        gwy = gwy[0]

    try:
        return gwy.tcs
    except IndexError:
        return None


async def load_test_gwy(dir_name: Path, **kwargs: Any) -> Gateway:
    """Create a system state from a packet log (using an optional
    configuration).

    :param dir_name: The directory containing config.json and packet.log.
    :type dir_name: Path
    :param kwargs: Additional configuration overrides.
    :type kwargs: Any
    :return: The initialized gateway instance.
    :rtype: Gateway
    """
    kwargs = SCH_GLOBAL_CONFIG({k: v for k, v in kwargs.items() if k[:1] != "_"})

    try:
        with open(f"{dir_name}/config.json") as f:
            config = json.load(f)
    except FileNotFoundError:
        config = {}

    if config:
        kwargs.update(config)

    config_dict = kwargs.pop("config", {})
    config_kwargs = {**config_dict, **kwargs}
    config_kwargs["input_file"] = f"{dir_name}/packet.log"

    valid_gwy_keys = {f.name for f in fields(GatewayConfig)}
    valid_eng_keys = {f.name for f in fields(EngineConfig)}

    gwy_kwargs: dict[str, Any] = {}
    eng_kwargs: dict[str, Any] = {}
    schema_kwargs = config_kwargs.pop("schema", {})

    for k, v in config_kwargs.items():
        if k.startswith("_"):
            continue
        if k in valid_gwy_keys:
            gwy_kwargs[k] = v
        elif k in valid_eng_keys:
            eng_kwargs[k] = v
        elif k in (
            "main_tcs",
            "orphans",
            "orphans_heat",
            "orphans_hvac",
            "system",
        ) or re.match(r"^[0-9]{2}:[0-9]{6}$", k):
            schema_kwargs[k] = v

    if schema_kwargs:
        gwy_kwargs["schema"] = schema_kwargs

    if eng_kwargs:
        gwy_kwargs["engine"] = EngineConfig(**eng_kwargs)

    gwy = Gateway(None, config=GatewayConfig(**gwy_kwargs))
    await gwy.start()

    # The Gateway with input_file uses a Transport that processes the file
    # automatically. We simply need to wait for the transport to finish
    # reading the file.
    # We pause discovery/sending during replay to avoid side effects.
    await gwy._engine._protocol.wait_for_connection_lost()

    dispatcher = getattr(gwy, "dispatcher", None)
    if dispatcher:
        if hasattr(dispatcher, "discovery_queue"):
            await dispatcher.discovery_queue.join()
        if hasattr(dispatcher, "ssot_queue"):
            await dispatcher.ssot_queue.join()
        if hasattr(dispatcher, "binding_fsm_queue"):
            await dispatcher.binding_fsm_queue.join()
    await asyncio.sleep(0.01)

    # Ensure all packets from the log are written to the DB before returning
    # This is critical for tests using the StorageWorker
    if gwy.message_store:
        gwy.message_store.flush()

    return gwy


def load_expected_results(dir_name: Path) -> dict[str, Any]:
    """Return the expected (global) schema/params/status & traits
    (aka known_list).

    :param dir_name: The directory containing the JSON result files.
    :type dir_name: Path
    :return: A dictionary containing the expected schema, known_list, params, and status.
    :rtype: dict[str, Any]
    """

    try:
        with open(f"{dir_name}/schema.json") as f:
            schema = json.load(f)
    except FileNotFoundError:
        schema = {}
    schema = SCH_GLOBAL_SCHEMAS(schema)

    try:
        with open(f"{dir_name}/known_list.json") as f:
            known_list = json.load(f)["known_list"]
    except FileNotFoundError:
        known_list = {}

    known_list = SCH_GLOBAL_TRAITS({"known_list": shrink(known_list)})["known_list"]

    try:
        with open(f"{dir_name}/params.json") as f:
            params = json.load(f)["params"]
    except FileNotFoundError:
        params = {}

    try:
        with open(f"{dir_name}/status.json") as f:
            status = json.load(f)["status"]
    except FileNotFoundError:
        status = {}

    return {
        "schema": schema,
        "known_list": known_list,
        "params": params,
        "status": status,
    }
