"""试点彩排：用假数据把《试点方案》里的关键流程在 1 个脚本里跑一遍。

不接触真实患者数据；生成临时文件夹（含中文名/大文件/空文件/子目录），
依次演练：建库 → 解锁/上锁 → 改文件 → 恢复密钥重设密码 → 文件占用时上锁中止
→ 审计导出 → 明文残留扫描 → 最终解锁校验。
结果写到 reports/pilot-rehearsal.json，供 M4 试点报告引用。

用法：
    python scripts/pilot_rehearsal.py            # 跑完自动清理临时目录
    python scripts/pilot_rehearsal.py --keep     # 保留临时目录（排查用）
    python scripts/pilot_rehearsal.py --report 报告路径.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from core.console import setup_utf8_stdio  # noqa: E402

os.environ.setdefault("FDVAULT_KDF_FAST", "1")  # 彩排只验证流程，不测 Argon2 耗时
os.environ.setdefault("FDVAULT_NO_PROTECT", "1")  # 临时目录里的保险箱不加防删除保护，用完能直接删

PASSWORD = "Pilot-Password-1234"
NEW_PASSWORD = "Pilot-Password-5678"
MARKER = "PILOT-REHEARSAL-PLAINTEXT-MARKER-c41d"

_steps: list[dict] = []


def _step(name: str):
    def deco(fn):
        def wrapper(*args, **kwargs):
            started = time.time()
            try:
                detail = fn(*args, **kwargs)
                _steps.append({"step": name, "ok": True, "seconds": round(time.time() - started, 2), "detail": detail})
                print(f"  ✓ {name}" + (f"（{detail}）" if detail else ""), flush=True)
            except Exception as exc:  # noqa: BLE001
                import traceback

                _steps.append(
                    {
                        "step": name,
                        "ok": False,
                        "seconds": round(time.time() - started, 2),
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                )
                print(f"  ✗ {name}：{type(exc).__name__}: {exc}", flush=True)
                traceback.print_exc()
                raise

        return wrapper

    return deco


def _digest(root: Path) -> dict:
    from core import constants as C

    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if rel == C.VAULT_DIRNAME or rel.startswith(C.VAULT_DIRNAME + "/") or rel == C.MARKER_NAME:
            continue
        if p.is_dir():
            out[rel] = ("dir", None)
        else:
            out[rel] = ("file", hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_size)
    return out


def _make_data(root: Path) -> dict:
    (root / "知情同意").mkdir(parents=True)
    (root / "知情同意" / "受试者 001 同意书.txt").write_text(f"假数据\n{MARKER}\n", encoding="utf-8")
    (root / "知情同意" / "版本 2 😀.txt").write_text("emoji 文件名\n", encoding="utf-8")
    (root / "伦理审查-记录.txt").write_text("伦理审查记录 假数据\n", encoding="utf-8")
    (root / "大文件.bin").write_bytes(b"PILOT-" + os.urandom(2 * 1024 * 1024))
    (root / "空文件.txt").write_bytes(b"")
    (root / "空目录").mkdir()
    return _digest(root)


@_step("建库并确认状态")
def step_create(root: Path, ctx: dict) -> str:
    from core import constants as C
    from core.vault import Vault

    v = Vault(root, audit_dir=ctx["audit_dir"])
    info = v.create(PASSWORD)
    ctx["recovery_key"] = info["recovery_key"]
    v.close()
    state = Vault(root).status()["state"]
    assert state == C.LOCKED, state
    return f"{info['stats']['files']} 个文件"


@_step("连续 2 次解锁/上锁，字节一致")
def step_roundtrip(root: Path, ctx: dict) -> str:
    from core import constants as C
    from core.vault import Vault

    for i in range(2):
        v = Vault(root, audit_dir=ctx["audit_dir"])
        v.unlock(PASSWORD)
        assert _digest(root) == ctx["before"], f"第 {i + 1} 轮解锁不一致"
        v.lock()
        v.close()
        assert Vault(root).status()["state"] == C.LOCKED
    return "2 轮"


@_step("解锁后改文件/加文件/删文件")
def step_modify(root: Path, ctx: dict) -> str:
    from core.vault import Vault

    v = Vault(root, audit_dir=ctx["audit_dir"])
    v.unlock(PASSWORD)
    (root / "伦理审查-记录.txt").write_text("改过的内容\n", encoding="utf-8")
    (root / "新增文件.txt").write_text("新增\n", encoding="utf-8")
    (root / "空文件.txt").unlink()
    v.lock()
    v.close()

    v = Vault(root, audit_dir=ctx["audit_dir"])
    v.unlock(PASSWORD)
    assert (root / "伦理审查-记录.txt").read_text(encoding="utf-8") == "改过的内容\n"
    assert (root / "新增文件.txt").exists()
    assert not (root / "空文件.txt").exists()
    v.lock()
    v.close()
    return "3 处变更"


@_step("恢复密钥重设密码，旧密码失效")
def step_recovery_root(root: Path, ctx: dict) -> str:
    from core.crypto import recovery_key_parse
    from core.errors import PasswordError
    from core.vault import Vault

    key = recovery_key_parse(ctx["recovery_key"])
    v = Vault(root, audit_dir=ctx["audit_dir"])
    v.reset_with_recovery_key(key, NEW_PASSWORD)
    v.close()
    v = Vault(root, audit_dir=ctx["audit_dir"])
    try:
        v.unlock(PASSWORD)
    except PasswordError:
        pass
    else:
        raise AssertionError("旧密码仍然可以解锁")
    v.unlock(NEW_PASSWORD)
    v.lock()
    v.close()
    ctx["password"] = NEW_PASSWORD
    return "旧密码已失效"


@_step("文件被占用时上锁中止")
def step_busy(root: Path, ctx: dict) -> str:
    from core.errors import VaultError
    from core.vault import Vault

    v = Vault(root, audit_dir=ctx["audit_dir"])
    v.unlock(ctx["password"])
    if sys.platform != "darwin":
        # 占用检测在 Windows 上走共享冲突、Linux 不启用；彩排只在 macOS 断言
        v.lock()
        v.close()
        return "非 macOS：跳过占用断言（真机检查记录里手工验证）"
    target = root / "大文件.bin"
    holder = subprocess.Popen(
        [sys.executable, "-c", f"import time; f=open({str(target)!r}, 'rb'); time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.6)
        try:
            v.lock()
        except VaultError as exc:
            msg = str(exc)
            if "正在被其他程序使用" not in msg and "占用" not in msg:
                raise AssertionError(f"提示不清楚：{msg}")
        else:
            raise AssertionError("文件被占用时上锁没有中止")
    finally:
        holder.kill()
        holder.wait()
    v.lock()
    v.close()
    return "中止后重试成功"


@_step("审计链校验 + CSV 导出")
def step_audit(root: Path, ctx: dict) -> str:
    from core.audit import AuditLog

    log = AuditLog(ctx["audit_dir"] / "audit.jsonl")
    ok, count, detail = log.verify()
    assert ok, detail
    csv = log.export_csv(ctx["audit_dir"] / "export.csv")
    raw = csv.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "CSV 没有 UTF-8 BOM"
    assert "操作人（系统账户）" in csv.read_text(encoding="utf-8-sig")
    return f"{count} 条记录"


@_step("密文目录无明文残留")
def step_residue(root: Path, ctx: dict) -> str:
    from core import constants as C

    needles = [MARKER.encode(), "受试者 001 同意书.txt".encode(), "知情同意".encode()]
    vault_dir = root / C.VAULT_DIRNAME
    for p in vault_dir.rglob("*"):
        if not p.is_file():
            continue
        data = p.read_bytes()
        for needle in needles:
            if needle in data:
                raise AssertionError(f"{p.relative_to(vault_dir)} 含明文 {needle!r}")
    leftover = [p.name for p in root.iterdir() if p.name not in (C.VAULT_DIRNAME, C.MARKER_NAME)]
    assert not leftover, f"根目录有多余文件：{leftover}"
    return "0 命中"


@_step("最终解锁校验并保持上锁")
def step_final(root: Path, ctx: dict) -> str:
    from core import constants as C
    from core.vault import Vault

    v = Vault(root, audit_dir=ctx["audit_dir"])
    v.unlock(ctx["password"])
    now = _digest(root)
    assert (root / "伦理审查-记录.txt").read_text(encoding="utf-8") == "改过的内容\n"
    v.lock()
    v.close()
    assert Vault(root).status()["state"] == C.LOCKED
    assert (root / C.VAULT_DIRNAME).exists()
    return f"{len(now)} 个条目"


def main() -> int:
    setup_utf8_stdio()
    parser = argparse.ArgumentParser(description="试点流程彩排（假数据）")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    parser.add_argument("--report", default=str(PROJECT / "reports" / "pilot-rehearsal.json"))
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="fdvault-pilot-"))
    root = tmp / "试点 数据"
    root.mkdir()
    ctx = {"audit_dir": tmp / "audit", "before": _make_data(root)}
    print("== 试点彩排（假数据，不接触真实患者数据） ==")
    try:
        step_create(root, ctx)
        step_roundtrip(root, ctx)
        step_modify(root, ctx)
        step_recovery_root(root, ctx)
        step_busy(root, ctx)
        step_audit(root, ctx)
        step_residue(root, ctx)
        step_final(root, ctx)
    except Exception:  # noqa: BLE001
        pass
    finally:
        report = {
            "ok": all(s["ok"] for s in _steps) and len(_steps) == 8,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "steps": _steps,
            "seconds": round(sum(s["seconds"] for s in _steps), 2),
        }
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n报告：{out}")
        if args.keep:
            print(f"临时目录（保留）：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    print()
    if report["ok"]:
        print(f"PILOT REHEARSAL PASS：8 步全部通过（{report['seconds']} s）")
        return 0
    failed = [s["step"] for s in _steps if not s["ok"]]
    print(f"PILOT REHEARSAL FAIL：{failed}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
