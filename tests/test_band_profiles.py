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

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tests"))


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


_BAND_SNR_USE_DEFAULT = -999.0

# Mirror of kUkBands - must match include/meek/band_profiles.hpp exactly.
kUkBands: List[BandProfile] = [
    BandProfile("ADS-B", "ADS-B 1090 MHz Mode-S transponders (PPM)",
                1090e6, 2e6, 1e6, "UNKNOWN", 3.0, 0.0,
                "PPM modulation - not classifiable by heuristic classifier. "
                "Capture and decode externally with dump1090 or readsb."),
    BandProfile("VDL2", "VHF Data Link Mode 2 (136.9 MHz)",
                136.9e6, 0.5e6, 25e3, "PSK_QAM_LIKE", 2.0, 0.15, ""),
    BandProfile("ACARS", "Aircraft Communications Addressing and Reporting System",
                131.725e6, 0.3e6, 8e3, "OOK_AM_LIKE", 1.0, 0.15, ""),
    BandProfile("AIS-A", "AIS channel A (161.975 MHz)",
                161.975e6, 0.05e6, 16e3, "FSK_LIKE", 1.0, 0.20, ""),
    BandProfile("AIS-B", "AIS channel B (162.025 MHz)",
                162.025e6, 0.05e6, 16e3, "FSK_LIKE", 1.0, 0.20, ""),
    BandProfile("POCSAG-153", "POCSAG paging (153 MHz band)",
                153.35e6, 2.0e6, 12.5e3, "FSK_LIKE", 0.0, 0.18, ""),
    BandProfile("FLEX-931", "FLEX high-speed paging (931 MHz)",
                931.9375e6, 2.0e6, 15e3, "FSK_LIKE", 1.0, 0.15, ""),
    BandProfile("RADIOSONDE", "Meteorological radiosonde (400-406 MHz)",
                402.5e6, 5.0e6, 100e3, "FSK_LIKE", 2.0, 0.18, ""),
    BandProfile("NOAA-APT", "NOAA weather satellite APT (137.5 MHz)",
                137.5e6, 0.2e6, 34e3, "FSK_LIKE", 1.0, 0.15, ""),
    BandProfile("ISM-433", "ISM 433 MHz band (OOK/ASK devices)",
                433.92e6, 2.0e6, 250e3, "OOK_AM_LIKE", 0.0, 0.10, ""),
    BandProfile("LORA-868", "LoRa IoT (868 MHz EU band)",
                868.1e6, 2.0e6, 500e3, "FSK_LIKE", 1.0, 0.15, ""),
    BandProfile("SMETS2", "Smart meter SMETS2 (868.3 MHz)",
                868.3e6, 0.5e6, 200e3, "FSK_LIKE", 1.0, 0.12, ""),
    BandProfile("ZWAVE-868", "Z-Wave home automation (868.42 MHz)",
                868.42e6, 0.1e6, 100e3, "FSK_LIKE", 1.0, 0.12, ""),
    BandProfile("TPMS-433", "Tyre Pressure Monitoring System (433 MHz)",
                433.92e6, 0.5e6, 100e3, "FSK_LIKE", 0.0, 0.12, ""),
    BandProfile("DAB-12B", "DAB Block 12B - UK National / BBC (218.640 MHz)",
                218.64e6, 0.9e6, 1.5e6, "PSK_QAM_LIKE", 3.0, 0.18, ""),
    BandProfile("DAB-11D", "DAB Block 11D - UK Commercial (222.064 MHz)",
                222.064e6, 0.9e6, 1.5e6, "PSK_QAM_LIKE", 3.0, 0.18, ""),
    BandProfile("TETRA", "TETRA public safety radio (380-430 MHz)",
                392.0e6, 20.0e6, 25e3, "PSK_QAM_LIKE", 2.0, 0.20, ""),
    BandProfile("DMR", "DMR digital voice (446 MHz PMR446)",
                446.0e6, 10.0e6, 12.5e3, "FSK_LIKE", 1.0, 0.15, ""),
    BandProfile("GPS-L1", "GPS L1 C/A (1575.42 MHz)",
                1575.42e6, 5e6, 2e6, "PSK_QAM_LIKE", -5.0, 0.10, ""),
    BandProfile("APRS", "APRS 2m packet radio (144.800 MHz)",
                144.8e6, 0.1e6, 16e3, "FSK_LIKE", _BAND_SNR_USE_DEFAULT, 0.18, ""),
    BandProfile("MARINE-CH16", "Marine VHF channel 16 (156.800 MHz)",
                156.8e6, 0.025e6, 16e3, "UNKNOWN", _BAND_SNR_USE_DEFAULT, 0.0, ""),
    BandProfile("MARINE-CH70", "Marine VHF DSC channel 70 (156.525 MHz)",
                156.525e6, 0.025e6, 16e3, "FSK_LIKE", _BAND_SNR_USE_DEFAULT, 0.15, ""),
    BandProfile("METEOR-LRPT", "Meteor-M LRPT satellite (137.1 MHz)",
                137.1e6, 0.15e6, 120e3, "PSK_QAM_LIKE", 3.0, 0.15, ""),
    BandProfile("ELT-406", "Emergency Locator Transmitter 406 MHz",
                406.028e6, 0.1e6, 12e3, "FSK_LIKE", _BAND_SNR_USE_DEFAULT, 0.12, ""),
    BandProfile("SIGFOX-868", "Sigfox IoT network (868.130 MHz)",
                868.13e6, 0.1e6, 200e3, "OOK_AM_LIKE", _BAND_SNR_USE_DEFAULT, 0.12, ""),
    BandProfile("WMBUS-169", "Wireless M-Bus 169 MHz",
                169.406e6, 0.1e6, 12.5e3, "FSK_LIKE", _BAND_SNR_USE_DEFAULT, 0.13, ""),
    BandProfile("ZIGBEE-868", "ZigBee 868 MHz (EU channel 0)",
                868.3e6, 0.05e6, 600e3, "PSK_QAM_LIKE", _BAND_SNR_USE_DEFAULT, 0.12, ""),
    BandProfile("DECT", "DECT cordless phones (1881.792 MHz)",
                1881.792e6, 20.0e6, 1.728e6, "FSK_LIKE", _BAND_SNR_USE_DEFAULT, 0.15, ""),
    BandProfile("PMR446", "PMR446 licence-free radio (446.006 MHz)",
                446.006e6, 0.5e6, 12.5e3, "FSK_LIKE", _BAND_SNR_USE_DEFAULT, 0.15, ""),
    BandProfile("ACARS-VHF", "ACARS VHF aviation data (136.9 MHz)",
                136.9e6, 0.05e6, 8e3, "OOK_AM_LIKE", _BAND_SNR_USE_DEFAULT, 0.15, ""),
    BandProfile("ISM-169", "ISM 169 MHz sub-GHz IoT",
                169.406e6, 0.05e6, 12.5e3, "FSK_LIKE", _BAND_SNR_USE_DEFAULT, 0.12, ""),
    BandProfile("IRIDIUM", "Iridium LEO satellite (1621.250 MHz)",
                1621.25e6, 5.0e6, 100e3, "PSK_QAM_LIKE", 3.0, 0.15, ""),
    BandProfile("INMARSAT-AERO", "Inmarsat Aero L-band (1545.000 MHz)",
                1545.0e6, 15.0e6, 500e3, "PSK_QAM_LIKE", 3.0, 0.13, ""),
    BandProfile("CNI-UHF", "Combat Net Radio UHF (225-400 MHz)",
                312.5e6, 87.5e6, 25e3, "FSK_LIKE", _BAND_SNR_USE_DEFAULT, 0.10, ""),
    BandProfile("GSM-R-876", "Network Rail GSM-R uplink (876 MHz)",
                876.0e6, 12e6, 200e3, "FSK_LIKE", 2.0, 0.15, ""),
    BandProfile("AIRBAND-VHF", "VHF airband AM voice (118-136 MHz)",
                127.0e6, 9e6, 8e3, "OOK_AM_LIKE", 1.0, 0.12, ""),
    BandProfile("VOLMET", "London VOLMET continuous weather broadcast",
                126.6e6, 0.05e6, 8e3, "OOK_AM_LIKE", _BAND_SNR_USE_DEFAULT, 0.10, ""),
    BandProfile("ACARS-129", "ACARS secondary frequency B (129.125 MHz)",
                129.125e6, 0.1e6, 8e3, "OOK_AM_LIKE", 1.0, 0.15, ""),
    BandProfile("ACARS-130", "ACARS secondary frequency C (130.025 MHz)",
                130.025e6, 0.1e6, 8e3, "OOK_AM_LIKE", 1.0, 0.15, ""),
]


