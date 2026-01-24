"""RAMSES RF - Background storage worker for async I/O."""

from __future__ import annotations

import contextlib
import logging
import queue
import sqlite3
import threading
from typing import Any, NamedTuple

_LOGGER = logging.getLogger(__name__)


class PacketLogEntry(NamedTuple):
    """Represents a packet to be written to the database."""

    dtm: Any
    verb: str
    src: str
    dst: str
    code: str
    ctx: str | None
    hdr: str
    plk: str


class PruneRequest(NamedTuple):
    """Represents a request to prune old records."""

    dtm_limit: Any


QueueItem = PacketLogEntry | PruneRequest | tuple[str, Any] | None


class StorageWorker:
    """A background worker thread to handle blocking storage I/O asynchronously."""

    def __init__(self, db_path: str = ":memory:") -> None:
        """Initialize the storage worker thread."""
        self._db_path = db_path
        self._queue: queue.SimpleQueue[QueueItem] = queue.SimpleQueue()
        self._ready_event = threading.Event()

        self._thread = threading.Thread(
            target=self._run,
            name="RamsesStorage",
            daemon=True,  # Allows process exit even if stop() is missed
        )
        self._thread.start()

    def wait_for_ready(self, timeout: float | None = None) -> bool:
        """Wait until the database is initialized and ready."""
        return self._ready_event.wait(timeout)

    def submit_packet(self, packet: PacketLogEntry) -> None:
        """Submit a packet tuple for SQL insertion (Non-blocking)."""
        self._queue.put(packet)

    def submit_prune(self, dtm_limit: Any) -> None:
        """Submit a prune request for SQL deletion (Non-blocking)."""
        self._queue.put(PruneRequest(dtm_limit))

    def flush(self, timeout: float = 10.0) -> None:
        """Block until all currently pending tasks are processed."""
        # We inject a special marker into the queue
        sentinel = threading.Event()
        self._queue.put(("MARKER", sentinel))

        # Wait for the worker to set the sentinel
        if not sentinel.wait(timeout):
            _LOGGER.warning("StorageWorker flush timed out")

    def stop(self) -> None:
        """Signal the worker to stop processing and close resources."""
        self._queue.put(None)  # Poison pill
        self._thread.join()

    def _init_db(self, conn: sqlite3.Connection) -> None:
        """Initialize the database schema."""
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                dtm    DTM      NOT NULL PRIMARY KEY,
                verb   TEXT(2)  NOT NULL,
                src    TEXT(12) NOT NULL,
                dst    TEXT(12) NOT NULL,
                code   TEXT(4)  NOT NULL,
                ctx    TEXT,
                hdr    TEXT     NOT NULL UNIQUE,
                plk    TEXT     NOT NULL
            )
            """
        )
        # Create indexes to speed up future reads
        for col in ("verb", "src", "dst", "code", "ctx", "hdr"):
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{col} ON messages ({col})")
        conn.commit()

    def _run(self) -> None:
        """The main loop running in the background thread."""
        _LOGGER.debug("StorageWorker thread started.")

        # Setup SQLite connection in this thread
        try:
            # uri=True allows opening "file::memory:?cache=shared"
            conn = sqlite3.connect(
                self._db_path,
                detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
                check_same_thread=False,
                uri=True,
                timeout=10.0,
            )

            # Enable Write-Ahead Logging for concurrency
            if self._db_path != ":memory:" and "mode=memory" not in self._db_path:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
            elif "cache=shared" in self._db_path:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("PRAGMA read_uncommitted = true")

            self._init_db(conn)
            self._ready_event.set()  # Signal that tables exist
        except sqlite3.Error as exc:
            _LOGGER.error("Failed to initialize storage database: %s", exc)
            self._ready_event.set()  # Avoid blocking waiters forever
            return

        while True:
            try:
                # Block here waiting for work
                item = self._queue.get()

                if item is None:  # Shutdown signal
                    break

                if isinstance(item, PacketLogEntry):
                    # Optimization: Batch processing
                    batch = [item]
                    # Drain queue of pending SQL tasks to bulk insert
                    while not self._queue.empty():
                        try:
                            # Peek/get next item without blocking
                            next_item = self._queue.get_nowait()
                            if next_item is None:
                                self._queue.put(None)  # Re-queue poison pill
                                break

                            if isinstance(next_item, PacketLogEntry):
                                batch.append(next_item)
                            else:
                                # Handle other types after this batch
                                self._queue.put(next_item)  # Re-queue
                                break
                        except queue.Empty:
                            break

                    try:
                        conn.executemany(
                            """
                            INSERT OR REPLACE INTO messages
                            (dtm, verb, src, dst, code, ctx, hdr, plk)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            batch,
                        )
                        conn.commit()
                    except sqlite3.Error as err:
                        _LOGGER.error("SQL Write Failed: %s", err)

                elif isinstance(item, PruneRequest):
                    try:
                        conn.execute(
                            "DELETE FROM messages WHERE dtm < ?", (item.dtm_limit,)
                        )
                        conn.commit()
                        _LOGGER.debug("Pruned records older than %s", item.dtm_limit)
                    except sqlite3.Error as err:
                        _LOGGER.error("SQL Prune Failed: %s", err)

                elif isinstance(item, tuple) and item[0] == "MARKER":
                    # Flush requested
                    item[1].set()

            except Exception as err:
                _LOGGER.exception("StorageWorker encountered an error: %s", err)

        # Cleanup
        conn.close()
        _LOGGER.debug("StorageWorker thread stopped.")
