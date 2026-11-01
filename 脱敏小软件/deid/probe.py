"""结构探查：统计数据的"形状"，用于调整脱敏规则。只读，不修改任何文件。

报告里只有：目录层级、文件数量、扩展名、字段名、厂商私有字段的创建者名、
设备厂商/模态/字符集等技术值，以及"哪些字段里出现过患者姓名/ID"的计数。
不包含任何患者姓名、ID、日期、文件夹名或文件名，可以发给开发者。

为了速度：
  1. 每个文件只打开一次，只解析文件头前部（0002–0028 组），不碰私有大字段和像素
  2. 每个序列只抽 首/中/尾 3 个文件做全字段分析和模拟脱敏
  3. 所有文件按每批 100 个分给多个进程并行，进度条按文件数实时更新
"""

import copy
import os
import shutil
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from .anonymizer import UidMapper, anonymize, collect_needles
from .pipeline import (IGNORED_NAMES, MAX_WORKERS, Control, _init_worker, burned_in_reason, checkpoint,
                       is_disk_full, read_dicom, read_header_fast)

SAFE_VALUE_TAGS = ("Modality", "Manufacturer", "ManufacturerModelName", "SpecificCharacterSet",
                   "BurnedInAnnotation", "ImageType", "PhotometricInterpretation", "BitsAllocated")
ID_KEYWORDS = {"PatientName", "PatientID", "OtherPatientIDs", "OtherPatientNames", "AccessionNumber"}
SAMPLES_PER_SERIES = 3
CHUNK = 100
MIN_FREE_MB = 20  # 报告本身一般不到 1 MB，留足余量


def _ext(fn):
    e = os.path.splitext(fn)[1].lower()
    return e if e and len(e) <= 6 and e[1:].isalnum() and not e[1:].isdigit() else "(无扩展名或数字)"


