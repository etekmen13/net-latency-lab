cmake_minimum_required(VERSION 3.15)
project(csc477_honors CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
include_directories(${CMAKE_SOURCE_DIR}/src)

# Optimization flags (Critical for your latency benchmarks)
add_compile_options(-O3 -march=native -Wall -Wextra)

# --- Receiver Variants ---

# 1. Baseline (Single-threaded / Blocking)
add_executable(receiver_baseline src/receiver/receiver_baseline.cpp)
target_link_libraries(receiver_baseline pthread)

# 2. Threaded (Optimized / Non-blocking)
add_executable(receiver_threaded src/receiver/receiver_threaded.cpp)
target_link_libraries(receiver_threaded pthread)

# --- Sender ---
add_executable(sender src/sender/sender.cpp)
target_link_libraries(sender pthread)
