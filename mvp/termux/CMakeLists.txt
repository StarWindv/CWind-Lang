cmake_minimum_required(VERSION 3.16)

# CWind - Termux (proot Ubuntu) 专用构建配置
#
#     通过 `cmake -S termux -B build.termux` 使用 (见 build.termux.sh)
#   - LLVM 固定使用仓库内置的 ./.LLVM18 (LLVM 18.1.8 aarch64 Linux 静态库版),
#     不搜索、不链接任何系统 LLVM
#   - 静态库清单直接由 .LLVM18/bin/llvm-config --libs 导出 (拓扑序),
#     避免手写清单遗漏 TargetMachine/CodeGen 等组件

project(
        cwind
        VERSION 0.0.1
        LANGUAGES C
)
set(PROJECT_VERSION_FULL "${PROJECT_VERSION}+Canary")

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

# 项目根目录: 本文件位于 <root>/termux/CMakeLists.txt
get_filename_component(CWIND_ROOT "../.." ABSOLUTE)


# 内置 LLVM18
set(CWIND_LLVM_DIR "${CWIND_ROOT}/.LLVM18" CACHE PATH "Bundled LLVM18 dir (no system LLVM)")
if(NOT EXISTS "${CWIND_LLVM_DIR}/include/llvm-c/Core.h")
    message(FATAL_ERROR
            "Bundled LLVM18 not found: ${CWIND_LLVM_DIR}/include/llvm-c/Core.h")
endif()
if(NOT EXISTS "${CWIND_LLVM_DIR}/bin/llvm-config")
    message(FATAL_ERROR
            "Bundled LLVM18 llvm-config not found: ${CWIND_LLVM_DIR}/bin/llvm-config")
endif()

execute_process(
        COMMAND "${CWIND_LLVM_DIR}/bin/llvm-config" --libs
        OUTPUT_VARIABLE CWIND_LLVM_LIB_FLAGS
        OUTPUT_STRIP_TRAILING_WHITESPACE
)
execute_process(
        COMMAND "${CWIND_LLVM_DIR}/bin/llvm-config" --libdir
        OUTPUT_VARIABLE CWIND_LLVM_LIBDIR
        OUTPUT_STRIP_TRAILING_WHITESPACE
)
string(REPLACE "-l" "" CWIND_LLVM_LIB_NAMES "${CWIND_LLVM_LIB_FLAGS}")
separate_arguments(CWIND_LLVM_LIB_NAMES)
set(CWIND_LLVM_LIBS "")
foreach(_name IN LISTS CWIND_LLVM_LIB_NAMES)
    list(APPEND CWIND_LLVM_LIBS "${CWIND_LLVM_LIBDIR}/lib${_name}.a")
endforeach()
list(LENGTH CWIND_LLVM_LIBS CWIND_LLVM_LIB_COUNT)

# LLVM 静态库需要的系统库 (对应 llvm-config --system-libs 去掉 -l 前缀;
# zlib/tinfo/xml2 由 apt 的 zlib1g-dev/libncurses-dev/libxml2-dev 提供)
set(CWIND_LLVM_SYSTEM_LIBS rt dl pthread m z tinfo xml2 stdc++)

message(STATUS "CWind root     : ${CWIND_ROOT}")
message(STATUS "LLVM18 dir     : ${CWIND_LLVM_DIR}")
message(STATUS "LLVM libs      : ${CWIND_LLVM_LIBDIR} (${CWIND_LLVM_LIB_COUNT} archives from llvm-config)")
message(STATUS "LLVM sys libs  : ${CWIND_LLVM_SYSTEM_LIBS}")


# 运行时库
file(GLOB STL_ESS_HEADERS    "${CWIND_ROOT}/rt-src/include/stl/ess/*.h")
file(GLOB STL_NORMAL_HEADERS "${CWIND_ROOT}/rt-src/include/stl/normal/*.h")
file(GLOB STL_JSON_HEADERS   "${CWIND_ROOT}/rt-src/include/stl/json/*.h")
file(GLOB MEMORY_HEADERS     "${CWIND_ROOT}/rt-src/include/memory/*.h")
file(GLOB OBJECT_HEADERS     "${CWIND_ROOT}/rt-src/include/object/*.h")
file(GLOB RT_HEADERS         "${CWIND_ROOT}/rt-src/include/rt/*.h")

