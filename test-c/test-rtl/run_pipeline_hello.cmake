# 流水线冒烟: cwindc --emit-exe -> 运行 exe -> 校验输出与退出码
execute_process(
        COMMAND "${CWINDC}" --emit-exe "${OUT_EXE}" "${IN_JSON}"
        RESULT_VARIABLE rc
        OUTPUT_VARIABLE out
        ERROR_VARIABLE err
)
if(NOT rc EQUAL 0)
    message(FATAL_ERROR "cwindc 失败 (rc=${rc}): ${err}")
endif()

execute_process(
        COMMAND "${OUT_EXE}"
        RESULT_VARIABLE rc
        OUTPUT_VARIABLE out
        ERROR_VARIABLE err
)
if(NOT rc EQUAL 6)
    message(FATAL_ERROR "hello 退出码应为 6, 实际 ${rc}: ${err}")
endif()
string(STRIP "${out}" out)
if(NOT out STREQUAL "7")
    message(FATAL_ERROR "hello 输出应为 7, 实际 '${out}'")
endif()
