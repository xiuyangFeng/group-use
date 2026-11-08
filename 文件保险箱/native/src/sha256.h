#pragma once
#include <cstddef>
#include <cstdint>

namespace fdv {

struct Sha256 {
    uint32_t state[8];
    uint64_t total;
    uint8_t buf[64];
    size_t used;
    int impl;

    Sha256();
    explicit Sha256(int impl);
    void update(const uint8_t* data, size_t len);
    void finish(uint8_t out[32]);
};

int sha256_best_impl();  // 0=可移植 1=SHA-NI 2=ARMv8 SHA2
void sha256_disable(int impl_mask);
const char* sha256_impl_name();

}  // namespace fdv
