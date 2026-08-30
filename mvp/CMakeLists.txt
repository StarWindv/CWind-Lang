cmake_minimum_required(VERSION 3.16)

project(
        cwind
        VERSION 0.0.1
        LANGUAGES C
)
set(PROJECT_VERSION_FULL "${PROJECT_VERSION}+Canary")
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)


file(GLOB STL_ESS_HEADERS    "rt-src/include/stl/ess/*.h")
file(GLOB STL_NORMAL_HEADERS "rt-src/include/stl/normal/*.h")
file(GLOB STL_JSON_HEADERS   "rt-src/include/stl/json/*.h")
file(GLOB MEMORY_HEADERS     "rt-src/include/memory/*.h")
file(GLOB GC_HEADERS         "rt-src/include/gc/*.h")
file(GLOB OBJECT_HEADERS     "rt-src/include/object/*.h")
file(GLOB RT_HEADERS         "rt-src/include/rt/*.h")

add_library(
        cwind_rt
        STATIC
        rt-src/rt/cwind_memcenter.c
        rt-src/rt/cwind_object.c
        rt-src/rt/cwind_container.c
        rt-src/rt/cwind_builtin.c
        rt-src/rt/cwind_builtin_table.c
        rt-src/rt/cwind_chkstk.c
        rt-src/rt/stackframe.c
        rt-src/rt/cwind_gc.c
        ${MEMORY_HEADERS}
        ${GC_HEADERS}
        ${OBJECT_HEADERS}
        ${RT_HEADERS}
        ${STL_ESS_HEADERS}
        ${STL_NORMAL_HEADERS}
        ${STL_JSON_HEADERS}
)
target_include_directories(cwind_rt PUBLIC rt-src/include)
target_compile_features(cwind_rt PUBLIC c_std_11)


add_library(cwind_stl INTERFACE)
target_include_directories(cwind_stl INTERFACE rt-src/include)
target_link_libraries(
        cwind_stl
        INTERFACE
        cwind_rt
)

# CFFI 测试用静态库 (todo-48/49): 被 fixtures/codegen_cffi.wind 的
# #[link(name=..., kind=..., path="./libcwindmath.a")] 引用,
# 产物名固定为 libcwindmath.a, pipeline_cffi 以构建目录为工作目录。
add_library(cwindmath STATIC test-c/test-rtl/ffi_math.c)
# SUFFIX 固定 .a: clang/MSVC 风格工具链默认产出 .lib, 会导致
# pipeline_cffi 等测试找不到 libcwindmath.a (todo-62 期间修复)
set_target_properties(cwindmath PROPERTIES PREFIX "lib" SUFFIX ".a")

add_library(cwind_compiler STATIC
        compiler/cwmodule.c
        compiler/cwtype.c
        compiler/cwlayout.c
        compiler/cwsymbol.c
)
target_include_directories(cwind_compiler PUBLIC compiler rt-src/include)
target_compile_features(cwind_compiler PUBLIC c_std_11)

