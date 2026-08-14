#pragma once
#include "log.hpp"
#include <concepts>
#include <cerrno>
#include <cstring>
#include <string>
#include <pthread.h>
#include <sched.h>
#include <thread>
#include <vector>

namespace nll::thread {

struct AffinityOutcome {
  int requested = -1;
  int observed = -1;
  std::string observed_cpu_set;
  bool success = false;
  std::string error;
};

inline std::string current_affinity_set() {
  cpu_set_t cpuset;
  CPU_ZERO(&cpuset);
  const int result = pthread_getaffinity_np(pthread_self(), sizeof(cpuset), &cpuset);
  if (result != 0) return "unknown";
  std::string value;
  for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu) {
    if (!CPU_ISSET(cpu, &cpuset)) continue;
    if (!value.empty()) value += ',';
    value += std::to_string(cpu);
  }
  return value;
}

template <std::integral T> inline AffinityOutcome pin_to_core(T core_id) {
  AffinityOutcome outcome{.requested = static_cast<int>(core_id),
                          .observed = -1, .observed_cpu_set = "unknown",
                          .success = false, .error = ""};
  if (core_id < 0 || core_id >= CPU_SETSIZE) {
    outcome.error = "CPU id is outside CPU_SETSIZE";
    return outcome;
  }
  cpu_set_t cpuset;
  CPU_ZERO(&cpuset);         // clear mem
  CPU_SET(core_id, &cpuset); // |= core_id
  pthread_t current_thread = pthread_self();

  int result = pthread_setaffinity_np(current_thread, sizeof(cpu_set_t),
                                      &cpuset); // pin thread to core

  if (result != 0) {
    NLL_ERROR("Failed to pin thread to core %d\n", core_id);
    outcome.error = std::strerror(result);
  } else {
    NLL_DEBUG("Thread pinned to core %d\n", core_id);
    outcome.success = true;
  }
  outcome.observed = sched_getcpu();
  outcome.observed_cpu_set = current_affinity_set();
  return outcome;
}

struct SchedulerOutcome {
  std::string requested_policy = "other";
  int requested_priority = 0;
  std::string observed_policy = "unknown";
  int observed_priority = -1;
  bool success = false;
  std::string error;
};

inline const char *policy_name(int policy) noexcept {
  switch (policy) {
  case SCHED_OTHER: return "other";
  case SCHED_FIFO: return "fifo";
  case SCHED_RR: return "rr";
  default: return "unknown";
  }
}

inline SchedulerOutcome set_scheduler(const std::string &policy_name_requested,
                                      int priority) {
  SchedulerOutcome outcome{.requested_policy = policy_name_requested,
                           .requested_priority = priority,
                           .observed_policy = "unknown",
                           .observed_priority = -1,
                           .success = false, .error = ""};
  int policy = SCHED_OTHER;
  if (policy_name_requested == "fifo") policy = SCHED_FIFO;
  else if (policy_name_requested == "rr") policy = SCHED_RR;

  sched_param param{};
  param.sched_priority = priority;

  const int result = pthread_setschedparam(pthread_self(), policy, &param);
  if (result != 0) {
    outcome.error = std::strerror(result);
    NLL_WARN("Failed to set scheduler %s/%d: %s\n",
             policy_name_requested.c_str(), priority, outcome.error.c_str());
  } else {
    outcome.success = true;
  }
  int observed_policy = 0;
  sched_param observed{};
  const int observed_result = pthread_getschedparam(
      pthread_self(), &observed_policy, &observed);
  if (observed_result == 0) {
    outcome.observed_policy = policy_name(observed_policy);
    outcome.observed_priority = observed.sched_priority;
  } else if (outcome.error.empty()) {
    outcome.error = std::strerror(observed_result);
  }
  return outcome;
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
