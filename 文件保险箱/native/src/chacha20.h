#pragma once
#include <cstddef>
#include <cstdint>

namespace fdv {

// out 可以等于 in（原地加解密）
void chacha20_xor(const uint8_t key[32], const uint8_t nonce[12], uint32_t counter, uint8_t* out,
                  const uint8_t* in, size_t len);
void chacha20_block(const uint8_t key[32], const uint8_t nonce[12], uint32_t counter, uint8_t out[64]);

// 自检 / 测试用
int chacha20_best_impl();  // 0=可移植 1=SSE2/NEON 2=AVX2
void chacha20_xor_impl(int impl, const uint8_t key[32], const uint8_t nonce[12], uint32_t counter,
                       uint8_t* out, const uint8_t* in, size_t len);
void chacha20_disable(int impl_mask);
const char* chacha20_impl_name();

}  // namespace fdv
