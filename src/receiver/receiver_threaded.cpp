#include "common/csv_writer.hpp"
#include "common/log.hpp"
#include "common/packet.hpp"
#include "common/spsc_queue.hpp"
#include "common/thread_utils.hpp"
#include "common/time.hpp"
#include <atomic>
#include <bits/types/struct_timeval.h>
#include <csignal>
#include <cstdio>
#include <filesystem>
#include <netinet/in.h>
#include <optional>
#include <stop_token>
#include <sys/socket.h>
#include <thread>

// define this to switch modes
// #define SINGLE_THREAD_MODE

struct Config {
  static constexpr bool single_thread_mode = false;
  static constexpr uint16_t port = 49200;
  static constexpr std::size_t queue_capacity = 1024;
  static constexpr uint16_t magic_number = 0x6584; // ET
  static constexpr std::string_view default_outfile =
      "/root/net-latency-lab/analysis/data/latency.bin";
  static constexpr uint32_t max_packets = 10'000;
};

std::atomic<bool> g_stop_requested{false};

void signal_handler(int) { g_stop_requested = true; }

class ScopedSocket {
  int fd_;

public:
  ScopedSocket() : fd_(socket(AF_INET, SOCK_DGRAM, 0)) {
    if (fd_ >= 0) {
      struct timeval tv;
      tv.tv_sec = 0;

      tv.tv_usec = 100 * 1000; // 100ms timeout
      setsockopt(fd_, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    }
    // set timeout on recvfrom to check for shutdown signals
    // otherwise recvfrom blocks forever and ignores Ctrl+C
  }
  ~ScopedSocket() {
    if (fd_ >= 0)
      close(fd_);
  }

  ScopedSocket(const ScopedSocket &) = delete;
  ScopedSocket &operator=(const ScopedSocket &) = delete;
  ScopedSocket(ScopedSocket &&other) noexcept : fd_(other.fd_) {
    other.fd_ = -1;
  }

  [[nodiscard]] int get() const { return fd_; }
  [[nodiscard]] bool is_valid() const { return fd_ >= 0; }
};

struct alignas(std::hardware_destructive_interference_size) Stats {
  std::atomic<uint64_t> packets_processed{0};
  std::atomic<uint64_t> accumulated_latency_ns{0};
  std::atomic<uint64_t> dropped_packets{0};
};

Stats g_stats;

void process_packet(nll::BinaryLogger<nll::LogEntry> &logger,
                    const nll::message_header &mh, uint64_t rx_time) {

  if (mh.magic != Config::magic_number) {
    NLL_ERROR("Warn: Invalid Magic: %hu\n", mh.magic);
    return;
  }

  int64_t latency = rx_time - mh.send_unix_ns;

  logger.log({.seq_idx = mh.seq_idx,
              .tx_ts = mh.send_unix_ns,
              .rx_ts = rx_time,
              .latency_ns = latency});
  g_stats.packets_processed.fetch_add(1, std::memory_order_relaxed);
  g_stats.accumulated_latency_ns.fetch_add(latency, std::memory_order_relaxed);
}
constexpr std::size_t queue_capacity = 1024;
void worker_routine(std::stop_token stoken,
                    nll::SPSCQueue<nll::message_header, queue_capacity> &queue,
                    nll::BinaryLogger<nll::LogEntry> &logger) {

  nll::thread::pin_to_core(3);

  NLL_INFO("Worker thread started on Core 2.\n");

  while (!stoken.stop_requested()) {
    const auto packet =
        queue.front(); // returns std::expected<const T*, std::string_view>
    if (!packet) {
      // spin loop
      nll::thread::cpu_relax();
    } else {
      process_packet(logger, **packet, nll::real_ns());

      queue.pop();
    }
  }
}

int main(int argc, char **argv) {
  std::signal(SIGINT, signal_handler);
  std::filesystem::path output_path = Config::default_outfile;
  if (argc <= 1) {
    NLL_INFO("Please provide an output path for data writing.\n");
  }
  if (argc > 1) {
    output_path = argv[1];
  }

  if (output_path.has_parent_path()) {
    std::filesystem::create_directories(output_path.parent_path());
  }

  NLL_INFO("Logging data to %s", output_path.c_str());
  ScopedSocket sock;
  if (!sock.is_valid()) {
    NLL_ERROR("Failed to create socket\n");
    return 1;
  }

  sockaddr_in addr{.sin_family = AF_INET,
                   .sin_port = htons(Config::port),
                   .sin_addr = {.s_addr = INADDR_ANY},
                   .sin_zero = {0}};

  if (bind(sock.get(), reinterpret_cast<struct sockaddr *>(&addr),
           sizeof(addr)) < 0) {
    NLL_ERROR("Bind failed on port %d\n", Config::port);
    return 1;
  }
  nll::BinaryLogger<nll::LogEntry> logger(output_path);
  nll::SPSCQueue<nll::message_header, queue_capacity> queue;
  std::optional<std::jthread> worker_thread;
  if constexpr (Config::single_thread_mode) {

    nll::thread::pin_to_core(3);
    NLL_INFO("Runnin in SINGLE_THREAD_MODE (eRPC Style)\n");
  } else {

    nll::thread::pin_to_core(2);
    worker_thread.emplace(worker_routine, std::ref(queue), std::ref(logger));
    NLL_INFO("Running in WORKER_THREAD_MODE\n");
  }
  nll::thread::set_realtime_priority();
  nll::message_header packet;

  while (!g_stop_requested && g_stats.packets_processed < Config::max_packets) {
    ssize_t len =
        recvfrom(sock.get(), &packet, sizeof(packet), 0, nullptr, nullptr);

    uint64_t rx_ts = nll::real_ns();

    if (len > 0) {
      packet.to_host();
      if constexpr (Config::single_thread_mode) {

        process_packet(logger, packet, rx_ts);
      } else {
        if (!queue.push(std::move(packet))) {
          g_stats.dropped_packets++;
        }
      }
    }
  }

  NLL_INFO("Shutting Down...\n");
  NLL_INFO(" Processed: %d\n", g_stats.packets_processed.load());
  NLL_INFO(" Dropped: %d\n", g_stats.dropped_packets.load());
  return 0;
}
