// C 接口 + 上电自检（已知答案测试 + SIMD 与可移植实现交叉比对）。
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "aead.h"
#include "chacha20.h"
#include "engine.h"
#include "fdv_native.h"
#include "platform.h"
#include "poly1305.h"
#include "sha256.h"

#ifndef FDV_VERSION_STR
#define FDV_VERSION_STR "1.0.0"
#endif

namespace {

using namespace fdv;

bool hex_eq(const uint8_t* got, const char* hex, size_t n) {
    for (size_t i = 0; i < n; ++i) {
        unsigned v = 0;
        std::sscanf(hex + 2 * i, "%2x", &v);
        if (got[i] != uint8_t(v)) return false;
    }
    return true;
}

struct Rng {
    uint64_t s;
    explicit Rng(uint64_t seed) : s(seed) {}
    uint64_t next() {
        s ^= s << 13;
        s ^= s >> 7;
        s ^= s << 17;
        return s;
    }
    void fill(uint8_t* p, size_t n) {
        for (size_t i = 0; i < n; ++i) p[i] = uint8_t(next() >> 32);
    }
};

void sha_with(int impl, const uint8_t* d, size_t n, uint8_t out[32], size_t split) {
    Sha256 h(impl);
    size_t off = 0;
    while (off < n) {
        size_t k = split ? std::min(split, n - off) : n - off;
        h.update(d + off, k);
        off += k;
    }
    h.finish(out);
}

// 返回空字符串表示通过
std::string kat_portable() {
    std::string fail;
    uint8_t out[64];
    // SHA-256（FIPS 180-2 附录）
    {
        const char* abc = "abc";
        sha_with(0, reinterpret_cast<const uint8_t*>(abc), 3, out, 0);
        if (!hex_eq(out, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", 32)) fail += " sha256-abc";
        sha_with(0, nullptr, 0, out, 0);
        if (!hex_eq(out, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", 32)) fail += " sha256-empty";
        const char* m2 = "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";
        sha_with(0, reinterpret_cast<const uint8_t*>(m2), std::strlen(m2), out, 0);
        if (!hex_eq(out, "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1", 32)) fail += " sha256-448";
    }
    // ChaCha20 块函数（RFC 8439 §2.3.2）
    {
        uint8_t key[32], nonce[12] = {0, 0, 0, 9, 0, 0, 0, 0x4a, 0, 0, 0, 0};
        for (int i = 0; i < 32; ++i) key[i] = uint8_t(i);
        chacha20_block(key, nonce, 1, out);
        if (!hex_eq(out,
                    "10f1e7e4d13b5915500fdd1fa32071c4c7d1f4c733c068030422aa9ac3d46c4e"
                    "d2826446079faa0914c2d705d98b02a2b5129cd1de164eb9cbd083e8a2503c4e",
                    64))
            fail += " chacha20-block";
    }
    // Poly1305（RFC 8439 §2.5.2）
    {
        const char* keyhex = "85d6be7857556d337f4452fe42d506a80103808afb0db2fd4abff6af4149f51b";
        uint8_t key[32];
        for (int i = 0; i < 32; ++i) {
            unsigned v;
            std::sscanf(keyhex + 2 * i, "%2x", &v);
            key[i] = uint8_t(v);
        }
        const char* msg = "Cryptographic Forum Research Group";
        Poly1305 p;
        p.init(key);
        p.update(reinterpret_cast<const uint8_t*>(msg), std::strlen(msg));
        p.finish(out);
        if (!hex_eq(out, "a8061dc1305136c6c22b8baf0c0127a9", 16)) fail += " poly1305";
    }
    return fail;
}

std::string kat_aead() {
    std::string fail;
    // AEAD（RFC 8439 §2.8.2）
    uint8_t key[32];
    for (int i = 0; i < 32; ++i) key[i] = uint8_t(0x80 + i);
    const uint8_t nonce[12] = {0x07, 0, 0, 0, 0x40, 0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47};
    const uint8_t aad[12] = {0x50, 0x51, 0x52, 0x53, 0xc0, 0xc1, 0xc2, 0xc3, 0xc4, 0xc5, 0xc6, 0xc7};
    const char* pt =
        "Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, sunscreen "
        "would be it.";
    size_t n = std::strlen(pt);
    std::vector<uint8_t> buf(pt, pt + n);
    uint8_t tag[16];
    aead_seal(key, nonce, aad, sizeof(aad), buf.data(), n, tag);
    if (!hex_eq(buf.data(), "d31a8d34648e60db7b86afbc53ef7ec2", 16)) fail += " aead-ct";
    if (!hex_eq(tag, "1ae10b594f09e26a7e902ecbd0600691", 16)) fail += " aead-tag";
    if (!aead_open(key, nonce, aad, sizeof(aad), buf.data(), n, tag) || std::memcmp(buf.data(), pt, n) != 0)
        fail += " aead-open";
    buf[3] ^= 1;
    if (aead_open(key, nonce, aad, sizeof(aad), buf.data(), n, tag)) fail += " aead-reject";
    return fail;
}

// SIMD 实现与可移植实现逐字节比对；不一致就停用该实现
std::string cross_check(std::string& disabled) {
    std::string notes;
    Rng rng(0x9e3779b97f4a7c15ULL);
    std::vector<uint8_t> a(70000 + 64), b(70000 + 64), src(70000 + 64);
    rng.fill(src.data(), src.size());
    const size_t lens[] = {0, 1, 63, 64, 65, 127, 128, 255, 256, 257, 511, 512, 513, 1000, 4103, 16384, 16447, 70000};

    int best_c = chacha20_best_impl();
    for (int impl = best_c; impl >= 1; --impl) {
        bool ok = true;
        for (size_t len : lens) {
            uint8_t key[32], nonce[12];
            rng.fill(key, 32);
            rng.fill(nonce, 12);
            uint32_t ctr = uint32_t(rng.next() & 0xffff);
            chacha20_xor_impl(0, key, nonce, ctr, a.data(), src.data(), len);
            chacha20_xor_impl(impl, key, nonce, ctr, b.data(), src.data(), len);
            if (std::memcmp(a.data(), b.data(), len) != 0) ok = false;
            // 原地
            std::memcpy(b.data(), src.data(), len);
            chacha20_xor_impl(impl, key, nonce, ctr, b.data(), b.data(), len);
            if (std::memcmp(a.data(), b.data(), len) != 0) ok = false;
        }
        if (!ok) {
            chacha20_disable(impl == 2 ? 2 : 1);
            disabled += impl == 2 ? " chacha20-avx2" : " chacha20-simd";
        }
    }

    int best_s = sha256_best_impl();
    if (best_s != 0) {
        bool ok = true;
        const size_t splits[] = {0, 1, 7, 64, 100};
        for (size_t len : lens) {
            for (size_t sp : splits) {
                uint8_t x[32], y[32];
                sha_with(0, src.data(), len, x, sp);
                sha_with(best_s, src.data(), len, y, sp);
                if (std::memcmp(x, y, 32) != 0) ok = false;
            }
        }
        if (!ok) {
            sha256_disable(best_s);
            disabled += best_s == 1 ? " sha256-shani" : " sha256-armv8";
        }
    }
    return notes;
}

std::once_flag g_post_once;
int g_post_result = 0;
std::string g_post_report;

void post() {
    std::string fail = kat_portable();
    std::string disabled;
    cross_check(disabled);
    // 用（可能已降级的）最优实现再跑一遍 AEAD 和 SHA 已知答案
    fail += kat_aead();
    {
        uint8_t out[32];
        Sha256 h;
        std::vector<uint8_t> a(1000000, 'a');
        h.update(a.data(), a.size());
        h.finish(out);
        if (!hex_eq(out, "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0", 32))
            fail += " sha256-million";
    }
    g_post_result = fail.empty() ? 0 : 1;
    g_post_report = fail.empty() ? "ok" : ("FAIL:" + fail);
    if (!disabled.empty()) g_post_report += " DISABLED:" + disabled;
}

void ensure_post() { std::call_once(g_post_once, post); }

std::string g_info;
std::mutex g_info_mu;

}  // namespace

extern "C" {

FDV_API int fdv_run(int op, fdv_job* jobs, size_t njobs, int threads, int stop_on_error, fdv_progress_fn cb,
                    void* user) {
    ensure_post();
    if (g_post_result != 0) return -2;  // 可移植实现都没通过已知答案测试：拒绝工作
    return fdv::run_batch(op, jobs, njobs, threads, stop_on_error, cb, user);
}

FDV_API int fdv_abi_version(void) { return FDV_ABI_VERSION; }

FDV_API size_t fdv_job_size(void) { return sizeof(fdv_job); }

// 默认线程数 = 逻辑核数 × 2：每个文件结束都要 fsync（几毫秒的等待），
// 多出来的线程在别人等盘的时候继续算，CPU 和磁盘队列都能用满。
FDV_API int fdv_default_threads(void) {
    unsigned n = std::thread::hardware_concurrency();
    if (n == 0) n = 4;
    n *= 2;
    if (n < 4) n = 4;
    if (n > 64) n = 64;
    return int(n);
}

FDV_API const char* fdv_version(void) { return FDV_VERSION_STR; }

FDV_API const char* fdv_impl_info(void) {
    ensure_post();
    std::lock_guard<std::mutex> lk(g_info_mu);
    g_info = std::string("chacha20=") + chacha20_impl_name() + " poly1305=64bit sha256=" + sha256_impl_name() +
             " threads=" + std::to_string(fdv_default_threads());
#if defined(FDV_EMULATE_X86)
    g_info += " arch=x86-emulated";
#elif FDV_X64
    g_info += " arch=x86_64";
#elif FDV_ARM64
    g_info += " arch=arm64";
#endif
    return g_info.c_str();
}

FDV_API int fdv_selftest(char* report, size_t report_len) {
    ensure_post();
    if (report && report_len) {
        std::snprintf(report, report_len, "%s", g_post_report.c_str());
    }
    return g_post_result;
}

FDV_API void fdv_aead_seal(const uint8_t key[32], const uint8_t nonce[12], const uint8_t* aad, size_t aadlen,
                           uint8_t* buf, size_t len, uint8_t tag[16]) {
    fdv::aead_seal(key, nonce, aad, aadlen, buf, len, tag);
}

FDV_API int fdv_aead_open(const uint8_t key[32], const uint8_t nonce[12], const uint8_t* aad, size_t aadlen,
                          uint8_t* buf, size_t len, const uint8_t tag[16]) {
    return fdv::aead_open(key, nonce, aad, aadlen, buf, len, tag) ? 1 : 0;
}

FDV_API void fdv_sha256(const uint8_t* data, size_t len, uint8_t out[32]) {
    fdv::Sha256 h;
    h.update(data, len);
    h.finish(out);
}

FDV_API void fdv_set_force_portable(int on) { fdv::set_force_portable(on != 0); }

}  // extern "C"
