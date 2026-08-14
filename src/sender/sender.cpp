#include "common/packet.hpp"
#include "common/thread_utils.hpp"
#include "common/time.hpp"

#include <arpa/inet.h>
#include <cerrno>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <getopt.h>
#include <limits>
#include <string>
#include <string_view>
#include <sys/socket.h>
#include <unistd.h>
#include <vector>

namespace {
struct Config {
  std::string destination = "127.0.0.1";
  std::uint16_t port = 49200;
  std::uint64_t rate_pps = 1000;
  double duration_seconds = 1.0;
  std::string mode = "steady";
  std::uint64_t burst_size = 1;
  std::uint32_t payload_size = sizeof(nll::message_header);
  int cpu = -1;
  int socket_buffer_bytes = 0;
  std::uint64_t timestamp_every = 1;
  std::filesystem::path stats_path = "sender_stats.json";
};

struct Stats {
  std::uint64_t attempted_sends = 0;
  std::uint64_t successful_sends = 0;
  std::uint64_t failed_sends = 0;
  std::uint64_t successful_bytes = 0;
  std::uint64_t elapsed_ns = 0;
  int last_error = 0;
  int requested_socket_buffer_bytes = 0;
  int observed_socket_buffer_bytes = 0;
};

template <typename T>
bool parse_unsigned(std::string_view text, T min, T max, T &value, const char *name) {
  T parsed{};
  const auto result = std::from_chars(text.data(), text.data() + text.size(), parsed);
  if (text.empty() || result.ec != std::errc{} || result.ptr != text.data() + text.size() || parsed < min || parsed > max) {
    std::fprintf(stderr, "Invalid %s: %.*s\n", name, static_cast<int>(text.size()), text.data());
    return false;
  }
  value = parsed; return true;
}

bool parse_duration(std::string_view text, double &value) {
  std::string copy(text);
  char *end = nullptr;
  errno = 0;
  const double parsed = std::strtod(copy.c_str(), &end);
  constexpr double maximum_duration_seconds =
      static_cast<double>(std::numeric_limits<std::uint64_t>::max()) / 1e9;
  if (errno != 0 || end != copy.c_str() + copy.size() || !std::isfinite(parsed) ||
      parsed <= 0.0 || parsed > maximum_duration_seconds) {
    std::fprintf(stderr, "Invalid duration: %s\n", copy.c_str()); return false;
  }
  value = parsed; return true;
}

std::string escape(std::string_view value) {
  std::string out;
  for (char c : value) { if (c == '\\') out += "\\\\"; else if (c == '"') out += "\\\""; else out += c; }
  return out;
}

bool write_stats(const Config &config, const Stats &stats,
                 const nll::thread::AffinityOutcome &affinity) {
  if (config.stats_path.has_parent_path()) {
    std::error_code ec; std::filesystem::create_directories(config.stats_path.parent_path(), ec);
    if (ec) { std::fprintf(stderr, "Cannot create stats directory: %s\n", ec.message().c_str()); return false; }
  }
  std::FILE *file = std::fopen(config.stats_path.c_str(), "w");
  if (!file) { std::fprintf(stderr, "Cannot open sender stats: %s\n", std::strerror(errno)); return false; }
  const double elapsed_seconds = static_cast<double>(stats.elapsed_ns) / 1e9;
  const double achieved = elapsed_seconds > 0.0 ? static_cast<double>(stats.successful_sends) / elapsed_seconds : 0.0;
  std::fprintf(file,
      "{\n"
      "  \"schema_version\": 1,\n"
      "  \"destination_ip\": \"%s\",\n"
      "  \"port\": %u,\n"
      "  \"requested_rate_pps\": %llu,\n"
      "  \"requested_duration_seconds\": %.9f,\n"
      "  \"mode\": \"%s\",\n"
      "  \"burst_size\": %llu,\n"
      "  \"payload_size\": %u,\n"
      "  \"timestamp_every\": %llu,\n"
      "  \"attempted_sends\": %llu,\n"
      "  \"successful_sends\": %llu,\n"
      "  \"failed_sends\": %llu,\n"
      "  \"successful_bytes\": %llu,\n"
      "  \"elapsed_ns\": %llu,\n"
      "  \"elapsed_seconds\": %.9f,\n"
      "  \"achieved_successful_send_pps\": %.6f,\n"
      "  \"last_errno\": %d,\n"
      "  \"requested_socket_buffer_bytes\": %d,\n"
      "  \"observed_socket_buffer_bytes\": %d,\n"
      "  \"cpu_affinity\": {\"requested_cpu\": %d, \"observed_cpu\": %d, \"observed_cpu_set\": \"%s\", \"success\": %s, \"error\": \"%s\"}\n"
      "}\n",
      escape(config.destination).c_str(), config.port,
      static_cast<unsigned long long>(config.rate_pps), config.duration_seconds,
      config.mode.c_str(), static_cast<unsigned long long>(config.burst_size), config.payload_size,
      static_cast<unsigned long long>(config.timestamp_every),
      static_cast<unsigned long long>(stats.attempted_sends),
      static_cast<unsigned long long>(stats.successful_sends),
      static_cast<unsigned long long>(stats.failed_sends),
      static_cast<unsigned long long>(stats.successful_bytes),
      static_cast<unsigned long long>(stats.elapsed_ns), elapsed_seconds, achieved,
      stats.last_error, stats.requested_socket_buffer_bytes,
      stats.observed_socket_buffer_bytes, affinity.requested, affinity.observed,
      escape(affinity.observed_cpu_set).c_str(),
      affinity.success ? "true" : "false", escape(affinity.error).c_str());
  return std::fclose(file) == 0;
}

void usage(std::FILE *out) {
  std::fprintf(out,
      "Usage: sender [options]\n\n"
      "  -i, --ip ADDRESS           destination IPv4 address\n"
      "  -p, --port PORT            destination UDP port\n"
      "  -r, --rate PPS             requested aggregate packet rate\n"
      "  -d, --duration SECONDS     positive runtime (fractional allowed)\n"
      "  -m, --mode MODE            steady or burst\n"
      "  -b, --burst N              packets per burst\n"
      "  -l, --payload-size BYTES   total UDP payload, 16..65507\n"
      "  -c, --cpu CPU              sender CPU affinity\n"
      "  -s, --stats PATH           structured JSON statistics\n"
      "  -B, --socket-buffer BYTES requested SO_SNDBUF size (0 = system default)\n"
      "  -T, --timestamp-every N   timestamp sequence multiples of N (0 = none)\n"
      "  -h, --help                 show this help\n");
}
}

