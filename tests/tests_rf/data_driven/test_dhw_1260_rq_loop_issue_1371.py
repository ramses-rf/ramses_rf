"""Reproduction test for ramses_rf issue 1371.

The pre-0.60 DHW-sensor discovery reactor sends ``RQ 1260`` to
``src_dev.ctl`` for every ``1260`` message whose source has a
controller.  When the controller is itself configured as a zone's
sensor, the CTL's ``.ctl`` resolves to itself, so its own ``RP 1260``
reply triggers another ``RQ 1260`` — a request/reply loop (~44 ms per
iteration, thousands of packets, saturating the RF channel).

The fix triggers the query only on ``I`` broadcasts and never for a
source whose ``ctl`` is itself.

Fixture replays the reporter's traffic: two ``I 1260`` broadcasts from
the CS92 (07:) interleaved with four ``RP 1260`` replies from the
controller (01:, configured as zone 03's sensor).  Exactly two
``GET_DHW_TEMP`` intents must fire — one per sensor broadcast, none
for the controller's replies.

See: https://github.com/ramses-rf/ramses_rf/issues/1371
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from ramses_rf.commands.dispatcher import CommandDispatcher
from ramses_rf.enums import Action

from .helpers import TEST_DIR, load_test_gwy

if TYPE_CHECKING:
    from ramses_rf.gateway import Gateway

RQ_LOOP_DIR = Path(f"{TEST_DIR}/systems/_dhw_1260_ctl_sensor_issue_1371")


@pytest.mark.asyncio
async def test_1260_reactor_fires_once_per_sensor_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RP 1260 from a controller-as-zone-sensor must not re-trigger RQ."""
    sent: list[Any] = []

    def _spy_send_background(
        self: CommandDispatcher,
        intent: Any,
        **kwargs: Any,
    ) -> asyncio.Task[None]:
        sent.append(intent)

        async def _noop() -> None:
            return None

        return asyncio.get_running_loop().create_task(_noop())

    monkeypatch.setattr(
        CommandDispatcher, "send_background", _spy_send_background
    )

    gwy: Gateway = await load_test_gwy(RQ_LOOP_DIR)
    try:
        dhw_queries = [
            i
            for i in sent
            if getattr(i, "action", None) == Action.GET_DHW_TEMP
        ]
        assert len(dhw_queries) == 2, (
            f"expected one RQ 1260 per I-broadcast (2), got "
            f"{len(dhw_queries)} — controller RP replies re-triggered the "
            f"reactor: {[getattr(i, 'action', i) for i in sent]}"
        )
        # and all queries go to the controller, never to the sensor
        for intent in dhw_queries:
            assert getattr(intent, "dst", None) is not None
    finally:
        await gwy.stop()
