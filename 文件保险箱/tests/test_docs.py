"""文档维护：Markdown 相对链接必须都能点开（改名/搬家后最容易坏）。"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def test_markdown_links_resolve():
    files = [PROJECT / "README.md"]
    files += sorted((PROJECT / "docs").glob("*.md"))
    files += sorted((PROJECT / "installers").rglob("*.md"))
    bad: list[str] = []
    for p in files:
        if not p.exists():
            bad.append(f"缺文件：{p}")
            continue
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"\]\(([^)]+)\)", text):
            target = m.group(1).split("#")[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (p.parent / target).exists():
                bad.append(f"{p.relative_to(PROJECT)} -> {target}")
    assert not bad, "坏链：\n" + "\n".join(bad)
