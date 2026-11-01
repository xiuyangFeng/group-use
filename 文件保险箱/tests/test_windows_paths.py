"""Windows 长路径/UNC 处理：在 mac 上通过 monkeypatch 模拟 Windows 分支。"""

from __future__ import annotations

import os

import pytest

from core import fsutil


def _as_windows(monkeypatch):
    monkeypatch.setattr(fsutil, "IS_WINDOWS", True)
    # Windows 上 os.path 是 ntpath；这里用恒等 abspath 模拟已给出的绝对路径
    monkeypatch.setattr(fsutil.os.path, "abspath", lambda s: s)


def test_long_path_adds_prefix(monkeypatch):
    _as_windows(monkeypatch)
    p = "/tmp/" + "很长的目录名" * 60
    got = fsutil.long_path(p)
    assert got.startswith("\\\\?\\"), got[:20]
    assert got.endswith("很长的目录名" * 60)


def test_short_path_unchanged(monkeypatch):
    _as_windows(monkeypatch)
    p = "/tmp/a.txt"
    assert fsutil.long_path(p) == p


def test_already_prefixed_unchanged(monkeypatch):
    _as_windows(monkeypatch)
    p = "\\\\?\\C:\\data\\x.txt"
    assert fsutil.long_path(p) == p


def test_unc_path_gets_unc_prefix(monkeypatch):
    _as_windows(monkeypatch)
    p = "\\\\server\\share\\患者数据\\a.txt"
    got = fsutil.long_path(p)
    assert got.startswith("\\\\?\\UNC\\"), got
    assert got.endswith("患者数据\\a.txt") or "患者数据" in got


@pytest.mark.skipif(os.name == "nt", reason="Windows 上 /tmp 会解析成当前盘符下的路径，属正常")
def test_posix_passthrough():
    p = "/tmp/" + "x" * 400
    assert fsutil.long_path(p) == p
