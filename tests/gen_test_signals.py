#!/usr/bin/env python3
"""
tests/gen_test_signals.py — Generate RRC-shaped synthetic IQ test vectors.

Generates complex float32 IQ files (.cf32) for multiple modulation types,
bands, and SNR levels to use as classifier test fixtures.

Usage:
    python3 tests/gen_test_signals.py --out-dir /tmp/iq_fixtures
    python3 tests/gen_test_signals.py --out-dir /tmp/iq_fixtures --bands 433 915
    python3 tests/gen_test_signals.py --out-dir /tmp/iq_fixtures --snrs 10 3 0 -3 -6 -10

Requires: numpy
Optional: scipy (for RRC filter design — falls back to rectangular pulse if absent)
"""
import argparse
import itertools
import os
import struct
import sys
from typing import Sequence

import numpy as np

try:
    from scipy.signal import firwin, lfilter
    HAVE_SCIPY = True
except ImportError:
    HAVE_SCIPY = False


# ---------------------------------------------------------------------------
# RRC pulse shape filter (fallback to rectangular if scipy absent)
# ---------------------------------------------------------------------------

def rrc_filter(sps: int, alpha: float = 0.35, num_taps: int = None) -> np.ndarray:
    """Return an RRC FIR filter impulse response."""
    if num_taps is None:
        num_taps = 6 * sps + 1
    t = np.arange(num_taps) - (num_taps - 1) / 2
    t /= sps
    with np.errstate(divide='ignore', invalid='ignore'):
        num = np.sin(np.pi * t * (1 - alpha)) + 4 * alpha * t * np.cos(np.pi * t * (1 + alpha))
        den = np.pi * t * (1 - (4 * alpha * t) ** 2)
        h = np.where(np.abs(den) < 1e-10,
                     np.where(np.abs(t) < 1e-10,
                              1 - alpha + 4 * alpha / np.pi,
                              alpha / np.sqrt(2) * (
                                  (1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha)) +
                                  (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))),
                     num / den)
    h /= np.sum(np.abs(h))
    return h.astype(np.float32)


def pulse_shape(symbols: np.ndarray, sps: int, alpha: float = 0.35) -> np.ndarray:
    """Upsample symbols and apply RRC pulse shaping."""
    up = np.zeros(len(symbols) * sps, dtype=complex)
    up[::sps] = symbols
    if HAVE_SCIPY:
        h = rrc_filter(sps, alpha)
        real = lfilter(h, [1.0], up.real).astype(np.float32)
        imag = lfilter(h, [1.0], up.imag).astype(np.float32)
        return (real + 1j * imag).astype(np.complex64)
    return up.astype(np.complex64)


# ---------------------------------------------------------------------------
# Modulation generators
# ---------------------------------------------------------------------------

def gen_bpsk(n_symbols: int, sps: int) -> np.ndarray:
    bits = np.random.randint(0, 2, n_symbols)
    symbols = (2 * bits - 1).astype(complex)
    return pulse_shape(symbols, sps)


def gen_qpsk(n_symbols: int, sps: int) -> np.ndarray:
    bits = np.random.randint(0, 2, (n_symbols, 2))
    symbols = ((2 * bits[:, 0] - 1) + 1j * (2 * bits[:, 1] - 1)) / np.sqrt(2)
    return pulse_shape(symbols, sps)


def gen_8psk(n_symbols: int, sps: int) -> np.ndarray:
    indices = np.random.randint(0, 8, n_symbols)
    symbols = np.exp(1j * 2 * np.pi * indices / 8)
    return pulse_shape(symbols, sps)


def gen_fsk2(n_symbols: int, sps: int, fdev: float, fs: float) -> np.ndarray:
    """Binary FSK: frequency deviations ±fdev at sample rate fs."""
    bits = np.random.randint(0, 2, n_symbols)
    freqs = (2 * bits - 1) * fdev
    phase_inc = 2 * np.pi * freqs / fs
    phase = np.repeat(phase_inc, sps)
    return np.exp(1j * np.cumsum(phase)).astype(np.complex64)


def gen_fsk4(n_symbols: int, sps: int, fdev: float, fs: float) -> np.ndarray:
    """4-FSK."""
    syms = np.random.randint(0, 4, n_symbols)
    freqs = (syms - 1.5) * fdev
    phase_inc = 2 * np.pi * freqs / fs
    phase = np.repeat(phase_inc, sps)
    return np.exp(1j * np.cumsum(phase)).astype(np.complex64)


def gen_gmsk(n_symbols: int, sps: int, bt: float, fdev: float, fs: float) -> np.ndarray:
    """Approximate GMSK via Gaussian-filtered FSK."""
    bits = np.random.randint(0, 2, n_symbols)
    nrz = (2 * bits - 1).astype(float)
    # Half-span in samples for the Gaussian pulse filter (covers ±3 BT periods)
    filter_half_width_sps = sps * 4
    t = np.arange(-filter_half_width_sps, filter_half_width_sps + 1) / sps
    sigma = np.sqrt(np.log(2)) / (2 * np.pi * bt)
    h = np.exp(-t ** 2 / (2 * sigma ** 2))
    h /= h.sum()
    nrz_up = np.zeros(len(nrz) * sps)
    nrz_up[::sps] = nrz
    phase_inc = np.convolve(nrz_up, h, mode='same') * np.pi * fdev / fs
    return np.exp(1j * np.cumsum(phase_inc)).astype(np.complex64)


