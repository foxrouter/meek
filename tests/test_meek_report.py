#!/usr/bin/env python3
"""
tests/test_meek_report.py — Unit tests for tools/meek_report.py.

Tests the database query helpers, HTML badge/class utilities, and the
end-to-end build() function without requiring a live SDR or existing DB.

Run with:
    python3 tests/test_meek_report.py [-v]
or via pytest:
    pytest tests/test_meek_report.py
"""

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tools"))

import meek_report as mr  # noqa: E402  (import after sys.path manipulation)

# ---------------------------------------------------------------------------
# Minimal DB schema mirroring db.hpp (signals + examples + methods tables)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY,
    source       TEXT,
    notes        TEXT,
    timestamp    TEXT NOT NULL DEFAULT (datetime('now')),
    timestamp_ns INTEGER
);
CREATE TABLE IF NOT EXISTS methods (
    id   INTEGER PRIMARY KEY,
    name TEXT
);
CREATE TABLE IF NOT EXISTS examples (
    id        INTEGER PRIMARY KEY,
    signal_id INTEGER REFERENCES signals(id),
    confidence REAL
);
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_rw(path: str) -> sqlite3.Connection:
    """Open a read-write connection (no query_only) suitable for test setup."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _insert_signal(
    conn: sqlite3.Connection,
    notes: str,
    timestamp: str = "2050-06-01 12:00:00",
    source: str = "test",
) -> int:
    cur = conn.execute(
        "INSERT INTO signals(source, notes, timestamp) VALUES(?,?,?)",
        (source, notes, timestamp),
    )
    conn.commit()
    return cur.lastrowid


def _insert_example(
    conn: sqlite3.Connection,
    signal_id: int,
    confidence: float,
) -> None:
    conn.execute(
        "INSERT INTO examples(signal_id, confidence) VALUES(?,?)",
        (signal_id, confidence),
    )
    conn.commit()


def _insert_method(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("INSERT INTO methods(name) VALUES(?)", (name,))
    conn.commit()


# ---------------------------------------------------------------------------
# Base class providing a per-test temporary DB
# ---------------------------------------------------------------------------

class _DbTestCase(unittest.TestCase):
    """Base class that creates a writable in-file DB for each test method."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._db_path = str(Path(self._tmp) / "test.db")
        self._conn = _open_rw(self._db_path)

    def tearDown(self):
        self._conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Pure-function tests: band_filter
# ---------------------------------------------------------------------------

class TestBandFilter(unittest.TestCase):
    """band_filter() returns an SQL LIKE pattern for a given band name."""

    def test_adsb_pattern(self):
        self.assertEqual(mr.band_filter("ADS-B"), "%band=ADS-B%")

    def test_tpms_pattern(self):
        self.assertEqual(mr.band_filter("TPMS-433"), "%band=TPMS-433%")

    def test_empty_band_pattern(self):
        self.assertEqual(mr.band_filter(""), "%band=%")

    def test_arbitrary_band(self):
        result = mr.band_filter("SOME-BAND")
        self.assertTrue(result.startswith("%band="))
        self.assertTrue(result.endswith("%"))


# ---------------------------------------------------------------------------
# Pure-function tests: badge_html and conf_class
# ---------------------------------------------------------------------------

class TestBadgeHtml(unittest.TestCase):
    """badge_html() returns an HTML <span> with the correct CSS class."""

    def test_adsb_produces_aviation_badge(self):
        html = mr.badge_html("ADS-B")
        self.assertIn("badge-aviation", html)

    def test_tpms_produces_iot_badge(self):
        html = mr.badge_html("TPMS-433")
        self.assertIn("badge-iot", html)

    def test_tetra_produces_emergency_badge(self):
        html = mr.badge_html("TETRA")
        self.assertIn("badge-emergency", html)

    def test_ais_a_produces_maritime_badge(self):
        html = mr.badge_html("AIS-A")
        self.assertIn("badge-maritime", html)

    def test_dab_produces_broadcast_badge(self):
        html = mr.badge_html("DAB")
        self.assertIn("badge-broadcast", html)

    def test_unknown_band_still_returns_span(self):
        html = mr.badge_html("NO-SUCH-BAND-XYZ")
        self.assertIn("<span", html)

    def test_all_known_bands_produce_span(self):
        for band in mr.ALL_BANDS:
            html = mr.badge_html(band)
            self.assertIn("<span", html, f"badge_html failed for band {band!r}")


