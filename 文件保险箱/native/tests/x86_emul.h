// 测试专用：用纯 C++ 按 Intel 手册语义模拟本项目用到的 SSE2/SSSE3/SSE4.1/AVX2/SHA-NI 内建函数，
// 这样在 Apple 芯片上也能验证 x86 SIMD 代码（编译时定义 FDV_EMULATE_X86）。
// 只实现了 chacha20.cpp / sha256.cpp 用到的那些。
#pragma once
#include <cstdint>
#include <cstring>

struct __m128i {
    uint32_t d[4];
};
struct __m256i {
    __m128i l[2];
};

namespace fdv_emul {
inline uint32_t rotr(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }
inline uint8_t byte(const __m128i& v, int i) {
    uint8_t b[16];
    std::memcpy(b, v.d, 16);
    return b[i];
}
inline __m128i from_bytes(const uint8_t b[16]) {
    __m128i r;
    std::memcpy(r.d, b, 16);
    return r;
}
}  // namespace fdv_emul

// ------------------------------------------------------------------ 128 位
inline __m128i _mm_loadu_si128(const __m128i* p) {
    __m128i r;
    std::memcpy(r.d, p, 16);
    return r;
}
inline __m128i _mm_load_si128(const __m128i* p) { return _mm_loadu_si128(p); }
inline void _mm_storeu_si128(__m128i* p, __m128i v) { std::memcpy(p, v.d, 16); }
inline __m128i _mm_setr_epi32(int a, int b, int c, int d) { return __m128i{{uint32_t(a), uint32_t(b), uint32_t(c), uint32_t(d)}}; }
inline __m128i _mm_set1_epi32(int a) { return _mm_setr_epi32(a, a, a, a); }
inline __m128i _mm_set_epi64x(long long hi, long long lo) {
    __m128i r;
    uint64_t l = uint64_t(lo), h = uint64_t(hi);
    std::memcpy(&r.d[0], &l, 8);
    std::memcpy(&r.d[2], &h, 8);
    return r;
}
inline __m128i _mm_add_epi32(__m128i a, __m128i b) {
    for (int i = 0; i < 4; ++i) a.d[i] += b.d[i];
    return a;
}
inline __m128i _mm_xor_si128(__m128i a, __m128i b) {
    for (int i = 0; i < 4; ++i) a.d[i] ^= b.d[i];
    return a;
}
inline __m128i _mm_or_si128(__m128i a, __m128i b) {
    for (int i = 0; i < 4; ++i) a.d[i] |= b.d[i];
    return a;
}
inline __m128i _mm_slli_epi32(__m128i a, int n) {
    for (int i = 0; i < 4; ++i) a.d[i] = n > 31 ? 0 : a.d[i] << n;
    return a;
}
inline __m128i _mm_srli_epi32(__m128i a, int n) {
    for (int i = 0; i < 4; ++i) a.d[i] = n > 31 ? 0 : a.d[i] >> n;
    return a;
}
inline __m128i _mm_unpacklo_epi32(__m128i a, __m128i b) { return __m128i{{a.d[0], b.d[0], a.d[1], b.d[1]}}; }
inline __m128i _mm_unpackhi_epi32(__m128i a, __m128i b) { return __m128i{{a.d[2], b.d[2], a.d[3], b.d[3]}}; }
inline __m128i _mm_unpacklo_epi64(__m128i a, __m128i b) { return __m128i{{a.d[0], a.d[1], b.d[0], b.d[1]}}; }
inline __m128i _mm_unpackhi_epi64(__m128i a, __m128i b) { return __m128i{{a.d[2], a.d[3], b.d[2], b.d[3]}}; }
inline __m128i _mm_shuffle_epi32(__m128i a, int imm) {
    __m128i r;
    for (int i = 0; i < 4; ++i) r.d[i] = a.d[(imm >> (2 * i)) & 3];
    return r;
}
inline __m128i _mm_shuffle_epi8(__m128i a, __m128i m) {
    uint8_t out[16];
    for (int i = 0; i < 16; ++i) {
        uint8_t k = fdv_emul::byte(m, i);
        out[i] = (k & 0x80) ? 0 : fdv_emul::byte(a, k & 15);
    }
    return fdv_emul::from_bytes(out);
}
// (a:b) 拼成 32 字节（a 在高位），右移 imm 字节，取低 16 字节
inline __m128i _mm_alignr_epi8(__m128i a, __m128i b, int imm) {
    uint8_t cat[32], out[16];
    std::memcpy(cat, b.d, 16);
    std::memcpy(cat + 16, a.d, 16);
    for (int i = 0; i < 16; ++i) out[i] = (imm + i < 32) ? cat[imm + i] : 0;
    return fdv_emul::from_bytes(out);
}
inline __m128i _mm_blend_epi16(__m128i a, __m128i b, int imm) {
    uint16_t x[8], y[8];
    std::memcpy(x, a.d, 16);
    std::memcpy(y, b.d, 16);
    for (int i = 0; i < 8; ++i)
        if ((imm >> i) & 1) x[i] = y[i];
    __m128i r;
    std::memcpy(r.d, x, 16);
    return r;
}
inline __m128i _mm_sha256msg1_epu32(__m128i a, __m128i b) {
    auto s0 = [](uint32_t x) { return fdv_emul::rotr(x, 7) ^ fdv_emul::rotr(x, 18) ^ (x >> 3); };
    uint32_t w0 = a.d[0], w1 = a.d[1], w2 = a.d[2], w3 = a.d[3], w4 = b.d[0];
    return __m128i{{w0 + s0(w1), w1 + s0(w2), w2 + s0(w3), w3 + s0(w4)}};
}
inline __m128i _mm_sha256msg2_epu32(__m128i a, __m128i b) {
    auto s1 = [](uint32_t x) { return fdv_emul::rotr(x, 17) ^ fdv_emul::rotr(x, 19) ^ (x >> 10); };
    uint32_t w14 = b.d[2], w15 = b.d[3];
    uint32_t w16 = a.d[0] + s1(w14);
    uint32_t w17 = a.d[1] + s1(w15);
    uint32_t w18 = a.d[2] + s1(w16);
    uint32_t w19 = a.d[3] + s1(w17);
    return __m128i{{w16, w17, w18, w19}};
}
// a = {C,D,G,H}（高到低），b = {A,B,E,F}，k 低两个字是 WK0、WK1；返回新的 {A,B,E,F}
inline __m128i _mm_sha256rnds2_epu32(__m128i a, __m128i b, __m128i k) {
    using fdv_emul::rotr;
    uint32_t A = b.d[3], B = b.d[2], E = b.d[1], F = b.d[0];
    uint32_t C = a.d[3], D = a.d[2], G = a.d[1], H = a.d[0];
    for (int i = 0; i < 2; ++i) {
        uint32_t wk = k.d[i];
        uint32_t ch = (E & F) ^ (~E & G);
        uint32_t maj = (A & B) ^ (A & C) ^ (B & C);
        uint32_t S0 = rotr(A, 2) ^ rotr(A, 13) ^ rotr(A, 22);
        uint32_t S1 = rotr(E, 6) ^ rotr(E, 11) ^ rotr(E, 25);
        uint32_t t1 = H + S1 + ch + wk;
        uint32_t t2 = S0 + maj;
        H = G;
        G = F;
        F = E;
        E = D + t1;
        D = C;
        C = B;
        B = A;
        A = t1 + t2;
    }
    return __m128i{{F, E, B, A}};
}

