#!/usr/bin/env python3
"""
tests/test_meek_report.py - Smoke tests for tools/meek_report.py.

Creates a minimal temporary on-disk SQLite database file seeded with
synthetic signal rows, runs meek_report.py via subprocess, and validates
that the HTML output contains expected structure and signal data.

Run with:
    python3 tests/test_meek_report.py [-v]

Requires: no external dependencies beyond stdlib
"""

import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MEEK_REPORT = _REPO_ROOT / "tools" / "meek_report.py"


class TestMeekReportSmoke(unittest.TestCase):
    """Smoke tests for meek_report.py HTML report generation."""

    @classmethod
    def setUpClass(cls):
        if not _MEEK_REPORT.exists():
            raise FileNotFoundError(
                f"meek_report.py not found at {_MEEK_REPORT}"
            )
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmpdir.cleanup)
        cls.tmp = Path(cls._tmpdir.name)
        cls.db_path = cls.tmp / "test.db"
        cls._seed_db(cls.db_path)
        # Run the report once; all passing tests share the cached result.
        cls._result = cls._run_report_cmd()
        out = cls.tmp / "report.html"
        cls._html = out.read_text(encoding="utf-8") if out.exists() else ""
        # Extract the embedded DATA JSON for structured assertions.
        m = re.search(r"const DATA = ({.*?});", cls._html, re.DOTALL)
        cls._data = json.loads(m.group(1)) if m else {}

    @staticmethod
    def _seed_db(db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                source TEXT,
                notes TEXT,
                timestamp_ns INTEGER
            );
            CREATE TABLE IF NOT EXISTS methods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                params TEXT
            );
            CREATE TABLE IF NOT EXISTS examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL REFERENCES signals(id),
                method_id INTEGER NOT NULL REFERENCES methods(id),
                result TEXT,
                confidence REAL,
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        signal_1_id = conn.execute(
            "INSERT INTO signals(source, notes, timestamp_ns) VALUES(?,?,?)",
            ("rf_adapt_intel",
             "snr=8.5dB avg_pow=1.2e-04 papr=3.2dB band=ISM-433 "
             "scores(fsk=0.78) -> fsk_like@0.65",
             1743379200000000000)).lastrowid
        signal_2_id = conn.execute(
            "INSERT INTO signals(source, notes, timestamp_ns) VALUES(?,?,?)",
            ("rf_adapt_intel",
             "snr=5.1dB avg_pow=8.0e-05 papr=4.1dB band=SMETS2 "
             "scores(fsk=0.61) -> fsk_like@0.62",
             1743379260000000000)).lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO methods(name, params) VALUES(?,?)",
            ("modulation_classifier", "{}"))
        method_id = conn.execute(
            "SELECT id FROM methods WHERE name = ?",
            ("modulation_classifier",)).fetchone()[0]
        conn.executemany(
            """
            INSERT INTO examples(signal_id, method_id, result, confidence, notes)
            VALUES(?,?,?,?,?)
            """,
            [
                (signal_1_id, method_id, "fsk_like", 0.65,
                 "seeded smoke-test example for ISM-433 detection"),
                (signal_2_id, method_id, "fsk_like", 0.62,
                 "seeded smoke-test example for SMETS2 detection"),
            ])
        conn.commit()
        conn.close()

    @classmethod
    def _run_report_cmd(
        cls, extra_args: list[str] | None = None
    ) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable,
            str(_MEEK_REPORT),
            "--db", str(cls.db_path),
            "--days", "365",
            "--out", str(cls.tmp / "report.html"),
        ]
        if extra_args:
            cmd += extra_args
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )

    def test_exits_zero(self):
        """meek_report.py must exit 0 on a valid database."""
        self.assertEqual(
            self._result.returncode, 0,
            f"meek_report.py exited {self._result.returncode}:\n"
            f"{self._result.stderr}",
        )

    def test_html_file_created(self):
        """meek_report.py must create the output HTML file."""
        out = self.tmp / "report.html"
        self.assertTrue(out.exists(), "Output HTML file was not created")
        self.assertGreater(
            out.stat().st_size, 0,
            "Output HTML file is empty",
        )

    def test_html_contains_signal_data(self):
        """HTML output must contain counts derived from the seeded DB rows."""
        # Verify the DATA JSON block was found and parsed.
        self.assertTrue(
            self._data,
            "Could not extract const DATA = {...} JSON from report HTML",
        )
        # band_totals are populated by the examples JOIN in league_table();
        # a non-zero count proves the seeded rows were actually queried.
        band_totals = self._data.get("band_totals", {})
        self.assertEqual(
            band_totals.get("ISM-433"), 1,
            f"band_totals['ISM-433'] should be 1; got {band_totals}",
        )
        self.assertEqual(
            band_totals.get("SMETS2"), 1,
            f"band_totals['SMETS2'] should be 1; got {band_totals}",
        )
        # group_totals are aggregated per band-group; both seeded bands belong
        # to 'IoT & Smart Infrastructure', so the total must be 2.
        group_totals = self._data.get("group_totals", {})
        self.assertEqual(
            group_totals.get("IoT & Smart Infrastructure"), 2,
            f"group_totals['IoT & Smart Infrastructure'] should be 2; "
            f"got {group_totals}",
        )

    def test_html_is_valid_structure(self):
        """HTML output must contain basic HTML structure tags."""
        for tag in ("<!DOCTYPE", "<html", "<head", "<body", "<title"):
            self.assertIn(
                tag, self._html,
                f"HTML output is missing expected tag: {tag}",
            )

    def test_missing_db_exits_nonzero(self):
        """meek_report.py must exit non-zero when the DB path does not exist."""
        result = subprocess.run(
            [
                sys.executable,
                str(_MEEK_REPORT),
                "--db", str(self.tmp / "does_not_exist.db"),
                "--out", str(self.tmp / "report_missing.html"),
            ],
            capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(
            result.returncode, 0,
            "meek_report.py should exit non-zero for a missing database",
        )


if __name__ == "__main__":
    unittest.main()
