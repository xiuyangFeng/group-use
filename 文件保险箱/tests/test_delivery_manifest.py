"""交付清单生成：空项目不崩、有产物时列出 SHA 与证据。"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import delivery_manifest as dm


def test_manifest_empty_project(tmp_path):
    md = dm.build_manifest(tmp_path)
    assert "交付清单" in md
    assert "安装包" in md
    assert "还不能自动验证" in md


def test_manifest_with_artifacts_and_reports(tmp_path):
    (tmp_path / "dist").mkdir()
    artifact = tmp_path / "dist" / "文件保险箱-0.3.0-macOS-arm64.zip"
    artifact.write_bytes(b"fake package")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "bench-10k-10gb.json").write_text(
        json.dumps({"files": 10000, "unlock_sec": 44.3, "lock_sec": 53.3, "verify_sec": 22.4}),
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "验收清单.md").write_text("| 1 | ✅ |\n| 2 | ✅ |\n| 3 | ☑ |\n", encoding="utf-8")

    md = dm.build_manifest(tmp_path)
    assert "文件保险箱-0.3.0-macOS-arm64.zip" in md
    assert dm.sha256(artifact) in md
    assert "解锁 44.3 s" in md
    assert "通过 ✅ 2 项" in md and "真机待确认 ☑ 1 项" in md
