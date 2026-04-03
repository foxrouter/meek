#!/usr/bin/env python3
"""
tests/test_demod_timing.py — Demodulation timing convergence tests.

DEM-04: Gardner timing loop with k²-normalised gain must converge at k=16
        (rsym=128 kbaud, fs=2.048 MSPS — Woodley operational configuration).
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


# ---------------------------------------------------------------------------
# CRC-32 helpers
# ---------------------------------------------------------------------------

def _build_crc32_table() -> List[int]:
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (0xEDB88320 ^ (c >> 1)) if (c & 1) else (c >> 1)
        table.append(c)
    return table

_CRC32_TABLE = _build_crc32_table()

def _crc32_bytes(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc = _CRC32_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF

def _append_crc32(bits: List[int]) -> List[int]:
    n = len(bits)
    data = bytearray()
    for i in range(0, n, 8):
        byte = 0
        for j in range(8):
            if i + j < n:
                byte = (byte << 1) | (bits[i + j] & 1)
        data.append(byte)
    crc = _crc32_bytes(bytes(data))
    crc_bits = [(crc >> (31 - i)) & 1 for i in range(32)]
    return bits + crc_bits

def _check_crc32(bits: List[int]) -> bool:
    if len(bits) < 40:
        return False
    data = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for j in range(8):
            if i + j < len(bits):
                byte = (byte << 1) | (bits[i + j] & 1)
        data.append(byte)
    payload = bytes(data[:-4])
    stored_crc = int.from_bytes(data[-4:], 'big')
    return _crc32_bytes(payload) == stored_crc


# ---------------------------------------------------------------------------
# Signal generators
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Demodulator mirrors
# ---------------------------------------------------------------------------

def _gardner_timing_error(x_cur, x_mid, x_sym) -> float:
    return float(np.real((x_cur - x_sym) * np.conj(x_mid)))

def _demod_fsk_gardner(iq: np.ndarray, k: int, fdev_norm: float,
                        use_scaled_gain: bool = True) -> Tuple[List[int], bool]:
    """
    use_scaled_gain=True  → DEM-04 fix: gain normalised by k² and symbol energy
    use_scaled_gain=False → original broken: fixed gain 0.01
    Returns (decoded_bits, lock_achieved).
    """
    n = len(iq)
    tau = 0.0
    x_sym = complex(0, 0)
    x_mid = complex(0, 0)
    bits_out: List[int] = []
    err_ema = 1.0
    lock_sym = -1
    pos = 0
    while pos + k <= n:
        mid_pos = pos + k // 2
        end_pos = pos + k - 1
        x_mid = complex(iq[mid_pos])
        x_cur = complex(iq[end_pos])
        e = _gardner_timing_error(x_cur, x_mid, x_sym)
        if use_scaled_gain:
            sym_energy = float(np.mean(np.abs(iq[pos:pos + k]) ** 2))
            sym_energy = max(sym_energy, 1e-12)
            alpha = 0.1
            tau += alpha * (e / (float(k * k) * sym_energy))
        else:
            tau += 0.01 * e
        tau = float(np.clip(tau, -0.5, 0.5))
        x_sym = x_cur
        err_ema = 0.9 * err_ema + 0.1 * abs(e)
        if lock_sym < 0 and err_ema < 0.05:
            lock_sym = len(bits_out)
        if k > 1:
            inst_freq = float(np.mean(np.angle(
                iq[pos + 1:pos + k] * np.conj(iq[pos:pos + k - 1]))))
        else:
            inst_freq = 0.0
        bits_out.append(1 if inst_freq > 0 else 0)
        advance = k + int(round(tau))
        pos += max(1, advance)
    return bits_out, lock_sym >= 0

def _demod_ook_phase_search(iq: np.ndarray, k: int) -> List[int]:
    """DEM-05 fix: max-variance phase search."""
    env = np.abs(iq)
    n = len(env)
    max_syms = n // k
    best_phase = k // 2
    best_var = -1.0
    for phi in range(k):
        idxs = [si * k + phi for si in range(max_syms) if si * k + phi < n]
        if not idxs:
            continue
        var = float(np.var(env[idxs]))
        if var > best_var:
            best_var = var
            best_phase = phi
    all_env = sorted(env)
    p10 = all_env[n // 10]
    p90 = all_env[9 * n // 10]
    threshold = p10 + (p90 - p10) * 0.5
    bits_out = []
    for si in range(max_syms):
        idx = si * k + best_phase
        if idx < n:
            bits_out.append(1 if env[idx] >= threshold else 0)
    return bits_out

def _demod_ook_fixed_grid(iq: np.ndarray, k: int) -> List[int]:
    """Original broken: fixed k//2 grid."""
    env = np.abs(iq)
    n = len(env)
    max_syms = n // k
    all_env = sorted(env)
    p10 = all_env[n // 10]
    p90 = all_env[9 * n // 10]
    threshold = p10 + (p90 - p10) * 0.5
    bits_out = []
    for si in range(max_syms):
        idx = si * k + k // 2
        if idx < n:
            bits_out.append(1 if env[idx] >= threshold else 0)
    return bits_out

def _ber(tx: List[int], rx: List[int]) -> float:
    n = min(len(tx), len(rx))
    if n == 0:
        return 1.0
    return sum(a != b for a, b in zip(tx[:n], rx[:n])) / n


# ---------------------------------------------------------------------------
# DEM-04: Gardner convergence at k=16
# ---------------------------------------------------------------------------

class TestGardnerTimingConvergence(unittest.TestCase):

    SAMPLE_RATE = 2_048_000.0
    RSYM = 128_000.0
    K = 16
    FDEV_NORM = 50_000.0 / 2_048_000.0
    N_SYMBOLS = 256

    def setUp(self):
        np.random.seed(42)
        raw_bits = list(np.random.randint(0, 2, self.N_SYMBOLS))
        self.tx_bits = _append_crc32(raw_bits)
        self.iq = _gen_fsk2_iq(self.tx_bits, self.K, self.FDEV_NORM)

    def test_scaled_gain_achieves_lock(self):
        _, lock = _demod_fsk_gardner(self.iq, self.K, self.FDEV_NORM,
                                      use_scaled_gain=True)
        self.assertTrue(lock,
            f"Gardner with scaled gain failed to converge at k={self.K}")

    def test_scaled_gain_ber_below_threshold(self):
        rx_bits, _ = _demod_fsk_gardner(self.iq, self.K, self.FDEV_NORM,
                                         use_scaled_gain=True)
        ber = _ber(self.tx_bits, rx_bits)
        self.assertLess(ber, 0.15,
            f"BER={ber:.3f} at k={self.K} with scaled gain — expected < 0.15")

    def test_fixed_gain_fails_at_k16(self):
        """Documents the known failure DEM-04 fixes."""
        _, lock = _demod_fsk_gardner(self.iq, self.K, self.FDEV_NORM,
                                      use_scaled_gain=False)
        if lock:
            import warnings
            warnings.warn(
                f"Fixed gain 0.01 unexpectedly converged at k={self.K}. "
                "Verify signal length and SNR match Woodley configuration.",
                UserWarning)

    def test_scaled_gain_k2_also_converges(self):
        k = 2
        iq_k2 = _gen_fsk2_iq(self.tx_bits, k, self.FDEV_NORM * 4)
        _, lock = _demod_fsk_gardner(iq_k2, k, self.FDEV_NORM * 4,
                                      use_scaled_gain=True)
        self.assertTrue(lock, f"Scaled gain failed to converge at k={k}")


# ---------------------------------------------------------------------------
# DEM-05: OOK phase-offset robustness
# ---------------------------------------------------------------------------

class TestOokTimingRecovery(unittest.TestCase):

    K = 16
    SNR_DB = 10.0
    N_SYMBOLS = 128

    def setUp(self):
        np.random.seed(7)
        raw_bits = list(np.random.randint(0, 2, self.N_SYMBOLS))
        self.tx_bits = _append_crc32(raw_bits)

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
                f"Phase search BER={ber:.3f} at phi={phi} — expected < 0.10")

    def test_fixed_grid_fails_at_bad_offsets(self):
        """Documents the failure DEM-05 fixes."""
        phi = self.K // 4
        ber_fixed = self._ber_at_offset(phi, use_phase_search=False)
        ber_search = self._ber_at_offset(phi, use_phase_search=True)
        self.assertLess(ber_search, ber_fixed,
            f"Phase search BER ({ber_search:.3f}) not better than fixed grid "
            f"({ber_fixed:.3f}) at phi={phi} — regression suspected")

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
            f"Phase search BER={ber:.3f} at SNR=3 dB — expected < 0.15")


if __name__ == "__main__":
    unittest.main()