// ------------------------------------------------------------------ 256 位（两个 128 位通道）
inline __m256i _mm256_loadu_si256(const __m256i* p) {
    __m256i r;
    std::memcpy(&r, p, 32);
    return r;
}
inline void _mm256_storeu_si256(__m256i* p, __m256i v) { std::memcpy(p, &v, 32); }
inline __m256i _mm256_setr_epi32(int a, int b, int c, int d, int e, int f, int g, int h) {
    return __m256i{{_mm_setr_epi32(a, b, c, d), _mm_setr_epi32(e, f, g, h)}};
}
inline __m256i _mm256_set1_epi32(int a) { return __m256i{{_mm_set1_epi32(a), _mm_set1_epi32(a)}}; }
inline __m256i _mm256_setr_epi8(char b0, char b1, char b2, char b3, char b4, char b5, char b6, char b7, char b8,
                                char b9, char b10, char b11, char b12, char b13, char b14, char b15, char b16,
                                char b17, char b18, char b19, char b20, char b21, char b22, char b23, char b24,
                                char b25, char b26, char b27, char b28, char b29, char b30, char b31) {
    const char b[32] = {b0,  b1,  b2,  b3,  b4,  b5,  b6,  b7,  b8,  b9,  b10, b11, b12, b13, b14, b15,
                        b16, b17, b18, b19, b20, b21, b22, b23, b24, b25, b26, b27, b28, b29, b30, b31};
    __m256i r;
    std::memcpy(&r, b, 32);
    return r;
}
#define FDV_EMUL_LANES2(name, f)                                     \
    inline __m256i name(__m256i a, __m256i b) {                      \
        return __m256i{{f(a.l[0], b.l[0]), f(a.l[1], b.l[1])}};      \
    }
FDV_EMUL_LANES2(_mm256_add_epi32, _mm_add_epi32)
FDV_EMUL_LANES2(_mm256_xor_si256, _mm_xor_si128)
FDV_EMUL_LANES2(_mm256_or_si256, _mm_or_si128)
FDV_EMUL_LANES2(_mm256_shuffle_epi8, _mm_shuffle_epi8)
FDV_EMUL_LANES2(_mm256_unpacklo_epi32, _mm_unpacklo_epi32)
FDV_EMUL_LANES2(_mm256_unpackhi_epi32, _mm_unpackhi_epi32)
FDV_EMUL_LANES2(_mm256_unpacklo_epi64, _mm_unpacklo_epi64)
FDV_EMUL_LANES2(_mm256_unpackhi_epi64, _mm_unpackhi_epi64)
#undef FDV_EMUL_LANES2
inline __m256i _mm256_slli_epi32(__m256i a, int n) { return __m256i{{_mm_slli_epi32(a.l[0], n), _mm_slli_epi32(a.l[1], n)}}; }
inline __m256i _mm256_srli_epi32(__m256i a, int n) { return __m256i{{_mm_srli_epi32(a.l[0], n), _mm_srli_epi32(a.l[1], n)}}; }
inline __m256i _mm256_permute2x128_si256(__m256i a, __m256i b, int imm) {
    const __m128i src[4] = {a.l[0], a.l[1], b.l[0], b.l[1]};
    const __m128i zero = {{0, 0, 0, 0}};
    __m256i r;
    r.l[0] = (imm & 0x08) ? zero : src[imm & 3];
    r.l[1] = (imm & 0x80) ? zero : src[(imm >> 4) & 3];
    return r;
}
inline void _mm256_zeroupper() {}
