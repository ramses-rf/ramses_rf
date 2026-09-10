"""Tests for the RAMSES-II base protocol layer."""

import logging
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ramses_tx.address import HGI_DEV_ADDR
from ramses_tx.exceptions import ProtocolError, TransportError
from ramses_tx.protocol.base import _DeviceIdFilterMixin
from ramses_tx.typing import DeviceIdT

# Ensure all tests in this file run within an asyncio event loop
pytestmark = pytest.mark.asyncio


class DummyProtocol(_DeviceIdFilterMixin):
    """Testable protocol class incorporating device ID filtering."""

    def __init__(self, msg_handler: Any) -> None:
        # Initialize the mixin, which handles the exclusion/inclusion list setup
        super().__init__(msg_handler)

    async def _send_cmd(self, cmd: Any, **kwargs: Any) -> Any:
        """Override the abstract _send_cmd to prevent NotImplementedError."""
        return cmd


@pytest.fixture
def mock_msg_handler() -> AsyncMock:
    """Provide a dummy message handler for the protocol."""
    return AsyncMock()


@pytest.fixture
async def protocol(mock_msg_handler: AsyncMock) -> DummyProtocol:
    """Provide a fresh instance of the testable base protocol."""
    return DummyProtocol(mock_msg_handler)


# --- CONNECTION LIFECYCLE TESTS (Issue #560 Fixes) ---


async def test_wait_for_connection_lost_no_connection(
    protocol: DummyProtocol,
) -> None:
    """Test wait_for_connection_lost when no connection was ever made."""
    result = await protocol.wait_for_connection_lost()
    assert result is None


async def test_wait_for_connection_lost_clean_disconnect(
    protocol: DummyProtocol,
) -> None:
    """Test wait_for_connection_lost returns None on a clean disconnect."""
    mock_transport = MagicMock()
    protocol.connection_made(mock_transport)
    protocol.connection_lost(None)

    result = await protocol.wait_for_connection_lost()
    assert result is None


async def test_wait_for_connection_lost_with_exception(
    protocol: DummyProtocol,
) -> None:
    """Test wait_for_connection_lost returns (does not raise) transport exceptions."""
    mock_transport = MagicMock()
    protocol.connection_made(mock_transport)

    expected_exc = Exception("Device disconnected unexpectedly")
    protocol.connection_lost(expected_exc)

    result = await protocol.wait_for_connection_lost()
    assert result is expected_exc


async def test_wait_for_connection_lost_timeout(
    protocol: DummyProtocol,
) -> None:
    """Test wait_for_connection_lost raises TransportError if it times out."""
    mock_transport = MagicMock()
    protocol.connection_made(mock_transport)

    with pytest.raises(
        TransportError, match="Transport did not unbind from Protocol"
    ):
        await protocol.wait_for_connection_lost(timeout=0.01)


# --- DEVICE ID FILTERING TESTS (_is_wanted_addrs) ---


async def test_is_wanted_addrs_empty_filters(protocol: DummyProtocol) -> None:
    """Test default behavior with no filters set."""
    assert (
        protocol._is_wanted_addrs(
            DeviceIdT("01:111111"), DeviceIdT("01:222222")
        )
        is True
    )


async def test_is_wanted_addrs_exclude_list(protocol: DummyProtocol) -> None:
    """Test that devices in the exclude list are rejected."""
    protocol._exclude = [DeviceIdT("01:111111")]
    assert (
        protocol._is_wanted_addrs(
            DeviceIdT("01:111111"), DeviceIdT("01:222222")
        )
        is False
    )
    assert (
        protocol._is_wanted_addrs(
            DeviceIdT("01:222222"), DeviceIdT("01:111111")
        )
        is False
    )
    assert (
        protocol._is_wanted_addrs(
            DeviceIdT("01:333333"), DeviceIdT("01:444444")
        )
        is True
    )


async def test_is_wanted_addrs_enforce_include(
    protocol: DummyProtocol,
) -> None:
    """Test enforce_include logic ensures ALL addresses are in the include list."""
    protocol.enforce_include = True
    protocol._include = [DeviceIdT("01:111111")]

    # Only one device included, the other isn't -> False
    assert (
        protocol._is_wanted_addrs(
            DeviceIdT("01:111111"), DeviceIdT("01:222222")
        )
        is False
    )

    # Both devices included -> True
    protocol._include = [DeviceIdT("01:111111"), DeviceIdT("01:222222")]
    assert (
        protocol._is_wanted_addrs(
            DeviceIdT("01:111111"), DeviceIdT("01:222222")
        )
        is True
    )


