// ChaCha20-Poly1305 AEAD（RFC 8439 §2.8），原地加解密。
// 按 16 KB 分段交替做 ChaCha20 和 Poly1305，让数据留在 L1/L2 里，只扫一遍内存。
#include "aead.h"

#include <cstring>

#include "chacha20.h"
#include "platform.h"
#include "poly1305.h"

namespace fdv {

namespace {

constexpr size_t SEG = 16 * 1024;  // 必须是 64 的倍数（ChaCha20 块计数连续）

void poly_key(const uint8_t key[32], const uint8_t nonce[12], Poly1305& p) {
    uint8_t block[64];
    chacha20_block(key, nonce, 0, block);
    p.init(block);
    volatile uint8_t* w = block;
    for (int i = 0; i < 64; ++i) w[i] = 0;
}

void poly_pad(Poly1305& p, size_t len) {
    static const uint8_t zeros[16] = {0};
    if (len % 16) p.update(zeros, 16 - (len % 16));
}

void poly_lengths(Poly1305& p, size_t aadlen, size_t len) {
    uint8_t lens[16];
    store64_le(lens, uint64_t(aadlen));
    store64_le(lens + 8, uint64_t(len));
    p.update(lens, 16);
}

}  // namespace

void aead_seal(const uint8_t key[32], const uint8_t nonce[12], const uint8_t* aad, size_t aadlen, uint8_t* buf,
               size_t len, uint8_t tag[16]) {
    Poly1305 p;
    poly_key(key, nonce, p);
    if (aadlen) p.update(aad, aadlen);
    poly_pad(p, aadlen);
    for (size_t off = 0; off < len; off += SEG) {
        size_t n = len - off < SEG ? len - off : SEG;
        chacha20_xor(key, nonce, uint32_t(1 + off / 64), buf + off, buf + off, n);
        p.update(buf + off, n);
    }
    poly_pad(p, len);
    poly_lengths(p, aadlen, len);
    p.finish(tag);
}

bool aead_open(const uint8_t key[32], const uint8_t nonce[12], const uint8_t* aad, size_t aadlen, uint8_t* buf,
               size_t len, const uint8_t tag[16]) {
    Poly1305 p;
    poly_key(key, nonce, p);
    if (aadlen) p.update(aad, aadlen);
    poly_pad(p, aadlen);
    // 先对这一段密文做 MAC，再原地解密；校验失败时调用方会丢弃整块缓冲，不会写出
    for (size_t off = 0; off < len; off += SEG) {
        size_t n = len - off < SEG ? len - off : SEG;
        p.update(buf + off, n);
        chacha20_xor(key, nonce, uint32_t(1 + off / 64), buf + off, buf + off, n);
    }
    poly_pad(p, len);
    poly_lengths(p, aadlen, len);
    uint8_t calc[16];
    p.finish(calc);
    uint8_t diff = 0;
    for (int i = 0; i < 16; ++i) diff |= uint8_t(calc[i] ^ tag[i]);
    return diff == 0;
}

}  // namespace fdv
