#!/usr/bin/env python3
"""
tests/test_decode_candidates.py — Unit/integration tests for
tools/decode_candidates.py.

Run with:
    python3 tests/test_decode_candidates.py [-v]

Requires: numpy (already required by gen_test_signals.py)
"""

import hashlib
import json
import math
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

# Make tools/ importable regardless of working directory
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tools"))
import decode_candidates as dc  # noqa: E402 (after sys.path manipulation)


# ---------------------------------------------------------------------------
# Synthetic IQ helpers (duplicates gen_test_signals.py logic to keep tests
# self-contained without depending on the generator script)
# ---------------------------------------------------------------------------

FS = 2_048_000  # default sample rate
SPS = 8         # samples per symbol


def _awgn(signal: np.ndarray, snr_db: float) -> np.ndarray:
    sig_pow   = float(np.mean(np.abs(signal) ** 2)) or 1.0
    noise_pow = sig_pow / 10 ** (snr_db / 10.0)
    noise     = np.sqrt(noise_pow / 2.0) * (
        np.random.randn(len(signal)) + 1j * np.random.randn(len(signal))
    )
    return (signal + noise).astype(np.complex64)


def make_fsk2(n_syms: int = 400, sps: int = SPS,
              fdev: float = 50_000, fs: float = FS,
              snr_db: float = 15.0) -> np.ndarray:
    bits      = np.random.randint(0, 2, n_syms)
    freqs     = (2 * bits - 1) * fdev
    phase_inc = 2.0 * math.pi * freqs / fs
    phase     = np.repeat(phase_inc, sps)
    s         = np.exp(1j * np.cumsum(phase)).astype(np.complex64)
    return _awgn(s, snr_db)


def make_qpsk(n_syms: int = 400, sps: int = SPS,
              snr_db: float = 15.0) -> np.ndarray:
    bits    = np.random.randint(0, 2, (n_syms, 2))
    symbols = ((2 * bits[:, 0] - 1) + 1j * (2 * bits[:, 1] - 1)) / math.sqrt(2)
    up      = np.zeros(n_syms * sps, dtype=complex)
    up[::sps] = symbols
    return _awgn(up.astype(np.complex64), snr_db)


def make_ook(n_syms: int = 400, sps: int = SPS,
             snr_db: float = 15.0) -> np.ndarray:
    bits = (np.random.random(n_syms) < 0.5).astype(float)
    s    = np.repeat(bits, sps).astype(np.complex64)
    return _awgn(s, snr_db)


def make_cw(n_samples: int = 3200, fs: float = FS,
            freq_offset: float = 1_000.0, snr_db: float = 20.0) -> np.ndarray:
    t = np.arange(n_samples) / fs
    s = np.exp(1j * 2.0 * math.pi * freq_offset * t).astype(np.complex64)
    return _awgn(s, snr_db)


def cf32_bytes(samples: np.ndarray) -> bytes:
    """Interleaved CF32 bytes from a complex64 numpy array."""
    interleaved = np.empty(len(samples) * 2, dtype=np.float32)
    interleaved[0::2] = samples.real
    interleaved[1::2] = samples.imag
    return interleaved.tobytes()


# ---------------------------------------------------------------------------
# SQLite DB fixture helpers
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    source TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS methods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    params TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    method_id INTEGER,
    result TEXT,
    confidence REAL,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

_TRACE_TMPL = (
    "snr=15.000dB avg_pow=1.000e-02 papr=3.000dB flat=0.500 occ=0.750 "
    "phase=0.400 trans=0.300 p50=1.000e-02 p90=2.000e-02 "
    "scores(cw=0.100,fsk=0.750,psk=0.200,ook=0.100) -> {mod}@{conf:.3f} "
    "band={band}(boost+0.10)"
)


def _populate_db(conn: sqlite3.Connection,
                 records: List[Tuple[str, float, str]]) -> List[int]:
    """Insert (mod_class, confidence, band) tuples; return list of signal IDs."""
    conn.executescript(_SCHEMA)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO methods (name, params) VALUES (?, ?)",
        ("modulation_classifier",
         '{"type":"heuristic","version":2,"classes":["cw_like","fsk_like","psk_qam_like","ook_am_like"]}'),
    )
    method_id = cur.lastrowid
    sig_ids: List[int] = []
    for mod, conf, band in records:
        trace = _TRACE_TMPL.format(mod=mod, conf=conf, band=band)
        cur.execute(
            "INSERT INTO signals (source, notes) VALUES (?, ?)",
            ("test_fixture", trace),
        )
        sig_id = cur.lastrowid
        cur.execute(
            "INSERT INTO examples (signal_id, method_id, result, confidence, notes) "
            "VALUES (?, ?, 'candidate', ?, ?)",
            (sig_id, method_id, conf, trace),
        )
        sig_ids.append(sig_id)
    conn.commit()
    return sig_ids


