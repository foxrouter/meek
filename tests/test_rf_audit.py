#!/usr/bin/env python3
"""
tests/test_rf_audit.py - Integration tests for the rf_audit CLI tool.

Builds rf_audit if not already present, generates synthetic CF32 test
vectors, runs rf_audit against them, and validates JSON output fields.

Run with:
    python3 tests/test_rf_audit.py [-v]

Requires: numpy
"""

import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILD_DIR = _REPO_ROOT / "build"
_RF_AUDIT = _BUILD_DIR / "rf_audit"


def _find_rf_audit() -> Path:
    """Return path to rf_audit binary, raising FileNotFoundError if absent."""
    if _RF_AUDIT.exists():
        return _RF_AUDIT
    # Also check /usr/local/bin (installed)
    installed = Path("/usr/local/bin/rf_audit")
    if installed.exists():
        return installed
    raise FileNotFoundError(
        f"rf_audit not found at {_RF_AUDIT} or /usr/local/bin/rf_audit. "
        "Build with: cmake --build build -t rf_audit"
    )


def _write_cf32(path: Path, samples: np.ndarray) -> None:
    """Write complex64 samples as interleaved float32 CF32 file."""
    interleaved = np.empty(len(samples) * 2, dtype=np.float32)
    interleaved[0::2] = samples.real
    interleaved[1::2] = samples.imag
    path.write_bytes(interleaved.tobytes())


def _make_tone_cf32(n: int = 4096, freq_norm: float = 0.1,
                    amplitude: float = 1.0) -> np.ndarray:
    """Generate a complex tone at normalised frequency."""
    t = np.arange(n, dtype=np.float32)
    return (amplitude * np.exp(1j * 2 * np.pi * freq_norm * t)).astype(np.complex64)


def _make_noise_cf32(n: int = 4096, power: float = 1e-6) -> np.ndarray:
    """Generate a noise-floor CF32 block."""
    rng = np.random.default_rng(42)
    return ((rng.standard_normal(n) +
             1j * rng.standard_normal(n)).astype(np.complex64)
            * np.sqrt(power / 2.0))


class TestRfAuditJsonOutput(unittest.TestCase):
    """Validate rf_audit JSON output fields and exit codes."""

    @classmethod
    def setUpClass(cls):
        cls.rf_audit = _find_rf_audit()
        cls.tmp = tempfile.mkdtemp()

    def _run(self, args: list, cf32_path: Path) -> dict:
        """Run rf_audit and return parsed JSON output."""
        cmd = [str(self.rf_audit)] + args + [str(cf32_path)]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0,
            f"rf_audit exited {result.returncode}:\n{result.stderr}")
        lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
        self.assertGreater(len(lines), 0, "rf_audit produced no output")
        return json.loads(lines[0])

    def test_output_has_required_fields(self):
        """rf_audit JSON must contain file, snr_db, confidence, and mod_class."""
        p = Path(self.tmp) / "tone.cf32"
        _write_cf32(p, _make_tone_cf32())
        j = self._run(["--snr-min", "0", "--conf-threshold", "0"], p)
        for field in ("file", "snr_db", "confidence", "mod_class"):
            self.assertIn(field, j,
                f"Required field '{field}' missing from rf_audit output: {j}")

    def test_tone_classifies_as_cw_or_fsk(self):
        """A clean tone should be classified as CW_LIKE or FSK_LIKE."""
        p = Path(self.tmp) / "tone_class.cf32"
        _write_cf32(p, _make_tone_cf32(n=8192, freq_norm=0.05, amplitude=1.0))
        j = self._run(["--snr-min", "0", "--conf-threshold", "0"], p)
        self.assertIn(j.get("mod_class"), ("CW_LIKE", "FSK_LIKE"),
            f"Tone classified as {j.get('mod_class')} — expected CW_LIKE or FSK_LIKE")

    def test_snr_field_is_numeric(self):
        """snr_db must be a finite float."""
        p = Path(self.tmp) / "snr_check.cf32"
        _write_cf32(p, _make_tone_cf32())
        j = self._run(["--snr-min", "0", "--conf-threshold", "0"], p)
        self.assertIsInstance(j["snr_db"], (int, float),
            f"snr_db is not numeric: {j['snr_db']!r}")
        self.assertTrue(abs(j["snr_db"]) < 200,
            f"snr_db={j['snr_db']} is implausible")

    def test_confidence_in_range(self):
        """confidence must be in [0.0, 1.0]."""
        p = Path(self.tmp) / "conf_check.cf32"
        _write_cf32(p, _make_tone_cf32())
        j = self._run(["--snr-min", "0", "--conf-threshold", "0"], p)
        self.assertGreaterEqual(j["confidence"], 0.0)
        self.assertLessEqual(j["confidence"], 1.0)

    def test_missing_file_exits_nonzero(self):
        """rf_audit must exit non-zero for a missing input file."""
        cmd = [str(self.rf_audit), "--snr-min", "0",
               str(Path(self.tmp) / "does_not_exist.cf32")]
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0,
            "rf_audit should exit non-zero for a missing file")

    def test_center_freq_triggers_band_match(self):
        """Passing --center-freq 433920000 must populate band_name in output."""
        p = Path(self.tmp) / "ism433.cf32"
        _write_cf32(p, _make_tone_cf32(n=8192, freq_norm=0.05))
        j = self._run(
            ["--snr-min", "0", "--conf-threshold", "0",
             "--center-freq", "433920000"], p)
        self.assertIn("band_name", j,
            "band_name field missing when --center-freq matches a known band")
        self.assertEqual(j["band_name"], "ISM-433",
            f"Expected band_name=ISM-433, got {j.get('band_name')!r}")

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
