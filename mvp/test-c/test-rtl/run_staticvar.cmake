# static 存储 (`static [mut] X: T = v;`) 的验收。
#
# 两件事一起验:
#
#  1. 数值: fixture 内部自带逐项对拍 (身份 / 初始化次数 / GC 压力之后的值
#     正确性), 少一行输出即为不一致 —— 少打一行 "bad: ..." 即全过, 末行
#     必须是 "all match";
#  2. 结构 (GC rooting): main 包装里**每个** `cwind.gstatic.<id>` 全局都必都
#     调 cwgc_global_register 登记为 GC 根, 并且这些登记全部排在对应的
#     `cwind.gstatic.<id>.init()` 调用**之前** —— 初始化式本身会分配, 越过
#     触发阈值就能跑一轮 GC, 那一刻写进静态槽的值必须已经是根。漏一条
#     登记就是悬垂, 所以这里逐槽对拍而不是只数条数。

if(NOT CWINDC OR NOT IN_JSON OR NOT OUT_DIR)
    message(FATAL_ERROR "run_staticvar.cmake requires CWINDC, IN_JSON and OUT_DIR")
endif()
if(DEFINED OPT_LEVEL)
    set(_lvl "${OPT_LEVEL}")
else()
    set(_lvl "2")
endif()

file(MAKE_DIRECTORY "${OUT_DIR}")
set(_exe "${OUT_DIR}/staticvar.exe")

# --- 1) 跑起来, 数值与末行必须全对 ----------------------------------------
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
    message(FATAL_ERROR "程序退出码 ${rc}, 期望 0: ${out}\n${err}")
endif()
string(STRIP "${out}" out)
if(NOT out MATCHES "all match$")
    message(FATAL_ERROR "末行应为 'all match' (逐项对拍全过), 实际:\n${out}")
endif()
if(NOT out STREQUAL "0\nall match")
    message(FATAL_ERROR "只应有 '0' 与 'all match' 两行输出, 实际:\n${out}")
endif()
message(STATUS "  [PASS] 数值/身份/初始化次数/GC 压力下的值全部正确")

# --- 2) 结构: 每个静态槽都被登记为 GC 根, 且登记早于初始化 ---------------
set(_ll "${OUT_DIR}/staticvar.ll")
execute_process(
        COMMAND "${CWINDC}" --emit llvm -O0 -o "${_ll}" "${IN_JSON}"
        RESULT_VARIABLE rc OUTPUT_QUIET ERROR_VARIABLE err
)
if(NOT rc EQUAL 0)
    message(FATAL_ERROR "cwindc --emit llvm 失败: ${err}")
endif()
file(READ "${_ll}" _txt)

# 只看 main 包装体: 从 "define i32 @main" 切到下一个 define。
# _rest 以本函数的 define 开头, 所以 "define " 的首个命中在 0; 真正的
# 函数边界要从第 1 个字符之后继续找 (CMake 的正则不支持 (?s), FIND 也
# 没有"从第 N 字符开始找"的重载, 全程用 SUBSTRING 手工推进)。
string(FIND "${_txt}" "define i32 @main" _start)
if(_start LESS 0)
    message(FATAL_ERROR "IR 里找不到 main 包装")
endif()
string(SUBSTRING "${_txt}" ${_start} -1 _rest)
string(SUBSTRING "${_rest}" 1 -1 _tail)
string(FIND "${_tail}" "define " _next)
if(_next LESS 0)
    set(_body "${_rest}")
else()
    string(SUBSTRING "${_rest}" 0 ${_next} _body)
endif()

# 槽 id 清单 (取自 IR 里的全局定义): "@cwind.gstatic.<id>.val|blob"。
# 初始化函数名是 "...init", 不算存储全局 —— 否则槽数会翻倍。
string(REGEX MATCHALL "@cwind\\.gstatic\\.[0-9]+\\.(val|blob)" _globals "${_txt}")
list(REMOVE_DUPLICATES _globals)
if(NOT _globals)
    message(FATAL_ERROR "IR 里没有任何 cwind.gstatic.* 存储全局")