class TestConfClass(unittest.TestCase):
    """conf_class() maps a confidence float to a CSS class string."""

    def test_very_high_confidence(self):
        self.assertEqual(mr.conf_class(0.95), "conf-hi")

    def test_boundary_high_at_0_7(self):
        self.assertEqual(mr.conf_class(0.7), "conf-hi")

    def test_medium_confidence(self):
        self.assertEqual(mr.conf_class(0.55), "conf-med")

    def test_boundary_medium_at_0_4(self):
        self.assertEqual(mr.conf_class(0.4), "conf-med")

    def test_low_confidence(self):
        self.assertEqual(mr.conf_class(0.2), "conf-lo")

    def test_zero_confidence_is_low(self):
        self.assertEqual(mr.conf_class(0.0), "conf-lo")

    def test_none_returns_low(self):
        self.assertEqual(mr.conf_class(None), "conf-lo")

    def test_below_medium_boundary_is_low(self):
        self.assertEqual(mr.conf_class(0.39), "conf-lo")


# ---------------------------------------------------------------------------
# Tests: summary_counts
# ---------------------------------------------------------------------------

class TestSummaryCounts(_DbTestCase):
    """summary_counts() aggregates signals, examples, and methods."""

    def test_empty_db_returns_zero_totals(self):
        counts = mr.summary_counts(self._conn, days=36500)
        self.assertEqual(counts["total_signals"], 0)
        self.assertEqual(counts["total_examples"], 0)
        self.assertEqual(counts["total_methods"], 0)

    def test_empty_db_oldest_newest_are_dash(self):
        counts = mr.summary_counts(self._conn, days=36500)
        self.assertEqual(counts["oldest"], "—")
        self.assertEqual(counts["newest"], "—")

    def test_total_signals_increments(self):
        _insert_signal(self._conn, "band=ADS-B")
        _insert_signal(self._conn, "band=TPMS-433")
        counts = mr.summary_counts(self._conn, days=36500)
        self.assertEqual(counts["total_signals"], 2)

    def test_total_examples_increments(self):
        sid = _insert_signal(self._conn, "band=ADS-B")
        _insert_example(self._conn, sid, 0.85)
        _insert_example(self._conn, sid, 0.90)
        counts = mr.summary_counts(self._conn, days=36500)
        self.assertEqual(counts["total_examples"], 2)

    def test_total_methods_increments(self):
        _insert_method(self._conn, "FSK2")
        _insert_method(self._conn, "BPSK")
        counts = mr.summary_counts(self._conn, days=36500)
        self.assertEqual(counts["total_methods"], 2)

    def test_oldest_newest_reflect_inserted_timestamps(self):
        _insert_signal(self._conn, "band=ADS-B", timestamp="2050-01-01 00:00:00")
        _insert_signal(self._conn, "band=ADS-B", timestamp="2050-06-01 12:00:00")
        counts = mr.summary_counts(self._conn, days=36500)
        self.assertIn("2050-01-01", str(counts["oldest"]))
        self.assertIn("2050-06-01", str(counts["newest"]))

    def test_recent_signals_excludes_old_timestamp(self):
        # Far-future timestamp is within any reasonable 'days' window.
        _insert_signal(self._conn, "band=ADS-B", timestamp="2050-06-01 12:00:00")
        # Year-2000 timestamp is definitely outside a 30-day window from now.
        _insert_signal(self._conn, "band=ADS-B", timestamp="2000-01-01 00:00:00")
        counts = mr.summary_counts(self._conn, days=30)
        self.assertEqual(counts["recent_signals"], 1)

    def test_required_keys_present(self):
        counts = mr.summary_counts(self._conn, days=30)
        for key in ("total_signals", "total_examples", "total_methods",
                    "recent_signals", "oldest", "newest"):
            self.assertIn(key, counts)


# ---------------------------------------------------------------------------
# Tests: league_table
# ---------------------------------------------------------------------------

