#include "receiver/receiver_common.hpp"

#include <atomic>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <getopt.h>

namespace {
std::atomic<bool> stop_requested{false};
void signal_handler(int) { stop_requested.store(true, std::memory_order_relaxed); }

void usage(std::FILE *out) {
  std::fprintf(out,
      "Usage: receiver_baseline [options]\n"
      "Synchronous recvfrom receiver; synthetic work runs inline.\n\n"
      "  -o, --output PATH          versioned binary log (default latency.bin)\n"
      "  -s, --stats PATH           structured JSON statistics\n"
      "  -p, --port PORT            UDP port (1..65535)\n"
      "  -c, --cpu CPU              receiver CPU affinity\n"
      "  -n, --max-packets N        stop after N datagrams (0 = unlimited)\n"
      "  -S, --scheduler POLICY     other, fifo, or rr\n"
      "  -P, --priority N           0 for other; 1..99 for fifo/rr\n"
      "  -W, --work NS              synthetic processing per valid packet\n"
      "  -e, --sample-every N      log every Nth valid packet (0 = counts only)\n"
      "  -B, --socket-buffer BYTES requested SO_RCVBUF size (0 = system default)\n"
      "  -h, --help                 show this help\n");
}
}

int main(int argc, char **argv) {
  nll::receiver::Config config{.variant = "baseline"};
  const option options[] = {{"output", required_argument, nullptr, 'o'},
                            {"stats", required_argument, nullptr, 's'},
                            {"port", required_argument, nullptr, 'p'},
                            {"cpu", required_argument, nullptr, 'c'},
                            {"max-packets", required_argument, nullptr, 'n'},
                            {"scheduler", required_argument, nullptr, 'S'},
                            {"priority", required_argument, nullptr, 'P'},
                            {"work", required_argument, nullptr, 'W'},
                            {"sample-every", required_argument, nullptr, 'e'},
                            {"socket-buffer", required_argument, nullptr, 'B'},
                            {"help", no_argument, nullptr, 'h'},
                            {nullptr, 0, nullptr, 0}};
  int opt = 0;
  while ((opt = getopt_long(argc, argv, "o:s:p:c:n:S:P:W:e:B:h", options, nullptr)) != -1) {
    std::uint64_t value = 0;
    switch (opt) {
    case 'o': config.output_path = optarg; break;
    case 's': config.stats_path = optarg; break;
    case 'p':
      if (!nll::receiver::parse_u64(optarg, 1, 65535, value, "port")) return 2;
      config.port = static_cast<std::uint16_t>(value); break;
    case 'c':
      if (!nll::receiver::parse_int(optarg, 0, CPU_SETSIZE - 1, config.cpu, "CPU")) return 2;
      break;
    case 'n':
      if (!nll::receiver::parse_u64(optarg, 0, UINT64_MAX, config.max_packets, "max packets")) return 2;
      break;
    case 'S': config.scheduler = optarg; break;
    case 'P':
      if (!nll::receiver::parse_int(optarg, 0, 99, config.priority, "priority")) return 2;
      break;
    case 'W':
      if (!nll::receiver::parse_u64(optarg, 0, UINT64_MAX, config.work_ns, "work")) return 2;
      break;
    case 'e':
      if (!nll::receiver::parse_u64(optarg, 0, UINT64_MAX, config.sample_every, "sample every")) return 2;
      break;
    case 'B': {
      int bytes = 0;
      if (!nll::receiver::parse_int(optarg, 0, INT_MAX, bytes, "socket buffer")) return 2;
      config.socket_buffer_bytes = bytes; break;
    }
    case 'h': usage(stdout); return 0;
    default: usage(stderr); return 2;
    }
  }
  if (optind != argc || !nll::receiver::validate_scheduler(config)) return 2;
  if (config.output_path.has_parent_path()) {
    std::error_code ec;
    std::filesystem::create_directories(config.output_path.parent_path(), ec);
    if (ec) { std::fprintf(stderr, "Cannot create output directory: %s\n", ec.message().c_str()); return 1; }
  }

  std::signal(SIGINT, signal_handler);
  nll::receiver::ScopedSocket socket(config.socket_buffer_bytes);
  if (!socket.valid() || !nll::receiver::bind_socket(socket.get(), config.port)) return 1;
  auto affinity = nll::receiver::apply_affinity(config.cpu);
  auto scheduler = nll::thread::set_scheduler(config.scheduler, config.priority);
  nll::BinaryLogger logger(config.output_path);
  if (!logger.is_open()) return 1;
  nll::receiver::Stats stats;
  stats.requested_socket_buffer_bytes = socket.requested_buffer_bytes();
  stats.observed_socket_buffer_bytes = socket.observed_buffer_bytes();
  nll::SequenceTracker receive_sequences;
  nll::receiver::ProcessingStats processing;
  std::byte buffer[65535];

  while (!stop_requested.load(std::memory_order_relaxed) &&
         (config.max_packets == 0 || stats.datagrams_received < config.max_packets)) {
    const ssize_t length = ::recvfrom(socket.get(), buffer, sizeof(buffer), 0, nullptr, nullptr);
    const std::uint64_t receive_ts = config.sample_every != 0 ? nll::real_ns() : 0;
    const std::uint64_t receive_mono_ts = nll::mono_ns();
    ++stats.receive_syscalls;
    if (length < 0) {
      if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) continue;
      ++stats.socket_errors; break;
    }
    ++stats.datagrams_received;
    if (static_cast<std::size_t>(length) < sizeof(nll::message_header)) {
      ++stats.short_packets; continue;
    }
    nll::message_header message{};
    std::memcpy(&message, buffer, sizeof(message));
    message.to_host();
    if (message.magic != 0x6584) { ++stats.invalid_magic; continue; }
    if (message.version != 1) { ++stats.unsupported_version; continue; }
    auto packet = nll::receiver::account_receive(stats, receive_sequences, message,
                                                  receive_ts, receive_mono_ts,
                                                  config.sample_every);
    nll::receiver::process_packet(logger, processing, packet, config.work_ns);
  }
  stats.interrupted = stop_requested.load(std::memory_order_relaxed);
  stats.socket_pending_bytes_at_shutdown = nll::receiver::pending_socket_bytes(socket.get());
  nll::receiver::finalize_receive_sequences(stats, receive_sequences);
  nll::receiver::merge_processing(stats, processing);
  logger.flush();
  const bool stats_ok = nll::receiver::write_stats(config, stats, affinity, scheduler);
  return stats_ok ? 0 : 1;
}