def find_band(center_hz: float) -> Optional[BandProfile]:
    """Python mirror of meek::find_band() - O(N) closest-within-tolerance."""
    best: Optional[BandProfile] = None
    best_dist = -1.0
    for bp in kUkBands:
        dist = abs(center_hz - bp.center_hz)
        if dist <= bp.tolerance_hz:
            if best is None or dist < best_dist:
                best = bp
                best_dist = dist
    return best


class TestBandReachability(unittest.TestCase):

    def test_tpms_433_present_and_narrower_than_ism(self):
        tpms = next((b for b in kUkBands if b.name == "TPMS-433"), None)
        ism = next((b for b in kUkBands if b.name == "ISM-433"), None)
        self.assertIsNotNone(tpms, "TPMS-433 profile missing from kUkBands")
        self.assertIsNotNone(ism, "ISM-433 profile missing from kUkBands")
        self.assertNotEqual(tpms.expected_mod, "OOK_AM_LIKE",
                            "TPMS-433 must have FSK_LIKE expected_mod, not OOK_AM_LIKE")
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
                    f"'{seen[key]}' - '{bp.name}' is permanently unreachable via find_band()")
            seen[key] = bp.name

    def test_ism_433_reachable_at_centre(self):
        bp = find_band(433.92e6)
        self.assertIsNotNone(bp)
        self.assertEqual(bp.name, "ISM-433",
                         f"Expected ISM-433 at 433.92 MHz, got {bp.name if bp else 'None'}")

    def test_smets2_reachable_at_centre(self):
        bp = find_band(868.3e6)
        self.assertIsNotNone(bp)
        self.assertEqual(bp.name, "SMETS2",
                         f"Expected SMETS2 at 868.3 MHz, got {bp.name if bp else 'None'}")


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
                                 f"DAB profile '{bp.name}' incorrectly matched airband at "
                                 f"{freq_hz/1e6:.1f} MHz - tolerance too wide")

    def test_dab_does_not_match_vdl2(self):
        bp = find_band(136.9e6)
        if bp is not None:
            self.assertFalse(bp.name.startswith("DAB"),
                             "DAB matched VDL2 at 136.9 MHz - tolerance too wide")

    def test_dab_12b_matches_at_centre(self):
        bp = find_band(218.64e6)
        self.assertIsNotNone(bp, "No profile matched 218.640 MHz")
        self.assertTrue(bp.name.startswith("DAB"),
                        f"Expected a DAB profile at 218.640 MHz, got '{bp.name}'")


