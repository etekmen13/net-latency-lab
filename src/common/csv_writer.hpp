#pragma once
#include "log.hpp"
#include <concepts>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <memory>
#include <type_traits>
#include <vector>

namespace nll {

struct __attribute((packed)) LogEntry {
  uint32_t seq_idx;
  uint64_t tx_ts;
  uint64_t rx_ts;
  int64_t latency_ns;
};

static_assert(sizeof(LogEntry) == 28, "LogEntry size mismatch!");
template <typename T>
concept BinaryLoggable =
    std::is_trivially_copyable_v<T> && std::is_standard_layout_v<T>;

struct FileDeleter {
  void operator()(std::FILE *fp) const {
    if (fp)
      std::fclose(fp);
  }
};
template <BinaryLoggable T> class BinaryLogger {
public:
  static constexpr size_t BUFFER_CAPACITY = 64 * 1024 / sizeof(T);

  explicit BinaryLogger(const std::filesystem::path &filename) {

    file_ = std::unique_ptr<std::FILE, FileDeleter>(
        std::fopen(filename.c_str(), "wb"));

    if (!file_) {
      NLL_ERROR("Failed to open log file %s\n", filename.c_str());
    }

    buffer_.reserve(BUFFER_CAPACITY);
  }

  BinaryLogger(const BinaryLogger &) = delete;
  BinaryLogger &operator=(const BinaryLogger &) = delete;
  BinaryLogger(BinaryLogger &&) = default;
  BinaryLogger &operator=(BinaryLogger &&) = default;

  ~BinaryLogger() { flush(); }

  void log(const T &entry) noexcept {
    if (buffer_.size() >= BUFFER_CAPACITY) {
      flush();
    }
    buffer_.push_back(entry);
  }

  void flush() noexcept {
    if (buffer_.empty() || !file_)
      return;

    std::size_t written =
        std::fwrite(buffer_.data(), sizeof(T), buffer_.size(), file_.get());

    if (written != buffer_.size()) {
      NLL_WARN("Partial write in BinaryLogger. Disk full?\n");
    }

    buffer_.clear();
  }

private:
  std::unique_ptr<std::FILE, FileDeleter> file_;

  std::vector<T> buffer_;
};
}; // namespace nll
