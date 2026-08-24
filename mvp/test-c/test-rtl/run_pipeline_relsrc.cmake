# todo-63: relative = "source" 流水线冒烟
# 把静态库改名后与 typed-ast 一起搬进独立暂存目录 (模拟"库随源码放置"),
# 信封里的 source 占位符 (@REL_SRC_DIR@) 替换为暂存目录, 再从当前工作
# 目录 (≠ 暂存目录) 编译运行 —— path 锚点若错回工作目录则链接必然失败。
# 参数:
#   CWINDC           cwindc 可执行文件
#   IN_JSON          typed-ast 模板 (含 @REL_SRC_DIR@ 占位符)
#   LIB_FILE         静态库源文件 (libcwindmath.a)
#   OUT_EXE          输出可执行文件
#   STAGE_DIR        暂存目录 (测试开始时清空重建)
#   EXPECTED_OUTPUT  分号分隔的期望输出行
#   EXPECTED_RC      期望退出码 (默认 6)
if(NOT DEFINED EXPECTED_RC)
    set(EXPECTED_RC 6)
endif()
if(EXISTS "${STAGE_DIR}")
    file(REMOVE_RECURSE "${STAGE_DIR}")
endif()
file(MAKE_DIRECTORY "${STAGE_DIR}")

file(COPY "${LIB_FILE}" DESTINATION "${STAGE_DIR}")
get_filename_component(_lib_name "${LIB_FILE}" NAME)
file(RENAME "${STAGE_DIR}/${_lib_name}" "${STAGE_DIR}/librelstage.a")

file(READ "${IN_JSON}" json_text)
string(REPLACE "@REL_SRC_DIR@" "${STAGE_DIR}" json_text "${json_text}")
file(WRITE "${STAGE_DIR}/codegen_cffi_relsrc.json" "${json_text}")

execute_process(
        COMMAND "${CWINDC}" --emit-exe "${OUT_EXE}"
                "${STAGE_DIR}/codegen_cffi_relsrc.json"
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
string(STRIP "${out}" out)
string(REPLACE ";" "\n" expected "${EXPECTED_OUTPUT}")
if(NOT out STREQUAL expected)
    message(FATAL_ERROR "输出应为 '${expected}', 实际 '${out}'")
endif()
