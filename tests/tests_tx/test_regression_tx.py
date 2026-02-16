"""Regression tests for the transport layer (packet parsing)."""

from pathlib import Path

from syrupy.assertion import SnapshotAssertion

from ramses_tx import Packet
from ramses_tx.exceptions import PacketInvalid

# Navigate up from tests/tests_tx/test_regression_tx.py to tests/fixtures/
# .parents[0] = tests/tests_tx/
# .parents[1] = tests/
FIXTURE_FILE = Path(__file__).parents[1] / "fixtures" / "regression_packets_sorted.txt"


def test_packet_parsing_regression(snapshot: SnapshotAssertion) -> None:
    """Check that all packets in the regression fixture parse consistently.

    Valid packets are snapshotted as their string representation.
    Invalid packets are snapshotted as their exception type and message.
    """
    if not FIXTURE_FILE.exists():
        raise FileNotFoundError(f"Fixture not found at {FIXTURE_FILE}")

    results: list[str] = []

    with FIXTURE_FILE.open("r", encoding="utf-8") as f:
        lines = f.readlines()

    for line_no, line in enumerate(lines, start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Logic mimics ramses_tx.transport.FileTransport._process_line_from_raw
        # Assuming strict 26-char timestamp format from log files
        if len(line) < 27:
            results.append(f"Line {line_no}: Skipped (too short)")
            continue

        dtm_str = line[:26]
        pkt_str = line[27:]

        try:
            pkt = Packet.from_file(dtm_str, pkt_str)
            # Snapshot the deterministic string representation
            results.append(f"VALID:   {pkt}")
        except PacketInvalid as err:
            # Catch expected invalid packets so we verify they STAY invalid
            results.append(f"INVALID: {type(err).__name__}: {err}")
        except ValueError as err:
            # Catch potential parsing errors (e.g. timestamp format)
            results.append(f"ERROR:   {type(err).__name__}: {err}")

    assert snapshot == results
