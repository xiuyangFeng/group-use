# 文件保险箱 C++ 加解密引擎
#   cmake -S native -B native/build -DCMAKE_BUILD_TYPE=Release && cmake --build native/build --config Release
# 一般用 scripts/build_native.py（会把库复制到 core/_native/）。
cmake_minimum_required(VERSION 3.15)
cmake_policy(SET CMP0091 NEW)  # MSVC 运行库选择
project(fdvnative VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)
set(CMAKE_VISIBILITY_INLINES_HIDDEN ON)
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release)
endif()
# Windows：静态链接 C++ 运行库，DLL 只依赖 kernel32，不需要同事装 VC++ 运行库
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

find_package(Threads REQUIRED)

set(FDV_SOURCES
  src/aead.cpp
  src/api.cpp
  src/chacha20.cpp
  src/cpu.cpp
  src/engine.cpp
  src/fileio.cpp
  src/poly1305.cpp
  src/sha256.cpp
)

function(fdv_flags target)
  target_include_directories(${target} PRIVATE include src)
  target_compile_definitions(${target} PRIVATE FDV_VERSION_STR="${PROJECT_VERSION}")
  if(MSVC)
    target_compile_options(${target} PRIVATE /O2 /W4 /utf-8 /EHsc /wd4127)
    target_compile_definitions(${target} PRIVATE _CRT_SECURE_NO_WARNINGS)
  else()
    target_compile_options(${target} PRIVATE -O3 -Wall -Wextra -fno-strict-aliasing)
  endif()
  target_link_libraries(${target} PRIVATE Threads::Threads)
endfunction()

add_library(fdvnative SHARED ${FDV_SOURCES})
fdv_flags(fdvnative)
if(APPLE)
  set_target_properties(fdvnative PROPERTIES INSTALL_NAME_DIR "@rpath")
endif()
if(MINGW)
  target_link_options(fdvnative PRIVATE -static -static-libgcc -static-libstdc++)
endif()

# 自检 + 性能测试小工具（不打进安装包）
add_executable(fdv_bench tools/fdv_bench.cpp ${FDV_SOURCES})
fdv_flags(fdv_bench)
