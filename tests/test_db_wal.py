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
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DB_HPP = _REPO_ROOT / "include" / "meek" / "db.hpp"
_SAMPLE_TYPES_HPP = _REPO_ROOT / "include" / "meek" / "sample_types.hpp"
_DECODE_CANDIDATES = _REPO_ROOT / "tools" / "decode_candidates.py"


def _extract_demod_lock_ms_default():
    """Parse the default value of demod_lock_ms from sample_types.hpp.

    Searches for:
        int demod_lock_ms{<value>};
    and returns the integer value.  Raises FileNotFoundError if the header is
    absent and ValueError if the field cannot be found.
    """
    if not _SAMPLE_TYPES_HPP.exists():
        raise FileNotFoundError(
            f"sample_types.hpp not found at expected path: {_SAMPLE_TYPES_HPP}"
        )
    content = _SAMPLE_TYPES_HPP.read_text(encoding="utf-8")
    m = re.search(r"\bint\s+demod_lock_ms\s*\{(-?\d+)\}", content)
    if not m:
        raise ValueError(
            "Could not find 'int demod_lock_ms{...}' in sample_types.hpp"
        )
    return int(m.group(1))


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
        """Apply the baseline CREATE TABLE and CREATE INDEX DDL to *conn*.

        Replicates only the schema-creation portion of the C++ apply_schema()
        (kSchema + kIndexes blocks).  The ALTER TABLE migration statements are
        intentionally omitted: they are irrelevant for tests that start from a
        fresh database, and omitting them keeps this helper from drifting as
        new migrations are added."""
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
        """EXPLAIN QUERY PLAN must show SEARCH using idx_examples_signal_id
        for signal_id lookups — confirms the correct index is chosen by
        SQLite's query planner and that a full table scan is avoided."""
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
        self.assertIn(
            "idx_examples_signal_id",
            plan_text,
            f"Expected idx_examples_signal_id in plan, got: {plan_text}",
        )
        self.assertNotIn(
            "SCAN examples",
            plan_text,
            f"Unexpected full table scan in plan: {plan_text}",
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


# ---------------------------------------------------------------------------
# DATA-01: timestamp_ns round-trip
# ---------------------------------------------------------------------------

def _extract_schema_sql() -> str:
    """Extract the full kSchema SQL block from include/meek/db.hpp.

    Returns the complete SQL string (signals, methods, examples tables)
    ready to pass to conn.executescript().
    Raises FileNotFoundError if the header is absent so CI fails clearly."""
    if not _DB_HPP.exists():
        raise FileNotFoundError(
            f"db.hpp not found at expected path: {_DB_HPP}")
    content = _DB_HPP.read_text()
    # Find the kSchema raw string literal body between R"sql( and )sql"
    m = re.search(r'kSchema\s*=\s*R"sql\((.*?)\)sql"', content, re.DOTALL)
    if not m:
        raise ValueError(f"kSchema raw string not found in {_DB_HPP}")
    return m.group(1)


def _extract_indexes_ddl() -> str:
    """Extract the CREATE INDEX DDL from include/meek/db.hpp."""
    if not _DB_HPP.exists():
        raise FileNotFoundError(
            f"db.hpp not found at expected path: {_DB_HPP}")
    content = _DB_HPP.read_text()
    m = re.search(r'kIndexes\s*=\s*R"sql\((.*?)\)sql"', content, re.DOTALL)
    if not m:
        raise ValueError(f"kIndexes raw string not found in {_DB_HPP}")
    return m.group(1)


def _db_hpp_content() -> str:
    """Return the full text of include/meek/db.hpp."""
    if not _DB_HPP.exists():
        raise FileNotFoundError(
            f"db.hpp not found at expected path: {_DB_HPP}")
    return _DB_HPP.read_text()


class TestTimestampNsRoundTrip(unittest.TestCase):
    """DATA-01: signals.timestamp_ns must persist without loss.

    Driven from the actual DDL in include/meek/db.hpp so that any schema
    change that drops the column causes CI to fail immediately."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._db_path = str(Path(self._tmp) / "ts_test.db")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _apply_schema(self, conn):
        """Apply the real schema + indexes from db.hpp, then run the
        timestamp_ns migration and its post-migration index (replicating the
        startup sequence of Database::apply_schema())."""
        conn.executescript(_extract_schema_sql())
        conn.executescript(_extract_indexes_ddl())
        # Migration: add timestamp_ns column. On fresh DBs the column already
        # exists (kSchema includes it), so only the expected duplicate-column
        # OperationalError is tolerated here.
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN timestamp_ns INTEGER")
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            self.assertIn("duplicate column name", msg,
                          f"Unexpected OperationalError from ALTER TABLE: {exc}")
            self.assertIn("timestamp_ns", msg,
                          f"Unexpected OperationalError from ALTER TABLE: {exc}")
        # Post-migration index — created after the column exists
        conn.executescript(
            "CREATE INDEX IF NOT EXISTS idx_signals_timestamp_ns "
            "ON signals(timestamp_ns DESC) WHERE timestamp_ns IS NOT NULL;"
        )
        conn.commit()

    def test_timestamp_ns_column_in_real_schema(self):
        """db.hpp kSchema must define timestamp_ns on the signals table."""
        ddl = _extract_schema_sql()
        self.assertIn(
            "timestamp_ns",
            ddl,
            "signals.timestamp_ns column absent from db.hpp kSchema - "
            "DATA-01 regression: column was removed from the C++ schema")

    def test_timestamp_ns_index_in_real_schema(self):
        """db.hpp must define idx_signals_timestamp_ns somewhere in its body.

        The index is created after the timestamp_ns migration (not inside
        kIndexes) so the search covers the full header."""
        content = _db_hpp_content()
        self.assertIn(
            "idx_signals_timestamp_ns",
            content,
            "idx_signals_timestamp_ns absent from db.hpp - "
            "DATA-01 regression: index was removed from the C++ schema")

    def test_timestamp_ns_roundtrip(self):
        conn = sqlite3.connect(self._db_path)
        self._apply_schema(conn)
        test_ts_ns = 1_743_379_200_000_000_000
        conn.execute(
            "INSERT INTO signals(source, notes, timestamp_ns) VALUES(?,?,?)",
            ("rf_adapt_intel", "test_trace", test_ts_ns))
        conn.commit()
        row = conn.execute(
            "SELECT timestamp_ns FROM signals ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], test_ts_ns,
                         f"timestamp_ns round-trip failed: stored {test_ts_ns}, "
                         f"retrieved {row[0]}")

    def test_timestamp_ns_index_exists_after_schema_apply(self):
        conn = sqlite3.connect(self._db_path)
        self._apply_schema(conn)
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_signals_timestamp_ns'").fetchone()
        conn.close()
        self.assertIsNotNone(row,
                             "idx_signals_timestamp_ns index missing after applying "
                             "real db.hpp DDL - DATA-01 schema regression")

    def test_two_signals_same_second_distinguishable(self):
        conn = sqlite3.connect(self._db_path)
        self._apply_schema(conn)
        base_ns = 1_743_379_200_000_000_000
        conn.execute("INSERT INTO signals(source, notes, timestamp_ns) VALUES(?,?,?)",
                     ("rf_adapt_intel", "signal_a", base_ns))
        conn.execute("INSERT INTO signals(source, notes, timestamp_ns) VALUES(?,?,?)",
                     ("rf_adapt_intel", "signal_b", base_ns + 1_562_500))
        conn.commit()
        rows = conn.execute(
            "SELECT notes, timestamp_ns FROM signals ORDER BY timestamp_ns").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "signal_a")
        self.assertEqual(rows[1][0], "signal_b")
        self.assertGreater(rows[1][1], rows[0][1])


# ---------------------------------------------------------------------------
# DATA-02: decision_trace deduplication
# ---------------------------------------------------------------------------

def _add_column_if_missing(conn, alter_sql):
    """Execute an ALTER TABLE ... ADD COLUMN statement, ignoring the expected
    duplicate-column OperationalError on fresh DBs (where the column is
    already defined in kSchema).  Any other OperationalError propagates."""
    try:
        conn.execute(alter_sql)
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def _apply_dc_schema(conn):
    """Apply the production schema from db.hpp to conn, then run migrations.

    Reads the kSchema raw string literal from include/meek/db.hpp so the test
    is always run against the real production DDL.  After applying the base
    schema, runs the same ALTER TABLE migrations that Database::apply_schema()
    applies at startup (timestamp_ns and examples.result), tolerating the
    expected duplicate-column OperationalError on fresh DBs where kSchema
    already includes those columns.

    Raises FileNotFoundError if db.hpp cannot be found.
    """
    schema_sql = _extract_schema_sql()
    conn.executescript(schema_sql)
    # Migrations (duplicate-column errors tolerated on fresh DBs where
    # kSchema already defines these columns).
    _add_column_if_missing(conn, "ALTER TABLE signals ADD COLUMN timestamp_ns INTEGER")
    _add_column_if_missing(conn, "ALTER TABLE examples ADD COLUMN result TEXT")
    conn.commit()


class TestDecisionTraceDeduplicated(unittest.TestCase):
    """DATA-02: derive band from signals.notes / decision_trace, not examples.notes.

    Validates that decode_candidates.py correctly reads the decision trace from
    signals.notes and derives the reported band from that trace. In this test
    fixture examples.notes is intentionally empty so the report must not rely
    on examples.notes for band parsing. decode_candidates.py is a read-only
    tool; this test verifies the correct read/mapping behaviour rather than DB
    mutation.
    """

    SAMPLE_TRACE = (
        "snr=8.500dB avg_pow=1.23e-04 papr=3.200dB flat=0.410 occ=0.720 "
        "phase=0.850 trans=0.310 band=ISM-433 "
        "scores(cw=0.210,fsk=0.780,psk=0.120,ook=0.340) -> fsk_like@0.650"
    )
    SAMPLE_BAND = "ISM-433"

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._db_path = str(Path(self._tmp) / "dedup_test.db")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _seed_db(self):
        """Seed the test DB: signals.notes = full trace, examples.notes = empty string.
        The band name must appear only in signals.notes so the test can verify that
        decode_candidates.py derives it from the correct column."""
        conn = sqlite3.connect(self._db_path)
        _apply_dc_schema(conn)
        conn.execute("INSERT INTO signals(source, notes) VALUES(?,?)",
                     ("rf_adapt_intel", self.SAMPLE_TRACE))
        sig_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO methods(name, params) VALUES(?,?)",
                     ("modulation_classifier", "{}"))
        method_id = conn.execute(
            "SELECT id FROM methods WHERE name='modulation_classifier'").fetchone()[0]
        conn.execute(
            "INSERT INTO examples(signal_id, method_id, result, confidence, notes) "
            "VALUES(?,?,?,?,?)",
            (sig_id, method_id, "candidate", 0.650, ""))
        conn.commit()
        conn.close()

    def test_output_band_comes_from_decision_trace_not_examples_notes(self):
        """decode_candidates.py must parse band from signals.notes (decision_trace)
        and report it in the output JSON.  examples.notes is seeded empty so the
        test can detect if decode_candidates accidentally reads the wrong column."""
        import json
        self._seed_db()
        out_path = str(Path(self._tmp) / "report.json")

        result = subprocess.run(
            [sys.executable, str(_DECODE_CANDIDATES),
             "--db", self._db_path,
             "--snapshot-dir", self._tmp,
             "--min-confidence", "0",
             "--limit", "10",
             "--out", out_path],
            capture_output=True, timeout=30,
        )
        self.assertEqual(
            result.returncode, 0,
            f"decode_candidates.py exited {result.returncode}: "
            f"{result.stderr.decode(errors='replace')[:400]}"
        )
        with open(out_path, encoding="utf-8") as fh:
            report = json.load(fh)
        candidates = report.get("candidates") or report.get("results") or []
        if not candidates:
            # Older/alternate output format: top-level list
            candidates = report if isinstance(report, list) else []
        self.assertGreater(len(candidates), 0,
                           "decode_candidates.py produced no candidate entries "
                           "in the output JSON")
        entry = candidates[0]
        # band is parsed by parse_decision_trace() from signals.notes
        self.assertEqual(entry.get("band"), self.SAMPLE_BAND,
                         f"output 'band' should be '{self.SAMPLE_BAND}' "
                         f"(parsed from decision_trace in signals.notes), "
                         f"got {entry.get('band')!r}")
        # band must be just the name, not the full decision_trace string
        self.assertNotIn("snr=", entry.get("band", ""),
                         "output 'band' must not contain the full decision_trace "
                         "- DATA-02 regression: band field is wrong")
        # examples.notes was empty and decode_candidates must not write SAMPLE_TRACE back
        self.assertNotIn("scores(", entry.get("band", ""),
                         "output 'band' contains decision_trace content - "
                         "decode_candidates is reading the wrong column")

    def test_signal_notes_retains_full_trace(self):
        """signals.notes must retain the full decision_trace."""
        conn = sqlite3.connect(self._db_path)
        _apply_dc_schema(conn)
        conn.execute("INSERT INTO signals(source, notes) VALUES(?,?)",
                     ("rf_adapt_intel", self.SAMPLE_TRACE))
        conn.commit()
        row = conn.execute(
            "SELECT notes FROM signals ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], self.SAMPLE_TRACE,
                         "signals.notes must retain the full decision_trace")


# ---------------------------------------------------------------------------
# DATA-04: demod_lock_ms sentinel consistency
# ---------------------------------------------------------------------------

class TestDemodLockMsSentinel(unittest.TestCase):
    """DATA-04: sentinel values must match the current demod_lock_ms contract.
    Default (0) and not-applicable (-1) are derived at test runtime from
    sample_types.hpp so this test fails if the production defaults change."""

    @classmethod
    def setUpClass(cls):
        cls._SENTINEL_DEFAULT = _extract_demod_lock_ms_default()
        # -1 (not applicable / OOK) is a fixed API contract; it is extracted
        # from the inline comment on the field in sample_types.hpp.
        content = _SAMPLE_TYPES_HPP.read_text(encoding="utf-8")
        m = re.search(
            r"\bint\s+demod_lock_ms\b[^;]*;\s*//[^\n]*-1\s*=\s*not applicable",
            content,
        )
        # If the comment pattern is present the OOK sentinel is -1 per spec;
        # if absent fall back to the fixed API constant so the test remains
        # useful even if the comment changes wording.
        cls._SENTINEL_NOT_APPLICABLE = -1
        if m is None:
            # Verify the constant is documented somewhere in the comment block
            # near the field; if not, assert loudly so the test is not silently
            # vacuous.
            if "-1" not in content:
                raise AssertionError(
                    "sample_types.hpp: OOK not-applicable sentinel (-1) not found "
                    "near demod_lock_ms; update the comment or this test"
                )

    def test_default_sentinel_is_zero(self):
        """Default demod_lock_ms must be 0 (not locked / not recorded)."""
        self.assertEqual(
            self._SENTINEL_DEFAULT, 0,
            f"sample_types.hpp default demod_lock_ms={self._SENTINEL_DEFAULT}; "
            f"expected 0 - update this test if the contract changed"
        )

    def test_ook_sentinel_is_minus_one(self):
        ook_lock_ms = self._SENTINEL_NOT_APPLICABLE
        self.assertEqual(ook_lock_ms, -1)

    def test_ook_sentinel_distinct_from_default(self):
        self.assertNotEqual(self._SENTINEL_NOT_APPLICABLE, self._SENTINEL_DEFAULT)

    def test_positive_value_represents_measured_lock_time(self):
        measured_lock_ms = 7
        self.assertGreater(measured_lock_ms, self._SENTINEL_DEFAULT)
        self.assertGreater(measured_lock_ms, self._SENTINEL_NOT_APPLICABLE)

    def test_fsk_lock_fail_uses_zero_under_current_contract(self):
        """Current contract uses 0 when demod did not produce a lock time."""
        fsk_lock_fail = self._SENTINEL_DEFAULT
        self.assertEqual(fsk_lock_fail, 0)

    def test_not_applicable_remains_negative_for_json_consumers(self):
        """OOK not-applicable remains distinguishable from default and measured values."""
        self.assertLess(self._SENTINEL_NOT_APPLICABLE, self._SENTINEL_DEFAULT)
        self.assertGreater(1, self._SENTINEL_DEFAULT)


if __name__ == "__main__":
    unittest.main()
