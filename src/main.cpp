/*
  rf_adapt_intel — C++20 SDR capture / classification daemon
  ─────────────────────────────────────────────────────────────
  Three std::jthread pipeline:
    capture_thread  → SpscRingBuffer<SampleBlock, 64>
                    → proc_thread
                    → SpscRingBuffer<ClassificationResult, 64>
                    → output_thread

  Signal handling:
    SIGTERM / SIGINT  → request cooperative stop on all jthreads

  Outputs:
    SQLite (WAL mode)         RF_DB_PATH
    JSON worker log           RF_WORKER_LOG
    Prometheus textfile       RF_METRICS_FILE
    Heartbeat                 RF_HEARTBEAT_FILE
    CF32 IQ snapshots         RF_SNAPSHOT_DIR
    HTTP /metrics (optional)  RF_PROMETHEUS_PORT
*/

#include <SoapySDR/Device.h>
#include <SoapySDR/Formats.h>
#include <SoapySDR/Version.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <complex>
#include <csignal>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <mutex>
#include <span>
#include <sstream>
#include <stop_token>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>

#include "meek/band_profiles.hpp"
#include "meek/classifier.hpp"
#include "meek/config.hpp"
#include "meek/db.hpp"
#include "meek/isdr_source.hpp"
#include "meek/metrics.hpp"
#include "meek/ring_buffer.hpp"
#include "meek/sample_types.hpp"

using namespace meek;
using json = nlohmann::json;

// ---------------------------------------------------------------------------
// SoapySdrSource implementation
// ---------------------------------------------------------------------------

SoapySdrSource::SoapySdrSource(double center_freq, double sample_rate,
                               double gain, long long read_timeout_us)
    : center_freq_hz_(center_freq),
      sample_rate_hz_(sample_rate),
      read_timeout_us_(read_timeout_us) {
  SoapySDRKwargs args = {};
  dev_ = SoapySDRDevice_makeStrArgs("");
  if (!dev_) throw std::runtime_error("SoapySDR: no device found");

  SoapySDRDevice_setSampleRate(dev_, SOAPY_SDR_RX, 0, sample_rate);
  SoapySDRDevice_setFrequency(dev_, SOAPY_SDR_RX, 0, center_freq, &args);
  SoapySDRDevice_setGainMode(dev_, SOAPY_SDR_RX, 0, 0);
  SoapySDRDevice_setGain(dev_, SOAPY_SDR_RX, 0, gain);

  stream_ = SoapySDRDevice_setupStream(dev_, SOAPY_SDR_RX, SOAPY_SDR_CF32,
                                       nullptr, 0, nullptr);
  if (!stream_) {
    SoapySDRDevice_unmake(dev_);
    dev_ = nullptr;
    throw std::runtime_error("SoapySDR: setupStream failed");
  }
  SoapySDRDevice_activateStream(dev_, stream_, 0, 0, 0);

  const char* drv = SoapySDRDevice_getDriverKey(dev_);
  description_ = drv ? drv : "SoapySDR";
}

SoapySdrSource::~SoapySdrSource() {
  if (stream_) {
    SoapySDRDevice_deactivateStream(dev_, stream_, 0, 0);
    SoapySDRDevice_closeStream(dev_, stream_);
    stream_ = nullptr;
  }
  if (dev_) {
    SoapySDRDevice_unmake(dev_);
    dev_ = nullptr;
  }
}

std::ptrdiff_t SoapySdrSource::read_samples(
    std::span<std::complex<float>> buf) {
  void* buffs[1] = {buf.data()};
  int flags = 0;
  long long time_ns = 0;
  const int n =
      SoapySDRDevice_readStream(dev_, stream_, buffs, buf.size(), &flags,
                                &time_ns, read_timeout_us_);
  if (n == SOAPY_SDR_TIMEOUT || n == SOAPY_SDR_OVERFLOW) return 0;
  if (n < 0) return -1;
  return static_cast<std::ptrdiff_t>(n);
}

