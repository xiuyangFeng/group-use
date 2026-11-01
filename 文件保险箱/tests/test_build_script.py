"""打包脚本的跨平台逻辑：在 mac 上模拟 Windows 分支，验证压缩包命名/布局/校验和。

不实际运行 PyInstaller，只造一个假的 onedir 目录，检查：
- Windows 产物名带 -Windows-x64.zip；
- zip 内路径用 `/` 且在“文件保险箱/”目录下（双击解压不乱）；
- SHA256SUMS.txt 格式和内容正确。
"""

from __future__ import annotations

import hashlib
import importlib.util
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def _load_build_module():
    spec = importlib.util.spec_from_file_location("fdvault_build_app", PROJECT / "scripts" / "build_app.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_windows_zip_layout_and_naming(tmp_path, monkeypatch):
    mod = _load_build_module()
    dist = tmp_path / "dist"
    bundle = dist / mod.APP_NAME
    (bundle / "_internal").mkdir(parents=True)
    (bundle / f"{mod.APP_NAME}.exe").write_bytes(b"fake exe")
    (bundle / "_internal" / "base_library.zip").write_bytes(b"fake zip")

    monkeypatch.setattr(mod, "DIST", dist)
    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(mod.platform, "machine", lambda: "AMD64")
    source = tmp_path / "source.zip"
    monkeypatch.setattr(mod, "make_source_zip", lambda: source)

    made = mod.archive(bundle)
    names = [p.name for p in made]
    zip_path = dist / f"{mod.APP_NAME}-{mod.version()}-Windows-x64.zip"
    assert zip_path.exists(), names
    assert zip_path in made

    with zipfile.ZipFile(zip_path) as zf:
        entries = zf.namelist()
    assert f"{mod.APP_NAME}/{mod.APP_NAME}.exe" in entries
    assert f"{mod.APP_NAME}/_internal/base_library.zip" in entries
    assert not any("\\" in e for e in entries), entries


def test_checksums_file(tmp_path, monkeypatch):
    mod = _load_build_module()
    dist = tmp_path / "dist"
    dist.mkdir()
    zip_path = dist / "a.zip"
    zip_path.write_bytes(b"hello")
    monkeypatch.setattr(mod, "DIST", dist)

    out = mod.checksums([zip_path])
    text = out.read_text(encoding="utf-8")
    digest = hashlib.sha256(b"hello").hexdigest()
    assert f"{digest}  a.zip" in text
