 #ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include "common/csv_writer.hpp"
#include "common/log.hpp"
#include "common/packet.hpp"
#include "common/spsc_queue.hpp"
#include "common/thread_utils.hpp"
#include "common/time.hpp"
#include <algorithm>
#include <atomic>
#include <bits/types/struct_timeval.h>
#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <getopt.h>
#include <netinet/in.h>
#include <optional>
#include <stop_token>
#include <sys/socket.h>
#include <thread>

constexpr std::size_t MAX_BATCH_CAPACITY = 1024;

constexpr std::size_t cache_line_size = 64;
struct GlobalConfig {
  bool single_thread_mode = false;
  uint16_t port = 49200;
  uint16_t magic_number = 0x6584;
  std::filesystem::path output_path =
      "/root/net-latency-lab/analysis/data/latency.bin";
  uint32_t max_packets = 10'000;

  int cpu_affinity = 3;
  int worker_affinity = 2;
  int batch_size = 32;
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
      struct timeval tv{0, 100 * 1000};
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
    NLL_WARN("Warn: Invalid Magic: %hu\n", mh.magic);
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

constexpr std::size_t queue_capacity = 4096;
void worker_routine(std::stop_token stoken,
                    nll::SPSCQueue<nll::message_header, queue_capacity> &queue,
                    nll::BinaryLogger<nll::LogEntry> &logger) {
  nll::thread::pin_to_core(g_config.worker_affinity);
  NLL_INFO("Worker thread started on Core %d (Worker Batch: %d).\n",
           g_config.worker_affinity, g_config.batch_size);

  while (!stoken.stop_requested()) {
    bool worked = false;
    for (int i = 0; i < g_config.batch_size; ++i) {
      const auto packet = queue.front();
      if (packet) {
        process_packet(logger, **packet, nll::real_ns());
        queue.pop();
        worked = true;
      } else {
        break;
      }
    }
    if (!worked)
      nll::thread::cpu_relax();
  }
}

void print_usage() { /* Same as before */ }

int main(int argc, char **argv) {
  std::signal(SIGINT, signal_handler);

  struct option long_options[] = {{"output", required_argument, 0, 'o'},
                                  {"port", required_argument, 0, 'p'},
                                  {"cpu", required_argument, 0, 'c'},
                                  {"worker-cpu", required_argument, 0, 'w'},
                                  {"batch", required_argument, 0, 'b'},
                                  {"single-thread", no_argument, 0, 's'},
                                  {0, 0, 0, 0}};
  int opt;
  while ((opt = getopt_long(argc, argv, "o:p:c:w:b:s", long_options,
                            nullptr)) != -1) {
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
    case 'w':
      g_config.worker_affinity = std::stoi(optarg);
      break;
    case 'b':
      g_config.batch_size = std::stoi(optarg);
      break;
    case 's':
      g_config.single_thread_mode = true;
      break;
    case 'W':
      g_config.processing_time_ns = std::stoi(optarg);
      break;
    default:
      print_usage();
      return 1;
    }
  }

  if (g_config.batch_size > static_cast<int>(MAX_BATCH_CAPACITY)) {
    NLL_WARN("Requested batch %d exceeds max %lu. Clamping.\n",
             g_config.batch_size, MAX_BATCH_CAPACITY);
    g_config.batch_size = MAX_BATCH_CAPACITY;
  }

  if (g_config.output_path.has_parent_path()) {
    std::filesystem::create_directories(g_config.output_path.parent_path());
  }
  ScopedSocket sock;
  if (!sock.is_valid())
    return 1;
  sockaddr_in addr{.sin_family = AF_INET,
                   .sin_port = htons(g_config.port),
                   .sin_addr = {.s_addr = INADDR_ANY},
                   .sin_zero = {0}};
  if (bind(sock.get(), reinterpret_cast<struct sockaddr *>(&addr),
           sizeof(addr)) < 0)
    return 1;

  nll::BinaryLogger<nll::LogEntry> logger(g_config.output_path);
  nll::SPSCQueue<nll::message_header, queue_capacity> queue;
  std::optional<std::jthread> worker_thread;

  if (g_config.single_thread_mode) {
    nll::thread::pin_to_core(g_config.cpu_affinity);
    NLL_INFO("Running in SINGLE_THREAD_MODE on Core %d\n",
             g_config.cpu_affinity);
  } else {
    nll::thread::pin_to_core(g_config.cpu_affinity);
    worker_thread.emplace(worker_routine, std::ref(queue), std::ref(logger));
    NLL_INFO(
        "Running in WORKER_THREAD_MODE (RX Core: %d) with recvmmsg batching\n",
        g_config.cpu_affinity);
  }

  nll::thread::set_realtime_priority();

  struct mmsghdr msgs[MAX_BATCH_CAPACITY];
  struct iovec iovecs[MAX_BATCH_CAPACITY];
  nll::message_header buffers[MAX_BATCH_CAPACITY];
  struct timespec timeout{1, 0};

  for (size_t i = 0; i < MAX_BATCH_CAPACITY; i++) {
    iovecs[i].iov_base = &buffers[i];
    iovecs[i].iov_len = sizeof(nll::message_header);
    msgs[i].msg_hdr.msg_iov = &iovecs[i];
    msgs[i].msg_hdr.msg_iovlen = 1;
    msgs[i].msg_hdr.msg_name = nullptr;
    msgs[i].msg_hdr.msg_namelen = 0;
  }

  NLL_INFO("Entering Loop (Batch Size: %d)...\n", g_config.batch_size);

  while (!g_stop_requested) {

    int retval = recvmmsg(sock.get(), msgs, g_config.batch_size, MSG_WAITFORONE,
                          &timeout);

    if (retval < 0) {
      // EAGAIN/EWOULDBLOCK (10) is normal when a timeout is set and no data
      // arrives.
      if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) {
        continue;
      }
      NLL_ERROR("recvmmsg errno %d: %s\n", errno, std::strerror(errno));
      break;
    }

    uint64_t now_ts = nll::real_ns();

    for (int i = 0; i < retval; i++) {
      if (msgs[i].msg_len >= sizeof(nll::message_header)) {
        buffers[i].to_host();

        if (g_config.single_thread_mode) {
          process_packet(logger, buffers[i], now_ts);
        } else {
          if (!queue.push(std::move(buffers[i]))) {
            g_stats.dropped_packets++;
          }
        }
      }
    }
  }

  NLL_INFO("Shutting Down. Processed: %lu, Dropped: %lu\n",
           g_stats.packets_processed.load(), g_stats.dropped_packets.load());
  return 0;
}
