#include <atomic>

#include "platform.h"

#if FDV_X64
#if FDV_MSVC
#include <intrin.h>
#include <immintrin.h>
#else
#include <cpuid.h>
#endif
#endif

namespace fdv {

namespace {

#if FDV_X64
void cpuid(uint32_t leaf, uint32_t sub, uint32_t r[4]) {
#if FDV_MSVC
    int v[4];
    __cpuidex(v, int(leaf), int(sub));
    for (int i = 0; i < 4; ++i) r[i] = uint32_t(v[i]);
#else
    __cpuid_count(leaf, sub, r[0], r[1], r[2], r[3]);
#endif
}

uint64_t xgetbv0() {
#if FDV_MSVC
    return _xgetbv(0);
#else
    uint32_t lo, hi;
    __asm__ volatile("xgetbv" : "=a"(lo), "=d"(hi) : "c"(0));
    return (uint64_t(hi) << 32) | lo;
#endif
}
#endif

CpuFeatures detect() {
    CpuFeatures f;
#if FDV_X64
    uint32_t r[4];
    cpuid(0, 0, r);
    uint32_t max_leaf = r[0];
    cpuid(1, 0, r);
    uint32_t ecx1 = r[2], edx1 = r[3];
    f.sse2 = (edx1 >> 26) & 1;
    f.ssse3 = (ecx1 >> 9) & 1;
    f.sse41 = (ecx1 >> 19) & 1;
    bool osxsave = (ecx1 >> 27) & 1;
    bool avx = (ecx1 >> 28) & 1;
    bool ymm_ok = false;
    if (osxsave && avx) ymm_ok = (xgetbv0() & 6) == 6;
    if (max_leaf >= 7) {
        cpuid(7, 0, r);
        uint32_t ebx7 = r[1];
        f.avx2 = ymm_ok && ((ebx7 >> 5) & 1);
        f.shani = ((ebx7 >> 29) & 1) && f.ssse3 && f.sse41;
    }
#endif
#if defined(FDV_EMULATE_X86)
    f.sse2 = f.ssse3 = f.sse41 = f.avx2 = f.shani = true;  // 模拟器实现了这些指令
#endif
#if FDV_HAVE_NEON
    f.neon = true;
#endif
#if FDV_HAVE_ARM_SHA2
    f.armsha2 = true;
#endif
    return f;
}

std::atomic<bool> g_force_portable{false};

}  // namespace

const CpuFeatures& cpu() {
    static const CpuFeatures f = detect();
    return f;
}

void set_force_portable(bool on) { g_force_portable.store(on); }
bool force_portable() { return g_force_portable.load(std::memory_order_relaxed); }

}  // namespace fdv
