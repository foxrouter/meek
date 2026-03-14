/*
  rf_adapt_intel — C++23 SDR capture / classification daemon
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

#ifdef HAVE_SOAPY
#include <SoapySDR/Device.h>
#include <SoapySDR/Formats.h>
#include <SoapySDR/Version.h>
#endif  // HAVE_SOAPY
#ifdef HAVE_SYSTEMD
#include <systemd/sd-daemon.h>
#else
// Minimal sd_notify fallback — implements the sd_notify(3) wire protocol via a
// Unix datagram socket without linking against libsystemd.  Returns 0 silently
// when $NOTIFY_SOCKET is unset (i.e. not running under systemd).
#include <sys/socket.h>
#include <sys/un.h>
#include <cstddef>
#include <cstdlib>
static int sd_notify(int /*unset_environment*/, const char* state) noexcept {
  const char* p = std::getenv("NOTIFY_SOCKET");
  if (!p || !*p) return 0;
  const int fd = ::socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
  if (fd < 0) return -errno;
  struct sockaddr_un sa{};
  sa.sun_family = AF_UNIX;
  const std::size_t plen = std::strlen(p);
  if (plen >= sizeof(sa.sun_path)) { ::close(fd); return -EINVAL; }
  std::memcpy(sa.sun_path, p, plen + 1);
  // Abstract-namespace sockets start with '@'; replace with the required NUL byte.
  if (sa.sun_path[0] == '@') sa.sun_path[0] = '\0';
  const socklen_t addrlen = static_cast<socklen_t>(
      offsetof(struct sockaddr_un, sun_path) +
      (sa.sun_path[0] == '\0' ? plen : plen + 1));
  const ssize_t r = ::sendto(fd, state, std::strlen(state), MSG_NOSIGNAL,
                             reinterpret_cast<const struct sockaddr*>(&sa), addrlen);
  ::close(fd);
  return r < 0 ? -errno : 1;
}
#endif  // HAVE_SYSTEMD
#include <fcntl.h>
#include <signal.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <complex>
#include <csignal>
#include <cstring>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <condition_variable>
#include <mutex>
#include <nlohmann/json.hpp>
#include <span>
#include <sstream>
#include <stop_token>
#include <string>
#include <thread>
#include <vector>

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

#ifdef HAVE_SOAPY

