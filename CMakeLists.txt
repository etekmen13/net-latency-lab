cmake_minimum_required(VERSION 3.15)
project(csc477_honors CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# 1. Point the include path to 'src' so logical includes like
#    #include "common/packet.hpp" resolve to src/common/packet.hpp
include_directories(${CMAKE_SOURCE_DIR}/src)

# 2. Update source paths to match the physical directory structure
add_executable(receiver src/receiver/receiver.cpp)
target_link_libraries(receiver pthread)

add_executable(sender src/sender/sender.cpp)
target_link_libraries(sender pthread)
