# cwindc 负例: 必须**编译失败**, 且诊断里含指定子串。
#
# 与 run_reverse_ffi_negatives.cmake (前端负例) 对称, 那一侧卡 cwindf, 这一侧
# 卡 cwindc。用途是把"某些写法是编译期诊断, 不是运行期损坏"钉住 —— 例如
# `Vec<String>` 的元素槽宽算不出来, 后端必须报错而不是按某个猜测的步长去写。
#
# 参数: CWINDC, IN_JSON, OUT_EXE, EXPECT_ERROR。

if(NOT CWINDC OR NOT IN_JSON OR NOT OUT_EXE OR NOT EXPECT_ERROR)
    message(FATAL_ERROR
            "run_cwindc_negative.cmake requires CWINDC, IN_JSON, OUT_EXE and EXPECT_ERROR")
endif()

execute_process(
        COMMAND "${CWINDC}" --emit-exe "${OUT_EXE}" "${IN_JSON}"
        RESULT_VARIABLE rc
        OUTPUT_VARIABLE out
        ERROR_VARIABLE err
)
if(rc EQUAL 0)
    message(FATAL_ERROR
            "cwindc unexpectedly succeeded (expected a diagnostic containing "
            "'${EXPECT_ERROR}'); stdout: ${out}")
endif()
string(FIND "${err}${out}" "${EXPECT_ERROR}" pos)
if(pos EQUAL -1)
    message(FATAL_ERROR
            "missing diagnostic '${EXPECT_ERROR}'; stderr: ${err}")
endif()
# 失败不该顺手留下一个可执行文件 (那会让人以为它编过了)
if(EXISTS "${OUT_EXE}")
    message(FATAL_ERROR "cwindc failed but still produced ${OUT_EXE}")
endif()
message(STATUS "  [PASS] cwindc 如期失败并给出诊断 '${EXPECT_ERROR}'")