async def test_is_wanted_addrs_active_hgi(protocol: DummyProtocol) -> None:
    """Test that the active HGI bypasses the enforce_include filter."""
    protocol.enforce_include = True
    protocol._include = [DeviceIdT("01:111111")]
    protocol._active_hgi = DeviceIdT("18:999999")

    # 18:999999 is the active HGI, so it should be permitted despite not being in _include
    assert (
        protocol._is_wanted_addrs(
            DeviceIdT("01:111111"), DeviceIdT("18:999999")
        )
        is True
    )


async def test_is_wanted_addrs_sending_to_hgi(protocol: DummyProtocol) -> None:
    """Test that the generic HGI address is always permitted.

    HGI_DEV_ADDR (18:000730) is the generic HGI broadcast address used
    by HGI80s that can't send with their real address.  It's always in
    the include list (added in __init__), so it passes both for sending
    and receiving (issue 1185 — HGI80 echo was being filtered).
    """
    protocol.enforce_include = True
    # Preserve the default include entries (ALL_DEV_ADDR, NON_DEV_ADDR,
    # HGI_DEV_ADDR) and add a known device.
    protocol._include += [DeviceIdT("01:111111")]

    # When sending, HGI_DEV_ADDR (18:000730) is always allowed
    assert (
        protocol._is_wanted_addrs(
            DeviceIdT("01:111111"), HGI_DEV_ADDR.id, sending=True
        )
        is True
    )
    # Also when receiving (HGI80 echo comes back with src=HGI_DEV_ADDR)
    assert (
        protocol._is_wanted_addrs(
            HGI_DEV_ADDR.id, DeviceIdT("01:111111"), sending=False
        )
        is True
    )


async def test_is_wanted_addrs_blocked_hgi_is_filtered(
    protocol: DummyProtocol,
) -> None:
    """A foreign HGI in the block_list is filtered out (issue 1020).

    A foreign HGI (different owner, marked as foreign in ramses_cc →
    block_list) does not communicate with our controller.  Blocking it
    is safe and prevents noise.  The issue 822 eavesdropping exemption
    only applies to unknown HGIs that might be our own second gateway.
    """
    protocol._active_hgi = DeviceIdT("18:191664")
    protocol._exclude = [DeviceIdT("18:072981")]  # foreign HGI in block_list

    # Packet from controller to blocked HGI — filtered
    assert (
        protocol._is_wanted_addrs(
            DeviceIdT("01:216136"), DeviceIdT("18:072981")
        )
        is False
    )
    # Packet from blocked HGI to controller — filtered
    assert (
        protocol._is_wanted_addrs(
            DeviceIdT("18:072981"), DeviceIdT("01:216136")
        )
        is False
    )


async def test_is_wanted_addrs_unknown_hgi_not_blocked(
    protocol: DummyProtocol,
) -> None:
    """An unknown HGI (not in any list) is NOT blocked (issue 822).

    An unknown HGI might be our own second gateway not yet configured.
    Its packets must pass through so the active gateway can eavesdrop
    on the controller's responses to it.
    """
    protocol._active_hgi = DeviceIdT("18:191664")
    protocol._exclude = []
    protocol._include = []

    # Packet from unknown HGI to controller — allowed through
    assert (
        protocol._is_wanted_addrs(
            DeviceIdT("18:072981"), DeviceIdT("01:216136")
        )
        is True
    )
    # Packet from controller to unknown HGI — allowed through
    assert (
        protocol._is_wanted_addrs(
            DeviceIdT("01:216136"), DeviceIdT("18:072981")
        )
        is True
    )


