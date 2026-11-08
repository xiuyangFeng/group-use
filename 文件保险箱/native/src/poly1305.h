#pragma once
#include <cstddef>
#include <cstdint>

namespace fdv {

// Poly1305（RFC 8439），64 位 limb 实现（poly1305-donna-64 的思路）。
struct Poly1305 {
    uint64_t r[3];
    uint64_t h[3];
    uint64_t pad[2];
    uint8_t buf[16];
    size_t leftover;

    void init(const uint8_t key[32]);
    void update(const uint8_t* m, size_t len);
    void finish(uint8_t mac[16]);

  private:
    void blocks(const uint8_t* m, size_t len, uint64_t hibit);
};

}  // namespace fdv