# ---------------------------------------------------------------------------
# Tests: parse_decision_trace
# ---------------------------------------------------------------------------

class TestParseDecisionTrace(unittest.TestCase):
    def test_fsk_with_band(self):
        trace = (
            "snr=12.500dB avg_pow=5.000e-03 papr=2.000dB flat=0.600 "
            "occ=0.800 phase=0.500 trans=0.350 p50=5.000e-03 p90=1.000e-02 "
            "scores(cw=0.050,fsk=0.820,psk=0.100,ook=0.030) -> fsk_like@0.820 "
            "band=ISM-433(boost+0.10)"
        )
        mod, conf, band, snr = dc.parse_decision_trace(trace)
        self.assertEqual(mod, "fsk_like")
        self.assertAlmostEqual(conf, 0.820, places=3)
        self.assertEqual(band, "ISM-433")
        self.assertAlmostEqual(snr, 12.5, places=1)

    def test_ook_no_band(self):
        trace = "snr=5.000dB occ=0.400 scores(cw=0.0,fsk=0.1,psk=0.0,ook=0.70) -> ook_am_like@0.700"
        mod, conf, band, snr = dc.parse_decision_trace(trace)
        self.assertEqual(mod, "ook_am_like")
        self.assertAlmostEqual(conf, 0.700, places=3)
        self.assertEqual(band, "")
        self.assertAlmostEqual(snr, 5.0, places=1)

    def test_empty_trace(self):
        mod, conf, band, snr = dc.parse_decision_trace("")
        self.assertEqual(mod, "unknown")
        self.assertEqual(conf, 0.0)

    def test_none_trace(self):
        mod, conf, band, snr = dc.parse_decision_trace(None)
        self.assertEqual(mod, "unknown")


# ---------------------------------------------------------------------------
# Tests: load_cf32 / sha256_file
# ---------------------------------------------------------------------------

class TestCf32Helpers(unittest.TestCase):
    def setUp(self):
        np.random.seed(0)
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmp)

    def _write_cf32(self, fname: str, samples: np.ndarray) -> str:
        path = os.path.join(self._tmp, fname)
        with open(path, "wb") as fh:
            fh.write(cf32_bytes(samples))
        return path

    def test_roundtrip(self):
        orig = (np.random.randn(100) + 1j * np.random.randn(100)).astype(np.complex64)
        path = self._write_cf32("test.cf32", orig)
        loaded = dc.load_cf32(path)
        np.testing.assert_allclose(loaded.real, orig.real, rtol=1e-5)
        np.testing.assert_allclose(loaded.imag, orig.imag, rtol=1e-5)

    def test_max_samples(self):
        orig = (np.random.randn(200) + 1j * np.random.randn(200)).astype(np.complex64)
        path = self._write_cf32("test_trunc.cf32", orig)
        loaded = dc.load_cf32(path, max_samples=50)
        self.assertEqual(len(loaded), 50)

    def test_sha256(self):
        orig = np.ones(16, dtype=np.complex64)
        path = self._write_cf32("sha.cf32", orig)
        digest = dc.sha256_file(path)
        self.assertEqual(len(digest), 64)
        # Deterministic: same file → same hash
        self.assertEqual(digest, dc.sha256_file(path))


# ---------------------------------------------------------------------------
# Tests: built-in decoders
# ---------------------------------------------------------------------------

class TestDecodeFsk(unittest.TestCase):
    def setUp(self):
        np.random.seed(42)

    def test_decoded_flag(self):
        samples = make_fsk2()
        result  = dc.decode_fsk(samples, fs=FS)
        self.assertTrue(result["decoded"])
        self.assertEqual(result["method"], "built_in_fm_demod")

    def test_returns_bit_string(self):
        samples = make_fsk2()
        result  = dc.decode_fsk(samples, fs=FS)
        bits    = result["first_64_bits"]
        self.assertIsInstance(bits, str)
        self.assertTrue(all(c in "01" for c in bits))
        self.assertGreater(len(bits), 0)

    def test_too_few_samples(self):
        result = dc.decode_fsk(np.zeros(8, dtype=np.complex64))
        self.assertFalse(result["decoded"])


class TestDecodePskQam(unittest.TestCase):
    def setUp(self):
        np.random.seed(42)

    def test_decoded_flag_qpsk(self):
        samples = make_qpsk()
        result  = dc.decode_psk_qam(samples, fs=FS)
        self.assertTrue(result["decoded"])
        self.assertEqual(result["method"], "built_in_phase_histogram")

    def test_constellation_range(self):
        samples = make_qpsk()
        result  = dc.decode_psk_qam(samples, fs=FS)
        self.assertIn(result["est_constellation"], ("2PSK", "4PSK", "8PSK"))

    def test_too_few_samples(self):
        result = dc.decode_psk_qam(np.zeros(10, dtype=np.complex64))
        self.assertFalse(result["decoded"])


