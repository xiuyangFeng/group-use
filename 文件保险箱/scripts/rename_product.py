"""产品改名工具：把所有出现「文件保险箱」的文件内容/文件名统一换成新名字。

默认 dry-run（只看不改）；加 --apply 才真正改，并在 .build 里留一份 zip 备份。
改名后需要重新打包（dist 里的旧产物不自动改），并注意用户数据目录名也会变。

用法：
    python scripts/rename_product.py --new 安全数据箱            # 预演
    python scripts/rename_product.py --new 安全数据箱 --apply    # 真正改
    python scripts/rename_product.py --new 安全数据箱 --apply --migrate-data
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from core.console import setup_utf8_stdio  # noqa: E402

SKIP_DIRS = {".venv", ".build", "dist", "__pycache__", ".pytest_cache", ".git", ".stress", ".bench", ".idea"}
TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".sh", ".command", ".ps1", ".bat", ".cmd", ".yml", ".yaml",
    ".json", ".plist", ".wflow", ".html", ".css", ".toml", ".ini", ".cfg", ".spec",
    ".gitignore", ".gitattributes",
}
BAD_CHARS = set('/\\:*?"<>|')


def _is_text_file(p: Path) -> bool:
    if p.name in (".gitignore", ".gitattributes"):
        return True
    return p.suffix.lower() in TEXT_SUFFIXES


def collect_changes(root: Path, old: str, new: str) -> tuple[list[tuple[Path, str]], list[Path]]:
    """返回 (需要改内容的 [(路径, 新内容)], 需要改名的路径)。"""
    content_changes: list[tuple[Path, str]] = []
    rename_paths: list[Path] = []
    for p in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.is_file():
            # 文件名含旧名字的，不管二进制还是文本都要改名（例如 .icns/.ico）
            if old in p.name:
                rename_paths.append(p)
            if _is_text_file(p):
                try:
                    text = p.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if old in text:
                    content_changes.append((p, text.replace(old, new)))
        elif p.is_dir() and old in p.name:
            rename_paths.append(p)
    # 目录自底向上改名，避免父目录先改导致子路径失效
    return content_changes, sorted(rename_paths, key=lambda x: len(x.parts), reverse=True)


def show(changes, renames, root: Path, limit: int = 200) -> None:
    print(f"内容要改的文件：{len(changes)} 个")
    for p, _ in changes[:limit]:
        print(f"  M {p.relative_to(root)}")
    if len(changes) > limit:
        print(f"  …还有 {len(changes) - limit} 个")
    print(f"名字要改的文件/目录：{len(renames)} 个")
    for p in renames[:limit]:
        print(f"  R {p.relative_to(root)}")
    if len(renames) > limit:
        print(f"  …还有 {len(renames) - limit} 个")


def backup(root: Path, changes, renames) -> Path | None:
    targets = {p for p, _ in changes} | set(renames)
    if not targets:
        return None
    out_dir = root / ".build"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"rename-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(targets):
            if p.is_file():
                zf.write(p, p.relative_to(root).as_posix())
            else:
                for sub in sorted(p.rglob("*")):
                    if sub.is_file():
                        zf.write(sub, sub.relative_to(root).as_posix())
    return out


def data_dir_for(root: Path, old: str) -> Path | None:
    """按 fsutil 规则算出当前用户数据目录（只在本机有效）。"""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / old
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(home)
        return Path(base) / old
    base = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    return Path(base) / old


def main() -> int:
    setup_utf8_stdio()
    parser = argparse.ArgumentParser(description="产品改名（默认只预演）")
    parser.add_argument("--new", required=True, help="新名字，例如：安全数据箱")
    parser.add_argument("--old", default="文件保险箱", help="旧名字（默认：文件保险箱）")
    parser.add_argument("--apply", action="store_true", help="真正写入；不加则只预演")
    parser.add_argument("--migrate-data", action="store_true", help="同时把用户数据目录改成新名字")
    parser.add_argument("--root", default=str(PROJECT), help=argparse.SUPPRESS)  # 测试用
    args = parser.parse_args()

    root = Path(args.root)
    new = args.new.strip()
    if not new or new == args.old:
        raise SystemExit("新名字不能为空，也不能和旧名字一样")
    if any(ch in BAD_CHARS for ch in new) or new.startswith("."):
        raise SystemExit(f"新名字里有不能用于文件夹的字符：{sorted(set(new) & BAD_CHARS)}")

    changes, renames = collect_changes(root, args.old, new)
    print(f"项目根目录：{root}")
    print(f"『{args.old}』→『{new}』")
    show(changes, renames, root)
    if not changes and not renames:
        print("没有需要改的地方。")
        return 0

    old_data = data_dir_for(root, args.old)
    new_data = data_dir_for(root, new)
    print()
    print(f"提示：用户数据目录（设置/审计/日志）会从\n  {old_data}\n变成\n  {new_data}")
    if old_data and old_data.exists():
        print("  当前旧目录存在。如果已经正式使用过，请先备份，再用 --migrate-data 迁移，")
        print("  否则新版本会看不到原来的设置和审计记录。")

    if not args.apply:
        print("\n这是预演，没有改动任何文件。确认无误后加 --apply 再运行。")
        return 0

    bak = backup(root, changes, renames)
    if bak:
        print(f"\n已备份到：{bak}")
    for p, text in changes:
        p.write_text(text, encoding="utf-8")
    for p in renames:
        target = p.with_name(p.name.replace(args.old, new))
        if target.exists():
            print(f"  跳过（目标已存在）：{target}")
            continue
        p.rename(target)
    print(f"已改名：{len(changes)} 个文件内容，{len(renames)} 个文件/目录名。")

    if args.migrate_data and old_data and new_data:
        if old_data.exists() and not new_data.exists():
            shutil.move(str(old_data), str(new_data))
            print(f"数据目录已迁移：{old_data} → {new_data}")
        elif new_data.exists():
            print(f"新数据目录已存在，未自动迁移：{new_data}")
        else:
            print("旧数据目录不存在，无需迁移。")
    print("\n下一步：重新打包（python scripts/build_app.py），dist 里的旧产物不会自动改名。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
