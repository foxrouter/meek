// tools/iq_metrics.cpp — IQ signal metrics (C++ port of autotune_thresholds.py)
//
// Reads a raw CF32 (interleaved complex<float>) file and computes:
//   avg_power          — mean instantaneous power E[|z|^2]
//   snr_db             — SNR estimate: median power as noise floor, mean of
//                        top-25% power as signal
//   spectral_flatness  — temporal power-envelope flatness (geo_mean / arith_mean)
//   est_bw_hz          — occupied bandwidth estimate from spectral flatness
//
// These functions mirror the Python implementations in tools/autotune_thresholds.py
// and the heuristics in src/main.cpp classify_block().
//
// Output: JSON on stdout.
//
// Usage:
//   iq_metrics [--sample-rate FS] [--block-size N] <file.cf32> [file2.cf32 ...]
//   iq_metrics --help
//
// Build:
//   cmake -DBUILD_HARDWARE_TARGETS=OFF -B build && cmake --build build -t iq_metrics

#include <algorithm>
#include <cmath>
#include <complex>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <limits>
#include <nlohmann/json.hpp>
#include <string>
#include <vector>

// ---------------------------------------------------------------------------
// Data types
// ---------------------------------------------------------------------------

struct IqMetrics {
  double avg_power;
  double snr_db;
  double spectral_flatness;
  double est_bw_hz;
  size_t n_samples;
};

// ---------------------------------------------------------------------------
// Metric computation
// ---------------------------------------------------------------------------

/// SNR estimate using median as noise floor and mean of top-25% as signal.
/// Mirrors Python: snr_db(s) in autotune_thresholds.py
static double compute_snr_db(std::vector<float>& powers) {
  const size_t n = powers.size();
  if (n == 0)
    return -999.0;

  // Partial sort to find median and top-25% mean efficiently.
  // nth_element is O(N) on average vs O(N log N) for full sort.
  const size_t med_idx = n / 2;
  const size_t top_idx = 3 * n / 4;

  std::nth_element(powers.begin(), powers.begin() + med_idx, powers.end());
  const float noise = powers[med_idx];

  if (noise < 1e-30f)
    return -999.0;

  // Ensure elements from top_idx onward are in their correct partition.
  std::nth_element(powers.begin() + med_idx + 1, powers.begin() + top_idx, powers.end());

  double sig_sum = 0.0;
  const size_t top_count = n - top_idx;
  for (size_t i = top_idx; i < n; ++i) sig_sum += powers[i];
  const double sig = sig_sum / static_cast<double>(top_count);

  if (sig <= static_cast<double>(noise))
    return 0.0;

  return 10.0 * std::log10(sig / static_cast<double>(noise));
}

/// Compute all metrics for a block in one pass where possible.
static IqMetrics compute_metrics(const std::vector<std::complex<float>>& samples,
                                 double sample_rate) {
  const size_t n = samples.size();
  IqMetrics m{};
  m.n_samples = n;

  if (n == 0) {
    m.avg_power = 0.0;
    m.snr_db = -999.0;
    m.spectral_flatness = 1.0;
    m.est_bw_hz = 0.0;
    return m;
  }

  // Single pass: compute power array, avg_power, log_sum, arith_sum
  std::vector<float> powers(n);
  double sum_power = 0.0;
  double log_sum = 0.0;
  double arith_sum = 0.0;
  size_t n_nonzero = 0;

  for (size_t i = 0; i < n; ++i) {
    const float p = std::norm(samples[i]);
    powers[i] = p;
    sum_power += static_cast<double>(p);
    if (p > 0.0f) {
      log_sum += std::log(static_cast<double>(p));
      arith_sum += static_cast<double>(p);
      ++n_nonzero;
    }
  }

  m.avg_power = sum_power / static_cast<double>(n);

  // Spectral flatness from accumulated sums
  if (n_nonzero > 0) {
    const double geo = std::exp(log_sum / static_cast<double>(n_nonzero));
    const double arith = arith_sum / static_cast<double>(n_nonzero);
    m.spectral_flatness = (arith > 0.0) ? geo / arith : 1.0;
  } else {
    m.spectral_flatness = 1.0;
  }

  // SNR via partial sort (mutates powers — ok, we own it)
  m.snr_db = compute_snr_db(powers);

  // Bandwidth estimate
  const double bw_frac = std::max(0.01, std::min(1.0, 1.0 - m.spectral_flatness));
  m.est_bw_hz = bw_frac * sample_rate;

  return m;
}

