"""Regression tests for the application layer (Gateway state)."""

import asyncio
import contextlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from syrupy.assertion import SnapshotAssertion

from ramses_rf import Gateway
from ramses_rf.device import DeviceHeat, DeviceHvac
from ramses_tx.exceptions import TransportError
from ramses_tx.transport import SZ_READER_TASK

# Navigate up from tests/tests_rf/test_regression_rf.py to tests/fixtures/
FIXTURE_FILE = Path(__file__).parents[1] / "fixtures" / "regression_packets_sorted.txt"


def serialize_device(dev: Any) -> dict[str, Any]:
    """Helper to serialize a device's state for snapshotting.

    Identifies attributes based on device type (Heat vs HVAC) and existence
    of properties to create a deterministic state snapshot.
    """
    # Base attributes for all devices
    data: dict[str, Any] = {
        "id": dev.id,
        "type": type(dev).__name__,
        "is_alive": getattr(dev, "_is_alive", None),
        "battery_low": getattr(dev, "battery_low", None),
    }

    # Capture specific state for Heating devices
    if isinstance(dev, DeviceHeat):
        # Topology
        zone = getattr(dev, "zone", None)
        tcs = getattr(dev, "tcs", None)

        data.update(
            {
                "tcs_id": tcs.id if tcs else None,
                "zone_idx": getattr(zone, "idx", None),
            }
        )

        # General Heating Attributes
        # We iterate and try to access each attribute.
        # Properties in ramses_rf might raise TypeError/ValueError if state is inconsistent
        # or if the library has a bug. We capture these errors in the snapshot
        # rather than crashing the entire test suite.
        for attr in (
            "active",  # BDR Switch
            "actuator_cycle",  # Actuators
            "actuator_state",
            "heat_demand",  # Many heat devices
            "heat_demands",  # UFC
            "modulation_level",  # OTB/Actuators
            "relay_demand",  # BDR/UFC
            "setpoint",  # Thermostats/TRVs
            "setpoints",  # UFC
            "temperature",  # Sensors
            "window_open",  # TRV
        ):
            try:
                # getattr triggers the @property logic
                val = getattr(dev, attr, None)
                if val is not None:
                    data[attr] = val
            except AttributeError:
                continue  # Attribute strictly does not exist on this object
            except Exception as err:
                # Capture functional regressions (bugs) in the library code as string data
                # e.g. "setpoints": "<TypeError: string indices must be integers...>"
                data[attr] = f"<{type(err).__name__}: {err}>"

        # OpenTherm Bridge (OTB) Specifics
        if getattr(dev, "_SLUG", None) == "OTB":
            for attr in (
                "boiler_output_temp",
                "boiler_return_temp",
                "boiler_setpoint",
                "ch_max_setpoint",
                "ch_water_pressure",
                "dhw_flow_rate",
                "dhw_setpoint",
                "dhw_temp",
                "fault_present",
                "flame_active",
                "max_rel_modulation",
                "oem_code",
                "otc_active",
                "outside_temp",
                "rel_modulation_level",
            ):
                try:
                    val = getattr(dev, attr, None)
                    if val is not None:
                        data[attr] = val
                except AttributeError:
                    continue
                except Exception as err:
                    data[attr] = f"<{type(err).__name__}: {err}>"

    # Capture specific state for HVAC devices
    if isinstance(dev, DeviceHvac):
        for attr in (
            "air_quality",
            "air_quality_base",
            "boost_timer",
            "bypass_mode",
            "bypass_position",
            "bypass_state",
            "co2_level",
            "dewpoint_temp",
            "exhaust_fan_speed",
            "exhaust_flow",
            "exhaust_temp",
            "fan_info",
            "fan_mode",
            "fan_rate",
            "filter_remaining",
            "indoor_humidity",
            "indoor_temp",
            "outdoor_humidity",
            "outdoor_temp",
            "post_heat",
            "pre_heat",
            "presence_detected",
            "remaining_mins",
            "speed_cap",
            "supply_fan_speed",
            "supply_flow",
            "supply_temp",
        ):
            try:
                val = getattr(dev, attr, None)
                if val is not None:
                    data[attr] = val
            except AttributeError:
                continue
            except Exception as err:
                data[attr] = f"<{type(err).__name__}: {err}>"

    # Return sorted dictionary for deterministic snapshots
    return {k: v for k, v in sorted(data.items())}


@pytest.mark.asyncio
async def test_gateway_replay_regression(snapshot: SnapshotAssertion) -> None:
    """Replay the packet log and snapshot the final Gateway state.

    This ensures that processing the same packets always results in the same
    device discovery, schema generation, and system state.
    """
    if not FIXTURE_FILE.exists():
        raise FileNotFoundError(f"Fixture not found at {FIXTURE_FILE}")

    # 1. Initialize Gateway with FileTransport
    # reduce_processing=0 ensures full processing (Parsing + State)
    # config options set to prevent networking attempts
    gwy = Gateway(
        None,  # port_name is required (positional arg)
        input_file=str(FIXTURE_FILE),
        config={
            "disable_discovery": True,
            "disable_sending": True,
            "reduce_processing": 0,
        },
    )

    # 2. Patch sending methods to prevent "Read-Only" errors & background noise.
    # The gateway logic might try to reply to RQs found in the logs.
    # We use AsyncMock to ensure a proper coroutine is returned for asyncio.create_task.
    mock_send = AsyncMock(return_value=None)

    with patch.object(gwy, "async_send_cmd", mock_send):
        # 3. Start the Gateway (spawns the reader task)
        # The library's `start()` method has a strict 1s timeout for file parsing.
        # Large regression files take longer, raising TransportError.
        # We catch this expected timeout gracefully.
        with contextlib.suppress(TransportError):
            await gwy.start()

        # 4. Wait for the Transport to finish reading the file
        # Instead of relying on the possibly-cancelled protocol future,
        # we await the specific reader task responsible for file processing.
        if gwy._transport:
            reader_task = gwy._transport.get_extra_info(SZ_READER_TASK)
            if reader_task:
                await reader_task

        # 5. Extract State for Snapshot
        # We create a deterministic dictionary of the system state
        system_state: dict[str, Any] = {
            "schema": gwy.schema,
            "devices": [
                serialize_device(d) for d in sorted(gwy.devices, key=lambda x: x.id)
            ],
        }

        # Add specific System (TCS) details if a TCS was discovered
        if gwy.tcs:
            system_state["tcs"] = {
                "id": gwy.tcs.id,
                "zones": {
                    z.idx: {
                        "name": z.name,
                        "type": type(z).__name__,
                        "sensor": z.sensor.id if z.sensor else None,
                        "actuators": sorted([a.id for a in z.actuators]),
                    }
                    for z in sorted(gwy.tcs.zones, key=lambda x: x.idx)
                },
            }

        # 6. Stop Gateway
        # We suppress CancelledError because the initial start() timeout likely
        # cancelled the internal connection_lost future, which stop() tries to await.
        with contextlib.suppress(asyncio.CancelledError, TransportError):
            await gwy.stop()

    # 7. Assert Snapshot
    assert snapshot == system_state
