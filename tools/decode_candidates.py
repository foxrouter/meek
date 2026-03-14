#!/usr/bin/env python3
"""
tools/decode_candidates.py — Retrieve candidate signal records from the
RF-adapt SQLite database, locate matching IQ snapshot files, attempt decoding
using available decoders, and produce a verifiable audit report.

Usage:
    python3 tools/decode_candidates.py \\
        --db rf_adapt_intel.db \\
        --snapshot-dir /var/lib/rf-adapt-intel/snapshots \\
        [--out /tmp/audit_report.json] \\
        [--min-confidence 0.6] \\
        # --sample-rate: snapshot capture rate; built-in decoders receive
        # samples resampled to the canonical analysis rate (2,048,000 Hz).
        [--sample-rate 2048000] \\
        [--limit 100] \\
        [--external]

Decoders (applied in priority order per mod class):
  1. Built-in Python (numpy-based, no external dependencies):
       fsk_like     : FM demodulation -> bit sequence + baud/deviation estimate
       psk_qam_like : phase histogram -> constellation order + symbol sequence
       ook_am_like  : envelope detection -> pulse sequence + duty cycle
       cw_like      : FFT peak -> carrier frequency + stability metric
  2. External free CLI tools (optional, enabled with --external flag):
       multimon-ng  : POCSAG/FLEX/OOK (fsk_like / ook_am_like)
       rtl_433      : OOK/ASK ISM-433 device packets (ook_am_like)
       direwolf     : APRS/AX.25 packet radio (fsk_like)
       acarsdec     : ACARS/VDL2 aviation data (ook_am_like, psk_qam_like)
       rtl-ais      : AIS maritime vessel tracking (fsk_like)
       rtl-wmbus    : Wireless M-Bus utility metering (fsk_like)
       dsd          : DMR/P25/NXDN/DSTAR/TETRA digital voice (fsk_like, psk_qam_like)
       iridium-extractor : Iridium LEO satellite bursts (psk_qam_like)
       satdump      : Inmarsat Aero L-band ACARS (psk_qam_like)

Requires: numpy, sqlite3 (stdlib)
Optional: scipy (improved resampling; auto-detected at runtime)
"""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    from scipy.signal import resample_poly as _resample_poly
    _HAVE_SCIPY = True
except ImportError:  # pragma: no cover
    _HAVE_SCIPY = False

# Audit report format version (increment when report schema changes)
_VERSION = "1.0.0"

# Canonical analysis sample rate fed to built-in decoders.  Snapshots that
# were captured at a different rate are resampled to this value before decoding.
_DECODER_FS = 2_048_000

# ---------------------------------------------------------------------------
# Band name -> centre frequency (Hz) table, mirrors UK_BANDS in main.cpp
# ---------------------------------------------------------------------------

_BAND_FREQ_HZ: Dict[str, int] = {
    "ADS-B":       1_090_000_000,
    "VDL2":          136_900_000,
    "ACARS":         131_725_000,
    "AIS-A":         161_975_000,
    "AIS-B":         162_025_000,
    "POCSAG-153":    153_350_000,
    "FLEX-931":      931_937_500,
    "RADIOSONDE":    402_500_000,
    "NOAA-APT":      137_500_000,
    "ISM-433":       433_920_000,
    "LORA-868":      868_100_000,
    "SMETS2":        868_300_000,
    "ZWAVE-868":     868_420_000,
    "TPMS-433":      433_920_000,
    "DAB":           218_640_000,
    "TETRA":         392_000_000,
    "DMR":           446_000_000,
    "GPS-L1":      1_575_420_000,
    "APRS":          144_800_000,
    "MARINE-CH16":   156_800_000,
    "MARINE-CH70":   156_525_000,
    "METEOR-LRPT":   137_100_000,
    "ELT-406":       406_028_000,
    "SIGFOX-868":    868_130_000,
    "WMBUS-169":     169_406_000,
    "ZIGBEE-868":    868_300_000,
    "DECT":        1_881_792_000,
    "PMR446":        446_006_000,
    "ACARS-VHF":     136_900_000,
    "ISM-169":       169_406_000,
    "IRIDIUM":     1_621_250_000,
    "INMARSAT-AERO": 1_545_000_000,
    "CNI-UHF":       312_500_000,
    "GSM-R-876":     876_000_000,
    "AIRBAND-VHF":   124_000_000,
    "VOLMET":        126_600_000,
    "ACARS-129":     129_125_000,
    "ACARS-130":     130_025_000,
}

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_CANDIDATES_SQL = """
    SELECT
        s.id          AS signal_id,
        s.timestamp   AS db_timestamp,
        s.source,
        s.note        AS decision_trace,
        e.id          AS example_id,
        e.confidence,
        e.result,
        e.notes,
        m.name        AS method_name,
        m.params_json
    FROM signals  s
    JOIN examples e ON e.signal_id = s.id
    JOIN methods  m ON e.method_id  = m.id
    WHERE e.confidence >= ?
      AND e.result = 'candidate'
    ORDER BY e.confidence DESC, s.id DESC
    LIMIT ?
"""


