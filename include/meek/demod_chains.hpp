// include/meek/demod_chains.hpp — liquid-dsp demodulation chains.
//
// Three noexcept functions in namespace meek:
//   demod_fsk       — binary FSK / GMSK demodulation
//   demod_psk_qam   — PSK / QAM demodulation (QPSK with BPSK fallback)
//   demod_ook_am    — OOK / AM envelope demodulation
//
// All three populate cr.demod_status, cr.demod_lock_ms, and cr.demod_soft_bits.
// demod_fsk also sets cr.demod_cfo_hz; demod_psk_qam also sets cr.demod_phase_error;
// demod_ook_am leaves cr.demod_cfo_hz and cr.demod_phase_error at their default values.
// All liquid objects are destroyed on every return path via scope guards.
//
// Compiled only when HAVE_LIQUID is defined.

#pragma once

#ifdef HAVE_LIQUID

#include <liquid/liquid.h>

#include <algorithm>
#include <cmath>
#include <complex>
#include <cstddef>
#include <cstdint>
#include <numbers>
#include <numeric>
#include <span>
#include <utility>
#include <vector>

#include "meek/config.hpp"
#include "meek/sample_types.hpp"

namespace meek {

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

namespace detail {

/// Minimal scope-exit guard — calls fn() in the destructor.
template <typename F>
struct scope_guard {
  F fn;
  explicit scope_guard(F&& f) noexcept : fn(std::forward<F>(f)) {}
  ~scope_guard() noexcept {
    fn();
  }
  scope_guard(scope_guard const&) = delete;
  scope_guard& operator=(scope_guard const&) = delete;
};

template <typename F>
[[nodiscard]] scope_guard<F> on_scope_exit(F&& f) noexcept {
  return scope_guard<F>(std::forward<F>(f));
}

/// IIR DC block: y[i] = x[i] - dc, dc = alpha*dc + (1-alpha)*x[i].
inline void dc_block(std::span<const std::complex<float>> src,
                     std::vector<std::complex<float>>& dst, float alpha = 0.995f) noexcept {
  const std::size_t n = src.size();
  dst.resize(n);
  std::complex<float> dc{0.f, 0.f};
  const float k = 1.f - alpha;
  for (std::size_t i = 0; i < n; ++i) {
    dc = dc * alpha + src[i] * k;
    dst[i] = src[i] - dc;
  }
}

/// Pack a vector of bit values (0/1) into bytes MSB-first.
/// Returns empty vector if fewer than 8 bits supplied or if the bit count is
/// not a multiple of 8 (non-byte-aligned input would misplace the CRC bytes).
inline std::vector<unsigned char> pack_bits(const std::vector<unsigned int>& bits) noexcept {
  const std::size_t n_bytes = bits.size() / 8;
  if (n_bytes == 0 || (bits.size() % 8) != 0)
    return {};
  std::vector<unsigned char> bytes(n_bytes, 0);
  for (std::size_t i = 0; i < n_bytes; ++i)
    for (std::size_t j = 0; j < 8; ++j)
      bytes[i] = static_cast<unsigned char>((bytes[i] << 1) | (bits[i * 8 + j] & 1u));
  return bytes;
}

/// Extract last 4 bytes as a big-endian uint32 CRC key.
inline unsigned int extract_crc32(const std::vector<unsigned char>& bytes) noexcept {
  const std::size_t n = bytes.size();
  return (static_cast<unsigned int>(bytes[n - 4]) << 24) |
         (static_cast<unsigned int>(bytes[n - 3]) << 16) |
         (static_cast<unsigned int>(bytes[n - 2]) << 8) | static_cast<unsigned int>(bytes[n - 1]);
}

/// Run CRC-32 check on packed bytes.  Returns OK or CRC_FAIL.
/// Messages shorter than 5 bytes (cannot contain a 4-byte CRC trailer) are
/// also reported as CRC_FAIL rather than LOCK_FAIL so that framing-length
/// issues do not inflate the lock_fail Prometheus counter.
inline DemodStatus check_crc(const std::vector<unsigned char>& msg_bytes) noexcept {
  if (msg_bytes.size() < 5)
    return DemodStatus::CRC_FAIL;
  const unsigned int key = extract_crc32(msg_bytes);
  // Copy payload bytes (excluding 4-byte CRC trailer) into a mutable buffer;
  // crc_check_key takes a non-const pointer and may mutate its input.
  std::vector<unsigned char> payload(
      msg_bytes.begin(), msg_bytes.begin() + static_cast<std::ptrdiff_t>(msg_bytes.size() - 4));
  const int ok =
      crc_check_key(LIQUID_CRC_32, key, payload.data(), static_cast<unsigned int>(payload.size()));
  return (ok == 1) ? DemodStatus::OK : DemodStatus::CRC_FAIL;
}

}  // namespace detail

// ---------------------------------------------------------------------------
// demod_fsk
// ---------------------------------------------------------------------------

/// Demodulate a binary FSK / GMSK signal.
///
/// Steps:
///   1. IIR DC block (α = 0.995).
///   2. Coarse CFO = mean arg(s[i+1] * conj(s[i])).
///   3. Fine correction via nco_crcf.
///   4. Gardner timing: e[n] = Re{(x[n]-x[n-2]) * conj(x[n-1])},
///      tau += 0.01*e[n].  Advances symbol boundaries by k+round(tau) each step.
///   5. fskdem with k = clamp(round(sample_rate/rsym), 2, 64).
///   6. Soft bits: clamp(|freq_dev|/max_dev * 255, 0, 255) per symbol.
///   7. CRC-32 check.
inline void demod_fsk(std::span<const std::complex<float>> s, const Config& cfg,
                      ClassificationResult& cr) noexcept {
  try {
    // Reset all demod fields to consistent defaults before any early returns.
    cr.demod_soft_bits.clear();
    cr.demod_lock_ms = 0;
    cr.demod_cfo_hz = 0.0f;
    cr.demod_status = DemodStatus::LOCK_FAIL;

    if (s.empty()) {
      return;
    }

    if (cfg.rsym <= 0.0 || cr.sample_rate_hz <= 0.0) {
      return;
    }
    const std::size_t n = s.size();
    const int k = static_cast<int>(std::clamp(std::round(cr.sample_rate_hz / cfg.rsym), 2.0, 64.0));

    // 1. IIR DC block
    std::vector<std::complex<float>> buf;
    detail::dc_block(s, buf);

    // 2. Coarse CFO estimate (mean instantaneous frequency)
    float cfo_rad = 0.f;
    for (std::size_t i = 0; i + 1 < n; ++i) cfo_rad += std::arg(buf[i + 1] * std::conj(buf[i]));
    if (n > 1)
      cfo_rad /= static_cast<float>(n - 1);
    cr.demod_cfo_hz = cfo_rad * static_cast<float>(cr.sample_rate_hz) /
                      (2.f * static_cast<float>(std::numbers::pi));

    // 3. Fine CFO correction via nco_crcf
    {
      nco_crcf nco = nco_crcf_create(LIQUID_VCO);
      if (!nco) {
        cr.demod_status = DemodStatus::LOCK_FAIL;
        return;
      }
      auto nco_guard = detail::on_scope_exit([&] { nco_crcf_destroy(nco); });
      nco_crcf_set_frequency(nco, -cfo_rad);
      for (std::size_t i = 0; i < n; ++i) {
        std::complex<float> out;
        nco_crcf_mix_down(nco, buf[i], &out);
        buf[i] = out;
        nco_crcf_step(nco);
      }
    }

    // 4. Gardner timing loop + 5. fskdem demodulation + 6. soft bits
    const float bw_norm = static_cast<float>(cfg.fdev / cr.sample_rate_hz);
    const float max_freq_rad =
        static_cast<float>(2.0 * std::numbers::pi * cfg.fdev / cr.sample_rate_hz);

    fskdem fsk = fskdem_create(2, static_cast<unsigned int>(k), bw_norm);
    if (!fsk) {
      cr.demod_status = DemodStatus::LOCK_FAIL;
      return;
    }
    auto fsk_guard = detail::on_scope_exit([&] { fskdem_destroy(fsk); });

    std::vector<unsigned int> syms;
    std::vector<uint8_t> soft_bits;
    syms.reserve(n / static_cast<std::size_t>(k) + 2);
    soft_bits.reserve(n / static_cast<std::size_t>(k) + 2);

    float tau = 0.f;
    std::complex<float> x_sym{0.f};  // x[n-2]: previous symbol boundary
    std::complex<float> x_mid{0.f};  // x[n-1]: midpoint sample

    // Track Gardner convergence for demod_lock_ms
    float err_ema = 1.f;  // exponential moving average of |e|
    int lock_sym = -1;

    std::size_t pos = 0;
    while (pos + static_cast<std::size_t>(k) <= n) {
      const std::size_t mid_pos = pos + static_cast<std::size_t>(k / 2);
      const std::size_t end_pos = pos + static_cast<std::size_t>(k) - 1;

      x_mid = buf[mid_pos];
      const std::complex<float> x_cur = buf[end_pos];

      // Gardner timing error
      const float e = ((x_cur - x_sym) * std::conj(x_mid)).real();
      tau += 0.01f * e;
      tau = std::clamp(tau, -0.5f, 0.5f);
      x_sym = x_cur;

      // Track convergence
      err_ema = 0.9f * err_ema + 0.1f * std::abs(e);
      if (lock_sym < 0 && err_ema < 0.05f)
        lock_sym = static_cast<int>(syms.size());

      // Per-symbol instantaneous frequency for soft bits
      float inst_freq = 0.f;
      if (k > 1) {
        for (int j = 0; j + 1 < k; ++j) {
          const std::size_t a = pos + static_cast<std::size_t>(j);
          const std::size_t b = a + 1;
          if (b < n)
            inst_freq += std::arg(buf[b] * std::conj(buf[a]));
        }
        inst_freq /= static_cast<float>(k - 1);
      }

      // Hard decision via fskdem
      unsigned int sym = 0;
      fskdem_demodulate(fsk, buf.data() + pos, &sym);
      syms.push_back(sym & 1u);

      // Soft bit
      const float max_dev = (max_freq_rad > 1e-9f) ? max_freq_rad : 1e-9f;
      const float sb = std::clamp(std::abs(inst_freq) / max_dev * 255.f, 0.f, 255.f);
      soft_bits.push_back(static_cast<uint8_t>(sb));

      // Advance symbol position using tau
      const int advance = k + static_cast<int>(std::round(tau));
      pos += static_cast<std::size_t>(std::max(1, advance));
    }

    if (syms.size() < 8) {
      cr.demod_status = DemodStatus::LOCK_FAIL;
      return;
    }

    cr.demod_soft_bits = std::move(soft_bits);

    // Lock time: symbols until Gardner error settled, converted to ms.
    // Report 0 when lock was never detected (lock_sym < 0).
    if (lock_sym >= 0) {
      cr.demod_lock_ms =
          static_cast<int>(std::lround(static_cast<float>(lock_sym + 1) * (1000.0f / cfg.rsym)));
    } else {
      cr.demod_lock_ms = 0;
    }

    // 7. CRC-32
    const auto msg_bytes = detail::pack_bits(syms);
    if (msg_bytes.size() < 5) {
      cr.demod_status = DemodStatus::CRC_FAIL;
      return;
    }
    cr.demod_status = detail::check_crc(msg_bytes);
  } catch (...) {
    // Ensure demod-related fields are in a consistent failure state.
    cr.demod_status = DemodStatus::LOCK_FAIL;
    cr.demod_lock_ms = 0;
    cr.demod_soft_bits.clear();
    cr.demod_cfo_hz = 0.0f;
  }
}

// ---------------------------------------------------------------------------
// demod_psk_qam
// ---------------------------------------------------------------------------

/// Demodulate a PSK / QAM signal (QPSK with BPSK fallback).
///
/// Steps:
///   1. IIR DC block.
///   2. symsync_crcf RRC, k sps, m=6, β=0.3, Npfb=32, loop BW 0.02.
///   3. modemcf QPSK + modemcf_get_demodulator_phase_error.
///   4. nco_crcf Costas + nco_crcf_pll_step.
///   5. Watchdog: RMS err > π/4 per 32 symbols → widen PLL BW (×3, max 0.05,
///      up to 3 times) → fallback BPSK.
///   6. Soft bits: clamp((π/2 - |phase_err|) / (π/2) * 255, 0, 255).
///   7. CRC-32.  cr.demod_phase_error = overall RMS.
inline void demod_psk_qam(std::span<const std::complex<float>> s, const Config& cfg,
                          ClassificationResult& cr) noexcept {
  try {
    // Reset all demod fields to consistent defaults before any early returns.
    cr.demod_soft_bits.clear();
    cr.demod_lock_ms = 0;
    cr.demod_phase_error = 0.0f;
    cr.demod_status = DemodStatus::LOCK_FAIL;

    if (s.empty()) {
      return;
    }

    // Validate symbol rate and sample rate before computing k.
    if (cfg.rsym <= 0.0 || cr.sample_rate_hz <= 0.0) {
      return;
    }

    const std::size_t n = s.size();
    const int k = static_cast<int>(std::clamp(std::round(cr.sample_rate_hz / cfg.rsym), 2.0, 64.0));
    const auto uk = static_cast<unsigned int>(k);

    // 1. IIR DC block
    std::vector<std::complex<float>> buf;
    detail::dc_block(s, buf);

    // 2. symsync_crcf RRC
    symsync_crcf sync = symsync_crcf_create_rnyquist(LIQUID_FIRFILT_RRC, uk, 6u, 0.3f, 32u);
    if (!sync) {
      cr.demod_status = DemodStatus::LOCK_FAIL;
      return;
    }
    {
      auto sync_guard = detail::on_scope_exit([&] { symsync_crcf_destroy(sync); });
      symsync_crcf_set_lf_bw(sync, 0.02f);

      // Output buffer: generously over-sized. With n input samples at k samples/symbol,
      // symsync_crcf produces O(n / k) output symbols plus filter transients, which
      // easily fits into this buffer.
      std::vector<std::complex<float>> sync_out(n + static_cast<std::size_t>(k) * 12u);
      unsigned int num_written = 0;
      symsync_crcf_execute(sync, buf.data(), static_cast<unsigned int>(n), sync_out.data(),
                           &num_written);
      sync_out.resize(num_written);

      if (num_written < 8) {
        cr.demod_status = DemodStatus::LOCK_FAIL;
        return;
      }

      // 3–5. modemcf QPSK + Costas PLL + watchdog
      modulation_scheme scheme = LIQUID_MODEM_QPSK;
      modemcf demod = modemcf_create(scheme);
      if (!demod) {
        cr.demod_status = DemodStatus::LOCK_FAIL;
        return;
      }
      auto demod_guard = detail::on_scope_exit([&] {
        if (demod)
          modemcf_destroy(demod);
      });

      nco_crcf pll = nco_crcf_create(LIQUID_VCO);
      if (!pll) {
        cr.demod_status = DemodStatus::LOCK_FAIL;
        return;
      }
      auto pll_guard = detail::on_scope_exit([&] { nco_crcf_destroy(pll); });
      float pll_bw = 0.02f;
      nco_crcf_pll_set_bandwidth(pll, pll_bw);

      std::vector<unsigned int> syms;
      std::vector<float> phase_errs;
      syms.reserve(num_written);
      phase_errs.reserve(num_written);

      float watch_rms = 0.f;
      int watch_cnt = 0;
      int widen_count = 0;
      int lock_sym = -1;

      for (std::size_t i = 0; i < num_written; ++i) {
        // Mix down via Costas PLL
        std::complex<float> corrected;
        nco_crcf_mix_down(pll, sync_out[i], &corrected);

        // Demodulate
        unsigned int sym = 0;
        modemcf_demodulate(demod, corrected, &sym);
        const float pe = modemcf_get_demodulator_phase_error(demod);

        // PLL step
        nco_crcf_pll_step(pll, pe);
        nco_crcf_step(pll);

        syms.push_back(sym);
        phase_errs.push_back(pe);

        // Watchdog
        watch_rms += pe * pe;
        ++watch_cnt;
        if (watch_cnt >= 32) {
          const float rms = std::sqrt(watch_rms / 32.f);
          constexpr float kPiOver4 = static_cast<float>(std::numbers::pi) / 4.f;
          constexpr float kPiOver8 = static_cast<float>(std::numbers::pi) / 8.f;

          if (lock_sym < 0 && rms < kPiOver8)
            lock_sym = static_cast<int>(i);

          if (rms > kPiOver4 && widen_count < 3) {
            ++widen_count;
            pll_bw = std::min(pll_bw * 3.f, 0.05f);
            nco_crcf_pll_set_bandwidth(pll, pll_bw);
            if (widen_count == 3) {
              modemcf_destroy(demod);
              demod = modemcf_create(LIQUID_MODEM_BPSK);
              if (!demod) {
                cr.demod_status = DemodStatus::LOCK_FAIL;
                return;
              }
              scheme = LIQUID_MODEM_BPSK;
              // Restart demod/bit collection for the final scheme to avoid
              // interpreting QPSK symbols as BPSK and misaligning the bitstream.
              syms.clear();
              phase_errs.clear();
              watch_rms = 0.f;
              watch_cnt = 0;
              lock_sym = -1;
            }
          }
          watch_rms = 0.f;
          watch_cnt = 0;
        }
      }

      if (syms.empty()) {
        cr.demod_status = DemodStatus::LOCK_FAIL;
        return;
      }

      // 7. RMS phase error
      float total_sq = 0.f;
      for (float e : phase_errs) total_sq += e * e;
      cr.demod_phase_error = std::sqrt(total_sq / static_cast<float>(phase_errs.size()));

      // Lock time: report 0 when lock was never detected (lock_sym < 0).
      if (lock_sym >= 0) {
        cr.demod_lock_ms =
            static_cast<int>(std::lround(static_cast<float>(lock_sym + 1) * (1000.0f / cfg.rsym)));
      } else {
        cr.demod_lock_ms = 0;
      }

      // 6. Soft bits
      constexpr float kPiOver2 = static_cast<float>(std::numbers::pi) / 2.f;
      std::vector<uint8_t> soft_bits;
      soft_bits.reserve(phase_errs.size());
      for (float pe : phase_errs) {
        const float sb = std::clamp((kPiOver2 - std::abs(pe)) / kPiOver2 * 255.f, 0.f, 255.f);
        soft_bits.push_back(static_cast<uint8_t>(sb));
      }
      cr.demod_soft_bits = std::move(soft_bits);

      // Expand symbol bits
      const unsigned int bits_per_sym = (scheme == LIQUID_MODEM_QPSK) ? 2u : 1u;
      std::vector<unsigned int> all_bits;
      all_bits.reserve(syms.size() * bits_per_sym);
      for (unsigned int sym : syms) {
        if (bits_per_sym == 2u) {
          all_bits.push_back((sym >> 1u) & 1u);
          all_bits.push_back(sym & 1u);
        } else {
          all_bits.push_back(sym & 1u);
        }
      }

      // CRC-32
      const auto msg_bytes = detail::pack_bits(all_bits);
      if (msg_bytes.size() < 5) {
        cr.demod_status = DemodStatus::CRC_FAIL;
        return;
      }
      cr.demod_status = detail::check_crc(msg_bytes);
    }
  } catch (...) {
    // On exception, ensure demod fields are reset to a consistent failure state.
    cr.demod_status = DemodStatus::LOCK_FAIL;
    cr.demod_soft_bits.clear();
    cr.demod_lock_ms = 0;
    cr.demod_phase_error = 0.0f;
  }
}

// ---------------------------------------------------------------------------
// demod_ook_am
// ---------------------------------------------------------------------------

/// Demodulate an OOK / AM signal.
///
/// Steps:
///   1. Envelope: env[i] = |s[i]|.
///   2. Percentile threshold: p10 + (p90 - p10) * 0.5.
///   3. Sample at rsym intervals (mid-point of each symbol window).
///   4. Soft bits: derive a confidence from (env - threshold) / (p90 - p10),
///      clamp it into [-1, 1], and encode as a signed byte centered at 128
///      (128 + confidence*128), yielding values in [0, 255] where 255 = fully
///      on, 0 = fully off, and 128 = exactly at threshold; values above 128
///      indicate symbols decoded as ON, values below 128 indicate OFF.
///   5. Duty-cycle guard: duty < 5% or > 95% → LOCK_FAIL.
///   6. CRC-32.
inline void demod_ook_am(std::span<const std::complex<float>> s, const Config& cfg,
                         ClassificationResult& cr) noexcept {
  try {
    // Reset all demod fields to consistent defaults before any early returns.
    // demod_lock_ms is set to -1 (not applicable) later once demod succeeds.
    cr.demod_soft_bits.clear();
    cr.demod_lock_ms = 0;
    cr.demod_status = DemodStatus::LOCK_FAIL;

    if (s.empty()) {
      return;
    }

    const std::size_t n = s.size();

    // 1. Envelope
    std::vector<float> env(n);
    for (std::size_t i = 0; i < n; ++i) env[i] = std::abs(s[i]);

    // 2. Percentile threshold (nth_element avoids a full sort)
    std::vector<float> tmp(env);
    const std::size_t p10_idx = n / 10;
    const std::size_t p90_idx = 9 * n / 10;
    auto p10_it = tmp.begin() + static_cast<std::ptrdiff_t>(p10_idx);
    std::nth_element(tmp.begin(), p10_it, tmp.end());
    const float p10 = *p10_it;
    auto p90_it = tmp.begin() + static_cast<std::ptrdiff_t>(p90_idx);
    std::nth_element(tmp.begin(), p90_it, tmp.end());
    const float p90 = *p90_it;
    const float range = p90 - p10;

    if (range < 1e-6f) {
      cr.demod_status = DemodStatus::LOCK_FAIL;
      return;
    }

    const float threshold = p10 + range * 0.5f;

    // 3. Sample at rsym intervals (mid-point of symbol window)
    if (cfg.rsym <= 0.0 || cr.sample_rate_hz <= 0.0) {
      cr.demod_status = DemodStatus::LOCK_FAIL;
      return;
    }
    const int k =
        static_cast<int>(std::clamp(std::round(cr.sample_rate_hz / cfg.rsym), 1.0, 1024.0));
    const std::size_t max_syms = n / static_cast<std::size_t>(k);

    if (max_syms < 8) {
      cr.demod_status = DemodStatus::LOCK_FAIL;
      return;
    }

    std::vector<unsigned int> bits;
    std::vector<uint8_t> soft_bits;
    bits.reserve(max_syms);
    soft_bits.reserve(max_syms);

    std::size_t on_count = 0;
    for (std::size_t sym_i = 0; sym_i < max_syms; ++sym_i) {
      const std::size_t mid = sym_i * static_cast<std::size_t>(k) + static_cast<std::size_t>(k / 2);
      const float e = env[mid];
      const unsigned int bit = (e >= threshold) ? 1u : 0u;
      bits.push_back(bit);
      if (bit)
        ++on_count;

      // 4. Soft bit: encode both bit value and confidence.
      const float norm = std::clamp((e - threshold) / range, -1.0f, 1.0f);
      const float sb = std::clamp(128.0f + norm * 128.0f, 0.0f, 255.0f);
      soft_bits.push_back(static_cast<uint8_t>(sb));
    }

    // 5. Duty-cycle guard
    const float duty = static_cast<float>(on_count) / static_cast<float>(bits.size());
    if (duty < 0.05f || duty > 0.95f) {
      cr.demod_status = DemodStatus::LOCK_FAIL;
      return;
    }

    cr.demod_soft_bits = std::move(soft_bits);
    cr.demod_lock_ms = -1;  // envelope detection — no carrier lock required / not applicable

    // 6. CRC-32
    const auto msg_bytes = detail::pack_bits(bits);
    if (msg_bytes.size() < 5) {
      cr.demod_status = DemodStatus::CRC_FAIL;
      return;
    }
    cr.demod_status = detail::check_crc(msg_bytes);
  } catch (...) {
    // Ensure demod-related fields are consistent on exception paths.
    cr.demod_status = DemodStatus::LOCK_FAIL;
    cr.demod_lock_ms = 0;
    cr.demod_soft_bits.clear();
  }
}

}  // namespace meek

#endif  // HAVE_LIQUID
