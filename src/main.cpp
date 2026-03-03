rf_adapt_intel — SoapySDR capture -> simple GMSK-ish detector (Hybrid mode)
  - Always stores candidate rows to DB when conf > RF_CONF_THRESHOLD
  - Only logs to console when conf >= RF_CONSOLE_CONF
  - Saves raw CF32 IQ snapshot files when conf >= RF_SNAPSHOT_CONF
  Runtime config comes from environment variables (see README /
  /etc/default/rf-adapt-intel)
*/
#include <SoapySDR/Device.hpp>
#include <SoapySDR/Errors.hpp>
#include <SoapySDR/Formats.hpp>
#include <algorithm> // clamp
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
#include <iomanip>
#include <iostream>
#include <mutex>
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

static sqlite3 *open_db(const std::string &path) {
  sqlite3 *db = nullptr;
  if (sqlite3_open(path.c_str(), &db) != SQLITE_OK) {
    std::cerr << "Cannot open DB: " << sqlite3_errmsg(db) << std::endl;
    return nullptr;
  }
  const char *schema =
      "CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY "
      "AUTOINCREMENT, timestamp TEXT "
      "DEFAULT (datetime('now')), source TEXT, note TEXT);"
      "CREATE TABLE IF NOT EXISTS methods (id INTEGER PRIMARY KEY "
      "AUTOINCREMENT, name TEXT NOT "
      "NULL, params_json TEXT, created_at TEXT DEFAULT (datetime('now')));"
      "CREATE TABLE IF NOT EXISTS examples (id INTEGER PRIMARY KEY "
      "AUTOINCREMENT, signal_id "
      "INTEGER, method_id INTEGER, result TEXT, confidence REAL, notes TEXT, "
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

static void insert_example(sqlite3 *db, int method_id, double confidence,
                           const std::string &notes) {
  sqlite3_stmt *stmt = nullptr;
  const char *sql = "INSERT INTO examples (method_id, result, confidence, "
                    "notes) VALUES (?, ?, ?, ?);";
  if (sqlite3_prepare_v2(db, sql, -1, &stmt, nullptr) != SQLITE_OK)
    return;
  sqlite3_bind_int(stmt, 1, method_id);
  sqlite3_bind_text(stmt, 2, "candidate", -1, SQLITE_TRANSIENT);
  sqlite3_bind_double(stmt, 3, confidence);
  sqlite3_bind_text(stmt, 4, notes.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_step(stmt);
  sqlite3_finalize(stmt);
}

// Simple heuristic detector: returns confidence [0,1]
static double attempt_gmsk_simple(const std::vector<std::complex<float>> &s,
                                  double min_power) {
  if (s.size() < 32)
    return 0.0;
  double sum_pow = 0.0;
  for (auto &c : s)
    sum_pow += std::norm(c);
  double avg_pow = sum_pow / s.size();

  size_t transitions = 0;
  double phase_sum = 0.0;
  std::complex<float> prev = s[0];
  for (size_t i = 1; i < s.size(); ++i) {
    std::complex<float> cur = s[i];
    float ang_prev = std::arg(prev);
    float ang_cur = std::arg(cur);
    float d = ang_cur - ang_prev;
    while (d > M_PI)
      d -= 2 * M_PI;
    while (d < -M_PI)
      d += 2 * M_PI;
    phase_sum += std::abs(d);
    if (std::abs(d) > 0.5f)
      transitions++;
    prev = cur;
  }
  double avg_abs_phase = phase_sum / (s.size() - 1);
  double trans_ratio = double(transitions) / double(s.size() - 1);

  double conf = 0.0;
  if (avg_pow > min_power && avg_pow < 1e3) {
    double pscore = std::clamp((avg_abs_phase - 0.05) / (1.2 - 0.05), 0.0, 1.0);
    double tscore = std::clamp((trans_ratio - 0.01) / (0.5 - 0.01), 0.0, 1.0);
    conf = 0.6 * pscore + 0.4 * tscore;
  }

  return conf;
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

static void processing_thread_func(sqlite3 *db, double min_power,
                                   double conf_threshold, double console_conf,
                                   double snapshot_conf,
                                   const std::string &snapshot_dir) {
  int method_id =
      insert_method(db, "gmsk_simple", R"({"type":"heuristic","version":1})");
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

    // compute actual average power for logging and tuning

    double sum_pow = 0.0;
    for (auto &c : block.samples)
      sum_pow += std::norm(c);
    double avg_pow =
        block.samples.empty() ? 0.0 : (sum_pow / block.samples.size());

    double conf = attempt_gmsk_simple(block.samples, min_power);

    // Always persist candidate when conf > conf_threshold

    if (conf > conf_threshold) {
      std::ostringstream note;
      note << "avg_pow=" << std::setprecision(3) << std::scientific << avg_pow
           << " ts=" << block.timestamp_ns;
      insert_example(db, method_id, conf, note.str());

      // Console logging controlled by console_conf

      if (conf >= console_conf) {
        std::cout << "[DETECT] confidence=" << conf << " notes=" << note.str()
                  << std::endl;
      }

      // Snapshot controlled by snapshot_conf

      if (conf >= snapshot_conf) {
        // copy samples for async write to avoid blocking processing

        std::vector<std::complex<float>> samples_copy = block.samples;
        uint64_t ts_copy = block.timestamp_ns;
        double conf_copy = conf;
        std::string dir_copy = snapshot_dir;
        std::thread t([dir_copy, samples_copy = std::move(samples_copy),
                       conf_copy, ts_copy]() mutable {
          async_write_snapshot(dir_copy, samples_copy, conf_copy, ts_copy);
        });
        t.detach();
      }
    }
  }
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
  dev->activateStream(rxStream, 0, 0, 0);

  std::vector<std::complex<float>> buff(block_len);
  void *buffs[1];
  buffs[0] = buff.data();

  while (running) {
    int flags = 0;
    int64_t ts = 0;
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

  std::cout << "Starting rf_adapt_intel: center=" << center_freq
            << " sps=" << sample_rate << " gain=" << gain
            << " block_len=" << block_len
            << " read_timeout_us=" << read_timeout_us
            << " conf_threshold=" << conf_threshold
            << " console_conf=" << console_conf
            << " snapshot_conf=" << snapshot_conf
            << " snapshot_dir=" << snapshot_dir << "\n";

  sqlite3 *db = open_db("rf_adapt_intel.db");
  if (!db)
    return 1;

  std::signal(SIGINT, sigint_handler);
  std::signal(SIGTERM, sigint_handler);

  std::thread proc_th(processing_thread_func, db, min_power, conf_threshold,
                      console_conf, snapshot_conf, snapshot_dir);
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
  sqlite3_close(db);
  return 0;
