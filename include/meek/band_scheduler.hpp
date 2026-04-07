// include/meek/band_scheduler.hpp — Band rotation scheduler for meek.
//
// BandScheduler rotates the SDR centre frequency through a list of BandSlots
// on a configurable dwell schedule.  It is owned by capture_loop and is
// entirely single-threaded (no mutex required).
//
// Usage:
//   auto sched = BandScheduler::from_env();   // build from env vars
//   if (sched.enabled()) {
//     while (running) {
//       if (auto slot = sched.tick(steady_clock::now())) {
//         retune_sdr(*slot);
//       }
//       // ... capture block ...
//     }
//   }

#pragma once

#include <chrono>
#include <cstddef>
#include <cstdlib>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace meek {

// ---------------------------------------------------------------------------
// BandSlot — one entry in the rotation schedule
// ---------------------------------------------------------------------------

struct BandSlot {
  double center_hz;                    // SDR centre frequency to tune to (Hz)
  std::chrono::milliseconds dwell_ms;  // how long to dwell on this slot
};

// ---------------------------------------------------------------------------
// BandScheduler — single-threaded band rotation controller
// ---------------------------------------------------------------------------

class BandScheduler {
 public:
  /// Default-construct in disabled state.  tick() always returns nullopt.
  BandScheduler() noexcept = default;
  ~BandScheduler() = default;

  BandScheduler(const BandScheduler&) = delete;
  BandScheduler& operator=(const BandScheduler&) = delete;
  BandScheduler(BandScheduler&&) noexcept = default;
  BandScheduler& operator=(BandScheduler&&) noexcept = default;

  /// Build a BandScheduler from environment variables.
  ///
  /// RF_SCHED_BANDS    — comma-separated centre frequencies in Hz
  ///                     (e.g. "433920000,868100000,144800000").
  ///                     Scheduling is enabled only when >= 2 valid entries
  ///                     are present.
  /// RF_SCHED_DWELL_MS — per-slot dwell duration in milliseconds (default 10000).
  ///                     A single value applies uniformly to all slots.
  ///                     If unset, unparsable, or non-positive, the default
  ///                     dwell is retained.
  ///
  /// Malformed frequency tokens are silently skipped.  If fewer than 2 valid
  /// slots remain the scheduler is returned in the disabled state.
  [[nodiscard]] static BandScheduler from_env();

  /// Returns true when band rotation is active (>= 2 slots loaded).
  [[nodiscard]] bool enabled() const noexcept {
    return enabled_;
  }

  /// Returns the number of rotation slots.
  [[nodiscard]] std::size_t slot_count() const noexcept {
    return slots_.size();
  }

  /// Returns a reference to the current BandSlot.
  /// Behaviour is undefined when enabled() is false.
  [[nodiscard]] const BandSlot& current() const noexcept {
    return slots_[current_idx_];
  }

  /// Advance to the next slot if the current dwell period has elapsed.
  ///
  /// Returns the newly selected BandSlot when a transition occurs, or
  /// std::nullopt if the dwell has not yet elapsed or scheduling is disabled.
  ///
  /// The caller is responsible for retuning the SDR hardware to
  /// slot->center_hz when a non-null value is returned.
  [[nodiscard]] std::optional<BandSlot> tick(std::chrono::steady_clock::time_point now) noexcept;

 private:
  explicit BandScheduler(std::vector<BandSlot> slots,
                         std::chrono::steady_clock::time_point start) noexcept;

  std::vector<BandSlot> slots_;
  std::size_t current_idx_{0};
  std::chrono::steady_clock::time_point dwell_start_{};
  bool enabled_{false};
};

// ---------------------------------------------------------------------------
// Inline implementation
// ---------------------------------------------------------------------------

inline BandScheduler::BandScheduler(std::vector<BandSlot> slots,
                                    std::chrono::steady_clock::time_point start) noexcept
    : slots_(std::move(slots)), current_idx_(0), dwell_start_(start), enabled_(true) {}

inline std::optional<BandSlot> BandScheduler::tick(
    std::chrono::steady_clock::time_point now) noexcept {
  if (!enabled_)
    return std::nullopt;
  if (now - dwell_start_ < slots_[current_idx_].dwell_ms)
    return std::nullopt;
  current_idx_ = (current_idx_ + 1) % slots_.size();
  dwell_start_ = now;
  return slots_[current_idx_];
}

inline BandScheduler BandScheduler::from_env() {
  using ms = std::chrono::milliseconds;

  ms dwell{10'000};
  if (const char* env = std::getenv("RF_SCHED_DWELL_MS"); env && *env != '\0') {
    try {
      const long long v = std::stoll(env);
      if (v > 0)
        dwell = ms{v};
    } catch (...) {
    }
  }

  const char* bands_env = std::getenv("RF_SCHED_BANDS");
  if (!bands_env || *bands_env == '\0')
    return {};

  std::vector<BandSlot> slots;
  const std::string input{bands_env};
  std::size_t pos = 0;
  while (pos <= input.size()) {
    const std::size_t comma = input.find(',', pos);
    const std::size_t end = (comma == std::string::npos) ? input.size() : comma;
    const std::string_view token{input.data() + pos, end - pos};
    const std::size_t first = token.find_first_not_of(" \t");
    if (first != std::string_view::npos) {
      const std::string trimmed{token.substr(first, token.find_last_not_of(" \t") - first + 1)};
      try {
        const double hz = std::stod(trimmed);
        if (hz > 0.0)
          slots.push_back({hz, dwell});
      } catch (...) {
      }
    }
    pos = (comma == std::string::npos) ? input.size() + 1 : comma + 1;
  }

  if (slots.size() < 2)
    return {};

  return BandScheduler{std::move(slots), std::chrono::steady_clock::now()};
}

}  // namespace meek
