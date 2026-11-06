"""文件保险箱命令行（M1 用；M2 的 GUI 直接调用 core，不经过这里）。

用法见 `python -m cli.main --help`。
退出码：0 成功 / 1 一般错误 / 2 用法错误 / 3 密码错误 / 4 密保答案或恢复密钥错误 / 5 需要先恢复 / 6 磁盘空间不足
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

from core import __version__, constants as C, crypto, fsutil, passwords
from core import questions as Q
from core.console import setup_utf8_stdio
from core.audit import AuditLog
from core.errors import (
    AnswerError,
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


def _read_line(reader: "SecretReader", prompt: str) -> str:
    """密保答案不算机密到要隐藏输入（方便核对有没有打错）；自动化时从标准输入读。"""
    if reader.use_stdin:
        line = sys.stdin.readline()
        if not line:
            raise VaultError("标准输入里没有足够的行（--password-stdin）")
        return line.rstrip("\r\n")
    try:
        return input(prompt)
    except EOFError:
        raise VaultError("输入被中断")


def _resolve_question(text: str) -> str:
    text = text.strip()
    if text.isdigit() and 1 <= int(text) <= len(Q.PRESETS):
        return Q.PRESETS[int(text) - 1][0]
    return text


def ask_questions(reader: "SecretReader") -> list[tuple[str, str]]:
    """设 3 个密保问题。自动化（--password-stdin）时每行一个「问题编号或原文=答案」。"""
    if reader.use_stdin:
        pairs = []
        for _ in range(Q.QUESTION_COUNT):
            line = _read_line(reader, "")
            q, sep, a = line.partition("=")
            if not sep:
                raise VaultError("密保问题要写成「问题编号=答案」，例如 1=1998-03-12")
            pairs.append((_resolve_question(q), a))
        return pairs

    print()
    print(f"设置 {Q.QUESTION_COUNT} 个密保问题（忘记密码时答对任意一个即可）。可选问题：")
    for i, (q, _k) in enumerate(Q.PRESETS, 1):
        print(f"  {i:>2}. {q}")
    while True:
        pairs = []
        for n in range(1, Q.QUESTION_COUNT + 1):
            while True:
                q = _resolve_question(_read_line(reader, f"第 {n} 个问题（输入编号）："))
                if q not in Q.PRESET_KINDS:
                    print("  请输入上面列表里的编号。")
                    continue
                if q in [x for x, _ in pairs]:
                    print("  这个问题已经选过了，换一个。")
                    continue
                break
            while True:
                a = _read_line(reader, f"  {q}（{Q.placeholder(q)}）：")
                problem = Q.answer_problem(q, a)
                if problem:
                    print(f"  {problem}")
                    continue
                break
            pairs.append((q, a))
        problems = Q.questions_problems(pairs)
        if not problems:
            return pairs
        print("密保问题不符合要求：" + "；".join(problems) + "，请重新设置。")


def ask_answers(vault: Vault, reader: "SecretReader") -> list[str]:
    """回答这个保险箱的密保问题；不会的直接回车跳过，答对一个即可。"""
    texts = vault.header.question_texts
    if not texts:
        raise AnswerError("这个保险箱没有设置密保问题（旧版本建的，请用 --recovery-key）")
    if not reader.use_stdin:
        print("回答密保问题（答对任意一个即可，不会的直接回车跳过）：")
    return [_read_line(reader, f"  {i}. {q}：") for i, q in enumerate(texts, 1)]


def _read_recovery_key(reader: "SecretReader") -> bytes:
    if reader.use_stdin:
        line = sys.stdin.readline()
        if not line:
            raise VaultError("标准输入里没有恢复密钥")
        text = line.strip()
    else:
        text = getpass.getpass("恢复密钥：").strip()
    return crypto.recovery_key_parse(text)


def credentials(args, vault: Vault, reader: "SecretReader") -> dict:
    """按参数取凭据：默认密码；--forgot 答密保；--recovery-key 用旧版恢复密钥。"""
    if getattr(args, "recovery_key", False):
        return {"recovery_key": _read_recovery_key(reader)}
    if getattr(args, "forgot", False):
        return {"answers": ask_answers(vault, reader)}
    return {"password": reader.ask("密码：")}


def confirm_backup() -> bool:
    print("开始前请确认三件事：")
    print("  1. 这个文件夹里的重要数据已经另外备份过（移动硬盘 / 另一台电脑）；")
    print(f"  2. 密码是组里共用的：{passwords.password_rule_text()}；")
    print("  3. 要设 3 个密保问题，忘记密码时答对任意一个即可找回。")
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
        f"设置密码（{passwords.password_rule_text()}）：",
        confirm=None if args.password_stdin else "再输一次：",
    )
    passwords.validate_password(password)
    pairs = ask_questions(reader)
    pp = ProgressPrinter(_progress_enabled(args))
    try:
        result = vault.create(password, pairs, progress=pp)
    finally:
        pp.done()

    stats = result["stats"]
    if args.json:
        _json_dump(
            {
                "ok": True,
                "questions": result["questions"],
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
    print("  密保问题：" + " / ".join(result["questions"]))
    print("  忘记密码时运行 reset-password，答对任意一个即可设新密码。")
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
    cred = credentials(args, vault, reader)
    pp = ProgressPrinter(_progress_enabled(args))
    try:
        stats = vault.unlock(progress=pp, **cred)
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
    cred = credentials(args, vault, reader)
    pp = ProgressPrinter(_progress_enabled(args))
    try:
        stats = vault.lock(progress=pp, **cred)
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
    if st["questions"]:
        print("密保问题：" + " / ".join(st["questions"]))
    else:
        print(f"恢复密钥指纹：{st['recovery_fingerprint']}（旧版保险箱，建议运行 set-questions 设置密保问题）")
    if st["state"] == "LOCKED":
        if not st["protected"]:
            st["protected"] = vault.ensure_protected()
        print(f"防移动/删除：{'已开启' if st['protected'] else '未开启（这个磁盘不支持，或已用 FDVAULT_NO_PROTECT=1 关闭）'}")
    print(f"审计日志：{vault._audit_dir / 'audit.jsonl'}")
    if st["in_use_by"]:
        print(f"正在使用：{st['in_use_by']}（用完之前不能上锁）")
    if st["needs_recovery"]:
        print()
        print("⚠ 上次操作没有完成，请运行：python -m cli.main recover <文件夹>")
    return EXIT["ok"]


def cmd_verify(args) -> int:
    vault = _vault(args)
    _maybe_recovery_prompt(vault, args)
    reader = SecretReader(args.password_stdin)
    cred = credentials(args, vault, reader)
    pp = ProgressPrinter(_progress_enabled(args))
    try:
        result = vault.verify(progress=pp, **cred)
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
    cred = credentials(args, vault, reader)
    pp = ProgressPrinter(_progress_enabled(args))
    try:
        result = vault.recover(progress=pp, rollback=rollback, **cred)
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
        f"新密码（{passwords.password_rule_text()}）：",
        confirm=None if args.password_stdin else "再输一次：",
    )
    vault.change_password(old, new)
    print("✔ 密码已修改。密保问题没有变，忘记密码时仍然可以用。")
    return EXIT["ok"]


def cmd_reset_password(args) -> int:
    vault = _vault(args)
    _maybe_recovery_prompt(vault, args)
    reader = SecretReader(args.password_stdin)
    use_key = args.recovery_key or not vault.header.question_texts
    if use_key:
        key = _read_recovery_key(reader)
    else:
        answers = ask_answers(vault, reader)
    new = reader.ask(
        f"新密码（{passwords.password_rule_text()}）：",
        confirm=None if args.password_stdin else "再输一次：",
    )
    if use_key:
        vault.reset_with_recovery_key(key, new)
        print("✔ 已用恢复密钥重置密码。之前的密码已失效。建议再运行 set-questions 设置密保问题。")
    else:
        vault.reset_with_answers(answers, new)
        print("✔ 密保答对了，密码已重置。之前的密码已失效，记得告诉组里同事。")
    return EXIT["ok"]


def cmd_set_questions(args) -> int:
    vault = _vault(args)
    _maybe_recovery_prompt(vault, args)
    reader = SecretReader(args.password_stdin)
    password = reader.ask("密码：")
    pairs = ask_questions(reader)
    result = vault.set_security_questions(password, pairs)
    if args.json:
        _json_dump(result)
        return EXIT["ok"]
    print("✔ 密保问题已保存：" + " / ".join(result["questions"]))
    if result["dropped_recovery_key"]:
        print("  原来的恢复密钥已经作废，忘记密码请用密保问题找回。")
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
            "  忘记密码解锁    python -m cli.main unlock ~/Desktop/患者数据 --forgot\n"
            "  忘记密码重置    python -m cli.main reset-password ~/Desktop/患者数据\n"
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

    def cred_opts(sp):
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--forgot", action="store_true", help="忘记密码：改为回答密保问题（答对一个即可）")
        g.add_argument("--recovery-key", action="store_true", help="改用恢复密钥（只有旧版本建的保险箱有）")

    sp = sub.add_parser("unlock", help="解锁（原地恢复成普通文件夹）")
    common(sp)
    cred_opts(sp)
    sp.set_defaults(func=cmd_unlock)

    sp = sub.add_parser("lock", help="上锁（原地加密，明文删除）")
    common(sp)
    cred_opts(sp)
    sp.set_defaults(func=cmd_lock)

    sp = sub.add_parser("status", help="查看状态（不需要密码）")
    common(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("verify", help="校验密文完整性")
    common(sp)
    cred_opts(sp)
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("recover", help="中断后继续或回滚")
    common(sp)
    sp.add_argument("--rollback", action="store_true", help="回滚到操作前的状态")
    cred_opts(sp)
    sp.set_defaults(func=cmd_recover)

    sp = sub.add_parser("change-password", help="修改密码")
    common(sp)
    sp.set_defaults(func=cmd_change_password)

    sp = sub.add_parser("reset-password", help="忘记密码：答对一个密保问题后设新密码")
    common(sp)
    sp.add_argument("--recovery-key", action="store_true", help="改用恢复密钥（只有旧版本建的保险箱有）")
    sp.set_defaults(func=cmd_reset_password)

    sp = sub.add_parser("set-questions", help="设置 / 修改 3 个密保问题（旧版保险箱的恢复密钥随之作废）")
    common(sp)
    sp.set_defaults(func=cmd_set_questions)

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
    except AnswerError as exc:
        print(f"错误：{exc}。可以换一个问题再试。", file=sys.stderr)
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
