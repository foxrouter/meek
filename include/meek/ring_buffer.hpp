// include/meek/ring_buffer.hpp — Lock-free single-producer / single-consumer
// ring buffer.
//
// SpscRingBuffer<T, Capacity> is suitable for use between exactly one producer
// thread and one consumer thread with no additional locking.  Capacity must be
// a power of two.
//
// Cache-line alignment on head_, tail_, and buffer_ prevents false sharing on
// SMP cores: the producer writes buffer_[head] and head_; the consumer writes
// tail_.  Each lives on its own 64-byte cache line.
//
// push() returns false when the buffer is full (non-blocking).  The item is
// only moved/copied into the buffer slot *after* the full-check passes, so
// a false return guarantees the caller's value was not consumed.
// Effective usable capacity is Capacity-1 (one slot is reserved as sentinel).
// pop() returns false when the buffer is empty (non-blocking).

#pragma once

#include <array>
#include <atomic>
#include <cassert>
#include <cstddef>
#include <type_traits>
#include <utility>

namespace meek {

template <typename T, std::size_t Capacity>
class SpscRingBuffer {
  static_assert(Capacity > 0, "Capacity must be > 0");
  static_assert((Capacity & (Capacity - 1)) == 0, "Capacity must be a power of 2");
  static_assert(std::is_default_constructible_v<T>);
  static_assert(std::is_nothrow_move_assignable_v<T> || std::is_nothrow_copy_assignable_v<T>);

 public:
  SpscRingBuffer() = default;

  // Non-copyable, non-movable (contains atomics).
  SpscRingBuffer(const SpscRingBuffer&) = delete;
  SpscRingBuffer& operator=(const SpscRingBuffer&) = delete;

  /// Push a copy of item.  Returns true on success, false if the buffer is
  /// full.  The item is only copied after the full-check passes.
  /// Called from the producer thread only.
  [[nodiscard]] bool push(const T& item) noexcept(std::is_nothrow_copy_assignable_v<T>) {
    const std::size_t head = head_.load(std::memory_order_relaxed);
    const std::size_t next = (head + 1) & kMask;
    if (next == tail_.load(std::memory_order_acquire)) {
      return false;  // full — item untouched
    }
    buffer_[head] = item;
    head_.store(next, std::memory_order_release);
    return true;
  }

  /// Push by move.  Returns true on success, false if the buffer is full.
  /// The item is only moved after the full-check passes; on false the caller's
  /// value remains valid.
  [[nodiscard]] bool push(T&& item) noexcept(std::is_nothrow_move_assignable_v<T>) {
    const std::size_t head = head_.load(std::memory_order_relaxed);
    const std::size_t next = (head + 1) & kMask;
    if (next == tail_.load(std::memory_order_acquire)) {
      return false;  // full — item untouched
    }
    buffer_[head] = std::move(item);
    head_.store(next, std::memory_order_release);
    return true;
  }

  /// Pop an item into out.  Returns true on success, false if empty.
  /// Called from the consumer thread only.
  [[nodiscard]] bool pop(T& out) noexcept(std::is_nothrow_move_assignable_v<T>) {
    const std::size_t tail = tail_.load(std::memory_order_relaxed);
    if (tail == head_.load(std::memory_order_acquire)) {
      return false;  // empty
    }
    out = std::move(buffer_[tail]);
    tail_.store((tail + 1) & kMask, std::memory_order_release);
    return true;
  }

  /// Approximate number of items in the buffer (may be stale).
  [[nodiscard]] std::size_t size_approx() const noexcept {
    const std::size_t h = head_.load(std::memory_order_relaxed);
    const std::size_t t = tail_.load(std::memory_order_relaxed);
    return (h - t) & kMask;
  }

  [[nodiscard]] bool empty_approx() const noexcept {
    return size_approx() == 0;
  }

  /// Effective usable capacity (one slot reserved as sentinel).
  [[nodiscard]] static constexpr std::size_t capacity() noexcept {
    return Capacity - 1;
  }

 private:
  static constexpr std::size_t kMask = Capacity - 1;

  alignas(64) std::atomic<std::size_t> head_{0};
  alignas(64) std::atomic<std::size_t> tail_{0};
  alignas(64) std::array<T, Capacity> buffer_{};
};

}  // namespace meek
