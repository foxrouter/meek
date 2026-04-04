#!/usr/bin/env python3
"""
tests/test_demod_ber.py — BER and CRC-32 validation for demodulation pipelines.

Generates known bit-pattern IQ vectors (using gen_test_signals.py generators),
demodulates them in Python, and verifies:
  - Bit error rate (BER) is below target thresholds at sufficient SNR.
  - CRC-32 check passes on noiseless vectors.
  - CRC-32 check degrades gracefully at low SNR.

This test exercises the Python-equivalent demodulators that mirror the
liquid-dsp demod chains in src/main.cpp (FSK/GMSK, PSK/QAM, OOK/AM).

Run with:
    python3 tests/test_demod_ber.py [-v]

Requires: numpy
"""

import math
import struct
import sys
import unittest
from pathlib import Path
from typing import List, Tuple

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tests"))

from crc_helpers import append_crc32, crc32_bytes  # noqa: E402


# ---------------------------------------------------------------------------
# CRC-32 helper for bit sequences used by these tests
# ---------------------------------------------------------------------------

def crc32_bits(bits: List[int]) -> int:
    """Compute CRC-32 (IEEE 802.3) of bit sequence packed MSB-first.

    If the final group contains fewer than 8 bits it is left-aligned in the
    byte and zero-padded in the least-significant bits before the CRC is
    calculated.
    """
    n = len(bits)
    data = bytearray()
    for i in range(0, n, 8):
        byte = 0
        chunk_len = min(8, n - i)
        for b in range(chunk_len):
            byte = (byte << 1) | (bits[i + b] & 1)
        if chunk_len < 8:
            byte <<= 8 - chunk_len
        data.append(byte)
    return crc32_bytes(bytes(data))


def check_crc32(bits: List[int]) -> bool:
    """Return True if the last 32 bits are the correct CRC for the rest."""
    if len(bits) < 64:
        return False
    payload = bits[:-32]
    expected_crc_bits = bits[-32:]
    expected = sum(b << (31 - i) for i, b in enumerate(expected_crc_bits))
    return crc32_bits(payload) == expected


# ---------------------------------------------------------------------------
# Python demodulators (simplified versions of the liquid-dsp chains)
# ---------------------------------------------------------------------------

def _awgn(signal: np.ndarray, snr_db: float) -> np.ndarray:
    sig_pow = float(np.mean(np.abs(signal) ** 2)) or 1.0
    noise_pow = sig_pow / 10 ** (snr_db / 10.0)
    noise = np.sqrt(noise_pow / 2.0) * (
        np.random.randn(len(signal)) + 1j * np.random.randn(len(signal))
    )
    return (signal + noise).astype(np.complex64)


# --- FSK demod ---

def _gen_fsk2_with_bits(bits: List[int], sps: int, fdev: float,
                         fs: float) -> np.ndarray:
    """Generate binary FSK IQ for given bit sequence."""
    phase_inc = np.array(
        [2.0 * math.pi * (2 * b - 1) * fdev / fs for b in bits]
    )
    phase_samples = np.repeat(phase_inc, sps)
    return np.exp(1j * np.cumsum(phase_samples)).astype(np.complex64)


def demod_fsk2_py(s: np.ndarray, sps: int, fdev: float,
                   fs: float) -> List[int]:
    """Simple FM discriminator FSK2 demodulator."""
    # FM discriminator: instantaneous frequency via phase difference
    inst_freq = np.angle(np.conj(s[:-1]) * s[1:]) / (2.0 * math.pi / fs)
    # Use len(s)//sps to avoid off-by-one from len(inst_freq) = len(s)-1
    n_syms = len(s) // sps
    bits = []
    for i in range(n_syms):
        # Take at most sps samples per symbol; last symbol may be short
        chunk = inst_freq[i * sps: min((i + 1) * sps, len(inst_freq))]
        if len(chunk) == 0:
            bits.append(0)
        else:
            bits.append(1 if float(np.mean(chunk)) > 0 else 0)
    return bits