def query_candidates(
    db_path: str, min_confidence: float, limit: int
) -> List[Dict[str, Any]]:
    """Return candidate rows (dicts) from signals + examples tables."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(_CANDIDATES_SQL, (min_confidence, limit))
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Snapshot file helpers
# ---------------------------------------------------------------------------

# Filename patterns:
#   Legacy:     snap_<ts_ns>_c<conf_pct>.cf32
#   With band:  snap_<ts_ns>_c<conf_pct>_b<band_name>.cf32  (rf_adapt_intel v3+)
_SNAP_RE = re.compile(r"^snap_(\d+)_c(\d+)(?:_b([A-Za-z0-9_\-]+))?\.cf32$")


def index_snapshots(snap_dir: str) -> List[Dict[str, Any]]:
    """Index all snapshot files in snap_dir; returns list of metadata dicts."""
    snaps: List[Dict[str, Any]] = []
    if not os.path.isdir(snap_dir):
        return snaps
    for fname in os.listdir(snap_dir):
        m = _SNAP_RE.match(fname)
        if not m:
            continue
        fpath = os.path.join(snap_dir, fname)
        try:
            st = os.stat(fpath)
        except OSError:
            continue
        snaps.append(
            {
                "ts_ns":      int(m.group(1)),
                "conf_pct":   int(m.group(2)),
                "band_name":  m.group(3) or "",
                "path":       fpath,
                "filename":   fname,
                "mtime":      st.st_mtime,
                "size_bytes": st.st_size,
            }
        )
    return snaps


def _build_snap_index(
    snapshots: List[Dict[str, Any]],
) -> Dict[int, List[Dict[str, Any]]]:
    """Return a dict mapping conf_pct -> list of snapshot dicts (O(n) build)."""
    idx: Dict[int, List[Dict[str, Any]]] = {}
    for s in snapshots:
        idx.setdefault(s["conf_pct"], []).append(s)
    return idx


def match_snapshot(
    candidate: Dict[str, Any],
    snapshots: List[Dict[str, Any]],
    _snap_index: Optional[Dict[int, List[Dict[str, Any]]]] = None,
) -> Optional[Dict[str, Any]]:
    """Return the best-matching snapshot for a DB candidate, or None.

    Pass a pre-built index from _build_snap_index() as *_snap_index* to avoid
    the O(n) linear scan when processing many candidates.
    """
    expected_conf_pct = int(candidate["confidence"] * 1000)
    if _snap_index is not None:
        # O(1) lookup with ±1 tolerance
        pool = (
            _snap_index.get(expected_conf_pct, [])
            + _snap_index.get(expected_conf_pct - 1, [])
            + _snap_index.get(expected_conf_pct + 1, [])
        )
    else:
        # Fallback linear scan (used when no index is provided)
        pool = [
            s for s in snapshots
            if abs(s["conf_pct"] - expected_conf_pct) <= 1
        ]
    if not pool:
        return None
    if len(pool) == 1:
        return pool[0]
    # Disambiguate by wall-clock proximity: compare file mtime to DB timestamp
    try:
        db_dt   = datetime.strptime(candidate["db_timestamp"], "%Y-%m-%d %H:%M:%S")
        db_unix = db_dt.replace(tzinfo=timezone.utc).timestamp()
        pool    = sorted(pool, key=lambda s: abs(s["mtime"] - db_unix))
    except (ValueError, TypeError):
        pass
    return pool[0]


# ---------------------------------------------------------------------------
# Decision-trace parsing helpers
# ---------------------------------------------------------------------------

_MOD_RE   = re.compile(r"->\s*(\w+)@([\d.]+)")
_BAND_RE  = re.compile(r"\bband=([A-Za-z0-9_\-]+)")
_SNR_RE   = re.compile(r"\bsnr=([\-\d.]+)dB")


def parse_decision_trace(
    trace: Optional[str],
) -> Tuple[str, float, str, float]:
    """Return (mod_class, confidence, band_name, snr_db) from a decision trace."""
    if not trace:
        return "unknown", 0.0, "", 0.0
    mod_class  = "unknown"
    confidence = 0.0
    band_name  = ""
    snr_db     = 0.0
    m = _MOD_RE.search(trace)
    if m:
        mod_class  = m.group(1)
        confidence = float(m.group(2))
    m = _BAND_RE.search(trace)
    if m:
        band_name = m.group(1)
    m = _SNR_RE.search(trace)
    if m:
        try:
            snr_db = float(m.group(1))
        except ValueError:
            pass
    return mod_class, confidence, band_name, snr_db


# ---------------------------------------------------------------------------
# IQ file loading
# ---------------------------------------------------------------------------

def load_cf32(path: str, max_samples: Optional[int] = None) -> np.ndarray:
    """Load complex float32 IQ samples from a .cf32 file.

    When *max_samples* is given only that many bytes are read, avoiding the
    need to map the full file into memory for large snapshots.
    """
    bytes_per_sample = 8  # 2 × float32
    with open(path, "rb") as fh:
        if max_samples is not None:
            raw = fh.read(max_samples * bytes_per_sample)
        else:
            raw = fh.read()
    floats = np.frombuffer(raw, dtype=np.float32)
    n = len(floats) // 2
    i_vals = floats[:n * 2:2]
    q_vals = floats[1:n * 2:2]
    return (i_vals + 1j * q_vals).astype(np.complex64)


def sha256_file(path: str) -> str:
    """Return hex SHA-256 digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def resample_iq(samples: np.ndarray, fs_in: float, fs_out: float) -> np.ndarray:
    """Resample complex IQ samples from *fs_in* to *fs_out* Hz.

    Uses scipy.signal.resample_poly when available (polyphase FIR, lower
    aliasing); falls back to numpy linear interpolation otherwise.

    Raises ``ValueError`` if either sample rate is non-positive, non-finite,
    or rounds to zero integer Hz.
    Returns an empty complex64 array when the output would contain zero
    samples (extreme decimation applied to very short inputs).
    """
    if not (math.isfinite(fs_in) and math.isfinite(fs_out)):
        raise ValueError(
            f"Sample rates must be finite; got fs_in={fs_in}, fs_out={fs_out}"
        )
    if fs_in <= 0 or fs_out <= 0:
        raise ValueError(
            f"Sample rates must be positive; got fs_in={fs_in}, fs_out={fs_out}"
        )
    # Canonicalize to integer Hz once so that n_out, the polyphase up/down
    # ratio, and the passthrough check all use the same representation.
    fs_in_i  = int(round(fs_in))
    fs_out_i = int(round(fs_out))
    if fs_in_i < 1 or fs_out_i < 1:
        raise ValueError(
            f"Sample rates must round to at least 1 Hz; got fs_in={fs_in}, fs_out={fs_out}"
        )
    if fs_in_i == fs_out_i:
        # Normalize dtype for consistency — callers always get complex64.
        return np.asarray(samples, dtype=np.complex64)
    n_out = int(round(len(samples) * fs_out_i / fs_in_i))
    if n_out < 1:
        return np.empty(0, dtype=np.complex64)
    # Derive exact rational via GCD reduction — no approximation, no time-axis
    # drift from limit_denominator capping.
    gcd  = math.gcd(fs_out_i, fs_in_i)
    up   = fs_out_i // gcd
    down = fs_in_i  // gcd
    if _HAVE_SCIPY:
        out = _resample_poly(samples, up, down).astype(np.complex64)
        # Trim or zero-pad to n_out so both backends return the same length.
        if len(out) > n_out:
            return out[:n_out]
        if len(out) < n_out:
            return np.concatenate(
                [out, np.zeros(n_out - len(out), dtype=np.complex64)]
            )
        return out
    # Numpy fallback: linear interpolation on I and Q channels separately.
    # linspace spans the full input index range [0, len-1] so no time-axis
    # compression occurs regardless of the resampling ratio.
    t_in  = np.arange(len(samples), dtype=np.float64)
    t_out = np.linspace(0, len(samples) - 1, n_out)
    i_out = np.interp(t_out, t_in, samples.real).astype(np.float32)
    q_out = np.interp(t_out, t_in, samples.imag).astype(np.float32)
    return (i_out + 1j * q_out).astype(np.complex64)


