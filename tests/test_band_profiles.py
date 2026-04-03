#!/usr/bin/env python3
"""
tests/test_band_profiles.py - Static correctness tests for kUkBands profiles.

BAND-01: TPMS-433 and ZIGBEE-868 must be reachable (distinct tolerances).
BAND-02: DAB tolerance must be <= 1.0 MHz (not the old 36 MHz Band III wildcard).
BAND-03: ADS-B expected_mod must be UNKNOWN (PPM - not OOK_AM_LIKE).
GENERAL: No two profiles share identical center_hz AND tolerance_hz.

Run with:
    python3 tests/test_band_profiles.py [-v]

Requires: no external dependencies
"""

import re
import unittest
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HEADER_PATH = _REPO_ROOT / "include" / "meek" / "band_profiles.hpp"

# kBandSnrUseDefault sentinel – extracted from the C++ header at import time.
_TOK_BAND_SNR_DEFAULT_DEF = re.compile(
    r'kBandSnrUseDefault\s*=\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)'
)


def _extract_band_snr_default() -> float:
    """Read kBandSnrUseDefault from band_profiles.hpp.

    Raises FileNotFoundError if the header is absent so CI fails clearly."""
    if not _HEADER_PATH.exists():
        raise FileNotFoundError(f"band_profiles.hpp not found: {_HEADER_PATH}")
    text = _HEADER_PATH.read_text()
    m = _TOK_BAND_SNR_DEFAULT_DEF.search(text)
    if m is None:
        raise ValueError("kBandSnrUseDefault definition not found in band_profiles.hpp")
    return float(m.group(1))


_BAND_SNR_USE_DEFAULT: float = _extract_band_snr_default()

# Regex helpers for parsing C++ entries.
_TOK_STR = re.compile(r'"((?:[^"\\]|\\.)*)"')
_TOK_MODCLASS = re.compile(r'ModClass::(\w+)')
_TOK_SNR_DEFAULT = re.compile(r'kBandSnrUseDefault')
_TOK_FLOAT = re.compile(r'([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)')


@dataclass
class BandProfile:
    name: str
    description: str
    center_hz: float
    tolerance_hz: float
    expected_bw_hz: float
    expected_mod: str
    snr_min_db: float
    prior_boost: float
    notes: str


def _parse_entry(entry_text: str) -> BandProfile:
    """Parse one brace-delimited kUkBands entry into a BandProfile."""
    pos = 0
    strings: List[str] = []
    floats: List[float] = []
    modclass: Optional[str] = None

    while pos < len(entry_text):
        m = _TOK_STR.match(entry_text, pos)
        if m:
            strings.append(m.group(1))
            pos = m.end()
            continue
        m = _TOK_MODCLASS.match(entry_text, pos)
        if m:
            modclass = m.group(1)
            pos = m.end()
            continue
        m = _TOK_SNR_DEFAULT.match(entry_text, pos)
        if m:
            floats.append(_BAND_SNR_USE_DEFAULT)
            pos = m.end()
            continue
        c = entry_text[pos]
        if c.isdigit() or c in '+-.':
            m = _TOK_FLOAT.match(entry_text, pos)
            if m:
                floats.append(float(m.group(1)))
                pos = m.end()
                continue
        pos += 1

    if len(strings) < 2:
        raise ValueError(
            f"Entry has fewer than 2 string literals: {entry_text[:80]!r}")
    if len(floats) < 5:
        raise ValueError(
            f"Entry has fewer than 5 numeric values: {entry_text[:80]!r}")
    if modclass is None:
        raise ValueError(
            f"No ModClass found in entry: {entry_text[:80]!r}")

    # C++ adjacent string literal concatenation: join without extra space.
    notes = ''.join(strings[2:]) if len(strings) > 2 else ''

    return BandProfile(
        name=strings[0],
        description=strings[1],
        center_hz=floats[0],
        tolerance_hz=floats[1],
        expected_bw_hz=floats[2],
        expected_mod=modclass,
        snr_min_db=floats[3],
        prior_boost=floats[4],
        notes=notes,
    )