def _samples(paths):
    if len(paths) <= SAMPLES_PER_SERIES:
        return paths
    return [paths[0], paths[len(paths) // 2], paths[-1]]


def _fmt_secs(sec):
    sec = int(sec)
    return f"{sec // 60} 分 {sec % 60} 秒" if sec >= 60 else f"{sec} 秒"


# ---------------------------------------------------------------- 子进程里运行的部分

def light_chunk(paths):
    """一批文件：只读文件头前部的少量字段。"""
    out = []
    for path in paths:
        checkpoint()
        ds = read_header_fast(path)
        if ds is None:
            out.append(None)
        elif os.path.basename(path).upper() == "DICOMDIR" or "DirectoryRecordSequence" in ds:
            out.append("DICOMDIR")
        else:
            out.append({
                "ts": str(getattr(ds, "file_meta", {}).get("TransferSyntaxUID", "(无文件头)")),
                "sop": str(ds.get("SOPClassUID", "")),
                "vals": {kw: str(ds.get(kw)) for kw in SAFE_VALUE_TAGS if kw in ds},
                "sid": str(ds.get("SeriesInstanceUID", "")),
                "burned": burned_in_reason(ds),
            })
    return out


def deep_series(paths):
    """一个序列的抽样文件：全字段分析 + 模拟脱敏。"""
    st = defaultdict(Counter)
    for k, path in enumerate(_samples(paths)):
        checkpoint()
        ds = read_dicom(path)
        if ds is None:
            continue
        st["sampled"]["n"] += 1
        raw_needles = collect_needles(ds)
        needles = [n.lower() for n in raw_needles]
        for elem in ds.iterall():
            if elem.tag.is_private:
                if 0x10 <= elem.tag.element < 0x100:
                    st["private"][f"({elem.tag.group:04X}) {elem.value}"] += 1
            else:
                st["keywords"][elem.keyword or str(elem.tag)] += 1
            if needles and elem.VR not in ("SQ", "OB", "OW", "UN") and elem.keyword not in ID_KEYWORDS:
                try:
                    text = str(elem.value).lower()
                except Exception:
                    continue
                if any(n in text for n in needles):
                    name = elem.keyword or f"私有{elem.tag}"
                    st["leak"][f"{name} [{elem.VR}]"] += 1
        if k == 0:  # 每个序列做一次模拟脱敏，看是否有残留
            test = copy.deepcopy(ds)
            anonymize(test, "P0000", UidMapper("probe"), raw_needles)
            for elem in test.iterall():
                if elem.VR == "PN" and elem.value and elem.keyword != "PatientName":
                    st["residual"][f"{elem.keyword} [PN]"] += 1
                elif elem.VR not in ("SQ", "OB", "OW") and needles and elem.value:
                    if any(n in str(elem.value).lower() for n in needles):
                        st["residual"][f"{elem.keyword} [{elem.VR}]"] += 1
    return dict(st)


# ---------------------------------------------------------------- 主进程

def probe(input_dir, log=print, progress=None, workers=None, control=None):
    """返回报告文本；用户点了“停止”则返回 None。"""
    progress = progress or (lambda stage, done, total: None)
    ctl = control or Control()
    workers = max(1, int(workers or MAX_WORKERS))
    input_dir = Path(input_dir)
    folders = sorted(p for p in input_dir.iterdir() if p.is_dir() and not p.name.startswith("."))

    # 1. 列出所有文件（只读目录，不打开文件）
    entries = []  # (文件夹序号, 路径, 层级, 文件名)
    last = time.monotonic()
    for i, folder in enumerate(folders):
        for root, dirs, files in os.walk(folder):
            dirs.sort()
            depth = len(Path(root).relative_to(folder).parts)
            for fn in sorted(files):
                if fn not in IGNORED_NAMES and not fn.startswith("._"):
                    entries.append((i, os.path.join(root, fn), depth, fn))
            if time.monotonic() - last > 1:
                log(f"正在列出文件：{i + 1}/{len(folders)} 个文件夹，已找到 {len(entries)} 个文件")
                last = time.monotonic()
    total_files = len(entries)
    log(f"共 {len(folders)} 个文件夹，{total_files} 个文件")

    folder_st = [dict(dcm=0, other=0, depth=Counter(), exts=Counter()) for _ in folders]
    series_files = defaultdict(list)  # (文件夹序号, 序列UID) -> [路径]
    total = defaultdict(Counter)
    errors = 0

    def stopped(futs):
        if ctl.stopped:
            for f in futs:
                f.cancel()
            return True
        return False

    with ProcessPoolExecutor(workers, initializer=_init_worker,
                             initargs=(None, ctl.run_event, ctl.stop_event)) as pool:
        # 2. 所有文件读头部少量字段，按批并行
        start = time.monotonic()
        results = [None] * total_files
        futs = {pool.submit(light_chunk, [e[1] for e in entries[k:k + CHUNK]]): k
                for k in range(0, total_files, CHUNK)}
        done = 0
        for fut in as_completed(futs):
            k = futs[fut]
            try:
                chunk = fut.result()
            except Exception:
                if stopped(futs):
                    break
                chunk = [None] * len(entries[k:k + CHUNK])
                errors += 1
            results[k:k + len(chunk)] = chunk
            done += len(chunk)
            progress("读取文件头", done, total_files)
            elapsed = time.monotonic() - start
            eta = elapsed / done * (total_files - done) if done else 0
            log("已暂停" if ctl.paused else f"读取文件头 {done}/{total_files}，预计剩余 {_fmt_secs(eta)}")
        if stopped(futs):
            return None

        for (i, path, depth, fn), r in zip(entries, results):
            fs = folder_st[i]
            if r is None:
                fs["other"] += 1
                fs["exts"][_ext(fn)] += 1
            elif r == "DICOMDIR":
                fs["exts"]["DICOMDIR"] += 1
            else:
                fs["dcm"] += 1
                fs["depth"][depth] += 1
                fs["exts"]["DICOM:" + _ext(fn)] += 1
                total["tsyntax"][r["ts"]] += 1
                total["sop"][r["sop"]] += 1
                for kw, v in r["vals"].items():
                    total["v:" + kw][v] += 1
                key = (i, r["sid"])
                if key not in series_files and r["burned"]:
                    total["burned"][r["burned"]] += 1
                series_files[key].append(path)

        # 3. 每个序列抽样做全字段分析
        futs = [pool.submit(deep_series, paths) for paths in series_files.values()]
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                for key, counter in fut.result().items():
                    total[key].update(counter)
            except Exception:
                if stopped(futs):
                    return None
                errors += 1
            progress("抽样分析", n, len(futs))
            log(f"抽样分析 {n}/{len(futs)} 个序列")

    n_series = Counter(i for i, _ in series_files)
    lines = [f"  文件夹{i + 1:03d}: DICOM {fs['dcm']} 个, 其他 {fs['other']} 个, 序列 {n_series[i]} 个, "
             f"DICOM所在层级 {dict(sorted(fs['depth'].items()))}, 扩展名 {dict(fs['exts'])}"
             for i, fs in enumerate(folder_st)]

    out = [f"结构探查报告  {datetime.now():%Y-%m-%d %H:%M}",
           "（本报告不含患者姓名、ID、日期、文件夹名和文件名）",
           f"（字段分析为抽样：每个序列抽首/中/尾 {SAMPLES_PER_SERIES} 个文件，共抽样 {total['sampled']['n']} 个）",
           f"耗时：{_fmt_secs(time.monotonic() - start)}，并行进程数：{workers}", ""]
    if errors:
        out += [f"[注意] 有 {errors} 批文件读取出错", ""]
    out += [f"子文件夹数：{len(folders)}",
            f"根目录散落文件数：{sum(1 for p in input_dir.iterdir() if p.is_file())}", ""]
    out += ["== 各文件夹概况 =="] + lines + [""]

    def section(title, counter, limit=None):
        out.append(f"== {title} ==")
        items = counter.most_common(limit)
        out.extend(f"  {k}: {v}" for k, v in items) if items else out.append("  （无）")
        out.append("")

    section("原始数据中含患者姓名/ID 的其他字段（抽样文件数）", total["leak"])
    section("模拟脱敏后仍残留的字段（应为空）", total["residual"])
    section("疑似图像烧录文字的序列", total["burned"])
    section("传输语法", total["tsyntax"])
    section("SOP 类型", total["sop"])
    for kw in SAFE_VALUE_TAGS:
        section(kw, total["v:" + kw], 30)
    section("厂商私有字段创建者（抽样文件数）", total["private"])
    section("出现过的标准字段（抽样文件数）", total["keywords"])
    return "\n".join(out)


class ReportSaveError(Exception):
    """探查已经做完，但报告保存失败。text 是完整报告，调用方可以换个位置再存。"""

    def __init__(self, text, report, err):
        self.text, self.report = text, report
        reason = ("磁盘空间不足（Errno 28）" if is_disk_full(err) else str(err))
        super().__init__(f"探查已完成，但报告保存失败：{reason}\n{report}")


def check_report_place(report):
    """开始探查前先确认报告能存下，免得探查跑完才发现磁盘满了。"""
    parent = Path(report).resolve().parent
    if not parent.is_dir():
        raise FileNotFoundError(f"保存报告的文件夹不存在：{parent}")
    try:
        free_mb = shutil.disk_usage(parent).free // (1024 * 1024)
    except OSError:
        return  # 有的网络盘查不到剩余空间，不拦，保存失败时还有兜底
    if free_mb < MIN_FREE_MB:
        raise OSError(28, f"保存报告的磁盘只剩 {free_mb} MB 空间（Errno 28 磁盘空间不足）。"
                          f"请把报告存到别的盘，或先清出一些空间：{report}")


def save_report(text, report):
    """新建文件写入报告，已存在则报错，不覆盖。"""
    with open(report, "x", encoding="utf-8-sig") as f:
        f.write(text)


def probe_to_file(input_dir, report, **kwargs):
    """探查并保存报告（已存在则报错，不覆盖），同时写一条处理记录。用户停止时返回 None。

    报告保存失败时抛出 ReportSaveError，里面带着完整报告，不会白跑一趟。
    """
    from .history import new_run_id, record
    if Path(report).exists():
        raise FileExistsError(f"报告文件已存在，为避免覆盖请换一个文件名：{report}")
    check_report_place(report)
    rid = new_run_id()
    record(rid, "start", kind="结构探查", input=str(Path(input_dir).resolve()), report=str(Path(report).resolve()))
    result = "出错"
    try:
        text = probe(input_dir, **kwargs)
        if text is None:
            result = "已停止（未生成报告）"
            return None
        try:
            save_report(text, report)
        except OSError as e:
            result = "探查完成，但报告保存失败" + ("（磁盘空间不足）" if is_disk_full(e) else "")
            raise ReportSaveError(text, report, e) from e
        result = "完成"
        return text
    finally:
        record(rid, "end", result=result)