class TestAdsbModClass(unittest.TestCase):

    def test_adsb_expected_mod_is_unknown(self):
        adsb = next((b for b in kUkBands if b.name == "ADS-B"), None)
        self.assertIsNotNone(adsb, "ADS-B profile missing from kUkBands")
        self.assertEqual(adsb.expected_mod, "UNKNOWN",
                         f"ADS-B expected_mod is '{adsb.expected_mod}' - must be 'UNKNOWN' "
                         "(PPM cannot be decoded by FSK/PSK/OOK demod chains)")

    def test_adsb_prior_boost_is_zero(self):
        adsb = next((b for b in kUkBands if b.name == "ADS-B"), None)
        self.assertIsNotNone(adsb)
        self.assertAlmostEqual(adsb.prior_boost, 0.0, places=6,
                               msg=f"ADS-B prior_boost={adsb.prior_boost} - must be 0.0")

    def test_adsb_reachable_at_1090mhz(self):
        bp = find_band(1090e6)
        self.assertIsNotNone(bp, "No profile matched 1090 MHz")
        self.assertEqual(bp.name, "ADS-B",
                         f"Expected ADS-B at 1090 MHz, got '{bp.name}'")

    def test_adsb_notes_mention_ppm(self):
        adsb = next((b for b in kUkBands if b.name == "ADS-B"), None)
        self.assertIsNotNone(adsb)
        self.assertIn("PPM", adsb.notes,
                      "ADS-B notes must mention PPM so the reason for UNKNOWN is clear "
                      "in decision_trace")


if __name__ == "__main__":
    unittest.main()