# ---------------------------------------------------------------------------
# Built-in decoders
# ---------------------------------------------------------------------------

def decode_fsk(samples: np.ndarray, fs: float = 2_048_000) -> Dict[str, Any]:
    """FM demodulate and extract FSK/GMSK bit sequence.

    Returns a result dict with decoded=True on success.
    """
    if len(samples) < 32:
        return {"decoded": False, "error": "too_few_samples"}

    # --- FM demodulation via differentiated phase ---
    phase = np.angle(samples)
    diff  = np.diff(phase)
    # Wrap to (-π, π]
    diff  = np.arctan2(np.sin(diff), np.cos(diff))

    # Instantaneous frequency in Hz
    inst_freq = diff * fs / (2.0 * math.pi)

    # Estimate symbol rate from zero-crossing density
    signs        = np.sign(inst_freq)
    sign_changes = int(np.sum(np.diff(signs) != 0))
    if sign_changes > 0:
        est_baud = int(sign_changes * fs / (2.0 * len(inst_freq)))
        est_sps  = max(2, int(round(fs / est_baud)))
    else:
        est_baud = int(fs / 8)
        est_sps  = 8

    # Symbol decisions: threshold at DC offset
    dc_offset = float(np.mean(inst_freq))
    n_symbols = len(inst_freq) // est_sps
    if n_symbols < 2:
        return {"decoded": False, "error": "insufficient_symbols"}

    sym_vals = (
        inst_freq[: n_symbols * est_sps]
        .reshape(n_symbols, est_sps)
        .mean(axis=1)
    )
    bits    = (sym_vals > dc_offset).astype(np.uint8)
    bit_str = "".join(str(b) for b in bits[:64])

    return {
        "decoded":         True,
        "method":          "built_in_fm_demod",
        "est_baud_hz":     est_baud,
        "est_sps":         est_sps,
        "n_symbols":       int(n_symbols),
        "freq_dev_hz":     float(np.mean(np.abs(sym_vals - dc_offset))),
        "dc_offset_hz":    dc_offset,
        "first_64_bits":   bit_str,
    }