add_library(
        cwind_rt
        STATIC
        "${CWIND_ROOT}/rt-src/rt/cwind_memcenter.c"
        "${CWIND_ROOT}/rt-src/rt/cwind_object.c"
        "${CWIND_ROOT}/rt-src/rt/cwind_container.c"
        "${CWIND_ROOT}/rt-src/rt/cwind_builtin.c"
        "${CWIND_ROOT}/rt-src/rt/cwind_builtin_table.c"
        "${CWIND_ROOT}/rt-src/rt/cwind_chkstk.c"
        "${CWIND_ROOT}/rt-src/rt/stackframe.c"
        ${MEMORY_HEADERS}
        ${OBJECT_HEADERS}
        ${RT_HEADERS}
        ${STL_ESS_HEADERS}
        ${STL_NORMAL_HEADERS}
        ${STL_JSON_HEADERS}
)
target_include_directories(cwind_rt PUBLIC "${CWIND_ROOT}/rt-src/include")
target_compile_features(cwind_rt PUBLIC c_std_11)

add_library(cwind_stl INTERFACE)
target_include_directories(cwind_stl INTERFACE "${CWIND_ROOT}/rt-src/include")
target_link_libraries(cwind_stl INTERFACE cwind_rt)


# 模块/类型/布局/符号
add_library(cwind_compiler STATIC
        "${CWIND_ROOT}/compiler/cwmodule.c"
        "${CWIND_ROOT}/compiler/cwtype.c"
        "${CWIND_ROOT}/compiler/cwlayout.c"
        "${CWIND_ROOT}/compiler/cwsymbol.c"
)
target_include_directories(cwind_compiler PUBLIC "${CWIND_ROOT}/compiler" "${CWIND_ROOT}/rt-src/include")
target_compile_features(cwind_compiler PUBLIC c_std_11)


# LLVM 后端 (cwllvm.c + cwcodegen.c), 只链接内置 .LLVM18
add_library(cwind_llvm STATIC
        "${CWIND_ROOT}/compiler/cwllvm.c"
        "${CWIND_ROOT}/compiler/cwcodegen.c"
)
target_include_directories(
        cwind_llvm
        PUBLIC
        "${CWIND_ROOT}/compiler"
        "${CWIND_ROOT}/rt-src/include"
        "${CWIND_LLVM_DIR}/include"
)
target_link_libraries(cwind_llvm PUBLIC cwind_compiler)
target_link_libraries(cwind_llvm PRIVATE ${CWIND_LLVM_LIBS} ${CWIND_LLVM_SYSTEM_LIBS})
target_compile_features(cwind_llvm PUBLIC c_std_11)
if(CMAKE_C_COMPILER_ID MATCHES "Clang")
    target_compile_options(cwind_llvm PRIVATE -Wno-language-extension-token)
endif()


# cwindc 内部会用 clang 把 IR 编成 obj, 再用 gcc 链接 rt 出可执行文件:
#   - clang 必须是内置 .LLVM18/bin/clang (绝对路径)
#   - gcc 必须是带斜杠的绝对路径, 否则会拼上 Windows 默认 CWINDC_GCC_DIR
set(CWIND_CLANG_DEFAULT "${CWIND_LLVM_DIR}/bin/clang")
set(CWIND_GCC_EXE "" CACHE FILEPATH "gcc used by cwindc to link generated exe (absolute path)")
if(NOT CWIND_GCC_EXE)
    if(EXISTS "/usr/bin/gcc")
        set(CWIND_GCC_EXE "/usr/bin/gcc")
    else()
        set(CWIND_GCC_EXE "gcc")
    endif()
endif()
message(STATUS "cwindc clang   : ${CWIND_CLANG_DEFAULT}")
message(STATUS "cwindc gcc     : ${CWIND_GCC_EXE}")

add_executable(cwindc "${CWIND_ROOT}/compiler/cwindc.c")
target_link_libraries(cwindc PRIVATE cwind_llvm)
target_compile_definitions(
        cwindc
        PRIVATE
        CWINDC_CLANG_DEFAULT="${CWIND_CLANG_DEFAULT}"
        CWINDC_RT_DIR="${CWIND_ROOT}/rt-src/rt"
        CWINDC_GCC="${CWIND_GCC_EXE}"
)