def _load_kuk_bands() -> List[BandProfile]:
    """Parse kUkBands from include/meek/band_profiles.hpp at test runtime.

    Raises FileNotFoundError if the header is absent so CI fails clearly."""
    if not _HEADER_PATH.exists():
        raise FileNotFoundError(
            f"band_profiles.hpp not found at expected path: {_HEADER_PATH}")

    content = _HEADER_PATH.read_text()
    m = re.search(r'kUkBands\s*=\s*\{\{(.*?)\}\};', content, re.DOTALL)
    if not m:
        raise ValueError(
            f"kUkBands constexpr array not found in {_HEADER_PATH}")
    array_body = m.group(1)

    # Extract each top-level {…} entry via brace matching; string literals
    # are tracked to avoid counting braces that appear inside them.
    entries: List[BandProfile] = []
    j = 0
    depth = 0
    start = -1
    in_str = False
    while j < len(array_body):
        c = array_body[j]
        if in_str:
            if c == '\\':
                j += 2          # skip escape sequence
                continue
            if c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == '{':
                if depth == 0:
                    start = j
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0 and start >= 0:
                    entries.append(_parse_entry(array_body[start + 1:j]))
                    start = -1
        j += 1

    if not entries:
        raise ValueError(
            f"No BandProfile entries extracted from {_HEADER_PATH}")
    return entries


# Load once at module import time; failures here abort the whole test run.
kUkBands: List[BandProfile] = _load_kuk_bands()


def find_band(center_hz: float) -> Optional[BandProfile]:
    """Python mirror of meek::find_band() - O(N) closest-within-tolerance.

    Tie-break: prefer narrower tolerance_hz so specific profiles beat wide ones
    at shared centre frequencies."""
    best: Optional[BandProfile] = None
    best_dist = -1.0
    for bp in kUkBands:
        dist = abs(center_hz - bp.center_hz)
        if dist <= bp.tolerance_hz:
            if (best is None
                    or dist < best_dist
                    or (dist == best_dist
                        and bp.tolerance_hz < best.tolerance_hz)):
                best = bp
                best_dist = dist
    return best


class TestBandReachability(unittest.TestCase):

    def test_tpms_433_present_and_narrower_than_ism(self):
        tpms = next((b for b in kUkBands if b.name == "TPMS-433"), None)
        ism = next((b for b in kUkBands if b.name == "ISM-433"), None)
        self.assertIsNotNone(tpms, "TPMS-433 profile missing from kUkBands")
        self.assertIsNotNone(ism, "ISM-433 profile missing from kUkBands")
        self.assertEqual(tpms.expected_mod, "FSK_LIKE",
                         "TPMS-433 must have FSK_LIKE expected_mod")
        self.assertLess(tpms.tolerance_hz, ism.tolerance_hz,
                        f"TPMS-433 tolerance ({tpms.tolerance_hz/1e3:.0f} kHz) must be "
                        f"< ISM-433 tolerance ({ism.tolerance_hz/1e3:.0f} kHz)")

    def test_zigbee_868_narrower_than_smets2(self):
        smets2 = next((b for b in kUkBands if b.name == "SMETS2"), None)
        zigbee = next((b for b in kUkBands if b.name == "ZIGBEE-868"), None)
        self.assertIsNotNone(smets2, "SMETS2 profile missing")
        self.assertIsNotNone(zigbee, "ZIGBEE-868 profile missing")
        self.assertLess(zigbee.tolerance_hz, smets2.tolerance_hz,
                        f"ZIGBEE-868 tolerance ({zigbee.tolerance_hz/1e3:.0f} kHz) must be "
                        f"< SMETS2 tolerance ({smets2.tolerance_hz/1e3:.0f} kHz)")

    def test_no_complete_shadows(self):
        seen = {}
        for bp in kUkBands:
            key = (bp.center_hz, bp.tolerance_hz)
            if key in seen:
                self.fail(
                    f"Profile '{bp.name}' shares center_hz={bp.center_hz/1e6:.3f} MHz "
                    f"and tolerance_hz={bp.tolerance_hz/1e3:.0f} kHz with "
                    f"'{seen[key]}' - '{bp.name}' is permanently unreachable "
                    f"via find_band()")
            seen[key] = bp.name

    def test_narrower_tolerance_wins_on_same_centre(self):
        """When two profiles share center_hz, find_band must prefer the one
        with narrower tolerance at exact centre - otherwise the narrower
        profile is permanently unreachable at that frequency."""
        groups = defaultdict(list)
        for bp in kUkBands:
            groups[bp.center_hz].append(bp)
        shared = {hz: profiles for hz, profiles in groups.items()
                  if len(profiles) > 1}
        for hz, profiles in shared.items():
            narrowest = min(profiles, key=lambda b: b.tolerance_hz)
            result = find_band(hz)
            self.assertIsNotNone(result,
                                 f"find_band returned None at {hz/1e6:.3f} MHz")
            self.assertEqual(result.name, narrowest.name,
                             f"At {hz/1e6:.3f} MHz find_band returned '{result.name}' "
                             f"but narrowest-tolerance profile is '{narrowest.name}' "
                             f"({narrowest.tolerance_hz/1e3:.0f} kHz) - update "
                             f"find_band() tie-break to prefer narrower tolerance")

    def test_ism_433_reachable_outside_tpms_range(self):
        """ISM-433 must be findable at a freq within its 2 MHz window
        but outside TPMS-433's narrower 500 kHz window."""
        # 433.92 + 600 kHz: within ISM-433 (±2 MHz) but outside TPMS-433 (±500 kHz)
        bp = find_band(433.92e6 + 600e3)
        self.assertIsNotNone(bp)
        self.assertEqual(bp.name, "ISM-433",
                         f"Expected ISM-433 at 434.52 MHz, got "
                         f"{bp.name if bp else 'None'}")

    def test_smets2_reachable_outside_zigbee_range(self):
        """SMETS2 must be findable at a freq within its 500 kHz window
        but outside ZIGBEE-868's narrower 50 kHz window."""
        # 868.3 - 60 kHz: within SMETS2 (±500 kHz) but outside ZIGBEE-868 (±50 kHz)
        bp = find_band(868.3e6 - 60e3)
        self.assertIsNotNone(bp)
        self.assertEqual(bp.name, "SMETS2",
                         f"Expected SMETS2 at 868.24 MHz, got "
                         f"{bp.name if bp else 'None'}")


