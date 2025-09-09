#pragma once
#include <cstdint>
#include <ctime>
#include <thread>
#include <chrono>

/*
**Time.hpp: single source of truth for time**

I need a monotonic clock for latency/jitter, wall clocks may desync and stutter due to NTP time adjustments
Linux provides two mono-clocks: MONOTIC and MONOTONIC_RAW
both are free from NTP time adjusments, but MONOTONIC may change its oscillation freq due to NTP (weird)
RAW is just the pure local clock.

I also want to keep functions inline and header-only to avoid link overhead
*/

namespace nll {
    constexpr std::uint64_t a_billi = 1'000'000'000ull;
inline std::uint64_t mono_ns_raw() noexcept {
    timespec ts; // nanosecond timeval

    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return static_cast<std::uint64_t>(ts.tv_sec) * a_billi+ static_cast<std::uint64_t>(ts.tv_nsec);
}

inline std::chrono::nanoseconds mono_ns() noexcept {
    return std::chrono::nanoseconds{ mono_ns_raw() };
}

// wall clock for logging
inline std::uint64_t real_ns_raw() noexcept {
    timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return static_cast<std::uint64_t>(ts.tv_sec) * a_billi + static_cast<std::uint64_t>(ts.tv_nsec);
}

inline std::chrono::nanoseconds real_ns() noexcept {
    return std::chrono::nanoseconds{ real_ns_raw() };
}

inline void sleep_ns(std::uint64_t ns) noexcept {
    timespec req{
        static_cast<time_t>(ns / a_billi), // s
        static_cast<long>(ns % a_billi) // ns
    };
    while (nanosleep(&req, &req) == -1);
}

} //namespace nll