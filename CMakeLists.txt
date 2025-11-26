cmake_minimum_required(VERSION 3.15)
project(csc477_honors CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
include_directories(${CMAKE_SOURCE_DIR}/src)

add_executable(receiver src/receiver/receiver.cpp)
target_link_libraries(receiver pthread)

add_executable(sender src/sender/sender.cpp)
target_link_libraries(sender pthread)

