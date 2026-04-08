#!/usr/bin/env python3
"""
tests/test_meek_report_queries.py — Unit tests for the query and utility
functions in tools/meek_report.py.

Imports meek_report directly (no subprocess) and exercises each function
against a minimal in-memory SQLite database so that regressions in SQL
logic, bucketing, 0-fill, and HTML helpers are caught quickly without a
full HTML render pass.

Run with:
    python3 tests/test_meek_report_queries.py [-v]

Requires: no external dependencies beyond stdlib
"""

import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tools"))
import meek_report as mr  # noqa: E402


# ---------------------------------------------------------------------------
# Shared DB fixture
# ---------------------------------------------------------------------------

_SCHEMA = """
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
"""


def _make_db(rows=None):
    """Return an in-memory connection seeded with optional signal rows.

    rows: list of (notes_str, confidence, timestamp_str) tuples.
    Each row's notes is used verbatim as signals.notes; the timestamp is
    inserted as-is so tests can control temporal filtering.
    """
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT OR IGNORE INTO methods(name, params) VALUES(?,?)",
        ("modulation_classifier", "{}"),
    )
    method_id = con.execute(
        "SELECT id FROM methods WHERE name=?", ("modulation_classifier",)
    ).fetchone()[0]
    if rows:
        for notes, conf, ts in rows:
            sig_id = con.execute(
                "INSERT INTO signals(timestamp, source, notes) VALUES(?,?,?)",
                (ts, "rf_adapt_intel", notes),
            ).lastrowid
            con.execute(
                "INSERT INTO examples(signal_id, method_id, result, confidence)"
                " VALUES(?,?,?,?)",
                (sig_id, method_id, "candidate", conf),
            )
    con.commit()
    return con