// ---------------------------------------------------------------------------
// File I/O
// ---------------------------------------------------------------------------

/// Read an entire CF32 (interleaved float32) file into a complex<float> vector.
/// Returns empty vector on error.
static std::vector<std::complex<float>> read_cf32(const std::string& path) {
  std::ifstream f(path, std::ios::binary | std::ios::ate);
  if (!f) {
    std::cerr << "iq_metrics: cannot open '" << path << "'\n";
    return {};
  }
  const std::streamsize bytes = f.tellg();
  if (bytes < 0 || bytes % 8 != 0) {
    std::cerr << "iq_metrics: file size not a multiple of 8 bytes: '" << path << "'\n";
    return {};
  }
  f.seekg(0);
  const size_t n = static_cast<size_t>(bytes) / 8;
  std::vector<float> raw(n * 2);
  if (!f.read(reinterpret_cast<char*>(raw.data()),
              static_cast<std::streamsize>(raw.size() * sizeof(float)))) {
    std::cerr << "iq_metrics: read error on '" << path << "'\n";
    return {};
  }
  std::vector<std::complex<float>> out(n);
  for (size_t i = 0; i < n; ++i) out[i] = {raw[2 * i], raw[2 * i + 1]};
  return out;
}

// ---------------------------------------------------------------------------
// JSON output helpers
// ---------------------------------------------------------------------------

static void emit_json(const std::string& path, const IqMetrics& m) {
  nlohmann::json j;
  j["file"] = path;
  j["n_samples"] = m.n_samples;
  j["avg_power"] = m.avg_power;
  j["snr_db"] = m.snr_db;
  j["spectral_flatness"] = m.spectral_flatness;
  j["est_bw_hz"] = m.est_bw_hz;
  std::cout << j.dump() << "\n";
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

static void print_usage(const char* prog) {
  std::cerr << "Usage: " << prog
            << " [--sample-rate FS] [--block-size N] <file.cf32> [file2.cf32 ...]\n"
            << "\n"
            << "  --sample-rate FS   Sample rate in Hz (default: 2048000)\n"
            << "  --block-size N     Limit analysis to first N samples (0=all, default: 0)\n"
            << "  --help             Show this message\n"
            << "\n"
            << "Output: one JSON object per line, one per input file.\n";
}

int main(int argc, char* argv[]) {
  double sample_rate = 2'048'000.0;
  size_t block_size = 0;  // 0 = read entire file
  std::vector<std::string> files;

  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--help" || arg == "-h") {
      print_usage(argv[0]);
      return 0;
    } else if (arg == "--sample-rate" && i + 1 < argc) {
      sample_rate = std::stod(argv[++i]);
    } else if (arg == "--block-size" && i + 1 < argc) {
      block_size = static_cast<size_t>(std::stoull(argv[++i]));
    } else if (arg.rfind("--", 0) == 0) {
      std::cerr << "iq_metrics: unknown option '" << arg << "'\n";
      print_usage(argv[0]);
      return 1;
    } else {
      files.push_back(arg);
    }
  }

  if (files.empty()) {
    print_usage(argv[0]);
    return 1;
  }

  int rc = 0;
  for (const auto& path : files) {
    auto samples = read_cf32(path);
    if (samples.empty()) {
      rc = 1;
      continue;
    }
    if (block_size > 0 && samples.size() > block_size)
      samples.resize(block_size);

    const IqMetrics m = compute_metrics(samples, sample_rate);
    emit_json(path, m);
  }
  return rc;
}
