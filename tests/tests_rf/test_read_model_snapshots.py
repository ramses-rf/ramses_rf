"""CQRS Read-Model API Snapshot Tests.

This module generates deterministic snapshots of the public API output
using mathematically proven packet logs. It establishes a baseline to
prevent regressions when refactoring internal state ingestion.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest

from ramses_rf import Gateway
from ramses_rf.device import Device, DeviceHeat, DeviceHvac
from ramses_rf.gateway import GatewayConfig
from ramses_rf.pipeline.dispatcher import CentralDispatcher
from ramses_rf.pipeline.ingestion import StateProjector
from ramses_rf.systems import DhwZone, System, Zone
from ramses_tx.config import EngineConfig
from ramses_tx.const import SZ_READER_TASK
from ramses_tx.exceptions import TransportError

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

LOG_HVAC = (
    Path(__file__).parent.parent / "tests" / "systems" / "_hvac_nuaire" / "packet.log"
)
LOG_OPENTHERM = (
    Path(__file__).parent
    / "logs"
    / "test_phase2_95_topology_parity_packet_log_OpenTherm.log"
)
LOG_STANDARD = (
    Path(__file__).parent / "logs" / "test_phase2_95_topology_parity_packet_log.log"
)

_LOGGER = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def suppress_noisy_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Suppress massive volume of parsing warnings for this test file.

    :param caplog: The pytest log capture fixture.
    :type caplog: pytest.LogCaptureFixture
    :return: None
    :rtype: None
    """
    caplog.set_level(logging.CRITICAL, logger="ramses_tx")
    caplog.set_level(logging.CRITICAL, logger="ramses_rf")
    caplog.set_level(logging.CRITICAL, logger="asyncio")


@pytest.fixture(
    params=[LOG_STANDARD, LOG_OPENTHERM, LOG_HVAC],
    ids=["standard", "opentherm", "hvac"],
)
def clean_log_path(request: pytest.FixtureRequest) -> Path:
    """Provide the clean packet log file paths for baseline snapshotting.

    :param request: The pytest fixture request object.
    :type request: pytest.FixtureRequest
    :return: The path to the target log file.
    :rtype: Path
    """
    return request.param  # type: ignore[no-any-return]


async def _get_attr_value(obj: Any, attr: str) -> Any:
    """Safely get and evaluate an attribute.

    Handles standard attributes, properties, synchronous methods,
    and asynchronous coroutine methods seamlessly.

    :param obj: The object containing the attribute.
    :type obj: Any
    :param attr: The name of the attribute to look up.
    :type attr: str
    :return: The evaluated attribute value, or None if it doesn't exist.
    :rtype: Any
    """
    val = getattr(obj, attr, None)
    if callable(val):
        val = val()
    if asyncio.iscoroutine(val):
        val = await val
    return val


async def serialize_logical_entity(entity: System | Zone | DhwZone) -> dict[str, Any]:
    """Serialize the specific public API properties for logical entities.

    :param entity: The logical system or zone entity to serialize.
    :type entity: System | Zone | DhwZone
    :return: A deterministic dictionary of the entity's public API state.
    :rtype: dict[str, Any]
    """
    data: dict[str, Any] = {
        "id": getattr(entity, "id", None) or getattr(entity, "idx", None),
        "type": type(entity).__name__,
    }

    # Target: System/Evohome Controller (TCS)
    if isinstance(entity, System):
        for attr in (
            "system_mode",
            "language",
            "schedule_version",
            "heat_demands",
            "relay_demands",
            "relay_failsafes",
            "active_faults",
            "latest_event",
            "latest_fault",
        ):
            try:
                val = await _get_attr_value(entity, attr)
                if val is not None:
                    # Convert FaultLogEntry tuples to string to prevent snapshot volatility
                    if attr in ("active_faults", "latest_event", "latest_fault"):
                        data[attr] = str(val)
                    else:
                        data[attr] = val
            except AttributeError:
                continue
            except Exception as err:
                data[attr] = f"<{type(err).__name__}: {err}>"

    # Target: Heating & DHW Zones
    if isinstance(entity, (Zone, DhwZone)):
        for attr in (
            "name",
            "config",
            "mode",
            "setpoint",
            "setpoint_bounds",
            "temperature",
            "heat_demand",
            "relay_demand",
            "relay_failsafe",
            "window_open",
            "schedule_version",
            "mix_config",
        ):
            try:
                val = await _get_attr_value(entity, attr)
                if val is not None:
                    data[attr] = val
            except AttributeError:
                continue
            except Exception as err:
                data[attr] = f"<{type(err).__name__}: {err}>"

    return {k: v for k, v in sorted(data.items())}


