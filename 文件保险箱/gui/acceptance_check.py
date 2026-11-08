"""安装包/源码的"真实参数"验收：默认 KDF + 默认分块大小，端到端跑一遍。

和 `--selftest` 的区别：
- selftest 为了快，用测试 KDF（FDVAULT_KDF_FAST）和小分块，检查组件能不能用；
- acceptance 用**默认参数**（Argon2id 64 MiB、4 MiB 分块），检查真实使用路径：
  创建 → 解锁 → 字节比对 → 上锁 → 密保答案解锁 → 审计链 → 权限位 → 明文残留扫描。

用法：
    文件保险箱 --acceptance
输出结尾 "ACCEPTANCE PASS：N 项"，退出码 0；失败 1。
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

PASSWORD = "246810"
QUESTIONS = [("我的生日", "1998-03-12"), ("我出生的城市", "北京"), ("母亲的生日", "1970-01-02")]
MARKER = "ACCEPTANCE-PLAINTEXT-MARKER-7f3a9c"
_ok = 0
_failed: list[str] = []
_lines: list[str] = []
_report_path: str | None = None


def _say(text: str) -> None:
    _lines.append(text)
    try:
        print(text, flush=True)
    except Exception:  # noqa: BLE001  Windows --windowed 没有 stdout
        pass


def _check(name: str, fn) -> None:
    global _ok
    try:
        fn()
    except Exception:  # noqa: BLE001
        import traceback

        _failed.append(name)
        _say(f"  ✗ {name}")
        _say(traceback.format_exc())
    else:
        _ok += 1
        _say(f"  ✓ {name}")


def _digest(root: Path) -> dict:
    from core import constants as C

    out = {}
    skip = {C.VAULT_DIRNAME, C.MARKER_NAME}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if rel in skip or rel.startswith(C.VAULT_DIRNAME + "/"):
            continue
        if p.is_dir():
            out[rel] = ("dir", None)
        else:
            st = p.stat()
            out[rel] = ("file", hashlib.sha256(p.read_bytes()).hexdigest(), st.st_size)
    return out


def _search_vault(vault_dir: Path, needles: list[bytes]) -> list[str]:
    hits = []
    for p in vault_dir.rglob("*"):
        if not p.is_file():
            continue
        data = p.read_bytes()
        for needle in needles:
            if needle in data:
                hits.append(f"{p.relative_to(vault_dir)} 含 {needle[:36]!r}")
    return hits


def _make_tree(root: Path) -> dict:
    from core import constants as C  # noqa: F401

    (root / "患者 名单").mkdir(parents=True)
    (root / "患者 名单" / "张三.txt").write_text(f"姓名：张三\n{MARKER}\n", encoding="utf-8")
    (root / "患者 名单" / "emoji 😀 文件.txt").write_text("emoji 文件名\n", encoding="utf-8")
    (root / "大文件.bin").write_bytes(b"BIG-" + os.urandom(9 * 1024 * 1024))
    (root / "空文件.txt").write_bytes(b"")
    deep = root
    for i in range(8):
        deep = deep / f"第{i + 1}层"
    deep.mkdir(parents=True, exist_ok=True)
    (deep / "深路径.txt").write_text("deep\n", encoding="utf-8")
    (root / "空目录" / "子目录").mkdir(parents=True)
    return _digest(root)


def _finish(code: int) -> int:
    if _report_path:
        try:
            out = Path(_report_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n".join(_lines) + "\n", encoding="utf-8")
        except OSError:
            pass
    return code


def run(report_path: str | None = None) -> int:
    global _report_path
    _report_path = report_path
    from core.audit import AuditLog
    from core import constants as C
    from core.vault import Vault

    tmp = Path(tempfile.mkdtemp(prefix="fdvault-acceptance-"))
    root = tmp / "验收 数据"
    root.mkdir()
    before = _make_tree(root)
    audit_dir = tmp / "audit"

    _say("== 真实参数端到端 ==")

    def _native_engine():
        from core import native

        info = native.info()
        assert info["engine"] == "C++", f"C++ 加解密引擎没加载，退回了 Python：{info.get('reason')}"
        assert info["selftest"] == "ok", f"C++ 引擎自检：{info['selftest']}"
        _say(f"    引擎：{info['impl']}")

    _check("C++ 加解密引擎已加载且自检通过", _native_engine)

    def _create():
        v = Vault(root, audit_dir=audit_dir)
        info = v.create(PASSWORD, QUESTIONS)
        assert info["questions"] == [q for q, _a in QUESTIONS]
        assert Vault(root).status()["state"] == C.LOCKED
        assert info["stats"]["files"] >= 5
        v.close()

    _check("创建保险箱（默认 KDF/分块）", _create)

    def _unlock_roundtrip():
        v = Vault(root, audit_dir=audit_dir)
        v.unlock(PASSWORD)
        try:
            assert _digest(root) == before, "解锁后字节/目录不一致"
        finally:
            v.lock()
            v.close()
        assert Vault(root).status()["state"] == C.LOCKED

    _check("解锁→字节/目录一致→上锁", _unlock_roundtrip)

    def _no_plaintext_residue():
        needles = [MARKER.encode(), "张三".encode(), "大文件.bin".encode(), "emoji 😀 文件.txt".encode()]
        hits = _search_vault(root / C.VAULT_DIRNAME, needles)
        assert not hits, f"密文目录里有明文残留：{hits[:5]}"
        leftovers = [p.name for p in root.iterdir() if p.name not in (C.VAULT_DIRNAME, C.MARKER_NAME)]
        assert not leftovers, f"上锁后根目录有多余文件：{leftovers}"

    _check("密文目录无明文/文件名/多余文件", _no_plaintext_residue)

    def _protected():
        from core import protect

        if not protect.enabled():
            return
        assert Vault(root).status()["protected"], "上锁后没有加上防移动/删除保护"
        leaked = protect.probe(root, tmp)
        assert not leaked, f"上锁后这些操作没被拦住：{leaked}"
        assert Vault(root).status()["state"] == C.LOCKED

    _check("上锁后不能移动 / 改名 / 删除", _protected)

    def _answer_unlock():
        v = Vault(root, audit_dir=audit_dir)
        v.unlock(answers=[None, "北京市", None])  # 只答对第 2 个问题
        try:
            assert _digest(root) == before
        finally:
            v.lock()
            v.close()

    _check("答对一个密保问题即可解锁（默认参数）", _answer_unlock)

    def _audit_chain():
        log = AuditLog(audit_dir / "audit.jsonl")
        ok, count, detail = log.verify()
        assert ok and count >= 3, (ok, count, detail)
        events = {r["event"] for r in log.records()}
        assert {"CREATE", "UNLOCK", "LOCK"} <= events, events

    _check("审计日志哈希链完整", _audit_chain)

    def _permissions():
        if os.name != "posix":
            return
        assert (root / C.VAULT_DIRNAME).stat().st_mode & 0o777 == 0o700
        assert (root / C.VAULT_DIRNAME / "header.json").stat().st_mode & 0o777 == 0o600
        assert (root / C.VAULT_DIRNAME / "manifest.db").stat().st_mode & 0o777 == 0o600

    _check("权限位（POSIX）", _permissions)

    from core import protect

    protect.release_tree(tmp)  # 撤掉保护，临时目录才能被系统清理
    _say("")
    if _failed:
        _say(f"ACCEPTANCE FAIL：{_ok} 通过，{len(_failed)} 失败 -> {_failed}")
        return _finish(1)
    _say(f"ACCEPTANCE PASS：{_ok} 项全部通过")
    return _finish(0)


if __name__ == "__main__":
    sys.exit(run())
