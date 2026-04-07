// include/meek/isdr_source.hpp — SDR hardware abstraction layer.
//
// ISdrSource is an abstract base class.  Concrete implementations:
//   SoapySdrSource — wraps the SoapySDR C API (RTL-SDR, HackRF, etc.)
//
// The SdrSourceConcept concept enables static (template-based) polymorphism
// when the exact source type is known at compile time.

#pragma once

#include <complex>
#include <concepts>
#include <cstddef>
#include <memory>
#include <span>
#include <string>

namespace meek {

// ---------------------------------------------------------------------------
// Concept — static duck-type check for SDR source implementations
// ---------------------------------------------------------------------------

template <typename T>
concept SdrSourceConcept = requires(T& t, std::span<std::complex<float>> buf) {
  { t.read_samples(buf) } -> std::same_as<std::ptrdiff_t>;
  { t.center_freq_hz() } -> std::same_as<double>;
  { t.sample_rate_hz() } -> std::same_as<double>;
  { t.is_open() } -> std::same_as<bool>;
};

// ---------------------------------------------------------------------------
// ISdrSource — abstract base for runtime polymorphism
// ---------------------------------------------------------------------------

class ISdrSource {
 public:
  virtual ~ISdrSource() = default;

  ISdrSource(const ISdrSource&) = delete;
  ISdrSource& operator=(const ISdrSource&) = delete;

  /// Read up to buf.size() IQ samples into buf.
  /// Returns  0 on timeout (non-fatal; caller should continue).
  /// Returns -2 on hardware overflow (SOAPY_SDR_OVERFLOW); samples were lost
  ///            but the stream is still alive — caller should count and continue.
  /// Returns -1 on any other fatal error; caller should abort.
  [[nodiscard]] virtual std::ptrdiff_t read_samples(std::span<std::complex<float>> buf) = 0;

  [[nodiscard]] virtual double center_freq_hz() const noexcept = 0;
  [[nodiscard]] virtual double sample_rate_hz() const noexcept = 0;
  [[nodiscard]] virtual bool is_open() const noexcept = 0;

  /// Human-readable description of the source (e.g. "RTL-SDR USB#0").
  [[nodiscard]] virtual std::string description() const = 0;

  /// Retune the SDR to a new centre frequency in Hz.
  /// Returns true on success, false if the operation failed or is not supported.
  /// Default implementation returns false (unsupported / test double).
  [[nodiscard]] virtual bool set_center_freq(double /*freq_hz*/) noexcept {
    return false;
  }

 protected:
  ISdrSource() = default;
};

// ---------------------------------------------------------------------------
// SoapySdrSource — SoapySDR C API implementation
// ---------------------------------------------------------------------------
// Only compiled when HAVE_SOAPY is defined (set by CMakeLists when SoapySDR
// is found).  This keeps the header usable in the rf_audit tool which does
// not link against SoapySDR.

#ifdef HAVE_SOAPY

#include <SoapySDR/Device.h>
#include <SoapySDR/Formats.h>

class SoapySdrSource final : public ISdrSource {
 public:
  /// Open the first available SoapySDR device and configure it.
  /// Throws std::runtime_error on failure.
  explicit SoapySdrSource(double center_freq, double sample_rate, double gain,
                          long long read_timeout_us = 500'000LL);

  ~SoapySdrSource() override;

  [[nodiscard]] std::ptrdiff_t read_samples(std::span<std::complex<float>> buf) override;

  [[nodiscard]] double center_freq_hz() const noexcept override {
    return center_freq_hz_;
  }
  [[nodiscard]] double sample_rate_hz() const noexcept override {
    return sample_rate_hz_;
  }
  [[nodiscard]] bool is_open() const noexcept override {
    return dev_ != nullptr;
  }
  [[nodiscard]] std::string description() const override {
    return description_;
  }

  [[nodiscard]] bool set_center_freq(double freq_hz) noexcept override;

 private:
  SoapySDRDevice* dev_{nullptr};
  SoapySDRStream* stream_{nullptr};
  double center_freq_hz_{0.0};
  double sample_rate_hz_{0.0};
  long long read_timeout_us_{500'000LL};
  std::string description_;
};

// Verify that SoapySdrSource satisfies the concept.
static_assert(SdrSourceConcept<SoapySdrSource>, "SoapySdrSource does not satisfy SdrSourceConcept");

#endif  // HAVE_SOAPY

}  // namespace meek
