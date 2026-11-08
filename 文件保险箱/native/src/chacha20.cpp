// ChaCha20（RFC 8439，96 位 nonce + 32 位块计数器）。
// 实现：可移植标量；x86-64 SSE2 4 路 / AVX2 8 路；arm64 NEON 4 路。运行时选择。
#include "chacha20.h"

#include <atomic>

#include "platform.h"

#if FDV_HAVE_NEON
#include <arm_neon.h>
#endif

namespace fdv {

namespace {

std::atomic<int> g_disabled{0};  // 自检失败时禁用的实现（位掩码：1=4 路，2=8 路）

inline uint32_t rotl32(uint32_t v, int n) { return (v << n) | (v >> (32 - n)); }

#define FDV_QR(a, b, c, d)          \
    a += b; d ^= a; d = rotl32(d, 16); \
    c += d; b ^= c; b = rotl32(b, 12); \
    a += b; d ^= a; d = rotl32(d, 8);  \
    c += d; b ^= c; b = rotl32(b, 7);

void block_scalar(const uint32_t s[16], uint8_t out[64]) {
    uint32_t x0 = s[0], x1 = s[1], x2 = s[2], x3 = s[3], x4 = s[4], x5 = s[5], x6 = s[6], x7 = s[7];
    uint32_t x8 = s[8], x9 = s[9], x10 = s[10], x11 = s[11], x12 = s[12], x13 = s[13], x14 = s[14], x15 = s[15];
    for (int i = 0; i < 10; ++i) {
        FDV_QR(x0, x4, x8, x12)
        FDV_QR(x1, x5, x9, x13)
        FDV_QR(x2, x6, x10, x14)
        FDV_QR(x3, x7, x11, x15)
        FDV_QR(x0, x5, x10, x15)
        FDV_QR(x1, x6, x11, x12)
        FDV_QR(x2, x7, x8, x13)
        FDV_QR(x3, x4, x9, x14)
    }
    store32_le(out + 0, x0 + s[0]);
    store32_le(out + 4, x1 + s[1]);
    store32_le(out + 8, x2 + s[2]);
    store32_le(out + 12, x3 + s[3]);
    store32_le(out + 16, x4 + s[4]);
    store32_le(out + 20, x5 + s[5]);
    store32_le(out + 24, x6 + s[6]);
    store32_le(out + 28, x7 + s[7]);
    store32_le(out + 32, x8 + s[8]);
    store32_le(out + 36, x9 + s[9]);
    store32_le(out + 40, x10 + s[10]);
    store32_le(out + 44, x11 + s[11]);
    store32_le(out + 48, x12 + s[12]);
    store32_le(out + 52, x13 + s[13]);
    store32_le(out + 56, x14 + s[14]);
    store32_le(out + 60, x15 + s[15]);
}
#undef FDV_QR

// 处理 n 个完整块，s[12] 随之递增
void xor_blocks_scalar(uint32_t s[16], uint8_t* out, const uint8_t* in, size_t n) {
    uint8_t ks[64];
    for (size_t b = 0; b < n; ++b) {
        block_scalar(s, ks);
        for (int i = 0; i < 64; i += 8) {
            uint64_t v = load64_le(in + i) ^ load64_le(ks + i);
            store64_le(out + i, v);
        }
        s[12] += 1;
        in += 64;
        out += 64;
    }
}

// ------------------------------------------------------------------ NEON 4 路
#if FDV_HAVE_NEON

FDV_INLINE uint32x4_t n_rotl16(uint32x4_t v) {
    return vreinterpretq_u32_u16(vrev32q_u16(vreinterpretq_u16_u32(v)));
}
#define N_ROTL(v, n) vsriq_n_u32(vshlq_n_u32(v, n), v, 32 - n)

#define N_QR(a, b, c, d)                                                          \
    x[a] = vaddq_u32(x[a], x[b]); x[d] = veorq_u32(x[d], x[a]); x[d] = n_rotl16(x[d]); \
    x[c] = vaddq_u32(x[c], x[d]); x[b] = veorq_u32(x[b], x[c]); x[b] = N_ROTL(x[b], 12); \
    x[a] = vaddq_u32(x[a], x[b]); x[d] = veorq_u32(x[d], x[a]);                      \
    x[d] = vreinterpretq_u32_u8(vqtbl1q_u8(vreinterpretq_u8_u32(x[d]), rot8));        \
    x[c] = vaddq_u32(x[c], x[d]); x[b] = veorq_u32(x[b], x[c]); x[b] = N_ROTL(x[b], 7);

size_t xor_blocks_neon(uint32_t s[16], uint8_t* out, const uint8_t* in, size_t n) {
    static const uint8_t ROT8[16] = {3, 0, 1, 2, 7, 4, 5, 6, 11, 8, 9, 10, 15, 12, 13, 14};
    static const uint32_t INC[4] = {0, 1, 2, 3};
    const uint8x16_t rot8 = vld1q_u8(ROT8);
    const uint32x4_t inc = vld1q_u32(INC);
    size_t done = 0;
    while (n - done >= 4) {
        uint32x4_t x[16], o[16];
        for (int i = 0; i < 16; ++i) o[i] = vdupq_n_u32(s[i]);
        o[12] = vaddq_u32(o[12], inc);
        for (int i = 0; i < 16; ++i) x[i] = o[i];
        for (int r = 0; r < 10; ++r) {
            N_QR(0, 4, 8, 12)
            N_QR(1, 5, 9, 13)
            N_QR(2, 6, 10, 14)
            N_QR(3, 7, 11, 15)
            N_QR(0, 5, 10, 15)
            N_QR(1, 6, 11, 12)
            N_QR(2, 7, 8, 13)
            N_QR(3, 4, 9, 14)
        }
        for (int i = 0; i < 16; ++i) x[i] = vaddq_u32(x[i], o[i]);
        for (int g = 0; g < 4; ++g) {
            uint32x4_t a0 = vtrn1q_u32(x[4 * g + 0], x[4 * g + 1]);
            uint32x4_t a1 = vtrn2q_u32(x[4 * g + 0], x[4 * g + 1]);
            uint32x4_t a2 = vtrn1q_u32(x[4 * g + 2], x[4 * g + 3]);
            uint32x4_t a3 = vtrn2q_u32(x[4 * g + 2], x[4 * g + 3]);
            uint32x4_t b[4];
            b[0] = vreinterpretq_u32_u64(vtrn1q_u64(vreinterpretq_u64_u32(a0), vreinterpretq_u64_u32(a2)));
            b[1] = vreinterpretq_u32_u64(vtrn1q_u64(vreinterpretq_u64_u32(a1), vreinterpretq_u64_u32(a3)));
            b[2] = vreinterpretq_u32_u64(vtrn2q_u64(vreinterpretq_u64_u32(a0), vreinterpretq_u64_u32(a2)));
            b[3] = vreinterpretq_u32_u64(vtrn2q_u64(vreinterpretq_u64_u32(a1), vreinterpretq_u64_u32(a3)));
            for (int j = 0; j < 4; ++j) {
                const uint8_t* ip = in + 64 * j + 16 * g;
                uint8_t* op = out + 64 * j + 16 * g;
                vst1q_u8(op, veorq_u8(vld1q_u8(ip), vreinterpretq_u8_u32(b[j])));
            }
        }
        s[12] += 4;
        in += 256;
        out += 256;
        done += 4;
    }
    return done;
}
#undef N_QR
#undef N_ROTL
#endif

// ------------------------------------------------------------------ SSE2 4 路 / AVX2 8 路
#if FDV_X86_CODE

#define S_ROTL(v, n) _mm_or_si128(_mm_slli_epi32(v, n), _mm_srli_epi32(v, 32 - n))
#define S_QR(a, b, c, d)                                                              \
    x[a] = _mm_add_epi32(x[a], x[b]); x[d] = _mm_xor_si128(x[d], x[a]); x[d] = S_ROTL(x[d], 16); \
    x[c] = _mm_add_epi32(x[c], x[d]); x[b] = _mm_xor_si128(x[b], x[c]); x[b] = S_ROTL(x[b], 12); \
    x[a] = _mm_add_epi32(x[a], x[b]); x[d] = _mm_xor_si128(x[d], x[a]); x[d] = S_ROTL(x[d], 8);  \
    x[c] = _mm_add_epi32(x[c], x[d]); x[b] = _mm_xor_si128(x[b], x[c]); x[b] = S_ROTL(x[b], 7);

size_t xor_blocks_sse2(uint32_t s[16], uint8_t* out, const uint8_t* in, size_t n) {
    size_t done = 0;
    const __m128i inc = _mm_setr_epi32(0, 1, 2, 3);
    while (n - done >= 4) {
        __m128i x[16], o[16];
        for (int i = 0; i < 16; ++i) o[i] = _mm_set1_epi32(int(s[i]));
        o[12] = _mm_add_epi32(o[12], inc);
        for (int i = 0; i < 16; ++i) x[i] = o[i];
        for (int r = 0; r < 10; ++r) {
            S_QR(0, 4, 8, 12)
            S_QR(1, 5, 9, 13)
            S_QR(2, 6, 10, 14)
            S_QR(3, 7, 11, 15)
            S_QR(0, 5, 10, 15)
            S_QR(1, 6, 11, 12)
            S_QR(2, 7, 8, 13)
            S_QR(3, 4, 9, 14)
        }
        for (int i = 0; i < 16; ++i) x[i] = _mm_add_epi32(x[i], o[i]);
        for (int g = 0; g < 4; ++g) {
            __m128i t0 = _mm_unpacklo_epi32(x[4 * g + 0], x[4 * g + 1]);
            __m128i t1 = _mm_unpacklo_epi32(x[4 * g + 2], x[4 * g + 3]);
            __m128i t2 = _mm_unpackhi_epi32(x[4 * g + 0], x[4 * g + 1]);
            __m128i t3 = _mm_unpackhi_epi32(x[4 * g + 2], x[4 * g + 3]);
            __m128i b[4];
            b[0] = _mm_unpacklo_epi64(t0, t1);
            b[1] = _mm_unpackhi_epi64(t0, t1);
            b[2] = _mm_unpacklo_epi64(t2, t3);
            b[3] = _mm_unpackhi_epi64(t2, t3);
            for (int j = 0; j < 4; ++j) {
                const __m128i* ip = reinterpret_cast<const __m128i*>(in + 64 * j + 16 * g);
                __m128i* op = reinterpret_cast<__m128i*>(out + 64 * j + 16 * g);
                _mm_storeu_si128(op, _mm_xor_si128(_mm_loadu_si128(ip), b[j]));
            }
        }
        s[12] += 4;
        in += 256;
        out += 256;
        done += 4;
    }
    return done;
}
#undef S_QR
#undef S_ROTL

#define A_ROTL(v, n) _mm256_or_si256(_mm256_slli_epi32(v, n), _mm256_srli_epi32(v, 32 - n))
#define A_QR(a, b, c, d)                                                                         \
    x[a] = _mm256_add_epi32(x[a], x[b]); x[d] = _mm256_xor_si256(x[d], x[a]); x[d] = _mm256_shuffle_epi8(x[d], r16); \
    x[c] = _mm256_add_epi32(x[c], x[d]); x[b] = _mm256_xor_si256(x[b], x[c]); x[b] = A_ROTL(x[b], 12);              \
    x[a] = _mm256_add_epi32(x[a], x[b]); x[d] = _mm256_xor_si256(x[d], x[a]); x[d] = _mm256_shuffle_epi8(x[d], r8);  \
    x[c] = _mm256_add_epi32(x[c], x[d]); x[b] = _mm256_xor_si256(x[b], x[c]); x[b] = A_ROTL(x[b], 7);

FDV_TARGET("avx2")
size_t xor_blocks_avx2(uint32_t s[16], uint8_t* out, const uint8_t* in, size_t n) {
    size_t done = 0;
    const __m256i inc = _mm256_setr_epi32(0, 1, 2, 3, 4, 5, 6, 7);
    const __m256i r16 = _mm256_setr_epi8(2, 3, 0, 1, 6, 7, 4, 5, 10, 11, 8, 9, 14, 15, 12, 13,
                                         2, 3, 0, 1, 6, 7, 4, 5, 10, 11, 8, 9, 14, 15, 12, 13);
    const __m256i r8 = _mm256_setr_epi8(3, 0, 1, 2, 7, 4, 5, 6, 11, 8, 9, 10, 15, 12, 13, 14,
                                        3, 0, 1, 2, 7, 4, 5, 6, 11, 8, 9, 10, 15, 12, 13, 14);
    while (n - done >= 8) {
        __m256i x[16], o[16];
        for (int i = 0; i < 16; ++i) o[i] = _mm256_set1_epi32(int(s[i]));
        o[12] = _mm256_add_epi32(o[12], inc);
        for (int i = 0; i < 16; ++i) x[i] = o[i];
        for (int r = 0; r < 10; ++r) {
            A_QR(0, 4, 8, 12)
            A_QR(1, 5, 9, 13)
            A_QR(2, 6, 10, 14)
            A_QR(3, 7, 11, 15)
            A_QR(0, 5, 10, 15)
            A_QR(1, 6, 11, 12)
            A_QR(2, 7, 8, 13)
            A_QR(3, 4, 9, 14)
        }
        for (int i = 0; i < 16; ++i) x[i] = _mm256_add_epi32(x[i], o[i]);
        // 每组 4 个字（g=0..3）做 128 位通道内 4x4 转置：
        // b[g][j] = [块 j 的字 4g..4g+3 | 块 j+4 的字 4g..4g+3]
        __m256i b[4][4];
        for (int g = 0; g < 4; ++g) {
            __m256i t0 = _mm256_unpacklo_epi32(x[4 * g + 0], x[4 * g + 1]);
            __m256i t1 = _mm256_unpacklo_epi32(x[4 * g + 2], x[4 * g + 3]);
            __m256i t2 = _mm256_unpackhi_epi32(x[4 * g + 0], x[4 * g + 1]);
            __m256i t3 = _mm256_unpackhi_epi32(x[4 * g + 2], x[4 * g + 3]);
            b[g][0] = _mm256_unpacklo_epi64(t0, t1);
            b[g][1] = _mm256_unpackhi_epi64(t0, t1);
            b[g][2] = _mm256_unpacklo_epi64(t2, t3);
            b[g][3] = _mm256_unpackhi_epi64(t2, t3);
        }
        for (int j = 0; j < 4; ++j) {
            // 块 j：前 32 字节 = 组 0、1 的低半；后 32 字节 = 组 2、3 的低半
            // 块 j+4：对应的高半
            __m256i lo01 = _mm256_permute2x128_si256(b[0][j], b[1][j], 0x20);
            __m256i lo23 = _mm256_permute2x128_si256(b[2][j], b[3][j], 0x20);
            __m256i hi01 = _mm256_permute2x128_si256(b[0][j], b[1][j], 0x31);
            __m256i hi23 = _mm256_permute2x128_si256(b[2][j], b[3][j], 0x31);
            const uint8_t* ipa = in + 64 * j;
            uint8_t* opa = out + 64 * j;
            const uint8_t* ipb = in + 64 * (j + 4);
            uint8_t* opb = out + 64 * (j + 4);
            _mm256_storeu_si256(reinterpret_cast<__m256i*>(opa),
                                _mm256_xor_si256(_mm256_loadu_si256(reinterpret_cast<const __m256i*>(ipa)), lo01));
            _mm256_storeu_si256(reinterpret_cast<__m256i*>(opa + 32),
                                _mm256_xor_si256(_mm256_loadu_si256(reinterpret_cast<const __m256i*>(ipa + 32)), lo23));
            _mm256_storeu_si256(reinterpret_cast<__m256i*>(opb),
                                _mm256_xor_si256(_mm256_loadu_si256(reinterpret_cast<const __m256i*>(ipb)), hi01));
            _mm256_storeu_si256(reinterpret_cast<__m256i*>(opb + 32),
                                _mm256_xor_si256(_mm256_loadu_si256(reinterpret_cast<const __m256i*>(ipb + 32)), hi23));
        }
        s[12] += 8;
        in += 512;
        out += 512;
        done += 8;
    }
    _mm256_zeroupper();
    return done;
}
#undef A_QR
#undef A_ROTL
#endif

void setup(uint32_t s[16], const uint8_t key[32], const uint8_t nonce[12], uint32_t counter) {
    s[0] = 0x61707865;
    s[1] = 0x3320646e;
    s[2] = 0x79622d32;
    s[3] = 0x6b206574;
    for (int i = 0; i < 8; ++i) s[4 + i] = load32_le(key + 4 * i);
    s[12] = counter;
    s[13] = load32_le(nonce + 0);
    s[14] = load32_le(nonce + 4);
    s[15] = load32_le(nonce + 8);
}

}  // namespace

void chacha20_block(const uint8_t key[32], const uint8_t nonce[12], uint32_t counter, uint8_t out[64]) {
    uint32_t s[16];
    setup(s, key, nonce, counter);
    block_scalar(s, out);
}

int chacha20_best_impl() {
    if (force_portable()) return 0;
    int dis = g_disabled.load(std::memory_order_relaxed);
#if FDV_X86_CODE
    if (cpu().avx2 && !(dis & 2)) return 2;
    if (!(dis & 1)) return 1;
#endif
#if FDV_HAVE_NEON
    if (!(dis & 1)) return 1;
#endif
    return 0;
}

void chacha20_xor_impl(int impl, const uint8_t key[32], const uint8_t nonce[12], uint32_t counter,
                       uint8_t* out, const uint8_t* in, size_t len) {
    uint32_t s[16];
    setup(s, key, nonce, counter);
    size_t nblocks = len / 64;
    size_t done = 0;
#if FDV_X86_CODE
    if (impl >= 2) done += xor_blocks_avx2(s, out, in, nblocks);
    if (impl >= 1) done += xor_blocks_sse2(s, out + 64 * done, in + 64 * done, nblocks - done);
#endif
#if FDV_HAVE_NEON
    if (impl >= 1) done += xor_blocks_neon(s, out, in, nblocks);
#endif
    (void)impl;
    xor_blocks_scalar(s, out + 64 * done, in + 64 * done, nblocks - done);
    size_t off = 64 * nblocks;
    size_t rem = len - off;
    if (rem) {
        uint8_t ks[64];
        block_scalar(s, ks);
        for (size_t i = 0; i < rem; ++i) out[off + i] = in[off + i] ^ ks[i];
    }
}

void chacha20_xor(const uint8_t key[32], const uint8_t nonce[12], uint32_t counter, uint8_t* out,
                  const uint8_t* in, size_t len) {
    chacha20_xor_impl(chacha20_best_impl(), key, nonce, counter, out, in, len);
}

const char* chacha20_impl_name() {
    switch (chacha20_best_impl()) {
        case 2: return "avx2";
#if FDV_HAVE_NEON
        case 1: return "neon";
#else
        case 1: return "sse2";
#endif
        default: return "portable";
    }
}

void chacha20_disable(int impl_mask) { g_disabled.fetch_or(impl_mask); }

}  // namespace fdv
