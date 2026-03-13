#!/usr/bin/env python3
"""
tools/autotune_thresholds.py — Automatically tune rf_adapt_intel thresholds.

Analyses IQ snapshot files (.cf32) from a snapshot directory (or generates
synthetic reference signals when no snapshots are available) to compute
data-driven recommendations for every threshold in thresholds.env.

Recommended thresholds:
  RF_MIN_POWER      — 10th-percentile observed power (keeps weak-but-real signals)
  RF_CONF_THRESHOLD — 20th-percentile classifier confidence
  RF_CONSOLE_CONF   — 80th-percentile classifier confidence (high-quality detects)
  RF_SNAPSHOT_CONF  — same as RF_CONF_THRESHOLD
  RF_SNR_MIN_DB     — 10th-percentile observed SNR (rejects noise floor)
  RF_EXPECTED_BW_HZ — median observed bandwidth, rounded to nearest 1 kHz
                      (0 = disabled when estimate is unreliable)

Usage:
    # Print recommendations (does not modify any file):
    python3 tools/autotune_thresholds.py

    # Write recommendations to /etc/rf_worker/thresholds.env:
    sudo python3 tools/autotune_thresholds.py --write

    # Use a custom snapshot directory:
    python3 tools/autotune_thresholds.py --snapshot-dir /path/to/snapshots

    # Use a custom config file path:
    python3 tools/autotune_thresholds.py --write --conf /etc/rf_worker/thresholds.env

    # Dry-run (show what would be written without writing):
    python3 tools/autotune_thresholds.py --write --dry-run

    # Save recommendations to a file instead of stdout:
    python3 tools/autotune_thresholds.py --out /tmp/tuned.env

Requires: numpy
"""

import argparse
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

_DEFAULT_SNAPSHOT_DIR = "/var/lib/rf-adapt-intel/snapshots"
_DEFAULT_CONF_FILE = "/etc/rf_worker/thresholds.env"
_SAMPLE_RATE = 2_048_000.0  # default sample rate used by the worker


# ---------------------------------------------------------------------------
# Signal metrics — mirrors C++ classify_block heuristics in src/main.cpp
# ---------------------------------------------------------------------------

def avg_power(s: np.ndarray) -> float:
    """Mean instantaneous power of a complex IQ block."""
    return float(np.mean(np.abs(s) ** 2))


