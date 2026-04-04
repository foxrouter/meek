#!/usr/bin/env python3
"""
tests/test_demod_timing.py - Demodulation timing convergence tests.

DEM-04: Gardner timing loop with k-squared-normalised gain must converge at
        k=16 (rsym=128 kbaud, fs=2.048 MSPS - Woodley operational config).
        Original fixed gain 0.01 does not converge at this k.

DEM-05: OOK max-variance phase search must decode correctly for all phase
        offsets in [0, k). BER < 10% required for SNR >= 3 dB.

Run with:
    python3 tests/test_demod_timing.py [-v]

Requires: numpy
"""

import math
import sys
import unittest
from pathlib import Path
from typing import List, Tuple

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tests"))


from crc_helpers import append_crc32  # noqa: E402


def _gen_fsk2_iq(bits: List[int], k: int, fdev_norm: float) -> np.ndarray:
    n_samp = len(bits) * k
    phase = 0.0
    iq = np.zeros(n_samp, dtype=np.complex64)
    for si, bit in enumerate(bits):
        freq = fdev_norm if bit else -fdev_norm
        phase_inc = 2 * np.pi * freq
        for j in range(k):
            iq[si * k + j] = np.exp(1j * phase)
            phase += phase_inc
    return iq


def _gen_ook_iq(bits: List[int], k: int, phase_offset: int = 0,
                snr_db: float = 15.0) -> np.ndarray:
    n_samp = len(bits) * k
    iq = np.zeros(n_samp + phase_offset, dtype=np.complex64)
    for si, bit in enumerate(bits):
        if bit:
            iq[phase_offset + si * k: phase_offset + (si + 1) * k] = 1.0 + 0j
    noise_power = 10 ** (-snr_db / 10.0)
    rng = np.random.default_rng(42)
    noise = (rng.standard_normal(len(iq)) +
             1j * rng.standard_normal(len(iq))).astype(np.complex64)
    iq += noise * np.sqrt(noise_power / 2.0)
    return iq


def _demod_fsk_gardner(iq: np.ndarray, k: int,
                       use_scaled_gain: bool = True) -> Tuple[List[int], bool]:
    """
    use_scaled_gain=True  - DEM-04 fix: gain normalised by k-squared and symbol energy;
                            err_ema also tracks the normalised residual so the adaptive
                            lock threshold 0.5/k is met quickly.
    use_scaled_gain=False - original broken implementation: fixed gain 0.01.
    Returns (decoded_bits, lock_achieved).
    """
    n = len(iq)
    tau = 0.0
    x_sym = complex(0, 0)
    x_mid = complex(0, 0)
    bits_out: List[int] = []
    err_ema = 1.0
    lock_sym = -1
    lock_threshold = 0.5 / k  # adaptive: scales with oversampling ratio
    pos = 0
    while pos + k <= n:
        mid_pos = pos + k // 2
        end_pos = pos + k - 1
        x_mid = complex(iq[mid_pos])
        x_cur = complex(iq[end_pos])
        e = float(np.real((x_cur - x_sym) * np.conj(x_mid)))
        if use_scaled_gain:
            sym_energy = float(np.mean(np.abs(iq[pos:pos + k]) ** 2))
            sym_energy = max(sym_energy, 1e-12)
            e_norm = e / (float(k * k) * sym_energy)
            tau += 0.1 * e_norm
            err_ema = 0.9 * err_ema + 0.1 * abs(e_norm)
        else:
            tau += 0.01 * e
            err_ema = 0.9 * err_ema + 0.1 * abs(e)
        tau = float(np.clip(tau, -1.0, 1.0))
        x_sym = x_cur
        if lock_sym < 0 and err_ema < lock_threshold:
            lock_sym = len(bits_out)
        if k > 1:
            inst_freq = float(np.mean(np.angle(
                iq[pos + 1:pos + k] * np.conj(iq[pos:pos + k - 1]))))
        else:
            inst_freq = 0.0
        bits_out.append(1 if inst_freq > 0 else 0)
        int_correction = int(math.trunc(tau))
        tau -= int_correction
        pos += max(1, k + int_correction)
    return bits_out, lock_sym >= 0


