cmake_minimum_required(VERSION 4.3)

project(
        cwind
        VERSION 0.0.1
        LANGUAGES C
)
set(PROJECT_VERSION_FULL "${PROJECT_VERSION}+Canary")


set(CMAKE_C_STANDARD 11)


file(GLOB MEMORY_HEADERS "rt-src/include/memory/*.h")
file(GLOB OBJECT_HEADERS "rt-src/include/object/*.h")
file(GLOB RT_HEADERS     "rt-src/include/rt/*.h")
file(GLOB STL_ESS_HEADERS "rt-src/include/stl/ess/*.h")

add_library(
        cwind_stl
        INTERFACE
        ${MEMORY_HEADERS}
        ${OBJECT_HEADERS}
        ${RT_HEADERS}
        ${STL_ESS_HEADERS}
        rt-src/include/object/cwind_obj_forward.h
)


target_sources(cwind_stl INTERFACE
        ${MEMORY_HEADERS}
        ${OBJECT_HEADERS}
        ${RT_HEADERS}
        ${STL_ESS_HEADERS}
)
