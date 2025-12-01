cmake_minimum_required(VERSION 3.15)
project(csc477_honors CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
include_directories(${CMAKE_SOURCE_DIR}/src)

add_compile_options(-O3 -march=native -Wall -Wextra)

add_executable(receiver_baseline src/receiver/receiver_baseline.cpp)
target_link_libraries(receiver_baseline pthread)

add_executable(receiver_threaded src/receiver/receiver_threaded.cpp)
target_link_libraries(receiver_threaded pthread)

add_executable(sender src/sender/sender.cpp)
target_link_libraries(sender pthread)