def decode_psk_qam(samples: np.ndarray, fs: float = 2_048_000) -> Dict[str, Any]:
    """Identify PSK constellation order and recover symbol sequence.

    Tries BPSK (order=2), QPSK (order=4), 8PSK (order=8) and selects the
    one with the lowest mean circular phase-to-centre distance.
    """
    if len(samples) < 32:
        return {"decoded": False, "error": "too_few_samples"}

    # Normalise to unit amplitude (strips AM component for clean phase estimate)
    amp  = np.abs(samples)
    amp  = np.where(amp < 1e-10, 1e-10, amp)
    norm = samples / amp
    phase = np.angle(norm)

    best_order = 2
    best_score = float("inf")
    for order in (2, 4, 8):
        centres = np.linspace(-math.pi, math.pi, order, endpoint=False)
        dists   = np.abs(phase[:, None] - centres[None, :])
        # Circular wrap
        dists   = np.minimum(dists, 2.0 * math.pi - dists)
        score   = float(dists.min(axis=1).mean())
        if score < best_score:
            best_score = score
            best_order = order

    # Take one sample per symbol (coarse timing: sample at midpoint)
    sps = max(2, int(round(fs / 4000)))  # assume ~4 kSym/s as fallback guess
    n_symbols = len(samples) // sps
    if n_symbols < 2:
        return {"decoded": False, "error": "insufficient_symbols"}

    sym_samples = samples[sps // 2::sps][:n_symbols]
    sym_phases  = np.angle(sym_samples)
    sector      = 2.0 * math.pi / best_order
    sym_indices = (
        np.round((sym_phases + math.pi) / sector).astype(int) % best_order
    )

    # Phase noise estimate
    centres_arr = (
        np.array([2.0 * math.pi * k / best_order for k in range(best_order)])
        - math.pi
    )
    assigned    = centres_arr[sym_indices]
    err         = sym_phases - assigned
    err         = np.arctan2(np.sin(err), np.cos(err))
    phase_noise = float(np.std(err))

    bits_per_sym = int(math.log2(best_order))
    sym_str      = "".join(str(s) for s in sym_indices[:32])

    return {
        "decoded":                True,
        "method":                 "built_in_phase_histogram",
        "est_constellation":      f"{best_order}PSK",
        "bits_per_symbol":        bits_per_sym,
        "n_symbols":              int(n_symbols),
        "phase_noise_rms_rad":    phase_noise,
        "phase_fit_score":        best_score,
        "first_32_symbols":       sym_str,
    }


def decode_ook_am(samples: np.ndarray, fs: float = 2_048_000) -> Dict[str, Any]:
    """Envelope-detect OOK/AM samples and extract a pulse sequence."""
    if len(samples) < 16:
        return {"decoded": False, "error": "too_few_samples"}

    envelope = np.abs(samples)
    med      = float(np.median(envelope))
    pk       = float(np.max(envelope))
    # Robust threshold: above noise floor but below peak
    threshold = med + (pk - med) * 0.3
    binary    = (envelope > threshold).astype(np.uint8)

    # Run-length encoding
    runs: List[Tuple[int, int]] = []
    if len(binary):
        cur_val, count = int(binary[0]), 1
        for b in binary[1:]:
            bv = int(b)
            if bv == cur_val:
                count += 1
            else:
                runs.append((cur_val, count))
                cur_val, count = bv, 1
        runs.append((cur_val, count))

    if not runs:
        return {"decoded": False, "error": "no_pulses_detected"}

    # Estimate baud from minimum observed pulse width
    min_pulse = min(r[1] for r in runs)
    est_sps   = max(1, min_pulse)
    est_baud  = int(round(fs / est_sps))

    # Reconstruct bit stream
    bits: List[int] = []
    for val, width in runs:
        n_bits = max(1, round(width / est_sps))
        bits.extend([val] * n_bits)
    bit_str    = "".join(str(b) for b in bits[:64])
    duty_cycle = float(np.mean(binary))

    return {
        "decoded":      True,
        "method":       "built_in_envelope_detect",
        "threshold":    float(threshold),
        "duty_cycle":   duty_cycle,
        "est_baud_hz":  est_baud,
        "est_sps":      est_sps,
        "n_pulses":     len(runs),
        "first_64_bits": bit_str,
    }


def decode_cw(samples: np.ndarray, fs: float = 2_048_000) -> Dict[str, Any]:
    """FFT-based CW carrier detection: returns carrier frequency and stability."""
    if len(samples) < 64:
        return {"decoded": False, "error": "too_few_samples"}

    n_fft = 1
    while n_fft < len(samples):
        n_fft <<= 1
    n_fft   = min(n_fft, 8192)

    spectrum = np.fft.fft(samples[:n_fft])
    power    = np.abs(spectrum) ** 2

    peak_idx = int(np.argmax(power))
    # Map negative-frequency bins to negative Hz
    if peak_idx > n_fft // 2:
        peak_idx -= n_fft
    carrier_hz  = float(peak_idx * fs / n_fft)
    peak_power  = float(power.max())
    noise_floor = float(np.median(power))
    snr_db      = (
        10.0 * math.log10(peak_power / noise_floor)
        if noise_floor > 0
        else 0.0
    )

    # Stability: FFT peak frequency across 8 equal sub-chunks
    n_chunks = min(8, len(samples) // 64)
    chunk_freqs: List[float] = []
    chunk_size  = len(samples) // max(n_chunks, 1)
    for i in range(n_chunks):
        chunk = samples[i * chunk_size:(i + 1) * chunk_size]
        if len(chunk) < 8:
            continue
        c_fft = np.fft.fft(chunk, n=64)
        c_pw  = np.abs(c_fft) ** 2
        ci    = int(np.argmax(c_pw))
        if ci > 32:
            ci -= 64
        chunk_freqs.append(float(ci * fs / 64))
    freq_stability_hz = float(np.std(chunk_freqs)) if len(chunk_freqs) > 1 else 0.0

    return {
        "decoded":            True,
        "method":             "built_in_fft_peak",
        "carrier_hz":         carrier_hz,
        "snr_db":             snr_db,
        "peak_power":         peak_power,
        "freq_stability_hz":  freq_stability_hz,
    }


_BUILTIN_DECODERS = {
    "fsk_like":     decode_fsk,
    "psk_qam_like": decode_psk_qam,
    "ook_am_like":  decode_ook_am,
    "cw_like":      decode_cw,
}

# Protocol keywords recognised in dsd stderr output
_DSD_PROTOCOL_KEYWORDS = ("DMR", "P25", "NXDN", "DSTAR", "TETRA")


# ---------------------------------------------------------------------------
# External free CLI decoders
# ---------------------------------------------------------------------------

def _fm_demod_to_s16(samples: np.ndarray) -> bytes:
    """Return FM-demodulated mono s16le PCM bytes (for multimon-ng input)."""
    phase = np.angle(samples)
    diff  = np.diff(phase)
    diff  = np.arctan2(np.sin(diff), np.cos(diff))
    # Scale to int16 range, clamp to avoid overflow
    fm = np.clip(diff / math.pi * 32767.0, -32767, 32767).astype(np.int16)
    return fm.tobytes()


def try_multimon_ng(
    snap_path: str, mod_class: str, fs: float = 2_048_000
) -> Optional[Dict[str, Any]]:
    """Attempt decoding with multimon-ng (POCSAG/FLEX/FSK/OOK).

    Returns a result dict, or None if multimon-ng is not installed or the
    mod class is not suitable.
    """
    if not shutil.which("multimon-ng"):
        return None

    # Select decoder modes by mod class
    modes: List[str] = []
    if mod_class in ("fsk_like", "unknown"):
        for name in ("POCSAG512", "POCSAG1200", "POCSAG2400", "FLEX", "FSK9600"):
            modes += ["-a", name]
    if mod_class in ("ook_am_like", "unknown"):
        modes += ["-a", "OOK"]
    if not modes:
        return None

    try:
        samples  = load_cf32(snap_path, max_samples=500_000)
        fm_bytes = _fm_demod_to_s16(samples)
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "decoded": False, "method": "multimon-ng", "error": str(exc),
            "traceback": traceback.format_exc(),
        }

    try:
        result = subprocess.run(
            ["multimon-ng", "-t", "raw", "-s", str(int(fs))] + modes,
            input=fm_bytes,
            capture_output=True,
            timeout=15,
        )
        stdout = result.stdout.decode(errors="replace").strip()
        lines  = [
            ln for ln in stdout.splitlines()
            if ln.strip() and not ln.startswith("#")
        ]
        return {
            "decoded":      bool(lines),
            "method":       "multimon-ng",
            "output_lines": lines[:20],
            "return_code":  result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"decoded": False, "method": "multimon-ng", "error": "timeout"}
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "decoded": False, "method": "multimon-ng", "error": str(exc),
            "traceback": traceback.format_exc(),
        }


