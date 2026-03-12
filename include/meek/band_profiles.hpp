// include/meek/band_profiles.hpp — Compile-time UK frequency band profiles.
//
// Contains a constexpr array of BandProfile structs covering 38 UK SDR-relevant
// frequency allocations.  No runtime JSON/YAML parsing is needed for static data.
// These are built-in defaults; runtime configuration is handled at a higher level.

#pragma once

#include <array>
#include <cmath>
#include <optional>
#include <string_view>

#include "meek/sample_types.hpp"

namespace meek {

// ---------------------------------------------------------------------------
// BandProfile — one frequency band definition
// ---------------------------------------------------------------------------

struct BandProfile {
  std::string_view name;          // short ID e.g. "ADS-B"
  std::string_view description;   // human-readable label
  double center_hz;               // nominal centre frequency (Hz)
  double tolerance_hz;            // ±match window (Hz)
  double expected_bw_hz;          // expected occupied bandwidth (Hz)
  ModClass expected_mod;          // classifier prior hint
  double snr_min_db;  // band-specific SNR gate (-999 = use global default)
  double prior_boost;  // score boost added to expected_mod class [0,1]
  std::string_view notes;          // extra info shown in decision trace / logs
};

// Sentinel value meaning "use the global default SNR gate".
inline constexpr double kBandSnrUseDefault = -999.0;

// ---------------------------------------------------------------------------
// UK band profile table (38 entries) — compiled in, zero runtime cost
// ---------------------------------------------------------------------------

// clang-format off
inline constexpr std::array<BandProfile, 38> kUkBands = {{
  {"ADS-B", "ADS-B 1090 MHz transponders",
   1090e6, 2e6, 1e6, ModClass::OOK_AM_LIKE, 3.0, 0.20,
   "Mode-S/ADS-B squitters at 1090 MHz. Decode with dump1090 or readsb."},
  {"VDL2", "VHF Data Link Mode 2 (136.9 MHz)",
   136.9e6, 0.5e6, 25e3, ModClass::PSK_QAM_LIKE, 2.0, 0.15,
   "ACARS replacement using D8PSK at 10500 bps. Active on 136.900/136.925/136.950 MHz in UK."},
  {"ACARS", "Aircraft Communications Addressing and Reporting System",
   131.725e6, 0.3e6, 8e3, ModClass::OOK_AM_LIKE, 1.0, 0.15,
   "AM-modulated VHF data link at 2400 bps. Decode with acarsdec."},
  {"AIS-A", "AIS channel A (161.975 MHz)",
   161.975e6, 0.05e6, 16e3, ModClass::FSK_LIKE, 1.0, 0.20,
   "Automatic Identification System for maritime vessels. GMSK 9600 bps."},
  {"AIS-B", "AIS channel B (162.025 MHz)",
   162.025e6, 0.05e6, 16e3, ModClass::FSK_LIKE, 1.0, 0.20,
   "AIS channel B. Same as AIS-A but on alternate channel."},
  {"POCSAG-153", "POCSAG paging (153 MHz band)",
   153.35e6, 2.0e6, 12.5e3, ModClass::FSK_LIKE, 0.0, 0.18,
   "Legacy numeric/alphanumeric paging. FSK 512/1200/2400 bps. Decode with multimon-ng."},
  {"FLEX-931", "FLEX high-speed paging (931 MHz)",
   931.9375e6, 2.0e6, 15e3, ModClass::FSK_LIKE, 1.0, 0.15,
   "FLEX 4-FSK paging at 1600/3200/6400 bps. Decode with multimon-ng."},
  {"RADIOSONDE", "Meteorological radiosonde (400-406 MHz)",
   402.5e6, 5.0e6, 100e3, ModClass::FSK_LIKE, 2.0, 0.18,
   "Weather balloon telemetry. FSK or GFSK. Decode with radiosonde_auto_rx."},
  {"NOAA-APT", "NOAA weather satellite APT (137.5 MHz)",
   137.5e6, 0.2e6, 34e3, ModClass::FSK_LIKE, 1.0, 0.15,
   "Analog weather image downlink at 137.500/137.620 MHz. FM subcarrier."},
  {"ISM-433", "ISM 433 MHz band (OOK/ASK devices)",
   433.92e6, 2.0e6, 250e3, ModClass::OOK_AM_LIKE, 0.0, 0.10,
   "License-free ISM band. OOK/ASK remote controls, keyfobs, weather stations."},
  {"LORA-868", "LoRa IoT (868 MHz EU band)",
   868.1e6, 2.0e6, 500e3, ModClass::FSK_LIKE, 1.0, 0.15,
   "LoRaWAN uplink/downlink on EU868 band (863-870 MHz). CSS modulation."},
  {"SMETS2", "Smart meter SMETS2 (868.3 MHz)",
   868.3e6, 0.5e6, 200e3, ModClass::FSK_LIKE, 1.0, 0.12,
   "UK smart electricity/gas meter SMETS2 mesh network. GFSK in 868 MHz band."},
  {"ZWAVE-868", "Z-Wave home automation (868.42 MHz)",
   868.42e6, 0.1e6, 100e3, ModClass::FSK_LIKE, 1.0, 0.12,
   "Z-Wave home automation protocol. GFSK 100 kbps. EU frequency 868.42 MHz."},
  {"TPMS-433", "Tyre Pressure Monitoring System (433 MHz)",
   433.92e6, 2.0e6, 100e3, ModClass::FSK_LIKE, 0.0, 0.12,
   "OBD/TPMS sensors from vehicles at 433.92 MHz. FSK or OOK. Decode with rtl_433."},
  {"DAB", "DAB/DAB+ digital radio (174-240 MHz)",
   218.64e6, 36e6, 1.5e6, ModClass::PSK_QAM_LIKE, 3.0, 0.18,
   "Digital Audio Broadcasting. OFDM/DQPSK in 1.536 MHz channels (Bands III/L)."},
  {"TETRA", "TETRA public safety radio (380-430 MHz)",
   392.0e6, 20.0e6, 25e3, ModClass::PSK_QAM_LIKE, 2.0, 0.20,
   "Terrestrial Trunked Radio. PI/4-DQPSK 25 kHz channels. UK emergency services."},
  {"DMR", "DMR digital voice (446 MHz PMR446)",
   446.0e6, 10.0e6, 12.5e3, ModClass::FSK_LIKE, 1.0, 0.15,
   "Digital Mobile Radio. 4FSK (CQPSK) in 12.5 kHz channels."},
  {"GPS-L1", "GPS L1 C/A (1575.42 MHz)",
   1575.42e6, 5e6, 2e6, ModClass::PSK_QAM_LIKE, -5.0, 0.10,
   "GPS civil signal at 1575.42 MHz. BPSK spread-spectrum (-130 dBm typical)."},
  {"APRS", "APRS 2m packet radio (144.800 MHz)",
   144.8e6, 0.1e6, 16e3, ModClass::FSK_LIKE, kBandSnrUseDefault, 0.18,
   "Automatic Packet Reporting System. Bell 202 AFSK 1200 bps on 144.800 MHz."},
  {"MARINE-CH16", "Marine VHF channel 16 (156.800 MHz)",
   156.8e6, 0.025e6, 16e3, ModClass::UNKNOWN, kBandSnrUseDefault, 0.0,
   "International distress, safety and calling channel. FM voice."},
  {"MARINE-CH70", "Marine VHF DSC channel 70 (156.525 MHz)",
   156.525e6, 0.025e6, 16e3, ModClass::FSK_LIKE, kBandSnrUseDefault, 0.15,
   "Digital Selective Calling distress and safety channel. GFSK 1200 bps."},
  {"METEOR-LRPT", "Meteor-M LRPT satellite (137.1 MHz)",
   137.1e6, 0.15e6, 120e3, ModClass::PSK_QAM_LIKE, 3.0, 0.15,
   "Russian Meteor-M weather satellite LRPT downlink at 137.100 MHz. OQPSK 72 kbps."},
  {"ELT-406", "Emergency Locator Transmitter 406 MHz",
   406.028e6, 0.1e6, 12e3, ModClass::FSK_LIKE, kBandSnrUseDefault, 0.12,
   "Aviation/maritime ELT/EPIRB/PLB distress beacons at 406.028 MHz. FSK."},
  {"SIGFOX-868", "Sigfox IoT network (868.130 MHz)",
   868.13e6, 0.1e6, 200e3, ModClass::OOK_AM_LIKE, kBandSnrUseDefault, 0.12,
   "Sigfox LPWAN uplink at 868.130 MHz. Ultra-narrow-band OOK DBPSK."},
  {"WMBUS-169", "Wireless M-Bus 169 MHz",
   169.406e6, 0.1e6, 12.5e3, ModClass::FSK_LIKE, kBandSnrUseDefault, 0.13,
   "Wireless M-Bus utility metering at 169.406 MHz (EN 13757-4 mode N). GFSK."},
  {"ZIGBEE-868", "ZigBee 868 MHz (EU channel 0)",
   868.3e6, 0.1e6, 600e3, ModClass::PSK_QAM_LIKE, kBandSnrUseDefault, 0.12,
   "ZigBee/IEEE 802.15.4 at 868.3 MHz (EU channel 0). O-QPSK 250 kbps."},
  {"DECT", "DECT cordless phones (1881.792 MHz)",
   1881.792e6, 20.0e6, 1.728e6, ModClass::FSK_LIKE, kBandSnrUseDefault, 0.15,
   "Digital Enhanced Cordless Telecommunications at 1880-1900 MHz. GFSK TDMA."},
  {"PMR446", "PMR446 licence-free radio (446.006 MHz)",
   446.006e6, 0.5e6, 12.5e3, ModClass::FSK_LIKE, kBandSnrUseDefault, 0.15,
   "Personal Mobile Radio 446 MHz. Analogue FM and digital DMR/dPMR."},
  {"ACARS-VHF", "ACARS VHF aviation data (136.9 MHz)",
   136.9e6, 0.05e6, 8e3, ModClass::OOK_AM_LIKE, kBandSnrUseDefault, 0.15,
   "Aircraft Communications Addressing and Reporting System on 136.900 MHz. AM-MSK."},
  {"ISM-169", "ISM 169 MHz sub-GHz IoT",
   169.406e6, 0.1e6, 12.5e3, ModClass::FSK_LIKE, kBandSnrUseDefault, 0.12,
   "ISM/metering devices at 169.406 MHz. GFSK."},
  {"IRIDIUM", "Iridium LEO satellite (1621.250 MHz)",
   1621.25e6, 5.0e6, 100e3, ModClass::PSK_QAM_LIKE, 3.0, 0.15,
   "Iridium LEO satellite burst signals at 1616-1626 MHz. QPSK/OQPSK."},
  {"INMARSAT-AERO", "Inmarsat Aero L-band (1545.000 MHz)",
   1545.0e6, 15.0e6, 500e3, ModClass::PSK_QAM_LIKE, 3.0, 0.13,
   "Inmarsat Aero L-band aviation satellite at 1545 MHz. BPSK/QPSK."},
  {"CNI-UHF", "Combat Net Radio UHF (225-400 MHz)",
   312.5e6, 87.5e6, 25e3, ModClass::FSK_LIKE, kBandSnrUseDefault, 0.10,
   "UK MoD/NATO Combat Net Radio UHF band. AM/FM/FSK tactical comms."},
  {"GSM-R-876", "Network Rail GSM-R uplink (876 MHz)",
   876.0e6, 12e6, 200e3, ModClass::FSK_LIKE, 2.0, 0.15,
   "GWR mainline + Elizabeth Line. Reading station 2.5 mi. "
   "Downlink 921 MHz. High burst rate during peak hours."},
  {"AIRBAND-VHF", "VHF airband AM voice (118-136 MHz)",
   127.0e6, 9e6, 8e3, ModClass::OOK_AM_LIKE, 1.0, 0.12,
   "White Waltham (EGLM) 4 mi. Heathrow TMA overhead. "
   "London Approach 119.725, Farnborough LARS 125.25, "
   "Thames Radar 132.7, White Waltham Info 119.975."},
  {"VOLMET", "London VOLMET continuous weather broadcast",
   126.6e6, 0.05e6, 8e3, ModClass::OOK_AM_LIKE, kBandSnrUseDefault, 0.10,
   "London VOLMET primary 126.600 MHz (Swanwick NATS). "
   "Continuous AM voice, 24/7."},
  {"ACARS-129", "ACARS secondary frequency B (129.125 MHz)",
   129.125e6, 0.1e6, 8e3, ModClass::OOK_AM_LIKE, 1.0, 0.15,
   "Heathrow ACARS B channel (Arinc 129B). "
   "Higher volume on departure push periods."},
  {"ACARS-130", "ACARS secondary frequency C (130.025 MHz)",
   130.025e6, 0.1e6, 8e3, ModClass::OOK_AM_LIKE, 1.0, 0.15,
   "Heathrow ACARS C channel (Arinc 130). "
   "Active during arrival and ground movement phases."},
}};
// clang-format on

// ---------------------------------------------------------------------------
// Band lookup
// ---------------------------------------------------------------------------

/// Returns a pointer to the closest BandProfile whose centre is within
/// tolerance_hz of center_hz, or nullptr if no profile matches.
/// Pure function; O(N) over kUkBands.
[[nodiscard]] inline const BandProfile* find_band(double center_hz) noexcept {
  const BandProfile* best = nullptr;
  double best_dist = -1.0;
  for (const auto& bp : kUkBands) {
    double dist = std::abs(center_hz - bp.center_hz);
    if (dist <= bp.tolerance_hz) {
      if (best == nullptr || dist < best_dist) {
        best = &bp;
        best_dist = dist;
      }
    }
  }
  return best;
}

}  // namespace meek