class TestDabTolerance(unittest.TestCase):

    def test_no_dab_profile_exceeds_1mhz_tolerance(self):
        dab_profiles = [b for b in kUkBands if b.name.startswith("DAB")]
        self.assertGreater(len(dab_profiles), 0, "No DAB profiles found")
        for bp in dab_profiles:
            self.assertLessEqual(bp.tolerance_hz, 1.0e6,
                                 f"DAB profile '{bp.name}' has tolerance "
                                 f"{bp.tolerance_hz/1e6:.1f} MHz "
                                 f"- must be <= 1.0 MHz")

    def test_dab_does_not_match_airband(self):
        for freq_hz in [118e6, 127e6, 136e6]:
            bp = find_band(freq_hz)
            if bp is not None:
                self.assertFalse(bp.name.startswith("DAB"),
                                 f"DAB profile '{bp.name}' incorrectly matched "
                                 f"airband at {freq_hz/1e6:.1f} MHz - tolerance too wide")

    def test_dab_does_not_match_vdl2(self):
        bp = find_band(136.9e6)
        if bp is not None:
            self.assertFalse(bp.name.startswith("DAB"),
                             "DAB matched VDL2 at 136.9 MHz - tolerance too wide")

    def test_dab_matches_at_218mhz(self):
        bp = find_band(218.64e6)
        self.assertIsNotNone(bp, "No profile matched 218.640 MHz")
        self.assertTrue(bp.name.startswith("DAB"),
                        f"Expected a DAB profile at 218.640 MHz, got '{bp.name}'")

    def test_dab_11d_matches_at_centre(self):
        """DAB-11D must match at its centre frequency 222.064 MHz."""
        bp = find_band(222.064e6)
        self.assertIsNotNone(bp, "No profile matched 222.064 MHz")
        self.assertTrue(bp.name.startswith("DAB"),
                        f"Expected a DAB profile at 222.064 MHz, got '{bp.name}'")


class TestAdsbModClass(unittest.TestCase):

    def test_adsb_expected_mod_is_unknown(self):
        adsb = next((b for b in kUkBands if b.name == "ADS-B"), None)
        self.assertIsNotNone(adsb, "ADS-B profile missing from kUkBands")
        self.assertEqual(adsb.expected_mod, "UNKNOWN",
                         f"ADS-B expected_mod is '{adsb.expected_mod}' "
                         "- must be 'UNKNOWN' "
                         "(PPM cannot be decoded by FSK/PSK/OOK demod chains)")

    def test_adsb_prior_boost_is_zero(self):
        adsb = next((b for b in kUkBands if b.name == "ADS-B"), None)
        self.assertIsNotNone(adsb)
        self.assertAlmostEqual(adsb.prior_boost, 0.0, places=6,
                               msg=f"ADS-B prior_boost={adsb.prior_boost} "
                               "- must be 0.0")

    def test_adsb_reachable_at_1090mhz(self):
        bp = find_band(1090e6)
        self.assertIsNotNone(bp, "No profile matched 1090 MHz")
        self.assertEqual(bp.name, "ADS-B",
                         f"Expected ADS-B at 1090 MHz, got '{bp.name}'")

    def test_adsb_notes_mention_ppm(self):
        adsb = next((b for b in kUkBands if b.name == "ADS-B"), None)
        self.assertIsNotNone(adsb)
        self.assertIn("PPM", adsb.notes,
                      "ADS-B notes must mention PPM so the reason for UNKNOWN "
                      "is clear in decision_trace")


if __name__ == "__main__":
    unittest.main()
