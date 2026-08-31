# pipeline_wrapping: Wrapping trait 逐值对拍 (期望输出行数多, 用文件比对).
# 期望文件 fixtures/wrapping_expected.txt 由 rustc 对同序表达式取证固化
# (生成方式见 .handover/record/handover.wrapping.md), 每行一个值.
if(NOT DEFINED EXPECTED_FILE)
    message(FATAL_ERROR "EXPECTED_FILE is required")
endif()
if(NOT DEFINED EXPECTED_RC)
    set(EXPECTED_RC 0)
endif()

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
if(NOT rc EQUAL ${EXPECTED_RC})
    message(FATAL_ERROR "退出码应为 ${EXPECTED_RC}, 实际 ${rc}: ${err}")
endif()

file(READ "${EXPECTED_FILE}" expected)
string(REPLACE "\r\n" "\n" expected "${expected}")
string(STRIP "${out}" out)
string(STRIP "${expected}" expected)
if(NOT out STREQUAL expected)
    # 逐行找第一个差异, 方便定位
    string(REPLACE "\n" ";" got_lines "${out}")
    string(REPLACE "\n" ";" exp_lines "${expected}")
    list(LENGTH got_lines n_got)
    list(LENGTH exp_lines n_exp)
    set(_diff_msg "行数: 期望 ${n_exp}, 实际 ${n_got}; ")
    set(_i 0)
    foreach(g IN LISTS got_lines)
        list(GET exp_lines ${_i} e)
        if(NOT g STREQUAL e)
            string(APPEND _diff_msg "首个差异在第 ${_i + 1} 行: 期望 '${e}', 实际 '${g}'.")
            break()
        endif()
        math(EXPR _i "${_i} + 1")
        if(_i GREATER_EQUAL n_exp OR _i GREATER_EQUAL n_got)
            break()
        endif()
    endforeach()
    message(FATAL_ERROR "wrapping 对拍失败: ${_diff_msg}")
endif()
