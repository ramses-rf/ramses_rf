#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

WIP - Utility to a configuration file from JSON to YAML & back.
"""

import argparse
import json

import yaml

from ramses_rf.helpers import shrink
from ramses_rf.schemas import load_config

parser = argparse.ArgumentParser(description="Convert a file JSON <-> YAML")
parser.add_argument("-i", "--input-file", type=argparse.FileType("r"), default="-")
args = parser.parse_args()


def convert_json_to_yaml(data: dict) -> None:
    """Convert from json (client.py -C config.json) to yaml (HA configuration.yaml)."""
    (config, schema, include, exclude) = load_config("/dev/ttyMOCK", None, **data)

    config = vars(config)
    config["use_regex"]["inbound"].pop("( 03:.* 03:.* (1060|2389|30C9) 003) ..")  # HACK

    result = {
        "serial_port": "/dev/ttyMOCK",
        "packet_log": None,
        "restore_cache": False,
        "ramses_rf": shrink(config),
        "schema": shrink(schema),
        "known_list": {k: shrink(v) for k, v in include.items()},
        "block_list": shrink(exclude),
    }

    print(yaml.dump({"ramses_cc": result}, sort_keys=False))


def convert_yaml_to_json(data: dict) -> None:
    """Convert from yaml (HA configuration.yaml) to json (client.py -C config.json)."""

    result = data["ramses_cc"]
    result["config"] = result.pop("ramses_rf", {})
    result.update(result.pop("schema", {}))

    result["orphans_heat"] = {}
    result["orphans_hvac"] = {}
    result["known_list"] = result.pop("known_list", {})
    result["block_list"] = result.pop("block_list", {})

    print(json.dumps(result, indent=4))


data_file = args.input_file.read()

try:
    convert_json_to_yaml(json.loads(data_file))
except json.JSONDecodeError:
    convert_yaml_to_json(yaml.safe_load(data_file))