def try_rtl_433(
    snap_path: str, center_freq_hz: int = 433_920_000, fs: float = 2_048_000
) -> Optional[Dict[str, Any]]:
    """Attempt decoding with rtl_433 (OOK/ASK ISM-433 device packets).

    Returns a result dict, or None if rtl_433 is not installed.
    """
    if not shutil.which("rtl_433"):
        return None

    try:
        result = subprocess.run(
            [
                "rtl_433",
                "-r", snap_path,
                "-f", str(center_freq_hz),
                "-s", str(int(fs)),
                "-F", "json",
            ],
            capture_output=True,
            timeout=30,
        )
        stdout = result.stdout.decode(errors="replace").strip()
        events: List[Any] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return {
            "decoded":     bool(events),
            "method":      "rtl_433",
            "events":      events[:10],
            "return_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"decoded": False, "method": "rtl_433", "error": "timeout"}
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "decoded": False, "method": "rtl_433", "error": str(exc),
            "traceback": traceback.format_exc(),
        }


def try_direwolf(
    snap_path: str, fs: float = 2_048_000
) -> Optional[Dict[str, Any]]:
    """Attempt APRS/AX.25 decoding with direwolf (fsk_like).

    FM-demodulates CF32 to s16 PCM and feeds it to direwolf via stdin.
    Returns a result dict, or None if direwolf is not installed.
    """
    if not shutil.which("direwolf"):
        return None

    try:
        samples  = load_cf32(snap_path, max_samples=500_000)
        fm_bytes = _fm_demod_to_s16(samples)
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "decoded": False, "method": "direwolf", "error": str(exc),
            "traceback": traceback.format_exc(),
        }

    try:
        result = subprocess.run(
            [
                "direwolf",
                "-r", str(int(fs)),
                "-n", "1",
                "-b", "16",
                "-t", "0",
                "-q", "hd",
                "-",
            ],
            input=fm_bytes,
            capture_output=True,
            timeout=20,
        )
        stdout = result.stdout.decode(errors="replace").strip()
        lines  = [ln for ln in stdout.splitlines() if ln.strip()]
        return {
            "decoded":      bool(lines),
            "method":       "direwolf",
            "output_lines": lines[:20],
            "return_code":  result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"decoded": False, "method": "direwolf", "error": "timeout"}
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "decoded": False, "method": "direwolf", "error": str(exc),
            "traceback": traceback.format_exc(),
        }


def try_acarsdec(
    snap_path: str, center_freq_hz: int = 136_900_000, fs: float = 2_048_000
) -> Optional[Dict[str, Any]]:
    """Attempt ACARS decoding with acarsdec (ook_am_like, psk_qam_like).

    Feeds raw CF32 bytes directly to acarsdec via stdin.
    Returns a result dict, or None if acarsdec is not installed.
    """
    if not shutil.which("acarsdec"):
        return None

    proc: Optional[subprocess.Popen] = None  # type: ignore[type-arg]
    try:
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            [
                "acarsdec",
                "-r", str(int(fs)),
                "-f", str(center_freq_hz),
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            chunk_size = 1024 * 1024
            with open(snap_path, "rb") as fh:
                while True:
                    chunk = fh.read(chunk_size)
                    if not chunk:
                        break
                    if proc.stdin is not None:
                        proc.stdin.write(chunk)
            if proc.stdin is not None:
                proc.stdin.close()
            stdout_bytes, _ = proc.communicate(timeout=20)
        except Exception:
            try:
                proc.kill()
                proc.wait()
            except Exception:  # pylint: disable=broad-except
                pass
            raise
        stdout = stdout_bytes.decode(errors="replace").strip()
        lines  = [ln for ln in stdout.splitlines() if ln.strip()]
        return {
            "decoded":      bool(lines),
            "method":       "acarsdec",
            "output_lines": lines[:20],
            "return_code":  proc.returncode,
        }
    except subprocess.TimeoutExpired:
        try:
            if proc is not None:
                proc.kill()
                proc.wait()
        except Exception:  # pylint: disable=broad-except
            pass
        return {"decoded": False, "method": "acarsdec", "error": "timeout"}
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "decoded": False, "method": "acarsdec", "error": str(exc),
            "traceback": traceback.format_exc(),
        }


def try_rtl_ais(
    snap_path: str, center_freq_hz: int = 161_975_000, fs: float = 2_048_000
) -> Optional[Dict[str, Any]]:
    """Attempt AIS decoding with rtl-ais (fsk_like).

    Feeds raw CF32 bytes to rtl-ais via stdin; filters output for NMEA sentences.
    Returns a result dict, or None if rtl-ais is not installed.
    """
    if not shutil.which("rtl-ais"):
        return None

    proc: Optional[subprocess.Popen] = None  # type: ignore[type-arg]
    try:
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            [
                "rtl-ais",
                "-S",
                "-f", str(center_freq_hz),
                "-s", str(int(fs)),
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            chunk_size = 1024 * 1024
            with open(snap_path, "rb") as fh:
                while True:
                    chunk = fh.read(chunk_size)
                    if not chunk:
                        break
                    if proc.stdin is not None:
                        proc.stdin.write(chunk)
            if proc.stdin is not None:
                proc.stdin.close()
            stdout_bytes, _ = proc.communicate(timeout=20)
        except Exception:
            try:
                proc.kill()
                proc.wait()
            except Exception:  # pylint: disable=broad-except
                pass
            raise
        stdout = stdout_bytes.decode(errors="replace").strip()
        lines  = [
            ln for ln in stdout.splitlines()
            if ln.strip() and ("!AIVDM" in ln or "!AIVDO" in ln)
        ]
        return {
            "decoded":      bool(lines),
            "method":       "rtl-ais",
            "output_lines": lines[:20],
            "return_code":  proc.returncode,
        }
    except subprocess.TimeoutExpired:
        try:
            if proc is not None:
                proc.kill()
                proc.wait()
        except Exception:  # pylint: disable=broad-except
            pass
        return {"decoded": False, "method": "rtl-ais", "error": "timeout"}
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "decoded": False, "method": "rtl-ais", "error": str(exc),
            "traceback": traceback.format_exc(),
        }


