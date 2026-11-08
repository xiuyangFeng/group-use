#include "poly1305.h"

#include <cstring>

#include "platform.h"

#if FDV_MSVC
#include <intrin.h>
#endif

namespace fdv {

namespace {

#if defined(__SIZEOF_INT128__)
typedef unsigned __int128 u128;
FDV_INLINE u128 mul64(uint64_t a, uint64_t b) { return u128(a) * b; }
FDV_INLINE void add(u128& o, const u128& v) { o += v; }
FDV_INLINE void addlo(u128& o, uint64_t v) { o += v; }
FDV_INLINE uint64_t shr(const u128& v, int s) { return uint64_t(v >> s); }
FDV_INLINE uint64_t lo(const u128& v) { return uint64_t(v); }
#else
struct u128 {
    uint64_t lo, hi;
};
FDV_INLINE u128 mul64(uint64_t a, uint64_t b) {
    u128 r;
    r.lo = _umul128(a, b, &r.hi);
    return r;
}
FDV_INLINE void add(u128& o, const u128& v) {
    uint64_t l = o.lo + v.lo;
    o.hi += v.hi + (l < o.lo ? 1 : 0);
    o.lo = l;
}
FDV_INLINE void addlo(u128& o, uint64_t v) {
    uint64_t l = o.lo + v;
    o.hi += (l < o.lo ? 1 : 0);
    o.lo = l;
}
FDV_INLINE uint64_t shr(const u128& v, int s) { return (v.hi << (64 - s)) | (v.lo >> s); }
FDV_INLINE uint64_t lo(const u128& v) { return v.lo; }
#endif

constexpr uint64_t M44 = 0xfffffffffffULL;
constexpr uint64_t M42 = 0x3ffffffffffULL;

}  // namespace

void Poly1305::init(const uint8_t key[32]) {
    uint64_t t0 = load64_le(key + 0);
    uint64_t t1 = load64_le(key + 8);
    r[0] = t0 & 0xffc0fffffffULL;
    r[1] = ((t0 >> 44) | (t1 << 20)) & 0xfffffc0ffffULL;
    r[2] = (t1 >> 24) & 0x00ffffffc0fULL;
    h[0] = h[1] = h[2] = 0;
    pad[0] = load64_le(key + 16);
    pad[1] = load64_le(key + 24);
    leftover = 0;
}

void Poly1305::blocks(const uint8_t* m, size_t len, uint64_t hibit) {
    const uint64_t r0 = r[0], r1 = r[1], r2 = r[2];
    const uint64_t s1 = r1 * (5 << 2);
    const uint64_t s2 = r2 * (5 << 2);
    uint64_t h0 = h[0], h1 = h[1], h2 = h[2];
    while (len >= 16) {
        uint64_t t0 = load64_le(m + 0);
        uint64_t t1 = load64_le(m + 8);
        h0 += t0 & M44;
        h1 += ((t0 >> 44) | (t1 << 20)) & M44;
        h2 += ((t1 >> 24) & M42) | hibit;

        u128 d0 = mul64(h0, r0);
        add(d0, mul64(h1, s2));
        add(d0, mul64(h2, s1));
        u128 d1 = mul64(h0, r1);
        add(d1, mul64(h1, r0));
        add(d1, mul64(h2, s2));
        u128 d2 = mul64(h0, r2);
        add(d2, mul64(h1, r1));
        add(d2, mul64(h2, r0));

        uint64_t c = shr(d0, 44);
        h0 = lo(d0) & M44;
        addlo(d1, c);
        c = shr(d1, 44);
        h1 = lo(d1) & M44;
        addlo(d2, c);
        c = shr(d2, 42);
        h2 = lo(d2) & M42;
        h0 += c * 5;
        c = h0 >> 44;
        h0 &= M44;
        h1 += c;

        m += 16;
        len -= 16;
    }
    h[0] = h0;
    h[1] = h1;
    h[2] = h2;
}

void Poly1305::update(const uint8_t* m, size_t len) {
    if (leftover) {
        size_t want = 16 - leftover;
        if (want > len) want = len;
        std::memcpy(buf + leftover, m, want);
        len -= want;
        m += want;
        leftover += want;
        if (leftover < 16) return;
        blocks(buf, 16, 1ULL << 40);
        leftover = 0;
    }
    if (len >= 16) {
        size_t want = len & ~size_t(15);
        blocks(m, want, 1ULL << 40);
        m += want;
        len -= want;
    }
    if (len) {
        std::memcpy(buf, m, len);
        leftover = len;
    }
}

void Poly1305::finish(uint8_t mac[16]) {
    if (leftover) {
        size_t i = leftover;
        buf[i++] = 1;
        for (; i < 16; ++i) buf[i] = 0;
        blocks(buf, 16, 0);
    }
    uint64_t h0 = h[0], h1 = h[1], h2 = h[2], c;
    c = h1 >> 44; h1 &= M44;
    h2 += c; c = h2 >> 42; h2 &= M42;
    h0 += c * 5; c = h0 >> 44; h0 &= M44;
    h1 += c; c = h1 >> 44; h1 &= M44;
    h2 += c; c = h2 >> 42; h2 &= M42;
    h0 += c * 5; c = h0 >> 44; h0 &= M44;
    h1 += c;

    // g = h + -p
    uint64_t g0 = h0 + 5; c = g0 >> 44; g0 &= M44;
    uint64_t g1 = h1 + c; c = g1 >> 44; g1 &= M44;
    uint64_t g2 = h2 + c - (1ULL << 42);

    // h < p 取 h，否则取 g（常数时间）
    c = (g2 >> 63) - 1;
    g0 &= c; g1 &= c; g2 &= c;
    c = ~c;
    h0 = (h0 & c) | g0;
    h1 = (h1 & c) | g1;
    h2 = (h2 & c) | g2;

    // h += pad
    uint64_t t0 = pad[0], t1 = pad[1];
    h0 += t0 & M44; c = h0 >> 44; h0 &= M44;
    h1 += (((t0 >> 44) | (t1 << 20)) & M44) + c; c = h1 >> 44; h1 &= M44;
    h2 += ((t1 >> 24) & M42) + c; h2 &= M42;

    h0 = h0 | (h1 << 44);
    h1 = (h1 >> 20) | (h2 << 24);
    store64_le(mac + 0, h0);
    store64_le(mac + 8, h1);

    // 擦掉状态
    volatile uint64_t* wipe = r;
    for (int i = 0; i < 3; ++i) wipe[i] = 0;
    volatile uint64_t* wipe_h = h;
    for (int i = 0; i < 3; ++i) wipe_h[i] = 0;
    volatile uint64_t* wipe_p = pad;
    for (int i = 0; i < 2; ++i) wipe_p[i] = 0;
}

}  // namespace fdv
