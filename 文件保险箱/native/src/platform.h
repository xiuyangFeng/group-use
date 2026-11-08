// 平台 / 编译器 / CPU 特性的统一封装。
#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

#if defined(__x86_64__) || defined(_M_X64)
#define FDV_X64 1
#else
#define FDV_X64 0
#endif

// FDV_EMULATE_X86：测试专用。用纯 C++ 模拟 SSE/AVX2/SHA-NI 指令（tests/x86_emul.h），
// 在没有这些指令的机器上（例如 Apple 芯片）验证 x86 SIMD 代码路径。
#if FDV_X64 || defined(FDV_EMULATE_X86)
#define FDV_X86_CODE 1
#else
#define FDV_X86_CODE 0
#endif

#if defined(__aarch64__) || defined(_M_ARM64)
#define FDV_ARM64 1
#else
#define FDV_ARM64 0
#endif

// NEON / ARMv8 SHA2 只在 clang/gcc 的 aarch64 上启用（macOS Apple 芯片）；
// MSVC ARM64 不是交付目标，走可移植实现。
#if defined(__aarch64__) && defined(__ARM_NEON) && !defined(FDV_EMULATE_X86)
#define FDV_HAVE_NEON 1
#else
#define FDV_HAVE_NEON 0
#endif

#if defined(__aarch64__) && defined(__ARM_FEATURE_SHA2) && !defined(FDV_EMULATE_X86)
#define FDV_HAVE_ARM_SHA2 1
#else
#define FDV_HAVE_ARM_SHA2 0
#endif

#if defined(_MSC_VER) && !defined(__clang__)
#define FDV_MSVC 1
#define FDV_TARGET(x)
#define FDV_INLINE __forceinline
#else
#define FDV_MSVC 0
#if defined(FDV_EMULATE_X86)
#define FDV_TARGET(x)
#else
#define FDV_TARGET(x) __attribute__((target(x)))
#endif
#define FDV_INLINE inline __attribute__((always_inline))
#endif

#if FDV_X86_CODE
#if defined(FDV_EMULATE_X86)
#include "../tests/x86_emul.h"
#else
#include <immintrin.h>
#endif
#endif

namespace fdv {

// 全部交付平台（x86-64、arm64）都是小端
inline uint32_t load32_le(const uint8_t* p) {
    uint32_t v;
    std::memcpy(&v, p, 4);
    return v;
}
inline uint64_t load64_le(const uint8_t* p) {
    uint64_t v;
    std::memcpy(&v, p, 8);
    return v;
}
inline void store32_le(uint8_t* p, uint32_t v) { std::memcpy(p, &v, 4); }
inline void store64_le(uint8_t* p, uint64_t v) { std::memcpy(p, &v, 8); }

inline uint32_t load32_be(const uint8_t* p) {
    return (uint32_t(p[0]) << 24) | (uint32_t(p[1]) << 16) | (uint32_t(p[2]) << 8) | uint32_t(p[3]);
}
inline void store32_be(uint8_t* p, uint32_t v) {
    p[0] = uint8_t(v >> 24);
    p[1] = uint8_t(v >> 16);
    p[2] = uint8_t(v >> 8);
    p[3] = uint8_t(v);
}
inline void store64_be(uint8_t* p, uint64_t v) {
    for (int i = 7; i >= 0; --i) {
        p[i] = uint8_t(v);
        v >>= 8;
    }
}

struct CpuFeatures {
    bool sse2 = false;
    bool ssse3 = false;
    bool sse41 = false;
    bool avx2 = false;
    bool shani = false;
    bool neon = false;
    bool armsha2 = false;
};

const CpuFeatures& cpu();

// 测试用：强制只用可移植实现（用来交叉比对 SIMD 结果）
void set_force_portable(bool on);
bool force_portable();

}  // namespace fdv
