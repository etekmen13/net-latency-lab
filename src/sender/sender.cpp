#include "common/packet.hpp"
#include "common/thread_utils.hpp"
#include "common/time.hpp"
#include "sender/sender_common.hpp"

#include <arpa/inet.h>
#include <atomic>
#include <barrier>
#include <cerrno>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <getopt.h>
#include <limits>
#include <numeric>
#include <string>
#include <string_view>
#include <sys/socket.h>
#include <thread>
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
  std::uint32_t send_batch_max = 1;
  std::uint64_t batch_window_us = 10;
  std::uint32_t threads = 1;
  std::vector<int> cpus;
  std::filesystem::path stats_path = "sender_stats.json";
  std::filesystem::path pacing_trace_path;
};

struct TraceRecord {
  std::uint64_t actual_mono_ns;
  std::uint64_t scheduled_mono_ns;
  std::uint64_t first_sequence;
  std::uint32_t packet_count;
  std::uint32_t thread_index;
};

struct WorkerStats {
  std::uint64_t attempted_sends = 0;
  std::uint64_t successful_sends = 0;
  std::uint64_t failed_sends = 0;
  std::uint64_t successful_bytes = 0;
  std::uint64_t syscall_count = 0;
  std::uint64_t partial_returns = 0;
  std::uint64_t error_returns = 0;
  std::uint64_t lateness_samples = 0;
  std::uint64_t lateness_sum_ns = 0;
  std::uint64_t lateness_max_ns = 0;
  int last_error = 0;
  int observed_socket_buffer_bytes = -1;
  nll::thread::AffinityOutcome affinity{.requested = -1, .observed = -1,
      .observed_cpu_set = "", .success = true, .error = ""};
  std::vector<std::uint64_t> batch_histogram;
  std::vector<std::uint64_t> lateness_ns;
  std::vector<TraceRecord> trace;
};

struct Stats : WorkerStats {
  std::uint64_t elapsed_ns = 0;
  int requested_socket_buffer_bytes = 0;
};

template <typename T>
bool parse_unsigned(std::string_view text, T min, T max, T &value,
                    const char *name) {
  T parsed{};
  const auto result = std::from_chars(text.data(), text.data() + text.size(), parsed);
  if (text.empty() || result.ec != std::errc{} ||
      result.ptr != text.data() + text.size() || parsed < min || parsed > max) {
    std::fprintf(stderr, "Invalid %s: %.*s\n", name,
                 static_cast<int>(text.size()), text.data());
    return false;
  }
  value = parsed;
  return true;
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
    std::fprintf(stderr, "Invalid duration: %s\n", copy.c_str());
    return false;
  }
  value = parsed;
  return true;
}

bool parse_cpu(std::string_view text, int &cpu) {
  const auto result = std::from_chars(text.data(), text.data() + text.size(), cpu);
  return !text.empty() && result.ec == std::errc{} &&
         result.ptr == text.data() + text.size() && cpu >= 0 && cpu < CPU_SETSIZE;
}

bool parse_cpu_list(std::string_view text, std::vector<int> &cpus) {
  cpus.clear();
  while (!text.empty()) {
    const auto comma = text.find(',');
    const auto item = text.substr(0, comma);
    int cpu = -1;
    if (!parse_cpu(item, cpu) ||
        std::find(cpus.begin(), cpus.end(), cpu) != cpus.end()) return false;
    cpus.push_back(cpu);
    if (comma == std::string_view::npos) break;
    text.remove_prefix(comma + 1);
    if (text.empty()) return false;
  }
  return !cpus.empty();
}

std::string escape(std::string_view value) {
  std::string out;
  for (char c : value) {
    if (c == '\\') out += "\\\\";
    else if (c == '"') out += "\\\"";
    else out += c;
  }
  return out;
}

std::uint64_t percentile(std::vector<std::uint64_t> values, double quantile) {
  if (values.empty()) return 0;
  const auto index = static_cast<std::size_t>(
      std::ceil(quantile * static_cast<double>(values.size())) - 1.0);
  std::nth_element(values.begin(), values.begin() + std::min(index, values.size() - 1),
                   values.end());
  return values[std::min(index, values.size() - 1)];
}