# 可选测试
option(CWIND_BUILD_TESTS "Build the standalone rt test executables" OFF)
if(CWIND_BUILD_TESTS)
    enable_testing()

    add_executable(test_memcenter "${CWIND_ROOT}/test-c/test-rtl/test_memcenter.c")
    target_link_libraries(test_memcenter PRIVATE cwind_rt)
    add_test(NAME memcenter COMMAND test_memcenter)

    add_executable(test_cwobject "${CWIND_ROOT}/test-c/test-rtl/test_cwobject.c")
    target_link_libraries(test_cwobject PRIVATE cwind_rt)
    add_test(NAME cwobject COMMAND test_cwobject)

    add_executable(test_stackframe "${CWIND_ROOT}/test-c/test-rtl/test_stackframe.c")
    target_link_libraries(test_stackframe PRIVATE cwind_rt)
    add_test(NAME stackframe COMMAND test_stackframe)

    add_executable(test_cwcontainer "${CWIND_ROOT}/test-c/test-rtl/test_cwcontainer.c")
    target_link_libraries(test_cwcontainer PRIVATE cwind_rt)
    add_test(NAME cwcontainer COMMAND test_cwcontainer)

    add_executable(test_cwabi "${CWIND_ROOT}/test-c/test-rtl/test_cwabi.c")
    target_link_libraries(test_cwabi PRIVATE cwind_rt)
    add_test(NAME cwabi COMMAND test_cwabi)

    add_executable(test_cwmodule "${CWIND_ROOT}/test-c/test-rtl/test_cwmodule.c")
    target_link_libraries(test_cwmodule PRIVATE cwind_compiler)
    add_test(NAME cwmodule COMMAND test_cwmodule)

    add_executable(test_cwbuiltin "${CWIND_ROOT}/test-c/test-rtl/test_cwbuiltin.c")
    target_link_libraries(test_cwbuiltin PRIVATE cwind_rt)
    add_test(NAME cwbuiltin COMMAND test_cwbuiltin)

    add_executable(test_cwtype "${CWIND_ROOT}/test-c/test-rtl/test_cwtype.c")
    target_link_libraries(test_cwtype PRIVATE cwind_compiler)
    add_test(NAME cwtype COMMAND test_cwtype)

    add_executable(test_cwsymbol "${CWIND_ROOT}/test-c/test-rtl/test_cwsymbol.c")
    target_link_libraries(test_cwsymbol PRIVATE cwind_compiler)
    add_test(NAME cwsymbol COMMAND test_cwsymbol)

    add_executable(test_cwcffi "${CWIND_ROOT}/test-c/test-rtl/test_cwcffi.c")
    target_link_libraries(test_cwcffi PRIVATE cwind_compiler)
    add_test(NAME cwcffi COMMAND test_cwcffi)

    # CFFI 测试用静态库 (todo-48/49), 产物名 libcwindmath.a
    add_library(cwindmath STATIC "${CWIND_ROOT}/test-c/test-rtl/ffi_math.c")
    set_target_properties(cwindmath PROPERTIES PREFIX "lib")

    if(TARGET cwind_llvm)
        function(cw_add_pipeline_test name json)
            set(_args
                    -DCWINDC=$<TARGET_FILE:cwindc>
                    -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/${name}_pipeline.exe
                    -DIN_JSON=${CWIND_ROOT}/test-c/test-rtl/fixtures/${json})
            if(ARGC GREATER 2)
                set(_out "${ARGV2}")
                string(REPLACE ";" "\\;" _out "${_out}")
                list(APPEND _args "-DEXPECTED_OUTPUT=${_out}")
            endif()
            if(ARGC GREATER 3)
                list(APPEND _args "-DEXPECTED_RC=${ARGV3}")
            endif()
            if(ARGC GREATER 4)
                list(APPEND _args "-DOPT_LEVEL=${ARGV4}")
            endif()
            add_test(
                    NAME ${name}
                    COMMAND ${CMAKE_COMMAND} ${_args}
                            -P ${CWIND_ROOT}/test-c/test-rtl/run_pipeline_hello.cmake
            )
            set_tests_properties(
                    ${name}
                    PROPERTIES
                    ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin:$ENV{PATH}"
            )
        endfunction()

        cw_add_pipeline_test(hello codegen_hello.json)
        cw_add_pipeline_test(containers codegen_containers.json "1;1;2;3;4;4;9" 4)
        cw_add_pipeline_test(maps codegen_maps.json "2;1;2;3;3;9;true;false;0" 5)
        cw_add_pipeline_test(mapiter codegen_mapiter.json "c;3;c;3;b;2;b;2;a;1;a;1;6;(7, x);7;x;7;x;(1, 2);1;2;n;(1, 2);2;();0;6;true" 9)
        cw_add_pipeline_test(strings codegen_strings.json "hello wind;10;xy;true;ab" 7)
        cw_add_pipeline_test(vecmethods codegen_vecmethods.json "4;3;4;9;1;9;4" 6)
        cw_add_pipeline_test(structs codegen_structs.json "3;4;9;13;1;2;2;4" 7)
        cw_add_pipeline_test(generics codegen_generics.json "3;7;hi;[1, 2];9" 7)
        cw_add_pipeline_test(genmethods codegen_genmethods.json "3;9;5;a" 7)
        cw_add_pipeline_test(structfull codegen_structfull.json "7;11;13;0;wind;1;14;x;3;9;1;n;1;3" 13)
        cw_add_pipeline_test(which codegen_which.json "after;6;after;0;hello;log" 9)
        cw_add_pipeline_test(newset codegen_newset.json "3;6;2;0;false;0;0" 6)
        cw_add_pipeline_test(setmethods codegen_setmethods.json "0;3;true;false;false;2;5;0" 8)
        cw_add_pipeline_test(numeric codegen_numeric.json "832040;12586269025;true;true;1.5;true;0.2;100001;4000000000;18446744073709551615;128;Int" 7)
        cw_add_pipeline_test(opt3 codegen_numeric.json "832040;12586269025;true;true;1.5;true;0.2;100001;4000000000;18446744073709551615;128;Int" 7 3)
        cw_add_pipeline_test(format codegen_format.json "v=42, b=true, name=wind;literal {x} and 1-2;plain;s=7" 0)
        cw_add_pipeline_test(patterns codegen_patterns.json "zero;small;big;one;10;20;30;7;103;1;0;1;0;1;0;1;0;5;hi;42;-1;7;103;2;90;-1;-1;42;10;11;12;zero;other" 17)
        cw_add_pipeline_test(enums codegen_enums.json "42;103;-1;42;-1;1;2;3;hi;none;7;0;9;-2;-1;9;102;-1;105;5" 11)
        cw_add_pipeline_test(conversions codegen_conversions.json "42;7;-3;255;4000000000;18446744073709551615;3.5;2.25;200;42;-3;18446744073709551615;2.25;0;-12345" 0)
        cw_add_pipeline_test(cffi codegen_cffi.json "7;4;1024;3024;4000000000;9;42" 0)
        # #[link(path="./libcwindmath.a")] 相对构建目录解析
        set_tests_properties(
                pipeline_cffi
                PROPERTIES
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        cw_add_pipeline_test(cffi_linkname codegen_cffi_linkname.json "42;70;9;9" 0)
        # #[link(path="./libcwindmath.a")] 相对构建目录解析
        set_tests_properties(
                pipeline_cffi_linkname
                PROPERTIES
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )

        add_executable(test_cwllvm "${CWIND_ROOT}/test-c/test-rtl/test_cwllvm.c")
        target_link_libraries(test_cwllvm PRIVATE cwind_llvm)
        add_test(NAME cwllvm COMMAND test_cwllvm)
        set_tests_properties(
                cwllvm
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin:$ENV{PATH}"
        )

        add_executable(test_cwcodegen "${CWIND_ROOT}/test-c/test-rtl/test_cwcodegen.c")
        target_link_libraries(test_cwcodegen PRIVATE cwind_llvm)
        target_compile_definitions(
                test_cwcodegen
                PRIVATE
                CWIND_FIXTURE_DIR="${CWIND_ROOT}/test-c/test-rtl/fixtures"
        )
        add_test(NAME cwcodegen COMMAND test_cwcodegen)
        set_tests_properties(
                cwcodegen
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin:$ENV{PATH}"
        )
    endif()
endif()
