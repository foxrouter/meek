cmake_minimum_required(VERSION 3.25)

# ── Project ──────────────────────────────────────────────────────────────────
project(rf_adapt_intel
  VERSION 3.0.0
  DESCRIPTION "SDR signal capture and classification daemon"
  LANGUAGES CXX
)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# ── Build options ─────────────────────────────────────────────────────────────
option(BUILD_HARDWARE_TARGETS
  "Build targets that require SoapySDR (rf_adapt_intel, soapy_read_test)" ON)

option(ENABLE_SANITIZERS
  "Enable AddressSanitizer and UndefinedBehaviourSanitizer" OFF)

# ── Find tools ────────────────────────────────────────────────────────────────
find_package(PkgConfig REQUIRED)

# ── FetchContent dependencies ─────────────────────────────────────────────────
include(FetchContent)

# nlohmann/json — header-only, used by rf_adapt_intel and rf_audit
FetchContent_Declare(
  nlohmann_json
  URL https://github.com/nlohmann/json/releases/download/v3.11.3/json.tar.xz
  URL_HASH SHA256=d6c65aca6b1ed68e7a182f4757257b107ae403032760ed6ef121c9d55e81757d
)
set(JSON_BuildTests OFF CACHE BOOL "" FORCE)
set(JSON_Install OFF CACHE BOOL "" FORCE)
FetchContent_MakeAvailable(nlohmann_json)

# ── Optional: liquid-dsp (FSK/PSK/OOK demodulation) ──────────────────────────
find_package(liquid QUIET CONFIG)
if(liquid_FOUND)
  message(STATUS "Found liquid via CMake config: ${liquid_VERSION}")
  set(LIQUID_USE_CMAKE TRUE)
  add_compile_definitions(HAVE_LIQUID)
else()
  pkg_check_modules(LIQUID_PKG QUIET liquid)
  if(LIQUID_PKG_FOUND)
    message(STATUS "Found liquid via pkg-config: ${LIQUID_PKG_VERSION}")
    add_compile_definitions(HAVE_LIQUID)
  else()
    message(STATUS "liquid not found — advanced demod disabled")
  endif()
endif()

# ── Include path shared by rf_adapt_intel, rf_audit, iq_metrics ──────────────
set(MEEK_INCLUDE_DIR "${CMAKE_SOURCE_DIR}/include")

