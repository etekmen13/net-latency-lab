#include "common/csv_writer.hpp"
#include "common/log.hpp"
#include "common/packet.hpp"
#include "common/thread_utils.hpp"
#include "common/time.hpp"

#include <atomic>
#include <csignal>
#include <cstdio>
#include <filesystem>
#include <getopt.h>
#include <netinet/in.h>
#include <sys/socket.h>
constexpr std::size_t cache_line_size = 64;
struct GlobalConfig {
  uint16_t port = 49200;
  uint16_t magic_number = 0x6584;
  std::filesystem::path output_path = "latency_baseline.bin";
  uint32_t max_packets = 100'000;
  int cpu_affinity = 3;
  uint64_t processing_time_ns = 0;
};

GlobalConfig g_config;
std::atomic<bool> g_stop_requested{false};

void signal_handler(int) { g_stop_requested = true; }

class ScopedSocket {
  int fd_;

public:
  ScopedSocket() : fd_(socket(AF_INET, SOCK_DGRAM, 0)) {
    if (fd_ >= 0) {
      struct timeval tv{.tv_sec = 0, .tv_usec = 100 * 1000};
      setsockopt(fd_, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
      int opt = 1;
      if (setsockopt(fd_, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt)) < 0) {
          perror("setsockopt(SO_REUSEADDR) failed");
      }
      if (setsockopt(fd_, SOL_SOCKET, SO_REUSEPORT, &opt, sizeof(opt)) < 0) {
          perror("setsockopt(SO_REUSEPORT) failed");
      }
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

struct alignas(cache_line_size) Stats {
  std::atomic<uint64_t> packets_processed{0};
  std::atomic<uint64_t> accumulated_latency_ns{0};
  std::atomic<uint64_t> dropped_packets{0};
};

Stats g_stats;

void process_packet(nll::BinaryLogger<nll::LogEntry> &logger,
                    const nll::message_header &mh, uint64_t rx_time) {

  if (mh.magic != g_config.magic_number) {
    NLL_WARN("Invalid Magic: %x\n", mh.magic);
    return;
  }
  if (g_config.processing_time_ns > 0) {
    uint64_t start = nll::mono_ns();
    while (nll::mono_ns() - start < g_config.processing_time_ns) {
      nll::thread::cpu_relax();
    }
  }
  int64_t latency = rx_time - mh.send_unix_ns;

  logger.log({.seq_idx = mh.seq_idx,
              .tx_ts = mh.send_unix_ns,
              .rx_ts = rx_time,
              .latency_ns = latency});

  g_stats.packets_processed.fetch_add(1, std::memory_order_relaxed);
  g_stats.accumulated_latency_ns.fetch_add(latency, std::memory_order_relaxed);
}

void print_usage() {
  printf("Usage: receiver_baseline [options]\n");
  printf("Options:\n");
  printf("  -o, --output <path>    Path to output bin file\n");
  printf("  -p, --port <port>      UDP port to bind (default: 49200)\n");
  printf("  -c, --cpu <id>         CPU core to pin to (default: 3)\n");
}

int main(int argc, char **argv) {
  std::signal(SIGINT, signal_handler);

  struct option long_options[] = {{"output", required_argument, 0, 'o'},
                                  {"port", required_argument, 0, 'p'},
                                  {"cpu", required_argument, 0, 'c'},
                                  {"work", required_argument, 0, 'W'},
                                  {0, 0, 0, 0}};

  int opt;
  while ((opt = getopt_long(argc, argv, "o:p:c:W", long_options, nullptr)) !=
         -1) {
    switch (opt) {
    case 'o':
      g_config.output_path = optarg;
      break;
    case 'p':
      g_config.port = static_cast<uint16_t>(std::stoi(optarg));
      break;
    case 'c':
      g_config.cpu_affinity = std::stoi(optarg);
      break;
    case 'W':
      g_config.processing_time_ns = std::stoi(optarg);
      break;
    default:
      print_usage();
      return 1;
    }
  }

  if (g_config.output_path.has_parent_path()) {
    std::filesystem::create_directories(g_config.output_path.parent_path());
  }

  ScopedSocket sock;
  if (!sock.is_valid()) {
    NLL_ERROR("Failed to create socket\n");
    return 1;
  }

  sockaddr_in addr{.sin_family = AF_INET,
                   .sin_port = htons(g_config.port),
                   .sin_addr = {.s_addr = INADDR_ANY},
                   .sin_zero = {0}};

  if (bind(sock.get(), reinterpret_cast<struct sockaddr *>(&addr),
           sizeof(addr)) < 0) {
    NLL_ERROR("Bind failed on port %d\n", g_config.port);
    return 1;
  }

  nll::thread::pin_to_core(g_config.cpu_affinity);
  nll::thread::set_realtime_priority();

  NLL_INFO("Baseline Receiver (Synchronous) running on Core %d...\n",
           g_config.cpu_affinity);
  NLL_INFO("Logging to: %s\n", g_config.output_path.c_str());

  {
    nll::BinaryLogger<nll::LogEntry> logger(g_config.output_path);
    nll::message_header packet;

    while (!g_stop_requested) {
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
