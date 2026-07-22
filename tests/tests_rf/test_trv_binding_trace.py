"""RAMSES RF - Unit tests for TopologyBuilder and DeviceRegistry implicit bindings."""

from datetime import datetime as dt
from unittest.mock import MagicMock

import pytest

from ramses_rf.address import Address
from ramses_rf.config import GatewayConfig
from ramses_rf.devices.dev_registry import DeviceRegistry
from ramses_rf.enums import Topic, TopologyAction
from ramses_rf.messages.core import Message
from ramses_rf.models import TopologyChangedEvent
from ramses_rf.pipeline.topology_builder import TopologyBuilder
from ramses_rf.routing import StateHeader
from ramses_tx.dtos import PacketDTO


@pytest.mark.asyncio
async def test_topology_builder_trv_implicit_binding() -> None:
    """Test the TopologyBuilder rule engine in pure isolation."""
    # 1. Arrange: The Event Sink (Strictly typed for Mypy)
    emitted_events: list[TopologyChangedEvent] = []

    # 2. Arrange: The Engine (TopologyBuilder)
    builder = TopologyBuilder(
        emit_event_cb=emitted_events.append,
        enable_eavesdrop=True,
    )

    # 3. Arrange: The L7 Fact (The TRV Broadcast with addr3 Controller)
    mock_packet = PacketDTO(
        rssi="-70",
        verb=" I",
        seq="000",
        addr1="04:111111",
        addr2="--:------",
        addr3="01:123456",
        code="1060",
        length="003",
        payload="01FF01",
        timestamp=dt.now(),
    )

    msg = Message(
        topic=Topic.TOPOLOGY_DISCOVERY,
        header=StateHeader.create(
            code="1060",
            verb=" I",
            source_id="04:111111",
            context_val=None,
        ),
        src=Address("04:111111"),
        dst=Address("--:------"),
        data={},
        packets=(mock_packet,),
        timestamp=dt.now(),
    )

    # 4. Act: Feed the message directly to the isolated brain
    await builder.consume(msg)

    # 5. Assert: Prove the brain made the correct logical deductions
    assert len(emitted_events) == 2, "Expected exactly 2 TopologyChangedEvents"

    # Verify the Prefix Heuristic fired
    promote_event = next(
        e for e in emitted_events if e.action == TopologyAction.UPDATE_DEVICE_CLASS
    )
    assert promote_event.device_id == "04:111111"

    # Verify the 3rd Address Binding fired
    bind_event = next(
        e for e in emitted_events if e.action == TopologyAction.BIND_DEVICE
    )
    assert bind_event.parent_id == "01:123456"
    assert bind_event.child_id == "04:111111"
    assert bind_event.causation == "Rule_3rd_Address_Declaration"


@pytest.mark.asyncio
async def test_device_registry_topology_ingestion() -> None:
    """Test the DeviceRegistry state ingestion in pure isolation."""
    # 1. Arrange: Dependencies
    config = GatewayConfig(enable_eavesdrop=True)

    mock_filter = MagicMock()
    mock_factory = MagicMock()

    registry = DeviceRegistry(
        device_filter=mock_filter,
        config=config,
        device_factory_cb=mock_factory,
    )

    # 2. Arrange: The Events (Simulating the output from TopologyBuilder)
    event_1 = TopologyChangedEvent(
        action=TopologyAction.UPDATE_DEVICE_CLASS,
        device_id="04:111111",
        metadata={"device_class": "TRV"},
        causation="Rule_Heating_Prefix_Heuristic",
    )

    event_2 = TopologyChangedEvent(
        action=TopologyAction.BIND_DEVICE,
        parent_id="01:123456",
        child_id="04:111111",
        metadata={"device_role": "actuator"},
        causation="Rule_3rd_Address_Declaration",
    )

    # 3. Act: Feed both events directly to the registry
    registry.handle_topology_event(event_1)
    registry.handle_topology_event(event_2)

    # 4. Assert: Prove the registry processed the events
    # Because we are using a Mock factory, generate_schema() will not work.
    # Instead, a true unit test asserts that the registry reacted to the event
    # by attempting to resolve/instantiate the devices via the factory callback.
    factory_calls = str(mock_factory.call_args_list)
    assert "04:111111" in factory_calls, "Registry did not resolve the TRV"
    assert "01:123456" in factory_calls, "Registry did not resolve the Controller"
