import asyncio
import contextlib
from typing import Any

import pytest

from ramses_rf.config import GatewayConfig
from ramses_rf.gateway import Gateway
from ramses_rf.schemas import SCH_GLOBAL_CONFIG, strip_and_map_traits
from ramses_rf.typing import DeviceIdT


@pytest.mark.asyncio
async def test_polling_schema_traits_parsing() -> None:
    # ARRANGE
    # Trait dict with ramses_cc extension keys (_polling_interval, _is_battery)
    raw_known_list: dict[str, dict[str, Any]] = {
        "01:111111": {
            "class": "CTL",
            "_polling_interval": {"1F41": 3600, "10E0": 600},
            "_is_battery": False,
        },
        "04:222222": {
            "class": "TRV",
            "_polling_interval": None,
            "_is_battery": True,
        },
    }

    # ACT
    mapped_traits = {
        dev_id: strip_and_map_traits(traits)
        for dev_id, traits in raw_known_list.items()
    }

    config = GatewayConfig(known_list=mapped_traits)
    loop = asyncio.get_running_loop()
    gateway = Gateway(port_name="/dev/null", config=config, loop=loop)

    # ASSERT
    # Layer 7 traits mapped cleanly
    ctl_dev = gateway.device_registry.get_device(DeviceIdT("01:111111"))
    trv_dev = gateway.device_registry.get_device(DeviceIdT("04:222222"))

    assert ctl_dev.polling_interval == {"1F41": 3600, "10E0": 600}
    assert ctl_dev.is_battery is False

    assert trv_dev.polling_interval is None
    assert trv_dev.is_battery is True

    # CLEANUP
    with contextlib.suppress(asyncio.CancelledError):
        await gateway.stop()


@pytest.mark.asyncio
async def test_disable_polling_config_alias() -> None:
    # ARRANGE
    config_dict = {
        "config": {
            "disable_polling": True,
        }
    }

    # ACT
    parsed = SCH_GLOBAL_CONFIG(config_dict)
    config = GatewayConfig(disable_polling=parsed["config"]["disable_polling"])

    # ASSERT
    assert config.disable_polling is True
    # Test backward compatibility alias property
    assert config.disable_discovery is True

    config.disable_discovery = False
    assert config.disable_polling is False