async def serialize_hardware_state(dev: Device) -> dict[str, Any]:
    """Serialize the specific public API properties for hardware twins.

    :param dev: The hardware device entity to serialize.
    :type dev: Device
    :return: A deterministic dictionary of the device's public API state.
    :rtype: dict[str, Any]
    """
    data: dict[str, Any] = {
        "id": dev.id,
        "type": type(dev).__name__,
        "battery_low": await _get_attr_value(dev, "battery_low"),
        "battery_state": await _get_attr_value(dev, "battery_state"),
    }

    # Heating, DHW, and Base Hardware Domains
    if isinstance(dev, DeviceHeat):
        for attr in (
            "temperature",
            "setpoint",
            "heat_demand",
            "relay_demand",
            "window_open",
            "dhw_params",
            "tpi_params",
            "actuator_cycle",
            "actuator_state",
        ):
            try:
                val = await _get_attr_value(dev, attr)
                if val is not None:
                    data[attr] = val
            except AttributeError:
                continue
            except Exception as err:
                data[attr] = f"<{type(err).__name__}: {err}>"

        # OpenTherm Bridge Telemetry
        if getattr(dev, "_SLUG", None) == "OTB":
            for attr in (
                "rel_modulation_level",
                "boiler_output_temp",
                "boiler_return_temp",
                "boiler_setpoint",
                "ch_water_pressure",
                "ch_setpoint",
                "ch_max_setpoint",
                "ch_active",
                "ch_enabled",
                "cooling_active",
                "cooling_enabled",
                "dhw_flow_rate",
                "dhw_setpoint",
                "dhw_temp",
                "dhw_active",
                "dhw_blocking",
                "dhw_enabled",
                "fault_present",
                "flame_active",
                "max_rel_modulation",
                "oem_code",
                "otc_active",
                "outside_temp",
                "summer_mode",
            ):
                try:
                    val = await _get_attr_value(dev, attr)
                    if val is not None:
                        data[attr] = val
                except AttributeError:
                    continue
                except Exception as err:
                    data[attr] = f"<{type(err).__name__}: {err}>"

    # HVAC Hardware Domain
    if isinstance(dev, DeviceHvac):
        for attr in (
            # Core environmental
            "co2_level",
            "indoor_humidity",
            "outdoor_humidity",
            "indoor_temp",  # or "temperature" depending on your getter
            "outdoor_temp",
            "dewpoint_temp",
            # Fan & Mechanical
            "fan_mode",
            "fan_rate",
            "bypass_position",
            "exhaust_fan_speed",
            "supply_fan_speed",
            "pre_heat",
            "post_heat",
            # Diagnostics
            "filter_dirty",
            "frost_cycle",
            "has_fault",
        ):
            try:
                val = await _get_attr_value(dev, attr)
                if val is not None:
                    data[attr] = val
            except AttributeError:
                continue
            except Exception as err:
                data[attr] = f"<{type(err).__name__}: {err}>"

    # Force the snapshot to capture the raw CQRS memory state side-by-side
    # with the legacy getters to prove parallel ingestion before cut-over.
    if hasattr(dev, "hvac_state") and dev.hvac_state:
        cqrs_data = {
            k: v
            for k, v in dataclasses.asdict(dev.hvac_state).items()
            if v is not None and k != "last_updated"
        }
        if cqrs_data:
            data["cqrs_hvac_state"] = cqrs_data

    return {k: v for k, v in sorted(data.items())}


@pytest.mark.asyncio
async def test_read_model_baseline_snapshot(
    clean_log_path: Path, snapshot: SnapshotAssertion
) -> None:
    """Stream clean logs and snapshot the public API read models.

    :param clean_log_path: The parameterized path to the packet log file.
    :type clean_log_path: Path
    :param snapshot: The Syrupy snapshot assertion fixture.
    :type snapshot: SnapshotAssertion
    :return: None
    :rtype: None
    """
    if not clean_log_path.exists():
        pytest.skip(f"Fixture not found at {clean_log_path}")

    gwy = Gateway(
        None,
        config=GatewayConfig(
            disable_discovery=True,
            reduce_processing=0,
            engine=EngineConfig(
                disable_sending=True,
                input_file=str(clean_log_path),
            ),
        ),
    )

    # Initialize the CQRS Pipeline and hook it to the legacy stream
    pipeline_in_queue: asyncio.Queue[Any] = asyncio.Queue()
    dispatcher = CentralDispatcher(pipeline_in_queue)
    worker = StateProjector(gwy, dispatcher.ssot_queue)

    await dispatcher.start()
    await worker.start()

    legacy_handler = gwy._msg_handler

    async def parallel_strangler_bridge(dto: Any) -> None:
        await legacy_handler(dto)
        this_msg = getattr(gwy, "_this_msg", None)
        if this_msg:
            pipeline_in_queue.put_nowait(this_msg)

    gwy._engine._set_msg_handler(parallel_strangler_bridge)

    mock_send = AsyncMock(return_value=None)

    with patch.object(gwy, "async_send_cmd", mock_send):
        with contextlib.suppress(TransportError):
            await gwy.start()

        if gwy._engine._transport:
            reader_task = gwy._engine._transport.get_extra_info(SZ_READER_TASK)
            if reader_task:
                await reader_task

        # CRITICAL: Deterministically wait for all async queues to drain!
        # This replaces the flaky `await asyncio.sleep(0.5)` which caused CI
        # race conditions on slower virtual machines.
        await pipeline_in_queue.join()
        await dispatcher.ssot_queue.join()

        if gwy.message_store:
            gwy.message_store.flush()

        # 1. Snapshot the Physical Hardware Twins
        devices_data = []
        for d in sorted(gwy.device_registry.devices, key=lambda x: x.id):
            devices_data.append(await serialize_hardware_state(d))

        api_state: dict[str, Any] = {
            "schema": await gwy.schema(),
            "devices": devices_data,
        }

        # 2. Snapshot the Logical System Twins
        if gwy.tcs:
            api_state["system"] = await serialize_logical_entity(gwy.tcs)

            zones_data = []
            for z in sorted(gwy.tcs.zones, key=lambda x: x.idx):
                zones_data.append(await serialize_logical_entity(z))

            if zones_data:
                api_state["zones"] = zones_data

            if getattr(gwy.tcs, "dhw", None):
                api_state["dhw"] = await serialize_logical_entity(gwy.tcs.dhw)

        # Shut down the CQRS pipeline cleanly
        await dispatcher.stop()
        await worker.stop()

        with contextlib.suppress(asyncio.CancelledError, TransportError):
            await gwy.stop()

    assert snapshot == api_state