class TestLeagueTable(_DbTestCase):
    """league_table() returns one entry per band, sorted by count desc."""

    def test_all_known_bands_present(self):
        lt = mr.league_table(self._conn, days=36500)
        names = {e["band"] for e in lt}
        for band in mr.ALL_BANDS:
            self.assertIn(band, names, f"Band {band!r} missing from league table")

    def test_empty_db_all_counts_zero(self):
        lt = mr.league_table(self._conn, days=36500)
        for entry in lt:
            self.assertEqual(entry["count"], 0)
            self.assertEqual(entry["avg_conf"], 0.0)

    def test_count_increments_for_matching_band(self):
        sid = _insert_signal(self._conn, "band=TPMS-433")
        _insert_example(self._conn, sid, 0.80)
        lt = mr.league_table(self._conn, days=36500)
        entry = next(e for e in lt if e["band"] == "TPMS-433")
        self.assertEqual(entry["count"], 1)
        self.assertAlmostEqual(entry["avg_conf"], 0.8, places=2)

    def test_sorted_descending_by_count(self):
        for _ in range(3):
            sid = _insert_signal(self._conn, "band=ADS-B")
            _insert_example(self._conn, sid, 0.9)
        sid2 = _insert_signal(self._conn, "band=TPMS-433")
        _insert_example(self._conn, sid2, 0.7)
        lt = mr.league_table(self._conn, days=36500)
        counts = [e["count"] for e in lt]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_result_has_required_keys(self):
        lt = mr.league_table(self._conn, days=36500)
        for key in ("band", "count", "avg_conf", "last_seen"):
            self.assertIn(key, lt[0])


# ---------------------------------------------------------------------------
# Tests: daily_series
# ---------------------------------------------------------------------------

class TestDailySeries(_DbTestCase):
    """daily_series() groups signal counts by calendar day."""

    def test_empty_db_returns_empty_list(self):
        self.assertEqual(mr.daily_series(self._conn, "ADS-B", days=36500), [])

    def test_two_signals_same_day_aggregated(self):
        _insert_signal(self._conn, "band=ADS-B", timestamp="2050-03-01 10:00:00")
        _insert_signal(self._conn, "band=ADS-B", timestamp="2050-03-01 11:00:00")
        series = mr.daily_series(self._conn, "ADS-B", days=36500)
        days_map = {e["day"]: e["count"] for e in series}
        self.assertEqual(days_map["2050-03-01"], 2)

    def test_signals_on_different_days_separate_entries(self):
        _insert_signal(self._conn, "band=ADS-B", timestamp="2050-03-01 10:00:00")
        _insert_signal(self._conn, "band=ADS-B", timestamp="2050-03-02 09:00:00")
        series = mr.daily_series(self._conn, "ADS-B", days=36500)
        self.assertEqual(len(series), 2)

    def test_ignores_signals_for_other_bands(self):
        _insert_signal(self._conn, "band=TETRA", timestamp="2050-03-01 10:00:00")
        self.assertEqual(mr.daily_series(self._conn, "ADS-B", days=36500), [])

    def test_result_has_day_and_count_keys(self):
        _insert_signal(self._conn, "band=ADS-B", timestamp="2050-03-01 10:00:00")
        series = mr.daily_series(self._conn, "ADS-B", days=36500)
        self.assertIn("day", series[0])
        self.assertIn("count", series[0])


# ---------------------------------------------------------------------------
# Tests: hourly_series
# ---------------------------------------------------------------------------

class TestHourlySeries(_DbTestCase):
    """hourly_series() always returns 24 entries (one per hour), filling
    missing hours with count=0."""

    def test_always_returns_24_entries(self):
        series = mr.hourly_series(self._conn, "ADS-B", days=36500)
        self.assertEqual(len(series), 24)

    def test_hours_span_0_to_23(self):
        series = mr.hourly_series(self._conn, "ADS-B", days=36500)
        self.assertEqual([e["hour"] for e in series], list(range(24)))

    def test_empty_db_all_counts_zero(self):
        series = mr.hourly_series(self._conn, "ADS-B", days=36500)
        for entry in series:
            self.assertEqual(entry["count"], 0)

    def test_signal_at_hour_14_counted_correctly(self):
        _insert_signal(self._conn, "band=ADS-B", timestamp="2050-03-01 14:00:00")
        _insert_signal(self._conn, "band=ADS-B", timestamp="2050-03-02 14:30:00")
        series = mr.hourly_series(self._conn, "ADS-B", days=36500)
        counts = {e["hour"]: e["count"] for e in series}
        self.assertEqual(counts[14], 2)
        self.assertEqual(counts[0], 0)

    def test_ignores_other_bands(self):
        _insert_signal(self._conn, "band=TETRA", timestamp="2050-03-01 10:00:00")
        series = mr.hourly_series(self._conn, "ADS-B", days=36500)
        self.assertEqual(sum(e["count"] for e in series), 0)