bool write_trace(const Config &config, const std::vector<WorkerStats> &workers) {
  if (config.pacing_trace_path.empty()) return true;
  if (config.pacing_trace_path.has_parent_path()) {
    std::error_code ec;
    std::filesystem::create_directories(config.pacing_trace_path.parent_path(), ec);
    if (ec) {
      std::fprintf(stderr, "Cannot create pacing trace directory: %s\n",
                   ec.message().c_str());
      return false;
    }
  }
  std::vector<TraceRecord> records;
  std::size_t count = 0;
  for (const auto &worker : workers) count += worker.trace.size();
  records.reserve(count);
  for (const auto &worker : workers)
    records.insert(records.end(), worker.trace.begin(), worker.trace.end());
  std::sort(records.begin(), records.end(), [](const auto &left, const auto &right) {
    return left.actual_mono_ns < right.actual_mono_ns;
  });
  std::FILE *file = std::fopen(config.pacing_trace_path.c_str(), "w");
  if (!file) {
    std::fprintf(stderr, "Cannot open pacing trace: %s\n", std::strerror(errno));
    return false;
  }
  std::fprintf(file, "actual_mono_ns,scheduled_mono_ns,first_sequence,packet_count,thread_index\n");
  for (const auto &record : records)
    std::fprintf(file, "%llu,%llu,%llu,%u,%u\n",
        static_cast<unsigned long long>(record.actual_mono_ns),
        static_cast<unsigned long long>(record.scheduled_mono_ns),
        static_cast<unsigned long long>(record.first_sequence),
        record.packet_count, record.thread_index);
  return std::fclose(file) == 0;
}

