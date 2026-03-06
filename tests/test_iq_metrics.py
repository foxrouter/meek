#!/usr/bin/env python3
"""
tests/test_iq_metrics.py — Validate C++ iq_metrics output against the Python
reference implementation in tools/autotune_thresholds.py.

Run:
    python3 tests/test_iq_metrics.py [<path-to-iq_metrics-binary>] [-v]

The path to the built iq_metrics binary can be supplied as the first positional
argument (e.g. from CTest via $<TARGET_FILE:iq_metrics>).  When omitted, the
test falls back to looking for 'iq_metrics' next to this file, then on PATH.

Requires: numpy
"""

import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tools"))
import autotune_thresholds as at  # noqa: E402

# ---------------------------------------------------------------------------
# Locate the iq_metrics binary
# ---------------------------------------------------------------------------


def _find_binary(argv: List[str]) -> str:
    """Return path to iq_metrics binary; raises if not found."""
    # Check if a path was supplied as positional arg (CTest passes it).
    for arg in argv[1:]:
        if not arg.startswith("-") and os.path.isfile(arg):
            return arg

    # Check next to this test file (useful when running from build dir).
    candidate = Path(__file__).parent.parent / "build" / "iq_metrics"
    if candidate.is_file():
        return str(candidate)

    # Search PATH
    import shutil
    found = shutil.which("iq_metrics")
    if found:
        return found

    raise FileNotFoundError(
        "iq_metrics binary not found. Build with:\n"
        "  cmake -DBUILD_HARDWARE_TARGETS=OFF -B build\n"
        "  cmake --build build -t iq_metrics\n"
        "Then re-run: python3 tests/test_iq_metrics.py build/iq_metrics"
    )


IQ_METRICS_BIN: Optional[str] = None


