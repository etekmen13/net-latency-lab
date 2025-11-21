cmake_minimum_required(VERSION 3.15)
project(csc477_honors CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Common include directory
include_directories(${CMAKE_SOURCE_DIR})

# Receiver
add_executable(receiver receiver/receiver.cpp)
target_link_libraries(receiver pthread)

# Sender
add_executable(sender sender/sender.cpp)
target_link_libraries(sender pthread)

