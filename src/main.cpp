/*
  rf_adapt_intel — SoapySDR capture -> modulation classifier + demod pipeline
  - Two-stage pipeline: presence/SNR gate -> multi-class classifier
  - Classifies: GMSK/FSK, PSK/QAM, OOK/AM, CW-like using spectral features
  - SNR gate (>0 dB by default, relaxed in canary mode)
  - BW guardrail: ±25% from band-expected bandwidth
  - Outputs decision_trace + per-class confidence to JSON worker log
  - Prometheus textfile at RF_METRICS_FILE; heartbeat at RF_HEARTBEAT_FILE
  - Always stores candidate rows to DB when conf > RF_CONF_THRESHOLD
  - Only logs to console when conf >= RF_CONSOLE_CONF
  - Saves raw CF32 IQ snapshot files when conf >= RF_SNAPSHOT_CONF
  Runtime config comes from environment variables (see README /
  /etc/default/rf-adapt-intel)
*/
#include <SoapySDR/Device.hpp>
#include <SoapySDR/Errors.hpp>
#include <SoapySDR/Formats.hpp>
#include <algorithm> // clamp, sort
#include <atomic>
#include <chrono>
#include <cmath>
#include <complex>
#include <condition_variable>
#include <csignal>
#include <cstdint>
#include <cstdlib> // getenv
#include <cstring>
#include <deque>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <numeric> // accumulate
#include <sqlite3.h>
#include <sstream>
#include <string>
#include <thread>
#include <vector>
#ifdef HAVE_LIQUID
#include <liquid/liquid.h>
// LIQUID_MODEM_8PSK was introduced in liquid-dsp 1.6.0; Debian bookworm ships
// 1.3.2 which only exposes the older LIQUID_MODEM_PSK8 enumerator.
#ifndef LIQUID_MODEM_8PSK
#define LIQUID_MODEM_8PSK LIQUID_MODEM_PSK8
#endif
#endif

static std::atomic<bool> running{true};
// Snapshot write error counter — incremented by the snapshot worker thread.
static std::atomic<uint64_t> g_snap_errors{0};

struct SampleBlock {
  std::vector<std::complex<float>> samples;
  uint64_t timestamp_ns;
};

static std::deque<SampleBlock> q;
static std::mutex q_m;
static std::condition_variable q_cv;

// Snapshot worker: a single background thread drains a task queue so snapshot
// writes are always joined cleanly on shutdown (no detached threads).
static std::deque<std::function<void()>> snap_q;
static std::mutex snap_m;
static std::condition_variable snap_cv;
static std::atomic<bool> snap_running{true};

static void snapshot_worker() {
  while (true) {
    std::function<void()> task;
    {
      std::unique_lock<std::mutex> lk(snap_m);
      snap_cv.wait(lk, [] { return !snap_q.empty() || !snap_running.load(); });
      if (snap_q.empty())
        break; // queue drained and shutdown requested
      task = std::move(snap_q.front());
      snap_q.pop_front();
    }
    task();
  }
}

static sqlite3 *open_db(const std::string &path) {
  sqlite3 *db = nullptr;
  int rc = sqlite3_open(path.c_str(), &db);
  if (rc != SQLITE_OK) {
    // db may still be allocated even on failure; use it for the message if so
    std::cerr << "Cannot open DB: "
              << (db ? sqlite3_errmsg(db) : sqlite3_errstr(rc)) << std::endl;
    if (db)
      sqlite3_close(db);
    return nullptr;
  }
  // Enable referential integrity enforcement
  sqlite3_exec(db, "PRAGMA foreign_keys = ON;", nullptr, nullptr, nullptr);
  const char *schema =
      "CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY "
      "AUTOINCREMENT, timestamp TEXT "
      "DEFAULT (datetime('now')), source TEXT, note TEXT);"
      "CREATE TABLE IF NOT EXISTS methods (id INTEGER PRIMARY KEY "
      "AUTOINCREMENT, name TEXT NOT "
      "NULL, params_json TEXT, created_at TEXT DEFAULT (datetime('now')));"
      "CREATE TABLE IF NOT EXISTS examples (id INTEGER PRIMARY KEY "
      "AUTOINCREMENT, signal_id "
      "INTEGER REFERENCES signals(id), method_id INTEGER, result TEXT, "
      "confidence REAL, notes TEXT, "
      "created_at TEXT "
      "DEFAULT (datetime('now')));";
  char *err = nullptr;
  if (sqlite3_exec(db, schema, nullptr, nullptr, &err) != SQLITE_OK) {
    std::cerr << "Failed to create schema: " << err << std::endl;
    sqlite3_free(err);
  }
  return db;
}

