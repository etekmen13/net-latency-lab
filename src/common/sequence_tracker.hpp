#pragma once

#include <array>
#include <cstdint>
#include <limits>
#include <unordered_map>

namespace nll {

// Exact online accounting without retaining one record per packet. The sparse
// bitmap uses 512 bytes per 4096 sequence numbers and is cheap for the sender's
// dense, monotonically increasing stream.
class SequenceTracker {
public:
  bool observe(std::uint32_t sequence) {
    auto &block = blocks_[sequence >> block_shift];
    const auto within = sequence & (block_size - 1);
    const auto word = within >> 6;
    const auto mask = std::uint64_t{1} << (within & 63U);
    if (block[word] & mask) {
      ++duplicates_;
      return false;
    }
    block[word] |= mask;
    if (unique_ != 0 && sequence < high_watermark_) ++reordered_;
    if (unique_ == 0) minimum_ = sequence;
    if (sequence < minimum_) minimum_ = sequence;
    if (sequence > high_watermark_) high_watermark_ = sequence;
    ++unique_;
    return true;
  }

  [[nodiscard]] std::uint64_t unique() const noexcept { return unique_; }
  [[nodiscard]] std::uint64_t duplicates() const noexcept { return duplicates_; }
  [[nodiscard]] std::uint64_t reordered() const noexcept { return reordered_; }
  [[nodiscard]] std::uint64_t gaps() const noexcept {
    if (unique_ == 0) return 0;
    return static_cast<std::uint64_t>(high_watermark_) - minimum_ + 1 - unique_;
  }

private:
  static constexpr std::uint32_t block_shift = 12;
  static constexpr std::uint32_t block_size = 1U << block_shift;
  using Block = std::array<std::uint64_t, block_size / 64>;
  std::unordered_map<std::uint32_t, Block> blocks_;
  std::uint64_t unique_ = 0;
  std::uint64_t duplicates_ = 0;
  std::uint64_t reordered_ = 0;
  std::uint32_t minimum_ = std::numeric_limits<std::uint32_t>::max();
  std::uint32_t high_watermark_ = 0;
};

} // namespace nll