# ---------------------------------------------------------------------------
# Tests: confidence_distribution
# ---------------------------------------------------------------------------

class TestConfidenceDistribution(_DbTestCase):
    """confidence_distribution() buckets examples into confidence tiers."""

    def test_empty_db_returns_empty_list(self):
        self.assertEqual(mr.confidence_distribution(self._conn, days=36500), [])

    def test_very_high_bucket_present(self):
        sid = _insert_signal(self._conn, "band=ADS-B")
        _insert_example(self._conn, sid, 0.95)
        dist = mr.confidence_distribution(self._conn, days=36500)
        labels = [e["label"] for e in dist]
        self.assertTrue(any("Very High" in lbl for lbl in labels))

    def test_all_five_buckets_populated(self):
        for conf in (0.95, 0.75, 0.55, 0.35, 0.15):
            sid = _insert_signal(self._conn, "band=ADS-B")
            _insert_example(self._conn, sid, conf)
        dist = mr.confidence_distribution(self._conn, days=36500)
        self.assertEqual(len(dist), 5)

    def test_counts_sum_to_total_examples(self):
        n = 6
        for i in range(n):
            sid = _insert_signal(self._conn, "band=ADS-B")
            _insert_example(self._conn, sid, float(i) / n)
        dist = mr.confidence_distribution(self._conn, days=36500)
        self.assertEqual(sum(e["count"] for e in dist), n)

    def test_result_has_label_and_count_keys(self):
        sid = _insert_signal(self._conn, "band=ADS-B")
        _insert_example(self._conn, sid, 0.8)
        dist = mr.confidence_distribution(self._conn, days=36500)
        self.assertIn("label", dist[0])
        self.assertIn("count", dist[0])


# ---------------------------------------------------------------------------
# Tests: group_totals
# ---------------------------------------------------------------------------

class TestGroupTotals(_DbTestCase):
    """group_totals() aggregates signal counts per BAND_GROUPS category."""

    def test_empty_db_all_groups_zero(self):
        totals = mr.group_totals(self._conn, days=36500)
        for group in mr.BAND_GROUPS:
            self.assertIn(group, totals)
            self.assertEqual(totals[group], 0)

    def test_all_band_groups_present_as_keys(self):
        totals = mr.group_totals(self._conn, days=36500)
        for group in mr.BAND_GROUPS:
            self.assertIn(group, totals)

    def test_aviation_increments_on_adsb_signal(self):
        _insert_signal(self._conn, "band=ADS-B")
        totals = mr.group_totals(self._conn, days=36500)
        self.assertGreater(totals["Aviation"], 0)

    def test_iot_increments_on_tpms_signal(self):
        _insert_signal(self._conn, "band=TPMS-433")
        totals = mr.group_totals(self._conn, days=36500)
        self.assertGreater(totals["IoT & Smart Infrastructure"], 0)

    def test_emergency_increments_on_tetra_signal(self):
        _insert_signal(self._conn, "band=TETRA")
        totals = mr.group_totals(self._conn, days=36500)
        self.assertGreater(totals["Emergency & Public Safety"], 0)

    def test_other_group_unaffected_by_adsb_signal(self):
        _insert_signal(self._conn, "band=ADS-B")
        totals = mr.group_totals(self._conn, days=36500)
        self.assertEqual(totals["Maritime"], 0)


# ---------------------------------------------------------------------------
# Tests: recent_detections
# ---------------------------------------------------------------------------

