#!/usr/bin/env python3
"""从设计稿 PNG 生成程序图标（开发机上手动跑一次，产物提交到仓库；平时打包不需要它）。

    python3 scripts/make_icons.py <设计稿.png> <输出目录>

需要 Pillow（只是生成时用，程序本身不依赖）；生成 .icns 需要 macOS 的 iconutil。

输出：
    mac-16.png … mac-1024.png   macOS 用：按苹果图标网格留边（1024 画布里圆角方块约 824），
                                 和程序坞里其它图标一样大
    app.icns                     由上面几张用 iconutil 合成
    app.ico                      Windows 用：16–256 共 7 个尺寸，方块撑满到约 94%（Windows 图标习惯不留大边）
设计稿外面必须是透明的（只裁掉透明边，不改画面本身）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

MAC_SIZES = [16, 32, 64, 128, 256, 512, 1024]
WIN_SIZES = [16, 24, 32, 48, 64, 128, 256]
MAC_BODY = 824 / 1024  # 苹果图标网格：圆角方块占画布的比例
WIN_BODY = 0.94


def _square_body(src: Image.Image) -> Image.Image:
    """裁掉四周透明的部分，再放进正方形（居中）。"""
    a = src.getchannel("A")
    bbox = a.point(lambda v: 255 if v > 8 else 0).getbbox()
    if bbox is None:
        raise SystemExit("设计稿是全透明的？")
    body = src.crop(bbox)
    side = max(body.size)
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.paste(body, ((side - body.width) // 2, (side - body.height) // 2))
    return sq


def _canvas(body: Image.Image, size: int, ratio: float) -> Image.Image:
    inner = max(1, round(size * ratio))
    scaled = body.resize((inner, inner), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    off = (size - inner) // 2
    out.paste(scaled, (off, off), scaled)
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    src = Image.open(argv[0]).convert("RGBA")
    out = Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    body = _square_body(src)

    for s in MAC_SIZES:
        _canvas(body, s, MAC_BODY).save(out / f"mac-{s}.png", optimize=True)

    win = [_canvas(body, s, WIN_BODY) for s in WIN_SIZES]
    win[-1].save(out / "app.ico", format="ICO", sizes=[(s, s) for s in WIN_SIZES], append_images=win[:-1])

    if sys.platform == "darwin" and shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as tmp:
            iset = Path(tmp) / "app.iconset"
            iset.mkdir()
            pairs = [(16, "16x16"), (32, "16x16@2x"), (32, "32x32"), (64, "32x32@2x"), (128, "128x128"),
                     (256, "128x128@2x"), (256, "256x256"), (512, "256x256@2x"), (512, "512x512"), (1024, "512x512@2x")]
            for s, name in pairs:
                shutil.copy(out / f"mac-{s}.png", iset / f"icon_{name}.png")
            subprocess.run(["iconutil", "-c", "icns", str(iset), "-o", str(out / "app.icns")], check=True)
    else:
        print("（不是 macOS：没有生成 app.icns）")
    for p in sorted(out.iterdir()):
        print(f"{p.name:14} {p.stat().st_size / 1024:7.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