bool write_stats(const Config &config, Stats stats,
                 const std::vector<WorkerStats> &workers,
                 std::uint64_t start_ns, std::uint64_t end_ns) {
  if (config.stats_path.has_parent_path()) {
    std::error_code ec;
    std::filesystem::create_directories(config.stats_path.parent_path(), ec);
    if (ec) {
      std::fprintf(stderr, "Cannot create stats directory: %s\n", ec.message().c_str());
      return false;
    }
  }
  std::FILE *file = std::fopen(config.stats_path.c_str(), "w");
  if (!file) {
    std::fprintf(stderr, "Cannot open sender stats: %s\n", std::strerror(errno));
    return false;
  }
  const double elapsed_seconds = static_cast<double>(stats.elapsed_ns) / 1e9;
  const double achieved = elapsed_seconds > 0.0
      ? static_cast<double>(stats.successful_sends) / elapsed_seconds : 0.0;
  const auto p50 = percentile(stats.lateness_ns, .50);
  const auto p90 = percentile(stats.lateness_ns, .90);
  const auto p99 = percentile(stats.lateness_ns, .99);
  std::fprintf(file,
      "{\n"
      "  \"schema_version\": 2,\n"
      "  \"destination_ip\": \"%s\",\n"
      "  \"port\": %u,\n"
      "  \"requested_rate_pps\": %llu,\n"
      "  \"requested_duration_seconds\": %.9f,\n"
      "  \"mode\": \"%s\",\n"
      "  \"burst_size\": %llu,\n"
      "  \"payload_size\": %u,\n"
      "  \"timestamp_every\": %llu,\n"
      "  \"send_batch_max\": %u,\n"
      "  \"batch_window_us\": %llu,\n"
      "  \"threads\": %u,\n"
      "  \"attempted_sends\": %llu,\n"
      "  \"successful_sends\": %llu,\n"
      "  \"failed_sends\": %llu,\n"
      "  \"successful_bytes\": %llu,\n"
      "  \"syscall_count\": %llu,\n"
      "  \"partial_returns\": %llu,\n"
      "  \"error_returns\": %llu,\n"
      "  \"elapsed_ns\": %llu,\n"
      "  \"elapsed_seconds\": %.9f,\n"
      "  \"achieved_successful_send_pps\": %.6f,\n"
      "  \"start_mono_ns\": %llu,\n"
      "  \"scheduled_end_mono_ns\": %llu,\n"
      "  \"last_errno\": %d,\n"
      "  \"requested_socket_buffer_bytes\": %d,\n"
      "  \"observed_socket_buffer_bytes\": %d,\n"
      "  \"pacing_lateness_ns\": {\"samples\": %llu, \"mean\": %.3f, \"p50\": %llu, \"p90\": %llu, \"p99\": %llu, \"max\": %llu},\n"
      "  \"pacing_trace\": {\"enabled\": %s, \"path\": \"%s\", \"format\": \"csv-v1\", \"records\": %zu},\n",
      escape(config.destination).c_str(), config.port,
      static_cast<unsigned long long>(config.rate_pps), config.duration_seconds,
      config.mode.c_str(), static_cast<unsigned long long>(config.burst_size),
      config.payload_size, static_cast<unsigned long long>(config.timestamp_every),
      config.send_batch_max, static_cast<unsigned long long>(config.batch_window_us),
      config.threads, static_cast<unsigned long long>(stats.attempted_sends),
      static_cast<unsigned long long>(stats.successful_sends),
      static_cast<unsigned long long>(stats.failed_sends),
      static_cast<unsigned long long>(stats.successful_bytes),
      static_cast<unsigned long long>(stats.syscall_count),
      static_cast<unsigned long long>(stats.partial_returns),
      static_cast<unsigned long long>(stats.error_returns),
      static_cast<unsigned long long>(stats.elapsed_ns), elapsed_seconds, achieved,
      static_cast<unsigned long long>(start_ns), static_cast<unsigned long long>(end_ns),
      stats.last_error, stats.requested_socket_buffer_bytes,
      stats.observed_socket_buffer_bytes,
      static_cast<unsigned long long>(stats.lateness_samples),
      stats.lateness_samples ? static_cast<double>(stats.lateness_sum_ns) / stats.lateness_samples : 0.0,
      static_cast<unsigned long long>(p50), static_cast<unsigned long long>(p90),
      static_cast<unsigned long long>(p99),
      static_cast<unsigned long long>(stats.lateness_max_ns),
      config.pacing_trace_path.empty() ? "false" : "true",
      escape(config.pacing_trace_path.string()).c_str(), stats.trace.size());
  std::fprintf(file, "  \"effective_batch_size_histogram\": {");
  bool first = true;
  for (std::size_t size = 1; size < stats.batch_histogram.size(); ++size) {
    if (!stats.batch_histogram[size]) continue;
    std::fprintf(file, "%s\"%zu\": %llu", first ? "" : ", ", size,
                 static_cast<unsigned long long>(stats.batch_histogram[size]));
    first = false;
  }
  std::fprintf(file, "},\n  \"thread_outcomes\": [\n");
  for (std::size_t index = 0; index < workers.size(); ++index) {
    const auto &worker = workers[index];
    std::fprintf(file,
        "    {\"thread_index\": %zu, \"attempted_sends\": %llu, \"successful_sends\": %llu, \"failed_sends\": %llu, \"syscall_count\": %llu, \"partial_returns\": %llu, \"error_returns\": %llu, \"requested_cpu\": %d, \"observed_cpu\": %d, \"observed_cpu_set\": \"%s\", \"affinity_success\": %s, \"affinity_error\": \"%s\"}%s\n",
        index, static_cast<unsigned long long>(worker.attempted_sends),
        static_cast<unsigned long long>(worker.successful_sends),
        static_cast<unsigned long long>(worker.failed_sends),
        static_cast<unsigned long long>(worker.syscall_count),
        static_cast<unsigned long long>(worker.partial_returns),
        static_cast<unsigned long long>(worker.error_returns), worker.affinity.requested,
        worker.affinity.observed, escape(worker.affinity.observed_cpu_set).c_str(),
        worker.affinity.success ? "true" : "false", escape(worker.affinity.error).c_str(),
        index + 1 == workers.size() ? "" : ",");
  }
  const auto &affinity = workers.front().affinity;
  std::fprintf(file,
      "  ],\n"
      "  \"cpu_affinity\": {\"requested_cpu\": %d, \"observed_cpu\": %d, \"observed_cpu_set\": \"%s\", \"success\": %s, \"error\": \"%s\"}\n"
      "}\n",
      affinity.requested, affinity.observed,
      escape(affinity.observed_cpu_set).c_str(), affinity.success ? "true" : "false",
      escape(affinity.error).c_str());
  return std::fclose(file) == 0;
}

