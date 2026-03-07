// include/meek/sample_types.hpp — Core data types for the meek pipeline.
//
// SampleBlock:           raw IQ samples captured from an SDR device.
// ClassificationResult:  result of feature extraction + modulation classifier.
//
// All types must be default-constructible and movable for use in the SPSC
// ring buffer.  No exceptions are thrown by these types.

#pragma once

#include <complex>
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace meek {

// ---------------------------------------------------------------------------
// Modulation classes
// ---------------------------------------------------------------------------

enum class ModClass : std::uint8_t {
  UNKNOWN = 0,
  CW_LIKE,
  FSK_LIKE,
  PSK_QAM_LIKE,
  OOK_AM_LIKE,
};

[[nodiscard]] constexpr const char* mod_class_name(ModClass m) noexcept {
  switch (m) {
    case ModClass::CW_LIKE:
      return "cw_like";
    case ModClass::FSK_LIKE:
      return "fsk_like";
    case ModClass::PSK_QAM_LIKE:
      return "psk_qam_like";
    case ModClass::OOK_AM_LIKE:
      return "ook_am_like";
    default:
      return "unknown";
  }
}

// ---------------------------------------------------------------------------
// SampleBlock — one capture window from the SDR device
// ---------------------------------------------------------------------------

struct SampleBlock {
  std::vector<std::complex<float>> samples;
  std::uint64_t timestamp_ns{0};
  double center_freq_hz{0.0};
  double sample_rate_hz{0.0};
};

// ---------------------------------------------------------------------------
// ClassificationResult — output from the processing thread
// ---------------------------------------------------------------------------

struct ClassificationResult {
  // Input metadata
  std::uint64_t timestamp_ns{0};
  double center_freq_hz{0.0};
  double sample_rate_hz{0.0};

  // Feature vector (computed by classifier.hpp)
  double avg_power{0.0};          // mean instantaneous power E[|z|^2]
  double snr_db{-999.0};          // SNR estimate (dB)
  double papr_db{0.0};            // peak-to-average power ratio (dB)
  double spectral_flatness{1.0};  // geo_mean/arith_mean of power envelope
  double occupied_bw_hz{0.0};     // estimated occupied bandwidth (Hz)

  // Derived / gate results
  double time_occupancy{0.0};
  double avg_abs_phase{0.0};
  double trans_ratio{0.0};
  double p50{0.0};  // 50th percentile power
  double p90{0.0};  // 90th percentile power
  bool snr_gate_pass{false};
  bool bw_gate_pass{false};

  // Classification output
  ModClass mod_class{ModClass::UNKNOWN};
  float confidence{0.0f};
  std::string band_name;
  std::string band_notes;
  std::string decision_trace;
};

}  // namespace meek
