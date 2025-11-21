#pragma once
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <ctime>

namespace nll {

constexpr std::uint64_t a_billi = 1'000'000'000ull;

inline std::uint64_t mono_ns() noexcept {
  timespec ts;
  clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
  return static_cast<std::uint64_t>(ts.tv_sec) * a_billi +
         static_cast<std::uint64_t>(ts.tv_nsec);
}

inline std::uint64_t real_ns() noexcept {
  timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  return static_cast<std::uint64_t>(ts.tv_sec) * a_billi +
         static_cast<std::uint64_t>(ts.tv_nsec);
}

inline void sleep_ns(std::uint64_t ns) noexcept {
  timespec req{static_cast<time_t>(ns / a_billi),
               static_cast<long>(ns % a_billi)};
  nanosleep(&req, nullptr);
}

} // namespace nll