void usage(std::FILE *out) {
  std::fprintf(out,
      "Usage: sender [options]\n\n"
      "  -i, --ip ADDRESS           destination IPv4 address\n"
      "  -p, --port PORT            destination UDP port\n"
      "  -r, --rate PPS             requested aggregate packet rate\n"
      "  -d, --duration SECONDS     positive runtime (fractional allowed)\n"
      "  -m, --mode MODE            steady, burst, or flood\n"
      "  -b, --burst N              packets per burst\n"
      "  -l, --payload-size BYTES   total UDP payload, 16..65507\n"
      "  -c, --cpu CPU              sender CPU affinity (one thread)\n"
      "  -s, --stats PATH           structured JSON statistics\n"
      "  -B, --socket-buffer BYTES requested SO_SNDBUF size (0 = system default)\n"
      "  -T, --timestamp-every N    timestamp sequence multiples of N (0 = none)\n"
      "      --send-batch-max N     maximum messages per sendmmsg call, 1..1024\n"
      "      --batch-window-us U    maximum scheduled span per batch, 1..1000000 us\n"
      "      --threads N            phase-staggered sender workers, 1..128\n"
      "      --cpus LIST            comma-separated worker CPU list\n"
      "      --pacing-trace PATH    buffered syscall pacing CSV\n"
      "  -h, --help                 show this help\n");
}

int connected_socket(const Config &config, WorkerStats &stats,
                     const sockaddr_in &destination) {
  const int fd = ::socket(AF_INET, SOCK_DGRAM, 0);
  if (fd < 0) { stats.last_error = errno; return -1; }
  if (config.socket_buffer_bytes > 0 &&
      ::setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &config.socket_buffer_bytes,
                   sizeof(config.socket_buffer_bytes)) < 0)
    std::fprintf(stderr, "SO_SNDBUF request failed: %s\n", std::strerror(errno));
  socklen_t length = sizeof(stats.observed_socket_buffer_bytes);
  if (::getsockopt(fd, SOL_SOCKET, SO_SNDBUF, &stats.observed_socket_buffer_bytes,
                   &length) < 0) stats.observed_socket_buffer_bytes = -1;
  if (::connect(fd, reinterpret_cast<const sockaddr *>(&destination),
                sizeof(destination)) < 0) {
    stats.last_error = errno;
    ::close(fd);
    return -1;
  }
  return fd;
}

void pace_until(std::uint64_t deadline) {
  for (;;) {
    const auto now = nll::mono_ns();
    if (now >= deadline) return;
    const auto remaining = deadline - now;
    if (remaining > 300'000) nll::sleep_ns(remaining - 100'000);
    else nll::thread::cpu_relax();
  }
}