// ---------------------------------------------------------------------------
// Signal handling
// ---------------------------------------------------------------------------

static std::atomic<bool> g_shutdown{false};

static void handle_term(int) noexcept {
  g_shutdown.store(true, std::memory_order_relaxed);
}

// ---------------------------------------------------------------------------
// Snapshot helpers
// ---------------------------------------------------------------------------

static void write_snapshot(const std::string& dir,
                            std::span<const std::complex<float>> samples,
                            double conf, std::uint64_t ts_ns,
                            std::atomic<std::uint64_t>& snap_errors) noexcept {
  try {
    std::filesystem::create_directories(dir);
    const int conf_pct = static_cast<int>(conf * 100.0);
    std::ostringstream name;
    name << dir << "/snap_" << ts_ns << "_c" << conf_pct << ".cf32";
    std::ofstream ofs(name.str(), std::ios::binary);
    if (!ofs) {
      snap_errors.fetch_add(1, std::memory_order_relaxed);
      return;
    }
    ofs.write(reinterpret_cast<const char*>(samples.data()),
              static_cast<std::streamsize>(samples.size() *
                                           sizeof(std::complex<float>)));
    if (!ofs.good()) {
      snap_errors.fetch_add(1, std::memory_order_relaxed);
    }
  } catch (...) {
    snap_errors.fetch_add(1, std::memory_order_relaxed);
  }
}

static void prune_old_snapshots(const std::string& dir, int retention_days) {
  if (retention_days <= 0) return;
  try {
    const auto cutoff =
        std::filesystem::file_time_type::clock::now() -
        std::chrono::hours(24 * retention_days);
    for (const auto& entry : std::filesystem::directory_iterator(dir)) {
      if (entry.path().extension() == ".cf32" &&
          entry.last_write_time() < cutoff) {
        std::filesystem::remove(entry.path());
      }
    }
  } catch (...) {
  }
}

// ---------------------------------------------------------------------------
// JSON log helper
// ---------------------------------------------------------------------------

static void write_json_log(const std::string& path,
                            const ClassificationResult& cr) {
  if (path.empty()) return;
  try {
    std::filesystem::create_directories(
        std::filesystem::path(path).parent_path());
    std::ofstream ofs(path, std::ios::app);
    if (!ofs) return;
    json j;
    j["schema_version"] = "2";
    j["ts_ns"] = cr.timestamp_ns;
    j["mod"] = mod_class_name(cr.mod_class);
    j["confidence"] = cr.confidence;
    j["snr_db"] = cr.snr_db;
    j["avg_power"] = cr.avg_power;
    j["papr_db"] = cr.papr_db;
    j["spectral_flatness"] = cr.spectral_flatness;
    j["time_occupancy"] = cr.time_occupancy;
    j["avg_abs_phase"] = cr.avg_abs_phase;
    j["trans_ratio"] = cr.trans_ratio;
    j["p50"] = cr.p50;
    j["p90"] = cr.p90;
    j["snr_gate_pass"] = cr.snr_gate_pass;
    j["bw_gate_pass"] = cr.bw_gate_pass;
    j["band"] = cr.band_name;
    j["decision_trace"] = cr.decision_trace;
    ofs << j.dump() << "\n";
  } catch (...) {
  }
}

// ---------------------------------------------------------------------------
// Snapshot task queue — separate jthread
// ---------------------------------------------------------------------------

struct SnapTask {
  std::vector<std::complex<float>> samples;
  std::string dir;
  double conf{0.0};
  std::uint64_t ts_ns{0};
};

// ---------------------------------------------------------------------------
// Capture thread
// ---------------------------------------------------------------------------