set(CWIND_LLVM_DIR "${CMAKE_SOURCE_DIR}/../.LLVM18" CACHE PATH "Path to LLVM18")
if(EXISTS "${CWIND_LLVM_DIR}/include/llvm-c/Core.h")
    add_library(cwind_llvm STATIC compiler/cwllvm.c compiler/cwcodegen.c)
    target_include_directories(
            cwind_llvm
            PUBLIC
            compiler
            rt-src/include
            "${CWIND_LLVM_DIR}/include"
    )

    if(EXISTS "/proc/sys/fs/binfmt_misc/WSLInterop")
        set(IS_WSL TRUE)
    endif()

    if(WIN32)
        set(LLVM_C_LIB "${CWIND_LLVM_DIR}/lib/LLVM-C.lib")
    elseif(IS_WSL)
        set(LLVM_C_LIB "${CWIND_LLVM_DIR}/lib/LLVM-C.lib")
    elseif(APPLE)
        if(EXISTS "${CWIND_LLVM_DIR}/lib/libLLVM-C.a")
            set(LLVM_C_LIB "${CWIND_LLVM_DIR}/lib/libLLVM-C.a")
        elseif(EXISTS "${CWIND_LLVM_DIR}/lib/libLLVM-C.dylib")
            set(LLVM_C_LIB "${CWIND_LLVM_DIR}/lib/libLLVM-C.dylib")
        else()
            message(FATAL_ERROR "Cannot find LLVM-C library in ${CWIND_LLVM_DIR}/lib")
        endif()
    else()
        if(EXISTS "${CWIND_LLVM_DIR}/lib/libLLVM-C.so")
            set(LLVM_C_LIB "${CWIND_LLVM_DIR}/lib/libLLVM-C.so")
        elseif(EXISTS "${CWIND_LLVM_DIR}/lib/libLLVM-C.a")
            set(LLVM_C_LIB "${CWIND_LLVM_DIR}/lib/libLLVM-C.a")
        else()
            set(LLVM_C_LIB
                "${CWIND_LLVM_DIR}/lib/libLLVMCore.a"
                "${CWIND_LLVM_DIR}/lib/libLLVMSupport.a"
                "${CWIND_LLVM_DIR}/lib/libLLVMBinaryFormat.a"
                "${CWIND_LLVM_DIR}/lib/libLLVMRemarks.a"
                "${CWIND_LLVM_DIR}/lib/libLLVMBitstreamReader.a"
                "${CWIND_LLVM_DIR}/lib/libLLVMTargetParser.a"
                "${CWIND_LLVM_DIR}/lib/libLLVMFrontendOpenMP.a"
            )
        endif()
    endif()

    message(STATUS "Using LLVM-C library: ${LLVM_C_LIB}")

    target_link_libraries(
            cwind_llvm
            PUBLIC
            cwind_compiler
            "${LLVM_C_LIB}"
    )
    target_compile_features(cwind_llvm PUBLIC c_std_11)
    if(CMAKE_C_COMPILER_ID MATCHES "Clang")
        target_compile_options(
                cwind_llvm
                PRIVATE
                -Wno-language-extension-token
        )
    endif()

    add_executable(cwindc compiler/cwindc.c)
    target_link_libraries(cwindc PRIVATE cwind_llvm)

    if(WIN32)
        set(CWIND_CLANG_DEFAULT "${CWIND_LLVM_DIR}/bin/clang.exe")
        set(CWIND_GCC_DEFAULT "gcc")
    else()
        set(CWIND_CLANG_DEFAULT "${CWIND_LLVM_DIR}/bin/clang")
        set(CWIND_GCC_DEFAULT "gcc")
    endif()

    target_compile_definitions(
            cwindc
            PRIVATE
            CWINDC_CLANG_DEFAULT="${CWIND_CLANG_DEFAULT}"
            CWINDC_RT_DIR="${CMAKE_SOURCE_DIR}/rt-src/rt"
            CWINDC_GCC="${CWIND_GCC_DEFAULT}"
    )
endif()

