cmake_minimum_required(VERSION 3.18)
project(LinkVST VERSION 0.1.0)

set(CMAKE_CXX_STANDARD 20)

# --- iPlug2 path (set via -DIPLUG2_PATH=... or env) ---
if(NOT DEFINED IPLUG2_PATH)
  set(IPLUG2_PATH "$ENV{IPLUG2_PATH}" CACHE PATH "Path to iPlug2 root")
endif()
if(NOT EXISTS "${IPLUG2_PATH}/IPlug/IPlug_include_in_plug_hdr.h")
  message(FATAL_ERROR "iPlug2 not found at ${IPLUG2_PATH}. Set -DIPLUG2_PATH=<path>")
endif()

# --- Dependencies via FetchContent ---
include(FetchContent)

FetchContent_Declare(
  nlohmann_json
  GIT_REPOSITORY https://github.com/nlohmann/json.git
  GIT_TAG        v3.11.3
  GIT_SHALLOW    TRUE
)
FetchContent_MakeAvailable(nlohmann_json)

find_package(CURL REQUIRED)

# --- Sources ---
set(SOURCES
  LinkVST.cpp
  ApiClient.cpp
  PreviewPlayer.cpp
  GeneratePanel.cpp
  LibraryPanel.cpp
)

if(APPLE)
  list(APPEND SOURCES DragOut_mac.mm)
elseif(WIN32)
  list(APPEND SOURCES DragOut_win.cpp)
endif()

# --- iPlug2 ---
include("${IPLUG2_PATH}/IPlug/IPlug.cmake")

iplug2_target_add(LinkVST APP  SOURCES ${SOURCES})
iplug2_target_add(LinkVST VST3 SOURCES ${SOURCES})
if(APPLE)
  iplug2_target_add(LinkVST AUv2 SOURCES ${SOURCES})
endif()

# --- Common config for all targets ---
set(ALL_TARGETS LinkVST_APP LinkVST_VST3)
if(APPLE)
  list(APPEND ALL_TARGETS LinkVST_AUv2)
endif()

foreach(_t ${ALL_TARGETS})
  target_include_directories(${_t} PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
  target_link_libraries(${_t} PRIVATE nlohmann_json::nlohmann_json CURL::libcurl)
  target_compile_definitions(${_t} PRIVATE
    LINKVST_API_URL="http://localhost:8002"
    LINKVST_API_KEY="linkvst-dev"
  )
  if(WIN32)
    target_link_libraries(${_t} PRIVATE ole32 oleaut32 uuid rpcrt4 shell32)
  endif()
  if(APPLE)
    target_link_libraries(${_t} PRIVATE "-framework AppKit" "-framework Foundation")
    set_source_files_properties(DragOut_mac.mm PROPERTIES COMPILE_FLAGS "-x objective-c++")
  endif()
endforeach()
