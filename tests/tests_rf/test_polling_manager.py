import asyncio
import contextlib
from datetime import UTC, datetime as dt, timedelta as td
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ramses_rf.config import GatewayConfig
from ramses_rf.const import Code, DevType, Verb
from ramses_rf.devices.dev_base import BatteryState, DeviceBase
from ramses_rf.exceptions import RamsesException
from ramses_rf.gateway import Gateway
from ramses_rf.models import DeviceTraits
from ramses_rf.pipeline.polling import (
    DEFAULT_POLLING_SCHEDULES,
    PollingManager,
)
from ramses_rf.protocol.opentherm import (
    OPENTHERM_PARAMS_DATA_IDS,
    OPENTHERM_POLL_DATA_IDS,
    OPENTHERM_STATUS_DATA_IDS,
    encode_opentherm_payload,
)
from ramses_rf.schemas import SCH_GLOBAL_CONFIG, strip_and_map_traits
from ramses_rf.typing import DeviceIdT
from ramses_tx import CommandDTO


class MockDevice(DeviceBase):
    """Mock device subclass for unit testing PollingManager schedule resolution."""

    def __init__(
        self,
        gwy: Any,
        device_id: str,
        slug: str = "DEFAULT",
        traits: DeviceTraits | None = None,
    ) -> None:
        mock_addr = MagicMock()
        mock_addr.id = device_id
        mock_addr.type = device_id[:2]
        super().__init__(gwy, mock_addr, traits=traits)
        self._SLUG = slug


class MockBatteryDevice(BatteryState):
    """Mock battery-powered device subclass for unit testing."""

    def __init__(
        self,
        gwy: Any,
        device_id: str,
        traits: DeviceTraits | None = None,
    ) -> None:
        mock_addr = MagicMock()
        mock_addr.id = device_id
        mock_addr.type = device_id[:2]
        super().__init__(gwy, mock_addr, traits=traits)
        self._SLUG = "TRV"


@pytest.fixture
def mock_gateway() -> MagicMock:
    """Create a mock gateway instance with mock device registry and config."""
    gwy = MagicMock()
    gwy.config = GatewayConfig()
    gwy.device_registry.devices = []
    gwy._async_send_dto = AsyncMock()
    gwy.add_task = MagicMock()
    return gwy


def test_polling_manager_schedule_resolution_defaults(
    mock_gateway: MagicMock,
) -> None:
    # ARRANGE
    poller = PollingManager(mock_gateway, shadow_mode=True)

    ctl_dev = MockDevice(mock_gateway, "01:111111", slug="CTL")
    bdr_dev = MockDevice(mock_gateway, "13:222222", slug="BDR")
    fan_dev = MockDevice(mock_gateway, "32:333333", slug="FAN")

    # ACT
    ctl_schedule = poller.resolve_schedule_for_device(ctl_dev)
    bdr_schedule = poller.resolve_schedule_for_device(bdr_dev)
    fan_schedule = poller.resolve_schedule_for_device(fan_dev)

    # ASSERT
    assert ctl_schedule == DEFAULT_POLLING_SCHEDULES["CTL"]
    assert bdr_schedule == DEFAULT_POLLING_SCHEDULES["BDR"]
    assert fan_schedule == DEFAULT_POLLING_SCHEDULES["FAN"]


def test_polling_manager_custom_trait_override(
    mock_gateway: MagicMock,
) -> None:
    # ARRANGE
    custom_traits = DeviceTraits(
        polling_interval={Code._1F41: 1800, Code._10E0: 43200}
    )
    ctl_dev = MockDevice(
        mock_gateway, "01:111111", slug="CTL", traits=custom_traits
    )
    poller = PollingManager(mock_gateway, shadow_mode=True)

    # ACT
    schedule = poller.resolve_schedule_for_device(ctl_dev)

    # ASSERT
    assert schedule[Code._1F41] == 1800
    assert schedule[Code._10E0] == 43200
    assert schedule[Code._313F] == DEFAULT_POLLING_SCHEDULES["CTL"][Code._313F]


