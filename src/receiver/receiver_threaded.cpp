#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "receiver/receiver_common.hpp"
#include "common/spsc_queue.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <csignal>
#include <cstring>
#include <getopt.h>
#include <sys/socket.h>
#include <thread>
#include <vector>

namespace {
constexpr std::uint32_t max_batch = 1024;
constexpr std::size_t queue_capacity = 4096;
std::atomic<bool> stop_requested{false};
void signal_handler(int) { stop_requested.store(true, std::memory_order_relaxed); }

struct WorkerOutcomes {
  nll::thread::AffinityOutcome affinity;
  nll::thread::SchedulerOutcome scheduler;
};

void usage(std::FILE *out) {
  std::fprintf(out,
      "Usage: receiver_threaded [options]\n"
      "recvmmsg ingress plus SPSC worker handoff; synthetic work runs after dequeue.\n\n"
      "  -o, --output PATH          versioned binary log\n"
      "  -s, --stats PATH           structured JSON statistics\n"
      "  -p, --port PORT            UDP port (1..65535)\n"
      "  -c, --cpu CPU              receiver CPU affinity\n"
      "  -w, --worker-cpu CPU       worker CPU affinity\n"
      "  -b, --batch N              recvmmsg and worker batch size (1..1024)\n"
      "  -n, --max-packets N        stop after N datagrams (0 = unlimited)\n"
      "  -S, --scheduler POLICY     other, fifo, or rr (both threads)\n"
      "  -P, --priority N           scheduler priority\n"
      "  -W, --work NS              worker synthetic work per valid packet\n"
      "  -e, --sample-every N      log every Nth valid packet (0 = counts only)\n"
      "  -B, --socket-buffer BYTES requested SO_RCVBUF size (0 = system default)\n"
      "  -h, --help                 show this help\n");
}
}