async def test_is_wanted_addrs_known_hgi_no_foreign_warning(
    protocol: DummyProtocol,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A known/configured HGI must not be flagged as a Foreign gateway.

    The discovery_scan config schema permits multiple HGIs to be declared
    in the known_list.  A second HGI that is in the known_list is not
    foreign — it is a declared gateway — so the 'potentially a Foreign
    gateway' warning must be suppressed for it (issue 1020).
    """
    protocol._active_hgi = DeviceIdT("18:130140")
    protocol._include = [DeviceIdT("18:154951")]  # second, known HGI

    with caplog.at_level(logging.WARNING):
        result = protocol._is_wanted_addrs(
            DeviceIdT("18:154951"), DeviceIdT("01:216136")
        )

    # Known HGIs are never blocked (foreign-HGI exemption still applies)
    assert result is True
    # And no Foreign gateway warning should be emitted for a known HGI
    assert not any("Foreign gateway" in r.message for r in caplog.records)


async def test_is_wanted_addrs_unknown_hgi_logs_info(
    protocol: DummyProtocol,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unknown HGI (not in the known_list) logs at INFO, not WARNING.

    Only HGIs declared in the known_list are suppressed (issue 1020); a
    genuinely unknown 18: device still produces the Foreign gateway
    message so the user can decide whether to configure it, but at INFO
    level since a foreign HGI is never blocked and the message is purely
    informational (issue 1020).
    """
    protocol._active_hgi = DeviceIdT("18:130140")
    protocol._include = []  # 18:154951 is not known

    with caplog.at_level(logging.INFO):
        protocol._is_wanted_addrs(
            DeviceIdT("18:154951"), DeviceIdT("01:216136")
        )

    # The Foreign gateway message is emitted at INFO level
    foreign_records = [
        r for r in caplog.records if "Foreign gateway" in r.message
    ]
    assert len(foreign_records) == 1
    assert foreign_records[0].levelno == logging.INFO


async def test_is_wanted_addrs_hgi_dev_addr_still_blocked(
    protocol: DummyProtocol,
) -> None:
    """HGI_DEV_ADDR (18:000730) is the generic broadcast, not a specific HGI.

    It must still be subject to the block_list — only specific foreign HGIs
    (18:XXXXXX where XXXXXX != 000730) are exempt.
    """
    protocol._exclude = [HGI_DEV_ADDR.id]

    assert (
        protocol._is_wanted_addrs(DeviceIdT("01:216136"), HGI_DEV_ADDR.id)
        is False
    )


async def test_is_wanted_addrs_blocked_hgi_no_warning(
    protocol: DummyProtocol,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A blocked HGI must not emit a Foreign gateway log message.

    The block_list check returns False before warn_foreign_hgi() is
    reached, so the packet is silently filtered — no log noise.
    """
    protocol._active_hgi = DeviceIdT("18:191664")
    protocol._exclude = [DeviceIdT("18:072981")]

    with caplog.at_level(logging.DEBUG):
        result = protocol._is_wanted_addrs(
            DeviceIdT("18:072981"), DeviceIdT("01:216136")
        )

    assert result is False
    assert not any("Foreign gateway" in r.message for r in caplog.records)


async def test_is_wanted_addrs_hgi_in_both_lists_known_wins(
    protocol: DummyProtocol,
) -> None:
    """An HGI in both block_list and known_list is allowed (known wins).

    For HGI devices, the known_list check runs before the block_list
    check.  This is intentional: if the user explicitly declared an
    HGI in the known_list, it should be allowed even if it also
    appears in the block_list (e.g. due to a config migration).
    """
    protocol._active_hgi = DeviceIdT("18:191664")
    protocol._include = [DeviceIdT("18:072981")]
    protocol._exclude = [DeviceIdT("18:072981")]

    assert (
        protocol._is_wanted_addrs(
            DeviceIdT("18:072981"), DeviceIdT("01:216136")
        )
        is True
    )


async def test_is_wanted_addrs_unknown_hgi_no_active_hgi_silent(
    protocol: DummyProtocol,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unknown HGI with no active HGI set passes silently.

    If _active_hgi is None (e.g. MQTT bridge before the online
    message), warn_foreign_hgi() is not called — the packet passes
    through without a log message.  See issue 1002.
    """
    protocol._active_hgi = None
    protocol._include = []
    protocol._exclude = []

    with caplog.at_level(logging.DEBUG):
        result = protocol._is_wanted_addrs(
            DeviceIdT("18:072981"), DeviceIdT("01:216136")
        )

    assert result is True
    assert not any("Foreign gateway" in r.message for r in caplog.records)


async def test_is_wanted_addrs_unknown_hgi_bypasses_enforce_include(
    protocol: DummyProtocol,
) -> None:
    """An unknown HGI bypasses enforce_include (issue 822).

    Even with enforce_include=True (strict filtering), an unknown HGI
    is allowed through — the 18: exemption continues before reaching
    the enforce_include check at the bottom of the loop.  The non-HGI
    device in the packet must still be in the known_list for the
    packet to pass.
    """
    protocol._active_hgi = DeviceIdT("18:191664")
    protocol._include = [DeviceIdT("01:216136")]
    protocol._exclude = []
    protocol.enforce_include = True

    assert (
        protocol._is_wanted_addrs(
            DeviceIdT("18:072981"), DeviceIdT("01:216136")
        )
        is True
    )


async def test_set_active_hgi_none_no_warning(
    protocol: DummyProtocol,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """_set_active_hgi(None) should not emit a warning (issue 1002).

    MQTT bridges (ramses_esp) don't know the HGI ID at connection time
    — the ID arrives later via the "online" LWT message.  Calling
    _set_active_hgi(None) should silently defer, not warn about
    'None: { class: HGI }' not being in the known_list.
    """
    with caplog.at_level(logging.WARNING):
        protocol._set_active_hgi(None)
    assert protocol._active_hgi is None  # not set
    assert not any("None" in r.message for r in caplog.records)
    assert not any("SHOULD be in" in r.message for r in caplog.records)


async def test_set_active_hgi_none_then_deferred_check(
    protocol: DummyProtocol,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deferred HGI check fires when transport learns the real ID (issue 1002).

    After _set_active_hgi(None), _is_wanted_addrs should detect the real
    HGI ID from the transport's extra info and run the known_list check.
    """
    from ramses_tx.const import SZ_ACTIVE_HGI

    # Simulate MQTT bridge: _set_active_hgi(None) at connection time
    protocol._set_active_hgi(None)
    assert protocol._active_hgi is None

    # Simulate the transport learning the real HGI ID from the online msg
    protocol._transport = MagicMock()
    protocol._transport.get_extra_info = MagicMock(
        side_effect=lambda key: (
            DeviceIdT("18:130140") if key == SZ_ACTIVE_HGI else None
        )
    )
    protocol._include = [DeviceIdT("01:111111")]
    protocol.enforce_include = True

    # First call to _is_wanted_addrs should trigger the deferred check
    with caplog.at_level(logging.WARNING):
        protocol._is_wanted_addrs(
            DeviceIdT("01:111111"), DeviceIdT("01:222222")
        )

    # _active_hgi should now be set — mypy can't track the side effect
    # through _is_wanted_addrs, so we cast to narrow the type
    hgi = cast(DeviceIdT, protocol._active_hgi)
    assert hgi == DeviceIdT("18:130140")
    # And the warning about the HGI not being in known_list should fire
    assert any("18:130140" in r.message for r in caplog.records)
    assert any("SHOULD be in" in r.message for r in caplog.records)


# --- INBOUND PACKET TESTS (_packet_received) ---


async def test_packet_received_included(protocol: DummyProtocol) -> None:
    """Test that wanted packets are passed up to the parent class."""
    mock_packet = MagicMock()
    mock_packet.src.id = "01:111111"
    mock_packet.dst.id = "01:222222"

    # Patch the base class to prevent the mock from triggering validation errors
    with patch(
        "ramses_tx.protocol.base._BaseProtocol._packet_received"
    ) as mock_base_recv:
        protocol._packet_received(mock_packet)
        mock_base_recv.assert_called_once_with(mock_packet)


async def test_packet_received_excluded(
    protocol: DummyProtocol, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that unwanted packets are dropped and logged."""
    protocol._exclude = [DeviceIdT("01:111111")]
    mock_packet = MagicMock()
    mock_packet.src.id = "01:111111"
    mock_packet.dst.id = "01:222222"

    with (
        caplog.at_level(logging.DEBUG),
        patch(
            "ramses_tx.protocol.base._BaseProtocol._packet_received"
        ) as mock_base_recv,
    ):
        protocol._packet_received(mock_packet)
        mock_base_recv.assert_not_called()

    assert "Packet excluded by device_id filter" in caplog.text


async def test_packet_received_excluded_bypasses_to_dto(
    protocol: DummyProtocol,
) -> None:

    # Arrange
    protocol._exclude = [DeviceIdT("01:111111")]
    mock_packet = MagicMock()
    mock_packet.src.id = "01:111111"
    mock_packet.dst.id = "01:222222"

    # Act
    protocol._packet_received(mock_packet)

    # Assert
    mock_packet.to_dto.assert_not_called()


async def test_packet_received_hgi80_echo_passes_enforce_include(
    protocol: DummyProtocol,
) -> None:
    """HGI80 echo (src=18:000730) passes the device_id filter even with
    enforce_include=True.

    The HGI80 firmware transmits with 18:000730 as the source address.
    The echo comes back with src=HGI_DEV_ADDR.  Without HGI_DEV_ADDR in
    the include list, enforce_include would filter it out, causing echo
    timeouts (issue 1185).
    """
    protocol.enforce_include = True
    protocol._include += [DeviceIdT("01:111111")]  # known device

    mock_packet = MagicMock()
    mock_packet.src.id = HGI_DEV_ADDR.id  # 18:000730
    mock_packet.dst.id = "01:111111"

    with patch(
        "ramses_tx.protocol.base._BaseProtocol._packet_received"
    ) as mock_base_recv:
        protocol._packet_received(mock_packet)
        mock_base_recv.assert_called_once_with(mock_packet)


async def test_packet_received_hgi80_echo_no_raw_handlers(
    protocol: DummyProtocol,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """HGI80 echo with no raw handlers is excluded at the fast-exit path
    when HGI_DEV_ADDR is not in the include list (regression guard).

    This test documents the OLD behavior (before the fix) to ensure
    the HGI_DEV_ADDR include list entry is not accidentally removed.
    """
    protocol.enforce_include = True
    # Deliberately remove HGI_DEV_ADDR from the include list to simulate
    # the pre-fix state.  This should cause the packet to be filtered.
    protocol._include = [
        DeviceIdT("01:111111"),
        DeviceIdT("63:262142"),  # ALL_DEV_ADDR
        DeviceIdT("--:------"),  # NON_DEV_ADDR
        # HGI_DEV_ADDR intentionally missing
    ]

    mock_packet = MagicMock()
    mock_packet.src.id = HGI_DEV_ADDR.id
    mock_packet.dst.id = "01:111111"

    with (
        caplog.at_level(logging.DEBUG),
        patch(
            "ramses_tx.protocol.base._BaseProtocol._packet_received"
        ) as mock_base_recv,
    ):
        protocol._packet_received(mock_packet)
        mock_base_recv.assert_not_called()

    assert "Packet excluded by device_id filter" in caplog.text


# --- OUTBOUND COMMAND TESTS (send_cmd) ---


async def test_send_cmd_included(protocol: DummyProtocol) -> None:
    """Test that wanted commands are sent down to the parent class."""
    mock_cmd = MagicMock()
    mock_cmd.src.id = "01:111111"
    mock_cmd.dst.id = "01:222222"
    protocol._is_evofw3 = (
        False  # Avoids triggering deep address parsing on the mock
    )

    result = await protocol.send_cmd(mock_cmd)

    assert result is mock_cmd


async def test_send_cmd_excluded(protocol: DummyProtocol) -> None:
    """Test that sending unwanted commands raises a ProtocolError."""
    protocol._exclude = [DeviceIdT("01:111111")]
    mock_cmd = MagicMock()
    mock_cmd.addr1 = "01:111111"
    mock_cmd.addr2 = "01:222222"

    with pytest.raises(
        ProtocolError, match="Command excluded by device_id filter"
    ):
        await protocol.send_cmd(mock_cmd)


async def test_patch_cmd_if_needed_evofw3(protocol: DummyProtocol) -> None:
    """Test that _patch_cmd_if_needed swaps the default HGI address for evofw3."""
    from ramses_tx.dtos import CommandDTO as Command

    protocol._is_evofw3 = True
    protocol._known_hgi = DeviceIdT(
        "18:123456"
    )  # Safely sets the hgi_id property

    original_cmd = Command.from_cli(
        "RQ --- 18:000730 01:222222 --:------ 12B0 001 00"
    )

    patched_cmd = protocol._patch_cmd_if_needed(original_cmd)

    assert patched_cmd is not original_cmd
    assert patched_cmd.addr1 == "18:123456"
    assert patched_cmd.addr2 == "01:222222"
    assert original_cmd.addr1 == "18:000730"  # Enforces immutability


async def test_patch_cmd_if_needed_hgi80_reverse(
    protocol: DummyProtocol,
) -> None:
    """Test that _patch_cmd_if_needed swaps the real HGI ID back to the
    placeholder for HGI80 (TI 3410).

    HGI80 firmware requires 18:000730 as the source address for frames it
    transmits.  Using the actual gateway ID causes a silent drop and WantEcho
    timeout (issue 835, cc 864).
    """
    from ramses_tx.dtos import CommandDTO as Command

    protocol._is_evofw3 = False  # HGI80
    protocol._known_hgi = DeviceIdT("18:123456")

    original_cmd = Command.from_cli(
        " W --- 18:123456 01:222222 --:------ 12B0 001 00"
    )

    patched_cmd = protocol._patch_cmd_if_needed(original_cmd)

    assert patched_cmd is not original_cmd
    assert patched_cmd.addr1 == "18:000730"  # swapped back to placeholder
    assert patched_cmd.addr2 == "01:222222"
    assert original_cmd.addr1 == "18:123456"  # Enforces immutability


async def test_patch_cmd_if_needed_hgi80_no_change_when_placeholder(
    protocol: DummyProtocol,
) -> None:
    """Test that _patch_cmd_if_needed does not alter a command that already
    uses the placeholder for HGI80."""
    from ramses_tx.dtos import CommandDTO as Command

    protocol._is_evofw3 = False  # HGI80
    protocol._known_hgi = DeviceIdT("18:123456")

    original_cmd = Command.from_cli(
        " W --- 18:000730 01:222222 --:------ 12B0 001 00"
    )

    patched_cmd = protocol._patch_cmd_if_needed(original_cmd)

    assert patched_cmd is original_cmd  # no change needed
    assert patched_cmd.addr1 == "18:000730"


async def test_patch_cmd_if_needed_hgi80_no_change_when_impersonating(
    protocol: DummyProtocol,
) -> None:
    """Test that _patch_cmd_if_needed does not alter a command that
    impersonates a non-gateway device (e.g. a thermostat) on HGI80.

    The HGI80 cannot impersonate, but the patching logic should not silently
    rewrite the source — the impersonation alert handles that case.
    """
    from ramses_tx.dtos import CommandDTO as Command

    protocol._is_evofw3 = False  # HGI80
    protocol._known_hgi = DeviceIdT("18:123456")

    original_cmd = Command.from_cli(
        " W --- 21:057310 01:222222 --:------ 12B0 001 00"
    )

    patched_cmd = protocol._patch_cmd_if_needed(original_cmd)

    assert patched_cmd is original_cmd  # no change — not the HGI ID
    assert patched_cmd.addr1 == "21:057310"


async def test_patch_cmd_if_needed_dynamic_evofw3_from_transport(
    protocol: DummyProtocol,
) -> None:
    """_patch_cmd_if_needed queries the transport for evofw3 dynamically.

    In a PooledTransport with a serial HGI80 primary + MQTT callback
    children, the cached ``_is_evofw3`` is False (set during
    ``connection_made`` for the HGI80).  But the pool's
    ``get_extra_info(SZ_IS_EVOFW3)`` returns True because of the
    callback-driven children.  The patch should use the live value
    from the transport, not the stale cached value (issue 1185).
    """
    from ramses_tx.const import SZ_IS_EVOFW3
    from ramses_tx.dtos import CommandDTO as Command

    # Simulate: cached _is_evofw3 is False (HGI80 primary), but the
    # transport reports True (callback-driven MQTT child in the pool)
    protocol._is_evofw3 = False
    protocol._known_hgi = DeviceIdT("18:123456")
    protocol._transport = MagicMock()
    protocol._transport.get_extra_info = lambda name, default=None: (
        True if name == SZ_IS_EVOFW3 else default
    )

    # Command with the real HGI ID as source — HGI80 patch would swap
    # it to 18:000730 if it used the cached _is_evofw3=False.
    original_cmd = Command.from_cli(
        "RQ --- 18:123456 01:222222 --:------ 12B0 001 00"
    )

    patched_cmd = protocol._patch_cmd_if_needed(original_cmd)

    # With dynamic evofw3=True from the transport, the HGI80 patch
    # should NOT fire — the command is returned unchanged.
    assert patched_cmd is original_cmd
    assert patched_cmd.addr1 == "18:123456"