def try_rtl_wmbus(
    snap_path: str, fs: float = 2_048_000
) -> Optional[Dict[str, Any]]:
    """Attempt Wireless M-Bus decoding with rtl-wmbus (fsk_like).

    Converts CF32 to interleaved uint8 (scale x127.5 + 127.5) and feeds stdin.
    Returns a result dict, or None if rtl-wmbus is not installed.
    """
    if not shutil.which("rtl-wmbus"):
        return None

    try:
        samples = load_cf32(snap_path, max_samples=500_000)
        # Interleave real/imag and scale to uint8 [0, 255]
        iq_flat = np.empty(len(samples) * 2, dtype=np.float32)
        iq_flat[0::2] = samples.real
        iq_flat[1::2] = samples.imag
        uint8_bytes = (
            np.clip(iq_flat * 127.5 + 127.5, 0, 255).astype(np.uint8).tobytes()
        )
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "decoded": False, "method": "rtl-wmbus", "error": str(exc),
            "traceback": traceback.format_exc(),
        }

    try:
        result = subprocess.run(
            ["rtl-wmbus", "-s", str(int(fs))],
            input=uint8_bytes,
            capture_output=True,
            timeout=20,
        )
        stdout = result.stdout.decode(errors="replace").strip()
        lines  = [ln for ln in stdout.splitlines() if ln.strip()]
        return {
            "decoded":      bool(lines),
            "method":       "rtl-wmbus",
            "output_lines": lines[:20],
            "return_code":  result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"decoded": False, "method": "rtl-wmbus", "error": "timeout"}
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "decoded": False, "method": "rtl-wmbus", "error": str(exc),
            "traceback": traceback.format_exc(),
        }


def try_dsd(
    snap_path: str, mod_class: str = "fsk_like", fs: float = 2_048_000
) -> Optional[Dict[str, Any]]:
    """Attempt digital voice decoding with dsd (fsk_like, psk_qam_like).

    FM-demodulates CF32 to s16 PCM and feeds it to dsd via stdin.
    Scans stderr for recognised protocol keywords.
    Returns a result dict, or None if dsd is not installed or mod class is unsuitable.
    """
    if not shutil.which("dsd"):
        return None
    if mod_class not in ("fsk_like", "psk_qam_like", "unknown"):
        return None

    try:
        samples  = load_cf32(snap_path, max_samples=500_000)
        fm_bytes = _fm_demod_to_s16(samples)
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "decoded": False, "method": "dsd", "error": str(exc),
            "traceback": traceback.format_exc(),
        }

    try:
        result = subprocess.run(
            ["dsd", "-i", "-", "-o", "/dev/null", "-n", "-f", "a"],
            input=fm_bytes,
            capture_output=True,
            timeout=20,
        )
        stderr = result.stderr.decode(errors="replace")
        proto_lines = [
            ln for ln in stderr.splitlines()
            if any(kw in ln for kw in _DSD_PROTOCOL_KEYWORDS)
        ]
        return {
            "decoded":      bool(proto_lines),
            "method":       "dsd",
            "output_lines": proto_lines[:20],
            "return_code":  result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"decoded": False, "method": "dsd", "error": "timeout"}
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "decoded": False, "method": "dsd", "error": str(exc),
            "traceback": traceback.format_exc(),
        }


def try_iridium_extractor(
    snap_path: str,
    center_freq_hz: int = 1_621_250_000,
    fs: float = 2_048_000,
) -> Optional[Dict[str, Any]]:
    """Attempt Iridium burst decoding (psk_qam_like).

    Runs iridium-extractor offline on the file then pipes stdout to
    iridium-parser.py (if found). Filters output for A:OK lines.
    Returns a result dict, or None if iridium-extractor is not installed.
    """
    if not shutil.which("iridium-extractor"):
        return None

    try:
        ext_cmd = [
            "iridium-extractor",
            "-c", str(center_freq_hz),
            "-r", str(int(fs)),
            "-f", "cf32_le",
            "--offline",
            snap_path,
        ]
        parser_path = shutil.which("iridium-parser.py") or shutil.which("iridium-parser")

        ext_result = subprocess.run(
            ext_cmd,
            capture_output=True,
            timeout=30,
        )
        raw_output = ext_result.stdout.decode(errors="replace")

        if parser_path:
            parse_result = subprocess.run(
                ["python3", parser_path],
                input=ext_result.stdout,
                capture_output=True,
                timeout=30,
            )
            raw_output = parse_result.stdout.decode(errors="replace")

        lines = [
            ln for ln in raw_output.splitlines()
            if "A:OK" in ln
        ]
        return {
            "decoded":      bool(lines),
            "method":       "iridium-extractor",
            "output_lines": lines[:20],
            "return_code":  ext_result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"decoded": False, "method": "iridium-extractor", "error": "timeout"}
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "decoded": False, "method": "iridium-extractor", "error": str(exc),
            "traceback": traceback.format_exc(),
        }


