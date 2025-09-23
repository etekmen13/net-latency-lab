add_executable(receiver)

target_sources(receiver PRIVATE
  ${CMAKE_CURRENT_LIST_DIR}/receiver.cpp
)

target_link_libraries(receiver PRIVATE common_headers)

if(UNIX AND NOT APPLE)
  target_link_libraries(receiver PRIVATE rt)
endif()