def gen_ook(n_symbols: int, sps: int, duty: float = 0.5) -> np.ndarray:
    """OOK: random on/off keying with given duty cycle."""
    bits = (np.random.random(n_symbols) < duty).astype(float)
    return np.repeat(bits, sps).astype(np.complex64)


def gen_cw(n_samples: int, fs: float, freq_offset: float = 1000.0) -> np.ndarray:
    """Continuous wave tone."""
    t = np.arange(n_samples) / fs
    return np.exp(1j * 2 * np.pi * freq_offset * t).astype(np.complex64)


# ---------------------------------------------------------------------------
# AWGN channel
# ---------------------------------------------------------------------------

def add_awgn(signal: np.ndarray, snr_db: float) -> np.ndarray:
    sig_pow = np.mean(np.abs(signal) ** 2)
    if sig_pow < 1e-20:
        sig_pow = 1.0
    noise_pow = sig_pow / 10 ** (snr_db / 10.0)
    noise = np.sqrt(noise_pow / 2) * (
        np.random.randn(len(signal)) + 1j * np.random.randn(len(signal))
    )
    return (signal + noise).astype(np.complex64)


# ---------------------------------------------------------------------------
# Band presets
# ---------------------------------------------------------------------------

BAND_PRESETS = {
    433:  {"center_hz": 433_920_000, "fs": 2_048_000, "rsym": 128_000, "fdev": 50_000},
    868:  {"center_hz": 868_000_000, "fs": 2_048_000, "rsym": 250_000, "fdev": 62_500},
    915:  {"center_hz": 915_000_000, "fs": 2_048_000, "rsym": 250_000, "fdev": 62_500},
    315:  {"center_hz": 315_000_000, "fs": 2_048_000, "rsym": 128_000, "fdev": 50_000},
    137:  {"center_hz": 137_500_000, "fs": 2_048_000, "rsym": 4_160,   "fdev": 17_000},
    150:  {"center_hz": 162_400_000, "fs": 2_048_000, "rsym": 9_600,   "fdev": 4_000},
}


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

def generate(out_dir: str, bands: Sequence[int], snrs: Sequence[float],
             n_symbols: int = 500, seed: int = 42) -> None:
    np.random.seed(seed)
    os.makedirs(out_dir, exist_ok=True)

    for band in bands:
        preset = BAND_PRESETS.get(band)
        if preset is None:
            print(f"[WARN] unknown band {band}, skipping", file=sys.stderr)
            continue
        fs = preset["fs"]
        rsym = preset["rsym"]
        fdev = preset["fdev"]
        sps = max(1, int(fs / rsym))

        mods = {
            "bpsk":  lambda: gen_bpsk(n_symbols, sps),
            "qpsk":  lambda: gen_qpsk(n_symbols, sps),
            "8psk":  lambda: gen_8psk(n_symbols, sps),
            "fsk2":  lambda: gen_fsk2(n_symbols, sps, fdev, fs),
            "fsk4":  lambda: gen_fsk4(n_symbols, sps, fdev, fs),
            "gmsk":  lambda: gen_gmsk(n_symbols, sps, bt=0.3, fdev=fdev, fs=fs),
            "ook":   lambda: gen_ook(n_symbols, sps),
            "cw":    lambda: gen_cw(n_symbols * sps, fs),
        }

        for mod_name, gen_fn in mods.items():
            signal = gen_fn()
            for snr_db in snrs:
                noisy = add_awgn(signal, snr_db)
                snr_tag = f"snr{snr_db:+.0f}dB".replace("+", "p").replace("-", "m")
                fname = f"{band}MHz_{mod_name}_{snr_tag}.cf32"
                fpath = os.path.join(out_dir, fname)
                noisy.tofile(fpath)
                print(f"  wrote {fpath}  samples={len(noisy)}")

    print(f"\nDone. {len(bands)} bands × {len(snrs)} SNRs written to {out_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate synthetic RRC-shaped IQ test vectors (.cf32)"
    )
    parser.add_argument("--out-dir", default="/tmp/iq_fixtures",
                        help="Output directory (default: /tmp/iq_fixtures)")
    parser.add_argument("--bands", nargs="+", type=int,
                        default=list(BAND_PRESETS.keys()),
                        help="Band(s) in MHz to generate (default: all presets)")
    parser.add_argument("--snrs", nargs="+", type=float,
                        default=[10, 3, 0, -3, -6, -10],
                        help="SNR values in dB (default: 10 3 0 -3 -6 -10)")
    parser.add_argument("--n-symbols", type=int, default=500,
                        help="Number of symbols per modulation (default: 500)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility (default: 42)")
    args = parser.parse_args()

    if not HAVE_SCIPY:
        print("[WARN] scipy not found — using rectangular pulse shaping "
              "(install scipy for RRC filtering)", file=sys.stderr)

    generate(out_dir=args.out_dir, bands=args.bands, snrs=args.snrs,
             n_symbols=args.n_symbols, seed=args.seed)


if __name__ == "__main__":
    main()