static int insert_method(sqlite3 *db, const std::string &name,
                         const std::string &params_json) {
  sqlite3_stmt *stmt = nullptr;
  const char *sql = "INSERT INTO methods (name, params_json) VALUES (?, ?);";
  if (sqlite3_prepare_v2(db, sql, -1, &stmt, nullptr) != SQLITE_OK)
    return -1;
  sqlite3_bind_text(stmt, 1, name.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(stmt, 2, params_json.c_str(), -1, SQLITE_TRANSIENT);
  if (sqlite3_step(stmt) != SQLITE_DONE) {
    sqlite3_finalize(stmt);
    return -1;
  }
  int id = static_cast<int>(sqlite3_last_insert_rowid(db));
  sqlite3_finalize(stmt);
  return id;
}

static int insert_signal(sqlite3 *db, const std::string &source,
                         const std::string &note) {
  sqlite3_stmt *stmt = nullptr;
  const char *sql = "INSERT INTO signals (source, note) VALUES (?, ?);";
  if (sqlite3_prepare_v2(db, sql, -1, &stmt, nullptr) != SQLITE_OK) {
    std::cerr << "insert_signal prepare failed: " << sqlite3_errmsg(db)
              << std::endl;
    return -1;
  }
  sqlite3_bind_text(stmt, 1, source.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(stmt, 2, note.c_str(), -1, SQLITE_TRANSIENT);
  if (sqlite3_step(stmt) != SQLITE_DONE) {
    std::cerr << "insert_signal step failed: " << sqlite3_errmsg(db)
              << std::endl;
    sqlite3_finalize(stmt);
    return -1;
  }
  int id = static_cast<int>(sqlite3_last_insert_rowid(db));
  sqlite3_finalize(stmt);
  return id;
}

static int insert_example(sqlite3 *db, int signal_id, int method_id,
                           double confidence, const std::string &notes) {
  sqlite3_stmt *stmt = nullptr;
  const char *sql = "INSERT INTO examples (signal_id, method_id, result, "
                    "confidence, notes) VALUES (?, ?, ?, ?, ?);";
  if (sqlite3_prepare_v2(db, sql, -1, &stmt, nullptr) != SQLITE_OK)
    return -1;
  sqlite3_bind_int(stmt, 1, signal_id);
  sqlite3_bind_int(stmt, 2, method_id);
  sqlite3_bind_text(stmt, 3, "candidate", -1, SQLITE_TRANSIENT);
  sqlite3_bind_double(stmt, 4, confidence);
  sqlite3_bind_text(stmt, 5, notes.c_str(), -1, SQLITE_TRANSIENT);
  int rc = (sqlite3_step(stmt) == SQLITE_DONE) ? 0 : -1;
  sqlite3_finalize(stmt);
  return rc;
}

// ---------------------------------------------------------------------------
// Spectral / time-domain feature extraction
// ---------------------------------------------------------------------------

// Minimum amplitude to consider a sample valid (avoids noisy phase estimates).
static constexpr float kAmplitudeEpsilon = 1e-6f;

// Compute average power of the sample block.
static double compute_avg_power(const std::vector<std::complex<float>> &s) {
  if (s.empty())
    return 0.0;
  double sum = 0.0;
  for (const auto &c : s)
    sum += std::norm(c);
  return sum / s.size();
}

// Compute SNR estimate (dB) using the median as a noise-floor proxy.
// Returns the ratio signal_power/noise_floor in dB.
// A copy is used so we can sort without disturbing the original block.
static double compute_snr_db(const std::vector<std::complex<float>> &s) {
  if (s.size() < 4)
    return -999.0;
  std::vector<float> powers;
  powers.reserve(s.size());
  for (const auto &c : s)
    powers.push_back(std::norm(c));
  std::sort(powers.begin(), powers.end());
  // noise floor ≈ median; signal ≈ mean of upper quartile
  size_t n = powers.size();
  double noise = static_cast<double>(powers[n / 2]);
  if (noise < 1e-30)
    return -999.0;
  double sig_sum = 0.0;
  size_t q3 = 3 * n / 4;
  for (size_t i = q3; i < n; ++i)
    sig_sum += powers[i];
  double sig = sig_sum / static_cast<double>(n - q3);
  return 10.0 * std::log10(sig / noise);
}

// Compute PAPR (peak-to-average power ratio) in dB.
static double compute_papr_db(const std::vector<std::complex<float>> &s,
                              double avg_pow) {
  if (s.empty() || avg_pow < 1e-30)
    return 0.0;
  float peak = 0.0f;
  for (const auto &c : s) {
    float p = std::norm(c);
    if (p > peak)
      peak = p;
  }
  return 10.0 * std::log10(static_cast<double>(peak) / avg_pow);
}

// Compute spectral flatness of instantaneous power (Wiener entropy proxy).
// Returns value in [0,1]; 1 = noise-flat, 0 = tonal.
static double
compute_spectral_flatness(const std::vector<std::complex<float>> &s) {
  if (s.size() < 4)
    return 1.0;
  // Use instantaneous power samples as a proxy for the spectral envelope.
  std::vector<double> p;
  p.reserve(s.size());
  for (const auto &c : s) {
    double pw = std::norm(c);
    if (pw > 0.0)
      p.push_back(pw);
  }
  if (p.empty())
    return 1.0;
  double log_sum = 0.0;
  double lin_sum = 0.0;
  for (double v : p) {
    log_sum += std::log(v);
    lin_sum += v;
  }
  double geo_mean = std::exp(log_sum / static_cast<double>(p.size()));
  double arith_mean = lin_sum / static_cast<double>(p.size());
  return (arith_mean > 0.0) ? geo_mean / arith_mean : 1.0;
}

// Compute p50 and p90 of instantaneous power (for burst-length heuristics).
static void compute_power_percentiles(const std::vector<std::complex<float>> &s,
                                      double &p50, double &p90) {
  if (s.empty()) {
    p50 = p90 = 0.0;
    return;
  }
  std::vector<float> pw;
  pw.reserve(s.size());
  for (const auto &c : s)
    pw.push_back(std::norm(c));
  std::sort(pw.begin(), pw.end());
  size_t n = pw.size();
  p50 = static_cast<double>(pw[n / 2]);
  p90 = static_cast<double>(pw[9 * n / 10]);
}

// Compute average absolute phase increment (FM-ness indicator).
// Also returns phase transition ratio for FSK/GMSK heuristic.
static void compute_phase_stats(const std::vector<std::complex<float>> &s,
                                double &avg_abs_phase, double &trans_ratio) {
  size_t transitions = 0;
  double phase_sum = 0.0;
  size_t valid_pairs = 0;
  std::complex<float> prev = s.empty() ? std::complex<float>(0) : s[0];
  for (size_t i = 1; i < s.size(); ++i) {
    const std::complex<float> cur = s[i];
    if (std::abs(prev) < kAmplitudeEpsilon ||
        std::abs(cur) < kAmplitudeEpsilon) {
      prev = cur;
      continue;
    }
    float d = std::arg(std::conj(prev) * cur);
    phase_sum += std::abs(d);
    if (std::abs(d) > 0.5f)
      transitions++;
    ++valid_pairs;
    prev = cur;
  }
  avg_abs_phase = valid_pairs ? phase_sum / valid_pairs : 0.0;
  trans_ratio = valid_pairs ? static_cast<double>(transitions) /
                                  static_cast<double>(valid_pairs)
                            : 0.0;
}

// Compute time-occupancy: fraction of samples above the block median power.
// Values near 1.0 suggest CW; lower values suggest OOK/burst.
static double
compute_time_occupancy(const std::vector<std::complex<float>> &s) {
  if (s.size() < 4)
    return 0.0;
  std::vector<float> pw;
  pw.reserve(s.size());
  for (const auto &c : s)
    pw.push_back(std::norm(c));
  std::sort(pw.begin(), pw.end());
  float median = pw[pw.size() / 2];
  // Count samples above the median in the original vector
  size_t above = 0;
  for (const auto &c : s)
    if (std::norm(c) > median)
      ++above;
  return static_cast<double>(above) / static_cast<double>(s.size());
}

// ---------------------------------------------------------------------------
// liquid-dsp demod helper utilities (compiled only when HAVE_LIQUID is set)
// ---------------------------------------------------------------------------
#ifdef HAVE_LIQUID

// CRC-32 (IEEE 802.3 polynomial 0xEDB88320).
// Packs bits MSB-first into bytes; checks last 32 bits as expected CRC.
// Returns true when the computed CRC matches the appended 32 CRC bits,
// which is useful for validating demodulated test vectors.
static bool check_crc32_bits(const std::vector<unsigned int> &bits) {
  if (bits.size() < 64)
    return false;
  static const auto build_table = []() {
    std::array<uint32_t, 256> t{};
    for (uint32_t i = 0; i < 256; ++i) {
      uint32_t c = i;
      for (int k = 0; k < 8; ++k)
        c = (c & 1u) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
      t[i] = c;
    }
    return t;
  };
  static const auto table = build_table();

  size_t payload = bits.size() - 32;
  uint32_t crc = 0xFFFFFFFFu;
  for (size_t i = 0; i < payload; i += 8) {
    uint8_t byte = 0;
    for (int b = 0; b < 8 && (i + static_cast<size_t>(b)) < payload; ++b)
      byte = static_cast<uint8_t>((byte << 1) | (bits[i + b] & 1u));
    crc = table[(crc ^ byte) & 0xFFu] ^ (crc >> 8);
  }
  crc ^= 0xFFFFFFFFu;
  uint32_t expected = 0;
  for (size_t i = payload; i < bits.size(); ++i)
    expected = (expected << 1) | (bits[i] & 1u);
  return (crc == expected);
}

// IIR DC blocker (first-order high-pass, pole radius = 0.995).
static void apply_dc_block(std::vector<std::complex<float>> &s) {
  if (s.empty())
    return;
  std::complex<float> prev_x{0.0f, 0.0f};
  std::complex<float> prev_y{0.0f, 0.0f};
  const float alpha = 0.995f;
  for (auto &x : s) {
    std::complex<float> y = x - prev_x + alpha * prev_y;
    prev_x = x;
    prev_y = y;
    x = y;
  }
}

// Coarse CFO estimate via mean phase increment (returns Hz).
static float estimate_cfo_hz(const std::vector<std::complex<float>> &s,
                              double fs) {
  if (s.size() < 2)
    return 0.0f;
  double phase_sum = 0.0;
  for (size_t i = 1; i < s.size(); ++i)
    phase_sum +=
        static_cast<double>(std::arg(std::conj(s[i - 1]) * s[i]));
  float rad_per_samp = static_cast<float>(phase_sum /
                                          static_cast<double>(s.size() - 1));
  return rad_per_samp * static_cast<float>(fs) /
         (2.0f * static_cast<float>(M_PI));
}

// Copy std::complex<float> array into liquid_float_complex array safely.
static void to_lfc(const std::vector<std::complex<float>> &src,
                   std::vector<liquid_float_complex> &dst) {
  dst.resize(src.size());
  for (size_t i = 0; i < src.size(); ++i)
    std::memcpy(&dst[i], &src[i], sizeof(liquid_float_complex));
}

// ---------------------------------------------------------------------------
// FSK/GMSK demod chain (liquid-dsp fskdem + NCO PLL)
// ---------------------------------------------------------------------------

struct FskDemodResult {
  std::vector<unsigned int> bits;
  float cfo_hz{0.0f};
  bool crc_pass{false};
  unsigned int n_syms{0};
};

// Demodulate a block of FSK/GMSK IQ samples:
//   1. DC removal (IIR high-pass).
//   2. Coarse CFO estimate (mean phase increment).
//   3. Fine CFO correction via nco_crcf PLL.
//   4. fskdem binary FSK demodulation (k = fs/rsym, BT = fdev/fs).
//   5. CRC-32 check on recovered bits (useful for test vectors).
static FskDemodResult
demod_fsk_block(const std::vector<std::complex<float>> &s, double fs,
                double rsym, double fdev) {
  FskDemodResult res;
  if (s.empty() || rsym <= 0.0 || fs <= 0.0)
    return res;

  std::vector<std::complex<float>> sc = s;

  // 1. DC removal
  apply_dc_block(sc);

  // 2. Coarse CFO estimate
  res.cfo_hz = estimate_cfo_hz(sc, fs);
  float cfo_rad =
      res.cfo_hz * 2.0f * static_cast<float>(M_PI) / static_cast<float>(fs);

  // 3. Fine PLL: mix down by coarse CFO estimate
  nco_crcf pll = nco_crcf_create(LIQUID_NCO);
  nco_crcf_set_frequency(pll, -cfo_rad);
  nco_crcf_pll_set_bandwidth(pll, 0.01f);
  for (auto &sample : sc) {
    liquid_float_complex y;
    liquid_float_complex x;
    std::memcpy(&x, &sample, sizeof(x));
    nco_crcf_mix_down(pll, x, &y);
    std::memcpy(&sample, &y, sizeof(sample));
    nco_crcf_step(pll);
  }
  nco_crcf_destroy(pll);

  // 4. fskdem binary FSK demodulation (M=1 bit/symbol)
  unsigned int k =
      std::max(1u, static_cast<unsigned int>(std::round(fs / rsym)));
  float bw = static_cast<float>(std::clamp(fdev / fs, 1e-4, 0.45));
  fskdem dem = fskdem_create(1, k, bw);

  std::vector<liquid_float_complex> sc_lfc;
  to_lfc(sc, sc_lfc);

  res.n_syms = static_cast<unsigned int>(sc_lfc.size() / k);
  res.bits.reserve(res.n_syms);
  for (unsigned int i = 0; i < res.n_syms; ++i) {
    unsigned int sym = fskdem_demodulate(dem, &sc_lfc[i * k]);
    res.bits.push_back(sym);
  }
  fskdem_destroy(dem);

  // 5. CRC-32 check
  res.crc_pass = check_crc32_bits(res.bits);
  return res;
}

// ---------------------------------------------------------------------------
// PSK/QAM demod chain (symsync + Costas-style NCO PLL + modemcf)
// ---------------------------------------------------------------------------

struct PskDemodResult {
  std::vector<unsigned int> symbols;
  float phase_err_rms{0.0f};
  bool crc_pass{false};
  bool carrier_lock{false};
  modulation_scheme scheme{LIQUID_MODEM_QPSK};
};

// Demodulate PSK/QAM IQ samples:
//   1. Symbol timing recovery via symsync_crcf (RRC matched filter).
//   2. Carrier recovery: try QPSK/BPSK/8PSK with decision-directed NCO PLL
//      (Costas-loop equivalent); select scheme with lowest RMS phase error.
//   3. Carrier-lock watchdog: re-init PLL with wider BW on persistent error.
//   4. Downshift: QPSK → BPSK when QPSK phase error remains high.
//   5. CRC-32 check on recovered bit sequence.
static PskDemodResult
demod_psk_block(const std::vector<std::complex<float>> &s, double fs,
                double rsym) {
  PskDemodResult res;
  if (s.empty() || rsym <= 0.0 || fs <= 0.0)
    return res;

  unsigned int k =
      std::max(2u, static_cast<unsigned int>(std::round(fs / rsym)));

  // 1. Symbol timing sync
  symsync_crcf sync =
      symsync_crcf_create_rnyquist(LIQUID_FIRFILT_RRC, k, 3, 0.35f, 32);

  std::vector<liquid_float_complex> in_lfc;
  to_lfc(s, in_lfc);

  // Over-allocate output (symsync produces slightly fewer than n/k symbols)
  std::vector<liquid_float_complex> synced(s.size() / k + k * 4);
  unsigned int n_out = 0;
  symsync_crcf_execute(sync, in_lfc.data(),
                       static_cast<unsigned int>(in_lfc.size()), synced.data(),
                       &n_out);
  symsync_crcf_destroy(sync);
  if (n_out == 0)
    return res;
  synced.resize(n_out);

  // Coarse CFO from synced symbols (symbol-rate domain)
  std::vector<std::complex<float>> synced_cpp(n_out);
  for (size_t i = 0; i < n_out; ++i)
    std::memcpy(&synced_cpp[i], &synced[i], sizeof(std::complex<float>));
  float cfo_sym_hz = estimate_cfo_hz(synced_cpp, rsym);
  float cfo_rad_sym =
      cfo_sym_hz * 2.0f * static_cast<float>(M_PI) / static_cast<float>(rsym);

  // 2. Try QPSK, BPSK, 8PSK — select scheme with lowest RMS phase error
  //    (Downshift: QPSK → BPSK on persistent high phase error)
  const modulation_scheme try_schemes[] = {LIQUID_MODEM_QPSK, LIQUID_MODEM_BPSK,
                                           LIQUID_MODEM_8PSK};
  float best_err = 1e9f;
  modulation_scheme best_scheme = LIQUID_MODEM_QPSK;

  for (auto scheme : try_schemes) {
    modemcf dem = modemcf_create(scheme);
    nco_crcf nco = nco_crcf_create(LIQUID_NCO);
    nco_crcf_set_frequency(nco, -cfo_rad_sym);
    nco_crcf_pll_set_bandwidth(nco, 0.02f);

    float err_sum = 0.0f;
    for (unsigned int i = 0; i < n_out; ++i) {
      liquid_float_complex y;
      nco_crcf_mix_down(nco, synced[i], &y);
      unsigned int sym;
      modemcf_demodulate(dem, y, &sym);
      float pe = modemcf_get_demodulator_phase_error(dem);
      nco_crcf_pll_step(nco, pe);
      nco_crcf_step(nco);
      err_sum += pe * pe;
    }
    float rms = (n_out > 0) ? std::sqrt(err_sum / static_cast<float>(n_out))
                             : 1e9f;
    if (rms < best_err) {
      best_err = rms;
      best_scheme = scheme;
    }
    modemcf_destroy(dem);
    nco_crcf_destroy(nco);
  }

  // 3. Carrier-lock watchdog: if RMS phase error is high, re-init PLL with
  //    wider bandwidth (coarse acquisition mode) and retry once.
  //    PLL BW 0.08 = 4× the tracking BW (0.02) to handle initial offsets
  //    while maintaining loop stability.
  if (best_err > 0.8f) {
    modemcf dem = modemcf_create(best_scheme);
    nco_crcf nco = nco_crcf_create(LIQUID_NCO);
    nco_crcf_set_frequency(nco, -cfo_rad_sym);
    nco_crcf_pll_set_bandwidth(nco, 0.08f); // wider BW for coarse re-acquisition
    float err_sum = 0.0f;
    for (unsigned int i = 0; i < n_out; ++i) {
      liquid_float_complex y;
      nco_crcf_mix_down(nco, synced[i], &y);
      unsigned int sym;
      modemcf_demodulate(dem, y, &sym);
      float pe = modemcf_get_demodulator_phase_error(dem);
      nco_crcf_pll_step(nco, pe);
      nco_crcf_step(nco);
      err_sum += pe * pe;
    }
    float rms = (n_out > 0) ? std::sqrt(err_sum / static_cast<float>(n_out))
                             : 1e9f;
    if (rms < best_err)
      best_err = rms;
    modemcf_destroy(dem);
    nco_crcf_destroy(nco);
  }

  res.scheme = best_scheme;
  res.phase_err_rms = best_err;
  res.carrier_lock = (best_err < 0.5f);

  // 4. Final demod pass with chosen scheme
  modemcf dem = modemcf_create(best_scheme);
  nco_crcf nco = nco_crcf_create(LIQUID_NCO);
  nco_crcf_set_frequency(nco, -cfo_rad_sym);
  nco_crcf_pll_set_bandwidth(nco, 0.02f);
  res.symbols.reserve(n_out);
  for (unsigned int i = 0; i < n_out; ++i) {
    liquid_float_complex y;
    nco_crcf_mix_down(nco, synced[i], &y);
    unsigned int sym;
    modemcf_demodulate(dem, y, &sym);
    float pe = modemcf_get_demodulator_phase_error(dem);
    nco_crcf_pll_step(nco, pe);
    nco_crcf_step(nco);
    res.symbols.push_back(sym);
  }
  modemcf_destroy(dem);
  nco_crcf_destroy(nco);

  // 5. Expand symbols to bits for CRC-32
  int bps = (best_scheme == LIQUID_MODEM_BPSK)   ? 1
            : (best_scheme == LIQUID_MODEM_QPSK) ? 2
                                                 : 3;
  std::vector<unsigned int> bits;
  bits.reserve(res.symbols.size() * static_cast<size_t>(bps));
  for (unsigned int sym : res.symbols)
    for (int b = bps - 1; b >= 0; --b)
      bits.push_back((sym >> b) & 1u);
  res.crc_pass = check_crc32_bits(bits);
  return res;
}

// ---------------------------------------------------------------------------
// OOK/AM envelope demod chain
// ---------------------------------------------------------------------------

struct OokDemodResult {
  std::vector<unsigned int> bits;
  float threshold{0.0f};
  float duty_cycle{0.0f};
  bool valid{false};
};

// Demodulate OOK/AM IQ samples:
//   1. Envelope detection: |z|.
//   2. MAD-based adaptive threshold (median + 1.4826 * MAD).
//   3. Duty-cycle consistency: reject if > 0.85 (likely CW, not OOK).
//   4. Bit recovery: integrate each symbol period and threshold.
static OokDemodResult
demod_ook_block(const std::vector<std::complex<float>> &s, double fs,
                double rsym) {
  OokDemodResult res;
  if (s.empty() || rsym <= 0.0 || fs <= 0.0)
    return res;

  unsigned int k =
      std::max(1u, static_cast<unsigned int>(std::round(fs / rsym)));

  // 1. Envelope
  std::vector<float> env;
  env.reserve(s.size());
  for (const auto &c : s)
    env.push_back(std::abs(c));

  // 2. MAD threshold
  std::vector<float> sorted_env = env;
  std::sort(sorted_env.begin(), sorted_env.end());
  float med = sorted_env[sorted_env.size() / 2];
  std::vector<float> abs_dev;
  abs_dev.reserve(env.size());
  for (float e : env)
    abs_dev.push_back(std::abs(e - med));
  std::sort(abs_dev.begin(), abs_dev.end());
  float mad = abs_dev[abs_dev.size() / 2];
  // MAD threshold: 1.4826 ≈ 1/Φ⁻¹(0.75), the scale factor that makes MAD
  // a consistent estimator of the standard deviation under normality.
  res.threshold = med + 1.4826f * mad;

  // 3. Duty-cycle consistency check (> 0.85 → CW-like; reject)
  size_t above = 0;
  for (float e : env)
    if (e > res.threshold)
      ++above;
  res.duty_cycle =
      static_cast<float>(above) / static_cast<float>(env.size());
  if (res.duty_cycle > 0.85f)
    return res; // valid stays false

  // 4. Bit recovery at expected symbol rate
  size_t n_syms = env.size() / k;
  res.bits.reserve(n_syms);
  for (size_t i = 0; i < n_syms; ++i) {
    float avg = 0.0f;
    for (size_t j = 0; j < k; ++j)
      avg += env[i * k + j];
    avg /= static_cast<float>(k);
    res.bits.push_back(avg > res.threshold ? 1u : 0u);
  }
  res.valid = true;
  return res;
}

#endif // HAVE_LIQUID

// ---------------------------------------------------------------------------
// UK RTL-SDR v3 band profile table
// ---------------------------------------------------------------------------

enum class ModClass { UNKNOWN, CW_LIKE, FSK_LIKE, PSK_QAM_LIKE, OOK_AM_LIKE };

struct BandProfile {
  const char *name;        // short ID e.g. "ADS-B"
  const char *description; // human-readable label
  double center_hz;        // nominal centre frequency in Hz
  double tolerance_hz;     // ±match window when auto-detecting band
  double expected_bw_hz;   // expected occupied bandwidth (for BW guardrail)
  ModClass expected_mod;   // classifier prior hint
  double snr_min_db; // band-specific SNR gate (kBandSnrUseDefault = use global)
  double prior_boost; // score boost added to expected_mod class [0,1]
  const char *notes;  // extra info shown in decision trace / logs
};

// clang-format off
static const BandProfile UK_BANDS[] = {
  {"ADS-B",       "ADS-B 1090 MHz transponders",
   1090e6, 2e6, 1e6, ModClass::OOK_AM_LIKE, 3.0, 0.20,
   "Mode-S/ADS-B squitters at 1090 MHz. Decode with dump1090 or readsb. "
   "RTL-SDR v3 direct-sampling not needed. Very active over UK airspace."},
  {"VDL2",        "VHF Data Link Mode 2 (136.9 MHz)",
   136.9e6, 0.5e6, 25e3, ModClass::PSK_QAM_LIKE, 2.0, 0.15,
   "ACARS replacement using D8PSK at 10500 bps. Decode with acarsdec or vdlm2dec. "
   "Active on 136.900/136.925/136.950 MHz in UK."},
  {"ACARS",       "Aircraft Communications Addressing and Reporting System",
   131.725e6, 0.3e6, 8e3, ModClass::OOK_AM_LIKE, 1.0, 0.15,
   "AM-modulated VHF data link at 2400 bps. Decode with acarsdec. "
   "Primary UK frequency 131.725 MHz; also 130.025/131.550."},
  {"AIS-A",       "AIS channel A (161.975 MHz)",
   161.975e6, 0.05e6, 16e3, ModClass::FSK_LIKE, 1.0, 0.20,
   "Automatic Identification System for maritime vessels. GMSK 9600 bps. "
   "Decode with rtl-ais or AISdispatcher. Very active in coastal UK areas."},
  {"AIS-B",       "AIS channel B (162.025 MHz)",
   162.025e6, 0.05e6, 16e3, ModClass::FSK_LIKE, 1.0, 0.20,
   "AIS channel B. Same as AIS-A but on alternate channel. "
   "Both channels must be monitored for full AIS coverage."},
  {"POCSAG-153",  "POCSAG paging (153 MHz band)",
   153.35e6, 2.0e6, 12.5e3, ModClass::FSK_LIKE, 0.0, 0.18,
   "Legacy numeric/alphanumeric paging. FSK 512/1200/2400 bps. "
   "Decode with multimon-ng. Still active in UK for NHS and emergency services."},
  {"FLEX-931",    "FLEX high-speed paging (931 MHz)",
   931.9375e6, 2.0e6, 15e3, ModClass::FSK_LIKE, 1.0, 0.15,
   "FLEX 4-FSK paging at 1600/3200/6400 bps. Decode with multimon-ng. "
   "Used by NHS and commercial paging in UK."},
  {"RADIOSONDE",  "Meteorological radiosonde (400-406 MHz)",
   402.5e6, 5.0e6, 100e3, ModClass::FSK_LIKE, 2.0, 0.18,
   "Weather balloon telemetry. FSK or GFSK. Decode with radiosonde_auto_rx. "
   "UK Met Office launches from Camborne, Watnall, Lerwick, Herstmonceux."},
  {"NOAA-APT",    "NOAA weather satellite APT (137.5 MHz)",
   137.5e6, 0.2e6, 34e3, ModClass::FSK_LIKE, 1.0, 0.15,
   "Analog weather image downlink at 137.500/137.620 MHz. FM subcarrier. "
   "Decode with WXtoImg or noaa-apt. Visible passes over UK several times daily."},
  {"ISM-433",     "ISM 433 MHz band (OOK/ASK devices)",
   433.92e6, 2.0e6, 250e3, ModClass::OOK_AM_LIKE, 0.0, 0.10,
   "License-free ISM band. OOK/ASK remote controls, keyfobs, weather stations. "
   "Very busy in UK. Decode with rtl_433 for hundreds of device types."},
  {"LORA-868",    "LoRa IoT (868 MHz EU band)",
   868.1e6, 2.0e6, 500e3, ModClass::FSK_LIKE, 1.0, 0.15,
   "LoRaWAN uplink/downlink on EU868 band (863-870 MHz). CSS modulation. "
   "Decode with gr-lora or chirpstack. TTN gateways common across UK."},
  {"SMETS2",      "Smart meter SMETS2 (868.3 MHz)",
   868.3e6, 0.5e6, 200e3, ModClass::FSK_LIKE, 1.0, 0.12,
   "UK smart electricity/gas meter SMETS2 mesh network. GFSK in 868 MHz band. "
   "Mandatory in all new UK smart meter installations since 2019."},
  {"ZWAVE-868",   "Z-Wave home automation (868.42 MHz)",
   868.42e6, 0.1e6, 100e3, ModClass::FSK_LIKE, 1.0, 0.12,
   "Z-Wave home automation protocol. GFSK 100 kbps. EU frequency 868.42 MHz. "
   "Common in UK smart home devices. Decode with Z-Wave protocol analyser."},
  {"TPMS-433",    "Tyre Pressure Monitoring System (433 MHz)",
   433.92e6, 2.0e6, 100e3, ModClass::FSK_LIKE, 0.0, 0.12,
   "OBD/TPMS sensors from vehicles at 433.92 MHz. FSK or OOK. "
   "Decode with rtl_433. Active near roads and car parks."},
  {"DAB",         "DAB/DAB+ digital radio (174-240 MHz)",
   218.64e6, 36e6, 1.5e6, ModClass::PSK_QAM_LIKE, 3.0, 0.18,
   "Digital Audio Broadcasting. OFDM/DQPSK in 1.536 MHz channels (Bands III/L). "
   "UK multiplex blocks at 174-240 MHz. Decode with welle.io or dablin."},
  {"TETRA",       "TETRA public safety radio (380-430 MHz)",
   392.0e6, 20.0e6, 25e3, ModClass::PSK_QAM_LIKE, 2.0, 0.20,
   "Terrestrial Trunked Radio. PI/4-DQPSK 25 kHz channels. "
   "UK emergency services (Airwave). Decode with telive + tetra-listener."},
  {"DMR",         "DMR digital voice (446 MHz PMR446)",
   446.0e6, 10.0e6, 12.5e3, ModClass::FSK_LIKE, 1.0, 0.15,
   "Digital Mobile Radio. 4FSK (CQPSK) in 12.5 kHz channels. "
   "UK commercial/amateur use. Decode with DSDPlus or OP25."},
  {"GPS-L1",      "GPS L1 C/A (1575.42 MHz)",
   1575.42e6, 5e6, 2e6, ModClass::PSK_QAM_LIKE, -5.0, 0.10,
   "GPS civil signal at 1575.42 MHz. BPSK spread-spectrum (-130 dBm typical). "
   "RTL-SDR v3 has marginal sensitivity at L1; use LNA + active antenna for "
   "any chance of signal. Useful as frequency reference check."},
  {"APRS",        "APRS 2m packet radio (144.800 MHz)",
   144.8e6, 0.1e6, 16e3, ModClass::FSK_LIKE, -999.0, 0.18,
   "Automatic Packet Reporting System. Bell 202 AFSK 1200 bps on 144.800 MHz. "
   "Decode with direwolf: direwolf -r <file> -n 1 -b 16 -t 0 -q hd -. "
   "Very active UK frequency used by hams, weather stations, trackers."},
  {"MARINE-CH16", "Marine VHF channel 16 (156.800 MHz)",
   156.8e6, 0.025e6, 16e3, ModClass::FSK_LIKE, -999.0, 0.15,
   "International distress, safety and calling channel. FM voice. "
   "FM demod sufficient for audio monitoring. Mandatory listening channel for vessels."},
  {"MARINE-CH70", "Marine VHF DSC channel 70 (156.525 MHz)",
   156.525e6, 0.025e6, 16e3, ModClass::FSK_LIKE, -999.0, 0.15,
   "Digital Selective Calling distress and safety channel. GFSK 1200 bps. "
   "Decode with rtl-ais. Carries automated DSC distress alerts."},
  {"METEOR-LRPT", "Meteor-M LRPT satellite (137.1 MHz)",
   137.1e6, 0.15e6, 120e3, ModClass::PSK_QAM_LIKE, 3.0, 0.15,
   "Russian Meteor-M weather satellite LRPT downlink at 137.100 MHz. OQPSK 72 kbps. "
   "Decode with meteor_demod: meteor_demod -r <file> -o out.s. "
   "Requires LNA + directional antenna for reliable decodes over UK passes."},
  {"ELT-406",     "Emergency Locator Transmitter 406 MHz",
   406.028e6, 0.1e6, 12e3, ModClass::FSK_LIKE, -999.0, 0.12,
   "Aviation/maritime ELT/EPIRB/PLB distress beacons at 406.028 MHz. FSK. "
   "Decode with multimon-ng. Monitored by COSPAS-SARSAT LEO/GEO satellite network."},
  {"SIGFOX-868",  "Sigfox IoT network (868.130 MHz)",
   868.13e6, 0.1e6, 200e3, ModClass::OOK_AM_LIKE, -999.0, 0.12,
   "Sigfox LPWAN uplink at 868.130 MHz. Ultra-narrow-band OOK DBPSK. "
   "Decode with rtl_433. Used for low-power IoT devices across UK."},
  {"WMBUS-169",   "Wireless M-Bus 169 MHz",
   169.406e6, 0.1e6, 12.5e3, ModClass::FSK_LIKE, -999.0, 0.13,
   "Wireless M-Bus utility metering at 169.406 MHz (EN 13757-4 mode N). GFSK. "
   "Decode with rtl-wmbus. Used for remote utility meter reading in EU/UK."},
  {"ZIGBEE-868",  "ZigBee 868 MHz (EU channel 0)",
   868.3e6, 0.1e6, 600e3, ModClass::PSK_QAM_LIKE, -999.0, 0.12,
   "ZigBee/IEEE 802.15.4 at 868.3 MHz (EU channel 0). O-QPSK 250 kbps. "
   "Decode with whsniff + Wireshark. Used in smart home, industrial IoT."},
  {"DECT",        "DECT cordless phones (1881.792 MHz)",
   1881.792e6, 20.0e6, 1.728e6, ModClass::FSK_LIKE, -999.0, 0.15,
   "Digital Enhanced Cordless Telecommunications at 1880-1900 MHz. GFSK TDMA. "
   "Decode with dect-scanner. Very common in UK residential/office environments."},
  {"PMR446",      "PMR446 licence-free radio (446.006 MHz)",
   446.006e6, 0.5e6, 12.5e3, ModClass::FSK_LIKE, -999.0, 0.15,
   "Personal Mobile Radio 446 MHz. Analogue FM and digital DMR/dPMR. "
   "Decode digital voice with dsd. Heavily used UK licence-free walkie-talkie band."},
  {"ACARS-VHF",   "ACARS VHF aviation data (136.9 MHz)",
   136.9e6, 0.05e6, 8e3, ModClass::OOK_AM_LIKE, -999.0, 0.15,
   "Aircraft Communications Addressing and Reporting System on 136.900 MHz. AM-MSK. "
   "Decode with acarsdec. Active over UK on multiple VHF frequencies."},
  {"ISM-169",     "ISM 169 MHz sub-GHz IoT",
   169.406e6, 0.1e6, 12.5e3, ModClass::FSK_LIKE, -999.0, 0.12,
   "ISM/metering devices at 169.406 MHz. GFSK. "
   "Decode with rtl_433. Overlaps with W-MBus N-mode in the 169 MHz allocation."},
  {"IRIDIUM",     "Iridium LEO satellite (1621.250 MHz)",
   1621.25e6, 5.0e6, 100e3, ModClass::PSK_QAM_LIKE, 3.0, 0.15,
   "Iridium LEO satellite burst signals at 1616-1626 MHz. QPSK/OQPSK. "
   "Decode headless with iridium-extractor --offline -f cf32_le <file> | "
   "python3 iridium-parser.py. No GUI required."},
  {"INMARSAT-AERO", "Inmarsat Aero L-band (1545.000 MHz)",
   1545.0e6, 15.0e6, 500e3, ModClass::PSK_QAM_LIKE, 3.0, 0.13,
   "Inmarsat Aero L-band aviation satellite at 1545 MHz. BPSK/QPSK. "
   "Decode headless with satdump process inmarsat_aero_105 --baseband <file> "
   "--baseband_format cf32. JAERO is soundcard-only and must not be used."},
};
// clang-format on

static constexpr size_t kNumUkBands = sizeof(UK_BANDS) / sizeof(UK_BANDS[0]);
// Sentinel value for BandProfile::snr_min_db meaning "use the global default".
static constexpr double kBandSnrUseDefault = -999.0;

// Returns a pointer to the closest BandProfile whose centre is within
// tolerance_hz of center_hz, or nullptr if no profile matches.
// Pure function with no side effects.
static const BandProfile *find_band(double center_hz) {
  const BandProfile *best = nullptr;
  double best_dist = -1.0;
  for (size_t i = 0; i < kNumUkBands; ++i) {
    double dist = std::abs(center_hz - UK_BANDS[i].center_hz);
    if (dist <= UK_BANDS[i].tolerance_hz) {
      if (best == nullptr || dist < best_dist) {
        best = &UK_BANDS[i];
        best_dist = dist;
      }
    }
  }
  return best;
}

// ---------------------------------------------------------------------------
// Multi-class modulation classifier result
// ---------------------------------------------------------------------------

static const char *mod_class_name(ModClass m) {
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

struct ClassifierResult {
  ModClass mod_class{ModClass::UNKNOWN};
  double confidence{0.0};     // winning class confidence [0,1]
  std::string decision_trace; // human-readable feature dump
  // feature values (for logging/Prometheus)
  double snr_db{0.0};
  double avg_pow{0.0};
  double papr_db{0.0};
  double spectral_flatness{0.0};
  double time_occupancy{0.0};
  double avg_abs_phase{0.0};
  double trans_ratio{0.0};
  double p50{0.0};
  double p90{0.0};
  bool snr_gate_pass{false};
  bool bw_gate_pass{true};
  // band profile fields (empty if no profile matched)
  std::string band_name{};
  std::string band_notes{};
};

// Multi-class heuristic classifier.
// snr_min_db: SNR gate threshold (default >0 dB).
// expected_bw_hz / sample_rate: used for BW guardrail check (0 = skip check).
// papr_max_db: maximum allowable PAPR in dB (0 = disabled).
// mod_hint_class: optional prior hint boosting one class score (+0.10).
// band: optional UK band profile; when non-null applies SNR/BW overrides and
//       adds a prior_boost to the expected modulation class score.
static ClassifierResult
classify_block(const std::vector<std::complex<float>> &s, double min_power,
               double snr_min_db, double expected_bw_hz, double sample_rate,
               double papr_max_db = 0.0,
               ModClass mod_hint_class = ModClass::UNKNOWN,
               const BandProfile *band = nullptr) {
  ClassifierResult r;
  if (s.size() < 32)
    return r;

  // Apply band-profile overrides before feature extraction gates
  if (band != nullptr) {
    if (band->snr_min_db > kBandSnrUseDefault)
      snr_min_db = band->snr_min_db;
    if (expected_bw_hz <= 0.0)
      expected_bw_hz = band->expected_bw_hz;
    r.band_name = band->name;
    r.band_notes = band->notes;
  }

  // --- feature extraction ---
  r.avg_pow = compute_avg_power(s);
  r.snr_db = compute_snr_db(s);
  r.papr_db = compute_papr_db(s, r.avg_pow);
  r.spectral_flatness = compute_spectral_flatness(s);
  r.time_occupancy = compute_time_occupancy(s);
  compute_phase_stats(s, r.avg_abs_phase, r.trans_ratio);
  compute_power_percentiles(s, r.p50, r.p90);

  // --- SNR gate ---
  r.snr_gate_pass = (r.snr_db >= snr_min_db);

  // --- BW guardrail: ±25% of expected bandwidth ---
  // Approximate occupied bandwidth from time-domain power variation;
  // a simple check: if expected BW is set and sample_rate > 0, verify the
  // block's estimated BW (p90/p50 power ratio as a proxy) is within range.
  r.bw_gate_pass = true;
  if (expected_bw_hz > 0.0 && sample_rate > 0.0) {
    // Spectral flatness near 1.0 means noise-like (wideband), near 0.0 means
    // tonal (narrowband).  (1.0 - flatness) therefore approximates the fraction
    // of the sample-rate occupied by the signal.
    double est_bw_frac = std::clamp(1.0 - r.spectral_flatness, 0.01, 1.0);
    double est_bw_hz = est_bw_frac * sample_rate;
    double bw_ratio = est_bw_hz / expected_bw_hz;
    r.bw_gate_pass = (bw_ratio >= 0.75 && bw_ratio <= 1.25);
  }

  // Build decision trace header
  std::ostringstream dt;
  dt << std::fixed << std::setprecision(3) << "snr=" << r.snr_db << "dB"
     << " avg_pow=" << std::scientific << r.avg_pow << " papr=" << std::fixed
     << r.papr_db << "dB" << " flat=" << r.spectral_flatness
     << " occ=" << r.time_occupancy << " phase=" << r.avg_abs_phase
     << " trans=" << r.trans_ratio << " p50=" << std::scientific << r.p50
     << " p90=" << r.p90;

  if (!r.snr_gate_pass) {
    dt << " [REJECT:snr_gate snr=" << std::fixed << r.snr_db << "<"
       << snr_min_db << "]";
    r.decision_trace = dt.str();
    return r; // gate failed, confidence stays 0
  }
  if (!r.bw_gate_pass) {
    dt << " [REJECT:bw_gate]";
    r.decision_trace = dt.str();
    return r;
  }
  if (r.avg_pow < min_power || r.avg_pow > 1e3) {
    dt << " [REJECT:power_range]";
    r.decision_trace = dt.str();
    return r;
  }
  // --- PAPR_MAX gate (disabled when papr_max_db <= 0) ---
  if (papr_max_db > 0.0 && r.papr_db > papr_max_db) {
    dt << " [REJECT:papr_max papr=" << std::fixed << r.papr_db << ">"
       << papr_max_db << "]";
    r.decision_trace = dt.str();
    return r;
  }

  // --- Per-class scoring ---
  // CW: high time-occupancy, low PAPR, moderate-low phase activity
  double cw_score = 0.0;
  {
    double occ_s =
        std::clamp((r.time_occupancy - 0.85) / (1.0 - 0.85), 0.0, 1.0);
    double papr_s = std::clamp(1.0 - r.papr_db / 10.0, 0.0, 1.0);
    double phase_s = std::clamp(1.0 - r.avg_abs_phase / 1.5, 0.0, 1.0);
    cw_score = 0.5 * occ_s + 0.3 * papr_s + 0.2 * phase_s;
  }

  // FSK/GMSK: high phase activity, moderate flatness, moderate PAPR
  double fsk_score = 0.0;
  {
    double phase_s =
        std::clamp((r.avg_abs_phase - 0.05) / (1.2 - 0.05), 0.0, 1.0);
    double trans_s =
        std::clamp((r.trans_ratio - 0.01) / (0.5 - 0.01), 0.0, 1.0);
    double flat_s =
        std::clamp((r.spectral_flatness - 0.3) / (0.8 - 0.3), 0.0, 1.0);
    fsk_score = 0.45 * phase_s + 0.35 * trans_s + 0.20 * flat_s;
  }

  // PSK/QAM: near-constant envelope (low PAPR), high phase activity,
  //          moderate-high time occupancy
  double psk_score = 0.0;
  {
    double papr_s = std::clamp(1.0 - r.papr_db / 6.0, 0.0, 1.0);
    double phase_s =
        std::clamp((r.avg_abs_phase - 0.3) / (2.5 - 0.3), 0.0, 1.0);
    double occ_s = std::clamp((r.time_occupancy - 0.5) / (1.0 - 0.5), 0.0, 1.0);
    psk_score = 0.4 * papr_s + 0.4 * phase_s + 0.2 * occ_s;
  }

  // OOK/AM: high PAPR, low time-occupancy, low-moderate phase activity
  double ook_score = 0.0;
  {
    double papr_s = std::clamp(r.papr_db / 10.0, 0.0, 1.0);
    double occ_s = std::clamp(1.0 - r.time_occupancy / 0.6, 0.0, 1.0);
    double flat_s = std::clamp(1.0 - r.spectral_flatness, 0.0, 1.0);
    ook_score = 0.45 * papr_s + 0.35 * occ_s + 0.20 * flat_s;
  }

  // Apply band prior boost (additive, after raw scoring, before selection)
  if (band != nullptr) {
    double boost = band->prior_boost;
    switch (band->expected_mod) {
    case ModClass::CW_LIKE:
      cw_score = std::min(1.0, cw_score + boost);
      break;
    case ModClass::FSK_LIKE:
      fsk_score = std::min(1.0, fsk_score + boost);
      break;
    case ModClass::PSK_QAM_LIKE:
      psk_score = std::min(1.0, psk_score + boost);
      break;
    case ModClass::OOK_AM_LIKE:
      ook_score = std::min(1.0, ook_score + boost);
      break;
    default:
      break;
    }
    dt << " band=" << band->name << "(boost+" << std::fixed
       << std::setprecision(2) << boost << ")";
  }

  // Apply MOD_HINT prior bias (additive +0.10, independent of band profile)
  if (mod_hint_class != ModClass::UNKNOWN) {
    constexpr double kHintBoost = 0.10;
    switch (mod_hint_class) {
    case ModClass::CW_LIKE:
      cw_score = std::min(1.0, cw_score + kHintBoost);
      break;
    case ModClass::FSK_LIKE:
      fsk_score = std::min(1.0, fsk_score + kHintBoost);
      break;
    case ModClass::PSK_QAM_LIKE:
      psk_score = std::min(1.0, psk_score + kHintBoost);
      break;
    case ModClass::OOK_AM_LIKE:
      ook_score = std::min(1.0, ook_score + kHintBoost);
      break;
    default:
      break;
    }
    dt << " hint=" << mod_class_name(mod_hint_class) << "(+"
       << std::fixed << std::setprecision(2) << kHintBoost << ")";
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
  r.confidence = best;

  dt << std::fixed << std::setprecision(3) << " scores(cw=" << cw_score
     << ",fsk=" << fsk_score << ",psk=" << psk_score << ",ook=" << ook_score
     << ") -> " << mod_class_name(r.mod_class) << "@" << r.confidence;
  r.decision_trace = dt.str();
  return r;
}

static void
async_write_snapshot(const std::string &dir,
                     const std::vector<std::complex<float>> &samples,
                     double conf, uint64_t ts_ns) {
  try {
    std::filesystem::create_directories(dir);
    std::ostringstream fname;
    // filename: snap_<ts_ns>_<conf_pct>.cf32

    int conf_pct = static_cast<int>(conf * 1000.0); // thousandths

    fname << dir << "/snap_" << ts_ns << "_c" << conf_pct << ".cf32";
    std::string path = fname.str();

    std::ofstream ofs(path, std::ios::binary | std::ios::out);
    if (!ofs) {
      std::cerr << "Failed to open snapshot file " << path << " for writing\n";
      ++g_snap_errors;
      return;
    }
    // write raw CF32 interleaved complex<float> samples

    if (!samples.empty()) {
      ofs.write(reinterpret_cast<const char *>(samples.data()),
                samples.size() * sizeof(std::complex<float>));
    }
    ofs.close();
    // optional small log that a snapshot was written (this is helpful)

    std::cout << "[SNAPSHOT] wrote " << path << " samples=" << samples.size()
              << "\n";
  } catch (const std::exception &ex) {
    std::cerr << "Snapshot write exception: " << ex.what() << std::endl;
    ++g_snap_errors;
  }
}

// Delete .cf32 snapshot files older than retention_days in dir.
// Called periodically from the processing thread; no-op if dir is empty or
// retention_days is 0.
static void prune_old_snapshots(const std::string &dir, int retention_days) {
  if (dir.empty() || retention_days <= 0)
    return;
  try {
    auto cutoff = std::filesystem::file_time_type::clock::now() -
                  std::chrono::hours(static_cast<int64_t>(24) * retention_days);
    for (const auto &entry :
         std::filesystem::directory_iterator(dir)) {
      if (!entry.is_regular_file())
        continue;
      if (entry.path().extension() != ".cf32")
        continue;
      if (entry.last_write_time() < cutoff) {
        std::filesystem::remove(entry.path());
        std::cout << "[SNAPSHOT] pruned old file: " << entry.path() << "\n";
      }
    }
  } catch (const std::exception &ex) {
    std::cerr << "Snapshot prune error: " << ex.what() << std::endl;
  }
}

struct ProcMetrics {
  uint64_t frames_total{0};
  uint64_t frames_rejected{0};
  uint64_t frames_candidate{0};
  double conf_sum{0.0};
  // per-class counters
  uint64_t class_cw{0};
  uint64_t class_fsk{0};
  uint64_t class_psk{0};
  uint64_t class_ook{0};
  // error counters
  uint64_t db_errors{0};
  uint64_t snap_errors{0};
};

static void write_prometheus_metrics(const std::string &metrics_file,
                                     const ProcMetrics &m) {
  if (metrics_file.empty())
    return;
  try {
    std::filesystem::create_directories(
        std::filesystem::path(metrics_file).parent_path());
    std::ofstream ofs(metrics_file, std::ios::out | std::ios::trunc);
    if (!ofs)
      return;
    double avg_conf = m.frames_candidate > 0
                          ? m.conf_sum / static_cast<double>(m.frames_candidate)
                          : 0.0;
    ofs << "# HELP rf_frames_total Total frames processed\n"
        << "# TYPE rf_frames_total counter\n"
        << "rf_frames_total " << m.frames_total << "\n"
        << "# HELP rf_frames_rejected Frames rejected by SNR/BW/power gates\n"
        << "# TYPE rf_frames_rejected counter\n"
        << "rf_frames_rejected " << m.frames_rejected << "\n"
        << "# HELP rf_frames_candidate Frames above confidence threshold\n"
        << "# TYPE rf_frames_candidate counter\n"
        << "rf_frames_candidate " << m.frames_candidate << "\n"
        << "# HELP rf_confidence_avg Average confidence of candidate frames\n"
        << "# TYPE rf_confidence_avg gauge\n"
        << "rf_confidence_avg " << avg_conf << "\n"
        << "# HELP rf_class_frames Frames by modulation class\n"
        << "# TYPE rf_class_frames counter\n"
        << "rf_class_frames{class=\"cw_like\"} " << m.class_cw << "\n"
        << "rf_class_frames{class=\"fsk_like\"} " << m.class_fsk << "\n"
        << "rf_class_frames{class=\"psk_qam_like\"} " << m.class_psk << "\n"
        << "rf_class_frames{class=\"ook_am_like\"} " << m.class_ook << "\n"
        << "# HELP rf_errors_total Total DB and snapshot write errors\n"
        << "# TYPE rf_errors_total counter\n"
        << "rf_errors_total{type=\"db\"} " << m.db_errors << "\n"
        << "rf_errors_total{type=\"snapshot\"} " << m.snap_errors << "\n";
  } catch (...) {
  }
}

static void write_heartbeat(const std::string &hb_file) {
  if (hb_file.empty())
    return;
  try {
    std::filesystem::create_directories(
        std::filesystem::path(hb_file).parent_path());
    std::ofstream ofs(hb_file, std::ios::out | std::ios::trunc);
    if (!ofs)
      return;
    auto now = std::chrono::system_clock::now();
    auto t = std::chrono::system_clock::to_time_t(now);
    ofs << "ok " << t << "\n";
  } catch (...) {
  }
}

// Emit one JSON log line to the worker log file (appends).
static void write_json_log(const std::string &log_file,
                           const ClassifierResult &cr, uint64_t ts_ns) {
  if (log_file.empty())
    return;
  try {
    std::filesystem::create_directories(
        std::filesystem::path(log_file).parent_path());
    std::ofstream ofs(log_file, std::ios::out | std::ios::app);
    if (!ofs)
      return;
    ofs << std::fixed << std::setprecision(6) << "{\"schema_version\":\"1\""
        << ",\"ts_ns\":" << ts_ns
        << ",\"mod\":\"" << mod_class_name(cr.mod_class) << "\""
        << ",\"confidence\":" << cr.confidence << ",\"snr_db\":" << cr.snr_db
        << ",\"avg_pow\":" << std::scientific << cr.avg_pow << std::fixed
        << ",\"papr_db\":" << cr.papr_db
        << ",\"spectral_flatness\":" << cr.spectral_flatness
        << ",\"time_occupancy\":" << cr.time_occupancy
        << ",\"avg_abs_phase\":" << cr.avg_abs_phase
        << ",\"trans_ratio\":" << cr.trans_ratio
        << ",\"snr_gate_pass\":" << (cr.snr_gate_pass ? "true" : "false")
        << ",\"bw_gate_pass\":" << (cr.bw_gate_pass ? "true" : "false")
        << ",\"band\":\"" << cr.band_name << "\"" << ",\"band_notes\":\""
        << cr.band_notes << "\"" << ",\"decision_trace\":\""
        << cr.decision_trace << "\"" << "}\n";
  } catch (...) {
  }
}

// ---------------------------------------------------------------------------
// Processing thread
// ---------------------------------------------------------------------------

static void processing_thread_func(
    sqlite3 *db, double center_freq, double min_power, double conf_threshold,
    double console_conf, double snapshot_conf, const std::string &snapshot_dir,
    double snr_min_db, double expected_bw_hz, double sample_rate,
    double papr_max_db, ModClass mod_hint_class, double rsym, double fdev,
    const std::string &metrics_file, const std::string &heartbeat_file,
    const std::string &worker_log, int snapshot_retention_days) {
  // Resolve band profile once at startup
  const BandProfile *band = find_band(center_freq);
  if (band != nullptr) {
    std::cout << "[BAND] Matched: " << band->name << " | " << band->description
              << " | center=" << (band->center_hz / 1e6) << " MHz"
              << " | expected_mod=" << mod_class_name(band->expected_mod)
              << " | prior_boost=" << band->prior_boost
              << " | notes: " << band->notes << "\n";
  } else {
    std::cout << "[BAND] No profile matched for center_freq=" << center_freq
              << " Hz\n";
  }
  int method_id = insert_method(
      db, "modulation_classifier",
      R"({"type":"heuristic","version":2,"classes":["cw_like","fsk_like","psk_qam_like","ook_am_like"]})");
  if (method_id < 0) {
    std::cerr << "Failed to register method in DB; DB logging disabled\n";
  }

  ProcMetrics metrics;
  auto last_metrics_write = std::chrono::steady_clock::now();
  auto last_prune = std::chrono::steady_clock::now();

  while (running) {
    SampleBlock block;
    {
      std::unique_lock<std::mutex> lk(q_m);
      q_cv.wait_for(lk, std::chrono::milliseconds(200),
                    [] { return !q.empty() || !running.load(); });
      if (!q.empty()) {
        block = std::move(q.front());
        q.pop_front();
      } else {
        continue;
      }
    }

    ClassifierResult cr =
        classify_block(block.samples, min_power, snr_min_db, expected_bw_hz,
                       sample_rate, papr_max_db, mod_hint_class, band);
    ++metrics.frames_total;

    if (!cr.snr_gate_pass || !cr.bw_gate_pass ||
        cr.mod_class == ModClass::UNKNOWN) {
      ++metrics.frames_rejected;
    }

    // Update per-class counters
    switch (cr.mod_class) {
    case ModClass::CW_LIKE:
      ++metrics.class_cw;
      break;
    case ModClass::FSK_LIKE:
      ++metrics.class_fsk;
      break;
    case ModClass::PSK_QAM_LIKE:
      ++metrics.class_psk;
      break;
    case ModClass::OOK_AM_LIKE:
      ++metrics.class_ook;
      break;
    default:
      break;
    }

    // Persist candidate when conf > conf_threshold
    if (cr.confidence > conf_threshold) {
      ++metrics.frames_candidate;
      metrics.conf_sum += cr.confidence;

      const std::string note_str = cr.decision_trace;

      if (method_id >= 0) {
        int signal_id = insert_signal(db, "rf_adapt_intel", note_str);
        if (signal_id >= 0) {
          if (insert_example(db, signal_id, method_id, cr.confidence,
                             note_str) < 0) {
            ++metrics.db_errors;
          }
        } else {
          std::cerr << "[DB] insert_signal failed\n";
          ++metrics.db_errors;
        }
      }

      // JSON worker log
      write_json_log(worker_log, cr, block.timestamp_ns);

      // Console logging
      if (cr.confidence >= console_conf) {
        std::cout << "[DETECT] band="
                  << (cr.band_name.empty() ? "<none>" : cr.band_name)
                  << " mod=" << mod_class_name(cr.mod_class)
                  << " confidence=" << cr.confidence
                  << " trace=" << cr.decision_trace << std::endl;
      }

      // Snapshot
      if (cr.confidence >= snapshot_conf) {
        std::vector<std::complex<float>> samples_copy = block.samples;
        uint64_t ts_copy = block.timestamp_ns;
        double conf_copy = cr.confidence;
        std::string dir_copy = snapshot_dir;
        {
          std::lock_guard<std::mutex> lk(snap_m);
          snap_q.emplace_back([dir_copy, samples_copy = std::move(samples_copy),
                               conf_copy, ts_copy]() mutable {
            async_write_snapshot(dir_copy, samples_copy, conf_copy, ts_copy);
          });
        }
        snap_cv.notify_one();
      }

#ifdef HAVE_LIQUID
      // Demod pipelines: invoked after classification when confidence passes.
      // Results are logged to console; extend worker_log as needed.
      if (cr.mod_class == ModClass::FSK_LIKE) {
        // FSK/GMSK: DC removal + CFO PLL + fskdem + CRC32
        auto fsk = demod_fsk_block(block.samples, sample_rate, rsym, fdev);
        std::cout << "[DEMOD/FSK] n_syms=" << fsk.n_syms
                  << " cfo_hz=" << fsk.cfo_hz
                  << " crc=" << (fsk.crc_pass ? "PASS" : "FAIL") << "\n";
      } else if (cr.mod_class == ModClass::PSK_QAM_LIKE) {
        // PSK/QAM: symsync + Costas NCO PLL + modemcf + CRC32
        auto psk = demod_psk_block(block.samples, sample_rate, rsym);
        const char *scheme_str =
            (psk.scheme == LIQUID_MODEM_BPSK)   ? "BPSK"
            : (psk.scheme == LIQUID_MODEM_QPSK) ? "QPSK"
                                                 : "8PSK";
        std::cout << "[DEMOD/PSK] scheme=" << scheme_str
                  << " lock=" << (psk.carrier_lock ? "yes" : "no")
                  << " phase_err_rms=" << psk.phase_err_rms
                  << " crc=" << (psk.crc_pass ? "PASS" : "FAIL") << "\n";
      } else if (cr.mod_class == ModClass::OOK_AM_LIKE) {
        // OOK/AM: envelope detection + MAD threshold + duty-cycle check
        auto ook = demod_ook_block(block.samples, sample_rate, rsym);
        std::cout << "[DEMOD/OOK] n_bits=" << ook.bits.size()
                  << " duty=" << ook.duty_cycle
                  << " thresh=" << ook.threshold
                  << " valid=" << (ook.valid ? "yes" : "no") << "\n";
      }
#endif // HAVE_LIQUID
    }

    // Write Prometheus metrics and heartbeat every ~10 s
    auto now = std::chrono::steady_clock::now();
    if (std::chrono::duration_cast<std::chrono::seconds>(now -
                                                         last_metrics_write)
            .count() >= 10) {
      metrics.snap_errors = g_snap_errors.load();
      write_prometheus_metrics(metrics_file, metrics);
      write_heartbeat(heartbeat_file);
      last_metrics_write = now;
    }

    // Prune old snapshots once per hour when retention is configured
    if (snapshot_retention_days > 0) {
      if (std::chrono::duration_cast<std::chrono::hours>(now - last_prune)
              .count() >= 1) {
        prune_old_snapshots(snapshot_dir, snapshot_retention_days);
        last_prune = now;
      }
    }
  }

  // Final flush of metrics on shutdown
  metrics.snap_errors = g_snap_errors.load();
  write_prometheus_metrics(metrics_file, metrics);
  write_heartbeat(heartbeat_file);
}

static int64_t env_to_ll(const char *name, int64_t def);

static void capture_thread_func(double center_freq, double sample_rate,
                                double gain, size_t block_len,
                                int64_t read_timeout_us) {
  const int max_retries =
      static_cast<int>(env_to_ll("RF_DEVICE_RETRY_MAX", 10));
  const int retry_base_ms =
      static_cast<int>(env_to_ll("RF_DEVICE_RETRY_BASE_MS", 2000));
  // Granularity of the retry-sleep loop; keeps SIGTERM latency bounded.
  constexpr int kSignalCheckIntervalMs = 100;

  SoapySDR::Kwargs kw;
  SoapySDR::Device *dev = nullptr;
  for (int attempt = 0; attempt <= max_retries && running; ++attempt) {
    try {
      dev = SoapySDR::Device::make(kw);
      if (dev)
        break;
      std::cerr << "[capture] No SoapySDR device found\n";
    } catch (const std::exception &ex) {
      std::cerr << "[capture] SoapySDR::Device::make() threw: " << ex.what()
                << "\n";
      // On the first failure, log an actionable hint: the most common cause of
      // "No RTL-SDR devices found" when rtl_test can see the dongle is that the
      // service user lacks access to the USB device node.  The udev rule grants
      // GROUP="plugdev" MODE="0664", so the service user must be in plugdev.
      if (attempt == 0) {
        std::cerr << "[capture] Hint: if rtl_test detects the dongle but"
                     " SoapySDR cannot, the service user likely lacks USB"
                     " access.\n"
                  << "[capture] Fix: ensure rf_worker is in the plugdev group"
                     " and re-plug the dongle:\n"
                  << "[capture]   sudo usermod -aG plugdev rf_worker\n"
                  << "[capture]   sudo udevadm control --reload-rules &&"
                     " sudo udevadm trigger\n";
      }
    }
    if (attempt < max_retries && running) {
      int delay_ms = std::min(retry_base_ms * (1 << attempt), 30000);
      std::cerr << "[capture] Retrying in " << delay_ms << " ms (attempt "
                << (attempt + 1) << "/" << max_retries << ")\n";
      // Sleep in small increments so SIGTERM is handled promptly.
      for (int elapsed = 0; elapsed < delay_ms && running;
           elapsed += kSignalCheckIntervalMs)
        std::this_thread::sleep_for(
            std::chrono::milliseconds(kSignalCheckIntervalMs));
    }
  }
  if (!dev) {
    std::cerr << "[capture] Giving up: no SoapySDR device after " << max_retries
              << " retries\n";
    running = false;
    return;
  }

  dev->setSampleRate(SOAPY_SDR_RX, 0, sample_rate);
  dev->setFrequency(SOAPY_SDR_RX, 0, center_freq);
  dev->setGain(SOAPY_SDR_RX, 0, gain);

  SoapySDR::Stream *rxStream = dev->setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32);
  if (!rxStream) {
    std::cerr << "Failed to setup SoapyRX stream\n";
    SoapySDR::Device::unmake(dev);
    running = false;
    return;
  }
  int activate_rc = dev->activateStream(rxStream, 0, 0, 0);
  if (activate_rc != 0) {
    std::cerr << "activateStream failed: " << SoapySDR::errToStr(activate_rc)
              << std::endl;
    dev->closeStream(rxStream);
    SoapySDR::Device::unmake(dev);
    running = false;
    return;
  }
  std::vector<std::complex<float>> buff(block_len);
  void *buffs[1];
  buffs[0] = buff.data();

  while (running) {
    int flags = 0;
    long long ts = 0;
    auto t0 = std::chrono::steady_clock::now();
    int ret = dev->readStream(rxStream, buffs, static_cast<int>(block_len),
                              flags, ts, read_timeout_us);
    auto t1 = std::chrono::steady_clock::now();
    auto elapsed_ms =
        std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count();

    if (ret > 0) {
      SampleBlock sb;
      sb.samples.assign(buff.begin(), buff.begin() + ret);
      sb.timestamp_ns = static_cast<uint64_t>(
          std::chrono::duration_cast<std::chrono::nanoseconds>(
              std::chrono::steady_clock::now().time_since_epoch())
              .count());
      {
        std::lock_guard<std::mutex> lk(q_m);
        q.emplace_back(std::move(sb));
        while (q.size() > 64)
          q.pop_front(); // increase queue depth for adaptive mode
      }
      q_cv.notify_one();
    } else if (ret == 0) {
      std::cerr << "[readStream] ret=0 elapsed_ms=" << elapsed_ms
                << " ts=" << ts << std::endl;
      continue;
    } else {
      std::cerr << "[readStream error] code=" << ret << " str=\""
                << SoapySDR::errToStr(ret) << "\" elapsed_ms=" << elapsed_ms
                << std::endl;
      if (SoapySDR::errToStr(ret) == std::string("TIMEOUT")) {
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
      } else {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
      }
    }
  }

  dev->deactivateStream(rxStream, 0, 0);
  dev->closeStream(rxStream);
  SoapySDR::Device::unmake(dev);
}

static void sigint_handler(int) {
  running = false;
  q_cv.notify_all();
}

static int64_t env_to_ll(const char *name, int64_t def) {
  const char *v = std::getenv(name);
  if (!v)
    return def;
  try {
    return std::stoll(v);
  } catch (...) {
    return def;
  }
}
static size_t env_to_sz(const char *name, size_t def) {
  const char *v = std::getenv(name);
  if (!v)
    return def;
  try {
    return std::stoul(v);
  } catch (...) {
    return def;
  }
}
static double env_to_d(const char *name, double def) {
  const char *v = std::getenv(name);
  if (!v)
    return def;
  try {
    return std::stod(v);
  } catch (...) {
    return def;
  }
}
static std::string env_to_str(const char *name, const char *def) {
  const char *v = std::getenv(name);
  if (!v)
    return std::string(def);
  return std::string(v);
}

int main(int argc, char **argv) {
  if (argc < 4) {
    std::cerr << "Usage: " << argv[0]
              << " <center_freq_Hz> <sample_rate_Sps> <gain>\n";
    std::cerr << "Example: " << argv[0] << " 433.92e6 1000000 20\n";
    return 1;
  }

  double center_freq = std::stod(argv[1]);
  double sample_rate = std::stod(argv[2]);
  double gain = std::stod(argv[3]);

  // runtime configurable via environment (defaults chosen for Pi)

  size_t block_len = env_to_sz("RF_BLOCK_LEN", env_to_sz("BLOCK_LEN", 4096));
  int64_t read_timeout_us =
      env_to_ll("RF_READ_TIMEOUT_US", env_to_ll("READ_TIMEOUT_US", 500000));
  double min_power = env_to_d("RF_MIN_POWER", 5e-6);
  double conf_threshold = env_to_d("RF_CONF_THRESHOLD", 0.6);

  double console_conf =
      env_to_d("RF_CONSOLE_CONF", 0.8); // when to print DETECT to journal

  double snapshot_conf =
      env_to_d("RF_SNAPSHOT_CONF", 0.6); // when to save IQ snapshot

  std::string snapshot_dir =
      env_to_str("RF_SNAPSHOT_DIR", "/var/lib/rf-adapt-intel/snapshots");

  // SNR gate: >0 dB by default; set RF_SNR_MIN_DB=0 for canary (pass-through)
  double snr_min_db = env_to_d("RF_SNR_MIN_DB", 0.0);

  // Expected bandwidth in Hz for BW guardrail (0 = disabled)
  double expected_bw_hz = env_to_d("RF_EXPECTED_BW_HZ", 0.0);

  // PAPR_MAX gate: reject blocks whose PAPR exceeds this (dB); 0 = disabled
  double papr_max_db = env_to_d("PAPR_MAX", 0.0);

  // MOD_HINT: optional prior hint to bias the classifier (gmsk|fsk|psk|qam|ook|cw)
  ModClass mod_hint_class = ModClass::UNKNOWN;
  {
    std::string hint = env_to_str("MOD_HINT", "");
    if (hint == "fsk" || hint == "gmsk")
      mod_hint_class = ModClass::FSK_LIKE;
    else if (hint == "psk" || hint == "qam")
      mod_hint_class = ModClass::PSK_QAM_LIKE;
    else if (hint == "ook" || hint == "am")
      mod_hint_class = ModClass::OOK_AM_LIKE;
    else if (hint == "cw")
      mod_hint_class = ModClass::CW_LIKE;
  }

  // RSYM / FDEV: symbol rate (sps) and frequency deviation (Hz) used by demod
  // chains; defaults match ISM-433 band preset.
  double rsym = env_to_d("RSYM", 128000.0);
  double fdev = env_to_d("FDEV", 50000.0);

  // Observability paths
  std::string metrics_file =
      env_to_str("RF_METRICS_FILE", "/var/lib/rf-adapt-intel/metrics.prom");
  std::string heartbeat_file =
      env_to_str("RF_HEARTBEAT_FILE", "/var/lib/rf-adapt-intel/heartbeat");
  std::string worker_log =
      env_to_str("RF_WORKER_LOG", "/var/lib/rf-adapt-intel/worker.log");

  // Snapshot retention: delete .cf32 files older than this many days (0 = keep forever)
  int snapshot_retention_days =
      static_cast<int>(env_to_ll("RF_SNAPSHOT_RETENTION_DAYS", 0));

  std::cout << "Starting rf_adapt_intel: center=" << center_freq
            << " sps=" << sample_rate << " gain=" << gain
            << " block_len=" << block_len
            << " read_timeout_us=" << read_timeout_us
            << " conf_threshold=" << conf_threshold
            << " console_conf=" << console_conf
            << " snapshot_conf=" << snapshot_conf
            << " snapshot_dir=" << snapshot_dir << " snr_min_db=" << snr_min_db
            << " expected_bw_hz=" << expected_bw_hz
            << " papr_max_db=" << papr_max_db
            << " rsym=" << rsym << " fdev=" << fdev
            << " metrics_file=" << metrics_file << " worker_log=" << worker_log
            << " snapshot_retention_days=" << snapshot_retention_days
            << "\n";

  // Print UK_BANDS table at startup
  std::cout << "\n[BANDS] UK RTL-SDR v3 band profile table (" << kNumUkBands
            << " entries):\n";
  std::cout << std::left << std::setw(4) << "#" << std::setw(14) << "name"
            << std::setw(12) << "freq_MHz" << std::setw(12) << "tol_kHz"
            << std::setw(16) << "expected_mod" << "description\n";
  for (size_t i = 0; i < kNumUkBands; ++i) {
    const BandProfile &bp = UK_BANDS[i];
    std::cout << std::left << std::setw(4) << (i + 1) << std::setw(14)
              << bp.name << std::setw(12) << std::fixed << std::setprecision(3)
              << (bp.center_hz / 1e6) << std::setw(12) << std::fixed
              << std::setprecision(1) << (bp.tolerance_hz / 1e3)
              << std::setw(16) << mod_class_name(bp.expected_mod)
              << bp.description << "\n";
  }
  std::cout << "\n";

  sqlite3 *db = open_db("rf_adapt_intel.db");
  if (!db)
    return 1;

  std::signal(SIGINT, sigint_handler);
  std::signal(SIGTERM, sigint_handler);

  std::thread proc_th(processing_thread_func, db, center_freq, min_power,
                      conf_threshold, console_conf, snapshot_conf, snapshot_dir,
                      snr_min_db, expected_bw_hz, sample_rate, papr_max_db,
                      mod_hint_class, rsym, fdev, metrics_file, heartbeat_file,
                      worker_log, snapshot_retention_days);
  std::thread snap_th(snapshot_worker);
  std::thread cap_th(capture_thread_func, center_freq, sample_rate, gain,
                     block_len, read_timeout_us);

  while (running) {
    std::this_thread::sleep_for(std::chrono::seconds(2));
    std::lock_guard<std::mutex> lk(q_m);
    std::cout << "[STATUS] queue=" << q.size() << std::endl;
  }

  std::cout << "Shutting down...\n";
  if (cap_th.joinable())
    cap_th.join();
  if (proc_th.joinable())
    proc_th.join();
  // Signal snapshot worker to drain its queue and exit, then join cleanly
  {
    std::lock_guard<std::mutex> lk(snap_m);
    snap_running = false;
  }
  snap_cv.notify_all();
  if (snap_th.joinable())
    snap_th.join();
  sqlite3_close(db);
  return 0;
}
