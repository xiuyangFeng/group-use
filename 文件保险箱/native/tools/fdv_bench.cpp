// 自检 + 单核 / 多核吞吐测试。用法：fdv_bench [MB]
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>
#include <vector>

#include "aead.h"
#include "chacha20.h"
#include "fdv_native.h"
#include "platform.h"
#include "poly1305.h"
#include "sha256.h"

using namespace fdv;
using clk = std::chrono::steady_clock;

static double secs(clk::time_point a) { return std::chrono::duration<double>(clk::now() - a).count(); }

int main(int argc, char** argv) {
    size_t mb = argc > 1 ? size_t(std::atoi(argv[1])) : 256;
    char report[512];
    int st = fdv_selftest(report, sizeof(report));
    std::printf("selftest: %s (%d)\n", report, st);
    std::printf("impl: %s\n", fdv_impl_info());
    if (st != 0) return 1;

    const size_t chunk = 4u << 20;
    std::vector<uint8_t> buf(chunk + 16, 0x5a);
    uint8_t key[32] = {1}, nonce[12] = {2};
    size_t iters = mb / 4;
    if (iters == 0) iters = 1;

    for (int impl = chacha20_best_impl(); impl >= 0; --impl) {
        auto t = clk::now();
        for (size_t i = 0; i < iters; ++i) chacha20_xor_impl(impl, key, nonce, 1, buf.data(), buf.data(), chunk);
        std::printf("chacha20 impl=%d   %8.0f MB/s\n", impl, iters * 4 / secs(t));
    }
    {
        auto t = clk::now();
        uint8_t mac[16];
        for (size_t i = 0; i < iters; ++i) {
            Poly1305 p;
            p.init(key);
            p.update(buf.data(), chunk);
            p.finish(mac);
        }
        std::printf("poly1305           %8.0f MB/s\n", iters * 4 / secs(t));
    }
    for (int impl : {sha256_best_impl(), 0}) {
        auto t = clk::now();
        uint8_t out[32];
        for (size_t i = 0; i < iters; ++i) {
            Sha256 h(impl);
            h.update(buf.data(), chunk);
            h.finish(out);
        }
        std::printf("sha256 impl=%d     %8.0f MB/s\n", impl, iters * 4 / secs(t));
        if (impl == 0) break;
    }
    {
        auto t = clk::now();
        for (size_t i = 0; i < iters; ++i) aead_seal(key, nonce, nonce, 12, buf.data(), chunk, buf.data() + chunk);
        std::printf("aead seal (1 核)    %8.0f MB/s\n", iters * 4 / secs(t));
    }
    {
        int nt = fdv_default_threads();
        std::vector<std::thread> th;
        auto t = clk::now();
        for (int k = 0; k < nt; ++k) {
            th.emplace_back([&, k] {
                std::vector<uint8_t> b(chunk + 16, uint8_t(k));
                for (size_t i = 0; i < iters; ++i) {
                    aead_seal(key, nonce, nonce, 12, b.data(), chunk, b.data() + chunk);
                    Sha256 h;
                    h.update(b.data(), chunk);
                    uint8_t o[32];
                    h.finish(o);
                }
            });
        }
        for (auto& x : th) x.join();
        std::printf("aead+sha (%d 线程)  %8.0f MB/s\n", nt, double(nt) * iters * 4 / secs(t));
    }
    return 0;
}
