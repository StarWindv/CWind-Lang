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

add_executable(cwindc compiler/cwindc.c)
target_link_libraries(cwindc PRIVATE cwind_compiler)


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
endif()
