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

static std::atomic<bool> running{true};

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

static void insert_example(sqlite3 *db, int signal_id, int method_id,
                           double confidence, const std::string &notes) {
  sqlite3_stmt *stmt = nullptr;
  const char *sql = "INSERT INTO examples (signal_id, method_id, result, "
                    "confidence, notes) VALUES (?, ?, ?, ?, ?);";
  if (sqlite3_prepare_v2(db, sql, -1, &stmt, nullptr) != SQLITE_OK)
    return;
  sqlite3_bind_int(stmt, 1, signal_id);
  sqlite3_bind_int(stmt, 2, method_id);
  sqlite3_bind_text(stmt, 3, "candidate", -1, SQLITE_TRANSIENT);
  sqlite3_bind_double(stmt, 4, confidence);
  sqlite3_bind_text(stmt, 5, notes.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_step(stmt);
  sqlite3_finalize(stmt);
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
// Multi-class modulation classifier result
// ---------------------------------------------------------------------------

enum class ModClass { UNKNOWN, CW_LIKE, FSK_LIKE, PSK_QAM_LIKE, OOK_AM_LIKE };

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
};

// Multi-class heuristic classifier.
// snr_min_db: SNR gate threshold (default >0 dB).
// expected_bw_hz / sample_rate: used for BW guardrail check (0 = skip check).
static ClassifierResult
classify_block(const std::vector<std::complex<float>> &s, double min_power,
               double snr_min_db, double expected_bw_hz, double sample_rate) {
  ClassifierResult r;
  if (s.size() < 32)
    return r;

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
  }
}

// ---------------------------------------------------------------------------
// Observability: Prometheus textfile + heartbeat
// ---------------------------------------------------------------------------

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
        << "rf_class_frames{class=\"ook_am_like\"} " << m.class_ook << "\n";
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
    ofs << std::fixed << std::setprecision(6) << "{\"ts_ns\":" << ts_ns
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
        << ",\"decision_trace\":\"" << cr.decision_trace << "\"" << "}\n";
  } catch (...) {
  }
}

// ---------------------------------------------------------------------------
// Processing thread
// ---------------------------------------------------------------------------

static void processing_thread_func(
    sqlite3 *db, double min_power, double conf_threshold, double console_conf,
    double snapshot_conf, const std::string &snapshot_dir, double snr_min_db,
    double expected_bw_hz, double sample_rate, const std::string &metrics_file,
    const std::string &heartbeat_file, const std::string &worker_log) {
  int method_id = insert_method(
      db, "modulation_classifier",
      R"({"type":"heuristic","version":2,"classes":["cw_like","fsk_like","psk_qam_like","ook_am_like"]})");
  if (method_id < 0) {
    std::cerr << "Failed to register method in DB; DB logging disabled\n";
  }

  ProcMetrics metrics;
  auto last_metrics_write = std::chrono::steady_clock::now();

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

    ClassifierResult cr = classify_block(block.samples, min_power, snr_min_db,
                                         expected_bw_hz, sample_rate);
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
          insert_example(db, signal_id, method_id, cr.confidence, note_str);
        }
      }

      // JSON worker log
      write_json_log(worker_log, cr, block.timestamp_ns);

      // Console logging
      if (cr.confidence >= console_conf) {
        std::cout << "[DETECT] mod=" << mod_class_name(cr.mod_class)
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
    }

    // Write Prometheus metrics and heartbeat every ~10 s
    auto now = std::chrono::steady_clock::now();
    if (std::chrono::duration_cast<std::chrono::seconds>(now -
                                                         last_metrics_write)
            .count() >= 10) {
      write_prometheus_metrics(metrics_file, metrics);
      write_heartbeat(heartbeat_file);
      last_metrics_write = now;
    }
  }

  // Final flush of metrics on shutdown
  write_prometheus_metrics(metrics_file, metrics);
  write_heartbeat(heartbeat_file);
}

static void capture_thread_func(double center_freq, double sample_rate,
                                double gain, size_t block_len,
                                int64_t read_timeout_us) {
  SoapySDR::Kwargs kw;
  SoapySDR::Device *dev = nullptr;
  try {
    dev = SoapySDR::Device::make(kw);
    if (!dev) {
      std::cerr << "No SoapySDR device found\n";
      running = false;
      return;
    }
  } catch (const std::exception &ex) {
    std::cerr << "SoapySDR::Device::make() threw: " << ex.what() << std::endl;
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

  // Observability paths
  std::string metrics_file =
      env_to_str("RF_METRICS_FILE", "/var/lib/rf-adapt-intel/metrics.prom");
  std::string heartbeat_file =
      env_to_str("RF_HEARTBEAT_FILE", "/var/lib/rf-adapt-intel/heartbeat");
  std::string worker_log =
      env_to_str("RF_WORKER_LOG", "/var/lib/rf-adapt-intel/worker.log");

  std::cout << "Starting rf_adapt_intel: center=" << center_freq
            << " sps=" << sample_rate << " gain=" << gain
            << " block_len=" << block_len
            << " read_timeout_us=" << read_timeout_us
            << " conf_threshold=" << conf_threshold
            << " console_conf=" << console_conf
            << " snapshot_conf=" << snapshot_conf
            << " snapshot_dir=" << snapshot_dir << " snr_min_db=" << snr_min_db
            << " expected_bw_hz=" << expected_bw_hz
            << " metrics_file=" << metrics_file << " worker_log=" << worker_log
            << "\n";

  sqlite3 *db = open_db("rf_adapt_intel.db");
  if (!db)
    return 1;

  std::signal(SIGINT, sigint_handler);
  std::signal(SIGTERM, sigint_handler);

  std::thread proc_th(processing_thread_func, db, min_power, conf_threshold,
                      console_conf, snapshot_conf, snapshot_dir, snr_min_db,
                      expected_bw_hz, sample_rate, metrics_file, heartbeat_file,
                      worker_log);
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