@pytest.mark.parametrize("device_id", ["18:130140", "18:072981"])
def test_polling_manager_hgi_device_zero_polling(
    mock_gateway: MagicMock, device_id: str
) -> None:
    hgi = MockDevice(
        mock_gateway,
        device_id,
        slug=DevType.HGI,
        traits=DeviceTraits(polling_interval={Code._10E0: 60}),
    )
    poller = PollingManager(mock_gateway, shadow_mode=False)

    assert poller.resolve_schedule_for_device(hgi) == {}
    assert poller.update_device_tasks(hgi) == set()
    assert poller.get_scheduled_cmds() == []


def test_polling_manager_battery_device_zero_polling(
    mock_gateway: MagicMock,
) -> None:
    # ARRANGE
    battery_dev = MockBatteryDevice(mock_gateway, "04:222222")
    explicit_battery_dev = MockDevice(
        mock_gateway,
        "04:333333",
        slug="TRV",
        traits=DeviceTraits(is_battery=True),
    )
    poller = PollingManager(mock_gateway, shadow_mode=True)

    # ACT
    battery_schedule = poller.resolve_schedule_for_device(battery_dev)
    explicit_schedule = poller.resolve_schedule_for_device(
        explicit_battery_dev
    )

    # ASSERT
    # Battery devices sleep and do not listen to RF commands; schedule must be empty
    assert battery_schedule == {}
    assert explicit_schedule == {}


def test_polling_manager_faked_device_zero_polling(
    mock_gateway: MagicMock,
) -> None:
    """Faked devices are virtual and must never be polled.

    A faked REM (e.g. 37:999999 created via ramses_cc's add_faked_rem
    service) does not exist on the RF network.  Polling it with 10E0
    generates a 20s timeout and log spam (issue 1067).
    """
    # ARRANGE — a CTL that would normally get a 10E0 poll, but is faked
    faked_dev = MockDevice(mock_gateway, "01:111111", slug="CTL")
    faked_dev._binding_manager = MagicMock()  # is_faked → True

    # A faked HVAC device with no explicit class (DeviceHvac, HVC slug)
    # — this is the scenario where a faked REM's class trait was lost
    # during schema migration, leaving it as a generic DeviceHvac.
    faked_hvc_dev = MockDevice(mock_gateway, "37:999999", slug="HVC")
    faked_hvc_dev._binding_manager = MagicMock()  # is_faked → True

    poller = PollingManager(mock_gateway, shadow_mode=False)

    # ACT
    faked_schedule = poller.resolve_schedule_for_device(faked_dev)
    faked_hvc_schedule = poller.resolve_schedule_for_device(faked_hvc_dev)

    # ASSERT — faked devices get zero polling regardless of their slug
    assert faked_schedule == {}
    assert faked_hvc_schedule == {}

    # No tasks should be created for faked devices
    assert poller.update_device_tasks(faked_dev) == set()
    assert poller.update_device_tasks(faked_hvc_dev) == set()


@pytest.mark.asyncio
async def test_polling_manager_shadow_execution_parity(
    mock_gateway: MagicMock,
) -> None:
    # ARRANGE
    poller = PollingManager(mock_gateway, shadow_mode=True)
    ctl_dev = MockDevice(mock_gateway, "01:111111", slug="CTL")
    mock_gateway.device_registry.devices = [ctl_dev]

    poller.update_device_tasks(ctl_dev)

    # Fast-forward next_due to trigger due commands
    past = dt.now(UTC) - td(seconds=10)
    for task in poller.get_scheduled_cmds():
        task.next_due = past

    # ACT
    processed_count = await poller.poll_due_commands()

    # ASSERT
    assert processed_count == len(DEFAULT_POLLING_SCHEDULES["CTL"])
    # Crucial Shadow Mode Guarantee: Zero RF network transmissions
    mock_gateway._async_send_dto.assert_not_called()


