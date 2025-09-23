cmake_minimum_required(VERSION 3.20)
project(net_latency_lab LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  add_compile_options(-Wall -Wextra -Wpedantic -Wconversion -Wshadow -Wdouble-promotion)
  add_compile_options(-O3 -march=armv8-a)
endif()


add_library(common_headers INTERFACE)
target_include_directories(common_headers INTERFACE
  "${CMAKE_SOURCE_DIR}/src"
)

add_subdirectory(src/sender)
add_subdirectory(src/receiver)
