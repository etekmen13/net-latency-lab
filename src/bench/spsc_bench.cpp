// Measures the SPSC handoff cost that decides whether pipelining can win.
//
// The pipelined receiver replaces "receive then process on one core" with
// "receive on one core, process on another", so its zero-loss capacity is
// 1/max(r_rx + p, W + c) instead of 1/(r + W), where p and c are the producer-
// and consumer-side handoff costs. Pipelining only pays when the handoff costs
// less than the receive work it removes from the critical path, i.e. p + c < r.
// With r measured at 1.66 us (recvfrom) and 1.03 us (recvmmsg b64), a handoff
// above ~1 us makes the architecture a loss no matter how the campaign is run,
// so this is a go/no-go gate that must be settled before spending hours of
// hardware time on a rate sweep.
//
// Reports producer and consumer cost separately because they land on different
// cores and therefore do not add: only the consumer term shares a core with the
// per-packet work budget.
//
// Usage: spsc_bench [producer_cpu] [consumer_cpu] [elements] [repetitions]

#include "common/spsc_queue.hpp"
#include "common/thread_utils.hpp"
#include "common/time.hpp"

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <thread>
#include <vector>

namespace {

// Mirrors the payload the threaded receiver actually hands across.
struct Item {
  std::uint32_t seq_idx;
  std::uint64_t send_unix_ns;
  std::uint64_t receive_real_ns;
  bool sampled;
};

constexpr std::size_t capacity = 4096;

struct Result {
  double producer_ns_per_item;
  double consumer_ns_per_item;
  double throughput_items_per_second;
  std::uint64_t overflows;
};

// Same queue, same item, one thread: push then immediately pop. No cross-core
// traffic at all, so this isolates the queue's own bookkeeping from the cache
// coherence cost. The gap between this and the two-thread case is what index
// caching and cache-line isolation can actually remove.
Result run_uncontended(int cpu, std::uint64_t elements) {
  nll::SPSCQueue<Item, capacity> queue;
  if (cpu >= 0) nll::thread::pin_to_core(cpu);
  const auto started = nll::mono_ns();
  for (std::uint64_t index = 0; index < elements; ++index) {
    Item item{static_cast<std::uint32_t>(index), index, index, false};
    if (!queue.push(std::move(item))) continue;
    auto held = queue.front();
    if (held && (**held).seq_idx == 0xFFFFFFFFU) std::fputs("", stderr);
    queue.pop();
  }
  const auto elapsed = nll::mono_ns() - started;
  const double per_item = static_cast<double>(elapsed) / static_cast<double>(elements);
  // One push plus one pop per iteration, so split the round trip evenly.
  return {per_item / 2, per_item / 2,
          static_cast<double>(elements) * 1e9 / static_cast<double>(elapsed), 0};
}

Result run_once(int producer_cpu, int consumer_cpu, std::uint64_t elements,
                bool drain_slowly) {
  nll::SPSCQueue<Item, capacity> queue;
  std::atomic<bool> producer_done{false};
  std::atomic<std::uint64_t> overflows{0};
  std::atomic<std::uint64_t> consumer_ns{0};
  std::atomic<std::uint64_t> consumed{0};

  std::thread consumer([&] {
    if (consumer_cpu >= 0) nll::thread::pin_to_core(consumer_cpu);
    std::uint64_t local = 0;
    const auto started = nll::mono_ns();
    while (true) {
      auto item = queue.front();
      if (!item) {
        if (producer_done.load(std::memory_order_acquire)) {
          if (!queue.front()) break;
          continue;
        }
        nll::thread::cpu_relax();
        continue;
      }
      // Touch the payload so the compiler cannot elide the read, and so the
      // cache-line transfer the queue exists to perform is actually paid for.
      if ((**item).seq_idx == 0xFFFFFFFFU) std::fputs("", stderr);
      queue.pop();
      ++local;
      if (drain_slowly) for (int spin = 0; spin < 8; ++spin) nll::thread::cpu_relax();
    }
    consumer_ns.store(nll::mono_ns() - started, std::memory_order_release);
    consumed.store(local, std::memory_order_release);
  });

  if (producer_cpu >= 0) nll::thread::pin_to_core(producer_cpu);
  const auto producer_started = nll::mono_ns();
  std::uint64_t dropped = 0;
  for (std::uint64_t index = 0; index < elements; ++index) {
    Item item{static_cast<std::uint32_t>(index), index, index, false};
    while (!queue.push(std::move(item))) {
      ++dropped;
      nll::thread::cpu_relax();
    }
  }
  const auto producer_elapsed = nll::mono_ns() - producer_started;
  producer_done.store(true, std::memory_order_release);
  consumer.join();
  overflows.store(dropped, std::memory_order_relaxed);

  const auto consumer_elapsed = consumer_ns.load(std::memory_order_acquire);
  return {static_cast<double>(producer_elapsed) / static_cast<double>(elements),
          static_cast<double>(consumer_elapsed) / static_cast<double>(elements),
          static_cast<double>(elements) * 1e9 / static_cast<double>(producer_elapsed),
          dropped};
}

double median(std::vector<double> values) {
  std::sort(values.begin(), values.end());
  return values[values.size() / 2];
}

} // namespace

int main(int argc, char **argv) {
  const int producer_cpu = argc > 1 ? std::atoi(argv[1]) : 3;
  const int consumer_cpu = argc > 2 ? std::atoi(argv[2]) : 2;
  const std::uint64_t elements = argc > 3 ? std::strtoull(argv[3], nullptr, 10) : 10'000'000;
  const int repetitions = argc > 4 ? std::atoi(argv[4]) : 5;

  std::printf("{\n  \"producer_cpu\": %d,\n  \"consumer_cpu\": %d,\n"
              "  \"elements\": %llu,\n  \"repetitions\": %d,\n  \"item_bytes\": %zu,\n"
              "  \"capacity\": %zu,\n  \"cases\": [\n",
              producer_cpu, consumer_cpu,
              static_cast<unsigned long long>(elements), repetitions,
              sizeof(Item), capacity);

  // "uncontended" is one thread doing push/pop with no cross-core traffic:
  // the queue's own bookkeeping. "cross_core" is the real configuration, a
  // producer and consumer on separate cores running flat out. The difference is
  // the cache coherence cost, which is what the receiver actually pays.
  const struct { const char *name; bool uncontended; } cases[] = {
      {"uncontended", true}, {"cross_core", false}};
  bool first_case = true;
  for (const auto &scenario : cases) {
    std::vector<double> producer, consumer, throughput;
    std::uint64_t overflows = 0;
    for (int repetition = 0; repetition < repetitions; ++repetition) {
      const auto result = scenario.uncontended
          ? run_uncontended(producer_cpu, elements)
          : run_once(producer_cpu, consumer_cpu, elements, false);
      producer.push_back(result.producer_ns_per_item);
      consumer.push_back(result.consumer_ns_per_item);
      throughput.push_back(result.throughput_items_per_second);
      overflows += result.overflows;
    }
    std::printf("%s    {\"scenario\": \"%s\", \"producer_ns_per_item\": %.4f, "
                "\"consumer_ns_per_item\": %.4f, \"handoff_ns_per_item\": %.4f, "
                "\"throughput_items_per_second\": %.1f, \"producer_retries\": %llu}",
                first_case ? "" : ",\n", scenario.name,
                median(producer), median(consumer),
                median(producer) + median(consumer), median(throughput),
                static_cast<unsigned long long>(overflows));
    first_case = false;
  }
  std::printf("\n  ]\n}\n");
  return 0;
}
