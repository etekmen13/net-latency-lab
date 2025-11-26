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
    NLL_ERROR("Failed to pin thread to core %d", core_id);
  } else {
    NLL_DEBUG("Thread pinned to core %d", core_id);
  }
}

inline void set_realtime_priority() {
  sched_param param;
  param.sched_priority = 90;

  if (pthread_setschedparam(pthread_self(), SCHED_FIFO, &param) != 0) {
    NLL_WARN("Failed to set SCHED_FIFO. Run with sudo for lower jitter.");
  } else {
    NLL_DEBUG("SHED_FIFO enabled with priority 90");
  }
}

}; // namespace nll::thread
