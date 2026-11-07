"""和「文件保险箱」联动：锁着的保险箱拒绝开始；处理期间留“正在使用”标记；输出放在保险箱里给提醒。

保险箱用一个最小的假结构模拟：文件夹/.vault/header.json 里只写 state。
"""

import hashlib
import importlib
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import make_fake_data  # noqa: E402
from deid import vault_link  # noqa: E402
from deid.pipeline import Job  # noqa: E402


def fake_vault(folder, state="UNLOCKED"):
    folder = Path(folder)
    (folder / ".vault").mkdir(parents=True, exist_ok=True)
    (folder / ".vault" / "header.json").write_text(json.dumps({"format": "fdv1", "state": state}), encoding="utf-8")
    return folder


def marker(folder):
    return json.loads((Path(folder) / ".vault" / vault_link.MARKER).read_text(encoding="utf-8"))


def snapshot(folder):
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(folder).rglob("*")) if p.is_file()}


def test_find_vault(tmp_path):
    v = fake_vault(tmp_path / "箱")
    assert vault_link.find_vault(v) == v.resolve()
    assert vault_link.find_vault(v / "映射表" / "还没建.xlsx") == v.resolve()   # 路径可以还不存在
    assert vault_link.find_vault(tmp_path / "别处") is None
    assert vault_link.find_vault("") is None


@pytest.mark.parametrize("state,why", [("LOCKED", "已上锁"), ("LOCKING", "正在上锁"),
                                       ("UNLOCKING", "正在解锁"), ("INCONSISTENT", "中断"), ("???", "读不出来")])
def test_mapping_in_locked_vault_refused(tmp_path, state, why):
    """映射表所在保险箱锁着时映射表“不存在”，绝不能新建一张空表（编号会从 P0001 重来）。"""
    make_fake_data.main(tmp_path / "input")
    box = fake_vault(tmp_path / "映射表保险箱", state)
    mp = box / "脱敏映射表_勿外发.xlsx"
    logs = []
    ok = Job(tmp_path / "input", tmp_path / "out", mp, log=logs.append).run()
    assert not ok
    text = "\n".join(logs)
    assert "映射表在保险箱「映射表保险箱」里" in text and why in text and "先在「文件保险箱」里解锁" in text
    assert not mp.exists() and not (box / "映射表备份").exists() and not (tmp_path / "out").exists()


def test_input_in_locked_vault_refused(tmp_path):
    box = fake_vault(tmp_path / "原始数据", "LOCKED")   # 上锁后患者文件夹都不在了
    err = Job(box, tmp_path / "out", tmp_path / "m.xlsx", log=lambda m: None).check_paths()
    assert err and "原始数据文件夹在保险箱「原始数据」里" in err and "已上锁" in err


def test_output_in_locked_vault_refused(tmp_path):
    make_fake_data.main(tmp_path / "input")
    box = fake_vault(tmp_path / "箱", "LOCKED")
    err = Job(tmp_path / "input", box / "out", tmp_path / "m.xlsx", log=lambda m: None).check_paths()
    assert err and "输出文件夹在保险箱「箱」里" in err


def test_run_inside_unlocked_vault_marks_in_use(tmp_path):
    """原始数据和映射表都在已解锁的保险箱里：照常脱敏；处理期间标记 active，结束改成 active=false。"""
    box = fake_vault(tmp_path / "保密数据")
    make_fake_data.main(box / "原始数据")
    (box / "如何解锁.txt").write_text("保险箱说明", encoding="utf-8")
    before = snapshot(box / "原始数据")
    mp = box / "映射表" / "map.xlsx"
    seen = {}

    def confirm(text):
        seen["plan"] = text
        seen["marker"] = marker(box)
        return True

    ok = Job(box / "原始数据", tmp_path / "发出去", mp, log=lambda m: None, confirm=confirm, workers=2).run()
    assert ok
    m = seen["marker"]
    assert m["active"] is True and m["tool"] == vault_link.TOOL_NAME and m["what"] == "原始数据、映射表"
    assert abs(m["heartbeat"] - time.time()) < 120
    assert "[保险箱] 原始数据、映射表在保险箱「保密数据」里" in seen["plan"]
    assert "[注意] 输出文件夹在保险箱" not in seen["plan"]
    end = marker(box)
    assert end["active"] is False and end["ended_at"]
    assert snapshot(box / "原始数据") == before
    assert mp.exists() and len(list((tmp_path / "发出去").rglob("*.dcm"))) == 19
    assert not list((box / ".vault").glob("*.tmp"))