# ── Hardware-dependent targets ────────────────────────────────────────────────
if(BUILD_HARDWARE_TARGETS)

  # Probe for the C++20/23 library features used only by rf_adapt_intel.
  # cmake_push_check_state(RESET) clears only CMAKE_REQUIRED_* variables;
  # CMAKE_CXX_STANDARD is inherited so the try-compile uses the same standard
  # (-std=c++23 on GCC/Clang, /std:c++latest on MSVC) without hard-coding a
  # compiler-specific flag.  This catches mismatched compiler+stdlib combos
  # (e.g. GCC 12 or Clang with an old libstdc++) regardless of compiler family.
  include(CMakePushCheckState)
  include(CheckCXXSourceCompiles)
  find_package(Threads REQUIRED)
  cmake_push_check_state(RESET)
  set(CMAKE_REQUIRED_LIBRARIES Threads::Threads)
  check_cxx_source_compiles([=[
#include <stop_token>
#include <thread>
#include <span>
int main() {
  std::jthread t([](std::stop_token) {});
  int arr[4]{};
  std::span<int> s(arr);
  (void)t; (void)s;
  return 0;
}
]=] _STDCXX_FEATURES_AVAILABLE)
  cmake_pop_check_state()
  if(NOT _STDCXX_FEATURES_AVAILABLE)
    # Provide targeted guidance for the most common failure: GCC 12 on Bookworm.
    if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU"
       AND CMAKE_CXX_COMPILER_VERSION VERSION_LESS "13.0")
      message(FATAL_ERROR
        "C++${CMAKE_CXX_STANDARD} feature check failed: "
        "GCC ${CMAKE_CXX_COMPILER_VERSION} does not fully support the required "
        "standard library (std::jthread, std::stop_token, std::span in C++${CMAKE_CXX_STANDARD} mode).\n"
        "GCC ≥ 13 is required.\n"
        "On Bookworm: sudo apt install gcc-13 g++-13\n"
        "Then recreate the build directory:\n"
        "  rm -rf build && cmake -S . -B build -DCMAKE_CXX_COMPILER=g++-13\n"
        "If you only need the analysis tools (rf_audit, iq_metrics), you can skip the\n"
        "hardware targets entirely:\n"
        "  cmake -S . -B build -DBUILD_HARDWARE_TARGETS=OFF")
    else()
      message(FATAL_ERROR
        "C++${CMAKE_CXX_STANDARD} feature check failed "
        "(compiler: ${CMAKE_CXX_COMPILER_ID} ${CMAKE_CXX_COMPILER_VERSION}).\n"
        "The probe tests std::jthread, std::stop_token, and std::span — "
        "all required by the hardware targets.\n"
        "Ensure your compiler and standard library support C++${CMAKE_CXX_STANDARD} "
        "(GCC ≥ 13 with libstdc++ ≥ 13, or Clang ≥ 17 with a compatible libc++).\n"
        "If you only need the analysis tools (rf_audit, iq_metrics), configure with:\n"
        "  -DBUILD_HARDWARE_TARGETS=OFF")
    endif()
  endif()

  pkg_check_modules(SOAPY_PKG QUIET SoapySDR)
  if(NOT SOAPY_PKG_FOUND)
    message(FATAL_ERROR
      "SoapySDR not found.  Install libsoapysdr-dev or set "
      "BUILD_HARDWARE_TARGETS=OFF to skip hardware targets.")
  endif()

  pkg_check_modules(SQLITE3 REQUIRED sqlite3)

  # cpp-httplib (optional Prometheus HTTP server)
  FetchContent_Declare(
    cpp_httplib
    URL https://github.com/yhirose/cpp-httplib/archive/refs/tags/v0.18.3.tar.gz
    URL_HASH SHA256=a0567bcd6c3fe5cef1b329b96245119047f876b49e06cc129a36a7a8dffe173e
  )
  set(HTTPLIB_COMPILE OFF CACHE BOOL "" FORCE)
  FetchContent_MakeAvailable(cpp_httplib)

  # ── rf_adapt_intel daemon ─────────────────────────────────────────────────
  add_executable(rf_adapt_intel src/main.cpp)

  target_include_directories(rf_adapt_intel PRIVATE
    ${MEEK_INCLUDE_DIR}
    ${SOAPY_PKG_INCLUDE_DIRS}
    ${SQLITE3_INCLUDE_DIRS}
  )

  target_link_libraries(rf_adapt_intel PRIVATE
    ${SOAPY_PKG_LIBRARIES}
    ${SQLITE3_LIBRARIES}
    Threads::Threads
    nlohmann_json::nlohmann_json
  )

  if(TARGET httplib::httplib)
    target_link_libraries(rf_adapt_intel PRIVATE httplib::httplib)
    target_compile_definitions(rf_adapt_intel PRIVATE HAVE_HTTPLIB)
  endif()

  if(LIQUID_USE_CMAKE)
    target_link_libraries(rf_adapt_intel PRIVATE liquid::liquid)
  elseif(LIQUID_PKG_FOUND)
    target_include_directories(rf_adapt_intel PRIVATE ${LIQUID_PKG_INCLUDE_DIRS})
    target_link_libraries(rf_adapt_intel PRIVATE ${LIQUID_PKG_LIBRARIES})
  endif()

  # Optional: libsystemd for sd_notify watchdog integration
  pkg_check_modules(LIBSYSTEMD QUIET libsystemd)
  if(LIBSYSTEMD_FOUND)
    message(STATUS "libsystemd found — READY/WATCHDOG/STOPPING all use native sd_notify "
                   "via <systemd/sd-daemon.h>")
    target_compile_definitions(rf_adapt_intel PRIVATE HAVE_SYSTEMD)
    target_link_libraries(rf_adapt_intel PRIVATE ${LIBSYSTEMD_LIBRARIES})
    target_include_directories(rf_adapt_intel PRIVATE ${LIBSYSTEMD_INCLUDE_DIRS})
  else()
    message(STATUS "libsystemd not found — READY/WATCHDOG/STOPPING all use socket-based "
                   "sd_notify fallback (NOTIFY_SOCKET datagram)")
  endif()

  target_compile_options(rf_adapt_intel PRIVATE
    -Wall -Wextra -Wno-unused-parameter)
  # HAVE_SOAPY enables the SoapySdrSource implementation in isdr_source.hpp
  target_compile_definitions(rf_adapt_intel PRIVATE HAVE_SOAPY)

  # ── soapy_read_test ───────────────────────────────────────────────────────
  add_executable(soapy_read_test tools/soapy_read_test.cpp)
  target_include_directories(soapy_read_test PRIVATE ${SOAPY_PKG_INCLUDE_DIRS})
  target_link_libraries(soapy_read_test PRIVATE ${SOAPY_PKG_LIBRARIES} Threads::Threads)
  target_compile_options(soapy_read_test PRIVATE
    -Wall -Wextra -Wno-unused-parameter)

  install(TARGETS rf_adapt_intel
    RUNTIME DESTINATION bin
    PERMISSIONS OWNER_READ OWNER_WRITE OWNER_EXECUTE
                GROUP_READ GROUP_EXECUTE
                WORLD_READ WORLD_EXECUTE
  )