@pytest.mark.asyncio
async def test_polling_manager_disabled_config_parity(
    mock_gateway: MagicMock,
) -> None:
    # ARRANGE
    mock_gateway.config.disable_polling = True
    poller = PollingManager(mock_gateway, shadow_mode=True)
    ctl_dev = MockDevice(mock_gateway, "01:111111", slug="CTL")
    mock_gateway.device_registry.devices = [ctl_dev]

    # ACT
    processed_count = await poller.poll_due_commands()

    # ASSERT
    assert processed_count == 0
    mock_gateway._async_send_dto.assert_not_called()


@pytest.mark.asyncio
async def test_polling_manager_stale_task_pruning(
    mock_gateway: MagicMock,
) -> None:
    # ARRANGE
    poller = PollingManager(mock_gateway, shadow_mode=True)
    ctl_dev = MockDevice(mock_gateway, "01:111111", slug="CTL")
    mock_gateway.device_registry.devices = [ctl_dev]

    # ACT
    await poller.poll_due_commands()
    initial_count = len(poller.get_scheduled_cmds())

    mock_gateway.device_registry.devices = []
    await poller.poll_due_commands()
    pruned_count = len(poller.get_scheduled_cmds())

    # ASSERT
    assert initial_count > 0
    assert pruned_count == 0


@pytest.mark.asyncio
async def test_polling_manager_send_cmd_exception_handling(
    mock_gateway: MagicMock,
) -> None:
    # ARRANGE
    poller = PollingManager(mock_gateway, shadow_mode=False)
    bdr_dev = MockDevice(mock_gateway, "13:222222", slug="BDR")
    mock_gateway.device_registry.devices = [bdr_dev]
    mock_gateway._async_send_dto.side_effect = RamsesException(
        "Transmission failure"
    )

    poller.update_device_tasks(bdr_dev)
    past = dt.now(UTC) - td(seconds=10)
    for task in poller.get_scheduled_cmds():
        task.next_due = past

    # ACT
    processed_count = await poller.poll_due_commands()

    # ASSERT
    assert processed_count == 1
    mock_gateway._async_send_dto.assert_called_once()


