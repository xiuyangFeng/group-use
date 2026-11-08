#!/usr/bin/env python3
"""编译 C++ 加解密引擎（native/），把动态库复制到 core/_native/。

用法（项目根目录）：
    python scripts/build_native.py           # Release 构建 + 自检
    python scripts/build_native.py --clean   # 删掉旧的构建目录重来

需要 CMake ≥ 3.15 和 C++17 编译器：macOS 用 Xcode 命令行工具（会编成 arm64 + x86_64 通用库），
Windows 用 Visual Studio 2019/2022（“使用 C++ 的桌面开发”），产物只依赖系统自带的 kernel32。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
NATIVE = PROJECT / "native"
BUILD = NATIVE / "build"
OUT = PROJECT / "core" / "_native"


def log(msg: str) -> None:
    print(f"[C++ 引擎] {msg}", flush=True)


def run(cmd: list[str]) -> None:
    log("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(PROJECT))


def built_library() -> Path:
    if sys.platform == "win32":
        for cand in (BUILD / "Release" / "fdvnative.dll", BUILD / "fdvnative.dll"):
            if cand.exists():
                return cand
        raise SystemExit("没找到编译出的 fdvnative.dll")
    name = "libfdvnative.dylib" if sys.platform == "darwin" else "libfdvnative.so"
    lib = BUILD / name
    if not lib.exists():
        raise SystemExit(f"没找到编译出的 {name}")
    return lib


def bench_binary() -> Path | None:
    for cand in (BUILD / "Release" / "fdv_bench.exe", BUILD / "fdv_bench.exe", BUILD / "fdv_bench"):
        if cand.exists():
            return cand
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args(argv)
    if shutil.which("cmake") is None:
        raise SystemExit("没找到 cmake：macOS 用 `brew install cmake`，Windows 装 Visual Studio 时勾上 CMake")
    if args.clean:
        shutil.rmtree(BUILD, ignore_errors=True)

    cfg = ["cmake", "-S", str(NATIVE), "-B", str(BUILD), "-DCMAKE_BUILD_TYPE=Release"]
    if sys.platform == "darwin":
        cfg += ["-DCMAKE_OSX_ARCHITECTURES=arm64;x86_64", "-DCMAKE_OSX_DEPLOYMENT_TARGET=11.0"]
    elif sys.platform == "win32":
        cfg += ["-A", "x64"]
    run(cfg)
    run(["cmake", "--build", str(BUILD), "--config", "Release", "--parallel"])

    lib = built_library()
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / lib.name
    shutil.copy2(lib, dest)
    log(f"已复制 → {dest.relative_to(PROJECT)}")

    bench = bench_binary()
    if bench is not None:
        run([str(bench), "64"])

    # 用 Python 端的加载逻辑再确认一次（ABI、结构体大小、自检）
    sys.path.insert(0, str(PROJECT))
    from core import native

    info = native.info()
    if info.get("engine") != "C++":
        raise SystemExit(f"C++ 引擎加载失败：{info}")
    if "DISABLED" in info.get("selftest", ""):
        raise SystemExit(f"有 SIMD 实现没通过自检：{info['selftest']}")
    log(f"加载成功：{info['impl']}（自检 {info['selftest']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