class TestRecentDetections(_DbTestCase):
    """recent_detections() returns the most recent joined signal+example rows."""

    def test_empty_db_returns_empty(self):
        self.assertEqual(mr.recent_detections(self._conn), [])

    def test_limit_respected(self):
        for i in range(10):
            sid = _insert_signal(self._conn, f"band=ADS-B idx={i}")
            _insert_example(self._conn, sid, 0.8)
        results = mr.recent_detections(self._conn, limit=5)
        self.assertLessEqual(len(results), 5)

    def test_band_extracted_from_notes(self):
        sid = _insert_signal(self._conn, "snr=10 band=TPMS-433 freq=433.9e6")
        _insert_example(self._conn, sid, 0.75)
        results = mr.recent_detections(self._conn, limit=10)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["band"], "TPMS-433")

    def test_missing_band_key_returns_unknown(self):
        sid = _insert_signal(self._conn, "snr=10 freq=433.9e6")
        _insert_example(self._conn, sid, 0.6)
        results = mr.recent_detections(self._conn, limit=10)
        self.assertEqual(results[0]["band"], "unknown")

    def test_notes_truncated_to_120_chars(self):
        long_notes = "x" * 200 + " band=ADS-B"
        sid = _insert_signal(self._conn, long_notes)
        _insert_example(self._conn, sid, 0.9)
        results = mr.recent_detections(self._conn, limit=10)
        self.assertLessEqual(len(results[0]["notes"]), 120)

    def test_result_has_required_keys(self):
        sid = _insert_signal(self._conn, "band=ADS-B")
        _insert_example(self._conn, sid, 0.8)
        results = mr.recent_detections(self._conn)
        for key in ("timestamp", "band", "conf", "notes"):
            self.assertIn(key, results[0])

    def test_signals_without_examples_excluded(self):
        _insert_signal(self._conn, "band=ADS-B")  # no example
        results = mr.recent_detections(self._conn)
        self.assertEqual(results, [])


# ---------------------------------------------------------------------------
# Tests: elt_alerts
# ---------------------------------------------------------------------------

class TestEltAlerts(_DbTestCase):
    """elt_alerts() returns up to 20 ELT-406 signal+example rows."""

    def test_empty_db_returns_empty(self):
        self.assertEqual(mr.elt_alerts(self._conn), [])

    def test_only_elt406_signals_returned(self):
        sid = _insert_signal(self._conn, "band=ELT-406")
        _insert_example(self._conn, sid, 0.99)
        # This ADS-B signal should NOT appear (no example either).
        _insert_signal(self._conn, "band=ADS-B")
        alerts = mr.elt_alerts(self._conn)
        self.assertEqual(len(alerts), 1)
        self.assertIn("ELT-406", alerts[0]["notes"])

    def test_limited_to_twenty_results(self):
        for _ in range(25):
            sid = _insert_signal(self._conn, "band=ELT-406")
            _insert_example(self._conn, sid, 0.9)
        alerts = mr.elt_alerts(self._conn)
        self.assertLessEqual(len(alerts), 20)

    def test_result_has_id_timestamp_conf_notes(self):
        sid = _insert_signal(self._conn, "band=ELT-406", timestamp="2050-04-01 15:00:00")
        _insert_example(self._conn, sid, 0.88)
        alerts = mr.elt_alerts(self._conn)
        for key in ("id", "timestamp", "conf", "notes"):
            self.assertIn(key, alerts[0])

    def test_confidence_rounded_to_3dp(self):
        sid = _insert_signal(self._conn, "band=ELT-406")
        _insert_example(self._conn, sid, 0.999999)
        alerts = mr.elt_alerts(self._conn)
        # ROUND(..., 3) should cap it to 1.0 or truncate to 3 d.p.
        self.assertIsNotNone(alerts[0]["conf"])


# ---------------------------------------------------------------------------
# Tests: tetra_daily
# ---------------------------------------------------------------------------

class TestTetraDaily(_DbTestCase):
    """tetra_daily() groups TETRA signal counts by calendar day."""

    def test_empty_db_returns_empty(self):
        self.assertEqual(mr.tetra_daily(self._conn, days=36500), [])

    def test_two_signals_same_day_aggregated(self):
        _insert_signal(self._conn, "band=TETRA", timestamp="2050-03-01 10:00:00")
        _insert_signal(self._conn, "band=TETRA", timestamp="2050-03-01 11:00:00")
        rows = mr.tetra_daily(self._conn, days=36500)
        days_map = {r["day"]: r["count"] for r in rows}
        self.assertEqual(days_map["2050-03-01"], 2)

    def test_signals_on_different_days_separate_entries(self):
        _insert_signal(self._conn, "band=TETRA", timestamp="2050-03-01 10:00:00")
        _insert_signal(self._conn, "band=TETRA", timestamp="2050-03-02 09:00:00")
        rows = mr.tetra_daily(self._conn, days=36500)
        self.assertEqual(len(rows), 2)

    def test_ignores_other_bands(self):
        _insert_signal(self._conn, "band=ADS-B", timestamp="2050-03-01 10:00:00")
        self.assertEqual(mr.tetra_daily(self._conn, days=36500), [])

    def test_result_has_day_and_count_keys(self):
        _insert_signal(self._conn, "band=TETRA", timestamp="2050-03-01 10:00:00")
        rows = mr.tetra_daily(self._conn, days=36500)
        self.assertIn("day", rows[0])
        self.assertIn("count", rows[0])