SoapySdrSource::SoapySdrSource(double center_freq, double sample_rate, double gain,
                               long long read_timeout_us)
    : center_freq_hz_(center_freq),
      sample_rate_hz_(sample_rate),
      read_timeout_us_(read_timeout_us) {
  SoapySDRKwargs args = {};

  // SoapySDR probes all installed driver plugins (including the ALSA audio
  // plugin) during make().  On systems without a sound card the ALSA library
  // writes benign "Invalid CTL default" / "No such file or directory" messages
  // directly to fd 2.  Suppress them by redirecting stderr to /dev/null for
  // the duration of the probe; restore it immediately afterwards so that any
  // genuine errors from the rest of the constructor remain visible.
  //
  // dup2() can be interrupted by a signal (EINTR); retry until it succeeds or
  // fails for a reason other than EINTR.
  auto dup2_eintr = [](int oldfd, int newfd) -> int {
    int r;
    do {
      r = ::dup2(oldfd, newfd);
    } while (r < 0 && errno == EINTR);
    return r;
  };
  {
    const int saved_stderr = ::dup(STDERR_FILENO);
    bool redirected = false;
    if (saved_stderr >= 0) {
      const int devnull = ::open("/dev/null", O_WRONLY | O_CLOEXEC);
      if (devnull >= 0) {
        redirected = (dup2_eintr(devnull, STDERR_FILENO) >= 0);
        ::close(devnull);
      }
    }
    dev_ = SoapySDRDevice_makeStrArgs("");
    if (saved_stderr >= 0) {
      if (redirected && dup2_eintr(saved_stderr, STDERR_FILENO) < 0) {
        // Last resort: report the failure on saved_stderr before closing it,
        // since STDERR_FILENO may still point at /dev/null.
        static constexpr char kMsg[] =
            "[WARN] rf_adapt_intel: failed to restore stderr after SoapySDR probe\n";
        if (::write(saved_stderr, kMsg, sizeof(kMsg) - 1) < 0) { /* best effort */
        }
      }
      ::close(saved_stderr);
    }
  }

  if (!dev_)
    throw std::runtime_error("SoapySDR: no device found");

  SoapySDRDevice_setSampleRate(dev_, SOAPY_SDR_RX, 0, sample_rate);
  SoapySDRDevice_setFrequency(dev_, SOAPY_SDR_RX, 0, center_freq, &args);
  SoapySDRDevice_setGainMode(dev_, SOAPY_SDR_RX, 0, 0);
  SoapySDRDevice_setGain(dev_, SOAPY_SDR_RX, 0, gain);

  stream_ = SoapySDRDevice_setupStream(dev_, SOAPY_SDR_RX, SOAPY_SDR_CF32, nullptr, 0, nullptr);
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

std::ptrdiff_t SoapySdrSource::read_samples(std::span<std::complex<float>> buf) {
  void* buffs[1] = {buf.data()};
  int flags = 0;
  long long time_ns = 0;
  const int n = SoapySDRDevice_readStream(dev_, stream_, buffs, buf.size(), &flags, &time_ns,
                                          read_timeout_us_);
  if (n == SOAPY_SDR_TIMEOUT || n == SOAPY_SDR_OVERFLOW)
    return 0;
  if (n < 0)
    return -1;
  return static_cast<std::ptrdiff_t>(n);
}

#endif  // HAVE_SOAPY

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

static void write_snapshot(const std::string& dir, std::span<const std::complex<float>> samples,
                           double conf, std::uint64_t ts_ns, const std::string& band_name,
                           std::atomic<std::uint64_t>& snap_errors) noexcept {
  try {
    std::filesystem::create_directories(dir);
    const int conf_pct = static_cast<int>(conf * 100.0);
    std::ostringstream name;
    name << dir << "/snap_" << ts_ns << "_c" << conf_pct;
    if (!band_name.empty()) {
      name << "_b" << band_name;
    }
    name << ".cf32";
    std::ofstream ofs(name.str(), std::ios::binary);
    if (!ofs) {
      snap_errors.fetch_add(1, std::memory_order_relaxed);
      return;
    }
    ofs.write(reinterpret_cast<const char*>(samples.data()),
              static_cast<std::streamsize>(samples.size() * sizeof(std::complex<float>)));
    if (!ofs.good()) {
      snap_errors.fetch_add(1, std::memory_order_relaxed);
    }
  } catch (...) {
    snap_errors.fetch_add(1, std::memory_order_relaxed);
  }
}

static void prune_old_snapshots(const std::string& dir, int retention_days) {
  if (retention_days <= 0)
    return;
  try {
    const auto cutoff =
        std::filesystem::file_time_type::clock::now() - std::chrono::hours(24 * retention_days);
    for (const auto& entry : std::filesystem::directory_iterator(dir)) {
      if (entry.path().extension() == ".cf32" && entry.last_write_time() < cutoff) {
        std::filesystem::remove(entry.path());
      }
    }
  } catch (...) {
  }
}

// ---------------------------------------------------------------------------
// JSON log helper
// ---------------------------------------------------------------------------

struct JsonLog {
  // max_bytes: rotate when log file reaches this size (0 = disabled).
  explicit JsonLog(const std::string& path,
                   std::uintmax_t max_bytes = 50ULL * 1024 * 1024)
      : path_(path), max_bytes_(max_bytes) {
    if (path_.empty()) return;
    try {
      std::filesystem::create_directories(
          std::filesystem::path(path_).parent_path());
      open_append();
    } catch (...) {
      failed_ = true;
    }
  }

  void write(const json& j) {
    if (failed_ || !ofs_) return;
    const std::string line = j.dump() + "\n";
    ofs_ << line;
    if (!ofs_.good()) {
      std::cerr << "[LOG] Write failed for worker log: " << path_ << "\n";
      failed_ = true;
      return;
    }
    bytes_written_ += line.size();
    if (max_bytes_ > 0 && bytes_written_ >= max_bytes_) {
      rotate();
    }
  }

  void flush() {
    if (ofs_) ofs_.flush();
  }

 private:
  std::string path_;
  std::ofstream ofs_;
  std::uintmax_t max_bytes_{0};
  std::uintmax_t bytes_written_{0};
  bool failed_{false};

  void open_append() {
    ofs_.open(path_, std::ios::app);
    if (!ofs_) {
      std::cerr << "[LOG] Failed to open worker log: " << path_ << "\n";
      failed_ = true;
      return;
    }
    // Track how many bytes already in the file so rotation triggers
    // correctly even on a restart mid-file.
    std::error_code ec;
    const auto sz = std::filesystem::file_size(path_, ec);
    bytes_written_ = ec ? 0 : sz;
  }

  void rotate() noexcept {
    try {
      ofs_.close();
      const std::string backup = path_ + ".1";
      // Use error_code overload to avoid throwing from a noexcept function.
      std::error_code ec;
      std::filesystem::rename(path_, backup, ec);
      if (ec) {
        std::cerr << "[LOG] Rotation rename failed (" << ec.message()
                  << "); reopening original log: " << path_ << "\n";
        // Fall back: reopen the original log so writes can continue.
        open_append();
        if (!ofs_) {
          failed_ = true;
          std::cerr << "[LOG] Fallback reopen failed for: " << path_ << "\n";
        }
        return;
      }
      open_append();
      if (!failed_ && ofs_) {
        std::cerr << "[LOG] Rotated worker log -> " << backup << "\n";
      } else {
        std::cerr << "[LOG] Rotation reopen failed for: " << path_ << "\n";
      }
    } catch (const std::exception& e) {
      std::cerr << "[LOG] Rotation failed: " << e.what() << "\n";
      failed_ = true;
    } catch (...) {
      std::cerr << "[LOG] Rotation failed: unknown exception\n";
      failed_ = true;
    }
  }
};

// ---------------------------------------------------------------------------
// Snapshot task queue — separate jthread
// ---------------------------------------------------------------------------

struct SnapTask {
  std::vector<std::complex<float>> samples;
  std::string dir;
  double conf{0.0};
  std::uint64_t ts_ns{0};
  std::string band_name;  // embedded in filename; empty when no band matched
};

// ---------------------------------------------------------------------------
// Capture thread
// ---------------------------------------------------------------------------

static void capture_loop(std::stop_token st, ISdrSource& sdr,
                         SpscRingBuffer<SampleBlock, 64>& out_buf, const Config& cfg,
                         std::atomic<std::uint64_t>& cap_dropped,
                         std::atomic<std::uint64_t>& cap_progress) {
  std::vector<std::complex<float>> buf(cfg.block_len);

  while (!st.stop_requested() && !g_shutdown.load(std::memory_order_relaxed)) {
    const auto n = sdr.read_samples(std::span{buf});
    // Update progress on every iteration; read_samples() has a 500 ms timeout,
    // so a missing update for > a few seconds indicates the thread is hung.
    cap_progress.store(
        static_cast<std::uint64_t>(
            std::chrono::steady_clock::now().time_since_epoch().count()),
        std::memory_order_relaxed);
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
    blk.timestamp_ns =
        static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
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
      // Block dropped under sustained backpressure; record the drop.
      cap_dropped.fetch_add(1, std::memory_order_relaxed);
    }
  }
}

