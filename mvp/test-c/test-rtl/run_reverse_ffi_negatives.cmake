# todo-55: 反向 FFI 前端负例 —— #[export] 非法签名 / 重名导出 /
# share 模式含 main。三条都必须让 cwindf 非零退出并给出指定诊断。
#
# 参数: CWINDF_COMMAND (list; python -m 回退时 CWINDF_PYTHONPATH 非空),
#       BAD_SIG_WIND, DUP_WIND, HELLO_WIND。

if(NOT CWINDF_COMMAND OR NOT BAD_SIG_WIND OR NOT DUP_WIND OR NOT HELLO_WIND)
    message(FATAL_ERROR
            "run_reverse_ffi_negatives.cmake: missing required -D arguments")
endif()

set(_cwindf ${CWINDF_COMMAND})
if(CWINDF_PYTHONPATH)
    set(_cwindf ${CMAKE_COMMAND} -E env "PYTHONPATH=${CWINDF_PYTHONPATH}"
            ${_cwindf})
endif()

function(expect_frontend_failure _label _needle)
    execute_process(
            COMMAND ${_cwindf} --emit share --typed-ast ${ARGN}
            RESULT_VARIABLE rc
            OUTPUT_VARIABLE out
            ERROR_VARIABLE err
    )
    if(rc EQUAL 0)
        message(FATAL_ERROR "${_label}: cwindf unexpectedly succeeded")
    endif()
    string(FIND "${err}" "${_needle}" pos)
    if(pos EQUAL -1)
        message(FATAL_ERROR
                "${_label}: missing diagnostic '${_needle}': ${err}")
    endif()
endfunction()

# 非法导出签名 (泛型容器无 C 布局)
expect_frontend_failure("bad signature" "no C-ABI mapping" "${BAD_SIG_WIND}")
# 重名导出
expect_frontend_failure("duplicate export" "uplicate exported symbol name"
        "${DUP_WIND}")
# share 模式含 main
expect_frontend_failure("share with main" "must not declare 'main'"
        "${HELLO_WIND}")
