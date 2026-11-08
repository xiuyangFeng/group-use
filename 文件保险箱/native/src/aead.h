#pragma once
#include <cstddef>
#include <cstdint>

namespace fdv {

// 原地加密 buf[0..len)，tag 写到 tag[16]
void aead_seal(const uint8_t key[32], const uint8_t nonce[12], const uint8_t* aad, size_t aadlen, uint8_t* buf,
               size_t len, uint8_t tag[16]);

// 原地解密；返回 false 表示认证失败（此时 buf 内容无意义，调用方必须丢弃）
bool aead_open(const uint8_t key[32], const uint8_t nonce[12], const uint8_t* aad, size_t aadlen, uint8_t* buf,
               size_t len, const uint8_t tag[16]);

}  // namespace fdv
