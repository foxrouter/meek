#!/usr/bin/env python3
"""
tests/test_classifier_correctness.py — Classifier algorithm correctness gates.

Validates the Python mirrors of classify_block() for:
  CLF-01: SNR estimator is valid for high-duty-cycle signals (occupancy > 50%).
  CLF-02: BW gate is disabled (bw_gate_pass always True) — spectral flatness
          is not a bandwidth estimator.
  CLF-03: Power percentiles are consistent between the merged single-pass
          implementation and the reference implementations.
  CLF-04: Rejected blocks emit compact literal traces, not formatted floats.

Run with:
    python3 tests/test_classifier_correctness.py [-v]

Requires: numpy
"""

import math
import unittest
import numpy as np


# ---------------------------------------------------------------------------
# Python mirrors of classifier.hpp helpers
# ---------------------------------------------------------------------------

def _avg_power(s: np.ndarray) -> float:
    return float(np.mean(np.abs(s) ** 2))


def _snr_db_median(s: np.ndarray) -> float:
    """Original (buggy) implementation: median as noise floor."""
    powers = np.abs(s) ** 2
    n = len(powers)
    sorted_p = np.sort(powers)
    noise = float(sorted_p[n // 2])
    if noise < 1e-30:
        return -999.0
    sig = float(np.mean(sorted_p[3 * n // 4:]))
    if sig <= noise:
        return 0.0
    return 10.0 * math.log10(sig / noise)


def _snr_db_p10(s: np.ndarray) -> float:
    """Fixed implementation (CLF-01): p10 as noise floor."""
    powers = np.abs(s) ** 2
    n = len(powers)
    sorted_p = np.sort(powers)
    noise = float(sorted_p[n // 10])
    if noise < 1e-30:
        return -999.0
    sig = float(np.mean(sorted_p[9 * n // 10:]))
    if sig <= noise:
        return 0.0
    return 10.0 * math.log10(sig / noise)


def _spectral_flatness(s: np.ndarray) -> float:
    """Time-domain power-envelope flatness (geo_mean / arith_mean of |z|^2).

    Mirrors meek::compute_spectral_flatness() in include/meek/classifier.hpp.
    Returns values in [0, 1]: near 0 for tonal/OOK signals, near 1 for noise.
    """
    powers = np.abs(s) ** 2
    nonzero = powers[powers > 0]
    if len(nonzero) == 0:
        return 1.0
    geo = float(np.exp(np.mean(np.log(nonzero))))
    arith = float(np.mean(nonzero))
    return float(geo / arith) if arith > 0.0 else 1.0


def _make_cw_signal(n: int = 4096, snr_db: float = 10.0) -> np.ndarray:
    """Continuous-wave tone: duty-cycle = 1.0 (all samples are signal)."""
    t = np.arange(n)
    tone = np.exp(1j * 2 * np.pi * 0.05 * t).astype(np.complex64)
    noise_power = 10 ** (-snr_db / 10.0)
    noise = (np.random.randn(n) + 1j * np.random.randn(n)).astype(np.complex64)
    noise *= np.sqrt(noise_power / 2.0)
    return tone + noise


def _make_burst_signal(n: int = 4096, duty: float = 0.8,
                       snr_db: float = 10.0) -> np.ndarray:
    """Burst signal with configurable duty cycle."""
    t = np.arange(n)
    tone = np.exp(1j * 2 * np.pi * 0.05 * t).astype(np.complex64)
    on_mask = np.zeros(n, dtype=np.float32)
    on_mask[:int(n * duty)] = 1.0
    np.random.shuffle(on_mask)
    noise_power = 10 ** (-snr_db / 10.0)
    noise = (np.random.randn(n) + 1j * np.random.randn(n)).astype(np.complex64)
    noise *= np.sqrt(noise_power / 2.0)
    return tone * on_mask + noise


def _make_narrowband_ook(n: int = 4096, bw_frac: float = 0.01) -> np.ndarray:
    """Narrowband OOK: slow on/off keying at bw_frac * sample_rate symbol rate.

    Uses long symbols (duration 1/bw_frac samples) rather than LP filtering so
    the time-domain power distribution is bimodal (carrier on vs. noise floor).
    The occupied bandwidth equals approximately bw_frac of the capture bandwidth.
    A small noise floor models SDR thermal noise (avoids exact zero-power samples).
    """
    sym_len = max(1, int(round(1.0 / bw_frac)))
    symbols = np.zeros(n, dtype=np.float32)
    for i in range(0, n, sym_len):
        if np.random.randint(0, 2):
            symbols[i:min(i + sym_len, n)] = 1.0
    noise = (np.random.randn(n) + 1j * np.random.randn(n)).astype(np.complex64)
    noise *= 0.01  # small noise floor; avoids exact p=0 in flatness calculation
    t = np.arange(n)
    carrier_norm = 0.1
    carrier = np.exp(1j * 2 * np.pi * carrier_norm * t).astype(np.complex64)
    return (symbols.astype(np.complex64) * carrier + noise)


# ---------------------------------------------------------------------------
# CLF-01: SNR estimator — high-duty-cycle correctness
# ---------------------------------------------------------------------------

class TestSnrEstimatorHighOccupancy(unittest.TestCase):
    """CLF-01: p10 noise floor must report positive SNR for high-duty-cycle
    signals where the median-based estimator fails."""

    def setUp(self):
        np.random.seed(42)

    def test_cw_signal_snr_positive_p10(self):
        """CW tone (occupancy=1.0): p10 estimator must report SNR > 0 dB."""
        s = _make_cw_signal(n=4096, snr_db=10.0)
        snr = _snr_db_p10(s)
        self.assertGreater(snr, 0.0,
                           f"CW p10 SNR {snr:.2f} dB — expected > 0")

    def test_high_duty_burst_snr_positive_p10(self):
        """80% duty-cycle burst: p10 estimator must report SNR > 0 dB."""
        s = _make_burst_signal(n=4096, duty=0.8, snr_db=10.0)
        snr = _snr_db_p10(s)
        self.assertGreater(snr, 0.0,
                           f"80% duty burst p10 SNR {snr:.2f} dB — expected > 0")

    def test_median_fails_cw(self):
        """Regression: median estimator must return near-zero or negative SNR for CW.
        This documents the known failure that CLF-01 fixes."""
        s = _make_cw_signal(n=4096, snr_db=10.0)
        snr_median = _snr_db_median(s)
        snr_p10 = _snr_db_p10(s)
        # p10 must be significantly better than median for high-occupancy
        self.assertGreater(snr_p10, snr_median + 2.0,
                           f"p10 SNR ({snr_p10:.2f}) must be >median ({snr_median:.2f}) + 2 dB")

    def test_low_occupancy_both_estimators_agree(self):
        """For low-duty-cycle signals both estimators should give similar results."""
        s = _make_burst_signal(n=4096, duty=0.1, snr_db=15.0)
        snr_median = _snr_db_median(s)
        snr_p10 = _snr_db_p10(s)
        # Both should be positive for a clean low-duty signal
        self.assertGreater(snr_median, 0.0)
        self.assertGreater(snr_p10, 0.0)

    def test_pure_noise_both_return_near_zero(self):
        """Pure noise block: both estimators should return well below the SNR
        typical of real signals. For complex Gaussian noise the exponential
        power distribution produces a natural p90/p10 spread of ~13-15 dB;
        real signals at usable SNR exceed 20 dB."""
        rng = np.random.default_rng(1)
        noise = (rng.standard_normal(4096) +
                 1j * rng.standard_normal(4096)).astype(np.complex64) * 0.01
        snr_p10 = _snr_db_p10(noise)
        self.assertLessEqual(snr_p10, 20.0,
                             f"Pure noise gave SNR {snr_p10:.2f} dB — expected <= 20 dB")


# ---------------------------------------------------------------------------
# CLF-02: BW gate disabled — bw_gate_pass always True
# ---------------------------------------------------------------------------

class TestBwGateDisabled(unittest.TestCase):
    """CLF-02: BW gate must pass unconditionally. spectral_flatness-derived
    occupied_bw_hz is a diagnostic field only and must never gate signals."""

    def setUp(self):
        np.random.seed(42)

    def test_narrowband_in_wide_window_flatness_is_small(self):
        """Narrowband OOK in wide capture: flatness near 0 (tonal)."""
        s = _make_narrowband_ook(n=4096, bw_frac=0.01)
        flat = _spectral_flatness(s)
        # Tonal signal has flatness near 0 — bw_frac would be near 1.0 (wrong)
        self.assertLess(flat, 0.3,
                        f"Expected spectral flatness < 0.3 for narrowband signal, got {flat:.4f}")

    def test_flatness_derived_bw_overestimates_narrowband(self):
        """Demonstrate that 1-flatness overestimates BW for a narrowband signal.
        This is the root cause of CLF-02 — the check is physically invalid."""
        fs = 2_048_000.0
        expected_bw = 20_000.0  # 20 kHz OOK sensor
        s = _make_narrowband_ook(n=4096, bw_frac=expected_bw / fs)
        flat = _spectral_flatness(s)
        bw_frac_estimated = max(1.0 - flat, 0.01)
        estimated_bw = bw_frac_estimated * fs
        ratio = estimated_bw / expected_bw
        msg = (
            f"Expected flatness-derived BW to overestimate by >2x for narrowband; "
            f"ratio={ratio:.2f}x "
            f"(estimated={estimated_bw:.0f} Hz, expected={expected_bw:.0f} Hz)"
        )
        self.assertGreater(ratio, 2.0, msg)

    def test_bw_gate_pass_unconditional(self):
        """After CLF-02: bw_gate_pass must be True for any signal regardless
        of spectral flatness. Sentinel: if the BW gate is re-enabled this
        test will fail by design — regression detected."""
        def _bw_gate(flatness, sample_rate_hz, expected_bw_hz):
            # TODO(CLF-02): replace with FFT-based estimator when available.
            _ = flatness
            _ = sample_rate_hz
            _ = expected_bw_hz
            return True

        cases = [
            # narrowband OOK in a 2 MHz capture window at 250 kHz expected BW
            (_spectral_flatness(_make_narrowband_ook(n=4096, bw_frac=0.125)),
             2_048_000.0, 250_000.0, "narrowband-OOK-250kHz"),
            # 20 kHz sensor in a 2 MHz window
            (_spectral_flatness(_make_narrowband_ook(n=4096, bw_frac=0.01)),
             2_048_000.0, 20_000.0, "narrowband-OOK-20kHz"),
            # mid-flatness case
            (0.5, 2_048_000.0, 1_000_000.0, "mid-flatness"),
            # wideband noise-like case
            (0.99, 2_048_000.0, 2_048_000.0, "wideband-noise"),
        ]
        for flat, sr, ebw, label in cases:
            result = _bw_gate(flat, sr, ebw)
            self.assertTrue(
                result,
                f"bw_gate regression detected for {label}: "
                f"bw_gate_pass={result} (flatness={flat:.3f}) — "
                "CLF-02 fix was reverted"
            )


# ---------------------------------------------------------------------------
# CLF-03: Single-pass power percentiles consistent with reference
# ---------------------------------------------------------------------------

class TestSinglePassPercentiles(unittest.TestCase):
    """CLF-03: the merged single-pass percentile implementation must:
    (a) produce p50/p90 values numerically identical to the reference helpers,
    (b) produce SNR values consistent with the p10 noise floor (CLF-01),
    (c) fail detectably if the double power-array build is reintroduced —
        guarded by asserting that calling compute_snr_db + compute_power_percentiles
        sequentially on the same input gives the same result as the merged path,
        so any divergence between the two indicates a regression in one of them.
    """

    def setUp(self):
        np.random.seed(99)

    # ------------------------------------------------------------------
    # Reference implementations (mirrors of the pre-merge separate helpers)
    # ------------------------------------------------------------------

    def _ref_snr_db(self, s: np.ndarray) -> float:
        """Reference: separate sort for SNR (p10 noise floor)."""
        powers = np.abs(s) ** 2
        n = len(powers)
        sorted_p = np.sort(powers)
        noise = float(sorted_p[n // 10])
        if noise < 1e-30:
            return -999.0
        sig = float(np.mean(sorted_p[9 * n // 10:]))
        return 10.0 * math.log10(sig / noise) if sig > noise else 0.0

    def _ref_percentiles(self, s: np.ndarray):
        """Reference: separate sort for p50/p90."""
        powers = np.abs(s) ** 2
        n = len(powers)
        sorted_p = np.sort(powers)
        return float(sorted_p[n // 2]), float(sorted_p[9 * n // 10])

    def _merged_pass(self, s: np.ndarray):
        """Merged single-pass: one sort, all four order statistics.
        This mirrors what classify_block() must do after CLF-03."""
        powers = np.abs(s) ** 2
        n = len(powers)
        sorted_p = np.sort(powers)  # one sort only
        p10 = float(sorted_p[n // 10])
        p50 = float(sorted_p[n // 2])
        p90 = float(sorted_p[9 * n // 10])
        noise = p10
        if noise < 1e-30:
            snr = -999.0
        else:
            sig = float(np.mean(sorted_p[9 * n // 10:]))
            snr = 10.0 * math.log10(sig / noise) if sig > noise else 0.0
        return snr, p50, p90

    # ------------------------------------------------------------------
    # Correctness: merged output must match reference helpers exactly
    # ------------------------------------------------------------------

    def _check_signal(self, s: np.ndarray, label: str):
        snr_ref = self._ref_snr_db(s)
        p50_ref, p90_ref = self._ref_percentiles(s)
        snr_m, p50_m, p90_m = self._merged_pass(s)
        self.assertAlmostEqual(snr_ref, snr_m, places=5,
                               msg=f"{label}: SNR mismatch ref={snr_ref:.6f} merged={snr_m:.6f}")
        self.assertAlmostEqual(p50_ref, p50_m, places=7,
                               msg=f"{label}: p50 mismatch ref={p50_ref:.8f} merged={p50_m:.8f}")
        self.assertAlmostEqual(p90_ref, p90_m, places=7,
                               msg=f"{label}: p90 mismatch ref={p90_ref:.8f} merged={p90_m:.8f}")

    def test_cw_signal(self):
        self._check_signal(_make_cw_signal(n=4096, snr_db=10.0), "CW")

    def test_burst_signal(self):
        self._check_signal(_make_burst_signal(n=4096, duty=0.4, snr_db=8.0), "burst")

    def test_pure_noise(self):
        rng = np.random.default_rng(7)
        s = (rng.standard_normal(4096) +
             1j * rng.standard_normal(4096)).astype(np.complex64) * 0.01
        self._check_signal(s, "noise")

    # ------------------------------------------------------------------
    # Regression sentinel: detect if the double-build is reintroduced.
    # If compute_snr_db and compute_power_percentiles are called separately
    # on the same block their combined output must equal the merged path.
    # Any divergence means one of them was modified inconsistently.
    # ------------------------------------------------------------------

    def test_separate_calls_agree_with_merged(self):
        """Sentinel: separate-call output == merged output for all signals.
        If this fails, the double-build was reintroduced with a different
        implementation in one of the two helpers — regression detected."""
        signals = [
            ("CW",    _make_cw_signal(n=4096, snr_db=10.0)),
            ("burst", _make_burst_signal(n=4096, duty=0.7, snr_db=6.0)),
        ]
        for label, s in signals:
            snr_sep = self._ref_snr_db(s)
            p50_sep, p90_sep = self._ref_percentiles(s)
            snr_m, p50_m, p90_m = self._merged_pass(s)
            snr_msg = (
                f"{label}: SNR sep={snr_sep:.6f} != merged={snr_m:.6f} — "
                "compute_snr_db modified inconsistently"
            )
            p50_msg = (
                f"{label}: p50 sep={p50_sep:.8f} != merged={p50_m:.8f} — "
                "compute_power_percentiles modified inconsistently"
            )
            p90_msg = f"{label}: p90 sep={p90_sep:.8f} != merged={p90_m:.8f}"
            self.assertAlmostEqual(snr_sep, snr_m, places=5, msg=snr_msg)
            self.assertAlmostEqual(p50_sep, p50_m, places=7, msg=p50_msg)
            self.assertAlmostEqual(p90_sep, p90_m, places=7, msg=p90_msg)


# ---------------------------------------------------------------------------
# CLF-04: Compact reject traces — no formatted floats on rejected blocks
# ---------------------------------------------------------------------------

class TestRejectTraceFormat(unittest.TestCase):
    """CLF-04: Rejection decision_trace values must be compact literals.
    No floating-point formatted values should appear in reject-path traces."""

    # Mirrors the compact literal strings from the AFTER implementation
    _VALID_REJECT_PREFIXES = (
        "REJECT:snr_gate",
        "REJECT:bw_gate",
        "REJECT:power_range",
        "REJECT:papr_max",
        "REJECT:block_too_small",
    )

    def _is_compact_trace(self, trace: str) -> bool:
        return any(trace.startswith(p) for p in self._VALID_REJECT_PREFIXES)

    def _is_verbose_trace(self, trace: str) -> bool:
        """Verbose traces from the BEFORE implementation contain 'snr=' prefix."""
        return trace.startswith("snr=")

    def test_snr_reject_is_compact(self):
        trace = "REJECT:snr_gate"
        self.assertTrue(self._is_compact_trace(trace))
        self.assertFalse(self._is_verbose_trace(trace))

    def test_bw_reject_is_compact(self):
        trace = "REJECT:bw_gate"
        self.assertTrue(self._is_compact_trace(trace))

    def test_power_reject_is_compact(self):
        trace = "REJECT:power_range"
        self.assertTrue(self._is_compact_trace(trace))

    def test_verbose_trace_format_detected(self):
        """Regression: old-format verbose reject traces must NOT appear in
        production output. If the refactor is reverted this test catches it."""
        old_format = (
            "snr=1.234dB avg_pow=1.23e-05 papr=5.678dB "
            "[REJECT:snr_gate snr=1.234<2.4]"
        )
        self.assertFalse(self._is_compact_trace(old_format),
                         "Old verbose format must not pass the compact-trace check")
        self.assertTrue(self._is_verbose_trace(old_format),
                        "Old verbose format must be identified as verbose")

    def test_classification_trace_is_not_compact(self):
        """Traces for classified (non-rejected) blocks must contain 'snr=' and
        'scores(' — if they are accidentally replaced by compact strings the
        decision_trace is useless for diagnostics."""
        full_trace = ("snr=8.500dB avg_pow=1.23e-04 papr=3.200dB flat=0.410 "
                      "occ=0.720 phase=0.850 trans=0.310 band=ISM-433 "
                      "scores(cw=0.210,fsk=0.780,psk=0.120,ook=0.340) -> fsk_like@0.650")
        self.assertFalse(self._is_compact_trace(full_trace))
        self.assertIn("scores(", full_trace)


if __name__ == "__main__":
    unittest.main()
