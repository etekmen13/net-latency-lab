#pragma once

#include <array>
#include <cstdint>
#include <limits>

namespace nll {

// Exact online accounting without retaining one record per packet.
//
// The bitmap is a direct-mapped ring of block_count blocks, each covering
// block_size sequence numbers, so the whole tracker is a fixed 8 KiB and stays
// resident in L1. The previous implementation used an std::unordered_map keyed
// on the block index, which cost a hash, a modulus and a node chase on every
// packet -- twice per packet, since both the receive and processing paths run a
// tracker -- and grew without bound (563 KiB per tracker over a 30 s run at
// 150 kpps, larger than the Pi 4's shared 1 MiB L2). That made the accounting
// instrumentation a material part of the measured per-packet cost, and it cost
// the two variants unequally: the threaded receiver spreads its two trackers
// across two cores while the synchronous variants serialise both on one.
//
// Retaining block_count * block_size sequence numbers bounds how far back a
// duplicate can be detected. The generator emits a dense, monotonically
// increasing stream from a single thread, and any run whose reordering exceeds
// 0.01% is disqualified upstream, so 65536 sequence numbers of lookback is far
// more history than the stream can use. Arrivals older than the retained window
// are counted in out_of_window() rather than being silently treated as unique.
class SequenceTracker {
public:
  bool observe(std::uint32_t sequence) {
    const std::uint64_t epoch = std::uint64_t{sequence >> block_shift} + 1;
    const std::size_t slot = (sequence >> block_shift) & (block_count - 1);
    std::uint64_t &resident = epochs_[slot];
    Block *block = &blocks_[slot];
    if (resident != epoch) {
      if (resident > epoch) {
        // Older than everything the ring still holds: the bits that would prove
        // whether this is a duplicate have already been retired.
        ++out_of_window_;
        block = nullptr;
      } else {
        block->fill(0);
        resident = epoch;
      }
    }
    if (block != nullptr) {
      const auto within = sequence & (block_size - 1);
      const auto word = within >> 6;
      const auto mask = std::uint64_t{1} << (within & 63U);
      if ((*block)[word] & mask) {
        ++duplicates_;
        return false;
      }
      (*block)[word] |= mask;
    }
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
  [[nodiscard]] std::uint64_t out_of_window() const noexcept { return out_of_window_; }
  [[nodiscard]] std::uint64_t gaps() const noexcept {
    if (unique_ == 0) return 0;
    const std::uint64_t span =
        static_cast<std::uint64_t>(high_watermark_) - minimum_ + 1;
    // An out-of-window arrival is counted as unique because its duplicate bit
    // has already been retired, so unique_ can exceed the observed span. Clamp
    // rather than wrapping an unsigned subtraction into a nonsense gap count.
    if (unique_ >= span) return 0;
    return span - unique_;
  }

private:
  static constexpr std::uint32_t block_shift = 12;
  static constexpr std::uint32_t block_size = 1U << block_shift;
  static constexpr std::size_t block_count = 16; // 65536 sequence numbers, 8 KiB
  static_assert((block_count & (block_count - 1)) == 0,
                "block_count must be a power of 2 for the direct-mapped index");
  using Block = std::array<std::uint64_t, block_size / 64>;
  std::array<Block, block_count> blocks_{};
  // Resident epoch per slot, stored biased by one so that zero means empty.
  std::array<std::uint64_t, block_count> epochs_{};
  std::uint64_t unique_ = 0;
  std::uint64_t duplicates_ = 0;
  std::uint64_t reordered_ = 0;
  std::uint64_t out_of_window_ = 0;
  std::uint32_t minimum_ = std::numeric_limits<std::uint32_t>::max();
  std::uint32_t high_watermark_ = 0;
};

} // namespace nll
