cmake_minimum_required(VERSION 3.16)

project(
        cwind
        VERSION 0.0.1
        LANGUAGES C
)
set(PROJECT_VERSION_FULL "${PROJECT_VERSION}+Canary")


set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)


file(GLOB STL_ESS_HEADERS    "rt-src/include/stl/ess/*.h")
file(GLOB STL_NORMAL_HEADERS "rt-src/include/stl/normal/*.h")
file(GLOB STL_JSON_HEADERS   "rt-src/include/stl/json/*.h")
file(GLOB MEMORY_HEADERS     "rt-src/include/memory/*.h")
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
        ${MEMORY_HEADERS}
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

add_library(cwind_compiler STATIC
        compiler/cwmodule.c
        compiler/cwtype.c
        compiler/cwlayout.c
        compiler/cwsymbol.c
)
target_include_directories(cwind_compiler PUBLIC compiler rt-src/include)
target_compile_features(cwind_compiler PUBLIC c_std_11)

set(CWIND_LLVM_DIR "${CMAKE_SOURCE_DIR}/.LLVM18" CACHE PATH "Path to LLVM18")
if(EXISTS "${CWIND_LLVM_DIR}/include/llvm-c/Core.h")
    add_library(cwind_llvm STATIC compiler/cwllvm.c compiler/cwcodegen.c)
    target_include_directories(
            cwind_llvm
            PUBLIC
            compiler
            rt-src/include
            "${CWIND_LLVM_DIR}/include"
    )
    target_link_libraries(
            cwind_llvm
            PUBLIC
            cwind_compiler
            "${CWIND_LLVM_DIR}/lib/LLVM-C.lib"
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
    target_compile_definitions(
            cwindc
            PRIVATE
            CWINDC_CLANG_DEFAULT="${CWIND_LLVM_DIR}/bin/clang.exe"
            CWINDC_RT_DIR="${CMAKE_SOURCE_DIR}/rt-src/rt"
            CWINDC_GCC="E:/MSYS2/mingw64/bin/gcc.exe"
    )
endif()


option(CWIND_BUILD_TESTS "Build the standalone rt test executables" OFF)
if(CWIND_BUILD_TESTS)
    enable_testing()

    add_executable(test_memcenter test-c/test-rtl/test_memcenter.c)
    target_link_libraries(test_memcenter PRIVATE cwind_rt)
    add_test(NAME memcenter COMMAND test_memcenter)

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
                        "-DEXPECTED_OUTPUT=7;11;0;wind;1;x;3;9;1;n;1;3"
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
