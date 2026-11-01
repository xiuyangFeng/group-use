"""映射表（Excel）。只能留在本地，绝不能随数据发出。

- 第一个工作表 "映射表"：每处理一个原始文件夹追加一行
- 隐藏工作表 "_key"：保存生成新 UID 用的密钥，保证同一患者重复处理时 UID 一致
"""

import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

SHEET = "映射表"
KEY_SHEET = "_key"
COLUMNS = [
    "编号", "原文件夹", "患者姓名", "患者ID", "检查号", "性别", "年龄", "出生日期",
    "检查日期", "DICOM文件数", "序列数", "跳过的非DICOM文件数", "疑似烧录文字的序列",
    "状态", "备注", "处理时间", "时间点", "原始数据文件夹", "批次",
]
OLD_COLUMNS = COLUMNS[:16]  # v5 及以前的表头；之后每个版本只在末尾加列
WIDTHS = {"原文件夹": 28, "患者姓名": 18, "疑似烧录文字的序列": 30, "备注": 50, "处理时间": 20, "时间点": 36,
          "原始数据文件夹": 40, "批次": 22}
CODE_RE = re.compile(r"^P(\d+)$")


def _header(ws):
    header = [c.value for c in ws[1]]
    while header and header[-1] in (None, ""):
        header.pop()
    return header


def read_rows(path):
    """只读方式读出映射表的所有行（按表头名取值，兼容旧版表头）。"""
    wb = load_workbook(path, read_only=True)
    try:
        if SHEET not in wb.sheetnames:
            return []
        it = wb[SHEET].iter_rows(values_only=True)
        header = list(next(it, ()))
        return [dict(zip(header, row)) for row in it if row and any(v not in (None, "") for v in row)]
    finally:
        wb.close()


class MappingTable:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.exists():
            self.wb = load_workbook(self.path)
            if SHEET not in self.wb.sheetnames:
                raise ValueError(f"{self.path} 不是本软件生成的映射表（缺少“{SHEET}”工作表）")
            self.ws = self.wb[SHEET]
            header = _header(self.ws)
            if header != COLUMNS:
                if len(header) >= len(OLD_COLUMNS) and COLUMNS[: len(header)] == header:
                    # 旧版映射表：在末尾补上新列（运行前已自动备份）
                    for i in range(len(header), len(COLUMNS)):
                        cell = self.ws.cell(row=1, column=i + 1, value=COLUMNS[i])
                        cell.font = Font(bold=True)
                        self.ws.column_dimensions[cell.column_letter].width = WIDTHS.get(COLUMNS[i], 12)
                else:
                    raise ValueError(f"{self.path} 的表头与本软件不一致，请勿手动改动表头")
        else:
            self.wb = Workbook()
            self.ws = self.wb.active
            self.ws.title = SHEET
            self.ws.append(COLUMNS)
            for cell in self.ws[1]:
                cell.font = Font(bold=True)
            for i, name in enumerate(COLUMNS):
                letter = self.ws.cell(row=1, column=i + 1).column_letter
                self.ws.column_dimensions[letter].width = WIDTHS.get(name, 12)
            self.ws.freeze_panes = "A2"

        if KEY_SHEET in self.wb.sheetnames:
            self.secret = str(self.wb[KEY_SHEET]["A1"].value)
        else:
            ks = self.wb.create_sheet(KEY_SHEET)
            ks["A1"] = secrets.token_hex(32)
            ks.sheet_state = "hidden"
            self.secret = ks["A1"].value

    def rows(self):
        for row in self.ws.iter_rows(min_row=2, values_only=True):
            if row and row[0]:
                yield dict(zip(COLUMNS, row))

    def code_by_patient_id(self):
        out = {}
        for r in self.rows():
            pid = str(r.get("患者ID") or "").strip()
            if pid:
                out.setdefault(pid, r["编号"])
        return out

    def max_number(self):
        n = 0
        for r in self.rows():
            m = CODE_RE.match(str(r["编号"]))
            if m:
                n = max(n, int(m.group(1)))
        return n

    def append(self, row: dict):
        # 全部按文本写入，避免 Excel 把 "000123" 之类的 ID 变成数字
        self.ws.append(["" if row.get(c) is None else str(row.get(c)) for c in COLUMNS])
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        self.wb.save(tmp)
        tmp.replace(self.path)


def backup_mapping(path):
    """每次运行前把已有映射表复制一份到“映射表备份”文件夹，历史版本永不丢失。"""
    path = Path(path)
    if not path.exists():
        return None
    folder = path.parent / "映射表备份"
    folder.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    n = 1
    while True:
        target = folder / f"{path.stem}_{stamp}{'' if n == 1 else f'_{n}'}{path.suffix}"
        if not target.exists():
            break
        n += 1
    shutil.copy2(path, target)
    return target


def check_writable(path):
    """Windows 上 Excel 打开文件时会加锁，提前检查以免处理到一半才失败。"""
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path, "r+b"):
            pass
    except PermissionError:
        return f"映射表 {path.name} 无法写入，可能正在被 Excel 打开，请先关闭。"
    return None
