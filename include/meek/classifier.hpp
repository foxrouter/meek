// include/meek/classifier.hpp — Signal feature extraction and modulation
// classifier.
//
// All feature extraction operates on std::span<const std::complex<float>>
// views into a SampleBlock.  No heap allocation occurs in the hot path;
// callers are responsible for pre-allocating any scratch buffers.
//
// classify_block() is the top-level entry point used by both the processing
// thread (daemon) and the rf_audit CLI tool.

#pragma once

#include <algorithm>
#include <cmath>
#include <complex>
#include <cstddef>
#include <numeric>
#include <optional>
#include <span>
#include <sstream>
#include <string>
#include <vector>

#include "meek/band_profiles.hpp"
#include "meek/sample_types.hpp"

namespace meek {

// ---------------------------------------------------------------------------
// Feature extraction helpers
// ---------------------------------------------------------------------------

/// Mean instantaneous power E[|z|^2] over the block.
[[nodiscard]] inline double compute_avg_power(
    std::span<const std::complex<float>> s) noexcept {
  if (s.empty()) return 0.0;
  double sum = 0.0;
  for (const auto& z : s) sum += static_cast<double>(std::norm(z));
  return sum / static_cast<double>(s.size());
}

/// SNR estimate: median power as noise floor, mean of top-25% as signal.
/// Mutates a scratch buffer to avoid allocation.
[[nodiscard]] inline double compute_snr_db(
    std::span<const std::complex<float>> s,
    std::vector<float>& scratch) noexcept {
  const std::size_t n = s.size();
  if (n == 0) return -999.0;

  scratch.resize(n);
  for (std::size_t i = 0; i < n; ++i) scratch[i] = std::norm(s[i]);

  const std::size_t med_idx = n / 2;
  const std::size_t top_idx = 3 * n / 4;

  std::nth_element(scratch.begin(), scratch.begin() + med_idx, scratch.end());
  const float noise = scratch[med_idx];
  if (noise < 1e-30f) return -999.0;

  std::nth_element(scratch.begin() + med_idx + 1, scratch.begin() + top_idx,
                   scratch.end());

  double sig_sum = 0.0;
  const std::size_t top_count = n - top_idx;
  for (std::size_t i = top_idx; i < n; ++i) sig_sum += scratch[i];
  const double sig = sig_sum / static_cast<double>(top_count);
  if (sig <= static_cast<double>(noise)) return 0.0;

  return 10.0 * std::log10(sig / static_cast<double>(noise));
}

/// Peak-to-average power ratio (dB).
[[nodiscard]] inline double compute_papr_db(
    std::span<const std::complex<float>> s, double avg_power) noexcept {
  if (s.empty() || avg_power <= 0.0) return 0.0;
  float peak = 0.0f;
  for (const auto& z : s) {
    float p = std::norm(z);
    if (p > peak) peak = p;
  }
  return 10.0 * std::log10(static_cast<double>(peak) / avg_power);
}

/// Spectral flatness: geometric mean / arithmetic mean of power envelope.
/// A value near 1.0 indicates noise; near 0.0 indicates a tonal signal.
[[nodiscard]] inline double compute_spectral_flatness(
    std::span<const std::complex<float>> s) noexcept {
  if (s.empty()) return 1.0;
  double log_sum = 0.0;
  double arith_sum = 0.0;
  std::size_t n_nonzero = 0;
  for (const auto& z : s) {
    const double p = static_cast<double>(std::norm(z));
    if (p > 0.0) {
      log_sum += std::log(p);
      arith_sum += p;
      ++n_nonzero;
    }
  }
  if (n_nonzero == 0) return 1.0;
  const double geo = std::exp(log_sum / static_cast<double>(n_nonzero));
  const double arith = arith_sum / static_cast<double>(n_nonzero);
  return (arith > 0.0) ? geo / arith : 1.0;
}

/// Fraction of samples whose power exceeds 10% of the mean (time occupancy).
[[nodiscard]] inline double compute_time_occupancy(
    std::span<const std::complex<float>> s, double avg_power) noexcept {
  if (s.empty() || avg_power <= 0.0) return 0.0;
  const float threshold = static_cast<float>(avg_power * 0.1);
  std::size_t count = 0;
  for (const auto& z : s) {
    if (std::norm(z) >= threshold) ++count;
  }
  return static_cast<double>(count) / static_cast<double>(s.size());
}

/// Mean absolute phase difference between consecutive samples.
/// Also computes transition ratio (fraction of transitions > pi/4 radians).
inline void compute_phase_stats(std::span<const std::complex<float>> s,
                                double& avg_abs_phase,
                                double& trans_ratio) noexcept {
  avg_abs_phase = 0.0;
  trans_ratio = 0.0;
  if (s.size() < 2) return;

  double phase_sum = 0.0;
  std::size_t transitions = 0;
  constexpr float kPiOver4 = static_cast<float>(M_PI / 4.0);

  for (std::size_t i = 1; i < s.size(); ++i) {
    const std::complex<float> delta = s[i] * std::conj(s[i - 1]);
    const float phi = std::abs(std::arg(delta));
    phase_sum += phi;
    if (phi > kPiOver4) ++transitions;
  }

  const double n = static_cast<double>(s.size() - 1);
  avg_abs_phase = phase_sum / n;
  trans_ratio = static_cast<double>(transitions) / n;
}

/// 50th and 90th percentile of the instantaneous power distribution.
inline void compute_power_percentiles(std::span<const std::complex<float>> s,
                                      double& p50, double& p90,
                                      std::vector<float>& scratch) noexcept {
  p50 = 0.0;
  p90 = 0.0;
  if (s.empty()) return;

  scratch.resize(s.size());
  for (std::size_t i = 0; i < s.size(); ++i) scratch[i] = std::norm(s[i]);

  const std::size_t idx50 = s.size() / 2;
  const std::size_t idx90 = 9 * s.size() / 10;
  std::nth_element(scratch.begin(), scratch.begin() + idx50, scratch.end());
  p50 = scratch[idx50];
  std::nth_element(scratch.begin() + idx50 + 1, scratch.begin() + idx90,
                   scratch.end());
  p90 = scratch[idx90];
}

// ---------------------------------------------------------------------------
// Top-level classifier
// ---------------------------------------------------------------------------

struct ClassifyOptions {
  double min_power{5e-6};
  double snr_min_db{0.0};
  double expected_bw_hz{0.0};  // 0 = disabled
  double papr_max_db{0.0};     // 0 = disabled
  double sample_rate_hz{0.0};  // required for BW guardrail (0 = disabled)
  ModClass mod_hint{ModClass::UNKNOWN};
  const BandProfile* band{nullptr};
};

/// Classify a block of IQ samples.  scratch is a caller-provided vector used
/// as a temporary power array to avoid per-call heap allocation.
[[nodiscard]] inline ClassificationResult classify_block(
    std::span<const std::complex<float>> s, const ClassifyOptions& opts,
    std::vector<float>& scratch) {
  ClassificationResult r;
  r.center_freq_hz = opts.band ? opts.band->center_hz : 0.0;

  if (s.size() < kMinClassifyBlockSamples) {
    r.decision_trace =
        "REJECT:block_too_small n=" + std::to_string(s.size());
    return r;
  }

  double snr_gate = opts.snr_min_db;
  double bw_exp = opts.expected_bw_hz;

  if (opts.band) {
    // Band-specific SNR floor raises the gate but never lowers the global
    // RF_SNR_MIN_DB setting.  Use std::max so the user-configured global
    // value always acts as a minimum: band can only tighten the gate.
    if (opts.band->snr_min_db > kBandSnrUseDefault)
      snr_gate = std::max(snr_gate, opts.band->snr_min_db);
    if (bw_exp <= 0.0) bw_exp = opts.band->expected_bw_hz;
    r.band_name = std::string(opts.band->name);
    r.band_notes = std::string(opts.band->notes);
  }

  // --- Feature extraction ---
  r.avg_power = compute_avg_power(s);
  r.snr_db = compute_snr_db(s, scratch);
  r.papr_db = compute_papr_db(s, r.avg_power);
  r.spectral_flatness = compute_spectral_flatness(s);
  r.time_occupancy = compute_time_occupancy(s, r.avg_power);
  compute_phase_stats(s, r.avg_abs_phase, r.trans_ratio);
  compute_power_percentiles(s, r.p50, r.p90, scratch);

  // Occupied bandwidth estimate
  const double bw_frac = std::clamp(1.0 - r.spectral_flatness, 0.01, 1.0);
  r.occupied_bw_hz =
      (opts.sample_rate_hz > 0.0) ? bw_frac * opts.sample_rate_hz : 0.0;

  // --- SNR gate ---
  r.snr_gate_pass = (r.snr_db >= snr_gate);

  // --- BW guardrail: ±25% of expected bandwidth ---
  r.bw_gate_pass = true;
  if (bw_exp > 0.0 && opts.sample_rate_hz > 0.0) {
    const double est_bw_hz = bw_frac * opts.sample_rate_hz;
    const double ratio = est_bw_hz / bw_exp;
    r.bw_gate_pass = (ratio >= 0.75 && ratio <= 1.25);
  }

  // Build decision trace
  std::ostringstream dt;
  dt << std::fixed;
  dt.precision(3);
  dt << "snr=" << r.snr_db << "dB avg_pow=" << std::scientific << r.avg_power
     << " papr=" << std::fixed << r.papr_db << "dB"
     << " flat=" << r.spectral_flatness << " occ=" << r.time_occupancy
     << " phase=" << r.avg_abs_phase << " trans=" << r.trans_ratio;

  if (!r.snr_gate_pass) {
    dt << " [REJECT:snr_gate snr=" << r.snr_db << "<" << snr_gate << "]";
    r.decision_trace = dt.str();
    return r;
  }
  if (!r.bw_gate_pass) {
    dt << " [REJECT:bw_gate]";
    r.decision_trace = dt.str();
    return r;
  }
  if (r.avg_power < opts.min_power || r.avg_power > 1e3) {
    dt << " [REJECT:power_range]";
    r.decision_trace = dt.str();
    return r;
  }
  if (opts.papr_max_db > 0.0 && r.papr_db > opts.papr_max_db) {
    dt << " [REJECT:papr_max]";
    r.decision_trace = dt.str();
    return r;
  }

  // --- Per-class scoring ---
  double cw_score = [&] {
    return 0.5 * std::clamp((r.time_occupancy - 0.85) / (1.0 - 0.85), 0.0, 1.0) +
           0.3 * std::clamp(1.0 - r.papr_db / 10.0, 0.0, 1.0) +
           0.2 * std::clamp(1.0 - r.avg_abs_phase / 1.5, 0.0, 1.0);
  }();

  double fsk_score = [&] {
    return 0.45 * std::clamp((r.avg_abs_phase - 0.05) / (1.2 - 0.05), 0.0, 1.0) +
           0.35 * std::clamp((r.trans_ratio - 0.01) / (0.5 - 0.01), 0.0, 1.0) +
           0.20 * std::clamp((r.spectral_flatness - 0.3) / (0.8 - 0.3), 0.0, 1.0);
  }();

  double psk_score = [&] {
    return 0.4 * std::clamp(1.0 - r.papr_db / 6.0, 0.0, 1.0) +
           0.4 * std::clamp((r.avg_abs_phase - 0.3) / (2.5 - 0.3), 0.0, 1.0) +
           0.2 * std::clamp((r.time_occupancy - 0.5) / (1.0 - 0.5), 0.0, 1.0);
  }();

  double ook_score = [&] {
    return 0.45 * std::clamp(r.papr_db / 10.0, 0.0, 1.0) +
           0.35 * std::clamp(1.0 - r.time_occupancy / 0.6, 0.0, 1.0) +
           0.20 * std::clamp(1.0 - r.spectral_flatness, 0.0, 1.0);
  }();

  // Apply band prior boost
  if (opts.band) {
    const double boost = opts.band->prior_boost;
    auto apply = [&](ModClass cls, double& score) {
      if (opts.band->expected_mod == cls) score = std::min(1.0, score + boost);
    };
    apply(ModClass::CW_LIKE, cw_score);
    apply(ModClass::FSK_LIKE, fsk_score);
    apply(ModClass::PSK_QAM_LIKE, psk_score);
    apply(ModClass::OOK_AM_LIKE, ook_score);
    dt << " band=" << opts.band->name;
  }

  // Apply MOD_HINT prior bias
  if (opts.mod_hint != ModClass::UNKNOWN) {
    constexpr double kHintBoost = 0.10;
    auto apply = [&](ModClass cls, double& score) {
      if (opts.mod_hint == cls) score = std::min(1.0, score + kHintBoost);
    };
    apply(ModClass::CW_LIKE, cw_score);
    apply(ModClass::FSK_LIKE, fsk_score);
    apply(ModClass::PSK_QAM_LIKE, psk_score);
    apply(ModClass::OOK_AM_LIKE, ook_score);
    dt << " hint=" << mod_class_name(opts.mod_hint);
  }

  // Winner-takes-all
  double best = cw_score;
  r.mod_class = ModClass::CW_LIKE;
  if (fsk_score > best) {
    best = fsk_score;
    r.mod_class = ModClass::FSK_LIKE;
  }
  if (psk_score > best) {
    best = psk_score;
    r.mod_class = ModClass::PSK_QAM_LIKE;
  }
  if (ook_score > best) {
    best = ook_score;
    r.mod_class = ModClass::OOK_AM_LIKE;
  }
  r.confidence = static_cast<float>(best);

  dt.precision(3);
  dt << std::fixed << " scores(cw=" << cw_score << ",fsk=" << fsk_score
     << ",psk=" << psk_score << ",ook=" << ook_score << ") -> "
     << mod_class_name(r.mod_class) << "@" << best;
  r.decision_trace = dt.str();
  return r;
}

}  // namespace meek
