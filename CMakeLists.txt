cmake_minimum_required(VERSION 3.20)
project(net_latency_lab CXX)

option(PERF_BUILD "Strip debug logs, optimize for measuremets" OFF)

add_library(nll_common INTERFACE)
target_include_directories(nll_common INTERFACE ${CMAKE_SOURCE_DIR}/src)
target_compile_features(nll_common INTERFACE cxx_std_20)

if (PERF_BUILD)
    target_compile_definitions(nll_common INTERFACE NLL_LOG_LEVEL=1) #0=ERROR,1=WARN, 2=INFO, 3=DEBUG
    set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
else()
    target_compile_definitions(nll_common INTERFACE NLL_LOG_LEVEL=3)
    set(CMAKE_BUILD_TYPE RelWithDebInfo CACHE STRING "" FORCE)
endif()

add_executable(clock_sanity src/apps/clock_sanity.cpp)
target_link_libraries(clock_sanity PRIVATE nll_common)