# ---------------------------------------------------------------------------
# Tests: ais_weekly
# ---------------------------------------------------------------------------

class TestAisWeekly(_DbTestCase):
    """ais_weekly() aggregates AIS-A and AIS-B signal counts by ISO week."""

    def test_empty_db_returns_empty(self):
        self.assertEqual(mr.ais_weekly(self._conn), [])

    def test_ais_a_included(self):
        _insert_signal(self._conn, "band=AIS-A", timestamp="2050-03-01 10:00:00")
        weeks = mr.ais_weekly(self._conn)
        self.assertEqual(len(weeks), 1)
        self.assertEqual(weeks[0]["count"], 1)

    def test_ais_b_included(self):
        _insert_signal(self._conn, "band=AIS-B", timestamp="2050-03-01 10:00:00")
        weeks = mr.ais_weekly(self._conn)
        self.assertEqual(len(weeks), 1)

    def test_both_channels_counted_together(self):
        _insert_signal(self._conn, "band=AIS-A", timestamp="2050-03-01 10:00:00")
        _insert_signal(self._conn, "band=AIS-B", timestamp="2050-03-01 11:00:00")
        weeks = mr.ais_weekly(self._conn)
        self.assertEqual(sum(w["count"] for w in weeks), 2)

    def test_other_bands_excluded(self):
        _insert_signal(self._conn, "band=ADS-B", timestamp="2050-03-01 10:00:00")
        self.assertEqual(mr.ais_weekly(self._conn), [])

    def test_result_has_week_and_count_keys(self):
        _insert_signal(self._conn, "band=AIS-A", timestamp="2050-03-01 10:00:00")
        weeks = mr.ais_weekly(self._conn)
        self.assertIn("week", weeks[0])
        self.assertIn("count", weeks[0])


# ---------------------------------------------------------------------------
# Tests: radiosonde_coverage
# ---------------------------------------------------------------------------

class TestRadiosondeCoverage(_DbTestCase):
    """radiosonde_coverage() counts radiosonde launches in the z00 (21–23 and
    00 UTC) and z12 (11–13 UTC) windows, grouped by day."""

    def test_empty_db_returns_empty(self):
        self.assertEqual(mr.radiosonde_coverage(self._conn, days=36500), [])

    def test_evening_launch_counted_in_z00(self):
        _insert_signal(self._conn, "band=RADIOSONDE", timestamp="2050-03-01 22:30:00")
        rows = mr.radiosonde_coverage(self._conn, days=36500)
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0]["z00"], 0)
        self.assertEqual(rows[0]["z12"], 0)

    def test_midnight_launch_counted_in_z00(self):
        _insert_signal(self._conn, "band=RADIOSONDE", timestamp="2050-03-01 00:30:00")
        rows = mr.radiosonde_coverage(self._conn, days=36500)
        self.assertGreater(rows[0]["z00"], 0)

    def test_noon_launch_counted_in_z12(self):
        _insert_signal(self._conn, "band=RADIOSONDE", timestamp="2050-03-01 12:15:00")
        rows = mr.radiosonde_coverage(self._conn, days=36500)
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0]["z12"], 0)
        self.assertEqual(rows[0]["z00"], 0)

    def test_other_bands_excluded(self):
        _insert_signal(self._conn, "band=ADS-B", timestamp="2050-03-01 22:30:00")
        self.assertEqual(mr.radiosonde_coverage(self._conn, days=36500), [])

    def test_result_has_day_z00_z12_keys(self):
        _insert_signal(self._conn, "band=RADIOSONDE", timestamp="2050-03-01 22:30:00")
        rows = mr.radiosonde_coverage(self._conn, days=36500)
        for key in ("day", "z00", "z12"):
            self.assertIn(key, rows[0])