def try_satdump_aero(
    snap_path: str,
    center_freq_hz: int = 1_545_000_000,
    fs: float = 2_048_000,
) -> Optional[Dict[str, Any]]:
    """Attempt Inmarsat Aero decoding with satdump (psk_qam_like).

    Runs: satdump process inmarsat_aero_105 <tmpdir> --baseband <file>
          --baseband_format cf32 --samplerate <fs> --frequency <freq>
    Scans output JSON files and stderr for ACARS/SU content lines.
    Returns a result dict, or None if satdump is not installed.
    """
    if not shutil.which("satdump"):
        return None

    tmpdir = tempfile.mkdtemp(prefix="satdump_aero_")
    try:
        result = subprocess.run(
            [
                "satdump",
                "process",
                "inmarsat_aero_105",
                tmpdir,
                "--baseband", snap_path,
                "--baseband_format", "cf32",
                "--samplerate", str(int(fs)),
                "--frequency", str(center_freq_hz),
            ],
            capture_output=True,
            timeout=45,
        )
        stderr = result.stderr.decode(errors="replace")

        # Collect decoded content from JSON output files and stderr
        acars_lines: List[str] = [
            ln for ln in stderr.splitlines()
            if "ACARS" in ln or " SU" in ln
        ]
        for json_file in Path(tmpdir).glob("*.json"):
            try:
                with open(json_file, encoding="utf-8") as jf:
                    content = jf.read()
                for ln in content.splitlines():
                    if "ACARS" in ln or " SU" in ln:
                        acars_lines.append(ln)
            except OSError:
                pass

        return {
            "decoded":      bool(acars_lines),
            "method":       "satdump",
            "output_lines": acars_lines[:20],
            "return_code":  result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"decoded": False, "method": "satdump", "error": "timeout"}
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "decoded": False, "method": "satdump", "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Per-candidate decode pipeline
# ---------------------------------------------------------------------------

def decode_candidate(
    candidate: Dict[str, Any],
    snap: Optional[Dict[str, Any]],
    fs: float,
    use_external: bool,
) -> Dict[str, Any]:
    """Run the full decode pipeline for one candidate record.

    Returns an audit entry dict.
    """
    mod_class, _conf, band_name, snr_db = parse_decision_trace(
        candidate.get("decision_trace")
    )

    entry: Dict[str, Any] = {
        "signal_id":      candidate["signal_id"],
        "example_id":     candidate["example_id"],
        "db_timestamp":   candidate["db_timestamp"],
        "source":         candidate.get("source", ""),
        "mod_class":      mod_class,
        "confidence":     candidate["confidence"],
        "snr_db":         snr_db,
        "band":           band_name,
        "snapshot_found": snap is not None,
        "snapshot_file":  snap["filename"] if snap else None,
        "snapshot_sha256": None,
        "snapshot_samples": None,
        "decode_result":  None,
        "decoded":        False,
        "decoder_used":   None,
    }

    if snap is None:
        entry["decode_result"] = {"error": "no_snapshot_file_found"}
        return entry

    # Integrity metadata
    try:
        entry["snapshot_sha256"] = sha256_file(snap["path"])
        entry["snapshot_samples"] = snap["size_bytes"] // 8  # CF32 = 8 bytes/sample
    except OSError as exc:
        entry["decode_result"] = {"error": f"file_read_error: {exc}"}
        return entry

    # ── 1. Built-in decoder ──────────────────────────────────────────────
    try:
        samples = load_cf32(snap["path"], max_samples=200_000)
    except Exception as exc:  # pylint: disable=broad-except
        entry["decode_result"] = {
            "error": f"load_cf32_failed: {exc}",
            "traceback": traceback.format_exc(),
        }
        return entry

    decoder_fn = _BUILTIN_DECODERS.get(mod_class)
    if decoder_fn is None:
        # Fallback: try FSK (most common), but mark method accordingly
        decoder_fn = decode_fsk

    # Normalise to the canonical analysis rate so that built-in decoders
    # receive the expected samples-per-symbol regardless of capture rate.
    # Validate fs before arithmetic (non-finite or non-positive rates are
    # rejected with a clear message; int(round(nan)) would raise an obscure
    # ValueError / OverflowError otherwise).
    if not (math.isfinite(fs) and fs > 0):
        raise ValueError(
            f"--sample-rate must be a finite positive number; got {fs}"
        )
    fs_i = int(round(fs))
    if fs_i != _DECODER_FS:
        samples = resample_iq(samples, fs, _DECODER_FS)
    fs_decode = _DECODER_FS

    result = decoder_fn(samples, fs=fs_decode)
    entry["decode_result"] = result
    entry["decoded"]       = result.get("decoded", False)
    entry["decoder_used"]  = result.get("method", "built_in_unknown")

    # ── 2. External decoder (if requested and built-in did not produce output) ──
    if use_external and not entry["decoded"]:
        center_freq = _BAND_FREQ_HZ.get(band_name, 433_920_000)

        ext_result: Optional[Dict[str, Any]] = None

        # Band-specialist decoders (highest priority)
        if ext_result is None and band_name == "APRS":
            ext_result = try_direwolf(snap["path"], fs=fs)
        if ext_result is None and band_name in ("ACARS", "ACARS-VHF", "ACARS-129", "ACARS-130", "VDL2"):
            ext_result = try_acarsdec(snap["path"], center_freq_hz=center_freq, fs=fs)
        if ext_result is None and band_name in ("AIS-A", "AIS-B"):
            ext_result = try_rtl_ais(snap["path"], center_freq_hz=center_freq, fs=fs)
        if ext_result is None and band_name in ("WMBUS-169", "SMETS2"):
            ext_result = try_rtl_wmbus(snap["path"], fs=fs)
        if ext_result is None and band_name == "IRIDIUM":
            ext_result = try_iridium_extractor(
                snap["path"], center_freq_hz=center_freq, fs=fs
            )
        if ext_result is None and band_name == "INMARSAT-AERO":
            ext_result = try_satdump_aero(
                snap["path"], center_freq_hz=center_freq, fs=fs
            )

        # Broad fallbacks in priority order
        if ext_result is None and mod_class in ("fsk_like", "ook_am_like", "unknown"):
            ext_result = try_multimon_ng(snap["path"], mod_class, fs=fs)
        if ext_result is None and mod_class in ("ook_am_like", "unknown"):
            ext_result = try_rtl_433(snap["path"], center_freq_hz=center_freq, fs=fs)
        if ext_result is None and mod_class in ("fsk_like", "psk_qam_like", "unknown"):
            ext_result = try_dsd(snap["path"], mod_class=mod_class, fs=fs)

        if ext_result is not None:
            entry["decode_result"] = ext_result
            entry["decoded"]       = ext_result.get("decoded", False)
            entry["decoder_used"]  = ext_result.get("method", "external_unknown")

    return entry


