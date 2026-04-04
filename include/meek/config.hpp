// include/meek/config.hpp — Runtime configuration parsed from environment
// variables and command-line arguments.
//
// All env-var reads happen once at startup only; configuration changes require
// restarting the daemon to take effect.

#pragma once

#include <cctype>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>

#include "meek/sample_types.hpp"

namespace meek {

struct Config {
  // SDR hardware
  double center_freq{433.92e6};
  double sample_rate{1'000'000.0};
  double gain{20.0};

  // Capture
  std::size_t block_len{4096};
  // Analysis window: sub-divides each captured block so that short bursty
  // signals are not diluted by averaging over a large noise-filled window.
  // Must be >= 32.  Values larger than block_len are silently clamped to
  // block_len (single-window path, identical to original behaviour).
  std::size_t analysis_len{4096};
  std::int64_t read_timeout_us{500'000};

  // Processing gates
  double min_power{5e-6};
  double snr_min_db{3.0};
  double expected_bw_hz{0.0};
  double papr_max_db{0.0};
  ModClass mod_hint{ModClass::UNKNOWN};

  // Classifier thresholds
  double conf_threshold{0.35};
  double console_conf{0.8};
  double snapshot_conf{0.35};

  // Demodulation hints — reserved for future liquid-dsp demodulation;
  // not currently read by classify_block() or any processing path.
#ifdef HAVE_LIQUID
  double rsym{128'000.0};
  double fdev{50'000.0};
#endif  // HAVE_LIQUID

  // Output paths
  std::string db_path{"/var/lib/rf-adapt-intel/rf_adapt_intel.db"};
  std::string snapshot_dir{"/var/lib/rf-adapt-intel/snapshots"};
  std::string metrics_file{"/var/lib/rf-adapt-intel/metrics.prom"};
  std::string heartbeat_file{"/var/lib/rf-adapt-intel/heartbeat"};
  std::string worker_log{"/var/lib/rf-adapt-intel/worker.log"};

  // Retention
  int snapshot_retention_days{0};

  // Prometheus HTTP server (0 = disabled; only serve textfile)
  std::uint16_t prometheus_port{0};
};

// ---------------------------------------------------------------------------
// Environment variable helpers
// ---------------------------------------------------------------------------

namespace detail {

inline std::int64_t env_ll(const char* name, std::int64_t def) noexcept {
  const char* v = std::getenv(name);
  if (!v)
    return def;
  try {
    return std::stoll(v);
  } catch (...) {
    return def;
  }
}

inline double env_d(const char* name, double def) noexcept {
  const char* v = std::getenv(name);
  if (!v)
    return def;
  try {
    return std::stod(v);
  } catch (...) {
    return def;
  }
}

inline std::string env_str(const char* name, const char* def) {
  const char* v = std::getenv(name);
  return v ? std::string(v) : std::string(def);
}

}  // namespace detail

/// Parse configuration from environment variables.  Command-line arguments
/// override: argv[1]=center_freq_Hz, argv[2]=sample_rate_Sps, argv[3]=gain.
[[nodiscard]] inline Config parse_config(int argc, char** argv) {
  Config cfg;

  // Command-line overrides (positional)
  auto parse_arg = [&](const char* name, const char* raw) -> double {
    try {
      std::size_t pos{};
      const double val = std::stod(raw, &pos);
      // Reject any trailing non-whitespace (e.g. "433.92e6junk").
      // Cast to unsigned char is required: on platforms where char is signed,
      // passing a negative value to std::isspace is undefined behaviour.
      while (raw[pos] != '\0') {
        if (!std::isspace(static_cast<unsigned char>(raw[pos]))) {
          throw std::invalid_argument("trailing characters");
        }
        ++pos;
      }
      return val;
    } catch (const std::exception&) {
      throw std::invalid_argument(std::string("invalid value for ") + name + ": '" + raw + "'");
    }
  };
  if (argc >= 2)
    cfg.center_freq = parse_arg("center_freq_Hz", argv[1]);
  if (argc >= 3)
    cfg.sample_rate = parse_arg("sample_rate_Sps", argv[2]);
  if (argc >= 4)
    cfg.gain = parse_arg("gain", argv[3]);

  // Capture
  cfg.block_len =
      static_cast<std::size_t>(detail::env_ll("RF_BLOCK_LEN", detail::env_ll("BLOCK_LEN", 4096)));

  // RF_ANALYSIS_LEN: clamp in signed space before casting to avoid negative
  // values wrapping when converted to size_t. Must be at least
  // kMinClassifyBlockSamples and no more than block_len (wider window adds
  // no benefit and would exceed the buffer).
  std::int64_t analysis_len_ll = detail::env_ll("RF_ANALYSIS_LEN", 4096);
  const std::int64_t min_analysis_ll = static_cast<std::int64_t>(kMinClassifyBlockSamples);
  const std::int64_t max_analysis_ll = static_cast<std::int64_t>(cfg.block_len);
  if (analysis_len_ll < min_analysis_ll) {
    std::cerr << "[WARN] RF_ANALYSIS_LEN " << analysis_len_ll
              << " below minimum " << min_analysis_ll
              << " — clamped to " << min_analysis_ll << "\n";
    analysis_len_ll = min_analysis_ll;
  }
  if (analysis_len_ll > max_analysis_ll) {
    std::cerr << "[WARN] RF_ANALYSIS_LEN " << analysis_len_ll
              << " exceeds block_len " << max_analysis_ll
              << " — clamped to " << max_analysis_ll << "\n";
    analysis_len_ll = max_analysis_ll;
  }
  cfg.analysis_len = static_cast<std::size_t>(analysis_len_ll);
  cfg.read_timeout_us =
      detail::env_ll("RF_READ_TIMEOUT_US", detail::env_ll("READ_TIMEOUT_US", 500'000));
  // Clamp to [1, kMaxReadTimeoutUs] so negative/zero/enormous values cannot
  // wrap or overflow when used as a SoapySDR timeout or to derive the watchdog
  // stale window.  1 µs is effectively instant; 300 s matches the upper cap
  // used by the watchdog stale-window calculation in src/main.cpp.
  constexpr std::int64_t kMaxReadTimeoutUs = 300'000'000LL;  // 300 s
  if (cfg.read_timeout_us < 1)
    cfg.read_timeout_us = 1;
  if (cfg.read_timeout_us > kMaxReadTimeoutUs)
    cfg.read_timeout_us = kMaxReadTimeoutUs;

  // Processing
  cfg.min_power = detail::env_d("RF_MIN_POWER", 5e-6);
  cfg.snr_min_db = detail::env_d("RF_SNR_MIN_DB", 3.0);
  cfg.expected_bw_hz = detail::env_d("RF_EXPECTED_BW_HZ", 0.0);
  // RF_PAPR_MAX is the canonical name; fall back to legacy PAPR_MAX so
  // existing deployments continue to work without a config change.
  cfg.papr_max_db = detail::env_d("RF_PAPR_MAX",
                       detail::env_d("PAPR_MAX", 0.0));

  // Classifier
  cfg.conf_threshold = detail::env_d("RF_CONF_THRESHOLD", 0.35);
  cfg.console_conf = detail::env_d("RF_CONSOLE_CONF", 0.8);
  // Default snapshot_conf to conf_threshold so every signal persisted to DB
  // also receives an IQ snapshot.  If RF_SNAPSHOT_CONF is set explicitly above
  // conf_threshold (e.g. a stale config with a higher default), clamp it down
  // to conf_threshold to prevent a silent gap where DB writes have no snapshot.
  cfg.snapshot_conf = detail::env_d("RF_SNAPSHOT_CONF", cfg.conf_threshold);
  if (cfg.snapshot_conf > cfg.conf_threshold) {
    cfg.snapshot_conf = cfg.conf_threshold;
  }

  // Demodulation
#ifdef HAVE_LIQUID
  cfg.rsym = detail::env_d("RSYM", 128'000.0);
  cfg.fdev = detail::env_d("FDEV", 50'000.0);
#else
  {
    const char* rsym_env = std::getenv("RSYM");
    const char* fdev_env = std::getenv("FDEV");
    if ((rsym_env && *rsym_env != '\0') || (fdev_env && *fdev_env != '\0'))
      std::cerr << "[CFG] WARN: RSYM/FDEV set but liquid-dsp not compiled in — ignored\n";
  }
#endif  // HAVE_LIQUID

  // Mod hint
  {
    const std::string hint = detail::env_str("MOD_HINT", "");
    if (hint == "fsk" || hint == "gmsk")
      cfg.mod_hint = ModClass::FSK_LIKE;
    else if (hint == "psk" || hint == "qam")
      cfg.mod_hint = ModClass::PSK_QAM_LIKE;
    else if (hint == "ook" || hint == "am")
      cfg.mod_hint = ModClass::OOK_AM_LIKE;
    else if (hint == "cw")
      cfg.mod_hint = ModClass::CW_LIKE;
  }

  // Output paths
  cfg.db_path = detail::env_str("RF_DB_PATH", "/var/lib/rf-adapt-intel/rf_adapt_intel.db");
  cfg.snapshot_dir = detail::env_str("RF_SNAPSHOT_DIR", "/var/lib/rf-adapt-intel/snapshots");
  cfg.metrics_file = detail::env_str("RF_METRICS_FILE", "/var/lib/rf-adapt-intel/metrics.prom");
  cfg.heartbeat_file = detail::env_str("RF_HEARTBEAT_FILE", "/var/lib/rf-adapt-intel/heartbeat");
  cfg.worker_log = detail::env_str("RF_WORKER_LOG", "/var/lib/rf-adapt-intel/worker.log");
  if (std::getenv("RF_WORKER_LOG_MAX_BACKUPS")) {
    std::cerr << "[CFG] RF_WORKER_LOG_MAX_BACKUPS is ignored: internal JsonLog rotation is "
                 "disabled (logrotate manages worker.log via copytruncate)\n";
  }

  // Retention
  cfg.snapshot_retention_days = static_cast<int>(detail::env_ll("RF_SNAPSHOT_RETENTION_DAYS", 0));

  // Prometheus HTTP port (0 = disabled)
  cfg.prometheus_port = static_cast<std::uint16_t>(detail::env_ll("RF_PROMETHEUS_PORT", 0));

  return cfg;
}

}  // namespace meek