def _now_ts():
    """Return a timestamp string for the current UTC time, always within any days window."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _days_ago_ts(n):
    """Return a timestamp string for N days ago in UTC."""
    dt = datetime.now(timezone.utc) - timedelta(days=n)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _days_ago_date(n):
    """Return a date string for N days ago in UTC."""
    dt = datetime.now(timezone.utc) - timedelta(days=n)
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Tests: band_filter
# ---------------------------------------------------------------------------

class TestBandFilter(unittest.TestCase):

    def test_produces_sql_like_pattern(self):
        self.assertEqual(mr.band_filter("ISM-433"), "%band=ISM-433%")

    def test_different_band(self):
        self.assertEqual(mr.band_filter("ADS-B"), "%band=ADS-B%")

    def test_empty_string(self):
        self.assertEqual(mr.band_filter(""), "%band=%")


# ---------------------------------------------------------------------------
# Tests: summary_counts
# ---------------------------------------------------------------------------

class TestSummaryCounts(unittest.TestCase):

    def test_empty_db_returns_zeros(self):
        con = _make_db()
        result = mr.summary_counts(con, days=30)
        self.assertEqual(result["total_signals"], 0)
        self.assertEqual(result["total_examples"], 0)
        self.assertEqual(result["recent_signals"], 0)
        self.assertEqual(result["oldest"], "—")
        self.assertEqual(result["newest"], "—")

    def test_single_row(self):
        con = _make_db([("band=ISM-433 snr=10dB", 0.75, _now_ts())])
        result = mr.summary_counts(con, days=365)
        self.assertEqual(result["total_signals"], 1)
        self.assertEqual(result["total_examples"], 1)
        self.assertNotEqual(result["oldest"], "—")
        self.assertNotEqual(result["newest"], "—")

    def test_recent_window_excludes_old_signals(self):
        rows = [
            ("band=ISM-433", 0.8, _days_ago_ts(400)),  # old: >365 days ago
            ("band=ISM-433", 0.7, _now_ts()),           # recent: today
        ]
        con = _make_db(rows)
        result = mr.summary_counts(con, days=30)
        # total_signals includes all rows regardless of window
        self.assertEqual(result["total_signals"], 2)
        # recent_signals should be 1 (only today's row falls within 30 days)
        self.assertEqual(result["recent_signals"], 1)


# ---------------------------------------------------------------------------
# Tests: league_table
# ---------------------------------------------------------------------------

class TestLeagueTable(unittest.TestCase):

    def test_returns_list_with_all_bands(self):
        con = _make_db()
        result = mr.league_table(con, days=30)
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        # Every known band must appear exactly once
        band_names = [r["band"] for r in result]
        self.assertEqual(len(band_names), len(set(band_names)))
        for band in mr.ALL_BANDS:
            self.assertIn(band, band_names)

    def test_empty_db_all_counts_zero(self):
        con = _make_db()
        result = mr.league_table(con, days=30)
        for row in result:
            self.assertEqual(row["count"], 0)
            self.assertEqual(row["avg_conf"], 0.0)
            self.assertEqual(row["last_seen"], "—")

    def test_sorted_by_count_descending(self):
        rows = [
            ("band=ISM-433 snr=10dB", 0.75, _now_ts()),
            ("band=ISM-433 snr=11dB", 0.80, _now_ts()),
            ("band=ADS-B snr=15dB", 0.90, _now_ts()),
        ]
        con = _make_db(rows)
        result = mr.league_table(con, days=365)
        counts = [r["count"] for r in result]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_ism433_count_matches_inserted_rows(self):
        rows = [("band=ISM-433", 0.70, _now_ts())] * 3
        con = _make_db(rows)
        result = mr.league_table(con, days=365)
        ism = next(r for r in result if r["band"] == "ISM-433")
        self.assertEqual(ism["count"], 3)
        self.assertAlmostEqual(ism["avg_conf"], 0.70, places=2)
    def test_required_keys_present(self):
        con = _make_db()
        result = mr.league_table(con, days=30)
        for row in result:
            for key in ("band", "count", "avg_conf", "last_seen"):
                self.assertIn(key, row)


# ---------------------------------------------------------------------------
# Tests: daily_series
# ---------------------------------------------------------------------------

class TestDailySeries(unittest.TestCase):

    def test_empty_db_returns_empty_list(self):
        con = _make_db()
        result = mr.daily_series(con, "ISM-433", days=7)
        self.assertEqual(result, [])

    def test_returns_dict_with_day_and_count(self):
        rows = [("band=ISM-433", 0.7, _now_ts())]
        con = _make_db(rows)
        result = mr.daily_series(con, "ISM-433", days=365)
        self.assertEqual(len(result), 1)
        self.assertIn("day", result[0])
        self.assertIn("count", result[0])
        self.assertEqual(result[0]["count"], 1)

    def test_other_bands_not_included(self):
        rows = [
            ("band=ISM-433", 0.7, _now_ts()),
            ("band=ADS-B", 0.9, _now_ts()),
        ]
        con = _make_db(rows)
        result = mr.daily_series(con, "ISM-433", days=365)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["count"], 1)

    def test_multiple_days(self):
        rows = [
            ("band=ISM-433", 0.7, _days_ago_ts(4) + ""),
            ("band=ISM-433", 0.8, _days_ago_ts(3) + ""),
            ("band=ISM-433", 0.9, _days_ago_ts(2) + ""),
        ]
        con = _make_db(rows)
        result = mr.daily_series(con, "ISM-433", days=365)
        self.assertEqual(len(result), 3)
        days = [r["day"] for r in result]
        self.assertEqual(days, sorted(days))


# ---------------------------------------------------------------------------
# Tests: hourly_series
# ---------------------------------------------------------------------------

class TestHourlySeries(unittest.TestCase):

    def test_returns_exactly_24_entries(self):
        con = _make_db()
        result = mr.hourly_series(con, "ISM-433", days=30)
        self.assertEqual(len(result), 24)

    def test_all_hours_present(self):
        con = _make_db()
        result = mr.hourly_series(con, "ISM-433", days=30)
        hours = [r["hour"] for r in result]
        self.assertEqual(hours, list(range(24)))

    def test_missing_hours_filled_with_zero(self):
        # Insert a single signal at hour 12; all other hours must be 0
        ts = _days_ago_date(1) + " 12:30:00"
        rows = [("band=ISM-433", 0.7, ts)]
        con = _make_db(rows)
        result = mr.hourly_series(con, "ISM-433", days=365)
        self.assertEqual(len(result), 24)
        for entry in result:
            if entry["hour"] == 12:
                self.assertEqual(entry["count"], 1)
            else:
                self.assertEqual(entry["count"], 0)

    def test_multiple_signals_at_same_hour(self):
        ts1 = _days_ago_date(1) + " 09:00:00"
        ts2 = _days_ago_date(1) + " 09:30:00"
        rows = [
            ("band=ISM-433", 0.7, ts1),
            ("band=ISM-433", 0.8, ts2),
        ]
        con = _make_db(rows)
        result = mr.hourly_series(con, "ISM-433", days=365)
        hour9 = next(r for r in result if r["hour"] == 9)
        self.assertEqual(hour9["count"], 2)


# ---------------------------------------------------------------------------
# Tests: confidence_distribution
# ---------------------------------------------------------------------------

class TestConfidenceDistribution(unittest.TestCase):

    def test_empty_db_returns_empty_list(self):
        con = _make_db()
        result = mr.confidence_distribution(con, days=30)
        self.assertEqual(result, [])

    def test_high_confidence_bucket(self):
        rows = [("band=ISM-433", 0.95, _now_ts())]
        con = _make_db(rows)
        result = mr.confidence_distribution(con, days=365)
        labels = [r["label"] for r in result]
        self.assertTrue(
            any("Very High" in label for label in labels),
            f"'Very High' bucket missing; got: {labels}",
        )

    def test_each_bucket_threshold(self):
        rows = [
            ("band=ISM-433", 0.95, _now_ts()),   # Very High >=0.90
            ("band=ISM-433", 0.75, _now_ts()),   # High 0.70-0.89
            ("band=ISM-433", 0.55, _now_ts()),   # Medium 0.50-0.69
            ("band=ISM-433", 0.35, _now_ts()),   # Low 0.30-0.49
            ("band=ISM-433", 0.10, _now_ts()),   # Noise <0.30
        ]
        con = _make_db(rows)
        result = mr.confidence_distribution(con, days=365)
        total = sum(r["count"] for r in result)
        self.assertEqual(total, 5)
        # All 5 buckets should be present
        self.assertEqual(len(result), 5)

    def test_result_keys(self):
        rows = [("band=ADS-B", 0.8, _now_ts())]
        con = _make_db(rows)
        result = mr.confidence_distribution(con, days=365)
        for row in result:
            self.assertIn("label", row)
            self.assertIn("count", row)


# ---------------------------------------------------------------------------
# Tests: group_totals
# ---------------------------------------------------------------------------

class TestGroupTotals(unittest.TestCase):

    def test_empty_db_all_zero(self):
        con = _make_db()
        result = mr.group_totals(con, days=30)
        for group in mr.BAND_GROUPS:
            self.assertEqual(result[group], 0)

    def test_iot_group_counts_ism_433(self):
        rows = [("band=ISM-433", 0.7, _now_ts())] * 2
        con = _make_db(rows)
        result = mr.group_totals(con, days=365)
        self.assertEqual(result["IoT & Smart Infrastructure"], 2)

    def test_aviation_group_counts_adsb(self):
        rows = [("band=ADS-B", 0.9, _now_ts())]
        con = _make_db(rows)
        result = mr.group_totals(con, days=365)
        self.assertEqual(result["Aviation"], 1)

    def test_returns_all_groups(self):
        con = _make_db()
        result = mr.group_totals(con, days=30)
        for group in mr.BAND_GROUPS:
            self.assertIn(group, result)

    def test_mixed_groups(self):
        rows = [
            ("band=ISM-433", 0.7, _now_ts()),
            ("band=ADS-B", 0.9, _now_ts()),
            ("band=AIS-A", 0.8, _now_ts()),
        ]
        con = _make_db(rows)
        result = mr.group_totals(con, days=365)
        self.assertEqual(result["IoT & Smart Infrastructure"], 1)
        self.assertEqual(result["Aviation"], 1)
        self.assertEqual(result["Maritime"], 1)


# ---------------------------------------------------------------------------
# Tests: recent_detections
# ---------------------------------------------------------------------------

class TestRecentDetections(unittest.TestCase):

    def test_empty_db_returns_empty_list(self):
        con = _make_db()
        result = mr.recent_detections(con, limit=50)
        self.assertEqual(result, [])

    def test_returned_keys(self):
        rows = [("band=ISM-433 snr=10dB", 0.8, _now_ts())]
        con = _make_db(rows)
        result = mr.recent_detections(con, limit=10)
        self.assertEqual(len(result), 1)
        for key in ("timestamp", "band", "conf", "notes"):
            self.assertIn(key, result[0])

    def test_band_extracted_from_notes(self):
        rows = [("band=SMETS2 snr=12dB fsk_like", 0.75, _now_ts())]
        con = _make_db(rows)
        result = mr.recent_detections(con, limit=10)
        # 'band=' prefix should be stripped
        self.assertEqual(result[0]["band"], "SMETS2")

    def test_notes_without_band_returns_unknown(self):
        rows = [("snr=12dB fsk_like (no band)", 0.5, _now_ts())]
        con = _make_db(rows)
        result = mr.recent_detections(con, limit=10)
        self.assertEqual(result[0]["band"], "unknown")

    def test_limit_respected(self):
        rows = [("band=ISM-433", 0.7, _now_ts())] * 20
        con = _make_db(rows)
        result = mr.recent_detections(con, limit=5)
        self.assertLessEqual(len(result), 5)

    def test_notes_truncated_to_120_chars(self):
        long_notes = "band=ISM-433 " + "x" * 200
        rows = [(long_notes, 0.7, _now_ts())]
        con = _make_db(rows)
        result = mr.recent_detections(con, limit=10)
        self.assertLessEqual(len(result[0]["notes"]), 120)

    def test_sorted_most_recent_first(self):
        rows = [
            ("band=ISM-433", 0.7, _days_ago_ts(4)),
            ("band=ISM-433", 0.8, _days_ago_ts(2)),
            ("band=ISM-433", 0.9, _days_ago_ts(3)),
        ]
        con = _make_db(rows)
        result = mr.recent_detections(con, limit=10)
        timestamps = [r["timestamp"] for r in result]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))


# ---------------------------------------------------------------------------
# Tests: elt_alerts
# ---------------------------------------------------------------------------

class TestEltAlerts(unittest.TestCase):

    def test_empty_db_returns_empty_list(self):
        con = _make_db()
        result = mr.elt_alerts(con)
        self.assertEqual(result, [])

    def test_elt_alert_returned(self):
        rows = [("band=ELT-406 snr=20dB", 0.9, _now_ts())]
        con = _make_db(rows)
        result = mr.elt_alerts(con)
        self.assertEqual(len(result), 1)
        self.assertIn("band=ELT-406", result[0]["notes"])

    def test_non_elt_signals_excluded(self):
        rows = [
            ("band=ISM-433 snr=10dB", 0.7, _now_ts()),
            ("band=ELT-406 snr=20dB", 0.9, _now_ts()),
        ]
        con = _make_db(rows)
        result = mr.elt_alerts(con)
        self.assertEqual(len(result), 1)
        self.assertIn("ELT-406", result[0]["notes"])

    def test_returns_dict_rows(self):
        rows = [("band=ELT-406 snr=20dB", 0.9, _now_ts())]
        con = _make_db(rows)
        result = mr.elt_alerts(con)
        self.assertIsInstance(result[0], dict)
        for key in ("id", "timestamp", "conf", "notes"):
            self.assertIn(key, result[0])

    def test_limit_20_results(self):
        rows = [("band=ELT-406 snr=20dB", 0.9, _now_ts())] * 30
        con = _make_db(rows)
        result = mr.elt_alerts(con)
        self.assertLessEqual(len(result), 20)


# ---------------------------------------------------------------------------
# Tests: tetra_daily
# ---------------------------------------------------------------------------

class TestTetraDaily(unittest.TestCase):

    def test_empty_db_returns_empty_list(self):
        con = _make_db()
        result = mr.tetra_daily(con, days=30)
        self.assertEqual(result, [])

    def test_tetra_signal_counted(self):
        rows = [("band=TETRA snr=8dB", 0.6, _now_ts())]
        con = _make_db(rows)
        result = mr.tetra_daily(con, days=365)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["count"], 1)

    def test_non_tetra_excluded(self):
        rows = [
            ("band=TETRA snr=8dB", 0.6, _now_ts()),
            ("band=ISM-433 snr=10dB", 0.7, _now_ts()),
        ]
        con = _make_db(rows)
        result = mr.tetra_daily(con, days=365)
        total = sum(r["count"] for r in result)
        self.assertEqual(total, 1)

    def test_result_keys(self):
        rows = [("band=TETRA snr=8dB", 0.6, _now_ts())]
        con = _make_db(rows)
        result = mr.tetra_daily(con, days=365)
        self.assertIn("day", result[0])
        self.assertIn("count", result[0])


# ---------------------------------------------------------------------------
# Tests: ais_weekly
# ---------------------------------------------------------------------------

class TestAisWeekly(unittest.TestCase):

    def test_empty_db_returns_empty_list(self):
        con = _make_db()
        result = mr.ais_weekly(con)
        self.assertEqual(result, [])

    def test_ais_a_counted(self):
        rows = [("band=AIS-A snr=15dB", 0.85, _now_ts())]
        con = _make_db(rows)
        result = mr.ais_weekly(con)
        self.assertGreater(len(result), 0)
        total = sum(r["count"] for r in result)
        self.assertEqual(total, 1)

    def test_ais_b_counted(self):
        rows = [("band=AIS-B snr=14dB", 0.80, _now_ts())]
        con = _make_db(rows)
        result = mr.ais_weekly(con)
        total = sum(r["count"] for r in result)
        self.assertEqual(total, 1)

    def test_both_ais_channels_counted(self):
        rows = [
            ("band=AIS-A snr=15dB", 0.85, _now_ts()),
            ("band=AIS-B snr=14dB", 0.80, _now_ts()),
        ]
        con = _make_db(rows)
        result = mr.ais_weekly(con)
        total = sum(r["count"] for r in result)
        self.assertEqual(total, 2)

    def test_non_ais_excluded(self):
        rows = [
            ("band=AIS-A snr=15dB", 0.85, _now_ts()),
            ("band=ISM-433 snr=10dB", 0.7, _now_ts()),
        ]
        con = _make_db(rows)
        result = mr.ais_weekly(con)
        total = sum(r["count"] for r in result)
        self.assertEqual(total, 1)

    def test_result_keys(self):
        rows = [("band=AIS-A snr=15dB", 0.85, _now_ts())]
        con = _make_db(rows)
        result = mr.ais_weekly(con)
        self.assertIn("week", result[0])
        self.assertIn("count", result[0])

    def test_limited_to_12_weeks(self):
        # Insert 20 signals spread across 20 distinct weeks (5 months apart each)
        rows = [
            (f"band=AIS-A snr=15dB", 0.85, _days_ago_ts(7 * i))
            for i in range(20)
        ]
        con = _make_db(rows)
        result = mr.ais_weekly(con)
        self.assertLessEqual(len(result), 12)


# ---------------------------------------------------------------------------
# Tests: radiosonde_coverage
# ---------------------------------------------------------------------------

class TestRadiosondeCoverage(unittest.TestCase):

    def test_empty_db_returns_empty_list(self):
        con = _make_db()
        result = mr.radiosonde_coverage(con, days=14)
        self.assertEqual(result, [])

    def test_z00_launch_counted(self):
        # 00Z nominal window: 21:00-23:59 or 00:00
        ts = _days_ago_date(1) + " 22:00:00"
        rows = [("band=RADIOSONDE snr=20dB", 0.9, ts)]
        con = _make_db(rows)
        result = mr.radiosonde_coverage(con, days=365)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["z00"], 1)
        self.assertEqual(result[0]["z12"], 0)

    def test_z12_launch_counted(self):
        # 12Z nominal window: 11:00-13:00
        ts = _days_ago_date(1) + " 12:00:00"
        rows = [("band=RADIOSONDE snr=20dB", 0.9, ts)]
        con = _make_db(rows)
        result = mr.radiosonde_coverage(con, days=365)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["z12"], 1)
        self.assertEqual(result[0]["z00"], 0)

    def test_non_radiosonde_excluded(self):
        ts = _days_ago_date(1) + " 22:00:00"
        rows = [
            ("band=RADIOSONDE snr=20dB", 0.9, ts),
            ("band=ISM-433 snr=10dB", 0.7, ts),
        ]
        con = _make_db(rows)
        result = mr.radiosonde_coverage(con, days=365)
        total_z00 = sum(r["z00"] for r in result)
        total_z12 = sum(r["z12"] for r in result)
        self.assertEqual(total_z00 + total_z12, 1)

    def test_result_keys(self):
        ts = _days_ago_date(1) + " 22:00:00"
        rows = [("band=RADIOSONDE snr=20dB", 0.9, ts)]
        con = _make_db(rows)
        result = mr.radiosonde_coverage(con, days=365)
        self.assertIn("day", result[0])
        self.assertIn("z00", result[0])
        self.assertIn("z12", result[0])


# ---------------------------------------------------------------------------
# Tests: badge_html
# ---------------------------------------------------------------------------

class TestBadgeHtml(unittest.TestCase):

    def test_ism433_produces_iot_badge(self):
        html = mr.badge_html("ISM-433")
        self.assertIn("badge-iot", html)

    def test_adsb_produces_aviation_badge(self):
        html = mr.badge_html("ADS-B")
        self.assertIn("badge-aviation", html)

    def test_ais_a_produces_maritime_badge(self):
        html = mr.badge_html("AIS-A")
        self.assertIn("badge-maritime", html)

    def test_tetra_produces_emergency_badge(self):
        html = mr.badge_html("TETRA")
        self.assertIn("badge-emergency", html)

    def test_unknown_band_produces_empty_badge(self):
        html = mr.badge_html("UNKNOWN-BAND-XYZ")
        self.assertIn("<span", html)
        self.assertIn("badge-", html)

    def test_output_is_html_span(self):
        html = mr.badge_html("ISM-433")
        self.assertTrue(html.startswith("<span"))
        self.assertTrue(html.endswith("</span>"))

    def test_dab_produces_broadcast_badge(self):
        html = mr.badge_html("DAB")
        self.assertIn("badge-broadcast", html)

    def test_radiosonde_produces_weather_badge(self):
        html = mr.badge_html("RADIOSONDE")
        self.assertIn("badge-weather", html)


# ---------------------------------------------------------------------------
# Tests: conf_class
# ---------------------------------------------------------------------------

class TestConfClass(unittest.TestCase):

    def test_none_returns_conf_lo(self):
        self.assertEqual(mr.conf_class(None), "conf-lo")

    def test_high_confidence_returns_conf_hi(self):
        self.assertEqual(mr.conf_class(0.7), "conf-hi")
        self.assertEqual(mr.conf_class(0.9), "conf-hi")
        self.assertEqual(mr.conf_class(1.0), "conf-hi")

    def test_medium_confidence_returns_conf_med(self):
        self.assertEqual(mr.conf_class(0.4), "conf-med")
        self.assertEqual(mr.conf_class(0.5), "conf-med")
        self.assertEqual(mr.conf_class(0.699), "conf-med")

    def test_low_confidence_returns_conf_lo(self):
        self.assertEqual(mr.conf_class(0.0), "conf-lo")
        self.assertEqual(mr.conf_class(0.1), "conf-lo")
        self.assertEqual(mr.conf_class(0.399), "conf-lo")

    def test_boundary_0_7_is_hi(self):
        # >=0.7 -> conf-hi; <0.7 -> conf-med (if >=0.4)
        self.assertEqual(mr.conf_class(0.7), "conf-hi")

    def test_boundary_0_4_is_med(self):
        self.assertEqual(mr.conf_class(0.4), "conf-med")

    def test_just_below_0_4_is_lo(self):
        # Use a value clearly below 0.4 to avoid floating-point edge ambiguity
        self.assertEqual(mr.conf_class(0.35), "conf-lo")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
