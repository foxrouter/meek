#!/usr/bin/env python3
"""
tests/test_db_wal.py — Verify that SQLite WAL journal mode is available and
behaves correctly on the host platform.

The C++ Database::open() enables WAL mode immediately after opening the
connection.  These tests confirm that:
  - PRAGMA journal_mode = WAL returns "wal" (i.e. WAL is actually applied,
    not silently ignored as can happen on some networked file-systems).
  - A -wal sidecar file is created once the first write has been made in
    WAL mode.
  - The journal_mode pragma is idempotent when called a second time.

Run with:
    python3 tests/test_db_wal.py [-v]
or via pytest:
    pytest tests/test_db_wal.py
"""

import queue
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path


class TestWalMode(unittest.TestCase):
    """WAL mode availability and basic write behaviour."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._db_path = str(Path(self._tmp) / "test.db")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _open_wal(self) -> sqlite3.Connection:
        """Open a SQLite connection with WAL mode applied, matching the C++
        Database::open() pragma sequence."""
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        row = conn.execute("PRAGMA journal_mode = WAL;").fetchone()
        self.assertIsNotNone(row, "PRAGMA journal_mode returned no result")
        actual_mode = row[0]
        self.assertEqual(
            actual_mode,
            "wal",
            f"Expected journal_mode=wal, got '{actual_mode}'. "
            "WAL mode may not be supported on this filesystem.",
        )
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_wal_mode_is_set(self):
        """Opening a fresh database and applying WAL returns mode 'wal'."""
        conn = self._open_wal()
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        self.assertEqual(mode, "wal")
        conn.close()

    def test_wal_mode_persists_after_reopen(self):
        """WAL mode is persisted in the database file and active on reopen."""
        conn = self._open_wal()
        conn.close()

        # Reopen without explicitly setting the pragma.
        conn2 = sqlite3.connect(self._db_path)
        mode = conn2.execute("PRAGMA journal_mode;").fetchone()[0]
        self.assertEqual(
            mode, "wal",
            "WAL mode should persist across connection re-opens",
        )
        conn2.close()

    def test_wal_sidecar_created_after_write(self):
        """A -wal file is present while a WAL-mode connection has active writes."""
        conn = self._open_wal()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT);"
        )
        conn.execute("INSERT INTO t(v) VALUES ('hello');")
        conn.commit()

        # Check for -wal sidecar while the connection is still open; the file
        # may be removed by an automatic WAL checkpoint on close.
        wal_file = Path(self._db_path + "-wal")
        self.assertTrue(
            wal_file.exists(),
            f"Expected WAL sidecar file at {wal_file}",
        )
        conn.close()

    def test_wal_mode_idempotent(self):
        """Applying PRAGMA journal_mode=WAL twice does not cause an error and
        still returns 'wal'."""
        conn = self._open_wal()
        mode = conn.execute("PRAGMA journal_mode = WAL;").fetchone()[0]
        self.assertEqual(mode, "wal")
        conn.close()

    def _apply_schema(self, conn: sqlite3.Connection) -> None:
        """Apply the db.hpp apply_schema() DDL (tables + indexes) to *conn*.

        Mirrors the C++ apply_schema() so that Python-only tests can verify
        schema behaviour without running the compiled binary."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
              id        INTEGER PRIMARY KEY AUTOINCREMENT,
              timestamp TEXT NOT NULL DEFAULT (datetime('now')),
              source    TEXT,
              notes     TEXT
            );
            CREATE TABLE IF NOT EXISTS methods (
              id     INTEGER PRIMARY KEY AUTOINCREMENT,
              name   TEXT UNIQUE NOT NULL,
              params TEXT
            );
            CREATE TABLE IF NOT EXISTS examples (
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              signal_id  INTEGER NOT NULL REFERENCES signals(id),
              method_id  INTEGER NOT NULL REFERENCES methods(id),
              confidence REAL,
              notes      TEXT,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_examples_signal_id
              ON examples(signal_id);
            CREATE INDEX IF NOT EXISTS idx_examples_method_id
              ON examples(method_id);
            CREATE INDEX IF NOT EXISTS idx_examples_confidence
              ON examples(confidence DESC)
              WHERE confidence IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_signals_timestamp
              ON signals(timestamp DESC);
        """)

    def test_indexes_present(self):
        """Verify that the four performance indexes are present after schema
        creation, matching the CREATE INDEX IF NOT EXISTS statements in db.hpp."""
        conn = self._open_wal()
        self._apply_schema(conn)
        idx_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index';"
            ).fetchall()
        }
        conn.close()
        expected = {
            "idx_examples_signal_id",
            "idx_examples_method_id",
            "idx_examples_confidence",
            "idx_signals_timestamp",
        }
        self.assertTrue(
            expected.issubset(idx_names),
            f"Missing indexes: {expected - idx_names}",
        )

    def test_examples_signal_id_uses_index(self):
        """EXPLAIN QUERY PLAN must show SEARCH (index) not SCAN for
        signal_id lookups — confirms the index is used by SQLite's planner."""
        conn = self._open_wal()
        self._apply_schema(conn)
        plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT confidence FROM examples WHERE signal_id = 1;"
        ).fetchall()
        conn.close()
        plan_text = " ".join(str(row) for row in plan)
        self.assertIn(
            "SEARCH",
            plan_text,
            f"Expected index SEARCH, got: {plan_text}",
        )

    def test_concurrent_write_no_busy_errors(self):
        """Two connections writing concurrently under WAL + busy_timeout must
        produce zero SQLITE_BUSY errors returned to callers.

        Mirrors the C++ fix: sqlite3_busy_timeout(raw, 5000) is set on the
        writer connection so that SQLite retries internally instead of
        immediately surfacing SQLITE_BUSY to the application.
        """
        conn_setup = self._open_wal()
        conn_setup.execute(
            "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT);"
        )
        conn_setup.commit()
        conn_setup.close()

        errors = queue.Queue()
        rows_per_thread = 50
        # Barrier ensures both threads enter the write loop simultaneously,
        # maximizing lock contention and ensuring the busy-timeout path is exercised.
        # 10 s barrier timeout prevents an indefinite hang if one thread
        # fails before reaching barrier.wait() (e.g., connect or PRAGMA error).
        barrier = threading.Barrier(2, timeout=10)

        def _writer(label):
            # timeout=5.0 is the Python sqlite3 equivalent of the C++
            # sqlite3_busy_timeout(raw, 5000) call added to Database::open().
            # Both instruct the SQLite library to retry internally for up to
            # 5 seconds on SQLITE_BUSY before surfacing an error to the caller.
            conn = sqlite3.connect(self._db_path, timeout=5.0)
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            try:
                barrier.wait()
                for i in range(rows_per_thread):
                    conn.execute("INSERT INTO t(v) VALUES (?);", (f"{label}-{i}",))
                    conn.commit()
            except (sqlite3.OperationalError, threading.BrokenBarrierError) as exc:
                errors.put(exc)
            finally:
                conn.close()

        t1 = threading.Thread(target=_writer, args=("a",))
        t2 = threading.Thread(target=_writer, args=("b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Drain the queue into a list for inspection; avoids relying on the
        # internal Queue.queue attribute which is an implementation detail.
        collected_errors = []
        while not errors.empty():
            try:
                collected_errors.append(errors.get_nowait())
            except queue.Empty:
                break
        self.assertEqual(
            collected_errors,
            [],
            f"Expected zero SQLITE_BUSY errors, got: {collected_errors}",
        )

        # Verify all rows were written.
        conn_check = sqlite3.connect(self._db_path)
        count = conn_check.execute("SELECT COUNT(*) FROM t;").fetchone()[0]
        conn_check.close()
        self.assertEqual(
            count,
            rows_per_thread * 2,
            f"Expected {rows_per_thread * 2} rows, got {count}",
        )


if __name__ == "__main__":
    unittest.main()
