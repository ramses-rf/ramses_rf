#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Test eavesdropping of a device class.
"""

import json
from pathlib import Path, PurePath

from ramses_rf import Gateway
from tests.helpers import TEST_DIR, assert_expected

WORK_DIR = f"{TEST_DIR}/eavesdrop_dev_class"


def id_fnc(param):
    return PurePath(param).name


def pytest_generate_tests(metafunc):
    folders = [f for f in Path(WORK_DIR).iterdir() if f.is_dir()]
    metafunc.parametrize("dir_name", folders, ids=id_fnc)


async def test_packets_from_log_file(dir_name):
    """Check eavesdropping of a src device _SLUG (from each packet line)."""

    def proc_log_line(msg):
        assert msg.src._SLUG in eval(msg._pkt.comment)

    with open(f"{dir_name}/packet.log") as f:
        gwy = Gateway(None, input_file=f, config={"enable_eavesdrop": False})
        gwy.config.enable_eavesdrop = True  # Test setting this config attr

        gwy.add_msg_handler(proc_log_line)

        try:
            await gwy.start()
        finally:
            await gwy.stop()


# duplicate in test_eavesdrop_schema
async def test_dev_eavesdrop_on_(dir_name):
    """Check discovery of schema and known_list *with* eavesdropping."""

    with open(f"{dir_name}/packet.log") as f:
        gwy = Gateway(None, input_file=f, config={"enable_eavesdrop": True})
        await gwy.start()

    with open(f"{dir_name}/known_list_eavesdrop_on.json") as f:
        assert_expected(gwy.known_list, json.load(f).get("known_list"))

    try:
        with open(f"{dir_name}/schema_eavesdrop_on.json") as f:
            assert_expected(gwy.schema, json.load(f))
    except FileNotFoundError:
        pass

    await gwy.stop()


# duplicate in test_eavesdrop_schema
async def test_dev_eavesdrop_off(dir_name):
    """Check discovery of schema and known_list *without* eavesdropping."""

    with open(f"{dir_name}/packet.log") as f:
        gwy = Gateway(None, input_file=f, config={"enable_eavesdrop": False})
        await gwy.start()

    try:
        with open(f"{dir_name}/known_list_eavesdrop_off.json") as f:
            assert_expected(gwy.known_list, json.load(f).get("known_list"))
    except FileNotFoundError:
        pass

    try:
        with open(f"{dir_name}/schema_eavesdrop_off.json") as f:
            assert_expected(gwy.schema, json.load(f))
    except FileNotFoundError:
        pass

    await gwy.stop()
