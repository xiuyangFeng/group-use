"""诊断包：只读本地信息，生成 zip；不联网、不碰保险箱内容。"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def test_diagnostics_zip(tmp_path):
    home = tmp_path / "home"
    data = home / "Library" / "Application Support" / "文件保险箱"
    data.mkdir(parents=True)
    (data / "config.json").write_text('{"auto_lock_minutes": 15}', encoding="utf-8")
    (data / "gui.log").write_text("line1\nline2\n最后一行错误\n", encoding="utf-8")
    (data / "audit.jsonl").write_text('{"event":"CREATE"}\n{"event":"LOCK"}\n', encoding="utf-8")

    out = tmp_path / "out"
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "QT_QPA_PLATFORM": "offscreen",
            "FDVAULT_NO_OPEN": "1",
            "PYTHONPATH": str(PROJECT),
        }
    )
    proc = subprocess.run(
        [sys.executable, str(PROJECT / "run_gui.py"), "--diagnostics", "--out", str(out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env=env,
        cwd=str(PROJECT),
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    zips = list(out.glob("文件保险箱诊断-*.zip"))
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0]) as zf:
        names = set(zf.namelist())
        assert {"说明.txt", "环境信息.txt", "config.json", "gui.log", "audit.jsonl"} <= names
        note = zf.read("说明.txt").decode("utf-8")
        assert "不含：密码、恢复密钥" in note
        assert "最后一行错误" in zf.read("gui.log").decode("utf-8")
        assert "CREATE" in zf.read("audit.jsonl").decode("utf-8")
        env_text = zf.read("环境信息.txt").decode("utf-8")
        assert "文件保险箱" in env_text
        assert "数据目录" in env_text