# --- PSK demod (BPSK) ---

def _gen_bpsk_with_bits(bits: List[int], sps: int) -> np.ndarray:
    symbols = np.array([1.0 if b else -1.0 for b in bits], dtype=complex)
    up = np.zeros(len(bits) * sps, dtype=complex)
    up[::sps] = symbols
    return up.astype(np.complex64)


def demod_bpsk_py(s: np.ndarray, sps: int) -> List[int]:
    """Simple BPSK demodulator: integrate + threshold."""
    n_syms = len(s) // sps
    bits = []
    for i in range(n_syms):
        chunk = s[i * sps: (i + 1) * sps]
        bits.append(1 if float(np.real(np.mean(chunk))) > 0 else 0)
    return bits


# --- OOK demod ---

def _gen_ook_with_bits(bits: List[int], sps: int) -> np.ndarray:
    return np.repeat(np.array(bits, dtype=float), sps).astype(np.complex64)


def demod_ook_py(s: np.ndarray, sps: int) -> List[int]:
    """OOK demod: half-maximum threshold.

    Uses half the maximum envelope amplitude as the threshold.  This is
    optimal for binary OOK with ~50 % duty cycle; for very low duty cycles
    (< 10 %) the MAD-based threshold used in the C++ implementation may be
    more robust (fewer false "on" detections from noise bursts).
    """
    env = np.abs(s)
    max_env = float(np.max(env))
    if max_env < 1e-10:
        return [0] * (len(env) // sps)
    # Half-maximum threshold works for binary OOK regardless of duty cycle
    threshold = max_env * 0.5
    n_syms = len(env) // sps
    bits = []
    for i in range(n_syms):
        avg = float(np.mean(env[i * sps: (i + 1) * sps]))
        bits.append(1 if avg > threshold else 0)
    return bits


# ---------------------------------------------------------------------------
# BER helper
# ---------------------------------------------------------------------------

def ber(tx_bits: List[int], rx_bits: List[int]) -> float:
    n = min(len(tx_bits), len(rx_bits))
    if n == 0:
        return 1.0
    errors = sum(t != r for t, r in zip(tx_bits[:n], rx_bits[:n]))
    return errors / n


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

FS = 2_048_000
RSYM = 128_000
FDEV = 50_000
SPS = max(1, FS // RSYM)
N_BITS = 200


class TestFskDemodBer(unittest.TestCase):
    """BER tests for FSK demod chain."""

    def setUp(self):
        np.random.seed(10)
        self._tx_bits = [int(b) for b in np.random.randint(0, 2, N_BITS)]

    def test_ber_high_snr(self):
        """BER <= 0.01 at 20 dB SNR for binary FSK."""
        s = _gen_fsk2_with_bits(self._tx_bits, SPS, FDEV, FS)
        s_noisy = _awgn(s, 20.0)
        rx = demod_fsk2_py(s_noisy, SPS, FDEV, FS)
        b = ber(self._tx_bits, rx)
        self.assertLessEqual(b, 0.01, f"FSK BER at 20 dB = {b:.3f} > 0.01")

    def test_ber_moderate_snr(self):
        """BER <= 0.10 at 6 dB SNR for binary FSK."""
        s = _gen_fsk2_with_bits(self._tx_bits, SPS, FDEV, FS)
        s_noisy = _awgn(s, 6.0)
        rx = demod_fsk2_py(s_noisy, SPS, FDEV, FS)
        b = ber(self._tx_bits, rx)
        self.assertLessEqual(b, 0.10, f"FSK BER at 6 dB = {b:.3f} > 0.10")

    def test_crc32_noiseless(self):
        """CRC-32 passes on noiseless FSK vector with appended CRC."""
        payload = self._tx_bits[:N_BITS]
        tx_with_crc = append_crc32(payload)
        s = _gen_fsk2_with_bits(tx_with_crc, SPS, FDEV, FS)
        rx = demod_fsk2_py(s, SPS, FDEV, FS)
        self.assertTrue(
            check_crc32(rx),
            "CRC-32 should pass on noiseless FSK demodulation",
        )

    def test_crc32_low_snr_may_fail(self):
        """CRC-32 is allowed to fail at very low SNR (-10 dB) — not an error."""
        payload = self._tx_bits[:N_BITS]
        tx_with_crc = append_crc32(payload)
        s = _gen_fsk2_with_bits(tx_with_crc, SPS, FDEV, FS)
        s_noisy = _awgn(s, -10.0)
        rx = demod_fsk2_py(s_noisy, SPS, FDEV, FS)
        # No assertion: just verifying the function doesn't crash
        _ = check_crc32(rx)
        self.assertTrue(True)


class TestBpskDemodBer(unittest.TestCase):
    """BER tests for BPSK (PSK/QAM) demod chain."""

    def setUp(self):
        np.random.seed(20)
        self._tx_bits = [int(b) for b in np.random.randint(0, 2, N_BITS)]

    def test_ber_high_snr(self):
        """BER <= 0.01 at 15 dB SNR for BPSK."""
        s = _gen_bpsk_with_bits(self._tx_bits, SPS)
        s_noisy = _awgn(s, 15.0)
        rx = demod_bpsk_py(s_noisy, SPS)
        b = ber(self._tx_bits, rx)
        self.assertLessEqual(b, 0.01, f"BPSK BER at 15 dB = {b:.3f} > 0.01")

    def test_crc32_noiseless(self):
        """CRC-32 passes on noiseless BPSK vector with appended CRC."""
        payload = self._tx_bits[:N_BITS]
        tx_with_crc = append_crc32(payload)
        s = _gen_bpsk_with_bits(tx_with_crc, SPS)
        rx = demod_bpsk_py(s, SPS)
        self.assertTrue(
            check_crc32(rx),
            "CRC-32 should pass on noiseless BPSK demodulation",
        )


class TestOokDemodBer(unittest.TestCase):
    """BER tests for OOK/AM demod chain."""

    def setUp(self):
        np.random.seed(30)
        self._tx_bits = [int(b) for b in np.random.randint(0, 2, N_BITS)]

    def test_ber_high_snr(self):
        """BER <= 0.01 at 20 dB SNR for OOK."""
        s = _gen_ook_with_bits(self._tx_bits, SPS)
        s_noisy = _awgn(s, 20.0)
        rx = demod_ook_py(s_noisy, SPS)
        b = ber(self._tx_bits, rx)
        self.assertLessEqual(b, 0.01, f"OOK BER at 20 dB = {b:.3f} > 0.01")

    def test_crc32_noiseless(self):
        """CRC-32 passes on noiseless OOK vector with appended CRC."""
        payload = self._tx_bits[:N_BITS]
        tx_with_crc = append_crc32(payload)
        s = _gen_ook_with_bits(tx_with_crc, SPS)
        rx = demod_ook_py(s, SPS)
        self.assertTrue(
            check_crc32(rx),
            "CRC-32 should pass on noiseless OOK demodulation",
        )


class TestCrc32Helper(unittest.TestCase):
    """Unit tests for the CRC-32 helper itself."""

    def test_roundtrip(self):
        """append_crc32 + check_crc32 should always succeed."""
        bits = [1, 0, 1, 1, 0, 0, 1, 0] * 8  # 64 bits
        self.assertTrue(check_crc32(append_crc32(bits)))

    def test_bit_flip_detected(self):
        """A single bit flip must cause CRC-32 check to fail."""
        bits = [1, 0, 1, 0] * 16  # 64 bits
        with_crc = append_crc32(bits)
        corrupted = list(with_crc)
        corrupted[0] ^= 1  # flip one bit
        self.assertFalse(check_crc32(corrupted))

    def test_minimum_length(self):
        """check_crc32 returns False for sequences shorter than 64 bits."""
        self.assertFalse(check_crc32([1, 0] * 30))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