void run_worker(std::uint32_t worker_index, const Config &config,
                const sockaddr_in &destination, std::barrier<> &start_barrier,
                const std::atomic<std::uint64_t> &start_ns,
                std::atomic<std::uint64_t> &next_sequence, WorkerStats &stats) {
  const int requested_cpu = config.cpus.empty()
      ? (config.threads == 1 ? config.cpu : -1) : config.cpus[worker_index];
  stats.affinity = requested_cpu >= 0
      ? nll::thread::pin_to_core(requested_cpu)
      : nll::thread::AffinityOutcome{.requested = -1, .observed = sched_getcpu(),
          .observed_cpu_set = nll::thread::current_affinity_set(),
          .success = true, .error = ""};
  const int socket_fd = connected_socket(config, stats, destination);
  stats.batch_histogram.resize(config.send_batch_max + 1);
  std::vector<std::byte> payloads(static_cast<std::size_t>(config.send_batch_max) *
                                  config.payload_size);
  std::vector<iovec> vectors(config.send_batch_max);
  std::vector<mmsghdr> messages(config.send_batch_max);
  for (std::uint32_t index = 0; index < config.send_batch_max; ++index) {
    vectors[index] = {.iov_base = payloads.data() +
          static_cast<std::size_t>(index) * config.payload_size,
        .iov_len = config.payload_size};
    messages[index] = {};
    messages[index].msg_hdr.msg_iov = &vectors[index];
    messages[index].msg_hdr.msg_iovlen = 1;
  }
  start_barrier.arrive_and_wait();
  const auto start = start_ns.load(std::memory_order_acquire);
  const auto duration_ns = static_cast<std::uint64_t>(config.duration_seconds * 1e9);
  const auto end = start + duration_ns;
  const auto packet_limit = nll::sender::scheduled_packet_count(duration_ns,
                                                                 config.rate_pps);
  std::uint64_t packet_index = worker_index;
  while (socket_fd >= 0 && nll::mono_ns() < end &&
         (config.mode == "flood" || packet_index < packet_limit)) {
    std::uint32_t count = 0;
    std::uint64_t scheduled = nll::mono_ns();
    if (config.mode == "flood") {
      count = config.send_batch_max;
    } else if (config.mode == "burst") {
      count = static_cast<std::uint32_t>(std::min<std::uint64_t>(
          {config.send_batch_max, config.burst_size - packet_index % config.burst_size,
           packet_limit - packet_index}));
      const auto burst_start = packet_index - packet_index % config.burst_size;
      scheduled = nll::sender::deadline_ns(start, burst_start, config.rate_pps);
      pace_until(scheduled);
    } else {
      count = nll::sender::adaptive_batch_count(packet_index, packet_limit,
          config.threads, config.rate_pps, config.send_batch_max,
          config.batch_window_us * 1000ULL);
      scheduled = nll::sender::deadline_ns(start, packet_index, config.rate_pps);
      pace_until(scheduled);
    }
    if (!count) break;
    const auto first_sequence = nll::sender::allocate_sequence_range(next_sequence, count);
    for (std::uint32_t index = 0; index < count; ++index) {
      const auto sequence = first_sequence + index;
      const bool timestamped = config.timestamp_every != 0 &&
                               sequence % config.timestamp_every == 0;
      nll::message_header message{.magic = 0x6584, .version = 1, .msg_type = 0,
          .seq_idx = static_cast<std::uint32_t>(sequence),
          .send_unix_ns = timestamped ? nll::real_ns() : 0};
      message.to_network();
      std::memcpy(payloads.data() + static_cast<std::size_t>(index) * config.payload_size,
                  &message, sizeof(message));
      messages[index].msg_len = 0;
    }
    stats.attempted_sends += count;
    std::uint32_t offset = 0;
    while (offset < count) {
      const auto invocation = nll::mono_ns();
      const int result = ::sendmmsg(socket_fd, messages.data() + offset,
                                    count - offset, 0);
      const int saved_errno = errno;
      ++stats.syscall_count;
      const auto outcome = nll::sender::classify_sendmmsg_result(
          result, saved_errno, count - offset);
      if (outcome.retry) {
        ++stats.error_returns;
        stats.last_error = saved_errno;
        continue;
      }
      if (outcome.failed) {
        ++stats.error_returns;
        stats.last_error = outcome.error;
        stats.failed_sends += outcome.failed;
        break;
      }
      if (outcome.partial) ++stats.partial_returns;
      const auto completion = nll::mono_ns();
      const auto successful = outcome.successful;
      stats.successful_sends += successful;
      stats.successful_bytes += static_cast<std::uint64_t>(successful) *
                                config.payload_size;
      ++stats.batch_histogram[successful];
      const auto offset_index = packet_index +
          static_cast<std::uint64_t>(offset) * config.threads;
      const auto offset_deadline = config.mode == "flood" ? invocation
          : nll::sender::deadline_ns(start, offset_index, config.rate_pps);
      const auto lateness = invocation > offset_deadline ? invocation - offset_deadline : 0;
      ++stats.lateness_samples;
      stats.lateness_sum_ns += lateness;
      stats.lateness_max_ns = std::max(stats.lateness_max_ns, lateness);
      stats.lateness_ns.push_back(lateness);
      if (!config.pacing_trace_path.empty())
        stats.trace.push_back({completion, offset_deadline,
            first_sequence + offset, successful, worker_index});
      offset += successful;
    }
    packet_index += static_cast<std::uint64_t>(count) * config.threads;
  }
  if (socket_fd < 0) {
    stats.attempted_sends = 1;
    stats.failed_sends = 1;
    ++stats.error_returns;
  } else {
    ::close(socket_fd);
  }
}
} // namespace

