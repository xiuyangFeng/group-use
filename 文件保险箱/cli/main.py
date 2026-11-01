"""文件保险箱命令行（M1 用；M2 的 GUI 直接调用 core，不经过这里）。

用法见 `python -m cli.main --help`。
退出码：0 成功 / 1 一般错误 / 2 用法错误 / 3 密码错误 / 4 恢复密钥错误 / 5 需要先恢复 / 6 磁盘空间不足
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import __version__, constants as C, crypto, fsutil
from core.console import setup_utf8_stdio
from core.audit import AuditLog
from core.errors import (
    DiskSpaceError,
    PasswordError,
    RecoveryKeyError,
    RecoveryNeededError,
    VaultError,
)
from core.vault import Vault

EXIT = {
    "ok": 0,
    "error": 1,
    "usage": 2,
    "password": 3,
    "recovery": 4,
    "recover_needed": 5,
    "disk": 6,
}

STATE_LABEL = {
    C.LOCKED: "已上锁（只有密文）",
    C.UNLOCKED: "已解锁（明文可见，可以正常使用）",
    C.UNLOCKING: "上次解锁没有完成（需要恢复）",
    C.LOCKING: "上次上锁没有完成（需要恢复）",
    C.INCONSISTENT: "状态异常（需要人工检查）",
}


# ---------------------------------------------------------------- 输入输出

class SecretReader:
    def __init__(self, use_stdin: bool):
        self.use_stdin = use_stdin

    def ask(self, prompt: str, confirm: str | None = None) -> str:
        if self.use_stdin:
            line = sys.stdin.readline()
            if not line:
                raise VaultError("标准输入里没有可用的密码（--password-stdin）")
            return line.rstrip("\r\n")
        value = getpass.getpass(prompt)
        if confirm:
            again = getpass.getpass(confirm)
            if value != again:
                raise VaultError("两次输入不一致")
        return value


class ProgressPrinter:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.t0 = time.monotonic()
        self.last = 0.0
        self._line_len = 0

    def __call__(self, p: dict) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        final = p["phase"] in ("解锁完成", "上锁完成", "恢复完成")
        if not final and now - self.last < 0.1:
            return
        self.last = now
        total_files = p.get("total_files") or 0
        done_files = p.get("done_files") or 0
        total_bytes = p.get("total_bytes") or 0
        done_bytes = p.get("done_bytes") or 0
        elapsed = max(0.001, now - self.t0)
        if total_files:
            pct = int(100 * done_files / total_files)
        elif total_bytes:
            pct = int(100 * done_bytes / total_bytes)
        else:
            pct = 0
        speed = fsutil.human_bytes(int(done_bytes / elapsed)) + "/s" if done_bytes else ""
        text = f"\r{p['phase']}：{done_files}/{total_files} 个文件（{pct}%）{fsutil.human_bytes(done_bytes)}"
        if speed:
            text += f"  {speed}"
        pad = max(0, self._line_len - len(text))
        self._line_len = len(text)
        sys.stderr.write(text + " " * pad)
        sys.stderr.flush()

    def done(self) -> None:
        if self.enabled and self._line_len:
            sys.stderr.write("\n")
            sys.stderr.flush()
            self._line_len = 0


def print_recovery_key(recovery_key: str, fingerprint: str) -> None:
    bar = "=" * 62
    print()
    print(bar)
    print("  恢复密钥（只显示这一次，请立刻抄下来）")
    print()
    print(f"    {recovery_key}")
    print()
    print(f"  指纹：{fingerprint}（以后可以用它核对是不是这张纸）")
    print()
    print("  用途：忘记密码时用它重置密码。没有它，密码丢了就真的打不开了。")
    print("  建议：抄在纸上，你一份、导师一份；不要截图发微信/邮件。")
    print(bar)
    print()


def confirm_backup() -> bool:
    print("开始前请确认三件事：")
    print("  1. 这个文件夹里的重要数据已经另外备份过（移动硬盘 / 另一台电脑）；")
    print("  2. 密码是组里共用的，至少 12 位，建议用一句长口令；")
    print("  3. 恢复密钥会显示一次，需要抄在纸上保存好。")
    try:
        ans = input("都确认了请输入 YES 继续（其它任意键退出）：").strip()
    except EOFError:
        return False
    return ans == "YES"


def _vault(args) -> Vault:
    return Vault(args.folder, audit_dir=getattr(args, "audit_dir", None))


def _json_dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------- 子命令

def cmd_create(args) -> int:
    vault = _vault(args)
    if not args.yes and not args.password_stdin:
        if not confirm_backup():
            print("已取消，没有改动任何文件。")
            return EXIT["ok"]
    reader = SecretReader(args.password_stdin)
    password = reader.ask(
        f"设置密码（至少 {C.MIN_PASSWORD_LEN} 位）：",
        confirm=None if args.password_stdin else "再输一次：",
    )
    pp = ProgressPrinter(_progress_enabled(args))
    try:
        result = vault.create(password, progress=pp)
    finally:
        pp.done()

    stats = result["stats"]
    if args.json:
        _json_dump(
            {
                "ok": True,
                "recovery_key": result["recovery_key"],
                "recovery_fingerprint": result["recovery_fingerprint"],
                "files": stats["files"],
                "bytes": stats["bytes"],
            }
        )
        return EXIT["ok"]

    print(f"\n✔ 已创建并上锁：{vault.root}")
    print(f"  {stats['files']} 个文件，共 {fsutil.human_bytes(stats['bytes'])}")
    if stats.get("skipped"):
        print(f"  注意：跳过了 {len(stats['skipped'])} 个符号链接/特殊文件（不会加密它们）")
    print(f"  审计日志：{Path(args.audit_dir) / 'audit.jsonl' if args.audit_dir else fsutil.platform_data_dir() / 'audit.jsonl'}")
    print_recovery_key(result["recovery_key"], result["recovery_fingerprint"])
    print("把上面这串恢复密钥抄下来后，这个文件夹就可以交给同事了。")
    return EXIT["ok"]


def _maybe_recovery_prompt(vault: Vault, args) -> None:
    st = vault.status()
    if st["needs_recovery"]:
        print(f"提示：上次操作没有完成（状态：{STATE_LABEL.get(st['state'], st['state'])}）。")
        print("      请先运行：python -m cli.main recover <文件夹>")
        raise RecoveryNeededError(st["state"])


def cmd_unlock(args) -> int:
    vault = _vault(args)
    _maybe_recovery_prompt(vault, args)
    reader = SecretReader(args.password_stdin)
    pp = ProgressPrinter(_progress_enabled(args))
    try:
        if args.recovery_key:
            if args.password_stdin:
                line = sys.stdin.readline().rstrip("\r\n")
            else:
                line = getpass.getpass("恢复密钥：")
            key = crypto.recovery_key_parse(line)
            stats = vault.unlock(progress=pp, recovery_key=key)
        else:
            password = reader.ask("密码：")
            stats = vault.unlock(password=password, progress=pp)
    finally:
        pp.done()
    if args.json:
        _json_dump({"ok": True, **stats})
        return EXIT["ok"]
    print(f"✔ 已解锁：{vault.root}")
    print(f"  {stats['files']} 个文件 / {fsutil.human_bytes(stats['bytes'])}，现在可以正常使用这个文件夹。")
    print("  用完记得回来上锁（GUI 版会在 15 分钟没操作时自动上锁；命令行版没有自动上锁）。")
    return EXIT["ok"]


def cmd_lock(args) -> int:
    vault = _vault(args)
    _maybe_recovery_prompt(vault, args)
    reader = SecretReader(args.password_stdin)
    password = reader.ask("密码：")
    pp = ProgressPrinter(_progress_enabled(args))
    try:
        stats = vault.lock(password=password, progress=pp)
    finally:
        pp.done()
    if args.json:
        _json_dump({"ok": True, **stats})
        return EXIT["ok"]
    print(f"✔ 已上锁：{vault.root}")
    print(f"  {stats['files']} 个文件 / {fsutil.human_bytes(stats['bytes'])} 已加密保存。")
    if stats.get("skipped"):
        print(f"  注意：{len(stats['skipped'])} 个符号链接/特殊文件没有加密，仍以明文留在文件夹里。")
    return EXIT["ok"]


def cmd_status(args) -> int:
    vault = _vault(args)
    st = vault.status()
    if args.json:
        _json_dump(st)
        return EXIT["ok"]
    print(f"保险箱：{st['root']}")
    print(f"状态：  {STATE_LABEL.get(st['state'], st['state'])}")
    print(f"创建：  {st['created_at']}")
    print(f"内容：  {st['file_count']} 个文件 / {fsutil.human_bytes(st['total_bytes'])}")
    print(f"恢复密钥指纹：{st['recovery_fingerprint']}")
    print(f"审计日志：{vault._audit_dir / 'audit.jsonl'}")
    if st["needs_recovery"]:
        print()
        print("⚠ 上次操作没有完成，请运行：python -m cli.main recover <文件夹>")
    return EXIT["ok"]


def cmd_verify(args) -> int:
    vault = _vault(args)
    _maybe_recovery_prompt(vault, args)
    reader = SecretReader(args.password_stdin)
    password = reader.ask("密码：")
    pp = ProgressPrinter(_progress_enabled(args))
    try:
        result = vault.verify(password=password, progress=pp)
    finally:
        pp.done()
    if args.json:
        _json_dump(result)
        return EXIT["ok"] if result["ok"] else EXIT["error"]
    if result["ok"]:
        print(f"✔ 校验通过：{result['files']} 个文件 / {fsutil.human_bytes(result['bytes'])}，全部可以正常解密。")
        return EXIT["ok"]
    print(f"✘ 发现 {len(result['problems'])} 个问题：")
    for p in result["problems"]:
        print(f"  - {p['path']}：{p['reason']}")
    return EXIT["error"]


def cmd_recover(args) -> int:
    vault = _vault(args)
    st = vault.status()
    if not st["needs_recovery"]:
        print("这个保险箱没有需要恢复的操作。")
        return EXIT["ok"]
    print(f"检测到：{STATE_LABEL.get(st['state'], st['state'])}")
    if args.rollback:
        rollback = True
    elif not args.yes and not args.password_stdin:
        print("  1) 继续完成上次的操作（推荐）")
        print("  2) 回滚上次的操作（回到操作前的状态）")
        ans = input("请选择 [1/2]，默认 1：").strip() or "1"
        rollback = ans == "2"
    else:
        rollback = False

    reader = SecretReader(args.password_stdin)
    password = reader.ask("密码：")
    pp = ProgressPrinter(_progress_enabled(args))
    try:
        result = vault.recover(password=password, progress=pp, rollback=rollback)
    finally:
        pp.done()
    if args.json:
        _json_dump({"ok": True, **result})
        return EXIT["ok"]
    label = STATE_LABEL.get(result["state"], result["state"])
    print(f"✔ 恢复完成：{result['action']}，现在状态是「{label}」")
    print(f"  {result['files']} 个文件 / {fsutil.human_bytes(result['bytes'])}")
    return EXIT["ok"]


def cmd_change_password(args) -> int:
    vault = _vault(args)
    _maybe_recovery_prompt(vault, args)
    reader = SecretReader(args.password_stdin)
    old = reader.ask("原密码：")
    new = reader.ask(
        f"新密码（至少 {C.MIN_PASSWORD_LEN} 位）：",
        confirm=None if args.password_stdin else "再输一次：",
    )
    vault.change_password(old, new)
    print("✔ 密码已修改。恢复密钥没有变，仍然可以解锁。")
    return EXIT["ok"]


def cmd_reset_password(args) -> int:
    vault = _vault(args)
    _maybe_recovery_prompt(vault, args)
    reader = SecretReader(args.password_stdin)
    if args.password_stdin:
        line = sys.stdin.readline()
        if not line:
            raise VaultError("标准输入里没有恢复密钥")
        rec_text = line.strip()
    else:
        rec_text = getpass.getpass("恢复密钥：").strip()
    key = crypto.recovery_key_parse(rec_text)
    new = reader.ask(
        f"新密码（至少 {C.MIN_PASSWORD_LEN} 位）：",
        confirm=None if args.password_stdin else "再输一次：",
    )
    vault.reset_with_recovery_key(key, new)
    print("✔ 已用恢复密钥重置密码。之前的密码已失效，恢复密钥仍然有效。")
    return EXIT["ok"]


def cmd_regenerate_recovery(args) -> int:
    vault = _vault(args)
    _maybe_recovery_prompt(vault, args)
    reader = SecretReader(args.password_stdin)
    password = reader.ask("密码：")
    result = vault.regenerate_recovery_key(password)
    if args.json:
        _json_dump(result)
        return EXIT["ok"]
    print_recovery_key(result["recovery_key"], result["recovery_fingerprint"])
    print("旧的恢复密钥已经作废，请把新的抄下来。")
    return EXIT["ok"]


def cmd_audit(args) -> int:
    audit_dir = Path(args.audit_dir).expanduser() if args.audit_dir else fsutil.platform_data_dir()
    log = AuditLog(audit_dir / "audit.jsonl")
    if args.export:
        out = log.export_csv(args.export)
        print(f"✔ 已导出：{out}")
        print("  文件末尾有说明：本日志只能证明「谁在操作」，不能证明「谁有权操作」。")
        return EXIT["ok"]
    if args.verify:
        ok, count, detail = log.verify()
        if ok:
            print(f"✔ 审计日志完整性正常：{count} 条记录。")
            return EXIT["ok"]
        print(f"✘ 审计日志可能被改过：{detail}")
        return EXIT["error"]
    recs = log.records()
    if args.limit:
        recs = recs[-args.limit :]
    if args.json:
        _json_dump(recs)
        return EXIT["ok"]
    if not recs:
        print("还没有审计记录。")
        return EXIT["ok"]
    print(f"{'时间':<20} {'事件':<16} {'操作人':<12} {'结果':<6} 保险箱")
    for r in recs:
        print(
            f"{r.get('ts', ''):<20} {r.get('event', ''):<16} {r.get('actor', ''):<12} "
            f"{r.get('result', ''):<6} {r.get('vault_path', '')}"
        )
    return EXIT["ok"]


# ---------------------------------------------------------------- 解析

def _progress_enabled(args) -> bool:
    if args.no_progress:
        return False
    if args.progress:
        return True
    return sys.stderr.isatty()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="文件保险箱",
        description="给文件夹加密上锁 / 解锁（M1 命令行版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  创建保险箱      python -m cli.main create ~/Desktop/患者数据\n"
            "  解锁            python -m cli.main unlock ~/Desktop/患者数据\n"
            "  上锁            python -m cli.main lock   ~/Desktop/患者数据\n"
            "  查看状态        python -m cli.main status ~/Desktop/患者数据\n"
            "  完整性校验      python -m cli.main verify ~/Desktop/患者数据\n"
            "  中断后恢复      python -m cli.main recover ~/Desktop/患者数据\n"
            "  审计日志        python -m cli.main audit --limit 20\n"
        ),
    )
    p.add_argument("--version", action="version", version=f"文件保险箱 {__version__}")
    p.add_argument("--audit-dir", default=None, help="审计日志目录（默认：系统的应用数据目录）")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, need_folder=True):
        if need_folder:
            sp.add_argument("folder", help="保险箱文件夹")
        sp.add_argument("--password-stdin", action="store_true", help="从标准输入逐行读取密码（自动化用）")
        sp.add_argument("--json", action="store_true", help="输出 JSON")
        sp.add_argument("--yes", action="store_true", help="跳过确认（自动化用）")
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--progress", action="store_true", help="显示进度条")
        g.add_argument("--no-progress", action="store_true", help="不显示进度条")
        sp.add_argument("--audit-dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    sp = sub.add_parser("create", help="创建保险箱并上锁")
    common(sp)
    sp.set_defaults(func=cmd_create)

    sp = sub.add_parser("unlock", help="解锁（原地恢复成普通文件夹）")
    common(sp)
    sp.add_argument("--recovery-key", action="store_true", help="改用恢复密钥解锁")
    sp.set_defaults(func=cmd_unlock)

    sp = sub.add_parser("lock", help="上锁（原地加密，明文删除）")
    common(sp)
    sp.set_defaults(func=cmd_lock)

    sp = sub.add_parser("status", help="查看状态（不需要密码）")
    common(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("verify", help="校验密文完整性")
    common(sp)
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("recover", help="中断后继续或回滚")
    common(sp)
    sp.add_argument("--rollback", action="store_true", help="回滚到操作前的状态")
    sp.set_defaults(func=cmd_recover)

    sp = sub.add_parser("change-password", help="修改密码")
    common(sp)
    sp.set_defaults(func=cmd_change_password)

    sp = sub.add_parser("reset-password", help="用恢复密钥重置密码")
    common(sp)
    sp.set_defaults(func=cmd_reset_password)

    sp = sub.add_parser("regenerate-recovery", help="重新生成恢复密钥（旧密钥作废）")
    common(sp)
    sp.set_defaults(func=cmd_regenerate_recovery)

    sp = sub.add_parser("audit", help="查看 / 校验 / 导出审计日志")
    sp.add_argument("--verify", action="store_true", help="校验哈希链完整性")
    sp.add_argument("--export", metavar="CSV", default=None, help="导出 CSV")
    sp.add_argument("--limit", type=int, default=20, help="显示最近 N 条（0 表示全部）")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--audit-dir", default=None)
    sp.set_defaults(func=cmd_audit)

    return p


def main(argv: list[str] | None = None) -> int:
    setup_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except PasswordError as exc:
        print(f"错误：{exc}。密码不对，或这个保险箱不是用这个密码创建的。", file=sys.stderr)
        return EXIT["password"]
    except RecoveryKeyError as exc:
        print(f"错误：{exc}。请检查恢复密钥是否抄错。", file=sys.stderr)
        return EXIT["recovery"]
    except RecoveryNeededError as exc:
        print(f"错误：{exc}。请先运行：python -m cli.main recover <文件夹>", file=sys.stderr)
        return EXIT["recover_needed"]
    except DiskSpaceError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT["disk"]
    except VaultError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT["error"]
    except KeyboardInterrupt:
        print(
            "\n已中止。保险箱处于中间状态，下次运行会提示恢复；"
            "也可以运行 python -m cli.main recover <文件夹> 继续或回滚。",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