# ---------------------------------------------------------------------------
# Audit report
# ---------------------------------------------------------------------------

def build_report(
    db_path: str,
    snap_dir: str,
    candidates: List[Dict[str, Any]],
    results: List[Dict[str, Any]],
    args_dict: Dict[str, Any],
) -> Dict[str, Any]:
    """Assemble the full audit report dict."""
    n_total    = len(results)
    n_snap     = sum(1 for r in results if r["snapshot_found"])
    n_decoded  = sum(1 for r in results if r["decoded"])
    by_decoder: Dict[str, int] = {}
    for r in results:
        if r["decoded"] and r["decoder_used"]:
            by_decoder[r["decoder_used"]] = by_decoder.get(r["decoder_used"], 0) + 1

    return {
        "report_version": _VERSION,
        "generated_at":   datetime.now(tz=timezone.utc).isoformat(),
        "parameters": {
            "db_path":        os.path.abspath(db_path),
            "snapshot_dir":   os.path.abspath(snap_dir),
            "min_confidence": args_dict.get("min_confidence"),
            "limit":          args_dict.get("limit"),
            "sample_rate_hz": args_dict.get("sample_rate"),
            "use_external":   args_dict.get("external"),
        },
        "summary": {
            "candidates_queried": len(candidates),
            "snapshots_matched":  n_snap,
            "snapshots_unmatched": len(candidates) - n_snap,
            "decoded":            n_decoded,
            "not_decoded":        n_total - n_decoded,
            "decoded_by_decoder": by_decoder,
        },
        "results": results,
    }


def write_report(report: Dict[str, Any], out_path: str) -> None:
    """Write the audit report as JSON to out_path."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Decode RF-adapt DB candidates and produce an audit report.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--db",             default="rf_adapt_intel.db",
                   help="Path to SQLite database")
    p.add_argument("--snapshot-dir",   default="/var/lib/rf-adapt-intel/snapshots",
                   help="Directory containing .cf32 snapshot files")
    p.add_argument("--out",            default="/tmp/rf_audit_report.json",
                   help="Output path for JSON audit report")
    p.add_argument("--min-confidence", type=float, default=0.6,
                   help="Minimum confidence threshold for candidate selection")
    p.add_argument("--limit",          type=int,   default=200,
                   help="Maximum number of DB candidates to process")
    p.add_argument("--sample-rate",    type=float, default=2_048_000,
                   help="IQ snapshot capture rate in Hz.  Built-in decoders always"
                        " receive samples resampled to the canonical analysis rate"
                        f" ({_DECODER_FS} Hz); this value is the source capture rate.")
    p.add_argument("--external",       action="store_true",
                   help="Also try external free CLI decoders (multimon-ng, rtl_433)")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    # ── DB ───────────────────────────────────────────────────────────────
    if not os.path.isfile(args.db):
        print(f"[ERROR] database not found: {args.db}", file=sys.stderr)
        return 1

    print(f"[decode_candidates] querying {args.db} "
          f"(min_confidence={args.min_confidence}, limit={args.limit})")
    candidates = query_candidates(args.db, args.min_confidence, args.limit)
    print(f"[decode_candidates] {len(candidates)} candidate record(s) found")

    # ── Snapshots ────────────────────────────────────────────────────────
    snapshots = index_snapshots(args.snapshot_dir)
    print(f"[decode_candidates] {len(snapshots)} snapshot file(s) in "
          f"{args.snapshot_dir}")

    if args.external:
        ext_tools = [t for t in ("multimon-ng", "rtl_433") if shutil.which(t)]
        print(f"[decode_candidates] external decoders available: "
              f"{ext_tools if ext_tools else 'none'}")

    # ── Decode ───────────────────────────────────────────────────────────
    snap_index = _build_snap_index(snapshots)
    results: List[Dict[str, Any]] = []
    for cand in candidates:
        snap   = match_snapshot(cand, snapshots, snap_index)
        entry  = decode_candidate(cand, snap, args.sample_rate, args.external)
        results.append(entry)
        status = "decoded" if entry["decoded"] else "not_decoded"
        snap_s = entry["snapshot_file"] or "<no snapshot>"
        print(f"  signal_id={entry['signal_id']:4d}  "
              f"mod={entry['mod_class']:<14s}  "
              f"conf={entry['confidence']:.3f}  "
              f"snap={snap_s:<40s}  {status}")

    # ── Report ───────────────────────────────────────────────────────────
    args_dict = vars(args)
    report    = build_report(args.db, args.snapshot_dir, candidates,
                             results, args_dict)
    write_report(report, args.out)

    s = report["summary"]
    print(
        f"\n[decode_candidates] SUMMARY\n"
        f"  Candidates queried : {s['candidates_queried']}\n"
        f"  Snapshots matched  : {s['snapshots_matched']}\n"
        f"  Decoded            : {s['decoded']}\n"
        f"  Not decoded        : {s['not_decoded']}\n"
        f"  By decoder         : {s['decoded_by_decoder']}\n"
        f"  Report written to  : {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