static void capture_loop(std::stop_token st, ISdrSource& sdr,
                          SpscRingBuffer<SampleBlock, 64>& out_buf,
                          const Config& cfg) {
  std::vector<std::complex<float>> buf(cfg.block_len);

  while (!st.stop_requested() &&
         !g_shutdown.load(std::memory_order_relaxed)) {
    const auto n = sdr.read_samples(std::span{buf});
    if (n <= 0) {
      if (n < 0) {
        std::cerr << "[CAPTURE] fatal read error\n";
        g_shutdown.store(true, std::memory_order_relaxed);
        break;
      }
      continue;
    }

    SampleBlock blk;
    blk.samples.assign(buf.begin(), buf.begin() + n);
    blk.timestamp_ns = static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::system_clock::now().time_since_epoch())
            .count());
    blk.center_freq_hz = sdr.center_freq_hz();
    blk.sample_rate_hz = sdr.sample_rate_hz();

    // Backpressure: spin-wait up to 10ms before dropping the block (100 × 100μs).
    // push(T&&) only moves from blk after confirming there is space; on false
    // return blk remains valid and can be retried.
    bool pushed = false;
    for (int retry = 0; retry < 100; ++retry) {
      if (out_buf.push(std::move(blk))) {
        pushed = true;
        break;
      }
      std::this_thread::sleep_for(std::chrono::microseconds(100));
    }
    if (!pushed) {
      // Block dropped under sustained backpressure; re-assign for next iteration.
      blk = SampleBlock{};
    }
  }
}

// ---------------------------------------------------------------------------
// Processing thread
// ---------------------------------------------------------------------------

static void proc_loop(std::stop_token st,
                      SpscRingBuffer<SampleBlock, 64>& in_buf,
                      SpscRingBuffer<ClassificationResult, 64>& out_buf,
                      const Config& cfg,
                      std::mutex& snap_mu,
                      std::deque<SnapTask>& snap_queue) {
  const BandProfile* band = find_band(cfg.center_freq);
  if (band) {
    std::cout << "[BAND] Matched: " << band->name << " (" << band->description
              << ")\n";
  }

  ClassifyOptions opts;
  opts.min_power = cfg.min_power;
  opts.snr_min_db = cfg.snr_min_db;
  opts.expected_bw_hz = cfg.expected_bw_hz;
  opts.papr_max_db = cfg.papr_max_db;
  opts.sample_rate_hz = cfg.sample_rate;
  opts.mod_hint = cfg.mod_hint;
  opts.band = band;

  std::vector<float> scratch;
  scratch.reserve(cfg.block_len);

  while (!st.stop_requested() || !in_buf.empty_approx()) {
    SampleBlock blk;
    if (!in_buf.pop(blk)) {
      if (st.stop_requested()) break;
      std::this_thread::sleep_for(std::chrono::microseconds(100));
      continue;
    }

    ClassificationResult cr =
        classify_block(std::span{blk.samples}, opts, scratch);
    cr.timestamp_ns = blk.timestamp_ns;
    cr.center_freq_hz = blk.center_freq_hz;
    cr.sample_rate_hz = blk.sample_rate_hz;

    // Enqueue CF32 snapshot when confidence exceeds the snapshot threshold.
    if (cr.confidence >= cfg.snapshot_conf && cr.snr_gate_pass) {
      SnapTask task;
      task.samples = blk.samples;  // copy raw IQ before blk is moved
      task.dir = cfg.snapshot_dir;
      task.conf = cr.confidence;
      task.ts_ns = cr.timestamp_ns;
      std::lock_guard lk(snap_mu);
      snap_queue.push_back(std::move(task));
    }

    // Backpressure: spin-wait up to 10ms before dropping the result.
    // push(T&&) only moves from cr after confirming there is space; on false
    // return cr remains valid and can be retried.
    bool pushed = false;
    for (int retry = 0; retry < 100; ++retry) {
      if (out_buf.push(std::move(cr))) {
        pushed = true;
        break;
      }
      std::this_thread::sleep_for(std::chrono::microseconds(100));
    }
    if (!pushed) {
      cr = ClassificationResult{};
    }
  }
}

