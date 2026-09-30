# #[opt(inline_loop)] 手工递归内联的验收。
#
# 三件事一起验 (变换本身是纯前端的, 后端只看到展开后的函数):
#
#  1. 数值: fixture 内部自带朴素递归对照组, 逐个值比对, 少一行输出即为
#     不一致 —— 展开错一层就会算错, 这是最硬的判据;
#  2. 结构: 标注版的回边 (br) 数必须**多于**对照组 —— 嵌套环真的生成了;
#  3. 递归没被消灭: 标注版函数体内恰好剩 1 个真实自调用 (main 那次不算)。

if(NOT CWINDC OR NOT IN_JSON OR NOT OUT_DIR)
    message(FATAL_ERROR "run_inline_loop.cmake requires CWINDC, IN_JSON and OUT_DIR")
endif()
if(DEFINED OPT_LEVEL)
    set(_lvl "${OPT_LEVEL}")
else()
    set(_lvl "2")
endif()

file(MAKE_DIRECTORY "${OUT_DIR}")
set(_exe "${OUT_DIR}/inline_loop.exe")

# --- 1) 跑起来, 输出必须以 "all match" 收尾 ------------------------------
execute_process(
        COMMAND "${CWINDC}" --emit-exe -O${_lvl} -o "${_exe}" "${IN_JSON}"
        RESULT_VARIABLE rc OUTPUT_VARIABLE out ERROR_VARIABLE err
)
if(NOT rc EQUAL 0)
    message(FATAL_ERROR "cwindc --emit-exe 失败 (rc=${rc}): ${err}")
endif()
execute_process(
        COMMAND "${_exe}"
        RESULT_VARIABLE rc OUTPUT_VARIABLE out ERROR_VARIABLE err
)
if(NOT rc EQUAL 0)
    message(FATAL_ERROR "程序退出码 ${rc}, 期望 0: ${err}")
endif()
string(STRIP "${out}" out)
if(NOT out MATCHES "all match$")
    message(FATAL_ERROR "末行应为 'all match' (标注版与朴素递归逐值对拍), 实际:\n${out}")
endif()
message(STATUS "  [PASS] 数值与朴素递归逐值一致 (末行 all match)")

# --- 2) 结构: 标注版回边多于对照组 ----------------------------------------
set(_ll "${OUT_DIR}/inline_loop.ll")
execute_process(
        COMMAND "${CWINDC}" --emit llvm -O0 -o "${_ll}" "${IN_JSON}"
        RESULT_VARIABLE rc OUTPUT_QUIET ERROR_VARIABLE err
)
if(NOT rc EQUAL 0)
    message(FATAL_ERROR "cwindc --emit llvm 失败: ${err}")
endif()
file(READ "${_ll}" _txt)

# 取某个函数的**函数体**(不含 define 行): 从 "@cwind.fn.<name>(" 处切到
# 下一个 define。CMake 的正则不支持 (?s), FIND 也没有"从第 N 字符开始找"
# 的重载, 所以全程用 SUBSTRING 手工推进。
# 少了 define 行是刻意的: 它本身也含 "@cwind.fn.fib(", 留着会把定义算成
# 一个调用点。
function(_body out_var fname)
    string(FIND "${_txt}" "@cwind.fn.${fname}(" _start)
    if(_start LESS 0)
        set(${out_var} "" PARENT_SCOPE)
        return()
    endif()
    string(SUBSTRING "${_txt}" ${_start} -1 _rest)
    # _rest 以本函数的 define 开头, 所以 "define " 的首个命中在 0; 真正的
    # 函数边界要从第 1 个字符之后继续找。
    string(SUBSTRING "${_rest}" 1 -1 _tail)
    string(FIND "${_tail}" "define " _next)
    if(_next LESS 0)
        set(_seg "${_rest}")
    else()
        string(SUBSTRING "${_rest}" 0 ${_next} _seg)
    endif()
    # 砍掉 define 行本身。
    string(FIND "${_seg}" "\n" _nl)
    if(_nl LESS 0)
        set(${out_var} "" PARENT_SCOPE)
    else()
        string(LENGTH "${_seg}" _seg_len)
        math(EXPR _take "${_seg_len} - ${_nl} - 1")
        string(SUBSTRING "${_seg}" ${_nl} ${_take} _body)
        set(${out_var} "${_body}" PARENT_SCOPE)
    endif()
endfunction()

function(_count out_var body pattern)
    string(REGEX MATCHALL "${pattern}" _hits "${body}")
    list(LENGTH _hits _n)
    set(${out_var} ${_n} PARENT_SCOPE)
endfunction()

_body(_fib_body "fib")
_body(_ref_body "ref_fib")
if("${_fib_body}" STREQUAL "" OR "${_ref_body}" STREQUAL "")
    message(FATAL_ERROR "未能在 IR 里定位 fib / ref_fib 的函数体")
endif()
_count(_fib_brs "${_fib_body}" "\\bbr\\b")
_count(_ref_brs "${_ref_body}" "\\bbr\\b")
if(_fib_brs LESS _ref_brs)
    message(FATAL_ERROR
        "标注版回边数 ${_fib_brs} 未超过对照组 ${_ref_brs} —— 嵌套环没有生成")
endif()
message(STATUS "  [PASS] 回边数 标注版 ${_fib_brs} > 对照组 ${_ref_brs} (嵌套环已生成)")

# --- 3) 标注版函数体内恰好剩一个真实自调用 -------------------------------
_count(_n_self "${_fib_body}" "@cwind\\.fn\\.fib\\(")
if(NOT _n_self EQUAL 1)
    message(FATAL_ERROR
        "标注版 fib 体内应恰好剩 1 个真实自调用, 实际 ${_n_self}")
endif()
message(STATUS "  [PASS] fib 体内恰好 1 个真实自调用 (最深层保留)")

message(STATUS "#[opt(inline_loop)] 断言全部通过 (opt=-O${_lvl})")
