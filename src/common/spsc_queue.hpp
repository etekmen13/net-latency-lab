#pragma once
#include <array>
#include <atomic>
#include <bit>
#include <concepts>
#include <expected>
#include <new>
#include <string_view>
#include <type_traits>
#include <utility>
namespace nll {

template <typename T>
concept Queueable = std::movable<T> && std::default_initializable<T>;

template <Queueable T, std::size_t Capacity> class SPSCQueue {
  static_assert(std::has_single_bit(Capacity),
                "Capacity must be a power of 2 for optimization");

public:
  SPSCQueue() noexcept : head_(0), tail_(0) {}

  SPSCQueue(const SPSCQueue &) = delete;
  SPSCQueue &operator=(const SPSCQueue &) = delete;

  /**
   * @brief Attempts to acquire a slot in the buffer for writing.
   * @return A raw pointer to the slot if available, or an error code.
   * Must call commit() to advance head_ pointer;
   */
  [[nodiscard]] std::expected<T *, std::string_view> try_alloc() noexcept {
    const std::size_t head = head_.load(std::memory_order_relaxed);
    const std::size_t next_head = (head + 1) & mask_;

    if (next_head == tail_.load(std::memory_order_acquire)) {
      return std::unexpected("Queue Full");
    }

    return &buffer_[head];
  }

  /**
   * @brief Publishes the previously allocated slot.
   * Must be called after writing to the pointer returned by try_alloc().
   */
  void commit() noexcept {
    const std::size_t head = head_.load(std::memory_order_relaxed);
    const std::size_t next_head = (head + 1) & mask_;

    head_.store(next_head, std::memory_order_release);
  }

  [[nodiscard]] bool push(T &&value) noexcept {
    auto slot = try_alloc();
    if (!slot)
      return false;
    **slot = std::move(value);
    commit();
    return true;
  }

  [[nodiscard]] std::expected<const T *, std::string_view>
  front() const noexcept {
    const std::size_t tail = tail_.load(std::memory_order_relaxed);
    if (head_.load(std::memory_order_acquire) == tail) {
      return std::unexpected("Queue Empty");
    }
    return &buffer_[tail];
  }

  void pop() noexcept {
    const std::size_t tail = tail_.load(std::memory_order_relaxed);

    if constexpr (!std::is_trivially_destructible_v<T>) {
      buffer_[tail] = T();
    }
    const std::size_t next_tail = (tail + 1) & mask_;

    tail_.store(next_tail, std::memory_order_release);
  }

private:
  static constexpr std::size_t mask_ = Capacity - 1;

  alignas(std::hardware_destructive_interference_size)
      std::atomic<std::size_t> head_;
  alignas(std::hardware_destructive_interference_size)
      std::atomic<std::size_t> tail_;

  std::array<T, Capacity> buffer_;
};
}; // namespace nll
