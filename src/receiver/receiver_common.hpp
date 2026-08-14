#pragma once

#include "common/csv_writer.hpp"
#include "common/log.hpp"
#include "common/packet.hpp"
#include "common/sequence_tracker.hpp"
#include "common/thread_utils.hpp"
#include "common/time.hpp"

#include <cerrno>
#include <charconv>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <limits>
#include <netinet/in.h>
#include <string>
#include <string_view>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <unistd.h>

namespace nll::receiver {

struct Config {
  std::string variant;
  std::uint16_t port = 49200;
  std::filesystem::path output_path = "latency.bin";
  std::filesystem::path stats_path = "receiver_stats.json";
  std::uint64_t max_packets = 0;
  int cpu = -1;
  int worker_cpu = -1;
  std::uint32_t batch_size = 1;
  std::uint64_t work_ns = 0;
  std::uint64_t sample_every = 1;
  int socket_buffer_bytes = 0;
  std::string scheduler = "other";
  int priority = 0;
};

struct ProcessingStats {
  std::uint64_t processed_packets = 0;
  std::uint64_t first_processing_mono_ns = 0;
  std::uint64_t last_processing_mono_ns = 0;
  nll::SequenceTracker sequences;
};

struct Stats {
  std::uint64_t datagrams_received = 0;
  std::uint64_t valid_packets = 0;
  std::uint64_t unique_valid_packets = 0;
  std::uint64_t receive_sequence_gaps = 0;
  std::uint64_t receive_duplicates = 0;
  std::uint64_t receive_reordered = 0;
  std::uint64_t processed_packets = 0;
  std::uint64_t unique_processed_packets = 0;
  std::uint64_t processed_sequence_gaps = 0;
  std::uint64_t processed_duplicates = 0;
  std::uint64_t processed_reordered = 0;
  std::uint64_t short_packets = 0;
  std::uint64_t invalid_magic = 0;
  std::uint64_t unsupported_version = 0;
  std::uint64_t spsc_overflow = 0;
  std::uint64_t socket_errors = 0;
  std::uint64_t receive_syscalls = 0;
  std::uint64_t sampled_packets = 0;
  std::uint64_t first_receive_mono_ns = 0;
  std::uint64_t last_receive_mono_ns = 0;
  std::uint64_t first_processing_mono_ns = 0;
  std::uint64_t last_processing_mono_ns = 0;
  std::uint64_t drain_duration_ns = 0;
  std::uint64_t queue_depth_at_shutdown = 0;
  std::uint64_t socket_pending_bytes_at_shutdown = 0;
  int requested_socket_buffer_bytes = 0;
  int observed_socket_buffer_bytes = 0;
  bool interrupted = false;
};

struct ReceivedPacket {
  nll::message_header message{};
  std::uint64_t receive_real_ns = 0;
  std::uint64_t receive_mono_ns = 0;
  bool sampled = false;
};

inline bool parse_u64(std::string_view text, std::uint64_t min,
                      std::uint64_t max, std::uint64_t &value,
                      const char *name) {
  std::uint64_t parsed = 0;
  const auto result = std::from_chars(text.data(), text.data() + text.size(), parsed);
  if (text.empty() || result.ec != std::errc{} ||
      result.ptr != text.data() + text.size() || parsed < min || parsed > max) {
    std::fprintf(stderr, "Invalid %s: %.*s (expected %llu..%llu)\n", name,
                 static_cast<int>(text.size()), text.data(),
                 static_cast<unsigned long long>(min),
                 static_cast<unsigned long long>(max));
    return false;
  }
  value = parsed;
  return true;
}

inline bool parse_int(std::string_view text, int min, int max, int &value,
                      const char *name) {
  int parsed = 0;
  const auto result = std::from_chars(text.data(), text.data() + text.size(), parsed);
  if (text.empty() || result.ec != std::errc{} ||
      result.ptr != text.data() + text.size() || parsed < min || parsed > max) {
    std::fprintf(stderr, "Invalid %s: %.*s (expected %d..%d)\n", name,
                 static_cast<int>(text.size()), text.data(), min, max);
    return false;
  }
  value = parsed;
  return true;
}

inline bool validate_scheduler(const Config &config) {
  if (config.scheduler != "other" && config.scheduler != "fifo" && config.scheduler != "rr") {
    std::fprintf(stderr, "Invalid scheduler: %s (expected other, fifo, or rr)\n", config.scheduler.c_str());
    return false;
  }
  if (config.scheduler == "other" && config.priority != 0) {
    std::fprintf(stderr, "Scheduler 'other' requires priority 0\n");
    return false;
  }
  if (config.scheduler != "other" && (config.priority < 1 || config.priority > 99)) {
    std::fprintf(stderr, "Realtime schedulers require priority 1..99\n");
    return false;
  }
  return true;
}

inline std::string json_escape(std::string_view value) {
  std::string escaped;
  for (const char c : value) {
    switch (c) {
    case '\\': escaped += "\\\\"; break;
    case '"': escaped += "\\\""; break;
    case '\n': escaped += "\\n"; break;
    case '\r': escaped += "\\r"; break;
    case '\t': escaped += "\\t"; break;
    default: escaped += c; break;
    }
  }
  return escaped;
}

inline void write_outcome(std::FILE *file, const char *name,
                          const nll::thread::AffinityOutcome &outcome,
                          bool comma = true) {
  std::fprintf(file,
      "  \"%s\": {\"requested_cpu\": %d, \"observed_cpu\": %d, "
      "\"observed_cpu_set\": \"%s\", \"success\": %s, \"error\": \"%s\"}%s\n",
      name, outcome.requested, outcome.observed,
      json_escape(outcome.observed_cpu_set).c_str(), outcome.success ? "true" : "false",
      json_escape(outcome.error).c_str(), comma ? "," : "");
}

inline void write_outcome(std::FILE *file, const char *name,
                          const nll::thread::SchedulerOutcome &outcome,
                          bool comma = true) {
  std::fprintf(file,
      "  \"%s\": {\"requested_policy\": \"%s\", \"requested_priority\": %d, "
      "\"observed_policy\": \"%s\", \"observed_priority\": %d, "
      "\"success\": %s, \"error\": \"%s\"}%s\n",
      name, json_escape(outcome.requested_policy).c_str(), outcome.requested_priority,
      json_escape(outcome.observed_policy).c_str(), outcome.observed_priority,
      outcome.success ? "true" : "false", json_escape(outcome.error).c_str(),
      comma ? "," : "");
}

inline void merge_processing(Stats &stats, const ProcessingStats &processing) {
  stats.processed_packets = processing.processed_packets;
  stats.unique_processed_packets = processing.sequences.unique();
  stats.processed_sequence_gaps = processing.sequences.gaps();
  stats.processed_duplicates = processing.sequences.duplicates();
  stats.processed_reordered = processing.sequences.reordered();
  stats.first_processing_mono_ns = processing.first_processing_mono_ns;
  stats.last_processing_mono_ns = processing.last_processing_mono_ns;
}

inline bool write_stats(const Config &config, const Stats &stats,
                        const nll::thread::AffinityOutcome &rx_affinity,
                        const nll::thread::SchedulerOutcome &rx_scheduler,
                        const nll::thread::AffinityOutcome *worker_affinity = nullptr,
                        const nll::thread::SchedulerOutcome *worker_scheduler = nullptr) {
  if (config.stats_path.has_parent_path()) {
    std::error_code ec;
    std::filesystem::create_directories(config.stats_path.parent_path(), ec);
    if (ec) { NLL_ERROR("Cannot create stats directory: %s\n", ec.message().c_str()); return false; }
  }
  std::FILE *file = std::fopen(config.stats_path.c_str(), "w");
  if (!file) { NLL_ERROR("Cannot open stats file %s: %s\n", config.stats_path.c_str(), std::strerror(errno)); return false; }
#define NLL_U64(name) std::fprintf(file, "  \"" #name "\": %llu,\n", static_cast<unsigned long long>(stats.name))
  std::fprintf(file, "{\n  \"schema_version\": 2,\n  \"variant\": \"%s\",\n", config.variant.c_str());
  std::fprintf(file, "  \"port\": %u,\n  \"batch_size\": %u,\n", config.port, config.batch_size);
  std::fprintf(file, "  \"work_ns\": %llu,\n  \"sample_every\": %llu,\n",
      static_cast<unsigned long long>(config.work_ns), static_cast<unsigned long long>(config.sample_every));
  NLL_U64(datagrams_received); NLL_U64(valid_packets); NLL_U64(unique_valid_packets);
  NLL_U64(receive_sequence_gaps); NLL_U64(receive_duplicates); NLL_U64(receive_reordered);
  NLL_U64(processed_packets); NLL_U64(unique_processed_packets);
  NLL_U64(processed_sequence_gaps); NLL_U64(processed_duplicates); NLL_U64(processed_reordered);
  NLL_U64(short_packets); NLL_U64(invalid_magic); NLL_U64(unsupported_version); NLL_U64(spsc_overflow);
  NLL_U64(socket_errors); NLL_U64(receive_syscalls); NLL_U64(sampled_packets);
  NLL_U64(first_receive_mono_ns); NLL_U64(last_receive_mono_ns);
  NLL_U64(first_processing_mono_ns); NLL_U64(last_processing_mono_ns);
  NLL_U64(drain_duration_ns); NLL_U64(queue_depth_at_shutdown); NLL_U64(socket_pending_bytes_at_shutdown);
#undef NLL_U64
  std::fprintf(file, "  \"requested_socket_buffer_bytes\": %d,\n", stats.requested_socket_buffer_bytes);
  std::fprintf(file, "  \"observed_socket_buffer_bytes\": %d,\n", stats.observed_socket_buffer_bytes);
  std::fprintf(file, "  \"interrupted\": %s,\n", stats.interrupted ? "true" : "false");
  write_outcome(file, "receiver_affinity", rx_affinity);
  if (worker_affinity) write_outcome(file, "worker_affinity", *worker_affinity);
  write_outcome(file, "receiver_scheduler", rx_scheduler, worker_scheduler != nullptr);
  if (worker_scheduler) write_outcome(file, "worker_scheduler", *worker_scheduler, false);
  std::fprintf(file, "}\n");
  return std::fclose(file) == 0;
}

class ScopedSocket {
public:
  explicit ScopedSocket(int requested_buffer_bytes = 0)
      : fd_(::socket(AF_INET, SOCK_DGRAM, 0)), requested_buffer_bytes_(requested_buffer_bytes) {
    if (fd_ < 0) return;
    const timeval timeout{.tv_sec = 0, .tv_usec = 100'000};
    const int one = 1;
    if (::setsockopt(fd_, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) < 0 ||
        ::setsockopt(fd_, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one)) < 0)
      NLL_WARN("A receiver socket option failed: %s\n", std::strerror(errno));
    if (requested_buffer_bytes_ > 0 &&
        ::setsockopt(fd_, SOL_SOCKET, SO_RCVBUF, &requested_buffer_bytes_, sizeof(requested_buffer_bytes_)) < 0)
      NLL_WARN("SO_RCVBUF request failed: %s\n", std::strerror(errno));
    socklen_t length = sizeof(observed_buffer_bytes_);
    if (::getsockopt(fd_, SOL_SOCKET, SO_RCVBUF, &observed_buffer_bytes_, &length) < 0)
      observed_buffer_bytes_ = -1;
  }
  ~ScopedSocket() { if (fd_ >= 0) ::close(fd_); }
  ScopedSocket(const ScopedSocket &) = delete;
  ScopedSocket &operator=(const ScopedSocket &) = delete;
  [[nodiscard]] int get() const noexcept { return fd_; }
  [[nodiscard]] bool valid() const noexcept { return fd_ >= 0; }
  [[nodiscard]] int requested_buffer_bytes() const noexcept { return requested_buffer_bytes_; }
  [[nodiscard]] int observed_buffer_bytes() const noexcept { return observed_buffer_bytes_; }
private:
  int fd_ = -1;
  int requested_buffer_bytes_ = 0;
  int observed_buffer_bytes_ = -1;
};

inline bool bind_socket(int fd, std::uint16_t port) {
  const sockaddr_in address{.sin_family = AF_INET, .sin_port = htons(port),
                            .sin_addr = {.s_addr = INADDR_ANY}, .sin_zero = {0}};
  if (::bind(fd, reinterpret_cast<const sockaddr *>(&address), sizeof(address)) < 0) {
    NLL_ERROR("Bind failed on port %u: %s\n", port, std::strerror(errno)); return false;
  }
  return true;
}

inline std::uint64_t pending_socket_bytes(int fd) noexcept {
  int pending = 0;
  return ::ioctl(fd, FIONREAD, &pending) == 0 && pending > 0
      ? static_cast<std::uint64_t>(pending) : 0;
}

inline void synthetic_work(std::uint64_t duration_ns) noexcept {
  if (duration_ns == 0) return;
  const auto start = nll::mono_ns();
  while (nll::mono_ns() - start < duration_ns) nll::thread::cpu_relax();
}

inline void process_packet(nll::BinaryLogger &logger, ProcessingStats &stats,
                           const ReceivedPacket &packet, std::uint64_t work_ns) {
  const auto processing_mono_start = nll::mono_ns();
  const auto processing_real_start = packet.sampled ? nll::real_ns() : 0;
  synthetic_work(work_ns);
  const auto processing_real_finish = packet.sampled ? nll::real_ns() : 0;
  const auto processing_mono_finish = work_ns != 0 ? nll::mono_ns() : processing_mono_start;
  if (stats.processed_packets == 0) stats.first_processing_mono_ns = processing_mono_start;
  stats.last_processing_mono_ns = processing_mono_finish;
  ++stats.processed_packets;
  stats.sequences.observe(packet.message.seq_idx);
  if (packet.sampled) {
    logger.log({.seq_idx = packet.message.seq_idx, .tx_ts = packet.message.send_unix_ns,
                .rx_ts = packet.receive_real_ns,
                .processing_start_ts = processing_real_start,
                .processing_finish_ts = processing_real_finish});
  }
}

inline ReceivedPacket account_receive(Stats &stats, nll::SequenceTracker &sequences,
                                      const nll::message_header &message,
                                      std::uint64_t receive_real_ns,
                                      std::uint64_t receive_mono_ns,
                                      std::uint64_t sample_every) {
  ++stats.valid_packets;
  if (stats.first_receive_mono_ns == 0) stats.first_receive_mono_ns = receive_mono_ns;
  stats.last_receive_mono_ns = receive_mono_ns;
  sequences.observe(message.seq_idx);
  const bool sampled = sample_every != 0 && message.seq_idx % sample_every == 0;
  if (sampled) ++stats.sampled_packets;
  return {.message = message, .receive_real_ns = receive_real_ns,
          .receive_mono_ns = receive_mono_ns, .sampled = sampled};
}

inline void finalize_receive_sequences(Stats &stats, const nll::SequenceTracker &sequences) {
  stats.unique_valid_packets = sequences.unique();
  stats.receive_sequence_gaps = sequences.gaps();
  stats.receive_duplicates = sequences.duplicates();
  stats.receive_reordered = sequences.reordered();
}

inline nll::thread::AffinityOutcome apply_affinity(int cpu) {
  if (cpu >= 0) return nll::thread::pin_to_core(cpu);
  return {.requested = -1, .observed = sched_getcpu(),
          .observed_cpu_set = nll::thread::current_affinity_set(),
          .success = true, .error = ""};
}

} // namespace nll::receiver
