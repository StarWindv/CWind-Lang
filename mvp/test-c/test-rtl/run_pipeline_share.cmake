# todo-55: 反向 FFI E2E —— `cwindc --emit share` -> C 宿主调用。
#
# 参数 (CMake -D, 由 mvp/CMakeLists.txt 传入):
#   CWINDC          编译器可执行
#   HOST_EXE        C 宿主 (ffi_reverse_types_host.c)
#   OUT_LIB         共享库输出路径
#   IN_JSON         fixtures/codegen_export.json
#   IN_MAIN_JSON    fixtures/codegen_hello.json (share 负例)
#   SYMTOOL         动态符号表工具 (llvm-readobj / nm)
#   SYMTOOL_KIND    "coff" (PE 导出表) 或 "nm" (ELF dynsym)
#   EXPECT_NAMES    期望的导出符号 (分号分隔)
# 可选:
#   OPT_ARGS        额外 cwindc 参数 (第二遍: -O3 --fast-math --gc disable ...)
#
# 断言:
#   1) cwindc --emit share 成功, 宿主调用全部类型映射断言通过;
#   2) 动态符号表恰好只含 EXPECT_NAMES (coff 精确集合), 且没有
#      cwind.fn.* / dead_helper / fnret.* (内部化 + globaldce);
#   3) share 模式含 main: --emit share 与 --check --emit share 都失败。

if(NOT CWINDC OR NOT HOST_EXE OR NOT OUT_LIB OR NOT IN_JSON
   OR NOT IN_MAIN_JSON OR NOT EXPECT_NAMES)
    message(FATAL_ERROR "run_pipeline_share.cmake: missing required -D arguments")
endif()

function(build_share _out _optlist)
    execute_process(
            COMMAND "${CWINDC}" --emit share ${_optlist} -o "${_out}" "${IN_JSON}"
            RESULT_VARIABLE rc
            OUTPUT_VARIABLE out
            ERROR_VARIABLE err
    )
    if(NOT rc EQUAL 0)
        message(FATAL_ERROR
                "cwindc --emit share ${_optlist} failed (rc=${rc}): ${err}")
    endif()
    if(NOT EXISTS "${_out}")
        message(FATAL_ERROR "cwindc produced no shared library: ${_out}")
    endif()
endfunction()

function(run_host _lib)
    execute_process(
            COMMAND "${HOST_EXE}" "${_lib}"
            RESULT_VARIABLE rc
            OUTPUT_VARIABLE out
            ERROR_VARIABLE err
    )
    if(NOT rc EQUAL 0)
        message(FATAL_ERROR "C host failed (rc=${rc}):\n${out}\n${err}")
    endif()
    string(FIND "${out}" "FAIL:" bad)
    if(NOT bad EQUAL -1)
        message(FATAL_ERROR "C host assertion failed:\n${out}")
    endif()
endfunction()

function(list_symbols _lib _out_var)
    if(SYMTOOL_KIND STREQUAL "coff")
        execute_process(
                COMMAND "${SYMTOOL}" --coff-exports "${_lib}"
                RESULT_VARIABLE rc
                OUTPUT_VARIABLE symout
                ERROR_VARIABLE symerr
        )
        if(NOT rc EQUAL 0)
            message(FATAL_ERROR "symbol tool failed (rc=${rc}): ${symerr}")
        endif()
        string(REGEX MATCHALL "Name: +[A-Za-z_][A-Za-z0-9_]*" _raw "${symout}")
        set(_names "")
        foreach(_s IN LISTS _raw)
            string(REGEX REPLACE "Name: +" "" _n "${_s}")
            list(APPEND _names "${_n}")
        endforeach()
    else()
        execute_process(
                COMMAND "${SYMTOOL}" -D --defined-only "${_lib}"
                RESULT_VARIABLE rc
                OUTPUT_VARIABLE symout
                ERROR_VARIABLE symerr
        )
        if(NOT rc EQUAL 0)
            message(FATAL_ERROR "symbol tool failed (rc=${rc}): ${symerr}")
        endif()
        string(REPLACE "\r\n" "\n" symout "${symout}")
        string(REPLACE "\n" ";" _lines "${symout}")
        set(_names "")
        foreach(_line IN LISTS _lines)
            if(_line MATCHES "^[0-9a-fA-F]+ +[A-Za-z] +(.+)$")
                list(APPEND _names "${CMAKE_MATCH_1}")
            endif()
        endforeach()
    endif()
    set(${_out_var} "${_names}" PARENT_SCOPE)
endfunction()

function(check_symbols _lib _label)
    list_symbols("${_lib}" names)
    list(LENGTH names n_names)
    list(LENGTH EXPECT_NAMES n_exp)
    if(SYMTOOL_KIND STREQUAL "coff" AND NOT n_names EQUAL n_exp)
        message(FATAL_ERROR
                "${_label}: dynamic symbol table has ${n_names} entries, "
                "expected ${n_exp}: ${names}")
    endif()
    foreach(_want IN LISTS EXPECT_NAMES)
        if(NOT _want IN_LIST names)
            message(FATAL_ERROR
                    "${_label}: missing export '${_want}' in ${names}")
        endif()
    endforeach()
    foreach(_n IN LISTS names)
        if(_n MATCHES "^cwind\\." OR _n STREQUAL "dead_helper"
           OR _n MATCHES "^fnret" OR _n MATCHES "^cwind\\.fn\\.")
            message(FATAL_ERROR
                    "${_label}: internal symbol '${_n}' leaked into the "
                    "dynamic symbol table")
        endif()
    endforeach()
    message(STATUS "${_label}: dynamic symbols = ${names}")
endfunction()

# 1) 默认档 (-O0, 无 --gc disable)
build_share("${OUT_LIB}" "")
run_host("${OUT_LIB}")
check_symbols("${OUT_LIB}" "share/-O0")

# 2) 优化档: -O3 + fast-math + target-cpu native + lto fat +
#    编译期关 GC (共享库分配走进程期存活); 宿主断言必须同样通过
if(OPT_ARGS)
    set(_opt_lib "${OUT_LIB}.opt")
    build_share("${_opt_lib}" "${OPT_ARGS}")
    run_host("${_opt_lib}")
    check_symbols("${_opt_lib}" "share/optimized")
endif()

# 3) share 模式含 main: cwindc --emit share 必须挡
execute_process(
        COMMAND "${CWINDC}" --emit share -o "${OUT_LIB}.main" "${IN_MAIN_JSON}"
        RESULT_VARIABLE rc
        OUTPUT_VARIABLE out
        ERROR_VARIABLE err
)
if(rc EQUAL 0)
    message(FATAL_ERROR "cwindc --emit share accepted an entry with main")
endif()
string(FIND "${err}" "must not declare 'main'" pos)
if(pos EQUAL -1)
    message(FATAL_ERROR "missing share-main diagnostic: ${err}")
endif()

# 4) `cwindc --check --emit share` 同样必须挡
execute_process(
        COMMAND "${CWINDC}" --check --emit share "${IN_MAIN_JSON}"
        RESULT_VARIABLE rc
        OUTPUT_VARIABLE out
        ERROR_VARIABLE err
)
if(rc EQUAL 0)
    message(FATAL_ERROR "cwindc --check --emit share accepted main")
endif()
string(FIND "${err}" "must not declare 'main'" pos)
if(pos EQUAL -1)
    message(FATAL_ERROR "missing share-main diagnostic on --check: ${err}")
endif()
