cmake_minimum_required(VERSION 3.10)
project(rf_adapt_intel CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_EXTENSIONS OFF)

# ---------------------------------------------------------------------------
# Build options
# ---------------------------------------------------------------------------

# Set to OFF in CI or environments without SDR hardware libraries.
option(BUILD_HARDWARE_TARGETS
  "Build targets that require SoapySDR (rf_adapt_intel, soapy_read_test)" ON)

# Enable AddressSanitizer + UndefinedBehaviourSanitizer for debug/CI builds.
option(ENABLE_SANITIZERS "Build with ASAN and UBSAN" OFF)

find_package(PkgConfig REQUIRED)

# ---------------------------------------------------------------------------
# Optional: liquid-dsp (FSK/PSK/OOK demodulation)
# ---------------------------------------------------------------------------
find_package(liquid QUIET CONFIG)
if (liquid_FOUND)
  message(STATUS "Found liquid via CMake config: ${liquid_VERSION}")
  set(LIQUID_USE_CMAKE TRUE)
  add_compile_definitions(HAVE_LIQUID)
else()
  pkg_check_modules(LIQUID_PKG QUIET liquid)
  if (LIQUID_PKG_FOUND)
    message(STATUS "Found liquid via pkg-config: ${LIQUID_PKG_VERSION}")
    add_compile_definitions(HAVE_LIQUID)
  else()
    message(STATUS "liquid not found (CMake config or pkg-config). GMSK advanced demod will be disabled.")
  endif()
endif()

# ---------------------------------------------------------------------------
# Hardware-dependent targets (require SoapySDR + SQLite3)
# ---------------------------------------------------------------------------
if (BUILD_HARDWARE_TARGETS)
  # SoapySDR via pkg-config
  pkg_check_modules(SOAPY_PKG QUIET SoapySDR)
  if (NOT SOAPY_PKG_FOUND)
    message(FATAL_ERROR
      "SoapySDR not found via pkg-config. Install libsoapysdr-dev or set "
      "BUILD_HARDWARE_TARGETS=OFF to skip hardware-dependent targets.")
  endif()

  # sqlite3
  pkg_check_modules(SQLITE3 REQUIRED sqlite3)
  include_directories(${SQLITE3_INCLUDE_DIRS})
  link_directories(${SQLITE3_LIBRARY_DIRS})

  include_directories(${SOAPY_PKG_INCLUDE_DIRS})
  link_directories(${SOAPY_PKG_LIBRARY_DIRS})

  if (NOT LIQUID_USE_CMAKE AND LIQUID_PKG_FOUND)
    include_directories(${LIQUID_PKG_INCLUDE_DIRS})
    link_directories(${LIQUID_PKG_LIBRARY_DIRS})
  endif()

  add_executable(rf_adapt_intel src/main.cpp)
  target_include_directories(rf_adapt_intel PRIVATE
    ${SOAPY_PKG_INCLUDE_DIRS} ${SQLITE3_INCLUDE_DIRS})
  target_link_libraries(rf_adapt_intel PRIVATE
    ${SOAPY_PKG_LIBRARIES} ${SQLITE3_LIBRARIES} pthread)
  if (LIQUID_USE_CMAKE)
    target_link_libraries(rf_adapt_intel PRIVATE liquid::liquid)
  elseif (LIQUID_PKG_FOUND)
    target_link_libraries(rf_adapt_intel PRIVATE ${LIQUID_PKG_LIBRARIES})
  endif()
  target_compile_options(rf_adapt_intel PRIVATE
    -Wall -Wextra -Wno-unused-parameter)

  # soapy_read_test — SoapySDR readStream diagnostic
  add_executable(soapy_read_test tools/soapy_read_test.cpp)
  target_include_directories(soapy_read_test PRIVATE ${SOAPY_PKG_INCLUDE_DIRS})
  target_link_libraries(soapy_read_test PRIVATE ${SOAPY_PKG_LIBRARIES} pthread)
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

# ---------------------------------------------------------------------------
# iq_metrics — standalone IQ signal metrics tool (no hardware dependencies)
#
# C++ proof-of-concept port of the core metric functions from
# tools/autotune_thresholds.py:
#   avg_power, snr_db, spectral_flatness, estimate_bandwidth_hz
#
# Build in CI with:
#   cmake -DBUILD_HARDWARE_TARGETS=OFF -B build
#   cmake --build build -t iq_metrics
# ---------------------------------------------------------------------------
add_executable(iq_metrics tools/iq_metrics.cpp)
target_compile_options(iq_metrics PRIVATE -Wall -Wextra -Wno-unused-parameter)
install(TARGETS iq_metrics
  RUNTIME DESTINATION bin
  PERMISSIONS OWNER_READ OWNER_WRITE OWNER_EXECUTE
              GROUP_READ GROUP_EXECUTE
              WORLD_READ WORLD_EXECUTE
)

# ---------------------------------------------------------------------------
# Sanitizer support (ASAN + UBSAN) — opt-in via -DENABLE_SANITIZERS=ON
# ---------------------------------------------------------------------------
if (ENABLE_SANITIZERS)
  message(STATUS "Sanitizers enabled: ASAN + UBSAN")
  target_compile_options(iq_metrics PRIVATE
    -fsanitize=address,undefined -fno-omit-frame-pointer)
  target_link_options(iq_metrics PRIVATE
    -fsanitize=address,undefined)
  if (BUILD_HARDWARE_TARGETS)
    target_compile_options(rf_adapt_intel PRIVATE
      -fsanitize=address,undefined -fno-omit-frame-pointer)
    target_link_options(rf_adapt_intel PRIVATE
      -fsanitize=address,undefined)
  endif()
endif()

# ---------------------------------------------------------------------------
# CTest integration for Python/shell test suites
# ---------------------------------------------------------------------------
enable_testing()

find_program(PYTHON3_EXECUTABLE python3)
find_program(BASH_EXECUTABLE bash)

if (PYTHON3_EXECUTABLE)
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
  # iq_metrics validation: compare C++ output with Python reference.
  # Requires iq_metrics binary to be built first (dependency handled by DEPENDS).
  add_test(
    NAME test_iq_metrics
    COMMAND ${PYTHON3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/test_iq_metrics.py
      $<TARGET_FILE:iq_metrics>
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  )
  set_tests_properties(test_iq_metrics PROPERTIES DEPENDS iq_metrics)
endif()

if (BASH_EXECUTABLE)
  add_test(
    NAME test_setup_sh
    COMMAND ${BASH_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tests/test_setup.sh
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  )
endif()