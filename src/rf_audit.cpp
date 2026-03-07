/*
  rf_audit — CLI validation tool for rf_adapt_intel CF32 captures.

  Reads raw CF32 (interleaved complex<float>) files, extracts signal features,
  classifies modulation, and profiles against the UK band table.  Output is
  one JSON object per file on stdout (same format used by the daemon's worker
  log).

  Usage:
    rf_audit [options] <file.cf32> [file2.cf32 ...]

  Options:
    --sample-rate FS     Sample rate in Hz (default: 2048000)
    --center-freq HZ     Centre frequency in Hz (for band matching, default: 0)
    --block-size N       Analyse only the first N samples per file (0 = all)
    --snr-min DB         SNR gate threshold in dB (default: 0.0)
    --conf-threshold T   Minimum confidence to report a candidate (default: 0.0)
    --pretty             Pretty-print JSON output
    --help               Show this message

  Exit codes:
    0  All files processed successfully
    1  One or more files could not be read or contained no samples
    2  Usage error
*/

#include <algorithm>
#include <complex>
#include <cstddef>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <span>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "meek/band_profiles.hpp"
#include "meek/classifier.hpp"
#include "meek/sample_types.hpp"

using namespace meek;
using json = nlohmann::json;

// ---------------------------------------------------------------------------
// File I/O
// ---------------------------------------------------------------------------

static std::vector<std::complex<float>> read_cf32(const std::string& path,
                                                   std::size_t max_samples) {
  std::ifstream f(path, std::ios::binary | std::ios::ate);
  if (!f) {
    std::cerr << "rf_audit: cannot open '" << path << "'\n";
    return {};
  }
  const auto bytes = static_cast<std::size_t>(f.tellg());
  if (bytes % 8 != 0) {
    std::cerr << "rf_audit: file size not a multiple of 8 bytes: '" << path
              << "'\n";
    return {};
  }
  f.seekg(0);
  std::size_t n = bytes / 8;
  if (max_samples > 0 && n > max_samples) n = max_samples;

  std::vector<float> raw(n * 2);
  if (!f.read(reinterpret_cast<char*>(raw.data()),
              static_cast<std::streamsize>(n * 2 * sizeof(float)))) {
    std::cerr << "rf_audit: read error on '" << path << "'\n";
    return {};
  }
  std::vector<std::complex<float>> out(n);
  for (std::size_t i = 0; i < n; ++i) out[i] = {raw[2 * i], raw[2 * i + 1]};
  return out;
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

static void print_usage(const char* prog) {
  std::cerr
      << "Usage: " << prog
      << " [options] <file.cf32> [file2.cf32 ...]\n"
      << "\n"
      << "  --sample-rate FS     Sample rate in Hz (default: 2048000)\n"
      << "  --center-freq HZ     Centre frequency for band matching (default: 0)\n"
      << "  --block-size N       Analyse first N samples per file (0=all)\n"
      << "  --snr-min DB         SNR gate in dB (default: 0.0)\n"
      << "  --conf-threshold T   Min confidence to flag as candidate (default: 0.0)\n"
      << "  --pretty             Pretty-print JSON\n"
      << "  --help               Show this message\n";
}

int main(int argc, char** argv) {
  double sample_rate = 2'048'000.0;
  double center_freq = 0.0;
  std::size_t block_size = 0;
  double snr_min = 0.0;
  double conf_threshold = 0.0;
  bool pretty = false;
  std::vector<std::string> files;

  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--help" || arg == "-h") {
      print_usage(argv[0]);
      return 0;
    } else if (arg == "--sample-rate" && i + 1 < argc) {
      try {
        sample_rate = std::stod(argv[++i]);
      } catch (const std::exception&) {
        std::cerr << "rf_audit: invalid value for --sample-rate: '" << argv[i] << "'\n";
        print_usage(argv[0]);
        return 2;
      }
    } else if (arg == "--center-freq" && i + 1 < argc) {
      try {
        center_freq = std::stod(argv[++i]);
      } catch (const std::exception&) {
        std::cerr << "rf_audit: invalid value for --center-freq: '" << argv[i] << "'\n";
        print_usage(argv[0]);
        return 2;
      }
    } else if (arg == "--block-size" && i + 1 < argc) {
      try {
        block_size = static_cast<std::size_t>(std::stoull(argv[++i]));
      } catch (const std::exception&) {
        std::cerr << "rf_audit: invalid value for --block-size: '" << argv[i] << "'\n";
        print_usage(argv[0]);
        return 2;
      }
    } else if (arg == "--snr-min" && i + 1 < argc) {
      try {
        snr_min = std::stod(argv[++i]);
      } catch (const std::exception&) {
        std::cerr << "rf_audit: invalid value for --snr-min: '" << argv[i] << "'\n";
        print_usage(argv[0]);
        return 2;
      }
    } else if (arg == "--conf-threshold" && i + 1 < argc) {
      try {
        conf_threshold = std::stod(argv[++i]);
      } catch (const std::exception&) {
        std::cerr << "rf_audit: invalid value for --conf-threshold: '" << argv[i] << "'\n";
        print_usage(argv[0]);
        return 2;
      }
    } else if (arg == "--pretty") {
      pretty = true;
    } else if (arg.rfind("--", 0) == 0) {
      std::cerr << "rf_audit: unknown option '" << arg << "'\n";
      print_usage(argv[0]);
      return 2;
    } else {
      files.push_back(arg);
    }
  }

  if (files.empty()) {
    print_usage(argv[0]);
    return 2;
  }

  const BandProfile* band = (center_freq > 0.0) ? find_band(center_freq) : nullptr;

  ClassifyOptions opts;
  opts.snr_min_db = snr_min;
  opts.sample_rate_hz = sample_rate;  // needed for BW guardrail
  opts.band = band;
  // min_power / papr_max / mod_hint left at defaults (permissive for audit)
  opts.min_power = 0.0;

  std::vector<float> scratch;
  int rc = 0;

  for (const auto& path : files) {
    auto samples = read_cf32(path, block_size);
    if (samples.empty()) {
      rc = 1;
      continue;
    }

    ClassificationResult cr = classify_block(std::span{samples}, opts, scratch);
    cr.sample_rate_hz = sample_rate;
    cr.center_freq_hz = center_freq;

    json j;
    j["file"] = path;
    j["n_samples"] = samples.size();
    j["sample_rate_hz"] = sample_rate;
    j["center_freq_hz"] = center_freq;
    j["mod"] = mod_class_name(cr.mod_class);
    j["confidence"] = cr.confidence;
    j["snr_db"] = cr.snr_db;
    j["avg_power"] = cr.avg_power;
    j["papr_db"] = cr.papr_db;
    j["spectral_flatness"] = cr.spectral_flatness;
    j["occupied_bw_hz"] = cr.occupied_bw_hz;
    j["time_occupancy"] = cr.time_occupancy;
    j["avg_abs_phase"] = cr.avg_abs_phase;
    j["trans_ratio"] = cr.trans_ratio;
    j["snr_gate_pass"] = cr.snr_gate_pass;
    j["bw_gate_pass"] = cr.bw_gate_pass;
    j["is_candidate"] = (cr.confidence >= conf_threshold &&
                          cr.snr_gate_pass && cr.bw_gate_pass);
    if (!cr.band_name.empty()) {
      j["band"] = cr.band_name;
      j["band_notes"] = cr.band_notes;
    }
    j["decision_trace"] = cr.decision_trace;

    std::cout << (pretty ? j.dump(2) : j.dump()) << "\n";
  }

  return rc;
}
