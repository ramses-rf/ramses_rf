"""RAMSES RF - Background storage worker for async I/O."""

from __future__ import annotations

import contextlib
import logging
import queue
import sqlite3
import threading
from typing import Any

_LOGGER = logging.getLogger(__name__)


class StorageWorker:
    """A background worker thread to handle blocking storage I/O asynchronously."""

    def __init__(self, db_path: str = ":memory:"):
        """Initialize the storage worker thread."""
        self._db_path = db_path
        self._queue: queue.SimpleQueue[tuple[str, Any] | None] = queue.SimpleQueue()
        self._ready_event = threading.Event()

        self._thread = threading.Thread(
            target=self._run,
            name="RamsesStorage",
            daemon=True,  # FIX: Set to True so the process can exit even if stop() is missed
        )
        self._thread.start()

    def wait_for_ready(self, timeout: float | None = None) -> bool:
        """Wait until the database is initialized and ready."""
        return self._ready_event.wait(timeout)

    def submit_packet(self, packet_data: tuple[Any, ...]) -> None:
        """Submit a packet tuple for SQL insertion (Non-blocking)."""
        self._queue.put(("SQL", packet_data))

    def flush(self, timeout: float = 5.0) -> None:
        """Block until all currently pending tasks are processed."""
        if self._queue.empty():
            return

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
                timeout=10.0,  # Increased timeout for locking
            )

            # Enable Write-Ahead Logging for concurrency
            if self._db_path != ":memory:" and "mode=memory" not in self._db_path:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")

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

                task_type, data = item

                if task_type == "MARKER":
                    # Flush requested
                    data.set()
                    continue

                if task_type == "SQL":
                    # Optimization: Batch processing
                    batch = [data]
                    # Drain queue of pending SQL tasks to bulk insert
                    while not self._queue.empty():
                        try:
                            # Peek/get next item without blocking
                            next_item = self._queue.get_nowait()
                            if next_item is None:
                                self._queue.put(None)  # Re-queue poison pill
                                break

                            next_type, next_data = next_item
                            if next_type == "SQL":
                                batch.append(next_data)
                            elif next_type == "MARKER":
                                # Handle marker after this batch
                                self._queue.put(next_item)  # Re-queue marker
                                break
                            else:
                                pass
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

            except Exception as err:
                _LOGGER.exception("StorageWorker encountered an error: %s", err)

        # Cleanup
        conn.close()
        _LOGGER.debug("StorageWorker thread stopped.")