def test_vault_dir_inside_input_not_treated_as_patient(tmp_path):
    """保险箱选的正好是原始数据文件夹：.vault 不能被当成一个患者。"""
    make_fake_data.main(tmp_path / "input")
    box = fake_vault(tmp_path / "input")
    logs = []
    assert Job(box, tmp_path / "out", tmp_path / "local" / "m.xlsx", log=logs.append).run()
    text = "\n".join(logs)
    assert ".vault" not in text.split("== 扫描")[1].split("预计输出")[0]
    assert marker(box)["active"] is False and marker(box)["what"] == "原始数据"


def test_output_in_unlocked_vault_warns(tmp_path):
    make_fake_data.main(tmp_path / "input")
    box = fake_vault(tmp_path / "箱")
    seen = {}

    def confirm(text):
        seen["plan"] = text
        return False

    Job(tmp_path / "input", box / "out", tmp_path / "m.xlsx", log=lambda m: None, confirm=confirm).run()
    assert "[注意] 输出文件夹在保险箱「箱」里" in seen["plan"]
    assert marker(box)["active"] is False   # 取消也要把标记收回


def test_no_vault_no_marker(tmp_path):
    make_fake_data.main(tmp_path / "input")
    logs = []
    assert Job(tmp_path / "input", tmp_path / "out", tmp_path / "m.xlsx", log=logs.append).run()
    assert not any("保险箱" in line for line in logs)
    assert not list(tmp_path.rglob(vault_link.MARKER))


def test_heartbeat_refreshes(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_link, "HEARTBEAT_SECONDS", 0.2)
    box = fake_vault(tmp_path / "箱")
    with vault_link.InUse({box: ["原始数据"]}, log=lambda m: None):
        first = marker(box)["heartbeat"]
        time.sleep(0.7)
        assert marker(box)["heartbeat"] > first
    assert marker(box)["active"] is False


def test_marker_write_failure_only_warns(tmp_path):
    logs = []
    with vault_link.InUse({tmp_path / "不存在": ["映射表"]}, log=logs.append):
        pass
    assert len(logs) == 1 and "没能在保险箱「不存在」里留下“正在使用”标记" in logs[0]


def test_gui_path_hints(tmp_path):
    from deid.gui import check_paths
    make_fake_data.main(tmp_path / "input")
    locked = fake_vault(tmp_path / "锁着", "LOCKED")
    opened = fake_vault(tmp_path / "开着")
    res, layout, single = check_paths(str(tmp_path / "input"), str(opened / "out"), str(locked / "m.xlsx"))
    assert layout.mode == "batch" and single is False
    assert res[0][0] == "ok"
    assert res[1][0] == "warn" and "锁回时脱敏结果也会被加密" in res[1][1]
    assert res[2][0] == "error" and "已上锁" in res[2][1]
    res, _, _ = check_paths(str(tmp_path / "input"), str(tmp_path / "out"), str(opened / "m.xlsx"))
    assert res[2][0] == "ok" and "处理期间不会自动上锁" in res[2][1]
    # 输出和原始数据互相包含这种错误，不能被“在保险箱里”的提醒盖掉
    res, _, _ = check_paths(str(opened), str(opened / "sub"), "")
    assert res[1][0] == "error" and "互相包含" in res[1][1]


def test_vault_app_reads_our_marker(tmp_path):
    """两边的标记约定一致：用保险箱自己的代码读本软件写的标记（同仓库里有保险箱时才跑）。"""
    vault_project = ROOT.parent / "文件保险箱"
    if not (vault_project / "core" / "inuse.py").is_file():
        pytest.skip("同级目录没有文件保险箱")
    sys.path.insert(0, str(vault_project))
    try:
        inuse = importlib.import_module("core.inuse")
    finally:
        sys.path.remove(str(vault_project))
    box = fake_vault(tmp_path / "箱")
    with vault_link.InUse({box: ["原始数据", "映射表"]}, log=lambda m: None):
        users = inuse.active_users(box)
        assert len(users) == 1 and "CT 数据脱敏工具" in inuse.describe(users)
    assert inuse.active_users(box) == []
