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

# --- Sources ---
set(SOURCES
  LinkVST.cpp
  ApiClient.cpp
  GeneratePanel.cpp
)

# --- iPlug2 boilerplate ---
include("${IPLUG2_PATH}/IPlug/IPlug.cmake")

iplug2_target_add(LinkVST APP SOURCES ${SOURCES})
iplug2_target_add(LinkVST VST3 SOURCES ${SOURCES})

# macOS: also build AU
if(APPLE)
  iplug2_target_add(LinkVST AUv2 SOURCES ${SOURCES})
endif()

# --- API config ---
target_compile_definitions(LinkVST_VST3 PRIVATE
  LINKVST_API_URL="http://localhost:8002"
  LINKVST_API_KEY=""
)
