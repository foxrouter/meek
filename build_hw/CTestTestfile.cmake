# CMake generated Testfile for 
# Source directory: /home/runner/work/meek/meek
# Build directory: /home/runner/work/meek/meek/build_hw
# 
# This file includes the relevant testing commands required for 
# testing this directory and lists subdirectories to be tested as well.
add_test([=[test_decode_candidates]=] "/usr/bin/python3" "/home/runner/work/meek/meek/tests/test_decode_candidates.py")
set_tests_properties([=[test_decode_candidates]=] PROPERTIES  WORKING_DIRECTORY "/home/runner/work/meek/meek" _BACKTRACE_TRIPLES "/home/runner/work/meek/meek/CMakeLists.txt;193;add_test;/home/runner/work/meek/meek/CMakeLists.txt;0;")
add_test([=[test_guardrails]=] "/usr/bin/python3" "/home/runner/work/meek/meek/tests/test_guardrails.py")
set_tests_properties([=[test_guardrails]=] PROPERTIES  WORKING_DIRECTORY "/home/runner/work/meek/meek" _BACKTRACE_TRIPLES "/home/runner/work/meek/meek/CMakeLists.txt;198;add_test;/home/runner/work/meek/meek/CMakeLists.txt;0;")
add_test([=[test_snr_sweep]=] "/usr/bin/python3" "/home/runner/work/meek/meek/tests/test_snr_sweep.py")
set_tests_properties([=[test_snr_sweep]=] PROPERTIES  WORKING_DIRECTORY "/home/runner/work/meek/meek" _BACKTRACE_TRIPLES "/home/runner/work/meek/meek/CMakeLists.txt;203;add_test;/home/runner/work/meek/meek/CMakeLists.txt;0;")
add_test([=[test_demod_ber]=] "/usr/bin/python3" "/home/runner/work/meek/meek/tests/test_demod_ber.py")
set_tests_properties([=[test_demod_ber]=] PROPERTIES  WORKING_DIRECTORY "/home/runner/work/meek/meek" _BACKTRACE_TRIPLES "/home/runner/work/meek/meek/CMakeLists.txt;208;add_test;/home/runner/work/meek/meek/CMakeLists.txt;0;")
add_test([=[bench_throughput]=] "/usr/bin/python3" "/home/runner/work/meek/meek/tests/bench_throughput.py")
set_tests_properties([=[bench_throughput]=] PROPERTIES  WORKING_DIRECTORY "/home/runner/work/meek/meek" _BACKTRACE_TRIPLES "/home/runner/work/meek/meek/CMakeLists.txt;213;add_test;/home/runner/work/meek/meek/CMakeLists.txt;0;")
add_test([=[test_autotune]=] "/usr/bin/python3" "/home/runner/work/meek/meek/tests/test_autotune.py")
set_tests_properties([=[test_autotune]=] PROPERTIES  WORKING_DIRECTORY "/home/runner/work/meek/meek" _BACKTRACE_TRIPLES "/home/runner/work/meek/meek/CMakeLists.txt;218;add_test;/home/runner/work/meek/meek/CMakeLists.txt;0;")
add_test([=[test_iq_metrics]=] "/usr/bin/python3" "/home/runner/work/meek/meek/tests/test_iq_metrics.py" "/home/runner/work/meek/meek/build_hw/iq_metrics")
set_tests_properties([=[test_iq_metrics]=] PROPERTIES  DEPENDS "iq_metrics" WORKING_DIRECTORY "/home/runner/work/meek/meek" _BACKTRACE_TRIPLES "/home/runner/work/meek/meek/CMakeLists.txt;224;add_test;/home/runner/work/meek/meek/CMakeLists.txt;0;")
add_test([=[test_db_wal]=] "/usr/bin/python3" "/home/runner/work/meek/meek/tests/test_db_wal.py")
set_tests_properties([=[test_db_wal]=] PROPERTIES  WORKING_DIRECTORY "/home/runner/work/meek/meek" _BACKTRACE_TRIPLES "/home/runner/work/meek/meek/CMakeLists.txt;232;add_test;/home/runner/work/meek/meek/CMakeLists.txt;0;")
add_test([=[test_setup_sh]=] "/usr/bin/bash" "/home/runner/work/meek/meek/tests/test_setup.sh")
set_tests_properties([=[test_setup_sh]=] PROPERTIES  WORKING_DIRECTORY "/home/runner/work/meek/meek" _BACKTRACE_TRIPLES "/home/runner/work/meek/meek/CMakeLists.txt;240;add_test;/home/runner/work/meek/meek/CMakeLists.txt;0;")
subdirs("_deps/nlohmann_json-build")
subdirs("_deps/cpp_httplib-build")
