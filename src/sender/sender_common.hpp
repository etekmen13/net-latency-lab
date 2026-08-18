#pragma once

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <cstdint>
#include <limits>

namespace nll::sender {

__extension__ typedef unsigned __int128 uint128;

struct SendResult {
  std::uint32_t successful = 0;
  std::uint32_t failed = 0;
  bool retry = false;
  bool partial = false;
  int error = 0;
};

inline SendResult classify_sendmmsg_result(int result, int error,
                                           std::uint32_t requested) noexcept {
  if (result > 0) {
    const auto successful = std::min<std::uint32_t>(result, requested);
    return {.successful = successful, .partial = successful < requested};
  }
  if (result < 0 && error == EINTR) return {.retry = true, .error = error};
  return {.failed = requested, .error = result < 0 ? error : EIO};
}

inline std::uint64_t allocate_sequence_range(std::atomic<std::uint64_t> &next,
                                             std::uint32_t count) noexcept {
  return next.fetch_add(count, std::memory_order_relaxed);
}

inline std::uint64_t deadline_ns(std::uint64_t start_ns,
                                 std::uint64_t packet_index,
                                 std::uint64_t rate_pps) noexcept {
  const auto offset = (static_cast<uint128>(packet_index) *
                       1'000'000'000ULL) / rate_pps;
  const auto maximum = std::numeric_limits<std::uint64_t>::max();
  return offset > maximum - start_ns ? maximum
                                    : start_ns + static_cast<std::uint64_t>(offset);
}

inline std::uint64_t scheduled_packet_count(std::uint64_t duration_ns,
                                            std::uint64_t rate_pps) noexcept {
  const auto product = static_cast<uint128>(duration_ns) * rate_pps;
  return static_cast<std::uint64_t>((product + 999'999'999ULL) /
                                    1'000'000'000ULL);
}

inline std::uint32_t adaptive_batch_count(std::uint64_t first_packet_index,
                                          std::uint64_t packet_limit,
                                          std::uint64_t packet_stride,
                                          std::uint64_t rate_pps,
                                          std::uint32_t batch_max,
                                          std::uint64_t batch_window_ns) noexcept {
  if (first_packet_index >= packet_limit || packet_stride == 0 || batch_max == 0)
    return 0;
  const auto first_offset = (static_cast<uint128>(first_packet_index) *
                             1'000'000'000ULL) / rate_pps;
  std::uint32_t count = 1;
  while (count < batch_max) {
    const auto index = static_cast<uint128>(first_packet_index) +
                       static_cast<uint128>(count) * packet_stride;
    if (index >= packet_limit) break;
    const auto offset = (index * 1'000'000'000ULL) / rate_pps;
    if (offset - first_offset > batch_window_ns) break;
    ++count;
  }
  return count;
}

} // namespace nll::sender