option(CWIND_BUILD_TESTS "Build the standalone rt test executables" OFF)
if(CWIND_BUILD_TESTS)
    enable_testing()

    # -----------------------------------------------------------------------
    # todo-148: pipeline fixture typed-JSON 自动生成。
    # fixtures/*.json 是 cwindf 的生成物 (.gitignore 排除, 不入库),
    # 新克隆机器上由本节在 `cmake --build` 时调用前端自动产出。
    # 解析优先级: 缓存变量 CWINDF_EXECUTABLE > 仓库 .venv > PATH > python -m
    # (python -m 回退需要前端依赖已在当前解释器可用, 仅作尽力而为)。
    set(CWINDF_EXECUTABLE "" CACHE FILEPATH
            "cwindf frontend executable used to generate test fixtures")
    set(_cwindf_cmd "")
    set(_cwindf_pythonpath "")
    if(CWINDF_EXECUTABLE)
        set(_cwindf_cmd "${CWINDF_EXECUTABLE}")
    else()
        foreach(_cand
                "${CMAKE_SOURCE_DIR}/../.venv/Scripts/cwindf.exe"
                "${CMAKE_SOURCE_DIR}/../.venv/bin/cwindf")
            if(EXISTS "${_cand}")
                set(_cwindf_cmd "${_cand}")
                break()
            endif()
        endforeach()
        if(NOT _cwindf_cmd)
            find_program(CWINDF_PROG cwindf)
            if(CWINDF_PROG)
                set(_cwindf_cmd "${CWINDF_PROG}")
            endif()
        endif()
        if(NOT _cwindf_cmd)
            find_package(Python3 QUIET COMPONENTS Interpreter)
            if(Python3_Interpreter_FOUND)
                set(_cwindf_cmd "${Python3_EXECUTABLE};-m;cwind_frontend.cli")
                set(_cwindf_pythonpath "${CMAKE_SOURCE_DIR}/frontend/src")
            endif()
        endif()
    endif()

    set(CWIND_FIXTURES_DIR "${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures")
    # 前端任何源文件变化都应触发 JSON 再生成 (可编辑安装, 改 .py 即生效)
    file(GLOB_RECURSE CWIND_FRONTEND_SOURCES "${CMAKE_SOURCE_DIR}/frontend/src/*.py")
    list(APPEND CWIND_FRONTEND_SOURCES "${CMAKE_SOURCE_DIR}/frontend/pyproject.toml")

    # 注册一个 typed-JSON 产物; FRONTEND_ARGS 之后可追加前端参数
    macro(cwind_typed_fixture _wind _json)
        if(_cwindf_cmd)
            add_custom_command(
                    OUTPUT "${_json}"
                    COMMAND ${CMAKE_COMMAND}
                            "-DCWINDF_COMMAND=${_cwindf_cmd}"
                            "-DCWINDF_PYTHONPATH=${_cwindf_pythonpath}"
                            -DSOURCE=${_wind}
                            -DTARGET=${_json}
                            "-DFRONTEND_ARGS=${ARGN}"
                            -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/gen_fixture.cmake
                    DEPENDS "${_wind}" ${CWIND_FRONTEND_SOURCES}
                    COMMENT "cwindf --typed-ast ${_wind} -> ${_json}"
                    VERBATIM
            )
            list(APPEND CWIND_FIXTURE_OUTPUTS "${_json}")
        endif()
    endmacro()

    # 编译器 C 单测 (test_cwmodule/cwtype/cwsymbol/cwllvm) 共用
    cwind_typed_fixture(
            "${CWIND_FIXTURES_DIR}/bindings_sample.wind"
            "${CWIND_FIXTURES_DIR}/bindings_sample.json")

    if(TARGET cwind_llvm)
        file(GLOB _fixture_winds "${CWIND_FIXTURES_DIR}/codegen_*.wind")
        foreach(_w IN LISTS _fixture_winds)
            get_filename_component(_n "${_w}" NAME_WE)
            if(_n STREQUAL "codegen_cfg")
                # todo-86/93: 同一 .wind 按 --target-os 生成四份
                foreach(_os IN ITEMS windows linux macos android)
                    cwind_typed_fixture(
                            "${_w}"
                            "${CWIND_FIXTURES_DIR}/codegen_cfg_${_os}.json"
                            "--target-os;${_os}")
                endforeach()
            else()
                cwind_typed_fixture("${_w}" "${CWIND_FIXTURES_DIR}/${_n}.json")
            endif()
        endforeach()
        # project_hello.json 是手写的静态清单 (todo-100), 非前端生成物,
        # 已通过 .gitignore 例外入库
        if(NOT EXISTS "${CWIND_FIXTURES_DIR}/project_hello.json")
            message(FATAL_ERROR
                    "fixtures/project_hello.json (static project manifest, tracked in git) is missing")
        endif()
        if(_cwindf_cmd)
            add_custom_target(cwind_fixtures ALL DEPENDS ${CWIND_FIXTURE_OUTPUTS})
        else()
            message(WARNING
                    "cwindf not found (set -DCWINDF_EXECUTABLE=<path> to override); "
                    "fixtures/*.json will not be generated and pipeline tests will fail")
        endif()
    endif()

    add_executable(test_memcenter test-c/test-rtl/test_memcenter.c)
    target_link_libraries(test_memcenter PRIVATE cwind_rt)
    add_test(NAME memcenter COMMAND test_memcenter)

    add_executable(test_cwgc test-c/test-rtl/test_cwgc.c)
    target_link_libraries(test_cwgc PRIVATE cwind_rt)
    add_test(NAME cwgc COMMAND test_cwgc)

    add_executable(test_cwgc_precise test-c/test-rtl/test_cwgc_precise.c)
    target_link_libraries(test_cwgc_precise PRIVATE cwind_rt)
    add_test(NAME cwgc_precise COMMAND test_cwgc_precise)

    add_executable(test_cwgc_stress test-c/test-rtl/test_cwgc_stress.c)
    target_link_libraries(test_cwgc_stress PRIVATE cwind_rt)
    add_test(NAME cwgc_stress COMMAND test_cwgc_stress)

    # MARK 增量期间的写屏障/分配屏障边界 (阶段 0-C)
    add_executable(test_cwgc_barrier test-c/test-rtl/test_cwgc_barrier.c)
    target_link_libraries(test_cwgc_barrier PRIVATE cwind_rt)
    add_test(NAME cwgc_barrier COMMAND test_cwgc_barrier)

    add_executable(test_cwobject test-c/test-rtl/test_cwobject.c)
    target_link_libraries(test_cwobject PRIVATE cwind_rt)
    add_test(NAME cwobject COMMAND test_cwobject)

    add_executable(test_stackframe test-c/test-rtl/test_stackframe.c)
    target_link_libraries(test_stackframe PRIVATE cwind_rt)
    add_test(NAME stackframe COMMAND test_stackframe)

    add_executable(test_cwcontainer test-c/test-rtl/test_cwcontainer.c)
    target_link_libraries(test_cwcontainer PRIVATE cwind_rt)
    add_test(NAME cwcontainer COMMAND test_cwcontainer)

    add_executable(test_cwabi test-c/test-rtl/test_cwabi.c)
    target_link_libraries(test_cwabi PRIVATE cwind_rt)
    add_test(NAME cwabi COMMAND test_cwabi)

    add_executable(test_cwmodule test-c/test-rtl/test_cwmodule.c)
    target_link_libraries(test_cwmodule PRIVATE cwind_compiler)
    add_test(NAME cwmodule COMMAND test_cwmodule)

    add_executable(test_cwbuiltin test-c/test-rtl/test_cwbuiltin.c)
    target_link_libraries(test_cwbuiltin PRIVATE cwind_rt)
    add_test(NAME cwbuiltin COMMAND test_cwbuiltin)

    add_executable(test_cwtype test-c/test-rtl/test_cwtype.c)
    target_link_libraries(test_cwtype PRIVATE cwind_compiler)
    add_test(NAME cwtype COMMAND test_cwtype)

    add_executable(test_cwsymbol test-c/test-rtl/test_cwsymbol.c)
    target_link_libraries(test_cwsymbol PRIVATE cwind_compiler)
    add_test(NAME cwsymbol COMMAND test_cwsymbol)

    add_executable(test_cwcffi test-c/test-rtl/test_cwcffi.c)
    target_link_libraries(test_cwcffi PRIVATE cwind_compiler)
    add_test(NAME cwcffi COMMAND test_cwcffi)

    if(TARGET cwind_llvm)
    add_test(
            NAME pipeline_hello
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/hello_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_hello.json
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_hello
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        # todo-100: project.json 输入解析出整程序工件后走既有管线
        add_test(
                NAME pipeline_projectjson
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/projectjson_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/project_hello.json
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_projectjson
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        # todo-74: 变量叹号取反 (Bool 逻辑非 + 整数按位取反)
        add_test(
                NAME pipeline_bnot
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/bnot_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_bnot.json
                        "-DEXPECTED_OUTPUT=-6;65535;55;-301;false"
                        -DEXPECTED_RC=42
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_bnot
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        # todo-17: 数值 as 转换 (截断/符号扩展/int<->float)
        add_test(
                NAME pipeline_as
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/as_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_as.json
                        "-DEXPECTED_OUTPUT=44;2;65535;3.5"
                        -DEXPECTED_RC=17
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_as
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_tail_mut
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/tail_mut_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_tail_mut.json
                        "-DEXPECTED_OUTPUT=21;tail mut"
                        -DEXPECTED_RC=17
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_tail_mut
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_containers
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/containers_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_containers.json
                        "-DEXPECTED_OUTPUT=1;1;2;3;4;4;9"
                        -DEXPECTED_RC=4
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_containers
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_maps
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/maps_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_maps.json
                        "-DEXPECTED_OUTPUT=2;1;2;3;3;9;true;false;0"
                        -DEXPECTED_RC=5
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_maps
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_mapiter
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/mapiter_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_mapiter.json
                        "-DEXPECTED_OUTPUT=c;3;c;3;b;2;b;2;a;1;a;1;6;(7, x);7;x;7;x;(1, 2);1;2;n;(1, 2);2;();0;6;true"
                        -DEXPECTED_RC=9
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_mapiter
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_strings
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/strings_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_strings.json
                        "-DEXPECTED_OUTPUT=hello wind;10;xy;true;ab"
                        -DEXPECTED_RC=7
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_strings
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_builtins
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/builtins_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_builtins.json
                        -DINPUT_FILE=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/builtins_input.txt
                        "-DEXPECTED_OUTPUT=Int;String;hello-line;10"
                        -DEXPECTED_RC=3
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_builtins
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_builtins_eof
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/builtins_eof_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_builtins.json
                        "-DEXPECTED_OUTPUT=Int;String;;0"
                        -DEXPECTED_RC=3
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_builtins_eof
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_vecmethods
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/vecmethods_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_vecmethods.json
                        "-DEXPECTED_OUTPUT=4;3;4;9;1;9;4"
                        -DEXPECTED_RC=6
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_vecmethods
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_structs
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/structs_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_structs.json
                        "-DEXPECTED_OUTPUT=3;4;9;13;1;2;2;4"
                        -DEXPECTED_RC=7
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_structs
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_generics
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/generics_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_generics.json
                        "-DEXPECTED_OUTPUT=3;7;hi;[1, 2];9"
                        -DEXPECTED_RC=7
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_generics
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_genmethods
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/genmethods_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_genmethods.json
                        "-DEXPECTED_OUTPUT=3;9;5;a"
                        -DEXPECTED_RC=7
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_genmethods
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_structfull
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/structfull_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_structfull.json
                        "-DEXPECTED_OUTPUT=7;11;13;0;wind;1;14;x;3;9;1;n;1;3"
                        -DEXPECTED_RC=13
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_structfull
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_which
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/which_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_which.json
                        "-DEXPECTED_OUTPUT=after;6;after;0;hello;log"
                        -DEXPECTED_RC=9
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_which
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        # todo-122: extra 块内 const 关联常量 (标量/String/常量算术/Self::)
        add_test(
                NAME pipeline_todo122
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/todo122_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_todo122.json
                        "-DEXPECTED_OUTPUT=99;34;34;cwind;cwind!;4000000007"
                        -DEXPECTED_RC=2
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_todo122
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_newset
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/newset_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_newset.json
                        "-DEXPECTED_OUTPUT=3;6;2;0;false;0;0"
                        -DEXPECTED_RC=6
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_newset
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_setmethods
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/setmethods_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_setmethods.json
                        "-DEXPECTED_OUTPUT=0;3;true;false;false;2;5;0"
                        -DEXPECTED_RC=8
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_setmethods
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_numeric
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/numeric_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_numeric.json
                        "-DEXPECTED_OUTPUT=832040;12586269025;true;true;1.5;true;0.2;100001;4000000000;18446744073709551615;128;Int"
                        -DEXPECTED_RC=7
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_numeric
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_opt3
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/opt3_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_numeric.json
                        -DOPT_LEVEL=3
                        "-DEXPECTED_OUTPUT=832040;12586269025;true;true;1.5;true;0.2;100001;4000000000;18446744073709551615;128;Int"
                        -DEXPECTED_RC=7
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_opt3
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_format
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/format_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_format.json
                        "-DEXPECTED_OUTPUT=v=42, b=true, name=wind;literal {x} and 1-2;plain;s=7"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_format
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_patterns
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/patterns_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_patterns.json
                        "-DEXPECTED_OUTPUT=zero;small;big;one;10;20;30;7;103;1;0;1;0;1;0;1;0;5;hi;42;-1;7;103;2;90;-1;-1;42;10;11;12;zero;other"
                        -DEXPECTED_RC=17
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_patterns
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_enums
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/enums_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_enums.json
                        "-DEXPECTED_OUTPUT=42;103;-1;42;-1;1;2;3;hi;none;7;0;9;-2;-1;9;102;-1;5;105"
                        -DEXPECTED_RC=11
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_enums
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_todo58
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/todo58_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_todo58.json
                        "-DEXPECTED_OUTPUT=-300;40000;39700;-600;20;9223372036854775806;-123;65535;65412;300;true;true;Int16;-123;4"
                        -DEXPECTED_RC=39
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_todo58
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_conversions
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/conversions_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_conversions.json
                        "-DEXPECTED_OUTPUT=42;7;-3;255;4000000000;18446744073709551615;3.5;2.25;200;42;-3;18446744073709551615;2.25;0;-12345"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_conversions
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_option
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/option_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_option.json
                        "-DEXPECTED_OUTPUT=42;hi;panic: Called `Option::unwrap()` on a `None` value"
                        -DEXPECTED_RC=1
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_option
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_heap
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/heap_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_heap.json
                        "-DEXPECTED_OUTPUT=0;k=9, v=nine;k=8, v=eight"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_heap
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_iterator
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/iterator_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_iterator.json
                        "-DEXPECTED_OUTPUT=1;2;3;done"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_iterator
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_traitconst
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/traitconst_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_traitconst.json
                        "-DEXPECTED_OUTPUT={;    \"CWind\": \"0\";}"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_traitconst
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_bugfixes
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/bugfixes_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_bugfixes.json
                        "-DEXPECTED_OUTPUT=UserString;UserString;7;7;{4, 3, 2, 1};3;3;v=42, b=true"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_bugfixes
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_fnptr
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/fnptr_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_fnptr.json
                        "-DEXPECTED_OUTPUT=8;15;9;24;6;7;14;30;22"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_fnptr
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_todo46
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/todo46_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_todo46.json
                        -DEXPECTED_RC=283
                        -DEXPECTED_OUTPUT=
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_todo46
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_cffi
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cffi_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cffi.json
                        "-DEXPECTED_OUTPUT=7;4;1024;3024;4000000000;9;42"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cffi
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                # #[link(path="./libcwindmath.a")] 相对本目录解析
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        add_test(
                NAME pipeline_cffi_string
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cffi_string_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cffi_string.json
                        "-DEXPECTED_OUTPUT=4;5;12;CWIND FFI;12;hello from C"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cffi_string
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        add_test(
                NAME pipeline_cffi_static
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cffi_static_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cffi_static.json
                        "-DEXPECTED_OUTPUT=4242;9;cwind-ffi;5;8;8"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cffi_static
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        add_test(
                NAME pipeline_cffi_callback
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cffi_callback_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cffi_callback.json
                        "-DEXPECTED_OUTPUT=42;5;42;25"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cffi_callback
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        add_test(
                NAME pipeline_cffi_agg
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cffi_agg_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cffi_agg.json
                        "-DEXPECTED_OUTPUT=12;41;1;7;4;11"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cffi_agg
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        add_test(
                NAME pipeline_cffi_linkname
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cffi_linkname_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cffi_linkname.json
                        "-DEXPECTED_OUTPUT=42;70;9;9"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cffi_linkname
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                # #[link(path="./libcwindmath.a")] 相对本目录解析
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        add_test(
                NAME pipeline_todo60
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/todo60_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_todo60.json
                        "-DEXPECTED_OUTPUT=100;10;10;99;3;300;1;250;1100"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_todo60
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        add_test(
                NAME pipeline_cffi_array
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cffi_array_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cffi_array.json
                        "-DEXPECTED_OUTPUT=8080;128;8081;8212;9130;9;42;7"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cffi_array
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                # #[link(path="./libcwindmath.a")] 相对本目录解析
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        add_test(
                NAME pipeline_cffi_smallagg
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cffi_smallagg_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cffi_smallagg.json
                        "-DEXPECTED_OUTPUT=42;16;17;12;15;12098700802;12115543554"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cffi_smallagg
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                # #[link(path="./libcwindmath.a")] 相对本目录解析
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        add_test(
                NAME pipeline_cffi_nested
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cffi_nested_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cffi_nested.json
                        "-DEXPECTED_OUTPUT=4040;16909060;2;16913118;4041;3;16913120;16916879"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cffi_nested
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                # #[link(path="./libcwindmath.a")] 相对本目录解析
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        add_test(
                NAME pipeline_cffi_decay
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cffi_decay_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cffi_decay.json
                        "-DEXPECTED_OUTPUT=36;132;66;100;10000200000300"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cffi_decay
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                # #[link(path="./libcwindmath.a")] 相对本目录解析
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        add_test(
                NAME pipeline_cffi_strptr
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cffi_strptr_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cffi_strptr.json
                        "-DEXPECTED_OUTPUT=74;2;80;true;true;1010;1;2;8080;127;1;16;63;11;40;51"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cffi_strptr
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        add_test(
                NAME pipeline_cffi_optstr
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cffi_optstr_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cffi_optstr.json
                        "-DEXPECTED_OUTPUT=cwind-env-value;15;0;is-none;true;false;true"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cffi_optstr
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        add_test(
                NAME pipeline_cffi_strptr_deref
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cffi_strptr_deref_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cffi_strptr_deref.json
                        "-DEXPECTED_OUTPUT=100;200;300;400;1;4;1010;true;100"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cffi_strptr_deref
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                # #[link(path="./libcwindmath.a")] 相对本目录解析
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        add_test(
                NAME pipeline_cffi_enumpay
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cffi_enumpay_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cffi_enumpay.json
                        "-DEXPECTED_OUTPUT=42;-1;56;-5;true;true;true;8"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cffi_enumpay
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        add_test(
                NAME pipeline_cffi_cbfnp
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cffi_cbfnp_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cffi_cbfnp.json
                        "-DEXPECTED_OUTPUT=4;6;16;42;2;5;100"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cffi_cbfnp
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                # #[link(path="./libcwindmath.a")] 相对本目录解析
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        add_test(
                NAME pipeline_cffi_relsrc
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cffi_relsrc.json
                        -DLIB_FILE=$<TARGET_FILE:cwindmath>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cffi_relsrc_pipeline.exe
                        -DSTAGE_DIR=${CMAKE_CURRENT_BINARY_DIR}/relsrc_stage
                        "-DEXPECTED_OUTPUT=3024;9"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_relsrc.cmake
        )
        set_tests_properties(
                pipeline_cffi_relsrc
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                # 故意不设 WORKING_DIRECTORY: 默认构建目录 ≠ 暂存的源目录,
                # 验证 relative = "source" 锚定的是源文件所在目录 (todo-63)
        )
        add_test(
                NAME pipeline_bug29
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/bug29_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_bug29.json
                        "-DEXPECTED_OUTPUT=4;-6;11;0.5;1"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_bug29
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                # #[link(path="./libcwindmath.a")] 相对本目录解析
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        add_test(
                NAME pipeline_bug33
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/bug33_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_bug33.json
                        "-DEXPECTED_OUTPUT=0;-1;3;7"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_bug33
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_bug53
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/bug53_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_bug53.json
                        "-DEXPECTED_OUTPUT=10;33;133;148"
                        -DEXPECTED_RC=148
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_bug53
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_bug54
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/bug54_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_bug54.json
                        "-DEXPECTED_OUTPUT=Thread Panicked:;Called `Option::unwrap()` on a `None` value"
                        -DEXPECTED_RC=1
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_bug54
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_bug55
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/bug55_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_bug55.json
                        "-DEXPECTED_OUTPUT=1037471908;1"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_bug55
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_bug34
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/bug34_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_bug34.json
                        "-DEXPECTED_OUTPUT=wind;cwind"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_bug34
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_bug35
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/bug35_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_bug35.json
                        "-DEXPECTED_OUTPUT=42;0;624"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_bug35
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_bug37
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/bug37_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_bug37.json
                        "-DEXPECTED_OUTPUT=0;clean"
                        -DEXPECTED_RC=7
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_bug37
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_mainargs
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/mainargs_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_mainargs.json
                        "-DEXE_ARGS=alpha;beta"
                        "-DEXPECTED_OUTPUT=2;alpha;beta"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_mainargs
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        # todo-86/93: #[cfg] 在前端按 --target-os 解析, fixture JSON 由
        # cwindf --typed-ast --target-os <os> 预生成, 同一 .wind 三份目标各一份。
        add_test(
                NAME pipeline_cfg_windows
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cfg_windows_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cfg_windows.json
                        "-DEXPECTED_OUTPUT=A-desktop;B-win;windows;5"
                        -DEXPECTED_RC=9
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cfg_windows
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_cfg_linux
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cfg_linux_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cfg_linux.json
                        "-DEXPECTED_OUTPUT=A-unix;B-nonwin;linux;5"
                        -DEXPECTED_RC=9
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cfg_linux
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_cfg_macos
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cfg_macos_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cfg_macos.json
                        "-DEXPECTED_OUTPUT=A-desktop;B-nonwin;macos;4"
                        -DEXPECTED_RC=9
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cfg_macos
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_test(
                NAME pipeline_cfg_android
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/cfg_android_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_cfg_android.json
                        "-DEXPECTED_OUTPUT=A-unix;B-nonwin;android;5"
                        -DEXPECTED_RC=9
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_cfg_android
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        # todo-85: 十六进制字面量 (含 u64 全范围) 端到端
        add_test(
                NAME pipeline_hex
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/hex_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_hex.json
                        "-DEXPECTED_OUTPUT=255;3735928559;18446744073709551615;-78187493530;192;true;low;pattern-ok"
                        -DEXPECTED_RC=42
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_hex
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        # todo-87: extern 变参 (...) 调 libc printf, 额外实参按 C 默认提升
        add_test(
                NAME pipeline_todo87
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/todo87_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_todo87.json
                        "-DEXPECTED_OUTPUT=int=42 u=7 str=wind f=3.50 e=3;no-arg-only-fmt ok"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_todo87
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                # #[link(path="./libcwindmath.a")] 相对本目录解析
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        # todo-108: 枚举指针 (*mut Opaque) 作为不透明句柄穿越 FFI
        add_test(
                NAME pipeline_todo108
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/todo108_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_todo108.json
                        "-DEXPECTED_OUTPUT=true;true"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_todo108
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
                # #[link(path="./libcwindmath.a")] 相对本目录解析
                WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
        )
        # todo-109/110/111: std::ctypedef + time.h 绑定端到端
        # (mktime 归一化写回 wday/yday 与时区无关; difftime 同值恒 0)
        add_test(
                NAME pipeline_ctime
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/ctime_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_ctime.json
                        "-DEXPECTED_OUTPUT=5;9;true;0"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_ctime
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        # bug-46: 函数调用允许 ``&mut arg`` —— 可变借用形参写穿回调用者
        add_test(
                NAME pipeline_bug46
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/bug46_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_bug46.json
                        "-DEXPECTED_OUTPUT=20;20"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_bug46
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        # bug-47/48: 容器字面量绑定注解元素类型, 容器内别名先展开
        add_test(
                NAME pipeline_bug47
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/bug47_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_bug47.json
                        "-DEXPECTED_OUTPUT=3;6.875;6.875;2;4;5;20.25"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_bug47
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        # bug-49: ``extra Cell<T>`` 泛型参数传播到 extra 内函数
        add_test(
                NAME pipeline_bug49
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/bug49_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_bug49.json
                        "-DEXPECTED_OUTPUT=2;7;seven"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_bug49
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        # todo-147: ``extra Cell<Int>`` 具体类型特化 (泛型 extra 与特化共存)
        add_test(
                NAME pipeline_todo147
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/todo147_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_todo147.json
                        "-DEXPECTED_OUTPUT=1;2;2;hi"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_todo147
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        # todo-145: &mut 引用解引用与标量写穿 (形参/let 绑定/复合写穿/
        # 共享引用读/引用中继)
        add_test(
                NAME pipeline_todo145
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/todo145_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_todo145.json
                        "-DEXPECTED_OUTPUT=2;6;6;10;10;11"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_todo145
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        # todo-35: GC e2e — 分配压力 + builtins::gc_collect 强制回收 +
        # 长存活数据完好; 调低 step 阈值放大增量 GC 触发频率
        add_test(
                NAME pipeline_gc
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/gc_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_gc.json
                        # fixture 打印序: 4 个观测布尔 (gc_alloc_bytes>0 /
                        # live 单调 / pause>0 / alloc 单调) 在前, 完好断言在后
                        "-DEXPECTED_OUTPUT=true;true;true;true;50;key17;20;137;70"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_gc
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH};CWGC_STEP_BYTES=4096"
        )
        # todo-35/50: GC 复杂边界 e2e — 槽替换 (写屏障) / 递归+每层分配 /
        # Set churn / 结构体容器字段穿 GC / 启停窗口 / mut 重赋值
        add_test(
                NAME pipeline_gc_edge
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/gc_edge_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_gc_edge.json
                        "-DEXPECTED_OUTPUT=24;0;2300;64;200;9;10;77;true;true;true;1;22"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_gc_edge
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH};CWGC_STEP_BYTES=4096"
        )
        # todo-151: 字段/下标赋值右侧 T::new() 携带容器泛型实参 tag
        # (与变量重赋值同纪律); 输出锚定字段 Vector/Map 赋值 +
        # 嵌套 Vector/Map 下标赋值 + GC 完好
        add_test(
                NAME pipeline_todo151
                COMMAND ${CMAKE_COMMAND}
                        -DCWINDC=$<TARGET_FILE:cwindc>
                        -DOUT_EXE=${CMAKE_CURRENT_BINARY_DIR}/todo151_pipeline.exe
                        -DIN_JSON=${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures/codegen_todo151.json
                        "-DEXPECTED_OUTPUT=42;70;111;33;1;0;1"
                        -DEXPECTED_RC=0
                        -P ${CMAKE_SOURCE_DIR}/test-c/test-rtl/run_pipeline_hello.cmake
        )
        set_tests_properties(
                pipeline_todo151
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH};CWGC_STEP_BYTES=4096"
        )
        add_executable(test_cwllvm test-c/test-rtl/test_cwllvm.c)
        target_link_libraries(test_cwllvm PRIVATE cwind_llvm)
        add_test(NAME cwllvm COMMAND test_cwllvm)
        set_tests_properties(
                cwllvm
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
        add_executable(test_cwcodegen test-c/test-rtl/test_cwcodegen.c)
        target_link_libraries(test_cwcodegen PRIVATE cwind_llvm)
        target_compile_definitions(
                test_cwcodegen
                PRIVATE
                CWIND_FIXTURE_DIR="${CMAKE_SOURCE_DIR}/test-c/test-rtl/fixtures"
        )
        add_test(NAME cwcodegen COMMAND test_cwcodegen)
        set_tests_properties(
                cwcodegen
                PROPERTIES
                ENVIRONMENT "PATH=${CWIND_LLVM_DIR}/bin;$ENV{PATH}"
        )
    endif()
endif()
