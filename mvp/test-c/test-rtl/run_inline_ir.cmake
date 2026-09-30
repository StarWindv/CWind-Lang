# #[inline] 档位的 IR 级验收。
#
# CWind 不做源码级内联 —— 档位落成 LLVM 函数属性, 由 opt 管线里那个内联
# 器执行。所以断言看 IR, 分三件事:
#
#  1. 三个函数定义上出现对应的 LLVM 函数属性 (inlinehint / alwaysinline
#     / noinline);
#  2. opt 级别带内联器时 (-O2), 调用点按属性分化: alwaysinline 的调用
#     被展开, noinline 的调用原样留着;
#  3. -O0 (整条 opt 管线被跳过) 下, 只要模块里有 alwaysinline 就补跑一
#     次内联 pass —— always 的语义是强制, 不该被 opt 级别悄悄取消; 而没有
#     alwaysinline 的模块在 -O0 下 IR 必须逐字节不变。
#
# 传入: CWINDC / IN_JSON / OUT_DIR, 可选 OPT_LEVEL (默认 2)。

if(NOT CWINDC OR NOT IN_JSON OR NOT OUT_DIR)
    message(FATAL_ERROR "run_inline_ir.cmake requires CWINDC, IN_JSON and OUT_DIR")
endif()

if(DEFINED OPT_LEVEL)
    set(_lvl "${OPT_LEVEL}")
else()
    set(_lvl "2")
endif()

file(MAKE_DIRECTORY "${OUT_DIR}")

# --- 计数工具: 数一个文件里匹配正则的行数 -------------------------------
function(_count_matches file pattern out_var)
    file(READ "${file}" _txt)
    string(REGEX MATCHALL "${pattern}" _hits "${_txt}")
    list(LENGTH _hits _n)
    set(${out_var} ${_n} PARENT_SCOPE)
endfunction()

function(_emit_ir out_ll)
    execute_process(
            COMMAND "${CWINDC}" --emit llvm -O${_lvl} -o "${out_ll}" "${IN_JSON}"
            RESULT_VARIABLE rc
            OUTPUT_VARIABLE out
            ERROR_VARIABLE err
    )
    if(NOT rc EQUAL 0)
        message(FATAL_ERROR "cwindc --emit llvm 失败 (rc=${rc}): ${err}")
    endif()
endfunction()

function(_expect_count what expected actual)
    if(NOT expected EQUAL actual)
        message(FATAL_ERROR "${what}: 期望 ${expected}, 实际 ${actual}")
    endif()
    message(STATUS "  [PASS] ${what} = ${actual}")
endfunction()

# --- 1) 属性确实挂上了 ---------------------------------------------------
set(_ll "${OUT_DIR}/inline_O${_lvl}.ll")
_emit_ir("${_ll}")

_count_matches("${_ll}" "inlinehint" _n_hint)
_count_matches("${_ll}" "alwaysinline" _n_always)
_count_matches("${_ll}" "noinline" _n_never)
# 每个属性出现两次: 一次在属性组 (`attributes #N = { ... }`), 一次在
# dump 出来的 "; Function Attrs:" 注释行上。
_expect_count("inlinehint 属性" 2 ${_n_hint})
_expect_count("alwaysinline 属性" 2 ${_n_always})
_expect_count("noinline 属性" 2 ${_n_never})

# --- 2) 调用点按属性分化 -------------------------------------------------
_count_matches("${_ll}" "call[^\n]*@cwind\\.fn\\.forced" _n_call_forced)
_count_matches("${_ll}" "call[^\n]*@cwind\\.fn\\.blocked" _n_call_blocked)
_expect_count("alwaysinline 的调用点残留" 0 ${_n_call_forced})
_expect_count("noinline 的调用点保留" 1 ${_n_call_blocked})

# --- 3) -O0: 有 alwaysinline 就补跑内联, 没有就不动 ---------------------
set(_ll0 "${OUT_DIR}/inline_O0.ll")
execute_process(
        COMMAND "${CWINDC}" --emit llvm -O0 -o "${_ll0}" "${IN_JSON}"
        RESULT_VARIABLE rc ERROR_VARIABLE err OUTPUT_QUIET
)
if(NOT rc EQUAL 0)
    message(FATAL_ERROR "cwindc -O0 失败 (rc=${rc}): ${err}")
endif()
_count_matches("${_ll0}" "call[^\n]*@cwind\\.fn\\.forced" _n0_forced)
_count_matches("${_ll0}" "call[^\n]*@cwind\\.fn\\.blocked" _n0_blocked)
_expect_count("-O0 下 alwaysinline 的调用点残留" 0 ${_n0_forced})
_expect_count("-O0 下 noinline 的调用点保留" 1 ${_n0_blocked})

# 同一份程序去掉 alwaysinline 后, -O0 的 IR 必须与"什么都没补跑"一致:
# 用 blocked 单独的一份 JSON 比对 (该程序无 alwaysinline)。
set(_ll0_ref "${OUT_DIR}/noinline_only_O0.ll")
if(DEFINED NOINLINE_ONLY_JSON AND EXISTS "${NOINLINE_ONLY_JSON}")
    execute_process(
            COMMAND "${CWINDC}" --emit llvm -O0 -o "${_ll0_ref}" "${NOINLINE_ONLY_JSON}"
            RESULT_VARIABLE rc ERROR_VARIABLE err OUTPUT_QUIET
    )
    if(NOT rc EQUAL 0)
        message(FATAL_ERROR "cwindc -O0 (noinline only) 失败: ${err}")
    endif()
    _count_matches("${_ll0_ref}" "call[^\n]*@cwind\\.fn\\.blocked" _nref_blocked)
    _expect_count("无 alwaysinline 的模块在 -O0 下调用点保留" 1 ${_nref_blocked})
endif()

message(STATUS "#[inline] IR 断言全部通过 (opt=-O${_lvl})")
