"""和组内另一个工具「文件保险箱」的联动。保险箱不用装，没有保险箱时这里什么都不做。

保险箱是"原地加密"：上锁后文件夹里只剩 `.vault/`（密文 + 明文的 header.json）和一个说明文件，
解锁后恢复成普通文件夹。header.json 不含任何秘密，其中 state 字段表示状态：
LOCKED 已上锁 / UNLOCKED 已解锁 / LOCKING 正在上锁 / UNLOCKING 正在解锁 / INCONSISTENT 上次中断。

这里做两件事：

1. 开始前检查：原始数据、输出、映射表所在的保险箱必须是"已解锁"。否则比如映射表所在的
   保险箱锁着，映射表文件看起来"不存在"，就会新建一张空表，编号从 P0001 重来、UID 也对不上。
2. 运行期间写"正在使用"标记 `.vault/in-use-ct-deid.json`，每 30 秒刷新心跳，结束时改成
   active=false（本软件不删除任何文件，所以不删标记）。保险箱看到有效标记就不自动上锁，
   手动上锁也会拦住；本软件崩溃后心跳停了，保险箱 3 分钟后自动忽略这个标记。
   标记格式的约定写在保险箱的 core/inuse.py 里，改动时两边一起改。
"""

import json
import os
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

VAULT_DIR = ".vault"
HEADER = "header.json"
MARKER = "in-use-ct-deid.json"
TOOL_NAME = "CT 数据脱敏工具"
HEARTBEAT_SECONDS = 30

UNLOCKED = "UNLOCKED"
STATE_TEXT = {
    "LOCKED": "目前已上锁",
    "LOCKING": "正在上锁",
    "UNLOCKING": "正在解锁",
    "INCONSISTENT": "上次上锁/解锁被中断，还没恢复",
}


def find_vault(path):
    """path 自己或某一级上级目录是保险箱，就返回保险箱根目录；否则 None。path 可以还不存在。"""
    if not path:
        return None
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        p = p.absolute()
    for d in (p, *p.parents):
        try:
            if (d / VAULT_DIR / HEADER).is_file():
                return d
        except OSError:
            continue
    return None


def vault_state(root):
    """读保险箱状态（不需要密码）；读不出来返回 None。"""
    try:
        data = json.loads((Path(root) / VAULT_DIR / HEADER).read_text(encoding="utf-8"))
        return str(data["state"]) if isinstance(data, dict) else None
    except (OSError, ValueError, KeyError):
        return None


def locked_problem(path, what):
    """path 在一个没解锁的保险箱里，返回给人看的原因；否则 None。"""
    root = find_vault(path)
    if root is None:
        return None
    state = vault_state(root)
    if state == UNLOCKED:
        return None
    why = STATE_TEXT.get(state, "状态读不出来")
    return f"{what}在保险箱「{root.name}」里，保险箱{why}。请先在「文件保险箱」里解锁，再开始脱敏"


def vaults_in_use(input_dir, output_dir, mapping_path):
    """{保险箱根目录: [用到的东西]}，只包含已解锁的保险箱（锁着的开始前就会被拦下）。"""
    roots = {}
    for path, what in ((input_dir, "原始数据"), (output_dir, "输出"), (mapping_path, "映射表")):
        root = find_vault(path)
        if root is not None and vault_state(root) == UNLOCKED:
            roots.setdefault(root, []).append(what)
    return roots


def _write_json(path, data):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    tmp.replace(path)


class InUse:
    """运行期间在用到的保险箱里放"正在使用"标记（上下文管理器）。写不进去只提示，不影响脱敏。"""

    def __init__(self, roots, log=print):
        self.roots = dict(roots)
        self.log = log
        self._stop = threading.Event()
        self._thread = None
        self._failed = set()
        self._started = datetime.now().astimezone().isoformat(timespec="seconds")

    def _write(self, active):
        for root, what in self.roots.items():
            data = {"tool": TOOL_NAME, "active": active, "pid": os.getpid(), "host": socket.gethostname(),
                    "started_at": self._started, "heartbeat": time.time(), "what": "、".join(what)}
            if not active:
                data["ended_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            try:
                _write_json(root / VAULT_DIR / MARKER, data)
            except OSError as e:
                if root not in self._failed:
                    self._failed.add(root)
                    self.log(f"[提示] 没能在保险箱「{root.name}」里留下“正在使用”标记（{e.strerror or e}）。"
                             f"处理期间请别让它自动上锁（可在「文件保险箱」设置里调长自动上锁时间）")

    def _beat(self):
        while not self._stop.wait(HEARTBEAT_SECONDS):
            self._write(True)

    def __enter__(self):
        if self.roots:
            self._write(True)
            self._thread = threading.Thread(target=self._beat, name="vault-heartbeat", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._thread is not None:
            self._stop.set()
            self._thread.join()
            self._write(False)
        return False
