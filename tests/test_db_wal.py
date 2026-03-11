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

import sqlite3
import tempfile
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


if __name__ == "__main__":
    unittest.main()