else()
  message(STATUS
    "BUILD_HARDWARE_TARGETS=OFF: skipping rf_adapt_intel and soapy_read_test.")
endif()

# ── rf_audit — CLI validation tool (no hardware deps) ─────────────────────────
add_executable(rf_audit src/rf_audit.cpp)

target_include_directories(rf_audit PRIVATE ${MEEK_INCLUDE_DIR})
target_link_libraries(rf_audit PRIVATE nlohmann_json::nlohmann_json)
target_compile_options(rf_audit PRIVATE -Wall -Wextra -Wno-unused-parameter)

install(TARGETS rf_audit
  RUNTIME DESTINATION bin
  PERMISSIONS OWNER_READ OWNER_WRITE OWNER_EXECUTE
              GROUP_READ GROUP_EXECUTE
              WORLD_READ WORLD_EXECUTE
)

# ── iq_metrics — standalone IQ signal metrics (no hardware deps) ──────────────
add_executable(iq_metrics tools/iq_metrics.cpp)
target_include_directories(iq_metrics PRIVATE ${MEEK_INCLUDE_DIR})
target_link_libraries(iq_metrics PRIVATE nlohmann_json::nlohmann_json)
target_compile_options(iq_metrics PRIVATE -Wall -Wextra -Wno-unused-parameter)

install(TARGETS iq_metrics
  RUNTIME DESTINATION bin
  PERMISSIONS OWNER_READ OWNER_WRITE OWNER_EXECUTE
              GROUP_READ GROUP_EXECUTE
              WORLD_READ WORLD_EXECUTE
)

# ── Sanitizers (opt-in) ───────────────────────────────────────────────────────
if(ENABLE_SANITIZERS)
  message(STATUS "Sanitizers enabled: ASAN + UBSAN")
  set(_san_flags -fsanitize=address,undefined -fno-omit-frame-pointer)

  foreach(_tgt iq_metrics rf_audit)
    target_compile_options(${_tgt} PRIVATE ${_san_flags})
    target_link_options(${_tgt} PRIVATE ${_san_flags})
  endforeach()

  if(BUILD_HARDWARE_TARGETS)
    target_compile_options(rf_adapt_intel PRIVATE ${_san_flags})
    target_link_options(rf_adapt_intel PRIVATE ${_san_flags})
  endif()
endif()

# ── CPack .deb packaging ──────────────────────────────────────────────────────
set(CPACK_GENERATOR "DEB")
set(CPACK_PACKAGE_NAME "rf-adapt-intel")
set(CPACK_PACKAGE_VERSION "${PROJECT_VERSION}")
set(CPACK_PACKAGE_CONTACT "foxrouter")
set(CPACK_PACKAGE_DESCRIPTION_SUMMARY "${PROJECT_DESCRIPTION}")
# Architecture is intentionally left unset so CPack/dpkg picks the host arch
# automatically (e.g. amd64, arm64, armhf for Raspberry Pi).
set(CPACK_DEBIAN_PACKAGE_DEPENDS
  "libsqlite3-0 (>= 3.31), libstdc++6 (>= 11)")
set(CPACK_DEBIAN_PACKAGE_SECTION "electronics")
set(CPACK_STRIP_FILES TRUE)
include(CPack)

# ── CTest integration ─────────────────────────────────────────────────────────
enable_testing()

find_program(PYTHON3_EXECUTABLE python3)
find_program(BASH_EXECUTABLE bash)

if(PYTHON3_EXECUTABLE)
  add_test(
    NAME test_decode_candidates
    COMMAND ${PYTHON3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/test_decode_candidates.py
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  )
  add_test(
    NAME test_guardrails
    COMMAND ${PYTHON3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/test_guardrails.py
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  )
  add_test(
    NAME test_snr_sweep
    COMMAND ${PYTHON3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/test_snr_sweep.py
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  )
  add_test(
    NAME test_demod_ber
    COMMAND ${PYTHON3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/test_demod_ber.py
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  )
  add_test(
    NAME bench_throughput
    COMMAND ${PYTHON3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/bench_throughput.py
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  )
  add_test(
    NAME test_autotune
    COMMAND ${PYTHON3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/test_autotune.py
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  )
  # iq_metrics validation: C++ output vs Python reference
  add_test(
    NAME test_iq_metrics
    COMMAND ${PYTHON3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/test_iq_metrics.py
      $<TARGET_FILE:iq_metrics>
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  )
  set_tests_properties(test_iq_metrics PROPERTIES DEPENDS iq_metrics)
  # SQLite WAL mode availability and behaviour
  add_test(
    NAME test_db_wal
    COMMAND ${PYTHON3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/test_db_wal.py
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  )
endif()

if(BASH_EXECUTABLE)
  add_test(
    NAME test_setup_sh
    COMMAND ${BASH_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/test_setup.sh -v
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  )
endif()
