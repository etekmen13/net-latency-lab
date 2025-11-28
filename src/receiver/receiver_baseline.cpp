#include "common/csv_writer.hpp"
#include "common/log.hpp"
#include "common/packet.hpp"
#include "common/thread_utils.hpp"
#include "common/time.hpp"

#include <atomic>
#include <csignal>
#include <cstdio>
#include <filesystem>
#include <netinet/in.h>
#include <sys/socket.h>

struct Config {
  static constexpr uint16_t port = 49200;
  static constexpr uint16_t magic_number = 0x6584;
  static constexpr uint32_t max_packets = 10'000;
};

std::atomic<bool> g_stop_requested{false};

void signal_handler(int) { g_stop_requested = true; }

class ScopedSocket {
  int fd_;

public:
  ScopedSocket() : fd_(socket(AF_INET, SOCK_DGRAM, 0)) {
    if (fd_ >= 0) {
      // 100ms timeout
      struct timeval tv{.tv_sec = 0, .tv_usec = 100 * 1000};
      setsockopt(fd_, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    }
  }
  ~ScopedSocket() {
    if (fd_ >= 0)
      close(fd_);
  }

  ScopedSocket(const ScopedSocket &) = delete;
  ScopedSocket &operator=(const ScopedSocket &) = delete;

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
    NLL_WARN("Invalid Magic: %x\n", mh.magic);
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

int main(int argc, char **argv) {
  std::signal(SIGINT, signal_handler);

  std::filesystem::path output_path = "latency_baseline.bin";

  if (argc <= 1) {
    NLL_INFO("Please provide output path for data writing.\n");
  }
  if (argc > 1) {
    output_path = argv[1];
  }

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

  nll::thread::pin_to_core(3);
  nll::thread::set_realtime_priority();

  NLL_INFO("Baseline Receiver (Synchronous) running on Core 3...\n");
  NLL_INFO("Logging to: %s\n", output_path.string().c_str());

  {
    nll::BinaryLogger<nll::LogEntry> logger(output_path);
    nll::message_header packet;

    while (!g_stop_requested &&
           g_stats.packets_processed < Config::max_packets) {
      ssize_t len =
          recvfrom(sock.get(), &packet, sizeof(packet), 0, nullptr, nullptr);
      uint64_t rx_ts = nll::real_ns();

      if (len > 0) {
        if (static_cast<size_t>(len) < sizeof(nll::message_header)) {
          g_stats.dropped_packets.fetch_add(1, std::memory_order_relaxed);
          continue;
        }

        packet.to_host();
        process_packet(logger, packet, rx_ts);
      }
    }
  }

  NLL_INFO("\nShutdown.\n");
  NLL_INFO("  Processed: %lu\n", g_stats.packets_processed.load());
  NLL_INFO("  Dropped:   %lu\n", g_stats.dropped_packets.load());

  return 0;
}
