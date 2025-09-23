add_executable(sender)

target_sources(sender PRIVATE
  ${CMAKE_CURRENT_LIST_DIR}/sender.cpp
)

target_link_libraries(sender PRIVATE common_headers)

if(UNIX AND NOT APPLE)
  target_link_libraries(sender PRIVATE rt)   
endif()