def snr_db(s: np.ndarray) -> float:
    """Estimate SNR in dB using a median-based noise floor estimator."""
    powers = np.sort(np.abs(s) ** 2)
    n = len(powers)
    noise = float(powers[n // 2])
    if noise < 1e-30:
        return -999.0
    sig = float(np.mean(powers[3 * n // 4:]))
    if sig <= noise:
        return 0.0
    return 10.0 * math.log10(sig / noise)


def spectral_flatness(s: np.ndarray) -> float:
    """Temporal power-envelope flatness: geometric_mean(power) / arithmetic_mean(power).

    Operates on instantaneous (time-domain) sample powers, not the frequency
    spectrum.  A signal with a nearly-constant envelope (e.g. FSK at high SNR)
    scores close to 1.0; a signal with high power variance (e.g. OOK, or a
    strong tone plus AWGN) scores well below 1.0.  White AWGN has power
    samples that follow an exponential distribution whose geometric/arithmetic
    mean ratio is e^{-γ} ≈ 0.56 (γ = Euler-Mascheroni constant).

    Used here to estimate occupied bandwidth fraction:
        est_bw_frac ≈ 1 − flatness
    """
    pw = np.abs(s) ** 2
    pw = pw[pw > 0]
    if len(pw) == 0:
        return 1.0
    geo = float(np.exp(np.mean(np.log(pw))))
    arith = float(np.mean(pw))
    return geo / arith if arith > 0 else 1.0


def estimate_bandwidth_hz(s: np.ndarray, sample_rate: float = _SAMPLE_RATE) -> float:
    """Estimate occupied bandwidth in Hz from spectral flatness."""
    flat = spectral_flatness(s)
    est_bw_frac = max(0.01, min(1.0, 1.0 - flat))
    return est_bw_frac * sample_rate


def _margin_confidence(scores: list) -> float:
    """Return margin-normalized confidence matching the C++ classify_block formula.

    margin = (winner - runner_up) / (winner + ε), clamped to [0, 1].
    """
    if len(scores) < 2:
        return 0.0
    sorted_scores = sorted(scores)
    best = sorted_scores[-1]
    runner_up = sorted_scores[-2]
    margin = (best - runner_up) / (best + 1e-9)
    return max(0.0, min(1.0, margin))


def heuristic_confidence(s: np.ndarray) -> float:
    """
    Compute a classifier confidence score in [0, 1] mirroring the C++
    heuristic in classify_block.  Returns the margin-normalized confidence:
    (winner - runner_up) / (winner + ε), matching the C++ formula.
    """
    if len(s) < 32:
        return 0.0

    pwr = avg_power(s)
    if pwr < 1e-30:
        return 0.0

    papr = 10.0 * math.log10(float(np.max(np.abs(s) ** 2)) / pwr)
    flat = spectral_flatness(s)

    powers = np.sort(np.abs(s) ** 2)
    n = len(powers)
    occ = float(np.sum(np.abs(s) ** 2 > float(np.median(np.abs(s) ** 2)))) / n

    if len(s) >= 2:
        diff = np.angle(np.conj(s[:-1]) * s[1:])
        abs_diff = np.abs(diff)
        avg_phase = float(np.mean(abs_diff))
        trans_ratio = float(np.mean(abs_diff > 0.5))
    else:
        avg_phase, trans_ratio = 0.0, 0.0

    cw_score = (
        0.5 * max(0.0, min(1.0, (occ - 0.85) / 0.15))
        + 0.3 * max(0.0, min(1.0, 1.0 - papr / 10.0))
        + 0.2 * max(0.0, min(1.0, 1.0 - avg_phase / 1.5))
    )
    fsk_score = (
        0.45 * max(0.0, min(1.0, (avg_phase - 0.05) / 1.15))
        + 0.35 * max(0.0, min(1.0, (trans_ratio - 0.01) / 0.49))
        + 0.20 * max(0.0, min(1.0, (flat - 0.3) / 0.5))
    )
    psk_score = (
        0.4 * max(0.0, min(1.0, 1.0 - papr / 6.0))
        + 0.4 * max(0.0, min(1.0, (avg_phase - 0.3) / 2.2))
        + 0.2 * max(0.0, min(1.0, (occ - 0.5) / 0.5))
    )
    ook_score = (
        0.45 * max(0.0, min(1.0, papr / 10.0))
        + 0.35 * max(0.0, min(1.0, 1.0 - occ / 0.6))
        + 0.20 * max(0.0, min(1.0, 1.0 - flat))
    )

    return _margin_confidence([cw_score, fsk_score, psk_score, ook_score])


# ---------------------------------------------------------------------------
# IQ file loading
# ---------------------------------------------------------------------------

def load_cf32(path: str) -> Optional[np.ndarray]:
    """Load a raw CF32 (interleaved float32) IQ file; returns None on error."""
    try:
        raw = np.fromfile(path, dtype=np.float32)
        if len(raw) < 64:
            return None
        return (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)
    except (OSError, ValueError):
        return None


def collect_snapshots(snapshot_dir: str) -> List[np.ndarray]:
    """Return a list of IQ arrays from all .cf32 files in snapshot_dir."""
    p = Path(snapshot_dir)
    if not p.is_dir():
        return []
    blocks: List[np.ndarray] = []
    for fpath in sorted(p.glob("**/*.cf32")):
        iq = load_cf32(str(fpath))
        if iq is not None:
            blocks.append(iq)
    return blocks


# ---------------------------------------------------------------------------
# Synthetic reference signal generators (fallback when no snapshots available)
# ---------------------------------------------------------------------------

def _awgn(signal: np.ndarray, snr_db_val: float) -> np.ndarray:
    sig_pow = float(np.mean(np.abs(signal) ** 2)) or 1.0
    noise_pow = sig_pow / 10 ** (snr_db_val / 10.0)
    noise = np.sqrt(noise_pow / 2.0) * (
        np.random.randn(len(signal)) + 1j * np.random.randn(len(signal))
    )
    return (signal + noise).astype(np.complex64)


def _gen_fsk2(n: int = 4096, fs: float = _SAMPLE_RATE,
              fdev: float = 50_000, rsym: float = 128_000) -> np.ndarray:
    """Generate continuous-phase FSK (CPFSK) with binary frequency deviation ±fdev."""
    sps = max(1, int(fs / rsym))
    n_sym = n // sps
    bits = np.random.randint(0, 2, n_sym)
    phase_inc = 2.0 * math.pi * (2 * bits - 1) * fdev / fs
    return np.exp(1j * np.cumsum(np.repeat(phase_inc, sps))).astype(np.complex64)


def _gen_qpsk(n: int = 4096) -> np.ndarray:
    sps = 16
    n_sym = n // sps
    bits = np.random.randint(0, 2, (n_sym, 2))
    sym = ((2 * bits[:, 0] - 1) + 1j * (2 * bits[:, 1] - 1)) / math.sqrt(2)
    up = np.zeros(n_sym * sps, dtype=complex)
    up[::sps] = sym
    return up.astype(np.complex64)


def _gen_ook(n: int = 4096, sps: int = 16) -> np.ndarray:
    n_sym = n // sps
    bits = (np.random.random(n_sym) < 0.5).astype(float)
    return np.repeat(bits, sps).astype(np.complex64)


def _gen_cw(n: int = 4096, fs: float = _SAMPLE_RATE) -> np.ndarray:
    t = np.arange(n) / fs
    return np.exp(1j * 2.0 * math.pi * 1_000.0 * t).astype(np.complex64)


def generate_synthetic_blocks(n_blocks_per_mod: int = 20,
                               snrs: Tuple[float, ...] = (5.0, 10.0, 15.0),
                               seed: int = 42) -> List[np.ndarray]:
    """
    Generate synthetic IQ blocks covering FSK, QPSK, OOK, and CW at several
    SNR levels.  Used as a fallback when no real snapshots are available.
    """
    np.random.seed(seed)
    generators = [_gen_fsk2, _gen_qpsk, _gen_ook, _gen_cw]
    blocks: List[np.ndarray] = []
    for _ in range(n_blocks_per_mod):
        for gen in generators:
            for snr in snrs:
                blocks.append(_awgn(gen(), snr))
    return blocks


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def _pct(values: List[float], p: float) -> float:
    """Return the p-th percentile (0–100) of a list of floats."""
    if not values:
        return 0.0
    return float(np.percentile(values, p))


def _round_to_khz(hz: float) -> int:
    """Round a frequency in Hz to the nearest 1 kHz."""
    return int(round(hz / 1000.0)) * 1000


# ---------------------------------------------------------------------------
# Core recommendation logic
# ---------------------------------------------------------------------------

def compute_recommendations(blocks: List[np.ndarray],
                             sample_rate: float = _SAMPLE_RATE,
                             verbose: bool = False) -> Dict[str, str]:
    """
    Compute threshold recommendations from a list of IQ blocks.

    Returns a dict mapping env-var name -> recommended value string.
    """
    if not blocks:
        raise ValueError("No IQ blocks provided for analysis.")

    powers: List[float] = []
    snrs: List[float] = []
    confs: List[float] = []
    bws: List[float] = []

    for iq in blocks:
        pwr = avg_power(iq)
        s = snr_db(iq)
        c = heuristic_confidence(iq)
        bw = estimate_bandwidth_hz(iq, sample_rate)
        powers.append(pwr)
        snrs.append(s)
        confs.append(c)
        bws.append(bw)

    if verbose:
        print(f"  Analysed {len(blocks)} IQ block(s).")
        print(f"  Power   — p10={_pct(powers,10):.2e} "
              f"p50={_pct(powers,50):.2e} p90={_pct(powers,90):.2e}")
        print(f"  SNR(dB) — p10={_pct(snrs,10):.1f} "
              f"p50={_pct(snrs,50):.1f} p90={_pct(snrs,90):.1f}")
        print(f"  Conf    — p20={_pct(confs,20):.2f} "
              f"p50={_pct(confs,50):.2f} p80={_pct(confs,80):.2f}")
        print(f"  BW(Hz)  — p10={_pct(bws,10):.0f} "
              f"p50={_pct(bws,50):.0f} p90={_pct(bws,90):.0f}")

    # --- RF_MIN_POWER: 10th percentile power among all observed blocks
    rec_min_power = max(_pct(powers, 10), 1e-9)

    # --- RF_SNR_MIN_DB: 10th-percentile SNR, floored at 0.0
    rec_snr_min = max(0.0, _pct(snrs, 10))

    # --- RF_CONF_THRESHOLD: 20th-percentile confidence, clamped [0.3, 0.85]
    rec_conf = max(0.3, min(0.85, _pct(confs, 20)))

    # --- RF_CONSOLE_CONF: 80th-percentile confidence, must be > rec_conf
    rec_console_conf = max(rec_conf + 0.05, min(0.99, _pct(confs, 80)))

    # --- RF_SNAPSHOT_CONF: same as RF_CONF_THRESHOLD
    rec_snapshot_conf = rec_conf

    # --- RF_EXPECTED_BW_HZ: median BW rounded to nearest 1 kHz.
    #   Disable (set to 0) if the interquartile range is very large relative
    #   to the median (IQR/median >= 0.5 means high variance → unreliable
    #   estimate) or if the median is near full-band (>= 90% of sample_rate,
    #   suggesting the estimate has collapsed to a noise floor value).
    med_bw = _pct(bws, 50)
    iqr_bw = _pct(bws, 75) - _pct(bws, 25)
    if med_bw > 0 and (iqr_bw / med_bw) < 0.5 and med_bw < sample_rate * 0.9:
        rec_bw = _round_to_khz(med_bw)
    else:
        rec_bw = 0  # disabled — too much variance or nearly full-band

    return {
        "RF_MIN_POWER": f"{rec_min_power:.2e}",
        "RF_CONF_THRESHOLD": f"{rec_conf:.2f}",
        "RF_CONSOLE_CONF": f"{rec_console_conf:.2f}",
        "RF_SNAPSHOT_CONF": f"{rec_snapshot_conf:.2f}",
        "RF_SNR_MIN_DB": f"{rec_snr_min:.1f}",
        "RF_EXPECTED_BW_HZ": str(rec_bw),
    }


# ---------------------------------------------------------------------------
# Config file writer
# ---------------------------------------------------------------------------

def update_conf_file(conf_file: str, recommendations: Dict[str, str],
                     dry_run: bool = False) -> None:
    """
    Update (or create) conf_file with the recommended values.

    For each key, any existing line ``KEY=...`` is replaced.  Keys that do
    not yet exist in the file are appended.  All other lines are preserved
    unchanged.
    """
    conf_path = Path(conf_file)

    # Read existing content
    if conf_path.exists():
        lines = conf_path.read_text(encoding="utf-8").splitlines(keepends=True)
    else:
        lines = []

    updated_keys = set()
    new_lines: List[str] = []

    for line in lines:
        matched = False
        for key in recommendations:
            if re.match(rf"^{re.escape(key)}=", line):
                new_lines.append(f"{key}={recommendations[key]}\n")
                updated_keys.add(key)
                matched = True
                break
        if not matched:
            new_lines.append(line)

    # Append any keys that were not already in the file
    for key, value in recommendations.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}\n")

    new_content = "".join(new_lines)

    if dry_run:
        print(f"[dry-run] Would write the following to {conf_file}:")
        for key, value in recommendations.items():
            print(f"  {key}={value}")
        return

    # Atomic write: write to a tmp file then rename
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(conf_path.parent), prefix=".autotune_", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        os.replace(tmp_path, str(conf_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    print(f"Wrote tuned thresholds to {conf_file}")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_recommendations(recommendations: Dict[str, str],
                            source: str) -> str:
    """Return a human-readable summary string."""
    lines = [
        "",
        f"=== autotune recommendations (source: {source}) ===",
        "",
    ]
    for key, value in recommendations.items():
        lines.append(f"  {key}={value}")
    lines += [
        "",
        "To apply: sudo python3 tools/autotune_thresholds.py --write",
        "          or copy the values above into /etc/rf_worker/thresholds.env",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--snapshot-dir",
        default=_DEFAULT_SNAPSHOT_DIR,
        metavar="DIR",
        help=f"Directory of .cf32 IQ snapshot files "
             f"(default: {_DEFAULT_SNAPSHOT_DIR})",
    )
    parser.add_argument(
        "--conf",
        default=_DEFAULT_CONF_FILE,
        metavar="FILE",
        help=f"Path to thresholds.env to update (default: {_DEFAULT_CONF_FILE})",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write recommendations to --conf (requires write permission).",
    )
    parser.add_argument(
        "--out",
        metavar="FILE",
        help="Save recommendations as key=value lines to this file (optional).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written without modifying any file.",
    )
    parser.add_argument(
        "--sample-rate",
        type=float,
        default=_SAMPLE_RATE,
        metavar="SPS",
        help=f"SDR sample rate in samples/s (default: {_SAMPLE_RATE:.0f})",
    )
    parser.add_argument(
        "--min-blocks",
        type=int,
        default=10,
        metavar="N",
        help="Minimum number of IQ blocks required from snapshots; "
             "fall back to synthetic signals if fewer are found (default: 10)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print per-metric percentile statistics.",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    print("=== rf_adapt_intel threshold auto-tuner ===")
    print("")

    # --- Collect IQ blocks ---
    print(f"Scanning snapshot directory: {args.snapshot_dir}")
    blocks = collect_snapshots(args.snapshot_dir)

    if len(blocks) >= args.min_blocks:
        source = f"{len(blocks)} IQ snapshot(s) from {args.snapshot_dir}"
        print(f"  Found {len(blocks)} snapshot file(s) — using real signal data.")
    else:
        if blocks:
            print(f"  Only {len(blocks)} snapshot(s) found "
                  f"(need >= {args.min_blocks}). "
                  "Supplementing with synthetic reference signals.")
        else:
            print("  No snapshots found. "
                  "Generating synthetic reference signals for self-tuning.")
        synthetic = generate_synthetic_blocks()
        blocks = blocks + synthetic
        source = "synthetic reference signals (FSK/QPSK/OOK/CW)"

    print("")

    # --- Compute recommendations ---
    if args.verbose:
        print("--- Signal statistics ---")
    recs = compute_recommendations(blocks,
                                   sample_rate=args.sample_rate,
                                   verbose=args.verbose)
    if args.verbose:
        print("")

    # --- Print recommendations ---
    print(format_recommendations(recs, source))

    # --- Write to --out file ---
    if args.out:
        out_path = Path(args.out)
        if args.dry_run:
            print(f"[dry-run] Would write key=value pairs to {args.out}")
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as fh:
                for key, value in recs.items():
                    fh.write(f"{key}={value}\n")
            print(f"Saved recommendations to {args.out}")

    # --- Optionally update conf file ---
    if args.write or args.dry_run:
        update_conf_file(args.conf, recs, dry_run=args.dry_run)

    return 0


if __name__ == "__main__":
    sys.exit(main())