class TestDecodeOokAm(unittest.TestCase):
    def setUp(self):
        np.random.seed(42)

    def test_decoded_flag(self):
        samples = make_ook()
        result  = dc.decode_ook_am(samples, fs=FS)
        self.assertTrue(result["decoded"])
        self.assertEqual(result["method"], "built_in_envelope_detect")

    def test_duty_cycle_in_range(self):
        samples = make_ook()
        result  = dc.decode_ook_am(samples, fs=FS)
        self.assertGreaterEqual(result["duty_cycle"], 0.0)
        self.assertLessEqual(result["duty_cycle"], 1.0)

    def test_too_few_samples(self):
        result = dc.decode_ook_am(np.zeros(4, dtype=np.complex64))
        self.assertFalse(result["decoded"])


class TestDecodeCw(unittest.TestCase):
    def setUp(self):
        np.random.seed(42)

    def test_decoded_flag(self):
        samples = make_cw(n_samples=4096)
        result  = dc.decode_cw(samples, fs=FS)
        self.assertTrue(result["decoded"])
        self.assertEqual(result["method"], "built_in_fft_peak")

    def test_carrier_frequency_ballpark(self):
        """Detected carrier should be within ±5 kHz of the injected 1 kHz tone."""
        samples = make_cw(n_samples=8192, freq_offset=1_000.0, snr_db=30.0)
        result  = dc.decode_cw(samples, fs=FS)
        self.assertAlmostEqual(result["carrier_hz"], 1_000.0, delta=5_000.0)

    def test_snr_positive_for_clean_cw(self):
        samples = make_cw(n_samples=4096, snr_db=30.0)
        result  = dc.decode_cw(samples, fs=FS)
        self.assertGreater(result["snr_db"], 0.0)

    def test_too_few_samples(self):
        result = dc.decode_cw(np.zeros(10, dtype=np.complex64))
        self.assertFalse(result["decoded"])


# ---------------------------------------------------------------------------
# Tests: snapshot indexing and matching
# ---------------------------------------------------------------------------

class TestSnapshotMatching(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmp)

    def _make_snap(self, ts_ns: int, conf_pct: int,
                   samples: np.ndarray, band: str = "") -> str:
        band_suffix = f"_b{band}" if band else ""
        fname = f"snap_{ts_ns}_c{conf_pct}{band_suffix}.cf32"
        path  = os.path.join(self._tmp, fname)
        with open(path, "wb") as fh:
            fh.write(cf32_bytes(samples))
        return path

    def test_index_finds_files(self):
        np.random.seed(0)
        for i in range(3):
            self._make_snap(1_000_000 + i, 750, np.ones(32, dtype=np.complex64))
        snaps = dc.index_snapshots(self._tmp)
        self.assertEqual(len(snaps), 3)

    def test_index_skips_non_matching_files(self):
        # Create a non-matching file
        Path(os.path.join(self._tmp, "other_file.txt")).write_text("hello")
        snaps = dc.index_snapshots(self._tmp)
        self.assertEqual(len(snaps), 0)

    def test_match_by_conf_pct(self):
        np.random.seed(0)
        self._make_snap(999_000_000, 750, np.ones(32, dtype=np.complex64))
        snaps = dc.index_snapshots(self._tmp)
        cand  = {
            "confidence":   0.750,
            "db_timestamp": "2025-01-01 12:00:00",
        }
        snap  = dc.match_snapshot(cand, snaps)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["conf_pct"], 750)

    def test_no_match_returns_none(self):
        snaps = dc.index_snapshots(self._tmp)  # empty dir
        snap  = dc.match_snapshot({"confidence": 0.750, "db_timestamp": ""}, snaps)
        self.assertIsNone(snap)

    def test_empty_snapshot_dir(self):
        snaps = dc.index_snapshots(self._tmp)
        self.assertEqual(snaps, [])

    def test_missing_snapshot_dir(self):
        snaps = dc.index_snapshots("/nonexistent/path")
        self.assertEqual(snaps, [])

    def test_index_with_band_tag(self):
        """Files with _b<band> tag are indexed and band_name is extracted."""
        self._make_snap(2_000_000, 800, np.ones(32, dtype=np.complex64), band="ISM-433")
        snaps = dc.index_snapshots(self._tmp)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["band_name"], "ISM-433")
        self.assertEqual(snaps[0]["conf_pct"], 800)

    def test_index_without_band_tag_has_empty_band_name(self):
        """Legacy files without _b<band> tag still index with empty band_name."""
        self._make_snap(3_000_000, 700, np.ones(32, dtype=np.complex64))
        snaps = dc.index_snapshots(self._tmp)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["band_name"], "")

    def test_index_mixed_legacy_and_banded(self):
        """Legacy and banded snapshots can coexist in the same directory."""
        self._make_snap(4_000_000, 650, np.ones(32, dtype=np.complex64))
        self._make_snap(5_000_000, 900, np.ones(32, dtype=np.complex64), band="LORA-868")
        snaps = dc.index_snapshots(self._tmp)
        self.assertEqual(len(snaps), 2)
        by_ts = {s["ts_ns"]: s for s in snaps}
        self.assertEqual(by_ts[4_000_000]["band_name"], "")
        self.assertEqual(by_ts[5_000_000]["band_name"], "LORA-868")


