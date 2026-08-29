# todo-148: fixture typed-JSON build-time generation runner.
#
# Invoked from mvp/CMakeLists.txt (never by hand):
#   cmake -DCWINDF_COMMAND=<command list>
#         [-DCWINDF_PYTHONPATH=<path>]
#         -DSOURCE=<x.wind> -DTARGET=<x.json>
#         ["-DFRONTEND_ARGS=--target-os;windows"]
#         -P gen_fixture.cmake
#
# CWINDF_COMMAND is a list: a single cwindf executable path, or
# "<python>; -m; cwind_frontend.cli" (then CWINDF_PYTHONPATH must be set).

if(NOT CWINDF_COMMAND OR NOT SOURCE OR NOT TARGET)
    message(FATAL_ERROR "gen_fixture.cmake requires CWINDF_COMMAND, SOURCE and TARGET")
endif()

set(_cmd ${CWINDF_COMMAND})
if(CWINDF_PYTHONPATH)
    set(_cmd ${CMAKE_COMMAND} -E env "PYTHONPATH=${CWINDF_PYTHONPATH}" ${_cmd})
endif()

get_filename_component(_src "${SOURCE}" ABSOLUTE)
get_filename_component(_out "${TARGET}" ABSOLUTE)

execute_process(
        COMMAND ${_cmd} ${FRONTEND_ARGS} --typed-ast "${_src}"
        OUTPUT_FILE "${_out}"
        ERROR_VARIABLE _err
        RESULT_VARIABLE _rc
)

if(NOT _rc EQUAL 0)
    # Do not leave a truncated/empty JSON behind on failure.
    file(REMOVE "${_out}")
    message(FATAL_ERROR "cwindf failed for ${_src} (exit ${_rc}):\n${_err}")
endif()

if(NOT EXISTS "${_out}")
    message(FATAL_ERROR "cwindf produced no output for ${_src}")
endif()