// ---------------------------------------------------------------------------
// Output thread
// ---------------------------------------------------------------------------

static void output_loop(std::stop_token st,
                         SpscRingBuffer<ClassificationResult, 64>& in_buf,
                         Database& db, const Config& cfg,
                         std::atomic<std::uint64_t>& snap_errors) {
  const std::int64_t method_id = db.upsert_method(
      "modulation_classifier",
      R"({"type":"heuristic","version":3,"classes":["cw_like","fsk_like","psk_qam_like","ook_am_like"]})");
  if (method_id < 0) {
    std::cerr << "[DB] upsert_method failed: all DB writes will be skipped\n";
  }

  ProcMetrics metrics;
  auto last_metrics_write = std::chrono::steady_clock::now();
  auto last_heartbeat = std::chrono::steady_clock::now();
  auto last_prune = std::chrono::steady_clock::now();

  while (!st.stop_requested() || !in_buf.empty_approx()) {
    ClassificationResult cr;
    if (!in_buf.pop(cr)) {
      if (st.stop_requested()) break;
      std::this_thread::sleep_for(std::chrono::microseconds(200));
      continue;
    }

    ++metrics.frames_total;

    if (!cr.snr_gate_pass || !cr.bw_gate_pass ||
        cr.mod_class == ModClass::UNKNOWN) {
      ++metrics.frames_rejected;
    }

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

    if (cr.confidence > cfg.conf_threshold) {
      ++metrics.frames_candidate;
      metrics.conf_sum += cr.confidence;

      if (method_id >= 0) {
        const std::int64_t sig_id =
            db.insert_signal("rf_adapt_intel", cr.decision_trace);
        if (sig_id >= 0) {
          if (db.insert_example(sig_id, method_id, cr.confidence,
                                 cr.decision_trace) < 0) {
            ++metrics.db_errors;
          }
        } else {
          ++metrics.db_errors;
        }
      } else {
        ++metrics.db_errors;
      }

      write_json_log(cfg.worker_log, cr);

      if (cr.confidence >= cfg.console_conf) {
        std::cout << "[DETECT] band="
                  << (cr.band_name.empty() ? "<none>" : cr.band_name)
                  << " mod=" << mod_class_name(cr.mod_class) << " conf="
                  << std::fixed << std::setprecision(3) << cr.confidence
                  << " snr=" << cr.snr_db << "dB\n";
      }
    }

    const auto now = std::chrono::steady_clock::now();
    if (now - last_metrics_write >= std::chrono::seconds(5)) {
      // Sync snapshot error counter from the shared atomic into metrics.
      metrics.snap_errors = snap_errors.load(std::memory_order_relaxed);
      write_prometheus_textfile(cfg.metrics_file, metrics);
      last_metrics_write = now;
    }
    if (now - last_heartbeat >= std::chrono::seconds(30)) {
      write_heartbeat(cfg.heartbeat_file);
      last_heartbeat = now;
    }
    if (now - last_prune >= std::chrono::hours(24)) {
      prune_old_snapshots(cfg.snapshot_dir, cfg.snapshot_retention_days);
      last_prune = now;
    }
  }

  metrics.snap_errors = snap_errors.load(std::memory_order_relaxed);
  write_prometheus_textfile(cfg.metrics_file, metrics);
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

int main(int argc, char** argv) {
  if (argc < 4) {
    std::cerr << "Usage: " << argv[0]
              << " <center_freq_Hz> <sample_rate_Sps> <gain>\n"
              << "Example: " << argv[0] << " 433.92e6 1000000 20\n";
    return 1;
  }

  const Config cfg = parse_config(argc, argv);

  std::signal(SIGINT, handle_term);
  std::signal(SIGTERM, handle_term);

  std::cout << "rf_adapt_intel v3 (C++20)\n"
            << "  center=" << cfg.center_freq << " Hz"
            << "  rate=" << cfg.sample_rate << " Sps"
            << "  gain=" << cfg.gain << "\n"
            << "  block_len=" << cfg.block_len
            << "  conf_threshold=" << cfg.conf_threshold
            << "  snr_min_db=" << cfg.snr_min_db << "\n"
            << "  db=" << cfg.db_path << "\n"
            << "  snapshots=" << cfg.snapshot_dir << "\n"
            << "  metrics=" << cfg.metrics_file << "\n"
            << "  bands loaded=" << kUkBands.size() << "\n";

  auto db = Database::open(cfg.db_path);
  if (!db) {
    std::cerr << "Failed to open database: " << cfg.db_path << "\n";
    return 1;
  }

  std::unique_ptr<ISdrSource> sdr;
  try {
    sdr = std::make_unique<SoapySdrSource>(cfg.center_freq, cfg.sample_rate,
                                            cfg.gain, cfg.read_timeout_us);
    std::cout << "[SDR] " << sdr->description() << " opened\n";
  } catch (const std::exception& e) {
    std::cerr << "Failed to open SDR device: " << e.what() << "\n";
    return 1;
  }

  SpscRingBuffer<SampleBlock, 64> cap_to_proc;
  SpscRingBuffer<ClassificationResult, 64> proc_to_out;

  // Snapshot worker — joinable background thread; uses std::deque for O(1)
  // pop_front() instead of std::vector::erase(begin()) which is O(n).
  std::mutex snap_mu;
  std::deque<SnapTask> snap_queue;
  std::atomic<std::uint64_t> snap_errors{0};

  std::jthread snap_thread([&](std::stop_token st) {
    while (!st.stop_requested()) {
      SnapTask task;
      {
        std::lock_guard lk(snap_mu);
        if (!snap_queue.empty()) {
          task = std::move(snap_queue.front());
          snap_queue.pop_front();
        }
      }
      if (!task.samples.empty()) {
        write_snapshot(task.dir, std::span{task.samples}, task.conf,
                        task.ts_ns, snap_errors);
      } else {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
      }
    }
    // Drain remaining tasks: move the queue out under the lock, then
    // release it before writing so proc_loop is never blocked on I/O.
    std::deque<SnapTask> remaining;
    {
      std::lock_guard lk(snap_mu);
      remaining = std::move(snap_queue);
    }
    for (auto& t : remaining) {
      write_snapshot(t.dir, std::span{t.samples}, t.conf, t.ts_ns,
                     snap_errors);
    }
  });

  std::jthread cap_thread([&](std::stop_token st) {
    capture_loop(st, *sdr, cap_to_proc, cfg);
  });

  std::jthread proc_thread([&](std::stop_token st) {
    proc_loop(st, cap_to_proc, proc_to_out, cfg, snap_mu, snap_queue);
  });

  std::jthread out_thread([&](std::stop_token st) {
    output_loop(st, proc_to_out, *db, cfg, snap_errors);
  });

#ifndef HAVE_HTTPLIB
  if (cfg.prometheus_port > 0) {
    std::cerr << "[WARN] RF_PROMETHEUS_PORT=" << cfg.prometheus_port
              << " set but HTTP /metrics endpoint is disabled in this build "
                 "(rebuild with HAVE_HTTPLIB to enable)\n";
  }
#endif

  while (!g_shutdown.load(std::memory_order_relaxed)) {
    std::this_thread::sleep_for(std::chrono::seconds(2));
    std::cout << "[STATUS] cap_queue=" << cap_to_proc.size_approx()
              << " out_queue=" << proc_to_out.size_approx() << "\n";
  }

  std::cout << "Shutdown requested — stopping threads\n";
  cap_thread.request_stop();
  proc_thread.request_stop();
  out_thread.request_stop();
  snap_thread.request_stop();
  // jthreads join at scope exit

  std::cout << "rf_adapt_intel stopped\n";
  return 0;
}
