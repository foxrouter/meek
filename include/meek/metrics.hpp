// include/meek/metrics.hpp — Prometheus metrics for rf_adapt_intel.
//
// ProcMetrics holds all counters/gauges updated by the output thread.
// write_prometheus_textfile() serialises them in Prometheus text exposition
// format (v0.0.4).
//
// Optionally, start_prometheus_http() spins up a minimal cpp-httplib server
// on a given port serving GET /metrics (compiled only when HAVE_HTTPLIB is
// defined).

#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <string>

// httplib.h must be included at global scope (outside any namespace) to avoid
// unqualified-lookup failures inside template instantiations on GCC/aarch64.
// Wrapping it in a namespace (e.g. namespace meek) causes errors such as
// "'decode_url' was not declared in this scope".
#ifdef HAVE_HTTPLIB
#include <httplib.h>
#include <memory>
#include <mutex>
#include <sstream>
#include <thread>
#include <utility>
#endif  // HAVE_HTTPLIB

namespace meek {

// ---------------------------------------------------------------------------
// Metrics store (updated from a single thread; no locking needed)
// ---------------------------------------------------------------------------

struct ProcMetrics {
  std::uint64_t frames_total{0};
  std::uint64_t frames_rejected{0};
  std::uint64_t frames_candidate{0};
  double conf_sum{0.0};
  std::uint64_t class_cw{0};
  std::uint64_t class_fsk{0};
  std::uint64_t class_psk{0};
  std::uint64_t class_ook{0};
  std::uint64_t db_errors{0};
  std::uint64_t snap_errors{0};
};

// ---------------------------------------------------------------------------
// Textfile output (for node_exporter textfile collector or canary scripts)
// ---------------------------------------------------------------------------

inline void write_prometheus_textfile(const std::string& path,
                                      const ProcMetrics& m) {
  if (path.empty()) return;
  try {
    std::filesystem::create_directories(std::filesystem::path(path).parent_path());
    std::ofstream ofs(path, std::ios::out | std::ios::trunc);
    if (!ofs) return;

    const double avg_conf = m.frames_candidate > 0
                                ? m.conf_sum / static_cast<double>(m.frames_candidate)
                                : 0.0;

    ofs << "# HELP rf_frames_total Total IQ frames processed by classifier\n"
        << "# TYPE rf_frames_total counter\n"
        << "rf_frames_total " << m.frames_total << "\n"
        << "# HELP rf_classifications_total Frames classified by modulation type\n"
        << "# TYPE rf_classifications_total counter\n"
        << "rf_classifications_total{class=\"cw_like\"} " << m.class_cw << "\n"
        << "rf_classifications_total{class=\"fsk_like\"} " << m.class_fsk << "\n"
        << "rf_classifications_total{class=\"psk_qam_like\"} " << m.class_psk << "\n"
        << "rf_classifications_total{class=\"ook_am_like\"} " << m.class_ook << "\n"
        << "# HELP rf_frames_rejected Frames rejected by SNR/BW/power gates\n"
        << "# TYPE rf_frames_rejected counter\n"
        << "rf_frames_rejected " << m.frames_rejected << "\n"
        << "# HELP rf_frames_candidate Frames above confidence threshold\n"
        << "# TYPE rf_frames_candidate counter\n"
        << "rf_frames_candidate " << m.frames_candidate << "\n"
        << "# HELP rf_confidence_avg Average confidence of candidate frames\n"
        << "# TYPE rf_confidence_avg gauge\n"
        << "rf_confidence_avg " << avg_conf << "\n"
        << "# HELP rf_errors_total Total write errors\n"
        << "# TYPE rf_errors_total counter\n"
        << "rf_errors_total{type=\"db\"} " << m.db_errors << "\n"
        << "rf_errors_total{type=\"snapshot\"} " << m.snap_errors << "\n";
  } catch (...) {
  }
}

// ---------------------------------------------------------------------------
// Heartbeat file
// ---------------------------------------------------------------------------

inline void write_heartbeat(const std::string& path) {
  if (path.empty()) return;
  try {
    std::filesystem::create_directories(std::filesystem::path(path).parent_path());
    std::ofstream ofs(path, std::ios::out | std::ios::trunc);
    if (!ofs) return;
    const auto t = std::chrono::system_clock::to_time_t(std::chrono::system_clock::now());
    ofs << "ok " << t << "\n";
  } catch (...) {
  }
}

// ---------------------------------------------------------------------------
// Optional HTTP server (cpp-httplib) — compiled only with HAVE_HTTPLIB
// ---------------------------------------------------------------------------

#ifdef HAVE_HTTPLIB

/// Thread-safe snapshot of metrics for the HTTP server.
struct MetricsSnapshot {
  std::mutex mu;
  ProcMetrics data;
};

/// Starts a background HTTP server on the given port serving GET /metrics.
/// Binds the port synchronously; listening runs on a background std::thread.
/// Returns {server, thread} on success or {nullptr, thread{}} if bind fails.
[[nodiscard]] inline std::pair<std::unique_ptr<httplib::Server>, std::thread>
start_prometheus_http(std::uint16_t port,
                      std::shared_ptr<MetricsSnapshot> snapshot) {
  auto svr = std::make_unique<httplib::Server>();
  svr->Get("/metrics", [snapshot](const httplib::Request&, httplib::Response& res) {
    ProcMetrics snap;
    {
      std::lock_guard lk(snapshot->mu);
      snap = snapshot->data;
    }
    std::ostringstream body;
    const double avg_conf = snap.frames_candidate > 0
                                ? snap.conf_sum / static_cast<double>(snap.frames_candidate)
                                : 0.0;
    body << "# HELP rf_frames_total Total IQ frames processed by classifier\n"
         << "# TYPE rf_frames_total counter\n"
         << "rf_frames_total " << snap.frames_total << "\n"
         << "# HELP rf_classifications_total Frames classified\n"
         << "# TYPE rf_classifications_total counter\n"
         << "rf_classifications_total{class=\"cw_like\"} " << snap.class_cw << "\n"
         << "rf_classifications_total{class=\"fsk_like\"} " << snap.class_fsk << "\n"
         << "rf_classifications_total{class=\"psk_qam_like\"} " << snap.class_psk << "\n"
         << "rf_classifications_total{class=\"ook_am_like\"} " << snap.class_ook << "\n"
         << "rf_frames_rejected " << snap.frames_rejected << "\n"
         << "rf_frames_candidate " << snap.frames_candidate << "\n"
         << "rf_confidence_avg " << avg_conf << "\n"
         << "rf_errors_total{type=\"db\"} " << snap.db_errors << "\n"
         << "rf_errors_total{type=\"snapshot\"} " << snap.snap_errors << "\n";
    res.set_content(body.str(), "text/plain; version=0.0.4; charset=utf-8");
  });

  if (!svr->bind_to_port("0.0.0.0", static_cast<int>(port))) {
    return {std::unique_ptr<httplib::Server>(), std::thread{}};
  }

  // listen_after_bind() is blocking; run it on a background thread.
  httplib::Server* raw = svr.get();
  std::thread listener([raw]() { raw->listen_after_bind(); });
  return {std::move(svr), std::move(listener)};
}
#endif  // HAVE_HTTPLIB

}  // namespace meek