endif()
set(_nstat 0)
foreach(_g IN LISTS _globals)
    math(EXPR _nstat "${_nstat} + 1")
endforeach()
message(STATUS "  [i] static 存储全局 ${_nstat} 个")

# 逐槽对拍: 每个存储槽必须有一条 cwgc_global_register, 且**全部**排在
# 对应的 .init 调用之前 (初始化式本身会分配, 越过触发阈值就能跑一轮
# GC, 那一刻写进静态槽的值必须已经是根)。
string(REGEX MATCHALL
        "cwgc_global_register\\(ptr @cwind\\.gstatic\\.[0-9]+\\.[a-z]+"
        _regs "${_body}")
string(REGEX MATCHALL
        "cwind\\.gstatic\\.[0-9]+\\.init\\(\\)"
        _inits "${_body}")
list(LENGTH _regs n_regs)
list(LENGTH _inits n_inits)
if(NOT n_regs EQUAL _nstat)
    message(FATAL_ERROR
        "static 槽 ${_nstat} 个, 但 cwgc_global_register 只有 ${n_regs} 次"
        " (漏登记的槽在 GC 下是悬垂)")
endif()
if(NOT n_inits EQUAL _nstat)
    message(FATAL_ERROR
        "static 槽 ${_nstat} 个, 但初始化调用只有 ${n_inits} 次"
        " (初始化式必须恰好跑一次)")
endif()
# 按 id 比对 (排序后): 登记与初始化的槽集合必须一致。登记顺序由 LLVM
# 的全局链表决定 (与 id 无关), 所以只比集合不比先后。
set(_reg_ids "")
foreach(_r IN LISTS _regs)
    string(REGEX MATCH "cwind\\.gstatic\\.([0-9]+)\\." _m "${_r}")
    list(APPEND _reg_ids "${CMAKE_MATCH_1}")
endforeach()
set(_init_ids "")
foreach(_i IN LISTS _inits)
    string(REGEX MATCH "cwind\\.gstatic\\.([0-9]+)\\." _m "${_i}")
    list(APPEND _init_ids "${CMAKE_MATCH_1}")
endforeach()
if(_reg_ids)
    list(SORT _reg_ids)
endif()
if(_init_ids)
    list(SORT _init_ids)
endif()
if(NOT _reg_ids STREQUAL _init_ids)
    message(FATAL_ERROR
        "登记的槽 (${_reg_ids}) 与初始化的槽 (${_init_ids}) 不是同一组")
endif()
# 先后: 最后一条登记的位置必须早于第一条初始化。
list(LENGTH _regs last_reg_idx)
math(EXPR _last_reg "${last_reg_idx} - 1")
list(GET _regs ${_last_reg} _last_reg_line)
string(FIND "${_body}" "${_last_reg_line}" _last_reg_pos)
list(GET _inits 0 _first_init_line)
string(FIND "${_body}" "${_first_init_line}" _first_init_pos)
if(_last_reg_pos GREATER _first_init_pos)
    message(FATAL_ERROR
        "最后一条 cwgc_global_register 出现在第一条 .init 调用之后"
        " (静态初始化里分配的载荷可能在登记前就被回收)")
endif()
message(STATUS
    "  [PASS] ${_nstat} 个静态槽全部登记为 GC 根, 且登记早于初始化调用")

# 初始化顺序 = id 升序 (确定性: 源码序)。
set(_init_seq "")
foreach(_i IN LISTS _inits)
    string(REGEX MATCH "cwind\\.gstatic\\.([0-9]+)\\." _m "${_i}")
    list(APPEND _init_seq "${CMAKE_MATCH_1}")
endforeach()
set(_prev "")
foreach(_id IN LISTS _init_seq)
    if(NOT _prev STREQUAL "" AND _id LESS _prev)
        message(FATAL_ERROR "初始化顺序不是 id 升序: ${_init_seq}")
    endif()
    set(_prev "${_id}")
endforeach()
message(STATUS "  [PASS] 初始化调用按 id 升序 (= 源码序) 发出")

message(STATUS "static 存储断言全部通过 (opt=-O${_lvl})")
