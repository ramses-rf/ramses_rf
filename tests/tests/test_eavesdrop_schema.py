#!/usr/bin/env python3
"""RAMSES RF - Test eavesdropping of a device class."""

import json
from pathlib import Path, PurePath

import pytest

from ramses_rf import Gateway

from .helpers import TEST_DIR, assert_expected, shuffle_dict

WORK_DIR = f"{TEST_DIR}/eavesdrop_schema"


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    def id_fnc(param: Path) -> str:
        return PurePath(param).name

    folders = [f for f in Path(WORK_DIR).iterdir() if f.is_dir() and f.name[:1] != "_"]
    folders.sort()
    metafunc.parametrize("dir_name", folders, ids=id_fnc)


async def assert_schemas_equal(gwy: Gateway, expected_schema: dict) -> None:
    """Check the gwy schema, then shuffle and test again."""

    schema, packets = gwy.get_state(include_expired=True)
    assert_expected(schema, expected_schema)

    packets = shuffle_dict(packets)
    await gwy._restore_cached_packets(packets)
    assert_expected(gwy.schema, expected_schema)


# duplicate in test_eavesdrop_dev_class
async def test_eavesdrop_off(dir_name: Path) -> None:
    """Check discovery of schema and known_list *without* eavesdropping."""

    path = f"{dir_name}/packet.log"
    gwy = Gateway(None, input_file=path, config={"enable_eavesdrop": False})
    await gwy.start()

    with open(f"{dir_name}/schema_eavesdrop_off.json") as f:
        await assert_schemas_equal(gwy, json.load(f))

    try:
        with open(f"{dir_name}/known_list_eavesdrop_off.json") as f:
            assert_expected(gwy.known_list, json.load(f).get("known_list"))
    except FileNotFoundError:
        pass

    await gwy.stop()


# duplicate in test_eavesdrop_dev_class
async def test_eavesdrop_on_(dir_name: Path) -> None:
    """Check discovery of schema and known_list *with* eavesdropping."""

    path = f"{dir_name}/packet.log"
    gwy = Gateway(None, input_file=path, config={"enable_eavesdrop": True})
    await gwy.start()

    with open(f"{dir_name}/schema_eavesdrop_on.json") as f:
        await assert_schemas_equal(gwy, json.load(f))

    try:
        with open(f"{dir_name}/known_list_eavesdrop_on.json") as f:
            assert_expected(gwy.known_list, json.load(f).get("known_list"))
    except FileNotFoundError:
        pass

    await gwy.stop()
