"""产品改名工具：默认预演不改；--apply 才改，且跳过 dist/.venv。"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
OLD = "文件保险箱"
NEW = "安全数据箱"


def _make_tree(root: Path) -> None:
    (root / "gui").mkdir(parents=True)
    (root / "gui" / "app.py").write_text(f'NAME = "{OLD}"\n', encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "说明.md").write_text(f"# {OLD} 使用说明\n", encoding="utf-8")
    wf = root / "installers" / f"用{OLD}上锁.workflow" / "Contents"
    wf.mkdir(parents=True)
    (wf / "document.wflow").write_text(f"<string>{OLD}</string>", encoding="utf-8")
    (root / "assets").mkdir()
    (root / "assets" / f"{OLD}.icns").write_bytes(b"\x00\x01binary")
    (root / "dist").mkdir()
    (root / "dist" / "old.zip").write_bytes(f"{OLD}".encode())
    (root / ".venv").mkdir()
    (root / ".venv" / "lib.py").write_text(OLD, encoding="utf-8")


def _run(root: Path, *extra: str):
    return subprocess.run(
        [sys.executable, str(PROJECT / "scripts" / "rename_product.py"), "--root", str(root), "--new", NEW, *extra],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_dry_run_changes_nothing(tmp_path):
    _make_tree(tmp_path)
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "这是预演" in proc.stdout
    assert (tmp_path / "gui" / "app.py").read_text(encoding="utf-8") == f'NAME = "{OLD}"\n'
    assert (tmp_path / "assets" / f"{OLD}.icns").exists()


def test_apply_renames_files_and_content(tmp_path):
    _make_tree(tmp_path)
    proc = _run(tmp_path, "--apply")
    assert proc.returncode == 0, proc.stderr
    assert NEW in (tmp_path / "gui" / "app.py").read_text(encoding="utf-8")
    assert (tmp_path / "installers" / f"用{NEW}上锁.workflow" / "Contents" / "document.wflow").exists()
    assert (tmp_path / "assets" / f"{NEW}.icns").exists()
    assert not (tmp_path / "assets" / f"{OLD}.icns").exists()
    # dist 和 .venv 不动
    assert (tmp_path / "dist" / "old.zip").read_bytes() == OLD.encode()
    assert (tmp_path / ".venv" / "lib.py").read_text(encoding="utf-8") == OLD
    # 有备份
    backups = list((tmp_path / ".build").glob("rename-backup-*.zip"))
    assert backups
    with zipfile.ZipFile(backups[0]) as zf:
        assert any(n.endswith("app.py") for n in zf.namelist())


def test_invalid_new_name_rejected(tmp_path):
    _make_tree(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(PROJECT / "scripts" / "rename_product.py"), "--root", str(tmp_path), "--new", "a/b"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0
    assert "不能用于文件夹" in (proc.stdout + proc.stderr)
