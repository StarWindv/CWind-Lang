# 流水线冒烟: cwindc --emit-exe -> 运行 exe -> 校验输出与退出码
# 可传 EXPECTED_OUTPUT (分号分隔的行) 与 EXPECTED_RC (默认 6)
if(DEFINED OPT_LEVEL)
    set(_opt_args --opt "${OPT_LEVEL}")
else()
    set(_opt_args)
endif()
execute_process(
        COMMAND "${CWINDC}" --emit-exe ${_opt_args} "${OUT_EXE}" "${IN_JSON}"
        RESULT_VARIABLE rc
        OUTPUT_VARIABLE out
        ERROR_VARIABLE err
)
if(NOT rc EQUAL 0)
    message(FATAL_ERROR "cwindc 失败 (rc=${rc}): ${err}")
endif()

if(DEFINED INPUT_FILE)
    execute_process(
            COMMAND "${OUT_EXE}" ${EXE_ARGS}
            INPUT_FILE "${INPUT_FILE}"
            RESULT_VARIABLE rc
            OUTPUT_VARIABLE out
            ERROR_VARIABLE err
    )
else()
    # bug-30: EXE_ARGS (分号分隔) 作为被编译程序的命令行参数
    execute_process(
            COMMAND "${OUT_EXE}" ${EXE_ARGS}
            RESULT_VARIABLE rc
            OUTPUT_VARIABLE out
            ERROR_VARIABLE err
    )
endif()
if(NOT DEFINED EXPECTED_RC)
    set(EXPECTED_RC 6)
endif()
if(NOT rc EQUAL ${EXPECTED_RC})
    message(FATAL_ERROR "退出码应为 ${EXPECTED_RC}, 实际 ${rc}: ${err}")
endif()
string(STRIP "${out}" out)
if(DEFINED EXPECTED_OUTPUT)
    string(REPLACE ";" "\n" expected "${EXPECTED_OUTPUT}")
    if(NOT out STREQUAL expected)
        message(FATAL_ERROR "输出应为 '${expected}', 实际 '${out}'")
    endif()
else()
    if(NOT out STREQUAL "7")
        message(FATAL_ERROR "hello 输出应为 7, 实际 '${out}'")
    endif()
endif()
