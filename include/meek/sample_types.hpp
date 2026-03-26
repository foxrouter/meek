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
// Demodulation status — closed set of outcomes for a demod attempt
// ---------------------------------------------------------------------------

enum class DemodStatus : std::uint8_t {
  UNKNOWN = 0,  // demod not attempted / status not set
  SKIPPED,      // demod was intentionally skipped (e.g. mod_class == ModClass::UNKNOWN)
  OK,           // demod succeeded
  CRC_FAIL,     // demod ran but CRC check failed
  LOCK_FAIL,    // carrier/timing lock was not achieved
};

[[nodiscard]] constexpr const char* demod_status_name(DemodStatus s) noexcept {
  switch (s) {
    case DemodStatus::UNKNOWN:
      return "unknown";
    case DemodStatus::SKIPPED:
      return "skipped";
    case DemodStatus::OK:
      return "ok";
    case DemodStatus::CRC_FAIL:
      return "crc_fail";
    case DemodStatus::LOCK_FAIL:
      return "lock_fail";
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

  // ── Demodulation results ─────────────────────────────────────────────────
  // demod_status is the single source of truth for demod outcome.
  // UNKNOWN = not attempted; SKIPPED = intentionally skipped; all other
  // values indicate demod was run.  demod_cfo_hz, demod_phase_error, and
  // demod_lock_ms are populated only when demod_status is OK, CRC_FAIL, or
  // LOCK_FAIL.  All fields default to zero/UNKNOWN when demod was not run.
  DemodStatus demod_status{DemodStatus::UNKNOWN};
  float demod_cfo_hz{0.0f};       // carrier frequency offset (Hz)
  float demod_phase_error{0.0f};  // RMS phase error (radians)
  int demod_lock_ms{0};           // time to carrier lock (ms); 0 = not locked
  // Soft information emitted by the demodulator. Semantics are demod-chain
  // dependent and may be per-bit or per-symbol. Values are in the range
  // [0,255], where 0/255 typically indicate strong evidence toward opposite
  // decisions and mid-range values indicate low confidence.
  std::vector<uint8_t> demod_soft_bits;
};

// Minimum number of IQ samples required for a meaningful block classification.
// Blocks smaller than this are rejected by classify_block() with
// REJECT:block_too_small.  Also used as the lower bound for RF_ANALYSIS_LEN.
constexpr std::size_t kMinClassifyBlockSamples = 32;

}  // namespace meek