int main(int argc, char **argv) {
  Config config;
  const option options[] = {{"ip", required_argument, nullptr, 'i'}, {"port", required_argument, nullptr, 'p'},
    {"rate", required_argument, nullptr, 'r'}, {"duration", required_argument, nullptr, 'd'},
    {"mode", required_argument, nullptr, 'm'}, {"burst", required_argument, nullptr, 'b'},
    {"payload-size", required_argument, nullptr, 'l'}, {"cpu", required_argument, nullptr, 'c'},
    {"stats", required_argument, nullptr, 's'}, {"socket-buffer", required_argument, nullptr, 'B'},
    {"timestamp-every", required_argument, nullptr, 'T'},
    {"help", no_argument, nullptr, 'h'}, {nullptr, 0, nullptr, 0}};
  int opt = 0;
  while ((opt = getopt_long(argc, argv, "i:p:r:d:m:b:l:c:s:B:T:h", options, nullptr)) != -1) {
    std::uint64_t value = 0;
    switch (opt) {
    case 'i': config.destination = optarg; break;
    case 'p': if (!parse_unsigned<std::uint64_t>(optarg, 1, 65535, value, "port")) return 2; config.port = value; break;
    case 'r': if (!parse_unsigned<std::uint64_t>(optarg, 1, UINT64_MAX, config.rate_pps, "rate")) return 2; break;
    case 'd': if (!parse_duration(optarg, config.duration_seconds)) return 2; break;
    case 'm': config.mode = optarg; break;
    case 'b': if (!parse_unsigned<std::uint64_t>(optarg, 1, UINT32_MAX, config.burst_size, "burst")) return 2; break;
    case 'l': if (!parse_unsigned<std::uint64_t>(optarg, sizeof(nll::message_header), 65507, value, "payload size")) return 2; config.payload_size = value; break;
    case 'c': { int parsed = 0; const std::string_view text(optarg); const auto result = std::from_chars(text.data(), text.data()+text.size(), parsed); if (result.ec != std::errc{} || result.ptr != text.data()+text.size() || parsed < 0 || parsed >= CPU_SETSIZE) { std::fprintf(stderr, "Invalid CPU: %s\n", optarg); return 2; } config.cpu = parsed; break; }
    case 's': config.stats_path = optarg; break;
    case 'B': { int parsed = 0; const std::string_view text(optarg); const auto result = std::from_chars(text.data(), text.data()+text.size(), parsed); if (result.ec != std::errc{} || result.ptr != text.data()+text.size() || parsed < 0) { std::fprintf(stderr, "Invalid socket buffer: %s\n", optarg); return 2; } config.socket_buffer_bytes = parsed; break; }
    case 'T': if (!parse_unsigned<std::uint64_t>(optarg, 0, UINT64_MAX, config.timestamp_every, "timestamp every")) return 2; break;
    case 'h': usage(stdout); return 0; default: usage(stderr); return 2;
    }
  }
  if (optind != argc) { usage(stderr); return 2; }
  if (config.mode != "steady" && config.mode != "burst") { std::fprintf(stderr, "Invalid mode: %s\n", config.mode.c_str()); return 2; }
  if (config.mode == "steady" && config.burst_size != 1) { std::fprintf(stderr, "Steady mode requires --burst 1\n"); return 2; }
  sockaddr_in destination{}; destination.sin_family = AF_INET; destination.sin_port = htons(config.port);
  if (::inet_pton(AF_INET, config.destination.c_str(), &destination.sin_addr) != 1) { std::fprintf(stderr, "Invalid IPv4 address: %s\n", config.destination.c_str()); return 2; }
  const int socket_fd = ::socket(AF_INET, SOCK_DGRAM, 0);
  if (socket_fd < 0) { std::fprintf(stderr, "socket failed: %s\n", std::strerror(errno)); return 1; }
  if (config.socket_buffer_bytes > 0 &&
      ::setsockopt(socket_fd, SOL_SOCKET, SO_SNDBUF, &config.socket_buffer_bytes,
                   sizeof(config.socket_buffer_bytes)) < 0) {
    std::fprintf(stderr, "SO_SNDBUF request failed: %s\n", std::strerror(errno));
  }

  nll::thread::AffinityOutcome affinity{.requested = -1,
      .observed = sched_getcpu(),
      .observed_cpu_set = nll::thread::current_affinity_set(),
      .success = true, .error = ""};
  if (config.cpu >= 0) affinity = nll::thread::pin_to_core(config.cpu);
  std::vector<std::byte> payload(config.payload_size);
  const long double interval_ns = 1.0e9L / static_cast<long double>(config.rate_pps);
  const std::uint64_t duration_ns = static_cast<std::uint64_t>(config.duration_seconds * 1e9);
  if (duration_ns == 0) {
    std::fprintf(stderr, "Duration must be at least one nanosecond\n");
    ::close(socket_fd);
    return 2;
  }
  const std::uint64_t start = nll::mono_ns();
  const std::uint64_t end = start + duration_ns;
  long double next_send = static_cast<long double>(start);
  std::uint32_t sequence = 0;
  Stats stats;
  stats.requested_socket_buffer_bytes = config.socket_buffer_bytes;
  socklen_t socket_buffer_length = sizeof(stats.observed_socket_buffer_bytes);
  if (::getsockopt(socket_fd, SOL_SOCKET, SO_SNDBUF,
                   &stats.observed_socket_buffer_bytes, &socket_buffer_length) < 0)
    stats.observed_socket_buffer_bytes = -1;
  while (nll::mono_ns() < end) {
    const std::uint64_t now = nll::mono_ns();
    if (static_cast<long double>(now) < next_send) {
      const std::uint64_t remaining = static_cast<std::uint64_t>(next_send - now);
      if (remaining > 300'000) nll::sleep_ns(remaining - 100'000);
      else nll::thread::cpu_relax();
      continue;
    }
    const std::uint64_t count = config.mode == "burst" ? config.burst_size : 1;
    for (std::uint64_t i = 0; i < count; ++i) {
      const bool timestamped = config.timestamp_every != 0 && sequence % config.timestamp_every == 0;
      nll::message_header message{.magic = 0x6584, .version = 1, .msg_type = 0,
                                  .seq_idx = sequence,
                                  .send_unix_ns = timestamped ? nll::real_ns() : 0};
      message.to_network(); std::memcpy(payload.data(), &message, sizeof(message));
      ++stats.attempted_sends;
      const ssize_t sent = ::sendto(socket_fd, payload.data(), payload.size(), 0,
          reinterpret_cast<const sockaddr *>(&destination), sizeof(destination));
      if (sent == static_cast<ssize_t>(payload.size())) {
        ++stats.successful_sends; stats.successful_bytes += static_cast<std::uint64_t>(sent); ++sequence;
      } else { ++stats.failed_sends; stats.last_error = sent < 0 ? errno : EMSGSIZE; }
    }
    next_send += interval_ns * static_cast<long double>(count);
  }
  stats.elapsed_ns = nll::mono_ns() - start;
  ::close(socket_fd);
  return write_stats(config, stats, affinity) ? 0 : 1;
}
