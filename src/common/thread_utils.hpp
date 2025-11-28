#pragma once
#include "log.hpp"
#include <concepts>
#include <iostream>
#include <pthread.h>
#include <sched.h>
#include <thread>
#include <vector>

namespace nll::thread {

template <std::integral T> inline void pin_to_core(T core_id) {
  cpu_set_t cpuset;
  CPU_ZERO(&cpuset);         // clear mem
  CPU_SET(core_id, &cpuset); // |= core_id
  pthread_t current_thread = pthread_self();

  int result = pthread_setaffinity_np(current_thread, sizeof(cpu_set_t),
                                      &cpuset); // pin thread to core

  if (result != 0) {
    NLL_ERROR("Failed to pin thread to core %d\n", core_id);
  } else {
    NLL_DEBUG("Thread pinned to core %d\n", core_id);
  }
}

inline void set_realtime_priority() {
  sched_param param;
  param.sched_priority = 90;

  if (pthread_setschedparam(pthread_self(), SCHED_FIFO, &param) != 0) {
    NLL_WARN("Failed to set SCHED_FIFO. Run with sudo for lower jitter.\n");
  } else {
    NLL_DEBUG("SHED_FIFO enabled with priority 90\n");
  }
}

#if defined(__x86_64__) || defined(_M_X64) || defined(__i386__)
#include <immintrin.h>
#endif

// Portable CPU relax function
inline void cpu_relax() {
#if defined(__x86_64__) || defined(_M_X64) || defined(__i386__)
  _mm_pause(); // Intel/AMD pause
#elif defined(__aarch64__) || defined(__arm__)
  __asm__ __volatile__("isb"); // ARM barrier/pause equivalent
#else
  // Fallback for unknown architectures
  // asm volatile ("nop");
#endif
}

#ifdef __cpp_lib_hardware_interference_size
using std::hardware_constructive_interference_size;
using std::hardware_destructive_interference_size;
#else
// 64 bytes on x86-64 | 128 bytes on ARM64/Apple Silicon
constexpr std::size_t hardware_destructive_interference_size = 64;
constexpr std::size_t hardware_constructive_interference_size = 64;
#endif
}; // namespace nll::thread