@pytest.mark.asyncio
async def test_polling_manager_rate_limiting(
    mock_gateway: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ARRANGE
    poller = PollingManager(mock_gateway, shadow_mode=False)
    ctl_dev = MockDevice(mock_gateway, "01:111111", slug="CTL")
    mock_gateway.device_registry.devices = [ctl_dev]

    poller.update_device_tasks(ctl_dev)
    past = dt.now(UTC) - td(seconds=10)
    tasks = poller.get_scheduled_cmds()
    for task in tasks:
        task.next_due = past

    sleep_calls: list[float] = []

    async def fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    # ACT
    processed_count = await poller.poll_due_commands()

    # ASSERT
    assert processed_count == len(tasks)
    assert len(sleep_calls) == len(tasks) - 1
    assert all(s == 0.5 for s in sleep_calls)


@pytest.mark.asyncio
async def test_polling_manager_live_dispatch_cutover(
    mock_gateway: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ARRANGE
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    poller = PollingManager(mock_gateway, shadow_mode=False)
    ctl_dev = MockDevice(mock_gateway, "01:111111", slug="CTL")
    mock_gateway.device_registry.devices = [ctl_dev]

    poller.update_device_tasks(ctl_dev)
    past = dt.now(UTC) - td(seconds=10)
    for task in poller.get_scheduled_cmds():
        task.next_due = past

    # ACT
    processed_count = await poller.poll_due_commands()

    # ASSERT
    assert processed_count == len(DEFAULT_POLLING_SCHEDULES["CTL"])
    assert mock_gateway._async_send_dto.call_count == processed_count

    call_args = mock_gateway._async_send_dto.call_args_list[0][0]
    sent_dto: CommandDTO = call_args[0]
    assert sent_dto.verb == Verb.RQ
    assert sent_dto.addr1 == "18:000730"
    assert sent_dto.addr2 == "01:111111"


def test_polling_manager_ctl_without_tcs_creates_device_level_0004(
    mock_gateway: MagicMock,
) -> None:
    """CTL without a TCS (no zones) gets a single device-level 0004 task.

    The MockDevice has no .tcs attribute, so 0004, 2349, and 30C9 are not
    expanded into per-zone tasks.  This verifies the fallback path.
    """
    # ARRANGE
    poller = PollingManager(mock_gateway, shadow_mode=True)
    ctl_dev = MockDevice(mock_gateway, "01:111111", slug="CTL")

    # ACT
    active_keys = poller.update_device_tasks(ctl_dev)

    # ASSERT
    # 8 device-level tasks: 10E0, 1F41, 2E04, 313F, 1100, 0004, 2349, 30C9
    assert len(active_keys) == 8
    assert ("01:111111", Code._0004) in active_keys
    assert ("01:111111", Code._2349) in active_keys
    assert ("01:111111", Code._30C9) in active_keys
    assert ("01:111111", Code._1100) in active_keys
    # No zone-level keys (3-tuples)
    assert all(len(k) == 2 for k in active_keys)


def test_polling_manager_ctl_with_zones_expands_0004_2349_and_30C9_per_zone(
    mock_gateway: MagicMock,
) -> None:
    """CTL with a TCS and zones gets per-zone 0004, 2349, and 30C9 tasks.

    0004 (zone name) is expanded for all zones (issue 947).
    2349 (zone mode) is expanded for all zones — the CTL does not
    broadcast 2349, so polling is the only way to get zone mode.
    30C9 (zone temperature) is expanded for zones without a sensor
    (issue 1013).  In this test, the mock zones have no sensor, so all
    zones get 30C9 tasks.
    """
    # ARRANGE
    poller = PollingManager(mock_gateway, shadow_mode=True)
    ctl_dev = MockDevice(mock_gateway, "01:111111", slug="CTL")

    # Mock a TCS with zones (no sensor → 30C9 will be polled)
    zone_03 = MagicMock()
    zone_03.index = "03"
    zone_03.sensor = None
    zone_07 = MagicMock()
    zone_07.index = "07"
    zone_07.sensor = None
    mock_tcs = MagicMock()
    mock_tcs.zones = [zone_03, zone_07]
    mock_tcs.zone_by_index = {"03": zone_03, "07": zone_07}
    ctl_dev.tcs = mock_tcs

    # ACT
    active_keys = poller.update_device_tasks(ctl_dev)

    # ASSERT
    # 5 device-level tasks (10E0, 1F41, 2E04, 313F, 1100)
    # + 2 zone-level 0004 tasks + 2 zone-level 2349 tasks + 2 zone-level 30C9 tasks
    device_level = {k for k in active_keys if len(k) == 2}
    zone_level = {k for k in active_keys if len(k) == 3}
    assert len(device_level) == 5
    assert len(zone_level) == 6

    # Zone-level keys for 0004
    assert ("01:111111", Code._0004, "03") in zone_level
    assert ("01:111111", Code._0004, "07") in zone_level

    # Zone-level keys for 2349
    assert ("01:111111", Code._2349, "03") in zone_level
    assert ("01:111111", Code._2349, "07") in zone_level

    # Zone-level keys for 30C9
    assert ("01:111111", Code._30C9, "03") in zone_level
    assert ("01:111111", Code._30C9, "07") in zone_level

    # Verify the 0004 tasks have the correct payload (zone_index + "00")
    task_03 = poller._tasks[("01:111111", Code._0004, "03")]
    assert task_03.payload == "0300"
    assert task_03.code == Code._0004

    task_07 = poller._tasks[("01:111111", Code._0004, "07")]
    assert task_07.payload == "0700"
    assert task_07.code == Code._0004

    # Verify the 2349 tasks have the correct payload (zone_index + "00")
    task_2349_03 = poller._tasks[("01:111111", Code._2349, "03")]
    assert task_2349_03.payload == "0300"
    assert task_2349_03.code == Code._2349

    task_2349_07 = poller._tasks[("01:111111", Code._2349, "07")]
    assert task_2349_07.payload == "0700"
    assert task_2349_07.code == Code._2349

    # Verify the 30C9 tasks have the correct payload (zone_index only, 1 byte)
    task_30c9_03 = poller._tasks[("01:111111", Code._30C9, "03")]
    assert task_30c9_03.payload == "03"
    assert task_30c9_03.code == Code._30C9

    task_30c9_07 = poller._tasks[("01:111111", Code._30C9, "07")]
    assert task_30c9_07.payload == "07"
    assert task_30c9_07.code == Code._30C9

    # Device-level tasks have no payload (default "00" in build_rq_cmd)
    task_10e0 = poller._tasks[("01:111111", Code._10E0)]
    assert task_10e0.payload is None


@pytest.mark.asyncio
async def test_polling_manager_0004_zone_uses_payload_in_cmd(
    mock_gateway: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When polling 0004 for a zone, the RQ payload includes the zone_index.

    The old DiscoveryService sent GET_ZONE_NAME with the zone_index in the
    payload.  The PollingManager must do the same — payload "00" would
    only query zone 00, not the target zone.
    """
    # ARRANGE
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    poller = PollingManager(mock_gateway, shadow_mode=False)
    ctl_dev = MockDevice(mock_gateway, "01:111111", slug="CTL")

    # Mock a TCS with one zone
    zone_05 = MagicMock()
    zone_05.index = "05"
    mock_tcs = MagicMock()
    mock_tcs.zones = [zone_05]
    ctl_dev.tcs = mock_tcs

    mock_gateway.device_registry.devices = [ctl_dev]
    poller.update_device_tasks(ctl_dev)

    # Fast-forward all tasks to trigger due commands
    past = dt.now(UTC) - td(seconds=10)
    for task in poller.get_scheduled_cmds():
        task.next_due = past

    # ACT
    await poller.poll_due_commands()

    # ASSERT: find the 0004 call and check its payload
    sent_codes = [
        call.args[0].code
        for call in mock_gateway._async_send_dto.call_args_list
    ]
    assert Code._0004 in sent_codes

    # Find the 0004 call and verify payload contains zone_index
    for call in mock_gateway._async_send_dto.call_args_list:
        dto: CommandDTO = call.args[0]
        if dto.code == Code._0004:
            assert dto.payload == "0500", (
                f"Expected 0004 payload '0500' for zone 05, got '{dto.payload}'"
            )
            break


def test_polling_manager_30C9_skips_zones_with_sensor(
    mock_gateway: MagicMock,
) -> None:
    """30C9 polling is only created for zones without a dedicated sensor.

    Zones with a sensor receive their temperature via the CTL's 30C9
    broadcast array, so polling them is redundant.  This test verifies
    the sensor filter in update_device_tasks (issue 1013).
    """
    # ARRANGE
    poller = PollingManager(mock_gateway, shadow_mode=True)
    ctl_dev = MockDevice(mock_gateway, "01:111111", slug="CTL")

    # zone_03 has a sensor → should NOT get 30C9 polling
    zone_03 = MagicMock()
    zone_03.index = "03"
    zone_03.sensor = MagicMock()  # non-None sensor

    # zone_07 has no sensor → SHOULD get 30C9 polling
    zone_07 = MagicMock()
    zone_07.index = "07"
    zone_07.sensor = None

    mock_tcs = MagicMock()
    mock_tcs.zones = [zone_03, zone_07]
    mock_tcs.zone_by_index = {"03": zone_03, "07": zone_07}
    ctl_dev.tcs = mock_tcs

    # ACT
    active_keys = poller.update_device_tasks(ctl_dev)

    # ASSERT
    zone_level = {k for k in active_keys if len(k) == 3}

    # 0004 is polled for all zones (no sensor filter)
    assert ("01:111111", Code._0004, "03") in zone_level
    assert ("01:111111", Code._0004, "07") in zone_level

    # 2349 is polled for all zones (no sensor filter — mode is needed
    # regardless of whether the zone has a sensor)
    assert ("01:111111", Code._2349, "03") in zone_level
    assert ("01:111111", Code._2349, "07") in zone_level

    # 30C9 is polled only for zone_07 (no sensor)
    assert ("01:111111", Code._30C9, "03") not in zone_level
    assert ("01:111111", Code._30C9, "07") in zone_level

    # Verify the 30C9 task payload is 1 byte (zone_index only)
    task_30c9_07 = poller._tasks[("01:111111", Code._30C9, "07")]
    assert task_30c9_07.payload == "07"


@pytest.mark.asyncio
async def test_polling_manager_30C9_zone_uses_payload_in_cmd(
    mock_gateway: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When polling 30C9 for a zone, the RQ payload is the zone_index (1 byte).

    The legacy Command.get_zone_temp used payload f"{zone_idx:02X}" (1 byte).
    The PollingManager must do the same — payload "00" would only query
    zone 00, not the target zone.
    """
    # ARRANGE
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    poller = PollingManager(mock_gateway, shadow_mode=False)
    ctl_dev = MockDevice(mock_gateway, "01:111111", slug="CTL")

    # Mock a TCS with one zone (no sensor → 30C9 will be polled)
    zone_0B = MagicMock()
    zone_0B.index = "0B"
    zone_0B.sensor = None
    mock_tcs = MagicMock()
    mock_tcs.zones = [zone_0B]
    mock_tcs.zone_by_index = {"0B": zone_0B}
    ctl_dev.tcs = mock_tcs

    mock_gateway.device_registry.devices = [ctl_dev]
    poller.update_device_tasks(ctl_dev)

    # Fast-forward all tasks to trigger due commands
    past = dt.now(UTC) - td(seconds=10)
    for task in poller.get_scheduled_cmds():
        task.next_due = past

    # ACT
    await poller.poll_due_commands()

    # ASSERT: find the 30C9 call and check its payload
    sent_codes = [
        call.args[0].code
        for call in mock_gateway._async_send_dto.call_args_list
    ]
    assert Code._30C9 in sent_codes

    for call in mock_gateway._async_send_dto.call_args_list:
        dto: CommandDTO = call.args[0]
        if dto.code == Code._30C9:
            assert dto.payload == "0B", (
                f"Expected 30C9 payload '0B' for zone 0B, got '{dto.payload}'"
            )
            break
    else:
        pytest.fail("No 30C9 command was sent")


@pytest.mark.asyncio
async def test_polling_manager_2349_zone_uses_payload_in_cmd(
    mock_gateway: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When polling 2349 for a zone, the RQ payload is zone_index + "00" (2 bytes).

    The legacy Command.get_zone_mode used payload f"{zone_idx:02X}00" (2 bytes),
    same as 0004 (zone name).  The PollingManager must do the same.
    """
    # ARRANGE
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    poller = PollingManager(mock_gateway, shadow_mode=False)
    ctl_dev = MockDevice(mock_gateway, "01:111111", slug="CTL")

    # Mock a TCS with one zone (2349 is polled for ALL zones, sensor or not)
    zone_0B = MagicMock()
    zone_0B.index = "0B"
    zone_0B.sensor = MagicMock()  # has a sensor — 2349 still polled
    mock_tcs = MagicMock()
    mock_tcs.zones = [zone_0B]
    mock_tcs.zone_by_index = {"0B": zone_0B}
    ctl_dev.tcs = mock_tcs

    mock_gateway.device_registry.devices = [ctl_dev]
    poller.update_device_tasks(ctl_dev)

    # Fast-forward all tasks to trigger due commands
    past = dt.now(UTC) - td(seconds=10)
    for task in poller.get_scheduled_cmds():
        task.next_due = past

    # ACT
    await poller.poll_due_commands()

    # ASSERT: find the 2349 call and check its payload
    sent_codes = [
        call.args[0].code
        for call in mock_gateway._async_send_dto.call_args_list
    ]
    assert Code._2349 in sent_codes

    for call in mock_gateway._async_send_dto.call_args_list:
        dto: CommandDTO = call.args[0]
        if dto.code == Code._2349:
            assert dto.payload == "0B00", (
                f"Expected 2349 payload '0B00' for zone 0B, got '{dto.payload}'"
            )
            break
    else:
        pytest.fail("No 2349 command was sent")


@pytest.mark.asyncio
async def test_polling_schema_traits_parsing() -> None:
    # ARRANGE
    raw_known_list: dict[str, dict[str, Any]] = {
        "01:111111": {
            "class": "CTL",
            "_polling_interval": {Code._1F41: 3600, Code._10E0: 600},
            "_is_battery": False,
        },
        "04:222222": {
            "class": "TRV",
            "_polling_interval": None,
            "_is_battery": True,
        },
    }

    mapped_traits = {
        dev_id: strip_and_map_traits(traits)
        for dev_id, traits in raw_known_list.items()
    }

    config = GatewayConfig(known_list=mapped_traits)
    loop = asyncio.get_running_loop()
    gateway = Gateway(port_name="/dev/null", config=config, loop=loop)

    # ACT
    ctl_dev = gateway.device_registry.get_device(DeviceIdT("01:111111"))
    trv_dev = gateway.device_registry.get_device(DeviceIdT("04:222222"))

    # ASSERT
    assert ctl_dev.polling_interval == {Code._1F41: 3600, Code._10E0: 600}
    assert ctl_dev.is_battery is False

    assert trv_dev.polling_interval is None
    assert trv_dev.is_battery is True

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
    assert config.disable_discovery is True

    config.disable_discovery = False
    assert config.disable_polling is False


# ── Structural checks (from ha_sim_test R56 + R57) ────────────────────


def test_polling_task_has_required_fields() -> None:
    """PollingTask dataclass has device_id, code, interval, next_due."""
    from ramses_rf.pipeline.polling import PollingTask

    fields = set(PollingTask.__dataclass_fields__)
    for required in ("device_id", "code", "interval", "next_due"):
        assert required in fields, f"missing field: {required}"


def test_polling_manager_accepts_shadow_mode() -> None:
    """PollingManager.__init__ accepts shadow_mode parameter."""
    import inspect

    sig = inspect.signature(PollingManager.__init__)
    assert "shadow_mode" in sig.parameters


def test_legacy_poller_deprecated_or_removed() -> None:
    """Legacy DiscoveryService.start_poller is deprecated/no-op or removed."""
    import importlib
    import inspect

    try:
        discovery = importlib.import_module("ramses_rf.discovery")
    except ModuleNotFoundError:
        return  # DiscoveryService fully removed — stronger than no-op

    src = inspect.getsource(discovery.DiscoveryService.start_poller)
    assert "deprecated" in src.lower() or "disabled" in src.lower()


def test_sz_polling_interval_constant() -> None:
    """SZ_POLLING_INTERVAL constant is 'polling_interval'."""
    from ramses_rf.const import SZ_POLLING_INTERVAL

    assert SZ_POLLING_INTERVAL == "polling_interval"


def test_sz_is_battery_constant() -> None:
    """SZ_IS_BATTERY constant is 'is_battery'."""
    from ramses_rf.const import SZ_IS_BATTERY

    assert SZ_IS_BATTERY == "is_battery"


def test_sch_polling_interval_validates_dict() -> None:
    """SCH_POLLING_INTERVAL validates dict[str, int]."""
    from ramses_rf.config import SCH_POLLING_INTERVAL

    validated = SCH_POLLING_INTERVAL({Code._10E0: 3600, Code._1F41: 1800})
    assert validated == {Code._10E0: 3600, Code._1F41: 1800}


def test_sch_polling_interval_rejects_negative() -> None:
    """SCH_POLLING_INTERVAL rejects negative intervals."""
    from ramses_rf.config import SCH_POLLING_INTERVAL

    with pytest.raises(Exception):  # noqa: B017
        SCH_POLLING_INTERVAL({Code._10E0: -1})


def test_encode_opentherm_payload_parity() -> None:
    # Arrange & Act & Assert
    assert OPENTHERM_STATUS_DATA_IDS == (0x00, 0x19, 0x1C, 0x01, 0x12)
    assert OPENTHERM_PARAMS_DATA_IDS == (0x38, 0x39)
    assert OPENTHERM_POLL_DATA_IDS == (
        0x00,
        0x19,
        0x1C,
        0x01,
        0x12,
        0x38,
        0x39,
    )
    assert 0x0E not in OPENTHERM_POLL_DATA_IDS
    assert 0x11 not in OPENTHERM_POLL_DATA_IDS

    assert encode_opentherm_payload(0x00) == "0000000000"
    assert encode_opentherm_payload(0x19) == "0080190000"
    assert encode_opentherm_payload(0x1C) == "00801C0000"
    assert encode_opentherm_payload(0x01) == "0080010000"
    assert encode_opentherm_payload(0x38) == "0080380000"
    assert encode_opentherm_payload(0x39) == "0000390000"


def test_polling_manager_otb_expands_3220_data_id_tasks(
    mock_gateway: MagicMock,
) -> None:
    # Arrange
    poller = PollingManager(mock_gateway, shadow_mode=True)
    otb_dev = MockDevice(mock_gateway, "10:048122", slug="OTB")

    # Act
    active_keys = poller.update_device_tasks(otb_dev)

    # Assert
    # 2 device-level tasks (10E0, 3EF0) + 6 Data-ID 3220 tasks
    device_level = {k for k in active_keys if len(k) == 2}
    ot_level = {k for k in active_keys if len(k) == 3}

    assert len(device_level) == 2
    assert ("10:048122", Code._10E0) in device_level
    assert ("10:048122", Code._3EF0) in device_level

    assert len(ot_level) == 7
    expected_data_ids = ("00", "19", "1C", "01", "12", "38", "39")
    for data_id_hex in expected_data_ids:
        assert ("10:048122", Code._3220, data_id_hex) in ot_level

    # Verify task payloads match parity-encoded 5-byte strings
    assert (
        poller._tasks[("10:048122", Code._3220, "00")].payload == "0000000000"
    )
    assert (
        poller._tasks[("10:048122", Code._3220, "19")].payload == "0080190000"
    )
    assert (
        poller._tasks[("10:048122", Code._3220, "1C")].payload == "00801C0000"
    )
    assert (
        poller._tasks[("10:048122", Code._3220, "01")].payload == "0080010000"
    )
    assert (
        poller._tasks[("10:048122", Code._3220, "38")].payload == "0080380000"
    )
    assert (
        poller._tasks[("10:048122", Code._3220, "39")].payload == "0000390000"
    )


@pytest.mark.asyncio
async def test_polling_manager_otb_live_dispatch_transmits_data_id_payloads(
    mock_gateway: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    poller = PollingManager(mock_gateway, shadow_mode=False)
    otb_dev = MockDevice(mock_gateway, "10:048122", slug="OTB")
    mock_gateway.device_registry.devices = [otb_dev]

    poller.update_device_tasks(otb_dev)
    past = dt.now(UTC) - td(seconds=10)
    for task in poller.get_scheduled_cmds():
        task.next_due = past

    # Act
    processed_count = await poller.poll_due_commands()

    # Assert
    # 2 device-level tasks + 7 Data-ID tasks = 9 total
    assert processed_count == 9
    assert mock_gateway._async_send_dto.call_count == 9

    # Extract all 3220 commands sent
    sent_3220_payloads: set[str] = set()
    for call in mock_gateway._async_send_dto.call_args_list:
        dto: CommandDTO = call.args[0]
        assert dto.addr2 == "10:048122"
        assert dto.verb == Verb.RQ
        if dto.code == Code._3220:
            assert dto.payload is not None
            sent_3220_payloads.add(dto.payload)

    expected_payloads = {
        "0000000000",
        "0080190000",
        "00801C0000",
        "0080010000",
        "0080380000",
        "0000390000",
        "0000120000",
    }
    assert sent_3220_payloads == expected_payloads
