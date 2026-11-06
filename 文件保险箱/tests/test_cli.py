"""CLI 层测试：参数、JSON 输出、退出码。"""

from __future__ import annotations

import json

from tests.conftest import PASSWORD, QUESTIONS_STDIN, make_tree, run_cli


def test_version_and_help():
    r = run_cli(["--version"], audit_dir=None)
    assert r.returncode == 0
    assert "文件保险箱" in r.stdout
    r = run_cli(["--help"], audit_dir=None)
    assert r.returncode == 0
    assert "create" in r.stdout


def test_cli_version_matches_gui():
    from core import __version__ as core_v

    from gui import __version__ as gui_v

    assert core_v == gui_v == "0.6.1"


def test_cli_survives_non_utf8_console(tmp_path, audit_dir):
    """Windows 控制台常是 cp1252/cp936：打印中文和 ✓ 不能 UnicodeEncodeError（CI 抓到的真 bug）。"""
    env = {"PYTHONIOENCODING": "cp1252"}
    r = run_cli(["--version"], audit_dir=None, env_extra=env)
    assert r.returncode == 0, r.stderr
    assert "文件保险箱 0.6.1" in r.stdout

    root = tmp_path / "data"
    make_tree(root)
    r = run_cli(
        ["create", str(root), "--password-stdin", "--yes", "--no-progress"],
        stdin=PASSWORD + "\n" + QUESTIONS_STDIN,
        audit_dir=audit_dir,
        env_extra=env,
    )
    assert r.returncode == 0, r.stderr
    assert "✔" in r.stdout  # 原来在 cp1252 下这里会崩


def test_create_json_and_status(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    r = run_cli(
        ["create", str(root), "--password-stdin", "--yes", "--no-progress", "--json"],
        stdin=PASSWORD + "\n" + QUESTIONS_STDIN,
        audit_dir=audit_dir,
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["ok"] is True
    assert data["questions"] == ["我的生日", "我出生的城市", "母亲的生日"]
    assert data["files"] == 6

    r = run_cli(["status", str(root), "--json"], audit_dir=audit_dir)
    assert r.returncode == 0, r.stderr
    st = json.loads(r.stdout)
    assert st["state"] == "LOCKED"
    assert st["file_count"] == 6
    assert st["needs_recovery"] is False


def test_cli_cycle(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    assert run_cli(["create", str(root), "--password-stdin", "--yes", "--no-progress"], stdin=PASSWORD + "\n" + QUESTIONS_STDIN, audit_dir=audit_dir).returncode == 0
    r = run_cli(["unlock", str(root), "--password-stdin", "--no-progress"], stdin=PASSWORD + "\n", audit_dir=audit_dir)
    assert r.returncode == 0, r.stderr
    r = run_cli(["verify", str(root), "--password-stdin", "--no-progress"], stdin=PASSWORD + "\n", audit_dir=audit_dir)
    assert r.returncode == 1  # 解锁状态不能校验
    r = run_cli(["lock", str(root), "--password-stdin", "--no-progress"], stdin=PASSWORD + "\n", audit_dir=audit_dir)
    assert r.returncode == 0, r.stderr
    r = run_cli(["verify", str(root), "--password-stdin", "--no-progress", "--json"], stdin=PASSWORD + "\n", audit_dir=audit_dir)
    assert r.returncode == 0, r.stderr
    result = json.loads(r.stdout)
    assert result["ok"] is True


def test_cli_wrong_password_exit_3(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    assert run_cli(["create", str(root), "--password-stdin", "--yes", "--no-progress"], stdin=PASSWORD + "\n" + QUESTIONS_STDIN, audit_dir=audit_dir).returncode == 0
    r = run_cli(["unlock", str(root), "--password-stdin", "--no-progress"], stdin="000000\n", audit_dir=audit_dir)
    assert r.returncode == 3
    assert "密码不对" in r.stderr


def test_cli_not_a_vault(tmp_path, audit_dir):
    root = tmp_path / "data"
    root.mkdir()
    r = run_cli(["status", str(root)], audit_dir=audit_dir)
    assert r.returncode == 1
    assert "不是保险箱" in r.stderr


def test_cli_recovery_needed_exit_5(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    assert run_cli(["create", str(root), "--password-stdin", "--yes", "--no-progress"], stdin=PASSWORD + "\n" + QUESTIONS_STDIN, audit_dir=audit_dir).returncode == 0
    r = run_cli(
        ["unlock", str(root), "--password-stdin", "--no-progress"],
        stdin=PASSWORD + "\n",
        audit_dir=audit_dir,
        env_extra={"FDVAULT_CRASH_AT": "unlock:after_rename=1"},
    )
    assert r.returncode == 70
    r = run_cli(["lock", str(root), "--password-stdin", "--no-progress"], stdin=PASSWORD + "\n", audit_dir=audit_dir)
    assert r.returncode == 5
    assert "recover" in r.stderr