int main(int argc, char **argv) {
  nll::receiver::Config config{.variant = "threaded", .worker_cpu = -1, .batch_size = 32};
  const option options[] = {{"output", required_argument, nullptr, 'o'}, {"stats", required_argument, nullptr, 's'},
    {"port", required_argument, nullptr, 'p'}, {"cpu", required_argument, nullptr, 'c'},
    {"worker-cpu", required_argument, nullptr, 'w'}, {"batch", required_argument, nullptr, 'b'},
    {"max-packets", required_argument, nullptr, 'n'}, {"scheduler", required_argument, nullptr, 'S'},
    {"priority", required_argument, nullptr, 'P'}, {"work", required_argument, nullptr, 'W'},
    {"sample-every", required_argument, nullptr, 'e'}, {"socket-buffer", required_argument, nullptr, 'B'},
    {"help", no_argument, nullptr, 'h'}, {nullptr, 0, nullptr, 0}};
  int opt = 0;
  while ((opt = getopt_long(argc, argv, "o:s:p:c:w:b:n:S:P:W:e:B:h", options, nullptr)) != -1) {
    std::uint64_t value = 0;
    switch (opt) {
    case 'o': config.output_path = optarg; break; case 's': config.stats_path = optarg; break;
    case 'p': if (!nll::receiver::parse_u64(optarg, 1, 65535, value, "port")) return 2; config.port = value; break;
    case 'c': if (!nll::receiver::parse_int(optarg, 0, CPU_SETSIZE - 1, config.cpu, "CPU")) return 2; break;
    case 'w': if (!nll::receiver::parse_int(optarg, 0, CPU_SETSIZE - 1, config.worker_cpu, "worker CPU")) return 2; break;
    case 'b': if (!nll::receiver::parse_u64(optarg, 1, max_batch, value, "batch")) return 2; config.batch_size = value; break;
    case 'n': if (!nll::receiver::parse_u64(optarg, 0, UINT64_MAX, config.max_packets, "max packets")) return 2; break;
    case 'S': config.scheduler = optarg; break;
    case 'P': if (!nll::receiver::parse_int(optarg, 0, 99, config.priority, "priority")) return 2; break;
    case 'W': if (!nll::receiver::parse_u64(optarg, 0, UINT64_MAX, config.work_ns, "work")) return 2; break;
    case 'e': if (!nll::receiver::parse_u64(optarg, 0, UINT64_MAX, config.sample_every, "sample every")) return 2; break;
    case 'B': { int bytes = 0; if (!nll::receiver::parse_int(optarg, 0, INT_MAX, bytes, "socket buffer")) return 2; config.socket_buffer_bytes = bytes; break; }
    case 'h': usage(stdout); return 0; default: usage(stderr); return 2;
    }
  }
  if (optind != argc || !nll::receiver::validate_scheduler(config)) return 2;
  if (config.output_path.has_parent_path()) { std::error_code ec; std::filesystem::create_directories(config.output_path.parent_path(), ec); if (ec) return 1; }
  std::signal(SIGINT, signal_handler);
  nll::receiver::ScopedSocket socket(config.socket_buffer_bytes);
  if (!socket.valid() || !nll::receiver::bind_socket(socket.get(), config.port)) return 1;
  auto rx_affinity = nll::receiver::apply_affinity(config.cpu);
  nll::BinaryLogger logger(config.output_path);
  if (!logger.is_open()) return 1;

  nll::SPSCQueue<nll::receiver::ReceivedPacket, queue_capacity> queue;
  std::atomic<bool> producer_done{false};
  nll::receiver::ProcessingStats processing;
  WorkerOutcomes worker_outcomes;
  // POSIX threads inherit their creator's affinity mask and scheduler. Create
  // the worker while the receiver is still SCHED_OTHER so it can migrate from
  // the inherited receiver CPU before either thread is promoted to real time.
  std::thread worker([&] {
    worker_outcomes.affinity = nll::receiver::apply_affinity(config.worker_cpu);
    worker_outcomes.scheduler = nll::thread::set_scheduler(config.scheduler, config.priority);
    while (!producer_done.load(std::memory_order_acquire)) {
      bool found = false;
      for (std::uint32_t i = 0; i < config.batch_size; ++i) {
        auto item = queue.front();
        if (!item) break;
        nll::receiver::process_packet(logger, processing, **item, config.work_ns);
        queue.pop(); found = true;
      }
      if (!found) nll::thread::cpu_relax();
    }
    // Producer has stopped. Drain every packet published before shutdown.
    while (true) {
      auto item = queue.front();
      if (!item) break;
      nll::receiver::process_packet(logger, processing, **item, config.work_ns);
      queue.pop();
    }
  });
  auto rx_scheduler = nll::thread::set_scheduler(config.scheduler, config.priority);

  nll::receiver::Stats stats;
  stats.requested_socket_buffer_bytes = socket.requested_buffer_bytes();
  stats.observed_socket_buffer_bytes = socket.observed_buffer_bytes();
  nll::SequenceTracker receive_sequences;
  std::vector<mmsghdr> messages(config.batch_size);
  std::vector<iovec> vectors(config.batch_size);
  std::vector<std::array<std::byte, sizeof(nll::message_header)>> buffers(config.batch_size);
  for (std::size_t i = 0; i < messages.size(); ++i) {
    vectors[i] = {.iov_base = buffers[i].data(), .iov_len = buffers[i].size()};
    messages[i].msg_hdr.msg_iov = &vectors[i]; messages[i].msg_hdr.msg_iovlen = 1;
  }
  while (!stop_requested.load(std::memory_order_relaxed) && (config.max_packets == 0 || stats.datagrams_received < config.max_packets)) {
    unsigned int count = config.batch_size;
    if (config.max_packets != 0) count = static_cast<unsigned int>(std::min<std::uint64_t>(count, config.max_packets - stats.datagrams_received));
    const int received = ::recvmmsg(socket.get(), messages.data(), count, MSG_WAITFORONE, nullptr);
    const std::uint64_t receive_ts = config.sample_every != 0 ? nll::real_ns() : 0;
    const std::uint64_t receive_mono_ts = nll::mono_ns();
    ++stats.receive_syscalls;
    if (received < 0) { if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) continue; ++stats.socket_errors; break; }
    for (int i = 0; i < received; ++i) {
      ++stats.datagrams_received;
      if (messages[i].msg_len < sizeof(nll::message_header)) { ++stats.short_packets; continue; }
      nll::message_header message{}; std::memcpy(&message, buffers[i].data(), sizeof(message)); message.to_host();
      if (message.magic != 0x6584) { ++stats.invalid_magic; continue; }
      if (message.version != 1) { ++stats.unsupported_version; continue; }
      auto packet = nll::receiver::account_receive(stats, receive_sequences, message,
                                                    receive_ts, receive_mono_ts,
                                                    config.sample_every);
      if (!queue.push(std::move(packet))) ++stats.spsc_overflow;
    }
  }
  stats.interrupted = stop_requested.load(std::memory_order_relaxed);
  stats.socket_pending_bytes_at_shutdown = nll::receiver::pending_socket_bytes(socket.get());
  const auto drain_start = nll::mono_ns();
  stats.queue_depth_at_shutdown = queue.size();
  producer_done.store(true, std::memory_order_release);
  worker.join();
  stats.drain_duration_ns = nll::mono_ns() - drain_start;
  nll::receiver::finalize_receive_sequences(stats, receive_sequences);
  nll::receiver::merge_processing(stats, processing);
  logger.flush();
  return nll::receiver::write_stats(config, stats, rx_affinity, rx_scheduler,
                                    &worker_outcomes.affinity,
                                    &worker_outcomes.scheduler) ? 0 : 1;
}
