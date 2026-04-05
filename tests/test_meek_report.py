#!/usr/bin/env python3
"""
tests/test_meek_report.py - Smoke tests for tools/meek_report.py.

Creates a minimal in-memory SQLite database seeded with synthetic signal
rows, runs meek_report.py via subprocess, and validates that the HTML
output contains expected structure and signal data.

Run with:
    python3 tests/test_meek_report.py [-v]

Requires: no external dependencies beyond stdlib
"""

import json
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
        conn.execute(
            "INSERT INTO signals(source, notes, timestamp_ns) VALUES(?,?,?)",
            ("rf_adapt_intel",
             "snr=8.5dB avg_pow=1.2e-04 papr=3.2dB band=ISM-433 "
             "scores(fsk=0.78) -> fsk_like@0.65",
             1743379200000000000))
        conn.execute(
            "INSERT INTO signals(source, notes, timestamp_ns) VALUES(?,?,?)",
            ("rf_adapt_intel",
             "snr=5.1dB avg_pow=8.0e-05 papr=4.1dB band=SMETS2 "
             "scores(fsk=0.61) -> fsk_like@0.62",
             1743379260000000000))
        conn.execute(
            "INSERT OR IGNORE INTO methods(name, params) VALUES(?,?)",
            ("modulation_classifier", "{}"))
        conn.commit()
        conn.close()

    def _run_report(self, extra_args: list | None = None) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable,
            str(_MEEK_REPORT),
            "--db", str(self.db_path),
            "--days", "365",
            "--out", str(self.tmp / "report.html"),
        ]
        if extra_args:
            cmd += extra_args
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )

    def test_exits_zero(self):
        """meek_report.py must exit 0 on a valid database."""
        result = self._run_report()
        self.assertEqual(
            result.returncode, 0,
            f"meek_report.py exited {result.returncode}:\n{result.stderr}",
        )

    def test_html_file_created(self):
        """meek_report.py must create the output HTML file."""
        result = self._run_report()
        self.assertEqual(
            result.returncode, 0,
            f"meek_report.py exited {result.returncode}:\n{result.stderr}",
        )
        out = self.tmp / "report.html"
        self.assertTrue(
            out.exists(),
            "Output HTML file was not created",
        )
        self.assertGreater(
            out.stat().st_size, 0,
            "Output HTML file is empty",
        )

    def test_html_contains_signal_data(self):
        """HTML output must contain data from the seeded signals."""
        result = self._run_report()
        self.assertEqual(
            result.returncode, 0,
            f"meek_report.py exited {result.returncode}:\n{result.stderr}",
        )
        html = (self.tmp / "report.html").read_text(encoding="utf-8")
        self.assertIn(
            "ISM-433", html,
            "HTML output does not contain expected band name ISM-433",
        )

    def test_html_is_valid_structure(self):
        """HTML output must contain basic HTML structure tags."""
        result = self._run_report()
        self.assertEqual(
            result.returncode, 0,
            f"meek_report.py exited {result.returncode}:\n{result.stderr}",
        )
        html = (self.tmp / "report.html").read_text(encoding="utf-8")
        for tag in ("<!DOCTYPE", "<html", "<head", "<body", "<title"):
            self.assertIn(
                tag, html,
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
