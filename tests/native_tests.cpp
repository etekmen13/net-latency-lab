#include "common/csv_writer.hpp"
#include "common/sequence_tracker.hpp"
#include "common/spsc_queue.hpp"
#include "sender/sender_common.hpp"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <thread>
#include <vector>

#include <gtest/gtest.h>

namespace {

TEST(BinaryLog, ExactHeaderBytes) {
  const auto bytes = nll::encode_log_header();
  const std::array<unsigned int, 16> expected{
      'N', 'L', 'L', 'O', 'G', 0, '\r', '\n', 1, 0, 16, 0, 36, 0, 0, 0};
  for (std::size_t index = 0; index < bytes.size(); ++index)
    EXPECT_EQ(std::to_integer<unsigned int>(bytes[index]), expected[index]);
  EXPECT_NO_THROW(nll::validate_log_header(bytes));
}

TEST(BinaryLog, ExactRecordBytesAndRoundTrip) {
  const nll::LogEntry entry{0x04030201U, 0x0807060504030201ULL,
      0x1817161514131211ULL, 0x2827262524232221ULL,
      0x3837363534333231ULL};
  const auto bytes = nll::encode_log_entry(entry);
  for (std::size_t index = 0; index < 4; ++index)
    EXPECT_EQ(std::to_integer<unsigned int>(bytes[index]), index + 1);
  EXPECT_EQ(nll::decode_log_entry(bytes), entry);
}

TEST(BinaryLog, EndianConversion) {
  std::array<std::byte, 8> bytes{};
  nll::encode_le<std::uint64_t>(0x8877665544332211ULL, bytes);
  EXPECT_EQ(std::to_integer<unsigned int>(bytes.front()), 0x11U);
  EXPECT_EQ(std::to_integer<unsigned int>(bytes.back()), 0x88U);
  EXPECT_EQ(nll::decode_le<std::uint64_t>(bytes), 0x8877665544332211ULL);
}

TEST(BinaryLog, RejectsUnsupportedVersionAndTruncation) {
  auto header = nll::encode_log_header();
  header[8] = std::byte{2};
  EXPECT_THROW(nll::validate_log_header(header), std::invalid_argument);
  EXPECT_THROW(nll::validate_log_header(std::span<const std::byte>(header).first(15)),
               std::invalid_argument);
  EXPECT_THROW(nll::decode_log_entry(std::span<const std::byte>(header)),
               std::invalid_argument);
}

TEST(SequenceTracker, CountsUniqueGapsDuplicatesAndReordering) {
  nll::SequenceTracker tracker;
  for (const auto value : {10U, 11U, 13U, 13U, 12U, 15U}) tracker.observe(value);
  EXPECT_EQ(tracker.unique(), 5U);
  EXPECT_EQ(tracker.gaps(), 1U);
  EXPECT_EQ(tracker.duplicates(), 1U);
  EXPECT_EQ(tracker.reordered(), 1U);
  EXPECT_EQ(tracker.out_of_window(), 0U);
}

// The bounded ring must stay exact for the stream the generator actually emits:
// dense, monotonically increasing, far longer than the 65536-sequence window.
TEST(SequenceTracker, BoundedRingStaysExactOverADenseStream) {
  nll::SequenceTracker tracker;
  constexpr std::uint32_t total = 400000; // ~6 full wraps of the ring
  for (std::uint32_t value = 0; value < total; ++value) tracker.observe(value);
  EXPECT_EQ(tracker.unique(), total);
  EXPECT_EQ(tracker.gaps(), 0U);
  EXPECT_EQ(tracker.duplicates(), 0U);
  EXPECT_EQ(tracker.reordered(), 0U);
  EXPECT_EQ(tracker.out_of_window(), 0U);
}

// Duplicates are still detected exactly while they remain inside the window,
// and reordering within the window is counted without being mistaken for loss.
TEST(SequenceTracker, DetectsDuplicatesAndReorderingInsideTheWindow) {
  nll::SequenceTracker tracker;
  for (std::uint32_t value = 0; value < 100000; ++value) tracker.observe(value);
  EXPECT_TRUE(tracker.observe(100001U));  // leaves 100000 missing
  EXPECT_FALSE(tracker.observe(99000U));  // duplicate, still inside the window
  EXPECT_FALSE(tracker.observe(100001U)); // duplicate of the newest
  EXPECT_TRUE(tracker.observe(100000U));  // fills the gap, counted as reordered
  EXPECT_EQ(tracker.duplicates(), 2U);
  EXPECT_EQ(tracker.reordered(), 1U);
  EXPECT_EQ(tracker.gaps(), 0U);
  EXPECT_EQ(tracker.out_of_window(), 0U);
}

// An arrival older than the retained window cannot be proven unique, so it is
// reported separately rather than silently counted as a new packet.
TEST(SequenceTracker, ReportsArrivalsOlderThanTheRetainedWindow) {
  nll::SequenceTracker tracker;
  for (std::uint32_t value = 0; value < 200000; ++value) tracker.observe(value);
  EXPECT_EQ(tracker.out_of_window(), 0U);
  EXPECT_TRUE(tracker.observe(5U)); // far below the retained 65536-wide window
  EXPECT_EQ(tracker.out_of_window(), 1U);
  EXPECT_EQ(tracker.duplicates(), 0U);
  EXPECT_EQ(tracker.reordered(), 1U);
}

TEST(SPSCQueue, EmptyFullAndWraparound) {
  nll::SPSCQueue<std::uint64_t, 8> queue;
  EXPECT_TRUE(queue.empty());
  for (std::uint64_t value = 0; value < queue.usable_capacity(); ++value)
    EXPECT_TRUE(queue.push(std::move(value)));
  std::uint64_t extra = 99;
  EXPECT_FALSE(queue.push(std::move(extra)));
  for (std::uint64_t expected = 0; expected < 20; ++expected) {
    auto front = queue.front();
    ASSERT_TRUE(front.has_value());
    EXPECT_EQ(**front, expected);
    queue.pop();
    if (expected + queue.usable_capacity() < 20) {
      auto next = expected + queue.usable_capacity();
      EXPECT_TRUE(queue.push(std::move(next)));
    }
  }
  EXPECT_TRUE(queue.empty());
}

TEST(SPSCQueue, ConcurrentOrderedTransfers) {
  constexpr std::uint64_t count = 2'000'000;
  nll::SPSCQueue<std::uint64_t, 4096> queue;
  std::atomic<bool> producer_done{false};
  std::atomic<bool> failed{false};
  std::thread producer([&] {
    for (std::uint64_t value = 0; value < count; ++value) {
      while (!queue.push(std::move(value))) std::this_thread::yield();
    }
    producer_done.store(true, std::memory_order_release);
  });
  std::uint64_t expected = 0;
  while (!producer_done.load(std::memory_order_acquire) || !queue.empty()) {
    auto front = queue.front();
    if (!front) { std::this_thread::yield(); continue; }
    if (**front != expected) failed.store(true, std::memory_order_relaxed);
    ++expected;
    queue.pop();
  }
  producer.join();
  EXPECT_FALSE(failed.load());
  EXPECT_EQ(expected, count);
}

TEST(SPSCQueue, ShutdownDrainsPublishedData) {
  nll::SPSCQueue<std::uint64_t, 1024> queue;
  for (std::uint64_t value = 0; value < 500; ++value)
    ASSERT_TRUE(queue.push(std::move(value)));
  bool producer_done = true;
  std::vector<std::uint64_t> drained;
  while (!producer_done || !queue.empty()) {
    auto front = queue.front();
    if (!front) continue;
    drained.push_back(**front);
    queue.pop();
  }
  ASSERT_EQ(drained.size(), 500U);
  for (std::size_t index = 0; index < drained.size(); ++index)
    EXPECT_EQ(drained[index], index);
}

TEST(SenderPacing, IntegerDeadlinesAndAdaptiveBatchesDoNotDrift) {
  constexpr std::uint64_t start = 123'456'789;
  EXPECT_EQ(nll::sender::deadline_ns(start, 950'000, 950'000),
            start + 1'000'000'000ULL);
  EXPECT_EQ(nll::sender::scheduled_packet_count(10'000'000'000ULL, 950'000),
            9'500'000ULL);
  EXPECT_EQ(nll::sender::adaptive_batch_count(0, 1000, 1, 950'000, 64, 10'000),
            10U);
  EXPECT_EQ(nll::sender::adaptive_batch_count(998, 1000, 1, 950'000, 64, 100'000),
            2U);
  EXPECT_EQ(nll::sender::adaptive_batch_count(0, 1000, 2, 950'000, 64, 10'000),
            5U);
}

// The scheduling window, not --send-batch-max, sets the batch size whenever
// fewer than send_batch_max packets are due inside it.  On the Pi 4 at 950k pps
// this caps a nominal 64-message batch at 10 messages for --batch-window-us 10.
TEST(SenderPacing, BatchWindowBindsBelowSendBatchMax) {
  constexpr std::uint64_t limit = 100'000'000;
  EXPECT_EQ(nll::sender::adaptive_batch_count(0, limit, 1, 950'000, 64, 10'000), 10U);
  EXPECT_EQ(nll::sender::adaptive_batch_count(0, limit, 1, 950'000, 64, 50'000), 48U);
  // Only a window wide enough to cover 64 packets lets send_batch_max bind.
  EXPECT_EQ(nll::sender::adaptive_batch_count(0, limit, 1, 950'000, 64, 100'000), 64U);
  // Worker striping divides the per-thread packet rate, so the same window
  // admits proportionally fewer messages per worker.
  EXPECT_EQ(nll::sender::adaptive_batch_count(0, limit, 3, 150'000, 64, 100'000), 6U);
}

TEST(SenderSubmission, PartialErrorsAndRetriesAreExplicit) {
  auto complete = nll::sender::classify_sendmmsg_result(4, 0, 4);
  EXPECT_EQ(complete.successful, 4U);
  EXPECT_FALSE(complete.partial);
  auto partial = nll::sender::classify_sendmmsg_result(2, 0, 4);
  EXPECT_EQ(partial.successful, 2U);
  EXPECT_TRUE(partial.partial);
  auto interrupted = nll::sender::classify_sendmmsg_result(-1, EINTR, 4);
  EXPECT_TRUE(interrupted.retry);
  EXPECT_EQ(interrupted.failed, 0U);
  auto failed = nll::sender::classify_sendmmsg_result(-1, ENETUNREACH, 4);
  EXPECT_EQ(failed.failed, 4U);
  EXPECT_EQ(failed.error, ENETUNREACH);
}

TEST(SenderSubmission, AtomicRangesAreUniqueAndContiguous) {
  std::atomic<std::uint64_t> next{0};
  std::vector<std::uint64_t> starts(4);
  std::vector<std::thread> workers;
  for (std::size_t index = 0; index < starts.size(); ++index)
    workers.emplace_back([&, index] {
      starts[index] = nll::sender::allocate_sequence_range(next, 16);
    });
  for (auto &worker : workers) worker.join();
  std::sort(starts.begin(), starts.end());
  EXPECT_EQ(starts, (std::vector<std::uint64_t>{0, 16, 32, 48}));
  EXPECT_EQ(next.load(), 64U);
}

} // namespace
