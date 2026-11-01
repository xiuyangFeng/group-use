"""验收要求"不联网、无遥测"：静态检查发布代码里没有联网/上报路径。

允许 socket.gethostname()（只取本机名写进本地审计日志，不联网）。
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
CODE_DIRS = [PROJECT / "core", PROJECT / "cli", PROJECT / "gui"]
CODE_FILES = sorted(PROJECT.glob("run_gui.py"))

FORBIDDEN_MODULES = {
    "requests", "urllib", "urllib3", "http", "httpx", "aiohttp", "websocket",
    "websockets", "ftplib", "smtplib", "telnetlib", "xmlrpc", "socketserver",
    "ssl", "qnetwork", "QtNetwork",
}
FORBIDDEN_CALLS = {
    "socket.socket", "socket.create_connection", "socket.create_server",
    "socket.getaddrinfo", "urlopen", "urlretrieve", "QNetworkAccessManager",
    "QNetworkRequest",
}
FORBIDDEN_STRINGS = ("https://", "http://", "telemetry", "analytics", "sentry", "bugsnag")


def _sources():
    for d in CODE_DIRS:
        yield from sorted(d.rglob("*.py"))
    yield from CODE_FILES


def test_no_network_imports():
    problems: list[str] = []
    for p in _sources():
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in FORBIDDEN_MODULES or alias.name in FORBIDDEN_MODULES:
                        problems.append(f"{p.relative_to(PROJECT)}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                top = mod.split(".")[0]
                if mod in FORBIDDEN_MODULES or top in FORBIDDEN_MODULES:
                    problems.append(f"{p.relative_to(PROJECT)}: from {mod} import ...")
    assert not problems, "发布代码里出现联网模块：\n" + "\n".join(problems)


def test_no_network_calls_or_urls():
    problems: list[str] = []
    for p in _sources():
        text = p.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                try:
                    name = ast.unparse(node.func)
                except Exception:  # noqa: BLE001
                    continue
                if name in FORBIDDEN_CALLS:
                    problems.append(f"{p.relative_to(PROJECT)}: 调用 {name}()")
        for s in FORBIDDEN_STRINGS:
            if s in text:
                problems.append(f"{p.relative_to(PROJECT)}: 出现 {s!r}")
    assert not problems, "疑似联网/遥测代码：\n" + "\n".join(problems)


def test_only_allowed_socket_use():
    """唯一允许的 socket 用法是 gethostname()（写本地审计日志）。"""
    import re

    offenders = []
    for p in _sources():
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"socket\.(\w+)", text):
            if m.group(1) not in ("gethostname",):
                offenders.append(f"{p.relative_to(PROJECT)}: socket.{m.group(1)}")
    assert not offenders, "\n".join(offenders)
