# C++ 加解密引擎

文件保险箱的文件内容加解密都在这里完成。设计、调度和正确性保障见 [技术方案 §16](../docs/技术方案.md)。

```bash
python scripts/build_native.py        # 项目根目录：编译 + 复制到 core/_native/ + 自检 + 性能测试
native/build/fdv_bench 512            # 单核 / 多核吞吐（MB）
```

| 目录 | 内容 |
|---|---|
| `include/fdv_native.h` | C 接口（`fdv_run` 批量任务、自检、单块原语） |
| `src/` | ChaCha20 / Poly1305 / SHA-256 / AEAD / 文件 I/O / 调度引擎 |
| `tests/x86_emul.h` | 测试用：纯 C++ 模拟 SSE/AVX2/SHA-NI，在 Apple 芯片上验证 x86 代码路径 |
| `tools/fdv_bench.cpp` | 自检 + 吞吐测试 |

在没有 AVX2 / SHA-NI 的机器上验证 x86 SIMD 代码：

```bash
clang++ -std=c++17 -O2 -DFDV_EMULATE_X86 -Inative/include -Inative/src \
    native/tools/fdv_bench.cpp native/src/*.cpp -o /tmp/fdv_emul && /tmp/fdv_emul 8
# 期望第一行：selftest: ok (0)，第二行 chacha20=avx2 ... sha256=sha-ni
```

改了密文格式相关的任何东西，必须同时改 `core/transform.py` 的 Python 参考实现，`tests/test_native.py` 会逐字节比对两者。
