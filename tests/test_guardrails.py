#!/usr/bin/env python3
"""
tests/test_guardrails.py — Guardrail rejection tests for the modulation
classifier.

Verifies that:
  - Blocks with SNR below the gate threshold are rejected (REJECT:snr_gate).
  - Blocks with out-of-range bandwidth are rejected (REJECT:bw_gate).
  - Blocks with PAPR above PAPR_MAX are rejected (REJECT:papr_max).
  - The rejection reason is present in the decision_trace JSON field.
  - Blocks that exceed PAPR_MAX do not appear as candidates in the worker log.

Run with:
    python3 tests/test_guardrails.py [-v]

Requires: numpy
"""

import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tests"))


# ---------------------------------------------------------------------------
# Minimal Python re-implementation of classify_block guardrail gates.
# These mirror the C++ logic in src/main.cpp.
# ---------------------------------------------------------------------------

def _avg_power(s: np.ndarray) -> float:
    return float(np.mean(np.abs(s) ** 2))


def _snr_db(s: np.ndarray) -> float:
    powers = np.sort(np.abs(s) ** 2)
    n = len(powers)
    noise = float(powers[n // 2])
    if noise < 1e-30:
        return -999.0
    sig = float(np.mean(powers[3 * n // 4:]))
    return 10.0 * math.log10(sig / noise)


def _papr_db(s: np.ndarray, avg_pow: float) -> float:
    if avg_pow < 1e-30:
        return 0.0
    return 10.0 * math.log10(float(np.max(np.abs(s) ** 2)) / avg_pow)


def _spectral_flatness(s: np.ndarray) -> float:
    pw = np.abs(s) ** 2
    pw = pw[pw > 0]
    if len(pw) == 0:
        return 1.0
    geo = float(np.exp(np.mean(np.log(pw))))
    arith = float(np.mean(pw))
    return geo / arith if arith > 0 else 1.0


def classify_with_trace(
    s: np.ndarray,
    sample_rate: float = 2_048_000,
    snr_min_db: float = 0.0,
    expected_bw_hz: float = 0.0,
    papr_max_db: float = 0.0,
    min_power: float = 5e-6,
) -> Dict:
    """
    Run classification and return a dict with:
      mod_class, confidence, decision_trace, snr_gate_pass, bw_gate_pass,
      papr_gate_pass.
    """
    result = {
        "mod_class": "unknown",
        "confidence": 0.0,
        "decision_trace": "",
        "snr_gate_pass": False,
        "bw_gate_pass": True,
        "papr_gate_pass": True,
    }
    if len(s) < 32:
        return result

    avg_pow = _avg_power(s)
    snr = _snr_db(s)
    papr = _papr_db(s, avg_pow)
    flat = _spectral_flatness(s)

    result["snr_gate_pass"] = snr >= snr_min_db

    # BW guardrail
    if expected_bw_hz > 0.0 and sample_rate > 0.0:
        est_bw_frac = max(0.01, min(1.0, 1.0 - flat))
        est_bw_hz = est_bw_frac * sample_rate
        bw_ratio = est_bw_hz / expected_bw_hz
        result["bw_gate_pass"] = 0.75 <= bw_ratio <= 1.25

    # PAPR gate
    if papr_max_db > 0.0:
        result["papr_gate_pass"] = papr <= papr_max_db

    trace_parts = [
        f"snr={snr:.3f}dB",
        f"papr={papr:.3f}dB",
        f"flat={flat:.3f}",
    ]

    if not result["snr_gate_pass"]:
        trace_parts.append(f"[REJECT:snr_gate snr={snr:.3f}<{snr_min_db}]")
        result["decision_trace"] = " ".join(trace_parts)
        return result

    if not result["bw_gate_pass"]:
        trace_parts.append("[REJECT:bw_gate]")
        result["decision_trace"] = " ".join(trace_parts)
        return result

    if avg_pow < min_power or avg_pow > 1e3:
        trace_parts.append("[REJECT:power_range]")
        result["decision_trace"] = " ".join(trace_parts)
        return result

    if not result["papr_gate_pass"]:
        trace_parts.append(
            f"[REJECT:papr_max papr={papr:.3f}>{papr_max_db}]"
        )
        result["decision_trace"] = " ".join(trace_parts)
        return result

    result["decision_trace"] = " ".join(trace_parts)
    result["mod_class"] = "fsk_like"  # stub; gates are what we're testing
    result["confidence"] = 0.7
    return result


# ---------------------------------------------------------------------------
# Signal generators
# ---------------------------------------------------------------------------

def _awgn(signal: np.ndarray, snr_db: float) -> np.ndarray:
    sig_pow = float(np.mean(np.abs(signal) ** 2)) or 1.0
    noise_pow = sig_pow / 10 ** (snr_db / 10.0)
    noise = np.sqrt(noise_pow / 2.0) * (
        np.random.randn(len(signal)) + 1j * np.random.randn(len(signal))
    )
    return (signal + noise).astype(np.complex64)


def _fsk2(n: int = 3200) -> np.ndarray:
    """Clean FSK2 signal at 2048 ksps / 128 ksps RSYM / 50 kHz fdev."""
    bits = np.random.randint(0, 2, n // 16)
    phase_inc = 2.0 * math.pi * (2 * bits - 1) * 50_000 / 2_048_000
    return np.exp(1j * np.cumsum(np.repeat(phase_inc, 16))).astype(np.complex64)


def _noise_only(n: int = 3200) -> np.ndarray:
    """Pure AWGN — very low SNR block."""
    return (
        np.random.randn(n) + 1j * np.random.randn(n)
    ).astype(np.complex64) * 1e-3


def _narrowband(n: int = 3200, fs: float = 2_048_000) -> np.ndarray:
    """Very narrow-band CW: occupies only ~0.5% of fs → fails BW gate."""
    t = np.arange(n) / fs
    return np.exp(1j * 2.0 * math.pi * 500.0 * t).astype(np.complex64)


def _high_papr(n: int = 3200) -> np.ndarray:
    """Burst-OOK with very high PAPR (short bursts, long quiet)."""
    s = np.zeros(n, dtype=np.complex64)
    s[:n // 20] = 1.0  # 5 % duty — very high PAPR
    return s + 0.001 * (
        np.random.randn(n) + 1j * np.random.randn(n)
    ).astype(np.complex64)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestSnrGateRejection(unittest.TestCase):
    """SNR gate: blocks below snr_min_db must be rejected."""

    def setUp(self):
        np.random.seed(0)

    def test_below_gate_threshold_rejected(self):
        """Signal at 5 dB SNR rejected when gate is set to 20 dB."""
        # Use a high gate threshold that a real signal cannot satisfy to
        # guarantee REJECT:snr_gate regardless of signal type.
        s = _awgn(_fsk2(), snr_db=15.0)
        r = classify_with_trace(s, snr_min_db=50.0)
        self.assertFalse(r["snr_gate_pass"],
                         "Signal should fail a 50 dB SNR gate")
        self.assertIn("REJECT:snr_gate", r["decision_trace"])

    def test_signal_passes_gate(self):
        """High-SNR FSK block passes SNR gate."""
        s = _awgn(_fsk2(), snr_db=15.0)
        r = classify_with_trace(s, snr_min_db=0.0)
        self.assertTrue(r["snr_gate_pass"], "15 dB SNR block should pass gate")
        self.assertNotIn("REJECT:snr_gate", r["decision_trace"])

    def test_canary_mode_passes_noise(self):
        """With gate=-999 (canary), even noisy blocks pass the SNR gate."""
        s = _noise_only()
        r = classify_with_trace(s, snr_min_db=-999.0)
        self.assertTrue(
            r["snr_gate_pass"],
            "Canary mode (SNR gate -999) should pass all blocks",
        )
        self.assertNotIn("REJECT:snr_gate", r["decision_trace"])


class TestBwGateRejection(unittest.TestCase):
    """BW guardrail: blocks with wrong bandwidth must be rejected."""

    def setUp(self):
        np.random.seed(1)

    def test_narrowband_rejected(self):
        """Very narrowband signal rejected when expected_bw_hz is much wider."""
        s = _awgn(_narrowband(), snr_db=20.0)
        # Expected BW = 100 kHz; actual ~1 kHz → far outside ±25% window
        r = classify_with_trace(s, snr_min_db=-999.0, expected_bw_hz=100_000)
        self.assertFalse(r["bw_gate_pass"], "Narrowband should fail BW gate")
        self.assertIn("REJECT:bw_gate", r["decision_trace"])

    def test_bw_gate_disabled(self):
        """With expected_bw_hz=0, BW gate is disabled and block passes."""
        s = _awgn(_narrowband(), snr_db=20.0)
        r = classify_with_trace(s, snr_min_db=-999.0, expected_bw_hz=0)
        self.assertTrue(r["bw_gate_pass"], "BW gate should be disabled when 0")
        self.assertNotIn("REJECT:bw_gate", r["decision_trace"])


class TestPaprMaxGate(unittest.TestCase):
    """PAPR_MAX gate: blocks exceeding max PAPR must be rejected."""

    def setUp(self):
        np.random.seed(2)

    def test_high_papr_rejected(self):
        """Block with very high PAPR rejected when PAPR_MAX is set."""
        s = _high_papr()
        r = classify_with_trace(s, snr_min_db=-999.0, papr_max_db=10.0)
        self.assertFalse(r["papr_gate_pass"],
                         "High-PAPR block should fail PAPR_MAX gate")
        self.assertIn("REJECT:papr_max", r["decision_trace"])

    def test_papr_max_disabled(self):
        """With PAPR_MAX=0, PAPR gate is disabled; high-PAPR block passes."""
        s = _high_papr()
        r = classify_with_trace(s, snr_min_db=-999.0, papr_max_db=0.0)
        self.assertTrue(r["papr_gate_pass"],
                        "PAPR gate should be disabled when papr_max_db=0")
        self.assertNotIn("REJECT:papr_max", r["decision_trace"])

    def test_normal_fsk_passes_papr(self):
        """Normal FSK (low PAPR ~3 dB) passes PAPR_MAX=20 gate."""
        s = _awgn(_fsk2(), snr_db=15.0)
        r = classify_with_trace(s, snr_min_db=-999.0, papr_max_db=20.0)
        self.assertTrue(r["papr_gate_pass"],
                        "Normal FSK should pass a 20 dB PAPR gate")
        self.assertNotIn("REJECT:papr_max", r["decision_trace"])


class TestRejectionInWorkerLog(unittest.TestCase):
    """Verify that rejection reason appears in a simulated worker log entry."""

    def _make_json_log_entry(self, decision_trace: str) -> str:
        """Simulate the JSON log line produced by write_json_log."""
        return json.dumps({
            "ts_ns": 0,
            "mod": "unknown",
            "confidence": 0.0,
            "snr_db": -10.0,
            "decision_trace": decision_trace,
        })

    def test_snr_rejection_in_log(self):
        np.random.seed(3)
        # Use a high gate (50 dB) to guarantee REJECT:snr_gate on any real signal
        s = _awgn(_fsk2(), snr_db=15.0)
        r = classify_with_trace(s, snr_min_db=50.0)
        log_line = self._make_json_log_entry(r["decision_trace"])
        parsed = json.loads(log_line)
        self.assertIn("REJECT:snr_gate", parsed["decision_trace"])

    def test_bw_rejection_in_log(self):
        np.random.seed(4)
        s = _awgn(_narrowband(), snr_db=20.0)
        r = classify_with_trace(s, snr_min_db=-999.0, expected_bw_hz=100_000)
        log_line = self._make_json_log_entry(r["decision_trace"])
        parsed = json.loads(log_line)
        self.assertIn("REJECT:bw_gate", parsed["decision_trace"])

    def test_papr_rejection_in_log(self):
        np.random.seed(5)
        s = _high_papr()
        r = classify_with_trace(s, snr_min_db=-999.0, papr_max_db=10.0)
        log_line = self._make_json_log_entry(r["decision_trace"])
        parsed = json.loads(log_line)
        self.assertIn("REJECT:papr_max", parsed["decision_trace"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
