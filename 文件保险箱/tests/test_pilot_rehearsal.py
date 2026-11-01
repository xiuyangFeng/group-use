"""试点彩排脚本：假数据 8 步全过，报告 JSON 可被 M4 引用。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def test_pilot_rehearsal(tmp_path):
    report = tmp_path / "pilot-rehearsal.json"
    env = dict(os.environ)
    env.update({"FDVAULT_KDF_FAST": "1", "PYTHONPATH": str(PROJECT)})
    # 测试套件里默认跳占用检查（省时间），彩排脚本要真跑 lsof，所以去掉
    env.pop("FDVAULT_SKIP_BUSY_CHECK", None)
    proc = subprocess.run(
        [sys.executable, str(PROJECT / "scripts" / "pilot_rehearsal.py"), "--report", str(report)],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        cwd=str(PROJECT),
    )
    assert proc.returncode == 0, (proc.stdout[-4000:], proc.stderr[-4000:])
    assert "PILOT REHEARSAL PASS" in proc.stdout
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert len(data["steps"]) == 8
    assert all(s["ok"] for s in data["steps"])
