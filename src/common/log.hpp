#pragma once

/*

I want speed, so no iostream. fprintf and line buffering
I want 4 log levels
0=ERROR, 1=WARN, 2=INFO, 3=DEBUG

I'll also add a threadsafe flag so the logs aren't garbled from multiple
threads.
*/

#include <cstdarg>
#include <cstdint>
#include <cstdio>

#include "common/time.hpp"

#ifndef NLL_LOG_LEVEL
#define NLL_LOG_LEVEL 3
#endif

#ifndef NLL_LOG_THREADSAFE
#define NLL_LOG_THREADSAFE 0
#endif

#if NLL_LOG_THREADSAFE
#include <mutex>
#endif

namespace nll::log {
namespace detail {

// prints log level
inline const char *lvl_name(int lvl) noexcept {
  switch (lvl) {
  case 0:
    return "ERR";
  case 1:
    return "WRN";
  case 2:
    return "INF";
  default:
    return "DBG";
  }
}

// colors (i think pandas can handle ANSI)
inline const char *lvl_color(int lvl) noexcept {
  switch (lvl) {
  case 0:
    return "\x1b[31m"; // red
  case 1:
    return "\x1b[33m"; // yellow
  case 2:
    return "\x1b[36m"; // cyan
  default:
    return "\x1b[90m"; // gray
  }
}
} // namespace detail

// lazy global trick from Accelerated C++
#if NLL_LOG_THREADSAFE
inline std::mutex &log_mutex() {
  static std::mutex m;
  return m;
}
#endif

inline void init_stderr_line_buffering() {
  static char buf[1 << 15];                  // 32KB
  setvbuf(stderr, buf, _IOLBF, sizeof(buf)); // _IOLBF = IO Line Buffer
}

// logger: [LVL s.ns] message
inline void vlogf(int lvl, const char *fmt, va_list ap) {
#if NLL_LOG_THREADSAFE
  const std::lock_guard<std::mutex> g(log_mutex());
#endif
  const std::uint64_t t = nll::mono_ns();
  const char *color = detail::lvl_color(lvl);
  const char *name = detail::lvl_name(lvl);

  std::fputs(color, stderr);
  std::fprintf(stderr, "[%s %12llu.%09llu] ", name,
               static_cast<std::uint64_t>(t / a_billi), // s
               static_cast<std::uint64_t>(t % a_billi)  // ns
  );
  std::vfprintf(stderr, fmt, ap);
  std::fputs("\x1b[0m", stderr); // reset color and flush
}
// wrapper with variadic args
inline void logf(int lvl, const char *fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  vlogf(lvl, fmt, ap);
  va_end(ap);
}
} // namespace nll::log

#if NLL_LOG_LEVEL >= 0
#define NLL_ERROR(...) ::nll::log::logf(0, __VA_ARGS__)
#else
#define NLL_ERROR(...) ((void)0)
#endif
#if NLL_LOG_LEVEL >= 1
#define NLL_WARN(...) ::nll::log::logf(1, __VA_ARGS__)
#else
#define NLL_WARN(...) ((void)0)
#endif
#if NLL_LOG_LEVEL >= 2
#define NLL_INFO(...) ::nll::log::logf(2, __VA_ARGS__)
#else
#define NLL_INFO(...) ((void)0)
#endif
#if NLL_LOG_LEVEL >= 3
#define NLL_DEBUG(...) ::nll::log::logf(3, __VA_ARGS__)
#else
#define NLL_DEBUG(...) ((void)0)
#endif
