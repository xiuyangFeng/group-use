// SHA-256：可移植标量；x86-64 SHA-NI；arm64 ARMv8 SHA2 指令。运行时选择。
#include "sha256.h"

#include <atomic>
#include <cstring>

#include "platform.h"

#if FDV_HAVE_ARM_SHA2
#include <arm_neon.h>
#endif

namespace fdv {

namespace {

std::atomic<int> g_disabled{0};

alignas(16) const uint32_t K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

inline uint32_t rotr(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }

void blocks_scalar(uint32_t st[8], const uint8_t* p, size_t nblocks) {
    uint32_t w[64];
    while (nblocks--) {
        for (int i = 0; i < 16; ++i) w[i] = load32_be(p + 4 * i);
        for (int i = 16; i < 64; ++i) {
            uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
            uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16] + s0 + w[i - 7] + s1;
        }
        uint32_t a = st[0], b = st[1], c = st[2], d = st[3], e = st[4], f = st[5], g = st[6], h = st[7];
        for (int i = 0; i < 64; ++i) {
            uint32_t S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
            uint32_t ch = (e & f) ^ (~e & g);
            uint32_t t1 = h + S1 + ch + K[i] + w[i];
            uint32_t S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
            uint32_t mj = (a & b) ^ (a & c) ^ (b & c);
            uint32_t t2 = S0 + mj;
            h = g;
            g = f;
            f = e;
            e = d + t1;
            d = c;
            c = b;
            b = a;
            a = t1 + t2;
        }
        st[0] += a;
        st[1] += b;
        st[2] += c;
        st[3] += d;
        st[4] += e;
        st[5] += f;
        st[6] += g;
        st[7] += h;
        p += 64;
    }
}

#if FDV_X86_CODE
FDV_TARGET("sha,sse4.1,ssse3")
void blocks_shani(uint32_t st[8], const uint8_t* p, size_t nblocks) {
    const __m128i MASK = _mm_set_epi64x(0x0c0d0e0f08090a0bULL, 0x0405060700010203ULL);
    __m128i tmp = _mm_loadu_si128(reinterpret_cast<const __m128i*>(&st[0]));
    __m128i state1 = _mm_loadu_si128(reinterpret_cast<const __m128i*>(&st[4]));
    tmp = _mm_shuffle_epi32(tmp, 0xB1);           // CDAB
    state1 = _mm_shuffle_epi32(state1, 0x1B);     // EFGH
    __m128i state0 = _mm_alignr_epi8(tmp, state1, 8);  // ABEF
    state1 = _mm_blend_epi16(state1, tmp, 0xF0);       // CDGH

    while (nblocks--) {
        const __m128i abef_save = state0;
        const __m128i cdgh_save = state1;
        __m128i msg[4];
        for (int i = 0; i < 4; ++i)
            msg[i] = _mm_shuffle_epi8(_mm_loadu_si128(reinterpret_cast<const __m128i*>(p + 16 * i)), MASK);
        for (int i = 0; i < 16; ++i) {
            __m128i m = _mm_add_epi32(msg[i & 3], _mm_load_si128(reinterpret_cast<const __m128i*>(&K[4 * i])));
            state1 = _mm_sha256rnds2_epu32(state1, state0, m);
            m = _mm_shuffle_epi32(m, 0x0E);
            state0 = _mm_sha256rnds2_epu32(state0, state1, m);
            if (i < 12) {
                __m128i t = _mm_sha256msg1_epu32(msg[i & 3], msg[(i + 1) & 3]);
                t = _mm_add_epi32(t, _mm_alignr_epi8(msg[(i + 3) & 3], msg[(i + 2) & 3], 4));
                msg[i & 3] = _mm_sha256msg2_epu32(t, msg[(i + 3) & 3]);
            }
        }
        state0 = _mm_add_epi32(state0, abef_save);
        state1 = _mm_add_epi32(state1, cdgh_save);
        p += 64;
    }

    tmp = _mm_shuffle_epi32(state0, 0x1B);        // FEBA
    state1 = _mm_shuffle_epi32(state1, 0xB1);     // DCHG
    state0 = _mm_blend_epi16(tmp, state1, 0xF0);  // DCBA
    state1 = _mm_alignr_epi8(state1, tmp, 8);     // HGFE
    _mm_storeu_si128(reinterpret_cast<__m128i*>(&st[0]), state0);
    _mm_storeu_si128(reinterpret_cast<__m128i*>(&st[4]), state1);
}
#endif

#if FDV_HAVE_ARM_SHA2
void blocks_armv8(uint32_t st[8], const uint8_t* p, size_t nblocks) {
    uint32x4_t state0 = vld1q_u32(&st[0]);
    uint32x4_t state1 = vld1q_u32(&st[4]);
    while (nblocks--) {
        const uint32x4_t abcd_save = state0;
        const uint32x4_t efgh_save = state1;
        uint32x4_t msg[4];
        for (int i = 0; i < 4; ++i)
            msg[i] = vreinterpretq_u32_u8(vrev32q_u8(vld1q_u8(p + 16 * i)));
        for (int i = 0; i < 16; ++i) {
            uint32x4_t wk = vaddq_u32(msg[i & 3], vld1q_u32(&K[4 * i]));
            uint32x4_t s0 = state0;
            state0 = vsha256hq_u32(state0, state1, wk);
            state1 = vsha256h2q_u32(state1, s0, wk);
            if (i < 12) {
                msg[i & 3] = vsha256su1q_u32(vsha256su0q_u32(msg[i & 3], msg[(i + 1) & 3]), msg[(i + 2) & 3],
                                             msg[(i + 3) & 3]);
            }
        }
        state0 = vaddq_u32(state0, abcd_save);
        state1 = vaddq_u32(state1, efgh_save);
        p += 64;
    }
    vst1q_u32(&st[0], state0);
    vst1q_u32(&st[4], state1);
}
#endif

void run_blocks(int impl, uint32_t st[8], const uint8_t* p, size_t nblocks) {
#if FDV_X86_CODE
    if (impl == 1) return blocks_shani(st, p, nblocks);
#endif
#if FDV_HAVE_ARM_SHA2
    if (impl == 2) return blocks_armv8(st, p, nblocks);
#endif
    blocks_scalar(st, p, nblocks);
}

}  // namespace

