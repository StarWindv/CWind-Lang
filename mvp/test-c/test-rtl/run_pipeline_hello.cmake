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
    # 前缀行匹配: 输出前 N 行逐行相等。unwind 附加帧含 ASLR 地址,
    # 无法整串精确匹配 —— 其内容由 EXPECTED_CONTAINS 做子串断言。
    string(REPLACE ";" "\n" expected "${EXPECTED_OUTPUT}")
    string(REPLACE "\n" ";" out_lines "${out}")
    string(REPLACE "\n" ";" exp_lines "${expected}")
    list(LENGTH exp_lines n_exp)
    list(LENGTH out_lines n_out)
    if(n_out LESS n_exp)
        message(FATAL_ERROR "输出行数 ${n_out} 少于期望 ${n_exp}: '${out}'")
    endif()
    set(_i 0)
    foreach(_e ${exp_lines})
        list(GET out_lines ${_i} _actual)
        if(NOT _actual STREQUAL _e)
            message(FATAL_ERROR "输出第 ${_i} 行应为 '${_e}', 实际 '${_actual}'")
        endif()
        math(EXPR _i "${_i} + 1")
    endforeach()
endif()
if(DEFINED EXPECTED_CONTAINS)
    foreach(_s ${EXPECTED_CONTAINS})
        string(FIND "${out}" "${_s}" _pos)
        if(_pos EQUAL -1)
            message(FATAL_ERROR "输出缺少 '${_s}': '${out}'")
        endif()
    endforeach()
endif()
if(NOT DEFINED EXPECTED_OUTPUT AND NOT DEFINED EXPECTED_CONTAINS)
    if(NOT out STREQUAL "7")
        message(FATAL_ERROR "hello 输出应为 7, 实际 '${out}'")
    endif()
endif()