int main(int argc, char **argv) {
  Config config;
  enum { send_batch_option = 1000, batch_window_option, threads_option,
         cpus_option, pacing_trace_option };
  const option options[] = {
    {"ip", required_argument, nullptr, 'i'}, {"port", required_argument, nullptr, 'p'},
    {"rate", required_argument, nullptr, 'r'}, {"duration", required_argument, nullptr, 'd'},
    {"mode", required_argument, nullptr, 'm'}, {"burst", required_argument, nullptr, 'b'},
    {"payload-size", required_argument, nullptr, 'l'}, {"cpu", required_argument, nullptr, 'c'},
    {"stats", required_argument, nullptr, 's'}, {"socket-buffer", required_argument, nullptr, 'B'},
    {"timestamp-every", required_argument, nullptr, 'T'},
    {"send-batch-max", required_argument, nullptr, send_batch_option},
    {"batch-window-us", required_argument, nullptr, batch_window_option},
    {"threads", required_argument, nullptr, threads_option},
    {"cpus", required_argument, nullptr, cpus_option},
    {"pacing-trace", required_argument, nullptr, pacing_trace_option},
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
    case 'c': if (!parse_cpu(optarg, config.cpu)) { std::fprintf(stderr, "Invalid CPU: %s\n", optarg); return 2; } break;
    case 's': config.stats_path = optarg; break;
    case 'B': { int parsed = 0; const std::string_view text(optarg); const auto result = std::from_chars(text.data(), text.data()+text.size(), parsed); if (result.ec != std::errc{} || result.ptr != text.data()+text.size() || parsed < 0) { std::fprintf(stderr, "Invalid socket buffer: %s\n", optarg); return 2; } config.socket_buffer_bytes = parsed; break; }
    case 'T': if (!parse_unsigned<std::uint64_t>(optarg, 0, UINT64_MAX, config.timestamp_every, "timestamp every")) return 2; break;
    case send_batch_option: if (!parse_unsigned<std::uint32_t>(optarg, 1, 1024, config.send_batch_max, "send batch max")) return 2; break;
    case batch_window_option: if (!parse_unsigned<std::uint64_t>(optarg, 1, 1'000'000, config.batch_window_us, "batch window")) return 2; break;
    case threads_option: if (!parse_unsigned<std::uint32_t>(optarg, 1, 128, config.threads, "threads")) return 2; break;
    case cpus_option: if (!parse_cpu_list(optarg, config.cpus)) { std::fprintf(stderr, "Invalid CPU list: %s\n", optarg); return 2; } break;
    case pacing_trace_option: config.pacing_trace_path = optarg; break;
    case 'h': usage(stdout); return 0;
    default: usage(stderr); return 2;
    }
  }
  if (optind != argc) { usage(stderr); return 2; }
  if (config.mode != "steady" && config.mode != "burst" && config.mode != "flood") {
    std::fprintf(stderr, "Invalid mode: %s\n", config.mode.c_str()); return 2;
  }
  if (config.mode == "steady" && config.burst_size != 1) {
    std::fprintf(stderr, "Steady mode requires --burst 1\n"); return 2;
  }
  if (config.mode == "burst" && config.threads != 1) {
    std::fprintf(stderr, "Burst mode supports one thread\n"); return 2;
  }
  if (!config.cpus.empty() && config.cpus.size() != config.threads) {
    std::fprintf(stderr, "--cpus must contain exactly --threads entries\n"); return 2;
  }
  if (!config.cpus.empty() && config.cpu >= 0) {
    std::fprintf(stderr, "--cpu and --cpus are mutually exclusive\n"); return 2;
  }
  if (config.threads > 1 && config.cpus.empty()) {
    std::fprintf(stderr, "Multiple threads require --cpus\n"); return 2;
  }
  const auto duration_ns = static_cast<std::uint64_t>(config.duration_seconds * 1e9);
  if (duration_ns == 0) {
    std::fprintf(stderr, "Duration must be at least one nanosecond\n"); return 2;
  }
  sockaddr_in destination{};
  destination.sin_family = AF_INET;
  destination.sin_port = htons(config.port);
  if (::inet_pton(AF_INET, config.destination.c_str(), &destination.sin_addr) != 1) {
    std::fprintf(stderr, "Invalid IPv4 address: %s\n", config.destination.c_str()); return 2;
  }
  std::vector<WorkerStats> workers(config.threads);
  std::atomic<std::uint64_t> next_sequence{0};
  std::atomic<std::uint64_t> start_ns{0};
  start_ns.store(nll::mono_ns() + 100'000'000ULL, std::memory_order_release);
  std::barrier start_barrier(static_cast<std::ptrdiff_t>(config.threads + 1));
  std::vector<std::thread> threads;
  threads.reserve(config.threads);
  for (std::uint32_t index = 0; index < config.threads; ++index)
    threads.emplace_back(run_worker, index, std::cref(config), std::cref(destination),
                         std::ref(start_barrier), std::cref(start_ns),
                         std::ref(next_sequence), std::ref(workers[index]));
  start_barrier.arrive_and_wait();
  for (auto &thread : threads) thread.join();
  const auto completion_ns = nll::mono_ns();

  Stats stats;
  stats.requested_socket_buffer_bytes = config.socket_buffer_bytes;
  stats.batch_histogram.resize(config.send_batch_max + 1);
  stats.observed_socket_buffer_bytes = workers.front().observed_socket_buffer_bytes;
  for (const auto &worker : workers) {
    stats.attempted_sends += worker.attempted_sends;
    stats.successful_sends += worker.successful_sends;
    stats.failed_sends += worker.failed_sends;
    stats.successful_bytes += worker.successful_bytes;
    stats.syscall_count += worker.syscall_count;
    stats.partial_returns += worker.partial_returns;
    stats.error_returns += worker.error_returns;
    stats.lateness_samples += worker.lateness_samples;
    stats.lateness_sum_ns += worker.lateness_sum_ns;
    stats.lateness_max_ns = std::max(stats.lateness_max_ns, worker.lateness_max_ns);
    if (worker.last_error) stats.last_error = worker.last_error;
    for (std::size_t size = 1; size < worker.batch_histogram.size(); ++size)
      stats.batch_histogram[size] += worker.batch_histogram[size];
    stats.lateness_ns.insert(stats.lateness_ns.end(), worker.lateness_ns.begin(),
                             worker.lateness_ns.end());
    stats.trace.insert(stats.trace.end(), worker.trace.begin(), worker.trace.end());
  }
  const auto start = start_ns.load(std::memory_order_acquire);
  stats.elapsed_ns = completion_ns > start ? completion_ns - start : 0;
  const bool trace_ok = write_trace(config, workers);
  const bool stats_ok = write_stats(config, std::move(stats), workers, start,
                                    start + duration_ns);
  return trace_ok && stats_ok ? 0 : 1;
}