int sha256_best_impl() {
    if (force_portable()) return 0;
    int dis = g_disabled.load(std::memory_order_relaxed);
#if FDV_X86_CODE
    if (cpu().shani && !(dis & 1)) return 1;
#endif
#if FDV_HAVE_ARM_SHA2
    if (!(dis & 2)) return 2;
#endif
    return 0;
}

void sha256_disable(int impl_mask) { g_disabled.fetch_or(impl_mask); }

const char* sha256_impl_name() {
    switch (sha256_best_impl()) {
        case 1: return "sha-ni";
        case 2: return "armv8-sha2";
        default: return "portable";
    }
}

Sha256::Sha256() : Sha256(sha256_best_impl()) {}

Sha256::Sha256(int impl_) : total(0), used(0), impl(impl_) {
    static const uint32_t IV[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                   0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    std::memcpy(state, IV, sizeof(IV));
}

void Sha256::update(const uint8_t* data, size_t len) {
    total += len;
    if (used) {
        size_t want = 64 - used;
        if (want > len) want = len;
        std::memcpy(buf + used, data, want);
        used += want;
        data += want;
        len -= want;
        if (used < 64) return;
        run_blocks(impl, state, buf, 1);
        used = 0;
    }
    if (len >= 64) {
        size_t n = len / 64;
        run_blocks(impl, state, data, n);
        data += 64 * n;
        len -= 64 * n;
    }
    if (len) {
        std::memcpy(buf, data, len);
        used = len;
    }
}

void Sha256::finish(uint8_t out[32]) {
    uint64_t bits = total * 8;
    uint8_t pad[72];
    size_t padlen = (used < 56) ? (56 - used) : (120 - used);
    std::memset(pad, 0, sizeof(pad));
    pad[0] = 0x80;
    store64_be(pad + padlen, bits);
    uint64_t keep_total = total;
    update(pad, padlen + 8);
    total = keep_total;
    for (int i = 0; i < 8; ++i) store32_be(out + 4 * i, state[i]);
}

}  // namespace fdv