# ---------------------------------------------------------------------------
# Tests: query_candidates
# ---------------------------------------------------------------------------

class TestQueryCandidates(unittest.TestCase):
    def setUp(self):
        self._tmp  = tempfile.mkdtemp()
        self._dbp  = os.path.join(self._tmp, "test.db")
        self._conn = sqlite3.connect(self._dbp)

    def tearDown(self):
        self._conn.close()
        shutil.rmtree(self._tmp)

    def _make_db(self, records):
        return _populate_db(self._conn, records)

    def test_returns_correct_count(self):
        self._make_db([
            ("fsk_like", 0.80, "ISM-433"),
            ("ook_am_like", 0.65, "TPMS-433"),
            ("cw_like", 0.50, ""),  # below default 0.6 threshold
        ])
        rows = dc.query_candidates(self._dbp, min_confidence=0.6, limit=100)
        self.assertEqual(len(rows), 2)

    def test_respects_limit(self):
        self._make_db([
            ("fsk_like", 0.90, "ISM-433"),
            ("fsk_like", 0.85, "ISM-433"),
            ("fsk_like", 0.80, "ISM-433"),
        ])
        rows = dc.query_candidates(self._dbp, min_confidence=0.6, limit=2)
        self.assertEqual(len(rows), 2)

    def test_ordered_by_confidence_desc(self):
        self._make_db([
            ("fsk_like", 0.70, ""),
            ("fsk_like", 0.90, ""),
            ("fsk_like", 0.80, ""),
        ])
        rows = dc.query_candidates(self._dbp, min_confidence=0.6, limit=10)
        confs = [r["confidence"] for r in rows]
        self.assertEqual(confs, sorted(confs, reverse=True))

    def test_empty_db_returns_empty_list(self):
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        rows = dc.query_candidates(self._dbp, min_confidence=0.6, limit=10)
        self.assertEqual(rows, [])


# ---------------------------------------------------------------------------
# Integration test: end-to-end with synthetic DB + snapshot files
# ---------------------------------------------------------------------------

