#pragma once

#include "log.hpp"

#include <array>
#include <algorithm>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <memory>
#include <span>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>

namespace nll {

inline constexpr std::array<std::byte, 8> BINARY_LOG_MAGIC{
    std::byte{'N'}, std::byte{'L'}, std::byte{'L'}, std::byte{'O'},
    std::byte{'G'}, std::byte{0}, std::byte{'\r'}, std::byte{'\n'}};
inline constexpr std::uint16_t BINARY_LOG_VERSION = 1;
inline constexpr std::uint16_t BINARY_LOG_HEADER_SIZE = 16;
inline constexpr std::uint32_t BINARY_LOG_ENTRY_SIZE = 36;

// On-disk v1 layout (all integers little-endian):
// header: magic[8], u16 version, u16 header_size, u32 entry_size
// record: u32 sequence, then u64 transmit, receive, processing-start, and
// processing-finish CLOCK_REALTIME timestamps.  Encoding is explicit so the
// file format is independent of host ABI, padding, alignment, and endianness.
struct LogEntry {
  std::uint32_t seq_idx{};
  std::uint64_t tx_ts{};
  std::uint64_t rx_ts{};
  std::uint64_t processing_start_ts{};
  std::uint64_t processing_finish_ts{};

  friend bool operator==(const LogEntry &, const LogEntry &) = default;
};

template <typename UInt>
constexpr void encode_le(UInt value, std::span<std::byte, sizeof(UInt)> output) {
  static_assert(std::is_unsigned_v<UInt>);
  for (std::size_t i = 0; i < sizeof(UInt); ++i)
    output[i] = static_cast<std::byte>((value >> (i * 8)) & 0xffU);
}

template <typename UInt>
constexpr UInt decode_le(std::span<const std::byte, sizeof(UInt)> input) {
  static_assert(std::is_unsigned_v<UInt>);
  UInt value = 0;
  for (std::size_t i = 0; i < sizeof(UInt); ++i)
    value |= static_cast<UInt>(std::to_integer<unsigned int>(input[i])) << (i * 8);
  return value;
}

inline constexpr std::array<std::byte, BINARY_LOG_HEADER_SIZE> encode_log_header() {
  std::array<std::byte, BINARY_LOG_HEADER_SIZE> bytes{};
  std::copy(BINARY_LOG_MAGIC.begin(), BINARY_LOG_MAGIC.end(), bytes.begin());
  encode_le<std::uint16_t>(BINARY_LOG_VERSION,
      std::span<std::byte, 2>(bytes.data() + 8, 2));
  encode_le<std::uint16_t>(BINARY_LOG_HEADER_SIZE,
      std::span<std::byte, 2>(bytes.data() + 10, 2));
  encode_le<std::uint32_t>(BINARY_LOG_ENTRY_SIZE,
      std::span<std::byte, 4>(bytes.data() + 12, 4));
  return bytes;
}

inline constexpr std::array<std::byte, BINARY_LOG_ENTRY_SIZE>
encode_log_entry(const LogEntry &entry) {
  std::array<std::byte, BINARY_LOG_ENTRY_SIZE> bytes{};
  encode_le<std::uint32_t>(entry.seq_idx,
      std::span<std::byte, 4>(bytes.data(), 4));
  encode_le<std::uint64_t>(entry.tx_ts,
      std::span<std::byte, 8>(bytes.data() + 4, 8));
  encode_le<std::uint64_t>(entry.rx_ts,
      std::span<std::byte, 8>(bytes.data() + 12, 8));
  encode_le<std::uint64_t>(entry.processing_start_ts,
      std::span<std::byte, 8>(bytes.data() + 20, 8));
  encode_le<std::uint64_t>(entry.processing_finish_ts,
      std::span<std::byte, 8>(bytes.data() + 28, 8));
  return bytes;
}

inline LogEntry decode_log_entry(std::span<const std::byte> bytes) {
  if (bytes.size() != BINARY_LOG_ENTRY_SIZE)
    throw std::invalid_argument("truncated log entry");
  return {
      .seq_idx = decode_le<std::uint32_t>(std::span<const std::byte, 4>(bytes.data(), 4)),
      .tx_ts = decode_le<std::uint64_t>(std::span<const std::byte, 8>(bytes.data() + 4, 8)),
      .rx_ts = decode_le<std::uint64_t>(std::span<const std::byte, 8>(bytes.data() + 12, 8)),
      .processing_start_ts = decode_le<std::uint64_t>(std::span<const std::byte, 8>(bytes.data() + 20, 8)),
      .processing_finish_ts = decode_le<std::uint64_t>(std::span<const std::byte, 8>(bytes.data() + 28, 8)),
  };
}

inline void validate_log_header(std::span<const std::byte> bytes) {
  if (bytes.size() < BINARY_LOG_HEADER_SIZE)
    throw std::invalid_argument("truncated log header");
  if (!std::equal(BINARY_LOG_MAGIC.begin(), BINARY_LOG_MAGIC.end(), bytes.begin()))
    throw std::invalid_argument("invalid log magic");
  const auto version = decode_le<std::uint16_t>(std::span<const std::byte, 2>(bytes.data() + 8, 2));
  const auto header_size = decode_le<std::uint16_t>(std::span<const std::byte, 2>(bytes.data() + 10, 2));
  const auto entry_size = decode_le<std::uint32_t>(std::span<const std::byte, 4>(bytes.data() + 12, 4));
  if (version != BINARY_LOG_VERSION)
    throw std::invalid_argument("unsupported log version");
  if (header_size != BINARY_LOG_HEADER_SIZE || entry_size != BINARY_LOG_ENTRY_SIZE)
    throw std::invalid_argument("unsupported log layout");
}

struct FileDeleter {
  void operator()(std::FILE *file) const { if (file) std::fclose(file); }
};

class BinaryLogger {
public:
  static constexpr std::size_t BUFFER_CAPACITY = 64 * 1024;

  explicit BinaryLogger(const std::filesystem::path &filename)
      : file_(std::fopen(filename.c_str(), "wb")) {
    if (!file_) {
      NLL_ERROR("Failed to open log file %s\n", filename.c_str());
      return;
    }
    const auto header = encode_log_header();
    if (std::fwrite(header.data(), 1, header.size(), file_.get()) != header.size()) {
      NLL_ERROR("Failed to write binary log header to %s\n", filename.c_str());
      file_.reset();
    }
    buffer_.reserve(BUFFER_CAPACITY);
  }

  BinaryLogger(const BinaryLogger &) = delete;
  BinaryLogger &operator=(const BinaryLogger &) = delete;
  ~BinaryLogger() { flush(); }

  void log(const LogEntry &entry) noexcept {
    const auto bytes = encode_log_entry(entry);
    if (buffer_.size() + bytes.size() > BUFFER_CAPACITY) flush();
    buffer_.insert(buffer_.end(), bytes.begin(), bytes.end());
  }

  void flush() noexcept {
    if (!file_) return;
    if (!buffer_.empty()) {
      const auto written = std::fwrite(buffer_.data(), 1, buffer_.size(), file_.get());
      if (written != buffer_.size()) NLL_WARN("Partial write in BinaryLogger. Disk full?\n");
      buffer_.clear();
    }
    std::fflush(file_.get());
  }

  [[nodiscard]] bool is_open() const noexcept { return file_ != nullptr; }

private:
  std::unique_ptr<std::FILE, FileDeleter> file_;
  std::vector<std::byte> buffer_;
};

} // namespace nll
