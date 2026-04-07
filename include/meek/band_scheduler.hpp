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
//       if (sched.dwell_elapsed(steady_clock::now())) {
//         const BandSlot& next = sched.peek_next();
//         if (retune_sdr(next))
//           sched.advance(steady_clock::now());
//         else
//           sched.reset_dwell(steady_clock::now());
//       }
//       // ... capture block ...
//     }
//   }

#pragma once

#include <chrono>
#include <cstddef>
#include <cstdlib>
#include <iostream>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "meek/config.hpp"

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
  /// Default-construct in disabled state.  dwell_elapsed() always returns false.
  BandScheduler() noexcept = default;
  ~BandScheduler() = default;

  BandScheduler(const BandScheduler&) = delete;
  BandScheduler& operator=(const BandScheduler&) = delete;

  BandScheduler(BandScheduler&& other) noexcept
      : slots_(std::move(other.slots_)),
        current_idx_(other.current_idx_),
        dwell_start_(other.dwell_start_),
        enabled_(other.enabled_),
        first_tick_done_(other.first_tick_done_) {
    other.current_idx_ = 0;
    other.dwell_start_ = std::chrono::steady_clock::time_point{};
    other.enabled_ = false;
    other.first_tick_done_ = false;
  }

  BandScheduler& operator=(BandScheduler&& other) noexcept {
    if (this != &other) {
      slots_ = std::move(other.slots_);
      current_idx_ = other.current_idx_;
      dwell_start_ = other.dwell_start_;
      enabled_ = other.enabled_;
      first_tick_done_ = other.first_tick_done_;
      other.current_idx_ = 0;
      other.dwell_start_ = std::chrono::steady_clock::time_point{};
      other.enabled_ = false;
      other.first_tick_done_ = false;
    }
    return *this;
  }

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

  /// Returns true if the current dwell period has elapsed.
  ///
  /// On the first call the dwell timer is anchored to `now` (so timing starts
  /// when capture actually begins, not when from_env() was called) and the
  /// method returns false — giving slot 0 its full dwell before the first
  /// transition.  Returns false when scheduling is disabled.
  [[nodiscard]] bool dwell_elapsed(std::chrono::steady_clock::time_point now) noexcept {
    if (!enabled_)
      return false;
    if (!first_tick_done_) {
      first_tick_done_ = true;
      dwell_start_ = now;
      return false;
    }
    return (now - dwell_start_) >= slots_[current_idx_].dwell_ms;
  }

  /// Returns a reference to the next slot without advancing the index.
  /// Behaviour is undefined when enabled() is false or slot_count() == 0.
  [[nodiscard]] const BandSlot& peek_next() const noexcept {
    return slots_[(current_idx_ + 1) % slots_.size()];
  }

  /// Commits the slot transition.  Call only after a successful retune.
  void advance(std::chrono::steady_clock::time_point now) noexcept {
    current_idx_ = (current_idx_ + 1) % slots_.size();
    dwell_start_ = now;
  }

  /// Resets the dwell timer without changing the current slot.
  /// Call after a failed retune to avoid an immediate retry on the next iteration.
  void reset_dwell(std::chrono::steady_clock::time_point now) noexcept {
    dwell_start_ = now;
  }

 private:
  explicit BandScheduler(std::vector<BandSlot> slots) noexcept;

  std::vector<BandSlot> slots_;
  std::size_t current_idx_{0};
  std::chrono::steady_clock::time_point dwell_start_{};
  bool enabled_{false};
  bool first_tick_done_{false};
};

// ---------------------------------------------------------------------------
// Inline implementation
// ---------------------------------------------------------------------------

inline BandScheduler::BandScheduler(std::vector<BandSlot> slots) noexcept
    : slots_(std::move(slots)), current_idx_(0), dwell_start_{}, enabled_(true) {}

inline BandScheduler BandScheduler::from_env() {
  using ms = std::chrono::milliseconds;
  constexpr std::string_view kWhitespace = " \t\r\n\f\v";

  // Use the shared env helper so parsing behaviour is consistent with the
  // rest of the RF_* configuration.
  const long long dwell_ll = detail::env_ll("RF_SCHED_DWELL_MS", 10'000);
  const ms dwell{dwell_ll > 0 ? dwell_ll : 10'000};

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
    const std::size_t first = token.find_first_not_of(kWhitespace);
    if (first != std::string_view::npos) {
      const std::size_t last = token.find_last_not_of(kWhitespace);
      const std::string trimmed{token.substr(first, last - first + 1)};
      try {
        std::size_t parsed_chars = 0;
        const double hz = std::stod(trimmed, &parsed_chars);
        const bool only_trailing_ws =
            trimmed.find_first_not_of(kWhitespace, parsed_chars) == std::string::npos;
        if (only_trailing_ws && hz > 0.0)
          slots.push_back({hz, dwell});
      } catch (...) {
      }
    }
    pos = (comma == std::string::npos) ? input.size() + 1 : comma + 1;
  }

  if (slots.size() < 2) {
    std::cerr << "[SCHED] WARN: RF_SCHED_BANDS produced fewer than 2 valid slots (" << slots.size()
              << ") — scheduling disabled\n";
    return {};
  }

  return BandScheduler{std::move(slots)};
}

}  // namespace meek
