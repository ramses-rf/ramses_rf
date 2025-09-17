#!/usr/bin/env python3
"""RAMSES RF - Test the Schema processor."""

import json
from pathlib import Path

import pytest

from ramses_rf import Gateway
from ramses_rf.helpers import shrink
from ramses_rf.schemas import load_schema

from .helpers import (
    TEST_DIR,
    gwy,  # noqa: F401
    shuffle_dict,
)

WORK_DIR = f"{TEST_DIR}/schemas"


@pytest.mark.parametrize(
    "f_name", [f.stem for f in Path(f"{WORK_DIR}/log_files").glob("*.log")]
)
async def test_schema_discover_from_log(f_name: Path) -> None:
    path = f"{WORK_DIR}/log_files/{f_name}.log"
    gwy = Gateway(None, input_file=path, config={})  # noqa: F811
    await gwy.start()  # this is what we're testing
    await gwy.stop()

    with open(f"{WORK_DIR}/log_files/{f_name}.json") as f:
        schema = json.load(f)

        assert shrink(gwy.schema) == shrink(schema)

        gwy.ser_name = "/dev/null"  # HACK: needed to pause engine
        schema, packets = gwy.get_state(include_expired=True)
        packets = shuffle_dict(packets)
        await gwy._restore_cached_packets(packets)

        assert shrink(gwy.schema) == shrink(schema)


@pytest.mark.parametrize(
    "f_name", [f.stem for f in Path(f"{WORK_DIR}/jsn_files").glob("*.json")]
)
async def test_schema_load_from_json(gwy: Gateway, f_name: Path) -> None:  # noqa: F811
    with open(f"{WORK_DIR}/jsn_files/{f_name}.json") as f:
        schema = json.load(f)

    load_schema(gwy, **schema)

    # print(json.dumps(schema, indent=4))
    # print(json.dumps(self.gwy.schema, indent=4))

    assert shrink(gwy.schema) == shrink(schema)
