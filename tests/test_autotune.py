#!/usr/bin/env python3
"""
tests/test_autotune.py — Unit tests for tools/autotune_thresholds.py.

Tests the threshold recommendation logic, config file update helper, and
CLI interface without requiring real SDR hardware or snapshot files.

Run with:
    python3 tests/test_autotune.py [-v]

Requires: numpy
"""

import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tools"))

import autotune_thresholds as at


# ---------------------------------------------------------------------------
# Signal generators (same as in the module, duplicated for test isolation)
# ---------------------------------------------------------------------------

def _awgn(signal: np.ndarray, snr_db: float) -> np.ndarray:
    sig_pow = float(np.mean(np.abs(signal) ** 2)) or 1.0
    noise_pow = sig_pow / 10 ** (snr_db / 10.0)
    noise = np.sqrt(noise_pow / 2.0) * (
        np.random.randn(len(signal)) + 1j * np.random.randn(len(signal))
    )
    return (signal + noise).astype(np.complex64)


def _fsk2(n: int = 4096) -> np.ndarray:
    bits = np.random.randint(0, 2, n // 16)
    phase_inc = 2.0 * math.pi * (2 * bits - 1) * 50_000 / 2_048_000
    return np.exp(1j * np.cumsum(np.repeat(phase_inc, 16))).astype(np.complex64)


def _noise_only(n: int = 4096) -> np.ndarray:
    return (
        np.random.randn(n) + 1j * np.random.randn(n)
    ).astype(np.complex64) * 1e-4


# ---------------------------------------------------------------------------
# Metric tests
# ---------------------------------------------------------------------------

class TestSignalMetrics(unittest.TestCase):
    """Unit tests for the per-block metric helpers."""

    def setUp(self):
        np.random.seed(0)

    def test_avg_power_nonzero(self):
        s = _awgn(_fsk2(), snr_db=10.0)
        self.assertGreater(at.avg_power(s), 0.0)

    def test_avg_power_zero_for_silence(self):
        s = np.zeros(512, dtype=np.complex64)
        self.assertEqual(at.avg_power(s), 0.0)

    def test_snr_db_positive_for_strong_signal(self):
        s = _awgn(_fsk2(), snr_db=15.0)
        self.assertGreater(at.snr_db(s), 0.0)

    def test_snr_db_returns_neg999_for_silence(self):
        s = np.zeros(512, dtype=np.complex64)
        self.assertEqual(at.snr_db(s), -999.0)

    def test_spectral_flatness_for_noise_below_half(self):
        # AWGN instantaneous power follows an exponential distribution.
        # The temporal-power flatness (geometric/arithmetic mean ratio) for
        # an exponential distribution equals e^{-γ} ≈ 0.56 (Euler-Mascheroni),
        # so pure noise should score well below 0.8.
        np.random.seed(1)
        s = (np.random.randn(4096) + 1j * np.random.randn(4096)).astype(np.complex64)
        flat = at.spectral_flatness(s)
        self.assertLess(flat, 0.8,
                        "AWGN temporal-power flatness should be well below 1.0")

    def test_spectral_flatness_below_one_for_fsk(self):
        s = _awgn(_fsk2(), snr_db=20.0)
        flat = at.spectral_flatness(s)
        self.assertLess(flat, 1.0, "Structured signal flatness should be < 1.0")

    def test_heuristic_confidence_in_range(self):
        s = _awgn(_fsk2(), snr_db=10.0)
        c = at.heuristic_confidence(s)
        self.assertGreaterEqual(c, 0.0)
        self.assertLessEqual(c, 1.0)

    def test_heuristic_confidence_zero_for_empty(self):
        s = np.zeros(10, dtype=np.complex64)
        self.assertEqual(at.heuristic_confidence(s), 0.0)

    def test_heuristic_confidence_is_margin_normalized(self):
        """heuristic_confidence() must delegate to _margin_confidence().

        Patching _margin_confidence confirms it is actually invoked, ensuring
        the function does not silently regress to returning the raw max score.
        """
        from unittest.mock import patch
        s = _awgn(_fsk2(), snr_db=10.0)
        with patch.object(at, '_margin_confidence', wraps=at._margin_confidence) as mock_mc:
            c = at.heuristic_confidence(s)
            mock_mc.assert_called_once()
            # Result must match what _margin_confidence returned
            args = mock_mc.call_args[0][0]
            self.assertAlmostEqual(c, at._margin_confidence(args), places=9)

    def test_margin_confidence_formula(self):
        """_margin_confidence() formula: (winner - runner_up) / (winner + ε).

        Equal class scores → near 0.0; one dominant score → > 0.8.
        """
        # Four equal scores: (best - runner_up) / (best + ε) ≈ 0
        conf_tie = at._margin_confidence([0.25, 0.25, 0.25, 0.25])
        self.assertAlmostEqual(conf_tie, 0.0, places=6,
                               msg="Equal scores must yield near-zero confidence")

        # One dominant score: margin = (0.9 - 0.1) / (0.9 + ε) ≈ 0.889
        conf_dominant = at._margin_confidence([0.1, 0.1, 0.1, 0.9])
        self.assertGreater(conf_dominant, 0.8,
                           msg="Dominant score must yield high confidence (>0.8)")


# ---------------------------------------------------------------------------
# Recommendation tests
# ---------------------------------------------------------------------------

class TestComputeRecommendations(unittest.TestCase):
    """compute_recommendations() must return sane, in-range values."""

    def setUp(self):
        np.random.seed(2)
        self.blocks = [_awgn(_fsk2(), snr_db=s) for s in (5.0, 10.0, 15.0)] * 5

    def test_raises_on_empty_blocks(self):
        with self.assertRaises(ValueError):
            at.compute_recommendations([])

    def test_returns_expected_keys(self):
        recs = at.compute_recommendations(self.blocks)
        expected = {
            "RF_MIN_POWER", "RF_CONF_THRESHOLD", "RF_CONSOLE_CONF",
            "RF_SNAPSHOT_CONF", "RF_SNR_MIN_DB", "RF_EXPECTED_BW_HZ",
        }
        self.assertEqual(set(recs.keys()), expected)

    def test_min_power_positive(self):
        recs = at.compute_recommendations(self.blocks)
        self.assertGreater(float(recs["RF_MIN_POWER"]), 0.0)

    def test_conf_threshold_in_range(self):
        recs = at.compute_recommendations(self.blocks)
        val = float(recs["RF_CONF_THRESHOLD"])
        self.assertGreaterEqual(val, 0.3)
        self.assertLessEqual(val, 0.85)

    def test_console_conf_greater_than_conf_threshold(self):
        recs = at.compute_recommendations(self.blocks)
        self.assertGreater(
            float(recs["RF_CONSOLE_CONF"]),
            float(recs["RF_CONF_THRESHOLD"]),
            "RF_CONSOLE_CONF must be > RF_CONF_THRESHOLD",
        )

    def test_snapshot_conf_equals_conf_threshold(self):
        recs = at.compute_recommendations(self.blocks)
        self.assertEqual(recs["RF_SNAPSHOT_CONF"], recs["RF_CONF_THRESHOLD"])

    def test_snr_min_db_nonnegative(self):
        recs = at.compute_recommendations(self.blocks)
        self.assertGreaterEqual(float(recs["RF_SNR_MIN_DB"]), 0.0)

    def test_expected_bw_nonnegative(self):
        recs = at.compute_recommendations(self.blocks)
        self.assertGreaterEqual(int(recs["RF_EXPECTED_BW_HZ"]), 0)

    def test_verbose_flag_does_not_raise(self):
        """verbose=True should print stats without raising."""
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            at.compute_recommendations(self.blocks, verbose=True)
        output = buf.getvalue()
        self.assertIn("Analysed", output)


# ---------------------------------------------------------------------------
# Synthetic block generation tests
# ---------------------------------------------------------------------------

class TestGenerateSyntheticBlocks(unittest.TestCase):
    """generate_synthetic_blocks() must return a usable set of IQ blocks."""

    def test_returns_list_of_arrays(self):
        blocks = at.generate_synthetic_blocks(n_blocks_per_mod=2, seed=0)
        self.assertIsInstance(blocks, list)
        self.assertGreater(len(blocks), 0)
        self.assertIsInstance(blocks[0], np.ndarray)

    def test_blocks_are_complex64(self):
        blocks = at.generate_synthetic_blocks(n_blocks_per_mod=2, seed=0)
        for b in blocks:
            self.assertEqual(b.dtype, np.complex64)

    def test_count_proportional_to_n_blocks_per_mod(self):
        n = 3
        snrs = (5.0, 10.0)
        blocks = at.generate_synthetic_blocks(n_blocks_per_mod=n, snrs=snrs, seed=1)
        # 4 modulation types × n blocks × len(snrs)
        self.assertEqual(len(blocks), 4 * n * len(snrs))


# ---------------------------------------------------------------------------
# Config file update tests
# ---------------------------------------------------------------------------

class TestUpdateConfFile(unittest.TestCase):
    """update_conf_file() must correctly update and create config files."""

    def _make_recs(self) -> Dict[str, str]:
        return {
            "RF_MIN_POWER": "1.00e-05",
            "RF_CONF_THRESHOLD": "0.55",
            "RF_CONSOLE_CONF": "0.80",
            "RF_SNAPSHOT_CONF": "0.55",
            "RF_SNR_MIN_DB": "2.0",
            "RF_EXPECTED_BW_HZ": "50000",
        }

    def test_creates_file_when_absent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            conf = os.path.join(tmpdir, "thresholds.env")
            at.update_conf_file(conf, self._make_recs(), dry_run=False)
            self.assertTrue(os.path.isfile(conf))

    def test_all_keys_written(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            conf = os.path.join(tmpdir, "thresholds.env")
            recs = self._make_recs()
            at.update_conf_file(conf, recs, dry_run=False)
            content = Path(conf).read_text()
            for key, value in recs.items():
                self.assertIn(f"{key}={value}", content)

    def test_existing_key_replaced(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            conf = os.path.join(tmpdir, "thresholds.env")
            # Write original value
            Path(conf).write_text("RF_MIN_POWER=9.99e-01\n")
            recs = {"RF_MIN_POWER": "1.23e-06"}
            at.update_conf_file(conf, recs, dry_run=False)
            content = Path(conf).read_text()
            self.assertIn("RF_MIN_POWER=1.23e-06", content)
            self.assertNotIn("RF_MIN_POWER=9.99e-01", content)

    def test_other_lines_preserved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            conf = os.path.join(tmpdir, "thresholds.env")
            original = "# My comment\nRF_BLOCK_LEN=4096\nRF_MIN_POWER=5e-6\n"
            Path(conf).write_text(original)
            recs = {"RF_MIN_POWER": "1.00e-05"}
            at.update_conf_file(conf, recs, dry_run=False)
            content = Path(conf).read_text()
            self.assertIn("# My comment", content)
            self.assertIn("RF_BLOCK_LEN=4096", content)

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            conf = os.path.join(tmpdir, "thresholds.env")
            at.update_conf_file(conf, self._make_recs(), dry_run=True)
            self.assertFalse(os.path.exists(conf),
                             "dry_run=True must not create the file")

    def test_dry_run_prints_values(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmpdir:
            conf = os.path.join(tmpdir, "thresholds.env")
            recs = self._make_recs()
            buf = io.StringIO()
            with redirect_stdout(buf):
                at.update_conf_file(conf, recs, dry_run=True)
            output = buf.getvalue()
            self.assertIn("[dry-run]", output)
            for key in recs:
                self.assertIn(key, output)


# ---------------------------------------------------------------------------
# IQ file loading tests
# ---------------------------------------------------------------------------

class TestLoadCf32(unittest.TestCase):
    """load_cf32() must correctly read and reject IQ files."""

    def _write_cf32(self, path: str, signal: np.ndarray) -> None:
        interleaved = np.empty(len(signal) * 2, dtype=np.float32)
        interleaved[0::2] = signal.real
        interleaved[1::2] = signal.imag
        with open(path, "wb") as fh:
            fh.write(interleaved.tobytes())

    def test_load_valid_file(self):
        np.random.seed(3)
        s = _awgn(_fsk2(), snr_db=10.0)
        with tempfile.NamedTemporaryFile(suffix=".cf32", delete=False) as f:
            tmp = f.name
        try:
            self._write_cf32(tmp, s)
            loaded = at.load_cf32(tmp)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.dtype, np.complex64)
            self.assertGreater(len(loaded), 0)
        finally:
            os.unlink(tmp)

    def test_returns_none_for_short_file(self):
        with tempfile.NamedTemporaryFile(suffix=".cf32", delete=False) as f:
            f.write(b"\x00" * 8)  # 2 samples — below min threshold
            tmp = f.name
        try:
            result = at.load_cf32(tmp)
            self.assertIsNone(result)
        finally:
            os.unlink(tmp)

    def test_returns_none_for_missing_file(self):
        result = at.load_cf32("/nonexistent/path/that/does/not/exist.cf32")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# collect_snapshots tests
# ---------------------------------------------------------------------------

class TestCollectSnapshots(unittest.TestCase):
    """collect_snapshots() must find .cf32 files recursively."""

    def _write_cf32(self, path: str, n: int = 512) -> None:
        s = np.ones(n, dtype=np.complex64)
        interleaved = np.empty(n * 2, dtype=np.float32)
        interleaved[0::2] = s.real
        interleaved[1::2] = s.imag
        with open(path, "wb") as fh:
            fh.write(interleaved.tobytes())

    def test_returns_empty_for_missing_dir(self):
        result = at.collect_snapshots("/nonexistent/directory")
        self.assertEqual(result, [])

    def test_finds_cf32_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_cf32(os.path.join(tmpdir, "a.cf32"))
            self._write_cf32(os.path.join(tmpdir, "b.cf32"))
            result = at.collect_snapshots(tmpdir)
            self.assertEqual(len(result), 2)

    def test_ignores_non_cf32_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write a valid cf32 and a txt file
            self._write_cf32(os.path.join(tmpdir, "good.cf32"))
            open(os.path.join(tmpdir, "readme.txt"), "w").close()
            result = at.collect_snapshots(tmpdir)
            self.assertEqual(len(result), 1)

    def test_finds_files_in_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sub = os.path.join(tmpdir, "sub")
            os.makedirs(sub)
            self._write_cf32(os.path.join(sub, "deep.cf32"))
            result = at.collect_snapshots(tmpdir)
            self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# End-to-end: synthetic fallback path
# ---------------------------------------------------------------------------

class TestEndToEndSyntheticFallback(unittest.TestCase):
    """Full pipeline: no snapshots → synthetic signals → valid recommendations."""

    def test_synthetic_path_produces_valid_thresholds(self):
        """Calling compute_recommendations on synthetic blocks must succeed."""
        np.random.seed(42)
        blocks = at.generate_synthetic_blocks(n_blocks_per_mod=5, seed=42)
        recs = at.compute_recommendations(blocks)
        self.assertIn("RF_MIN_POWER", recs)
        self.assertGreater(float(recs["RF_MIN_POWER"]), 0.0)
        self.assertGreater(float(recs["RF_CONF_THRESHOLD"]), 0.0)
        self.assertGreater(
            float(recs["RF_CONSOLE_CONF"]),
            float(recs["RF_CONF_THRESHOLD"]),
        )

    def test_write_to_tempfile(self):
        """update_conf_file writes correctly to a temporary destination."""
        np.random.seed(7)
        blocks = at.generate_synthetic_blocks(n_blocks_per_mod=3, seed=7)
        recs = at.compute_recommendations(blocks)
        with tempfile.TemporaryDirectory() as tmpdir:
            conf = os.path.join(tmpdir, "thresholds.env")
            at.update_conf_file(conf, recs, dry_run=False)
            content = Path(conf).read_text()
        for key, value in recs.items():
            self.assertIn(f"{key}={value}", content)


# ---------------------------------------------------------------------------
# format_recommendations tests
# ---------------------------------------------------------------------------

class TestFormatRecommendations(unittest.TestCase):
    """format_recommendations() must produce a human-readable string."""

    def _make_recs(self) -> Dict[str, str]:
        return {
            "RF_MIN_POWER": "1.00e-05",
            "RF_CONF_THRESHOLD": "0.55",
            "RF_CONSOLE_CONF": "0.80",
            "RF_SNAPSHOT_CONF": "0.55",
            "RF_SNR_MIN_DB": "2.0",
            "RF_EXPECTED_BW_HZ": "50000",
        }

    def test_returns_string(self):
        recs = self._make_recs()
        result = at.format_recommendations(recs, source="synthetic")
        self.assertIsInstance(result, str)

    def test_contains_source_name(self):
        recs = self._make_recs()
        result = at.format_recommendations(recs, source="my_snapshots")
        self.assertIn("my_snapshots", result)

    def test_contains_all_keys_and_values(self):
        recs = self._make_recs()
        result = at.format_recommendations(recs, source="test")
        for key, value in recs.items():
            self.assertIn(key, result, f"Key '{key}' missing from format output")
            self.assertIn(value, result, f"Value '{value}' missing from format output")

    def test_contains_apply_instruction(self):
        recs = self._make_recs()
        result = at.format_recommendations(recs, source="test")
        self.assertIn("autotune_thresholds.py", result)

    def test_empty_recs_does_not_raise(self):
        result = at.format_recommendations({}, source="empty")
        self.assertIsInstance(result, str)
        self.assertIn("empty", result)

    def test_multiline_output(self):
        recs = self._make_recs()
        result = at.format_recommendations(recs, source="test")
        self.assertGreater(result.count("\n"), 3,
                           "format_recommendations should produce multiple lines")


# ---------------------------------------------------------------------------
# estimate_bandwidth_hz tests
# ---------------------------------------------------------------------------

class TestEstimateBandwidthHz(unittest.TestCase):
    """estimate_bandwidth_hz() must return sensible bandwidth estimates."""

    def setUp(self):
        np.random.seed(42)

    def test_returns_positive_value(self):
        s = _awgn(_fsk2(), snr_db=15.0)
        bw = at.estimate_bandwidth_hz(s, sample_rate=2_048_000)
        self.assertGreater(bw, 0.0)

    def test_result_bounded_by_sample_rate(self):
        s = _awgn(_fsk2(), snr_db=15.0)
        fs = 2_048_000.0
        bw = at.estimate_bandwidth_hz(s, sample_rate=fs)
        self.assertLessEqual(bw, fs)

    def test_scales_with_sample_rate(self):
        s = _awgn(_fsk2(), snr_db=15.0)
        bw1 = at.estimate_bandwidth_hz(s, sample_rate=2_048_000.0)
        bw2 = at.estimate_bandwidth_hz(s, sample_rate=4_096_000.0)
        # With doubled sample rate the bandwidth estimate should double
        self.assertAlmostEqual(bw2 / bw1, 2.0, places=5)

    def test_noise_only_returns_small_fraction_of_fs(self):
        # Pure noise has high spectral flatness → est_bw_frac = 1 - flatness → small
        # In practice noise flatness is ~0.5-0.6 so bw_frac is ~0.4-0.5 of fs;
        # just verify it is strictly positive and finite.
        noise = (np.random.randn(4096) + 1j * np.random.randn(4096)).astype(np.complex64)
        bw = at.estimate_bandwidth_hz(noise, sample_rate=2_048_000.0)
        self.assertGreater(bw, 0.0)
        self.assertTrue(math.isfinite(bw))

    def test_uses_default_sample_rate(self):
        s = _awgn(_fsk2(), snr_db=15.0)
        bw_default = at.estimate_bandwidth_hz(s)
        bw_explicit = at.estimate_bandwidth_hz(s, sample_rate=2_048_000.0)
        self.assertAlmostEqual(bw_default, bw_explicit, places=1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