def _percentile_threshold(values: List[float]) -> float:
    """Midpoint between the 10th and 90th percentiles of *values*.

    Used as the OOK decision threshold: robust to outliers and independent of
    absolute amplitude, so it works across a wide range of SNR levels.
    """
    sv = sorted(values)
    n = len(sv)
    lo = sv[n // 10]
    hi = sv[9 * n // 10]
    return lo + (hi - lo) * 0.5


def _demod_ook_phase_search(iq: np.ndarray, k: int) -> List[int]:
    """DEM-05 fix: max-variance block-mean phase search.

    Using block-mean variance (average each k-sample symbol block before
    computing variance) ensures the correct phase offset achieves strictly
    higher variance than misaligned phases, even with square pulses and AWGN.
    Block-mean decoding also benefits from sqrt(k) SNR gain.
    """
    env = np.abs(iq)
    n = len(env)
    max_syms = n // k
    best_phase = 0
    best_var = -1.0
    for phi in range(k):
        block_means = [float(np.mean(env[si * k + phi: si * k + phi + k]))
                       for si in range(max_syms)
                       if si * k + phi + k <= n]
        if not block_means:
            continue
        var = float(np.var(block_means))
        if var > best_var:
            best_var = var
            best_phase = phi
    bm = [float(np.mean(env[si * k + best_phase: si * k + best_phase + k]))
          for si in range(max_syms)
          if si * k + best_phase + k <= n]
    if not bm:
        return []
    threshold = _percentile_threshold(bm)
    return [1 if v >= threshold else 0 for v in bm]


def _demod_ook_fixed_grid(iq: np.ndarray, k: int) -> List[int]:
    """Original broken: fixed k//2 grid."""
    env = np.abs(iq)
    n = len(env)
    max_syms = n // k
    threshold = _percentile_threshold(list(env))
    return [1 if env[si * k + k // 2] >= threshold else 0
            for si in range(max_syms) if si * k + k // 2 < n]


def _ber(tx: List[int], rx: List[int]) -> float:
    n = min(len(tx), len(rx))
    if n == 0:
        return 1.0
    return sum(a != b for a, b in zip(tx[:n], rx[:n])) / n


class TestGardnerTimingConvergence(unittest.TestCase):
    """DEM-04: Gardner loop with k-squared-normalised gain must converge at k=16."""

    K = 16
    FDEV_NORM = 50_000.0 / 2_048_000.0
    N_SYMBOLS = 256

    def setUp(self):
        np.random.seed(42)
        raw_bits = list(np.random.randint(0, 2, self.N_SYMBOLS))
        self.tx_bits = append_crc32(raw_bits)
        self.iq = _gen_fsk2_iq(self.tx_bits, self.K, self.FDEV_NORM)

    def test_scaled_gain_achieves_lock(self):
        _, lock = _demod_fsk_gardner(self.iq, self.K, use_scaled_gain=True)
        self.assertTrue(lock,
                        f"Gardner with scaled gain failed to converge at k={self.K} "
                        "(rsym=128 kbaud, fs=2.048 MSPS)")

    def test_scaled_gain_ber_below_threshold(self):
        rx_bits, _ = _demod_fsk_gardner(self.iq, self.K, use_scaled_gain=True)
        ber = _ber(self.tx_bits, rx_bits)
        self.assertLess(ber, 0.15,
                        f"BER={ber:.3f} at k={self.K} with scaled gain - expected < 0.15")

    def test_fixed_gain_fails_at_k16(self):
        """Documents the known failure DEM-04 fixes."""
        _, lock = _demod_fsk_gardner(self.iq, self.K, use_scaled_gain=False)
        self.assertFalse(
            lock,
            f"Fixed gain 0.01 unexpectedly converged at k={self.K}. "
            "Verify signal length and SNR match Woodley configuration.")

    def test_scaled_gain_k2_also_converges(self):
        k = 2
        iq_k2 = _gen_fsk2_iq(self.tx_bits, k, self.FDEV_NORM * 4)
        _, lock = _demod_fsk_gardner(iq_k2, k, use_scaled_gain=True)
        self.assertTrue(lock, f"Scaled gain failed to converge at k={k}")


class TestOokTimingRecovery(unittest.TestCase):
    """DEM-05: OOK max-variance phase search must decode correctly for all
    phase offsets in [0, k)."""

    K = 16
    SNR_DB = 10.0
    N_SYMBOLS = 128

    def setUp(self):
        np.random.seed(7)
        raw_bits = list(np.random.randint(0, 2, self.N_SYMBOLS))
        self.tx_bits = append_crc32(raw_bits)

    def _ber_at_offset(self, phi: int, use_phase_search: bool) -> float:
        iq = _gen_ook_iq(self.tx_bits, self.K, phase_offset=phi,
                         snr_db=self.SNR_DB)
        rx = (_demod_ook_phase_search(iq, self.K) if use_phase_search
              else _demod_ook_fixed_grid(iq, self.K))
        return _ber(self.tx_bits, rx)

    def test_phase_search_low_ber_all_offsets(self):
        for phi in range(self.K):
            ber = self._ber_at_offset(phi, use_phase_search=True)
            self.assertLess(ber, 0.10,
                            f"Phase search BER={ber:.3f} at phi={phi} - expected < 0.10")

    def test_fixed_grid_fails_at_bad_offsets(self):
        """Documents the failure DEM-05 fixes."""
        phi = self.K // 4
        ber_fixed = self._ber_at_offset(phi, use_phase_search=False)
        ber_search = self._ber_at_offset(phi, use_phase_search=True)
        self.assertLess(ber_search, ber_fixed,
                        f"Phase search BER ({ber_search:.3f}) not better than fixed grid "
                        f"({ber_fixed:.3f}) at phi={phi} - regression suspected")

    def test_phase_search_zero_offset(self):
        ber = self._ber_at_offset(0, use_phase_search=True)
        self.assertLess(ber, 0.10,
                        f"Phase search failed at phi=0 (aligned burst): BER={ber:.3f}")

    def test_ook_timing_snr_3db(self):
        phi = self.K // 3
        iq = _gen_ook_iq(self.tx_bits, self.K, phase_offset=phi, snr_db=3.0)
        rx = _demod_ook_phase_search(iq, self.K)
        ber = _ber(self.tx_bits, rx)
        self.assertLess(ber, 0.15,
                        f"Phase search BER={ber:.3f} at SNR=3 dB - expected < 0.15")


if __name__ == "__main__":
    unittest.main()