def _run_iq_metrics(path: str) -> dict:
    """Run iq_metrics on *path* and return parsed JSON dict."""
    result = subprocess.run(
        [IQ_METRICS_BIN, path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"iq_metrics exited with {result.returncode}:\n{result.stderr}"
        )
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Synthetic signal helpers (self-contained, no gen_test_signals dependency)
# ---------------------------------------------------------------------------

FS = 2_048_000.0
SPS = 16


def _awgn(signal: np.ndarray, snr_db_val: float) -> np.ndarray:
    sig_pow = float(np.mean(np.abs(signal) ** 2)) or 1.0
    noise_pow = sig_pow / 10 ** (snr_db_val / 10.0)
    noise = np.sqrt(noise_pow / 2.0) * (
        np.random.randn(len(signal)) + 1j * np.random.randn(len(signal))
    )
    return (signal + noise).astype(np.complex64)


def _fsk2(n: int = 4096) -> np.ndarray:
    bits = np.random.randint(0, 2, n // SPS)
    phase_inc = 2.0 * math.pi * (2 * bits - 1) * 50_000 / FS
    return np.exp(1j * np.cumsum(np.repeat(phase_inc, SPS))).astype(np.complex64)


def _noise_only(n: int = 4096) -> np.ndarray:
    return (np.random.randn(n) + 1j * np.random.randn(n)).astype(np.complex64) * 1e-4


def _ook(n: int = 4096) -> np.ndarray:
    bits = (np.random.random(n // SPS) < 0.5).astype(np.float32)
    return np.repeat(bits, SPS).astype(np.complex64)


def _cw(n: int = 4096) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / FS
    return np.exp(1j * 2.0 * math.pi * 1_000.0 * t).astype(np.complex64)


def _save_cf32(arr: np.ndarray, path: str) -> None:
    raw = np.empty(2 * len(arr), dtype=np.float32)
    raw[0::2] = arr.real
    raw[1::2] = arr.imag
    raw.tofile(path)


# ---------------------------------------------------------------------------
# Tolerance
# ---------------------------------------------------------------------------
# All comparisons use relative tolerance to account for floating-point
# rounding differences between C++ (double arithmetic throughout) and Python
# (NumPy float64).  The tolerance is generous at 0.5% — the algorithms are
# identical so any larger discrepancy indicates a logic bug.
REL_TOL = 5e-3  # 0.5 %
ABS_TOL_SNR = 0.05  # ±0.05 dB absolute for snr_db (low-SNR edge cases)


def _assert_close(name: str, py_val: float, cpp_val: float,
                  rel_tol: float = REL_TOL, abs_tol: float = 0.0) -> None:
    """Assert |cpp - py| ≤ max(rel_tol * |py|, abs_tol)."""
    if not math.isfinite(py_val) and not math.isfinite(cpp_val):
        return
    denom = max(abs(py_val), 1e-30)
    rel_err = abs(cpp_val - py_val) / denom
    abs_err = abs(cpp_val - py_val)
    ok = rel_err <= rel_tol or abs_err <= abs_tol
    if not ok:
        raise AssertionError(
            f"{name}: Python={py_val:.8g}, C++={cpp_val:.8g}, "
            f"rel_err={rel_err:.3e} (tol={rel_tol:.3e}), "
            f"abs_err={abs_err:.3e} (tol={abs_tol:.3e})"
        )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestIqMetricsVsPython(unittest.TestCase):
    """Compare iq_metrics C++ output with autotune_thresholds Python reference."""

    def _check(self, label: str, samples: np.ndarray,
               snr_abs_tol: float = ABS_TOL_SNR) -> None:
        with tempfile.NamedTemporaryFile(suffix=".cf32", delete=False) as f:
            tmp = f.name
        try:
            _save_cf32(samples, tmp)
            cpp = _run_iq_metrics(tmp)
        finally:
            os.unlink(tmp)

        py_avg_pow = at.avg_power(samples)
        py_snr = at.snr_db(samples)
        py_flat = at.spectral_flatness(samples)
        py_bw = at.estimate_bandwidth_hz(samples, FS)

        self.assertEqual(cpp["n_samples"], len(samples),
                         f"{label}: n_samples mismatch")
        _assert_close(f"{label}/avg_power", py_avg_pow, cpp["avg_power"])
        _assert_close(f"{label}/snr_db", py_snr, cpp["snr_db"],
                      rel_tol=0.02, abs_tol=snr_abs_tol)
        _assert_close(f"{label}/spectral_flatness", py_flat, cpp["spectral_flatness"])
        _assert_close(f"{label}/est_bw_hz", py_bw, cpp["est_bw_hz"])

    def test_awgn_noise_only(self) -> None:
        """Pure AWGN — low SNR, flatness near e^{-γ} ≈ 0.56."""
        np.random.seed(1)
        self._check("awgn", _noise_only(4096))

    def test_fsk2_high_snr(self) -> None:
        """FSK2 signal at high SNR — high flatness, clear signal power."""
        np.random.seed(2)
        sig = _awgn(_fsk2(4096), snr_db_val=20.0)
        self._check("fsk2_20dB", sig)

    def test_fsk2_low_snr(self) -> None:
        """FSK2 at SNR=5 dB — near noise floor; larger SNR tolerance."""
        np.random.seed(3)
        sig = _awgn(_fsk2(4096), snr_db_val=5.0)
        self._check("fsk2_5dB", sig, snr_abs_tol=0.5)

    def test_ook_signal(self) -> None:
        """OOK signal — low flatness due to on/off envelope."""
        np.random.seed(4)
        sig = _awgn(_ook(4096), snr_db_val=15.0)
        self._check("ook_15dB", sig)

    def test_cw_tone(self) -> None:
        """CW (single tone) — very high flatness, narrow BW."""
        np.random.seed(5)
        sig = _awgn(_cw(4096), snr_db_val=25.0)
        self._check("cw_25dB", sig)

    def test_large_block(self) -> None:
        """Large 65536-sample block — verify no integer overflow / precision loss."""
        np.random.seed(6)
        sig = _awgn(_fsk2(65536), snr_db_val=15.0)
        self._check("fsk2_65536_15dB", sig)

    def test_zero_power(self) -> None:
        """All-zero IQ — degenerate edge case; snr_db should return -999."""
        samples = np.zeros(512, dtype=np.complex64)
        with tempfile.NamedTemporaryFile(suffix=".cf32", delete=False) as f:
            tmp = f.name
        try:
            _save_cf32(samples, tmp)
            cpp = _run_iq_metrics(tmp)
        finally:
            os.unlink(tmp)

        self.assertAlmostEqual(cpp["avg_power"], 0.0, places=10,
                               msg="zero block: avg_power should be 0")
        self.assertLessEqual(cpp["snr_db"], -900.0,
                             msg="zero block: snr_db should be ≤ -900")
        self.assertAlmostEqual(cpp["spectral_flatness"], 1.0, places=6,
                               msg="zero block: flatness should be 1 (no non-zero samples)")

    def test_multiple_files(self) -> None:
        """Multiple files in one invocation: one JSON line per file."""
        np.random.seed(7)
        files = []
        try:
            for i in range(3):
                arr = _awgn(_fsk2(1024), snr_db_val=15.0).astype(np.complex64)
                f = tempfile.NamedTemporaryFile(suffix=".cf32", delete=False)
                _save_cf32(arr, f.name)
                f.close()
                files.append(f.name)

            result = subprocess.run(
                [IQ_METRICS_BIN] + files,
                capture_output=True, text=True, check=True,
            )
            lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
            self.assertEqual(len(lines), 3, "Expected one JSON line per file")
            for line in lines:
                data = json.loads(line)
                self.assertIn("avg_power", data)
                self.assertIn("snr_db", data)
        finally:
            for f in files:
                os.unlink(f)

    def test_sample_rate_flag(self) -> None:
        """--sample-rate flag changes est_bw_hz proportionally."""
        np.random.seed(8)
        sig = _awgn(_fsk2(4096), snr_db_val=15.0)
        with tempfile.NamedTemporaryFile(suffix=".cf32", delete=False) as f:
            tmp = f.name
        try:
            _save_cf32(sig, tmp)
            r1 = json.loads(subprocess.run(
                [IQ_METRICS_BIN, "--sample-rate", "2048000", tmp],
                capture_output=True, text=True, check=True).stdout)
            r2 = json.loads(subprocess.run(
                [IQ_METRICS_BIN, "--sample-rate", "4096000", tmp],
                capture_output=True, text=True, check=True).stdout)
        finally:
            os.unlink(tmp)

        # snr_db and spectral_flatness must be identical (independent of FS)
        self.assertAlmostEqual(r1["snr_db"], r2["snr_db"], places=6)
        self.assertAlmostEqual(r1["spectral_flatness"], r2["spectral_flatness"], places=6)
        # est_bw_hz should be ~2× when sample rate is 2×
        ratio = r2["est_bw_hz"] / r1["est_bw_hz"]
        self.assertAlmostEqual(ratio, 2.0, places=5)

    def test_block_size_flag(self) -> None:
        """--block-size N restricts analysis to first N samples."""
        np.random.seed(9)
        sig = _awgn(_fsk2(8192), snr_db_val=15.0)
        with tempfile.NamedTemporaryFile(suffix=".cf32", delete=False) as f:
            tmp = f.name
        try:
            _save_cf32(sig, tmp)
            full = json.loads(subprocess.run(
                [IQ_METRICS_BIN, tmp],
                capture_output=True, text=True, check=True).stdout)
            first_half = json.loads(subprocess.run(
                [IQ_METRICS_BIN, "--block-size", "4096", tmp],
                capture_output=True, text=True, check=True).stdout)
        finally:
            os.unlink(tmp)

        self.assertEqual(full["n_samples"], 8192)
        self.assertEqual(first_half["n_samples"], 4096)
        # Metrics on first half should match Python reference on same slice
        py_avg_pow = at.avg_power(sig[:4096])
        _assert_close("block_size/avg_power", py_avg_pow, first_half["avg_power"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Allow binary path as first positional arg; strip it before unittest
    try:
        IQ_METRICS_BIN = _find_binary(sys.argv)
    except FileNotFoundError as exc:
        print(f"SKIP: {exc}", file=sys.stderr)
        sys.exit(0)

    # Remove non-flag positional args (the binary path) from argv so unittest
    # doesn't choke on them.
    cleaned = [sys.argv[0]] + [
        a for a in sys.argv[1:]
        if a.startswith("-") or not os.path.isfile(a)
    ]
    unittest.main(argv=cleaned, verbosity=2 if "-v" in sys.argv else 1)