class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        np.random.seed(7)
        self._tmp    = tempfile.mkdtemp()
        self._dbp    = os.path.join(self._tmp, "test.db")
        self._snapd  = os.path.join(self._tmp, "snapshots")
        self._report = os.path.join(self._tmp, "report.json")
        os.makedirs(self._snapd, exist_ok=True)

        # Build DB with 3 candidate records
        conn   = sqlite3.connect(self._dbp)
        sig_ids = _populate_db(
            conn,
            [
                ("fsk_like",     0.810, "ISM-433"),
                ("ook_am_like",  0.720, "TPMS-433"),
                ("psk_qam_like", 0.650, "VDL2"),
            ],
        )
        conn.close()

        # Create matching snapshot files (conf_pct = int(conf * 1000))
        self._snap_paths: Dict[int, str] = {}
        pairs = [
            (sig_ids[0], 810, make_fsk2()),
            (sig_ids[1], 720, make_ook()),
            (sig_ids[2], 650, make_qpsk()),
        ]
        for sig_id, conf_pct, samples in pairs:
            ts_ns = 1_700_000_000_000_000_000 + sig_id * 1_000_000
            fname = f"snap_{ts_ns}_c{conf_pct}.cf32"
            path  = os.path.join(self._snapd, fname)
            with open(path, "wb") as fh:
                fh.write(cf32_bytes(samples))
            self._snap_paths[sig_id] = path

    def tearDown(self):
        shutil.rmtree(self._tmp)

    def test_main_exits_zero(self):
        rc = dc.main([
            "--db",           self._dbp,
            "--snapshot-dir", self._snapd,
            "--out",          self._report,
            "--min-confidence", "0.6",
            "--limit",        "50",
        ])
        self.assertEqual(rc, 0)

    def test_report_written(self):
        dc.main([
            "--db",           self._dbp,
            "--snapshot-dir", self._snapd,
            "--out",          self._report,
        ])
        self.assertTrue(os.path.isfile(self._report))

    def test_report_structure(self):
        dc.main([
            "--db",           self._dbp,
            "--snapshot-dir", self._snapd,
            "--out",          self._report,
        ])
        with open(self._report, encoding="utf-8") as fh:
            report = json.load(fh)

        self.assertIn("report_version", report)
        self.assertIn("generated_at",   report)
        self.assertIn("parameters",     report)
        self.assertIn("summary",        report)
        self.assertIn("results",        report)

        summ = report["summary"]
        self.assertEqual(summ["candidates_queried"], 3)
        self.assertEqual(summ["snapshots_matched"],  3)
        self.assertGreaterEqual(summ["decoded"], 1)

    def test_each_result_has_required_keys(self):
        dc.main([
            "--db",           self._dbp,
            "--snapshot-dir", self._snapd,
            "--out",          self._report,
        ])
        with open(self._report, encoding="utf-8") as fh:
            report = json.load(fh)

        required = {
            "signal_id", "db_timestamp", "mod_class", "confidence",
            "snapshot_found", "snapshot_file", "snapshot_sha256",
            "snapshot_samples", "decode_result", "decoded", "decoder_used",
        }
        for r in report["results"]:
            self.assertTrue(required.issubset(set(r.keys())),
                            msg=f"Missing keys in: {r}")

    def test_snapshot_sha256_is_hex64(self):
        dc.main([
            "--db",           self._dbp,
            "--snapshot-dir", self._snapd,
            "--out",          self._report,
        ])
        with open(self._report, encoding="utf-8") as fh:
            results = json.load(fh)["results"]
        for r in results:
            if r["snapshot_found"]:
                self.assertIsNotNone(r["snapshot_sha256"])
                self.assertRegex(r["snapshot_sha256"], r"^[0-9a-f]{64}$")

    def test_missing_db_returns_nonzero(self):
        rc = dc.main([
            "--db",           "/nonexistent/db.sqlite",
            "--snapshot-dir", self._snapd,
            "--out",          self._report,
        ])
        self.assertNotEqual(rc, 0)

    def test_invalid_sample_rate_nan_returns_nonzero(self):
        rc = dc.main([
            "--db",           self._dbp,
            "--snapshot-dir", self._snapd,
            "--out",          self._report,
            "--sample-rate",  "nan",
        ])
        self.assertNotEqual(rc, 0)

    def test_invalid_sample_rate_zero_returns_nonzero(self):
        rc = dc.main([
            "--db",           self._dbp,
            "--snapshot-dir", self._snapd,
            "--out",          self._report,
            "--sample-rate",  "0",
        ])
        self.assertNotEqual(rc, 0)

    def test_invalid_sample_rate_sub_hz_returns_nonzero(self):
        """sub-Hz rate that rounds to 0 integer Hz must also fail upfront."""
        rc = dc.main([
            "--db",           self._dbp,
            "--snapshot-dir", self._snapd,
            "--out",          self._report,
            "--sample-rate",  "0.4",
        ])
        self.assertNotEqual(rc, 0)

    def test_no_snapshot_dir_graceful(self):
        """Should still produce a report when snapshot dir does not exist."""
        rc = dc.main([
            "--db",           self._dbp,
            "--snapshot-dir", "/nonexistent/snaps",
            "--out",          self._report,
        ])
        self.assertEqual(rc, 0)
        with open(self._report, encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertEqual(report["summary"]["snapshots_matched"], 0)


# ---------------------------------------------------------------------------
# Tests: build_report structure
# ---------------------------------------------------------------------------

class TestBuildReport(unittest.TestCase):
    def _dummy_result(self, decoded: bool, decoder: str = "built_in_fm_demod",
                      snap_found: bool = True) -> Dict[str, Any]:
        return {
            "signal_id": 1, "example_id": 1, "db_timestamp": "2025-01-01 00:00:00",
            "source": "test", "mod_class": "fsk_like", "confidence": 0.8,
            "snr_db": 12.0, "band": "ISM-433", "snapshot_found": snap_found,
            "snapshot_file": "snap_123_c800.cf32" if snap_found else None,
            "snapshot_sha256": "a" * 64 if snap_found else None,
            "snapshot_samples": 1000 if snap_found else None,
            "decode_result": {"decoded": decoded, "method": decoder},
            "decoded": decoded,
            "decoder_used": decoder if decoded else None,
        }

    def test_summary_counts(self):
        results = [
            self._dummy_result(True),
            self._dummy_result(True),
            self._dummy_result(False),
            self._dummy_result(False, snap_found=False),
        ]
        report = dc.build_report("/db.sqlite", "/snaps", results, results,
                                 {"min_confidence": 0.6, "limit": 100,
                                  "sample_rate": 2_048_000, "external": False})
        s = report["summary"]
        self.assertEqual(s["candidates_queried"], 4)
        self.assertEqual(s["snapshots_matched"],  3)
        self.assertEqual(s["decoded"],            2)
        self.assertEqual(s["not_decoded"],        2)
        self.assertEqual(s["decoded_by_decoder"]["built_in_fm_demod"], 2)


# ---------------------------------------------------------------------------
# Tests: ACARS-129 / ACARS-130 external decoder dispatch
# ---------------------------------------------------------------------------

class TestAcarsDispatchTable(unittest.TestCase):
    """Ensure new ACARS secondary-channel bands are correctly wired up."""

    _ACARSDEC_BANDS = {"ACARS", "ACARS-VHF", "ACARS-129", "ACARS-130", "VDL2"}

    def test_acars_129_in_band_freq_hz(self):
        self.assertIn("ACARS-129", dc._BAND_FREQ_HZ)
        self.assertEqual(dc._BAND_FREQ_HZ["ACARS-129"], 129_125_000)

    def test_acars_130_in_band_freq_hz(self):
        self.assertIn("ACARS-130", dc._BAND_FREQ_HZ)
        self.assertEqual(dc._BAND_FREQ_HZ["ACARS-130"], 130_025_000)

    def test_acars_bands_route_to_acarsdec(self):
        """All ACARS/VDL2 band names must be present in the acarsdec dispatch set."""
        import ast
        import inspect
        source = inspect.getsource(dc.decode_candidate)
        # Find the string literal set used in the acarsdec dispatch condition.
        # It appears as: band_name in ("ACARS", "ACARS-VHF", "ACARS-129", ...)
        for band in self._ACARSDEC_BANDS:
            self.assertIn(f'"{band}"', source,
                          msg=f'Band "{band}" missing from acarsdec dispatch in decode_candidate()')


# ---------------------------------------------------------------------------
# Tests: resample_iq (scipy / numpy backends)
# ---------------------------------------------------------------------------

class TestResampleIq(unittest.TestCase):
    """Tests for dc.resample_iq() covering both scipy and numpy code paths."""

    def setUp(self):
        np.random.seed(0)
        self._samples = make_fsk2(n_syms=200, sps=8, fs=FS)  # 1600 samples

    def test_passthrough_same_rate(self):
        out = dc.resample_iq(self._samples, FS, FS)
        np.testing.assert_array_equal(out, self._samples)
        self.assertEqual(out.dtype, np.complex64)

    def test_passthrough_normalises_dtype(self):
        """Passthrough must return complex64 even for non-complex64 input."""
        c128 = self._samples.astype(np.complex128)
        out  = dc.resample_iq(c128, FS, FS)
        self.assertEqual(out.dtype, np.complex64)

    def test_decimation_length(self):
        fs_out = FS // 4  # 512 000 Hz
        out = dc.resample_iq(self._samples, FS, fs_out)
        expected = int(round(len(self._samples) * fs_out / FS))
        self.assertEqual(len(out), expected)

    def test_upsampling_length(self):
        fs_out = FS * 2  # 4 096 000 Hz
        out = dc.resample_iq(self._samples, FS, fs_out)
        expected = int(round(len(self._samples) * fs_out / FS))
        self.assertEqual(len(out), expected)

    def test_output_dtype(self):
        out = dc.resample_iq(self._samples, FS, FS // 2)
        self.assertEqual(out.dtype, np.complex64)

    def test_invalid_rate_zero_raises(self):
        with self.assertRaises(ValueError):
            dc.resample_iq(self._samples, 0, FS)
        with self.assertRaises(ValueError):
            dc.resample_iq(self._samples, FS, 0)

    def test_invalid_rate_negative_raises(self):
        with self.assertRaises(ValueError):
            dc.resample_iq(self._samples, -FS, FS)

    def test_invalid_rate_nan_raises(self):
        with self.assertRaises(ValueError):
            dc.resample_iq(self._samples, float("nan"), FS)
        with self.assertRaises(ValueError):
            dc.resample_iq(self._samples, FS, float("nan"))

    def test_invalid_rate_inf_raises(self):
        with self.assertRaises(ValueError):
            dc.resample_iq(self._samples, float("inf"), FS)
        with self.assertRaises(ValueError):
            dc.resample_iq(self._samples, FS, float("-inf"))

    def test_invalid_rate_sub_hz_rounds_to_zero_raises(self):
        """Rates that round to 0 integer Hz must raise ValueError, not ZeroDivisionError."""
        with self.assertRaises(ValueError):
            dc.resample_iq(self._samples, 0.4, FS)
        with self.assertRaises(ValueError):
            dc.resample_iq(self._samples, FS, 0.4)

    def test_extreme_decimation_returns_empty(self):
        """A single sample decimated by a large factor yields an empty array."""
        tiny = self._samples[:1]
        out = dc.resample_iq(tiny, FS * 1_000, FS)
        self.assertEqual(len(out), 0)
        self.assertEqual(out.dtype, np.complex64)

    def test_scipy_path(self):
        """scipy backend produces the correct output length."""
        if not dc._HAVE_SCIPY:
            self.skipTest("scipy not installed")
        fs_out = FS // 4
        out = dc.resample_iq(self._samples, FS, fs_out)
        expected = int(round(len(self._samples) * fs_out / FS))
        self.assertEqual(len(out), expected)
        self.assertEqual(out.dtype, np.complex64)

    def test_numpy_fallback_path(self):
        """numpy fallback produces the correct output length when scipy is absent."""
        orig = dc._HAVE_SCIPY
        try:
            dc._HAVE_SCIPY = False
            fs_out = FS // 4
            out = dc.resample_iq(self._samples, FS, fs_out)
            expected = int(round(len(self._samples) * fs_out / FS))
            self.assertEqual(len(out), expected)
            self.assertEqual(out.dtype, np.complex64)
        finally:
            dc._HAVE_SCIPY = orig

    def test_numpy_fallback_spans_full_range(self):
        """numpy fallback must not compress the time axis (linspace, not arange)."""
        orig = dc._HAVE_SCIPY
        try:
            dc._HAVE_SCIPY = False
            # Use a simple ramp so we can check first/last values
            n = 100
            ramp = np.arange(n, dtype=np.float32) + 1j * np.arange(n, dtype=np.float32)
            out = dc.resample_iq(ramp, float(n), float(n // 4))
            # First sample should be ~0+0j, last should be ~(n-1)+(n-1)j
            self.assertAlmostEqual(out[0].real, 0.0, places=3)
            self.assertAlmostEqual(out[-1].real, float(n - 1), places=1)
        finally:
            dc._HAVE_SCIPY = orig

    def test_scipy_and_numpy_same_length(self):
        """Both scipy and numpy backends must return the same output length."""
        if not dc._HAVE_SCIPY:
            self.skipTest("scipy not installed")
        fs_out = FS // 3
        out_scipy = dc.resample_iq(self._samples, FS, fs_out)
        orig = dc._HAVE_SCIPY
        try:
            dc._HAVE_SCIPY = False
            out_numpy = dc.resample_iq(self._samples, FS, fs_out)
        finally:
            dc._HAVE_SCIPY = orig
        self.assertEqual(len(out_scipy), len(out_numpy))

    def test_decode_candidate_resamples_at_non_default_fs(self):
        """decode_candidate() resamples snapshots not at _DECODER_FS."""
        np.random.seed(42)
        # Generate FSK at 2× the decoder rate so decode_candidate must downsample
        fs_capture = dc._DECODER_FS * 2
        samples_hi = make_fsk2(n_syms=400, sps=16, fs=fs_capture)
        tmp = tempfile.mkdtemp()
        try:
            snap_path = os.path.join(tmp, "snap_1234567890000000000_c810.cf32")
            with open(snap_path, "wb") as fh:
                fh.write(cf32_bytes(samples_hi))
            candidate = {
                "signal_id": 1, "example_id": 1,
                "db_timestamp": "2023-11-15T00:00:00",
                "source": "test",
                "confidence": 0.81,
                "decision_trace": (
                    "snr=20dB band=ISM-433 scores(fsk_like=0.81,ook_am_like=0.1)"
                    " -> fsk_like@0.81"
                ),
            }
            snap = {
                "path": snap_path,
                "filename": os.path.basename(snap_path),
                "size_bytes": os.path.getsize(snap_path),
            }
            result = dc.decode_candidate(candidate, snap, fs_capture, use_external=False)
            self.assertTrue(result["decoded"], msg=f"decode failed after resample: {result}")
        finally:
            shutil.rmtree(tmp)

    def test_rational_ratio_44100_to_48000(self):
        """resample_iq produces the correct output length for 44100→48000 Hz.

        With limit_denominator(1000) the exact GCD fraction is preserved:
        gcd=300, up=160, down=147 — denominator 147 is well below the cap.
        """
        # 44100 → 48000: gcd=300, exact up=160, down=147 (denominator < 1000).
        out = dc.resample_iq(self._samples, 44_100, 48_000)
        expected = int(round(len(self._samples) * 48_000 / 44_100))
        self.assertEqual(len(out), expected)
        self.assertEqual(out.dtype, np.complex64)

    def test_excessive_upsample_ratio_raises(self):
        """resample_iq raises ValueError when the upsample ratio exceeds _MAX_UPSAMPLE_RATIO."""
        # fs_out / fs_in = 2_048_000 / 1 = 2_048_000 >> _MAX_UPSAMPLE_RATIO
        with self.assertRaises(ValueError):
            dc.resample_iq(self._samples, 1, dc._DECODER_FS)

    def test_max_output_samples_guard_raises(self):
        """resample_iq raises ValueError when n_out would exceed _MAX_RESAMPLE_OUTPUT."""
        # Use a ratio just within _MAX_UPSAMPLE_RATIO but with a long input
        # so that n_out = len * ratio > _MAX_RESAMPLE_OUTPUT.
        # ratio=999, input_len = _MAX_RESAMPLE_OUTPUT // 999 + 1 overflows.
        input_len = dc._MAX_RESAMPLE_OUTPUT // 999 + 1
        big_samples = np.zeros(input_len, dtype=np.complex64)
        # fs_out/fs_in = 999: just within ratio limit but n_out > 20M
        with self.assertRaisesRegex(ValueError, r"exceeds the maximum"):
            dc.resample_iq(big_samples, 1000, 999_000)

    def test_decode_candidate_max_load_scales_with_fs(self):
        """decode_candidate loads min(_MAX_RESAMPLE_OUTPUT, max(1, round(_MAX_DECODE_SAMPLES * fs_i / _DECODER_FS))) samples."""
        import unittest.mock as mock

        # At fs=_DECODER_FS/10, max_load should be _MAX_DECODE_SAMPLES/10.
        fs_capture = dc._DECODER_FS // 10
        expected_max_load = min(
            dc._MAX_RESAMPLE_OUTPUT,
            max(1, round(dc._MAX_DECODE_SAMPLES * fs_capture / dc._DECODER_FS)),
        )
        calls = []

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        snap_path = os.path.join(tmp, "snap_1234567890000000000_c810.cf32")
        # Write a minimal CF32 file large enough for sha256_file to succeed
        with open(snap_path, "wb") as fh:
            fh.write(np.zeros(100, dtype=np.float32).tobytes())

        snap = {"path": snap_path, "filename": os.path.basename(snap_path),
                "size_bytes": os.path.getsize(snap_path)}
        cand = {
            "signal_id": 1, "example_id": 1, "db_timestamp": "",
            "source": "", "confidence": 0.9,
            "decision_trace": "snr=10dB scores(FSK=0.9) -> FSK@0.9",
        }

        orig_load = dc.load_cf32
        def patched_load(path, max_samples):
            calls.append(max_samples)
            return orig_load(path, max_samples)

        with mock.patch.object(dc, "load_cf32", side_effect=patched_load):
            dc.decode_candidate(cand, snap, float(fs_capture), False)

        self.assertEqual(len(calls), 1, "load_cf32 should be called exactly once")
        self.assertEqual(calls[0], expected_max_load)

    def test_decode_candidate_max_load_capped_at_max_resample_output(self):
        """max_load is capped at _MAX_RESAMPLE_OUTPUT even for very high capture rates."""
        import unittest.mock as mock

        # A capture rate 1000× _DECODER_FS would naively give max_load =
        # _MAX_DECODE_SAMPLES * 1000 = 200_000_000, but the cap must clamp it
        # to _MAX_RESAMPLE_OUTPUT = 20_000_000.
        fs_capture = dc._DECODER_FS * 1000
        calls = []

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        snap_path = os.path.join(tmp, "snap_1234567890000000000_c810.cf32")
        with open(snap_path, "wb") as fh:
            fh.write(np.zeros(100, dtype=np.float32).tobytes())

        snap = {"path": snap_path, "filename": os.path.basename(snap_path),
                "size_bytes": os.path.getsize(snap_path)}
        cand = {
            "signal_id": 1, "example_id": 1, "db_timestamp": "",
            "source": "", "confidence": 0.9,
            "decision_trace": "snr=10dB scores(FSK=0.9) -> FSK@0.9",
        }

        orig_load = dc.load_cf32
        def patched_load(path, max_samples):
            calls.append(max_samples)
            return orig_load(path, max_samples)

        with mock.patch.object(dc, "load_cf32", side_effect=patched_load):
            dc.decode_candidate(cand, snap, float(fs_capture), False)

        self.assertEqual(len(calls), 1, "load_cf32 should be called exactly once")
        self.assertEqual(calls[0], dc._MAX_RESAMPLE_OUTPUT,
                         "max_load must be capped at _MAX_RESAMPLE_OUTPUT")

    def test_decode_candidate_resample_error_recorded_not_raised(self):
        """decode_candidate catches ValueError from resample_iq and records it instead of raising."""
        import unittest.mock as mock

        # Write a minimal CF32 snapshot file (8 float32 values = 32 bytes = 4 complex samples)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        snap_path = os.path.join(tmp, "snap_1234567890000000000_c810.cf32")
        with open(snap_path, "wb") as fh:
            fh.write(np.zeros(8, dtype=np.float32).tobytes())

        snap = {"path": snap_path, "filename": os.path.basename(snap_path),
                "size_bytes": os.path.getsize(snap_path)}
        cand = {
            "signal_id": 1, "example_id": 1, "db_timestamp": "",
            "source": "", "confidence": 0.9,
            "decision_trace": "snr=10dB scores(FSK=0.9) -> FSK@0.9",
        }
        # Patch resample_iq to raise ValueError to simulate OOM guard firing
        with mock.patch.object(dc, "resample_iq", side_effect=ValueError("simulated OOM")):
            entry = dc.decode_candidate(cand, snap, dc._DECODER_FS / 2, False)

        self.assertIn("resample_failed", entry["decode_result"].get("error", ""))
        self.assertFalse(entry["decoded"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