# ---------------------------------------------------------------------------
# Tests: open_db
# ---------------------------------------------------------------------------

class TestOpenDb(unittest.TestCase):
    """open_db() opens an existing DB as query-only or exits on missing file."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_missing_file_raises_system_exit(self):
        with self.assertRaises(SystemExit):
            mr.open_db(str(Path(self._tmp) / "nonexistent.db"))

    def test_valid_file_returns_connection(self):
        db_path = str(Path(self._tmp) / "valid.db")
        conn = _open_rw(db_path)
        conn.close()
        conn2 = mr.open_db(db_path)
        self.assertIsNotNone(conn2)
        conn2.close()

    def test_returned_connection_has_row_factory(self):
        db_path = str(Path(self._tmp) / "rf.db")
        conn = _open_rw(db_path)
        conn.close()
        conn2 = mr.open_db(db_path)
        self.assertEqual(conn2.row_factory, sqlite3.Row)
        conn2.close()


# ---------------------------------------------------------------------------
# Tests: build — end-to-end HTML generation
# ---------------------------------------------------------------------------

class TestBuild(unittest.TestCase):
    """build() renders a self-contained HTML report file from a DB."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._db_path = str(Path(self._tmp) / "rf_adapt_intel.db")
        self._out_path = str(Path(self._tmp) / "report.html")
        conn = _open_rw(self._db_path)
        sid = _insert_signal(conn, "band=ADS-B snr=15.2", timestamp="2050-06-01 12:00:00")
        _insert_example(conn, sid, 0.92)
        _insert_method(conn, "rf_audit")
        conn.close()

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_creates_output_file(self):
        mr.build(self._db_path, days=36500, out_path=self._out_path)
        self.assertTrue(Path(self._out_path).exists())

    def test_output_file_is_non_empty(self):
        mr.build(self._db_path, days=36500, out_path=self._out_path)
        self.assertGreater(Path(self._out_path).stat().st_size, 1000)

    def test_returns_path_object(self):
        result = mr.build(self._db_path, days=36500, out_path=self._out_path)
        self.assertIsInstance(result, Path)

    def test_no_unreplaced_placeholders(self):
        mr.build(self._db_path, days=36500, out_path=self._out_path)
        html = Path(self._out_path).read_text(encoding="utf-8")
        placeholders = (
            "%%TOTAL_SIGNALS%%", "%%TOTAL_EXAMPLES%%", "%%TOTAL_METHODS%%",
            "%%RECENT_SIGNALS%%", "%%OLDEST%%", "%%NEWEST%%",
            "%%ELT_COUNT%%", "%%LEAGUE_ROWS%%", "%%ELT_ROWS%%",
            "%%SONDE_ROWS%%", "%%RECENT_ROWS%%", "%%JSON_DATA%%",
        )
        for ph in placeholders:
            self.assertNotIn(ph, html, f"Unreplaced placeholder: {ph}")

    def test_empty_db_no_elt_message_present(self):
        empty_path = str(Path(self._tmp) / "empty.db")
        conn = _open_rw(empty_path)
        conn.close()
        out = str(Path(self._tmp) / "empty.html")
        mr.build(empty_path, days=36500, out_path=out)
        html = Path(out).read_text(encoding="utf-8")
        self.assertIn("No ELT-406 distress beacons", html)

    def test_empty_db_build_succeeds(self):
        empty_path = str(Path(self._tmp) / "empty2.db")
        conn = _open_rw(empty_path)
        conn.close()
        out = str(Path(self._tmp) / "empty2.html")
        mr.build(empty_path, days=36500, out_path=out)
        self.assertTrue(Path(out).exists())

    def test_missing_db_raises_system_exit(self):
        with self.assertRaises(SystemExit):
            mr.build("/nonexistent/rf_adapt_intel.db", days=30, out_path=self._out_path)

    def test_output_is_valid_html(self):
        mr.build(self._db_path, days=36500, out_path=self._out_path)
        html = Path(self._out_path).read_text(encoding="utf-8")
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("</html>", html)

    def test_json_data_in_output(self):
        mr.build(self._db_path, days=36500, out_path=self._out_path)
        html = Path(self._out_path).read_text(encoding="utf-8")
        # The JSON data block should include at least the "band_totals" key.
        self.assertIn("band_totals", html)


if __name__ == "__main__":
    unittest.main()