// ---------------------------------------------------------------------------
// Processing thread
// ---------------------------------------------------------------------------

static void proc_loop(std::stop_token st, SpscRingBuffer<SampleBlock, 64>& in_buf,
                      SpscRingBuffer<ClassificationResult, 64>& out_buf, const Config& cfg,
                      std::mutex& snap_mu, std::condition_variable& snap_cv,
                      std::deque<SnapTask>& snap_queue,
                      std::atomic<std::uint64_t>& snap_dropped,
                      std::atomic<std::uint64_t>& proc_dropped,
                      std::atomic<std::uint64_t>& proc_progress) {
  const BandProfile* band = find_band(cfg.center_freq);
  if (band) {
    std::cout << "[BAND] Matched: " << band->name << " (" << band->description << ")\n";
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
  scratch.reserve(cfg.analysis_len);

  while ((!st.stop_requested() && !g_shutdown.load(std::memory_order_relaxed)) ||
         !in_buf.empty_approx()) {
    SampleBlock blk;
    if (!in_buf.pop(blk)) {
      if (st.stop_requested() || g_shutdown.load(std::memory_order_relaxed))
        break;
      // Update progress even when the queue is momentarily empty; this proves
      // the thread is spinning and not deadlocked.
      proc_progress.store(
          static_cast<std::uint64_t>(
              std::chrono::steady_clock::now().time_since_epoch().count()),
          std::memory_order_relaxed);
      std::this_thread::sleep_for(std::chrono::microseconds(100));
      continue;
    }
    proc_progress.store(
        static_cast<std::uint64_t>(
            std::chrono::steady_clock::now().time_since_epoch().count()),
        std::memory_order_relaxed);

    // Sub-window classification: slide through blk.samples in analysis_len
    // steps and keep the highest-confidence window.  When block_len ==
    // analysis_len there is exactly one iteration (original behaviour).
    // have_best ensures the first window's result (including decision_trace)
    // is always recorded, even when all windows return confidence == 0.
    ClassificationResult cr;
    std::size_t best_offset = 0;
    std::size_t best_len = 0;
    bool have_best = false;

    const std::size_t n = blk.samples.size();
    const std::size_t step = cfg.analysis_len;
    for (std::size_t off = 0; off < n; off += step) {
      const std::size_t win = std::min(step, n - off);
      if (win < kMinClassifyBlockSamples) break;
      std::span<const std::complex<float>> window{blk.samples.data() + off, win};
      ClassificationResult sub = classify_block(window, opts, scratch);
      if (!have_best || sub.confidence > cr.confidence) {
        cr = sub;
        best_offset = off;
        best_len = win;
        have_best = true;
      }
    }

    // Fallback: for too-small blocks the loop above never calls classify_block,
    // which would leave cr default-constructed with an empty decision_trace.
    // Restore the previous behaviour by classifying the full block so that
    // short reads/timeouts still produce a diagnosable reject trace.
    if (best_len == 0 && n > 0 && n < kMinClassifyBlockSamples) {
      std::span<const std::complex<float>> window{blk.samples.data(), n};
      cr = classify_block(window, opts, scratch);
      best_offset = 0;
      best_len = n;
    }
    cr.timestamp_ns = blk.timestamp_ns;
    cr.center_freq_hz = blk.center_freq_hz;
    cr.sample_rate_hz = blk.sample_rate_hz;

    // Enqueue CF32 snapshot when confidence exceeds the snapshot threshold.
    // Cap at 64 pending tasks (~2 MB) so stalled snapshot I/O cannot cause
    // unbounded memory growth.  Save only the best analysis window so the
    // snapshot contains the actual signal, not surrounding noise.
    if (cr.confidence >= cfg.snapshot_conf && cr.snr_gate_pass) {
      bool enqueued = false;
      {
        std::lock_guard lk(snap_mu);
        if (snap_queue.size() < 64) {
          SnapTask task;
          const auto snap_begin =
              blk.samples.begin() + static_cast<std::ptrdiff_t>(best_offset);
          const auto snap_end = snap_begin + static_cast<std::ptrdiff_t>(best_len);
          task.samples.assign(snap_begin, snap_end);
          task.dir = cfg.snapshot_dir;
          task.conf = cr.confidence;
          task.ts_ns = cr.timestamp_ns;
          task.band_name = cr.band_name;
          snap_queue.push_back(std::move(task));
          enqueued = true;
        } else {
          snap_dropped.fetch_add(1, std::memory_order_relaxed);
        }
      }
      if (enqueued) {
        snap_cv.notify_one();
      }
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
      proc_dropped.fetch_add(1, std::memory_order_relaxed);
      cr = ClassificationResult{};
    }
  }
}

// ---------------------------------------------------------------------------
// Output thread
// ---------------------------------------------------------------------------

static void output_loop(std::stop_token st, SpscRingBuffer<ClassificationResult, 64>& in_buf,
                        Database& db, const Config& cfg, std::atomic<std::uint64_t>& snap_errors,
                        std::atomic<std::uint64_t>& snap_dropped,
                        std::atomic<std::uint64_t>& cap_dropped,
                        std::atomic<std::uint64_t>& proc_dropped,
                        std::shared_ptr<MetricsSnapshot> metrics_snapshot) {
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
  JsonLog jlog(cfg.worker_log);

  while ((!st.stop_requested() && !g_shutdown.load(std::memory_order_relaxed)) ||
         !in_buf.empty_approx()) {
    ClassificationResult cr;
    if (!in_buf.pop(cr)) {
      if (st.stop_requested() || g_shutdown.load(std::memory_order_relaxed))
        break;
      std::this_thread::sleep_for(std::chrono::microseconds(200));
      continue;
    }

    ++metrics.frames_total;

    if (!cr.snr_gate_pass || !cr.bw_gate_pass || cr.mod_class == ModClass::UNKNOWN) {
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

    if (cr.confidence >= cfg.conf_threshold && cr.snr_gate_pass) {
      ++metrics.frames_candidate;
      metrics.conf_sum += cr.confidence;

      if (method_id >= 0) {
        const std::int64_t sig_id = db.insert_signal("rf_adapt_intel", cr.decision_trace);
        if (sig_id >= 0) {
          if (db.insert_example(sig_id, method_id, cr.confidence, cr.decision_trace) < 0) {
            ++metrics.db_errors;
          }
        } else {
          ++metrics.db_errors;
        }
      } else {
        ++metrics.db_errors;
      }

      {
        json j;
        j["schema_version"] = "2";
        j["ts_ns"]          = cr.timestamp_ns;
        j["mod"]            = mod_class_name(cr.mod_class);
        j["confidence"]     = cr.confidence;
        j["snr_db"]         = cr.snr_db;
        j["avg_power"]      = cr.avg_power;
        j["papr_db"]        = cr.papr_db;
        j["spectral_flatness"] = cr.spectral_flatness;
        j["time_occupancy"] = cr.time_occupancy;
        j["avg_abs_phase"]  = cr.avg_abs_phase;
        j["trans_ratio"]    = cr.trans_ratio;
        j["p50"]            = cr.p50;
        j["p90"]            = cr.p90;
        j["snr_gate_pass"]  = cr.snr_gate_pass;
        j["bw_gate_pass"]   = cr.bw_gate_pass;
        j["band"]           = cr.band_name;
        j["decision_trace"] = cr.decision_trace;
        jlog.write(j);
      }

      if (cr.confidence >= cfg.console_conf) {
        std::cout << "[DETECT] band=" << (cr.band_name.empty() ? "<none>" : cr.band_name)
                  << " mod=" << mod_class_name(cr.mod_class) << " conf=" << std::fixed
                  << std::setprecision(3) << cr.confidence << " snr=" << cr.snr_db << "dB\n";
      }
    }

    const auto now = std::chrono::steady_clock::now();
    if (now - last_metrics_write >= std::chrono::seconds(5)) {
      // Sync shared atomic counters (updated by other threads) into metrics.
      metrics.snap_errors = snap_errors.load(std::memory_order_relaxed);
      metrics.snap_dropped = snap_dropped.load(std::memory_order_relaxed);
      metrics.frames_cap_dropped = cap_dropped.load(std::memory_order_relaxed);
      metrics.frames_proc_dropped = proc_dropped.load(std::memory_order_relaxed);
      write_prometheus_textfile(cfg.metrics_file, metrics);
      if (metrics_snapshot) {
        std::lock_guard lk(metrics_snapshot->mu);
        metrics_snapshot->data = metrics;
      }
      jlog.flush();
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
  metrics.snap_dropped = snap_dropped.load(std::memory_order_relaxed);
  metrics.frames_cap_dropped = cap_dropped.load(std::memory_order_relaxed);
  metrics.frames_proc_dropped = proc_dropped.load(std::memory_order_relaxed);
  write_prometheus_textfile(cfg.metrics_file, metrics);
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

int main(int argc, char** argv) {
  if (argc < 4) {
    std::cerr << "Usage: " << argv[0] << " <center_freq_Hz> <sample_rate_Sps> <gain>\n"
              << "Example: " << argv[0] << " 433.92e6 1000000 20\n";
    return 1;
  }

  const Config cfg = [&]() -> Config {
    try {
      return parse_config(argc, argv);
    } catch (const std::exception& e) {
      std::cerr << "Error: " << e.what() << "\n"
                << "Usage: " << argv[0] << " <center_freq_Hz> <sample_rate_Sps> <gain>\n"
                << "Example: " << argv[0] << " 433.92e6 1000000 20\n";
      std::exit(1);
    }
  }();

  {
    struct sigaction sa{};
    sa.sa_handler = handle_term;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = SA_RESTART;
    if (sigaction(SIGINT, &sa, nullptr) != 0 ||
        sigaction(SIGTERM, &sa, nullptr) != 0) {
      const int saved_errno = errno;
      std::cerr << "[WARN] sigaction failed: " << std::strerror(saved_errno)
                << "; falling back to std::signal\n";
      std::signal(SIGINT, handle_term);
      std::signal(SIGTERM, handle_term);
    }
  }

  std::cout << "rf_adapt_intel v3 (C++23)\n"
            << "  center=" << cfg.center_freq << " Hz" << "  rate=" << cfg.sample_rate << " Sps"
            << "  gain=" << cfg.gain << "\n"
            << "  block_len=" << cfg.block_len << "  analysis_len=" << cfg.analysis_len
            << "  conf_threshold=" << cfg.conf_threshold
            << "  snr_min_db=" << cfg.snr_min_db << "\n"
            << "  db=" << cfg.db_path << "\n"
            << "  snapshots=" << cfg.snapshot_dir << "\n"
            << "  metrics=" << cfg.metrics_file << "\n"
            << "  bands loaded=" << kUkBands.size() << "\n";

  auto db = Database::open(cfg.db_path);
  if (!db) {
    const std::filesystem::path db_path_obj(cfg.db_path);
    const std::string db_dir =
        db_path_obj.has_parent_path() ? db_path_obj.parent_path().string() : std::string(".");
    std::cerr << "[FATAL] Failed to open database: " << cfg.db_path << "\n"
              << "  Ensure the directory exists and is writable by this process.\n"
              << "  Run: sudo mkdir -p " << db_dir << " && sudo chown rf_worker:rf_worker "
              << db_dir << "\n";
    return 1;
  }

  std::unique_ptr<ISdrSource> sdr;
#ifdef HAVE_SOAPY
  try {
    sdr = std::make_unique<SoapySdrSource>(cfg.center_freq, cfg.sample_rate, cfg.gain,
                                           cfg.read_timeout_us);
    std::cout << "[SDR] " << sdr->description() << " opened\n";
    sd_notify(0, "READY=1\nSTATUS=SDR device open, capturing");
  } catch (const std::exception& e) {
    std::cerr << "Failed to open SDR device: " << e.what() << "\n";
    return 1;
  }
#else
  std::cerr << "[FATAL] Built without SoapySDR support (HAVE_SOAPY is not defined).\n"
            << "  Rebuild with BUILD_HARDWARE_TARGETS=ON and SoapySDR installed.\n";
  return 1;
#endif  // HAVE_SOAPY

  SpscRingBuffer<SampleBlock, 64> cap_to_proc;
  SpscRingBuffer<ClassificationResult, 64> proc_to_out;

  // Snapshot worker — joinable background thread; uses std::deque for O(1)
  // pop_front() instead of std::vector::erase(begin()) which is O(n).
  // Queue is capped at 64 entries in proc_loop to bound memory use.
  std::mutex snap_mu;
  std::condition_variable snap_cv;
  std::deque<SnapTask> snap_queue;
  std::atomic<std::uint64_t> snap_errors{0};
  std::atomic<std::uint64_t> snap_dropped{0};
  std::atomic<std::uint64_t> cap_dropped{0};
  std::atomic<std::uint64_t> proc_dropped{0};

  // Initialise to "now" so the main loop doesn't see a stale value before the
  // threads have had a chance to run their first iteration.
  const auto startup_time_ns = static_cast<std::uint64_t>(
      std::chrono::steady_clock::now().time_since_epoch().count());
  std::atomic<std::uint64_t> cap_progress{startup_time_ns};
  std::atomic<std::uint64_t> proc_progress{startup_time_ns};

  std::jthread snap_thread([&](std::stop_token st) {
    while (!st.stop_requested()) {
      SnapTask task;
      {
        std::unique_lock<std::mutex> lk(snap_mu);
        snap_cv.wait(lk, [&] { return !snap_queue.empty() || st.stop_requested(); });
        if (snap_queue.empty()) break;
        task = std::move(snap_queue.front());
        snap_queue.pop_front();
      }
      write_snapshot(task.dir, std::span{task.samples}, task.conf, task.ts_ns, task.band_name,
                     snap_errors);
    }
    // Drain remaining tasks: move the queue out under the lock, then
    // release it before writing so proc_loop is never blocked on I/O.
    std::deque<SnapTask> remaining;
    {
      std::lock_guard lk(snap_mu);
      remaining = std::move(snap_queue);
    }
    for (auto& t : remaining) {
      write_snapshot(t.dir, std::span{t.samples}, t.conf, t.ts_ns, t.band_name, snap_errors);
    }
  });

  // Only create the shared MetricsSnapshot when the Prometheus HTTP server will
  // be used; output_loop receives nullptr otherwise and skips the mutex copy.
  std::shared_ptr<MetricsSnapshot> metrics_snapshot;
#ifdef HAVE_HTTPLIB
  if (cfg.prometheus_port > 0) {
    metrics_snapshot = std::make_shared<MetricsSnapshot>();
  }
#endif

  std::jthread cap_thread([&](std::stop_token st) {
    capture_loop(st, *sdr, cap_to_proc, cfg, cap_dropped, cap_progress);
  });

  std::jthread proc_thread([&](std::stop_token st) {
    proc_loop(st, cap_to_proc, proc_to_out, cfg, snap_mu, snap_cv, snap_queue, snap_dropped,
              proc_dropped, proc_progress);
  });

  std::jthread out_thread([&](std::stop_token st) {
    output_loop(st, proc_to_out, *db, cfg, snap_errors, snap_dropped, cap_dropped, proc_dropped,
                metrics_snapshot);
  });

#ifdef HAVE_HTTPLIB
  std::unique_ptr<httplib::Server> http_svr;
  std::thread http_thread;
  if (metrics_snapshot) {
    try {
      auto [svr, thr] = start_prometheus_http(cfg.prometheus_port, metrics_snapshot);
      if (svr && thr.joinable()) {
        std::cout << "[HTTP] Prometheus /metrics on port " << cfg.prometheus_port << "\n";
        http_svr = std::move(svr);
        http_thread = std::move(thr);
      } else {
        std::cerr << "[WARN] Failed to bind Prometheus HTTP server on port "
                  << cfg.prometheus_port << "\n";
        // If bind succeeded but thread creation somehow failed, stop the server
        // before discarding the unique_ptr.
        if (svr) svr->stop();
      }
    } catch (const std::exception& e) {
      std::cerr << "[WARN] Exception while starting Prometheus HTTP server on port "
                << cfg.prometheus_port << ": " << e.what()
                << " (continuing with textfile-only metrics)\n";
    } catch (...) {
      std::cerr << "[WARN] Unknown exception while starting Prometheus HTTP server on port "
                << cfg.prometheus_port << " (continuing with textfile-only metrics)\n";
    }
  }
#else
  if (cfg.prometheus_port > 0) {
    std::cerr << "[WARN] RF_PROMETHEUS_PORT=" << cfg.prometheus_port
              << " set but HTTP /metrics endpoint is disabled in this build "
                 "(rebuild with HAVE_HTTPLIB to enable)\n";
  }
#endif

  while (!g_shutdown.load(std::memory_order_relaxed)) {
    std::this_thread::sleep_for(std::chrono::seconds(2));
    // Only pet the watchdog when both pipeline threads have made recent
    // progress; 10 s stale threshold keeps well within WatchdogSec=30 s.
    constexpr std::uint64_t kStaleNs = 10'000'000'000ULL;  // 10 s
    const auto now_ns = static_cast<std::uint64_t>(
        std::chrono::steady_clock::now().time_since_epoch().count());
    const bool threads_healthy =
        (now_ns - cap_progress.load(std::memory_order_relaxed) < kStaleNs) &&
        (now_ns - proc_progress.load(std::memory_order_relaxed) < kStaleNs);
    if (threads_healthy) {
      sd_notify(0, "WATCHDOG=1");
    }
    std::cout << "[STATUS] cap_queue=" << cap_to_proc.size_approx()
              << " out_queue=" << proc_to_out.size_approx() << "\n";
  }

  std::cout << "Shutdown requested — stopping threads\n";
  cap_thread.request_stop();
  proc_thread.request_stop();
  out_thread.request_stop();
  snap_thread.request_stop();
  snap_cv.notify_all();
  // jthreads join at scope exit

#ifdef HAVE_HTTPLIB
  if (http_svr) {
    http_svr->stop();
    if (http_thread.joinable())
      http_thread.join();
  }
#endif

  std::cout << "rf_adapt_intel stopped\n";
  return 0;
}
