"""整体流程：扫描 → 分配编号 → 并行脱敏 → 校验 → 写映射表。"""

import errno
import io
import multiprocessing
import os
import re
import shutil
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pydicom
from pydicom.errors import InvalidDicomError
from pydicom.filereader import read_partial

from . import rules
from .anonymizer import (UidMapper, anonymize, collect_needles, compute_age, element_vr, has_cjk, raw_is_empty)
from pydicom.datadict import keyword_for_tag
from .history import new_run_id, record
from .mapping import MappingTable, backup_mapping, check_writable

DICOMDIR_CLASS = "1.2.840.10008.1.3.10"
IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
PIXEL_TAG = b"\xe0\x7f\x10\x00"
MAX_WORKERS = int(os.environ.get("DEID_WORKERS", 0)) or max(1, min(4, os.cpu_count() or 1))


# ---------------------------------------------------------------- 扫描

@dataclass
class FileRec:
    path: str
    study_uid: str
    series_uid: str
    study_key: str
    series_number: int
    instance_number: int
    series_desc: str
    burned_in: str  # 疑似烧录文字的原因，空串表示无
    size: int = 0
    burned_strong: bool = False  # 强信号（彩色/截图类/设备标记），一定排除


@dataclass
class PatientScan:
    folder: str
    files: list = field(default_factory=list)
    non_dicom: list = field(default_factory=list)
    dicomdir: int = 0
    patient_ids: Counter = field(default_factory=Counter)
    needles: set = field(default_factory=set)
    info: dict = field(default_factory=dict)
    error: str = ""
    info_counts: dict = field(default_factory=lambda: defaultdict(Counter))

    @property
    def name(self):
        return Path(self.folder).name

    @property
    def patient_id(self):
        return self.patient_ids.most_common(1)[0][0] if self.patient_ids else ""


def looks_like_dicom(path):
    """只读文件开头 132 字节判断是否可能是 DICOM，避免把 pdf/jpg 等大文件当 DICOM 强行解析。"""
    try:
        with open(path, "rb") as f:
            head = f.read(132)
    except OSError:
        return False
    if head[128:132] == b"DICM":
        return True
    # 没有 128 字节前导的老格式：开头直接是 (0002,xxxx) 或 (0008,xxxx) 字段
    return head[:2] in (b"\x02\x00", b"\x08\x00")


def _stop_after_group_0028(tag, vr, length):
    return tag.group > 0x0028


def read_header_fast(path):
    """快速读取文件头前部（0002–0028 组）。只打开一次文件，不读私有大字段和像素。

    扫描、探查要用的字段（患者信息、日期、UID、序列号、模态、BurnedInAnnotation 等）
    都在这些组里，所以不需要往后解析。不是 DICOM 返回 None。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(132)
            if head[128:132] == b"DICM":
                force = False
            elif head[:2] in (b"\x02\x00", b"\x08\x00"):
                force = True  # 没有 128 字节前导的老格式
            else:
                return None
            f.seek(0)
            ds = read_partial(f, stop_when=_stop_after_group_0028, force=force)
    except Exception:
        return None
    if force and not ("SOPInstanceUID" in ds and "SOPClassUID" in ds):
        return None
    return ds


def read_dicom(path, header_only=True, **kwargs):
    """能读成 DICOM 就返回 Dataset，否则返回 None。兼容没有 128 字节前导的老文件。

    kwargs 透传给 dcmread，例如 specific_tags（只解析指定字段）、defer_size（大字段不读入内存）。
    """
    if not looks_like_dicom(path):
        return None
    if kwargs.get("specific_tags") is not None:
        # 判断无前导老格式文件是否为 DICOM 需要这两个字段
        kwargs["specific_tags"] = list(kwargs["specific_tags"]) + ["SOPInstanceUID", "SOPClassUID"]
    try:
        return pydicom.dcmread(path, stop_before_pixels=header_only, **kwargs)
    except InvalidDicomError:
        pass
    except Exception:
        return None
    try:
        ds = pydicom.dcmread(path, stop_before_pixels=header_only, force=True, **kwargs)
        if "SOPInstanceUID" in ds and "SOPClassUID" in ds:
            return ds
    except Exception:
        pass
    return None


def _int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def burned_in_check(ds):
    """判断图像里是否可能烧录了文字（姓名等直接画在像素上）。返回 (原因, 是否强信号)，原因为空表示没问题。"""
    if str(ds.get("BurnedInAnnotation", "")).upper() == "YES":
        return "BurnedInAnnotation=YES", True
    if str(ds.get("SOPClassUID", "")).startswith(rules.BURNED_IN_SOP_CLASS_PREFIX):
        return "截图类(Secondary Capture)", True
    if str(ds.get("Modality", "")).upper() in rules.BURNED_IN_MODALITIES:
        return f"Modality={ds.Modality}", True
    photometric = str(ds.get("PhotometricInterpretation", "")).upper()
    if photometric.startswith(rules.COLOR_PHOTOMETRIC) or _int(ds.get("SamplesPerPixel"), 1) > 1:
        return f"彩色图像({photometric or 'SamplesPerPixel>1'})", True
    image_type = str(ds.get("ImageType", "")).upper()
    for kw in rules.BURNED_IN_IMAGE_TYPES:
        if kw in image_type:
            return f"ImageType含“{kw}”", True
    text = " ".join([str(ds.get("SeriesDescription", "")), image_type]).upper()
    for kw in rules.BURNED_IN_KEYWORDS:
        if kw in text:
            return f"描述含“{kw}”", False
    return "", False


def burned_in_reason(ds):
    return burned_in_check(ds)[0]


SCAN_CHUNK = 200  # 扫描时每个并行任务读多少个文件头


def list_files(folder):
    """列出患者文件夹里的所有文件 [(路径, 大小)]。

    用 os.scandir：Windows 上目录项自带文件大小，不需要再逐个 stat。顺序和 os.walk 一致。
    """
    out = []

    def walk(d):
        with os.scandir(d) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            if e.is_file() and e.name not in IGNORED_NAMES and not e.name.startswith("._"):
                out.append((e.path, e.stat().st_size))
        for e in entries:
            if e.is_dir():
                walk(e.path)

    walk(folder)
    return out


def scan_files(folder, items):
    """读一批文件的文件头（可在子进程里运行）。返回只含这批文件的 PatientScan，之后用 merge_scan 合并。"""
    scan = PatientScan(folder=str(folder))
    try:
        for path, size in items:
            checkpoint()
            ds = read_header_fast(path)
            if ds is None:
                scan.non_dicom.append(os.path.relpath(path, folder))
                continue
            meta_class = str(getattr(ds, "file_meta", {}).get("MediaStorageSOPClassUID", ""))
            if os.path.basename(path).upper() == "DICOMDIR" or meta_class == DICOMDIR_CLASS \
                    or "DirectoryRecordSequence" in ds:
                scan.dicomdir += 1
                continue
            pid = str(ds.get("PatientID", "") or "").strip()
            if pid:
                scan.patient_ids[pid] += 1
            scan.needles |= collect_needles(ds)
            compute_age(ds)
            for key, kw in (("name", "PatientName"), ("sex", "PatientSex"), ("age", "PatientAge"),
                            ("birth", "PatientBirthDate"), ("study_date", "StudyDate"),
                            ("accession", "AccessionNumber")):
                v = str(ds.get(kw, "") or "").strip()
                if v:
                    scan.info_counts[key][v] += 1
            burned = burned_in_check(ds)
            scan.files.append(FileRec(
                path=path,
                study_uid=str(ds.get("StudyInstanceUID", "")),
                series_uid=str(ds.get("SeriesInstanceUID", "")),
                study_key=f"{ds.get('StudyDate', '')}{ds.get('StudyTime', '')}",
                series_number=_int(ds.get("SeriesNumber"), 99999),
                instance_number=_int(ds.get("InstanceNumber"), 999999),
                series_desc=str(ds.get("SeriesDescription", "") or ""),
                burned_in=burned[0],
                size=size,
                burned_strong=burned[1],
            ))
    except Stopped:
        scan.error = "已停止"
    except Exception as e:
        scan.error = f"扫描出错: {e}"
    return scan


def merge_scan(folder, parts):
    """把同一文件夹的多批扫描结果（按顺序）合并成一个。"""
    scan = PatientScan(folder=str(folder))
    for part in parts:
        scan.files.extend(part.files)
        scan.non_dicom.extend(part.non_dicom)
        scan.dicomdir += part.dicomdir
        scan.patient_ids.update(part.patient_ids)
        scan.needles |= part.needles
        for k, c in part.info_counts.items():
            scan.info_counts[k].update(c)
        scan.error = scan.error or part.error
    scan.info = {k: "; ".join(v for v, _ in c.most_common()) for k, c in scan.info_counts.items()}
    return scan


def scan_patient(folder):
    try:
        items = list_files(folder)
    except Exception as e:
        return PatientScan(folder=str(folder), error=f"扫描出错: {e}")
    return merge_scan(folder, [scan_files(folder, items)])


# ---------------------------------------------------------------- 处理

def _study_date(rec):
    d = rec.study_key[:8]
    return d if len(d) == 8 and d.isdigit() else ""


def plan_output(files):
    """规划输出目录：时间点 T1、T2……（按检查日期先后）→ 序列 S01、S02…… → 文件 0001.dcm……

    同一天的检查算同一个时间点；没有检查日期的，按 Study UID 单独成为一个时间点。
    返回 ([(相对目录 "T1/S01", [FileRec...]), ...], [("T1", "2024-01-01"), ...])
    """
    by_tp = defaultdict(list)
    for f in files:
        d = _study_date(f)
        by_tp[("D", d) if d else ("U", f.study_uid)].append(f)
    # 有日期的按日期排在前，没日期的排在后
    tps = sorted(by_tp.items(), key=lambda kv: (kv[0][0] != "D", min(f.study_key for f in kv[1]), kv[0][1]))
    plan, timepoints = [], []
    for t, ((kind, val), recs) in enumerate(tps, 1):
        tname = f"T{t}"
        timepoints.append((tname, f"{val[:4]}-{val[4:6]}-{val[6:]}" if kind == "D" else "日期未知"))
        groups = defaultdict(list)
        for f in recs:
            groups[(f.study_uid, f.series_uid)].append(f)
        ordered = sorted(groups.values(),
                         key=lambda g: (g[0].study_key, g[0].study_uid, g[0].series_number, g[0].series_uid))
        width = max(2, len(str(len(ordered))))
        for i, g in enumerate(ordered, 1):
            plan.append((f"{tname}/S{i:0{width}d}", sorted(g, key=lambda f: (f.instance_number, f.path))))
    return plan, timepoints


def split_burned_in(files):
    """把疑似烧录文字的序列挑出来。返回 (保留的文件, 排除的序列说明列表)。

    - 强信号（彩色、截图类、设备标记 BurnedInAnnotation/屏幕截图/三维渲染）：整个序列排除
    - 弱信号（只是序列描述里有 DOSE/REPORT 等词）：排除；但如果它是该时间点张数最多的序列
      （主 CT），就保留并提示人工检查，防止误删主数据
    """
    plan, _ = plan_output(files)
    by_tp = defaultdict(list)
    for sname, recs in plan:
        by_tp[sname.split("/")[0]].append(recs)
    kept, excluded = [], []
    for tname, series in by_tp.items():
        largest = max(series, key=len)
        for recs in series:
            reasons = sorted({r.burned_in for r in recs if r.burned_in})
            strong = any(r.burned_strong for r in recs)
            if reasons and (strong or recs is not largest):
                r0 = recs[0]
                excluded.append(f"{tname} 序列号{r0.series_number if r0.series_number != 99999 else '?'}"
                                f"({r0.series_desc or '无描述'}; {'/'.join(reasons)}; {len(recs)} 个文件)")
            else:
                kept.extend(recs)
    return kept, excluded


def short_error(e):
    """异常信息只保留第一行（pydicom 会把整段 traceback 塞进异常信息里）。"""
    lines = str(e).strip().splitlines()
    return f"{type(e).__name__}: {lines[0] if lines else ''}"[:300]


def is_disk_full(e):
    return (isinstance(e, OSError) and e.errno == errno.ENOSPC) or "No space left" in str(e) \
        or getattr(e, "winerror", None) == 112


def read_full(path):
    """完整读取一个 DICOM 文件：一次系统调用读进内存再解析（Windows 上大量小读比一次大读慢很多）。"""
    with open(path, "rb", buffering=0) as f:
        data = f.read()
    return pydicom.dcmread(io.BytesIO(data), force=data[128:132] != b"DICM")


class Stopped(Exception):
    """用户点了“停止”。"""


class Control:
    """暂停 / 继续 / 停止。用多进程 Event，所有子进程共享同一个开关。

    子进程在开始处理每个文件之前检查开关，所以暂停/停止总是在当前文件写完之后生效，
    不会留下写了一半的文件。
    """

    def __init__(self):
        self.run_event = multiprocessing.Event()   # 置位 = 运行，清除 = 暂停
        self.run_event.set()
        self.stop_event = multiprocessing.Event()

    def pause(self):
        self.run_event.clear()

    def resume(self):
        self.run_event.set()

    def stop(self):
        self.stop_event.set()
        self.run_event.set()  # 唤醒暂停中的进程，让它们看到“停止”

    @property
    def paused(self):
        return not self.run_event.is_set() and not self.stop_event.is_set()

    @property
    def stopped(self):
        return self.stop_event.is_set()


_progress_queue = None
_run_event = None
_stop_event = None


def _init_worker(q=None, run_event=None, stop_event=None):
    global _progress_queue, _run_event, _stop_event
    _progress_queue, _run_event, _stop_event = q, run_event, stop_event


def checkpoint():
    """暂停时在这里等待；已停止则抛出 Stopped。"""
    if _stop_event is not None and _stop_event.is_set():
        raise Stopped
    if _run_event is not None:
        while not _run_event.wait(0.5):
            pass
        if _stop_event is not None and _stop_event.is_set():
            raise Stopped


def _report(n):
    if _progress_queue is not None and n:
        try:
            _progress_queue.put_nowait(n)
        except Exception:
            pass


CHUNK_FILES = 40  # 每个并行任务处理的文件数：大患者拆成多块，所有核心一直有活干，最后不会只剩一个核心在跑


def new_result(code):
    return {"code": code, "written": 0, "existing": 0, "series": 0, "errors": [], "verify": [],
            "burned_in": [], "excluded": [], "excluded_files": 0, "cleaned": Counter(), "disk_full": False,
            "stopped": False, "timepoints": []}


def merge_result(total, part):
    for k in ("written", "existing"):
        total[k] += part[k]
    for k in ("errors", "verify"):
        total[k].extend(part[k])
    total["cleaned"].update(part["cleaned"])
    total["disk_full"] |= part["disk_full"]
    total["stopped"] |= part["stopped"]
    return total


def prepare_patient(code, files, output_root, secret, exclude_burned_in=True):
    """在主进程里做：排除疑似烧录文字的序列、检查输出目录、规划 T/S 目录。
    返回 (result, [(原路径, 相对输出路径), ...])。

    出问题（旧版目录结构、别的患者的数据、和本次规划对不上）时任务列表为空，原因写在 result["errors"]。
    """
    result = new_result(code)
    if exclude_burned_in:
        kept, result["excluded"] = split_burned_in(files)
        result["excluded_files"] = len(files) - len(kept)
        files = kept
        if not files:
            return result, []
    out_dir = Path(output_root) / code
    uid_map = UidMapper(secret)

    # 旧版本（v5 及以前）输出是 P0001/S01/…，新结构是 P0001/T1/S01/…，不能混在一个文件夹里
    if out_dir.is_dir() and any(p.is_dir() and re.fullmatch(r"S\d+", p.name) for p in out_dir.iterdir()):
        result["errors"].append(f"输出目录 {out_dir} 是旧版本的目录结构（没有 T1/T2 时间点层），"
                                f"为避免新旧结构混在一起已跳过。请换一个新的输出文件夹（编号不会变）")
        return result, []

    # 续跑：输出目录已存在时，先确认里面确实是同一个患者（用同一密钥映射出的 Study UID 对得上）
    if out_dir.exists():
        existing = next(out_dir.rglob("*.dcm"), None)
        if existing is not None:
            expected = {uid_map(f.study_uid) for f in files}
            try:
                found = str(pydicom.dcmread(existing, stop_before_pixels=True).get("StudyInstanceUID", ""))
            except Exception:
                found = ""
            if found not in expected:
                result["errors"].append(f"输出目录 {out_dir} 里已有其他患者（或其他映射表）的数据，已跳过")
                return result, []

    series, result["timepoints"] = plan_output(files)
    result["series"] = len(series)

    # 续跑：已有的每个序列目录必须和本次规划的是同一个序列，而且不能有本次没有的目录
    # （比如上次没排除的截图序列、或编号规则变了），否则拒绝，避免不同序列混在同一个目录里
    if out_dir.is_dir():
        planned = {sname: recs[0].series_uid for sname, recs in series}
        mismatch = []
        for tdir in sorted(p for p in out_dir.iterdir() if p.is_dir()):
            for sdir in sorted(p for p in tdir.iterdir() if p.is_dir()):
                rel = f"{tdir.name}/{sdir.name}"
                first = next((f for f in sorted(os.listdir(sdir)) if f.endswith(".dcm")), None)
                if first is None:
                    continue
                if rel not in planned:
                    mismatch.append(rel)
                    continue
                try:
                    found = str(pydicom.dcmread(sdir / first, stop_before_pixels=True,
                                                specific_tags=["SeriesInstanceUID"]).get("SeriesInstanceUID", ""))
                except Exception:
                    found = ""
                if found != uid_map(planned[rel]):
                    mismatch.append(rel)
        if mismatch:
            result["errors"].append(
                f"输出目录 {out_dir} 里的 {', '.join(mismatch[:5])} 和本次规划的序列对不上"
                f"（多半是旧版本输出的，里面可能还有现在会被排除的截图序列）。为避免混在一起已跳过，"
                f"请换一个新的输出文件夹（编号不会变）")
            return result, []
    tasks = []
    for sname, recs in series:
        reasons = {r.burned_in for r in recs if r.burned_in}
        if reasons:
            result["burned_in"].append(f"{sname}({recs[0].series_desc or '无描述'}; {'/'.join(sorted(reasons))})")
        width = max(4, len(str(len(recs))))
        tasks.extend((rec.path, f"{code}/{sname}/{i:0{width}d}.dcm") for i, rec in enumerate(recs, 1))
    return result, tasks


def chunks(tasks, size=None):
    """按序列切块（同一个块只属于一个序列目录），块内最多 size 个文件。"""
    size = size or CHUNK_FILES
    out, cur, cur_dir = [], [], None
    for t in tasks:
        d = t[1].rsplit("/", 1)[0]
        if cur and (d != cur_dir or len(cur) >= size):
            out.append(cur)
            cur = []
        cur.append(t)
        cur_dir = d
    if cur:
        out.append(cur)
    return out


def process_chunk(code, tasks, needles, output_root, secret):
    """子进程里运行：处理一块文件（都在同一个序列目录下）。"""
    result = new_result(code)
    if not tasks:
        return result
    uid_map = UidMapper(secret)
    sdir = Path(output_root) / tasks[0][1].rsplit("/", 1)[0]
    sname = sdir.relative_to(Path(output_root) / code).as_posix()
    sdir.mkdir(parents=True, exist_ok=True)
    done = set(os.listdir(sdir))  # 一次列目录代替逐个文件 stat（Windows 上 stat 很慢）
    pending = 0
    for src, rel in tasks:
        try:
            checkpoint()
        except Stopped:
            result["stopped"] = True
            _report(pending)
            return result
        pending += 1
        if pending >= 10:
            _report(pending)
            pending = 0
        name = rel.rsplit("/", 1)[1]
        out_path = sdir / name
        if name in done:  # 上次已完成的文件：不覆盖，直接跳过
            result["existing"] += 1
            continue
        try:
            ds = read_full(src)
            result["cleaned"].update(anonymize(ds, code, uid_map, needles))
            buf = io.BytesIO()
            ds.save_as(buf, enforce_file_format=True)
            data = buf.getvalue()
            del ds
        except Exception as e:
            result["errors"].append(f"{src}: {short_error(e)}")
            continue
        # 在内存里校验即将写出的字节，不通过就不写出
        issues = verify_bytes(data, code, needles)
        if issues:
            result["verify"].append(f"{sname}/{name}: {'; '.join(issues)}（未写出）")
            continue
        # 先写 .part，写完整后再改名。.part 只可能是本软件上次没写完的临时文件，可以重写
        part = out_path.with_name(name + ".part")
        try:
            with open(part, "wb", buffering=0) as f:  # 一次系统调用写完
                f.write(data)
            # Windows 的 rename 遇到已存在的目标会报错；Mac/Linux 会覆盖，所以先查一下。.dcm 永不覆盖
            if os.name != "nt" and os.path.exists(out_path):
                raise FileExistsError(f"{out_path} 已存在")
            os.rename(part, out_path)
        except Exception as e:
            if is_disk_full(e):
                result["disk_full"] = True
                result["errors"].append(f"输出盘空间已满，在 {sname}/{name} 处停止")
                _report(pending)
                return result
            result["errors"].append(f"写入 {sname}/{name} 失败: {short_error(e)}")
            continue
        result["written"] += 1
    _report(pending)
    return result


def process_patient(code, files, needles, output_root, secret, exclude_burned_in=True):
    """单进程处理一个患者（测试和简单调用用；正式运行时 Job 把患者拆成块并行）。"""
    result, tasks = prepare_patient(code, files, output_root, secret, exclude_burned_in)
    for chunk in chunks(tasks):
        part = process_chunk(code, chunk, needles, output_root, secret)
        merge_result(result, part)
        if part["stopped"] or part["disk_full"]:
            break
    return result


def _safe_chunk(*args):
    try:
        return process_chunk(*args)
    except Exception as e:
        r = new_result(args[0])
        r["errors"].append(traceback.format_exc() if not is_disk_full(e) else "输出盘空间已满")
        r["disk_full"] = is_disk_full(e)
        return r


# ---------------------------------------------------------------- 校验

def verify_bytes(data, code, needles):
    """对即将写出的完整文件字节做独立校验（重新解析，不依赖脱敏时的内存对象）。"""
    issues = []
    idx = data.find(PIXEL_TAG)
    header = data[:idx] if idx > 0 else data
    header_lower = header.lower()
    for n in needles:
        if n.isdigit():
            continue  # 纯数字 ID 在二进制里容易误报，下面按字段值检查
        if has_cjk(n):
            if any(n.encode(enc, "ignore") in header for enc in ("utf-8", "gb18030")):
                issues.append(f"残留“{n}”")
        elif n.lower().encode("ascii", "ignore") in header_lower:
            issues.append(f"残留“{n}”")

    ds = pydicom.dcmread(io.BytesIO(data), stop_before_pixels=True)
    if str(ds.get("PatientName", "")) != code or str(ds.get("PatientID", "")) != code:
        issues.append("PatientName/PatientID 未替换")
    needles_lower = [n.lower() for n in needles]

    def check(d):  # 同样只解码需要看内容的字段
        for tag in list(d.keys()):
            if tag.is_private:
                issues.append(f"私有字段 {tag}")
                continue
            vr = element_vr(d, tag)
            kw = keyword_for_tag(tag)
            if vr in ("PN", "DA", "DT"):
                if kw != "PatientName" and not raw_is_empty(d, tag):
                    issues.append(f"{kw} 未清空")
            elif vr == "SQ":
                for item in d[tag].value:
                    check(item)
            elif vr in ("LO", "SH", "LT", "ST", "UT", "UC") and needles_lower:
                value = d[tag].value
                if value and any(n in str(value).lower() for n in needles_lower):
                    issues.append(f"{kw} 含患者信息")

    check(ds)
    return sorted(set(issues))


def verify_file(path, code, needles):
    return verify_bytes(Path(path).read_bytes(), code, needles)


# ---------------------------------------------------------------- 编排

def disk_free(path):
    path = Path(path)
    while not path.exists() and path.parent != path:
        path = path.parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def same_disk(a, b):
    """两个路径是否在同一个分区上（输出路径可能还不存在，用最近的已存在上级目录）。"""
    def dev(path):
        path = Path(path)
        while not path.exists() and path.parent != path:
            path = path.parent
        return os.stat(path).st_dev
    try:
        return dev(a) == dev(b)
    except OSError:
        return False


def _fmt_secs(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}时{sec % 3600 // 60}分"
    return f"{sec // 60}分{sec % 60}秒" if sec >= 60 else f"{sec}秒"


def unique_path(path):
    """返回一个不存在的文件名（已存在就加 _2、_3……），保证不覆盖已有文件。"""
    path = Path(path)
    n = 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        n += 1
    return candidate


class Job:
    def __init__(self, input_dir, output_dir, mapping_path, log=print, progress=None, confirm=None, workers=None,
                 control=None, exclude_burned_in=True):
        self.control = control or Control()
        self.exclude_burned_in = exclude_burned_in  # 疑似烧录文字的序列（截图、三维渲染、剂量报告）不输出
        self.workers = max(1, int(workers or MAX_WORKERS))
        self.input_dir = Path(input_dir).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.mapping_path = Path(mapping_path).resolve()
        self._log_cb = log
        self.progress = progress or (lambda stage, done, total: None)
        self.confirm = confirm or (lambda text: True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = unique_path(self.mapping_path.parent / f"脱敏日志_{stamp}.txt")
        self._log_lines = []
        self.run_id = None   # 确认开始后才生成，写进处理记录和映射表的“批次”列
        self.stats = {}

    def log(self, msg=""):
        self._log_lines.append(msg)
        self._log_cb(msg)

    def _flush_log(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "w", encoding="utf-8-sig") as f:  # 只写本次运行自己的日志文件
            f.write("\n".join(self._log_lines) + "\n")

    def check_paths(self):
        inp, out, mp = self.input_dir, self.output_dir, self.mapping_path
        if not inp.is_dir():
            return f"输入文件夹不存在：{inp}"
        if out == inp or out.is_relative_to(inp) or inp.is_relative_to(out):
            return "输出文件夹不能和输入文件夹相同或互相包含"
        if mp.is_relative_to(out):
            return "映射表不能放在输出文件夹里（输出文件夹是要发出去的）"
        if mp.is_relative_to(inp):
            return "映射表不要放在原始数据文件夹里"
        if mp.suffix.lower() != ".xlsx":
            return "映射表文件需为 .xlsx"
        return check_writable(mp)

    def run(self):
        """返回 True 表示全部成功；出错时返回 False 并已写日志。"""
        # 扫描和脱敏共用一组子进程：每启动一个子进程都要重新加载 pydicom，Windows 上尤其慢
        self._q = multiprocessing.Queue()
        self._pool = ProcessPoolExecutor(self.workers, initializer=_init_worker,
                                         initargs=(self._q, self.control.run_event, self.control.stop_event))
        try:
            return self._run()
        finally:
            self._pool.shutdown(wait=True, cancel_futures=True)

    def _run(self):
        err = self.check_paths()
        if err:
            self.log(f"[错误] {err}")
            return False
        try:
            backup = backup_mapping(self.mapping_path)
            mapping = MappingTable(self.mapping_path)
        except Exception as e:
            self.log(f"[错误] 无法打开映射表：{e}")
            return False

        self.log(f"输入：{self.input_dir}")
        self.log(f"输出：{self.output_dir}")
        self.log(f"映射表：{self.mapping_path}")
        if backup:
            self.log(f"映射表已备份：{backup}")

        entries = sorted(self.input_dir.iterdir(), key=lambda p: p.name)
        folders = [p for p in entries if p.is_dir() and not p.name.startswith(".")]
        loose = [p for p in entries if p.is_file() and p.name not in IGNORED_NAMES and not p.name.startswith(".")]
        if loose:
            self.log(f"[警告] 输入文件夹根目录下有 {len(loose)} 个散落文件，不属于任何患者文件夹，已忽略")
        if not folders:
            self.log("[错误] 输入文件夹下没有子文件夹")
            return False

        # 1. 扫描
        self.log(f"\n== 扫描 {len(folders)} 个文件夹 ==")
        # 各文件夹并行列出文件；列完一个，就把它的文件切成小块并行读文件头（大文件夹不会拖在最后）
        ctl = self.control
        parts, nparts, list_errors = defaultdict(dict), {}, {}
        files_total = files_done = 0
        with nullcontext(self._pool) as pool:
            listing = {pool.submit(list_files, str(f)): f for f in folders}
            scanning = {}
            pending = set(listing)
            while pending:
                finished, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                if ctl.stopped:
                    for f in pending:
                        f.cancel()
                    break
                for fut in finished:
                    if fut in listing:
                        folder = listing[fut]
                        try:
                            items = fut.result()
                        except Exception as e:
                            list_errors[folder] = f"扫描出错: {e}"
                            continue
                        blocks = [items[k:k + SCAN_CHUNK] for k in range(0, len(items), SCAN_CHUNK)]
                        nparts[folder] = len(blocks)
                        files_total += len(items)
                        for k, block in enumerate(blocks):
                            sf = pool.submit(scan_files, str(folder), block)
                            scanning[sf] = (folder, k, len(block))
                            pending.add(sf)
                    else:
                        folder, k, n = scanning[fut]
                        parts[folder][k] = fut.result()
                        files_done += n
                self.progress(f"扫描（已列出 {len(nparts) + len(list_errors)}/{len(folders)} 个文件夹）",
                              files_done, max(files_total, 1))
        if not ctl.stopped:
            scans = [PatientScan(folder=str(f), error=list_errors[f]) if f in list_errors
                     else merge_scan(f, [parts[f][k] for k in range(nparts[f])]) for f in folders]
        if ctl.stopped:
            self.log("已停止（扫描阶段），没有写出任何文件。")
            self._flush_log()
            return False
        scans.sort(key=lambda s: s.name)

        # 2. 分配编号
        existing = mapping.code_by_patient_id()
        by_folder = {str(r["原文件夹"]): r["编号"] for r in mapping.rows()}
        next_no = mapping.max_number() + 1
        by_pid = {}
        groups = defaultdict(list)  # code -> [PatientScan]
        skipped = []
        for s in scans:
            if s.error or not s.files:
                skipped.append(s)
                continue
            pid = s.patient_id
            if pid and pid in existing:
                code = existing[pid]
            elif not pid and s.name in by_folder and (self.output_dir / by_folder[s.name]).exists():
                code = by_folder[s.name]  # 无患者ID的续跑
            elif pid and pid in by_pid:
                code = by_pid[pid]
            else:
                code = f"P{next_no:04d}"
                next_no += 1
            if pid:
                by_pid[pid] = code
            groups[code].append(s)

        lines = [f"共 {len(scans)} 个文件夹，将生成 {len(groups)} 个患者编号："]
        for code, ss in groups.items():
            for s in ss:
                tag = "（沿用已有编号）" if s.patient_id in existing else ""
                extra = f"，{len(s.patient_ids)} 个不同患者ID！" if len(s.patient_ids) > 1 else ""
                lines.append(f"  {s.name}  →  {code}{tag}  ({len(s.files)} 个DICOM文件{extra})")
        for s in skipped:
            lines.append(f"  {s.name}  →  跳过：{s.error or '没有找到 DICOM 文件'}")
        merged = [c for c, ss in groups.items() if len(ss) > 1]
        if merged:
            lines.append(f"[注意] {', '.join(merged)} 由多个文件夹合并（患者ID相同，视为同一患者的不同检查），"
                         f"输出时按检查日期分为 T1、T2……")
        if self.exclude_burned_in:
            excl = [(code, e) for code, ss in groups.items()
                    for e in split_burned_in([f for s in ss for f in s.files])[1]]
            if excl:
                lines.append(f"[排除] {len(excl)} 个疑似图像内烧录文字的序列（截图、三维渲染、剂量报告等，"
                             f"图上可能直接印着姓名）不会输出：")
                lines.extend(f"  {code} {e}" for code, e in excl[:15])
                if len(excl) > 15:
                    lines.append(f"  ……共 {len(excl)} 个，完整列表见日志")
        old_layout = [c for c in groups if (self.output_dir / c).is_dir() and any(
            p.is_dir() and re.fullmatch(r"S\d+", p.name) for p in (self.output_dir / c).iterdir())]
        if old_layout:
            lines.append(f"[注意] 输出文件夹里 {', '.join(old_layout[:10])}{' 等' if len(old_layout) > 10 else ''} "
                         f"是旧版本目录结构（没有 T1/T2 层），这些患者会被跳过。建议换一个新的输出文件夹，编号不会变。")
        need = sum(f.size for ss in groups.values() for s in ss for f in s.files)
        free = disk_free(self.output_dir)
        lines.append(f"预计输出约 {need / 1e9:.1f} GB，输出盘剩余 {free / 1e9:.1f} GB")
        if same_disk(self.input_dir, self.output_dir):
            lines.append("[提速建议] 原始数据和输出在同一个盘上。如果是机械硬盘或移动硬盘，边读边写会互相抢，"
                         "把输出放到另一块硬盘（最好是固态硬盘）通常会快很多。")
        if free < need * 1.02 + 0.5e9:
            lines.append("[空间可能不足] 输出盘剩余空间小于预计输出大小。可以先处理，空间写满时软件会自动停止；"
                         "腾出空间后用相同设置再运行一次，会从中断处继续（已完成的文件不重复处理）。")
        plan_text = "\n".join(lines)
        self.log(plan_text)
        if not groups:
            self.log("[错误] 没有可处理的数据")
            self._flush_log()
            return False
        if not self.confirm(plan_text):
            self.log("用户取消")
            self._flush_log()
            return False

        # 3. 脱敏（开始和结束都写进处理记录，下次打开软件可以查看）
        total_files = sum(len(s.files) for ss in groups.values() for s in ss)
        self.run_id = new_run_id()
        self.stats = {"folders": len(scans), "patients": len(groups), "skipped": len(skipped),
                      "files_total": total_files}
        warn = record(self.run_id, "start", kind="脱敏", input=str(self.input_dir), output=str(self.output_dir),
                      mapping=str(self.mapping_path), log=str(self.log_path), workers=self.workers,
                      exclude_burned_in=self.exclude_burned_in, **self.stats)
        if warn:
            self.log(f"[提示] {warn}")
        result = "出错（程序异常）"
        try:
            ok, result = self._process(groups, skipped, mapping, total_files)
            return ok
        finally:
            record(self.run_id, "end", result=result, **self.stats)

    def _process(self, groups, skipped, mapping, total_files):
        ctl = self.control
        now = lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        batch = {"原始数据文件夹": str(self.input_dir), "批次": self.run_id}
        for s in skipped:  # 跳过的文件夹也记一行，以后核对时知道它看过了
            mapping.append({"原文件夹": s.name, "状态": f"跳过：{s.error or '没有找到 DICOM 文件'}",
                            "跳过的非DICOM文件数": len(s.non_dicom), "处理时间": now(), **batch})
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ok = True
        disk_full = False
        written = existing = 0
        status_counts = Counter()
        # 先在主进程里检查输出目录、规划好每个文件的输出位置，再把所有文件切成小块分给各进程
        results, remaining, cancelled = {}, {}, Counter()
        jobs = []  # (code, chunk, needles)
        for code, ss in groups.items():
            files = [f for s in ss for f in s.files]
            needles = set().union(*(s.needles for s in ss))
            results[code], tasks = prepare_patient(code, files, str(self.output_dir), mapping.secret,
                                                   self.exclude_burned_in)
            parts = chunks(tasks)
            remaining[code] = len(parts)
            jobs.extend((code, c, needles) for c in parts)
        total_files = sum(len(c) for _, c, _ in jobs)
        excluded_files = sum(r["excluded_files"] for r in results.values())
        self.stats.update(files_total=total_files, excluded_files=excluded_files)
        self.log(f"\n== 开始脱敏（{self.workers} 个并行进程，共 {total_files} 个文件"
                 + (f"；另有 {excluded_files} 个疑似烧录文字的文件不输出" if excluded_files else "") + "）==")
        n_done = 0
        n_patients = len(groups)

        def finish(code):
            """一个患者的所有块都结束了：写日志和映射表。"""
            nonlocal ok, written, existing, n_done
            r = results[code]
            ran = remaining_total[code] - cancelled[code]
            if remaining_total[code] and ran == 0:
                return  # 一块都没开始（停止或磁盘满），不记录，下次从头处理
            n_done += 1
            if cancelled[code] and not (r["stopped"] or r["disk_full"]):
                # 部分块因停止或磁盘满没有开始
                r["stopped"] = True
            all_excluded = bool(r["excluded_files"]) and not r["series"] and not r["errors"]
            failed = r["written"] + r["existing"] == 0 and not r["stopped"] and not r["disk_full"] \
                and not all_excluded
            warn = bool(r["errors"] or r["verify"])
            ok &= not (failed or warn or r["stopped"] or r["disk_full"])
            if r["disk_full"] or (cancelled[code] and disk_full):
                status = "未完成（磁盘已满）"
            elif r["stopped"]:
                status = "未完成（已停止）"
            elif all_excluded:
                status = "完成（全部是截图类图像，已排除，无输出）"
            else:
                status = "失败" if failed else ("完成（有警告）" if warn else "完成")
            written += r["written"]
            existing += r["existing"]
            status_counts[status] += 1
            self.stats.update(written=written, existing=existing, status_counts=dict(status_counts))
            extra = f"，{r['existing']} 个上次已完成" if r["existing"] else ""
            tps = "; ".join(f"{t}={d}" for t, d in r["timepoints"])
            if r["excluded_files"]:
                extra += f"，排除 {r['excluded_files']} 个疑似烧录文字的文件"
            self.log(f"[{n_done}/{n_patients}] {code}: {status}，写出 {r['written']} 个文件{extra}，"
                     f"{len(r['timepoints'])} 个时间点，{r['series']} 个序列")
            for e in r["excluded"]:
                self.log(f"    [已排除] 疑似图像内烧录文字，未输出：{e}")
            for msg in r["errors"][:10]:
                self.log(f"    [错误] {msg}")
            if len(r["errors"]) > 10:
                self.log(f"    ……共 {len(r['errors'])} 个错误")
            for msg in r["verify"][:10]:
                self.log(f"    [校验未通过] {msg}")
            if len(r["verify"]) > 10:
                self.log(f"    ……共 {len(r['verify'])} 个文件校验未通过")
            if r["cleaned"]:
                detail = "，".join(f"{k}×{v}" for k, v in r["cleaned"].most_common())
                self.log(f"    已清空含患者姓名/ID 的文本字段：{detail}")
            for b in r["burned_in"]:
                self.log(f"    [提示] 疑似图像内烧录文字，但它是该时间点张数最多的主序列，已保留，请人工检查：{b}")
            for s in groups[code]:
                notes = []
                if s.non_dicom:
                    self.log(f"    跳过 {s.name} 中 {len(s.non_dicom)} 个非 DICOM 文件：" + ", ".join(s.non_dicom[:10]))
                if s.dicomdir:
                    notes.append("已丢弃 DICOMDIR")
                if len(s.patient_ids) > 1:
                    notes.append(f"文件夹内有多个患者ID: {', '.join(s.patient_ids)}")
                if r["errors"]:
                    notes.append(f"{len(r['errors'])} 个错误")
                if r["verify"]:
                    notes.append(f"{len(r['verify'])} 个文件校验未通过（未写出）")
                mapping.append({
                    "编号": code, "原文件夹": s.name, "患者姓名": s.info.get("name", ""),
                    "患者ID": s.patient_id, "检查号": s.info.get("accession", ""),
                    "性别": s.info.get("sex", ""), "年龄": s.info.get("age", ""),
                    "出生日期": s.info.get("birth", ""), "检查日期": s.info.get("study_date", ""),
                    "DICOM文件数": len(s.files), "序列数": r["series"],
                    "跳过的非DICOM文件数": len(s.non_dicom),
                    "疑似烧录文字的序列": "; ".join([f"已排除 {e}" for e in r["excluded"]]
                                                    + [f"已保留请检查 {b}" for b in r["burned_in"]]),
                    "状态": status, "备注": "; ".join(notes), "处理时间": now(),
                    "时间点": tps, **batch,
                })
            self._flush_log()  # 每完成一个患者就落盘，程序意外退出时日志也在

        remaining_total = dict(remaining)
        for code in [c for c, n in remaining.items() if n == 0]:
            finish(code)  # 输出目录有问题等，没有可处理的文件

        q = self._q
        start = time.monotonic()
        with nullcontext(self._pool) as pool:
            futs = {pool.submit(_safe_chunk, code, chunk, needles, str(self.output_dir), mapping.secret): code
                    for code, chunk, needles in jobs}
            pending = set(futs)
            done_files = 0
            paused_total, paused_since, stop_handled = 0.0, None, False
            while pending:
                finished, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                if ctl.stopped and not stop_handled:
                    stop_handled = True
                    for f in pending:
                        f.cancel()  # 还没开始的块不再启动
                    self.log("\n正在停止：等待各进程写完当前文件……")
                if ctl.paused:
                    if paused_since is None:
                        paused_since = time.monotonic()
                        self.log("已暂停（各进程写完当前文件后暂停）")
                    self.progress("已暂停", done_files, total_files)
                elif paused_since is not None:
                    paused_total += time.monotonic() - paused_since
                    paused_since = None
                    if not ctl.stopped:
                        self.log("继续处理")
                while not q.empty():
                    try:
                        done_files += q.get_nowait()
                    except Exception:
                        break
                done_files = min(done_files, total_files)
                elapsed = time.monotonic() - start - paused_total
                eta = elapsed / done_files * (total_files - done_files) if done_files else 0
                if not ctl.paused:
                    rate = f"，{done_files / elapsed:.0f} 个/秒" if done_files and elapsed > 1 else ""
                    self.progress(f"脱敏 剩余约{_fmt_secs(eta)}{rate}", done_files, total_files)
                for fut in finished:
                    code = futs[fut]
                    if fut.cancelled():
                        cancelled[code] += 1
                    else:
                        part = fut.result()
                        merge_result(results[code], part)
                        if part["disk_full"] and not disk_full:
                            disk_full = True
                            for f in pending:
                                f.cancel()  # 还没开始的块不再启动
                            self.log("\n[严重] 输出盘空间已满，已停止启动新的任务，正在等待进行中的任务结束……")
                    remaining[code] -= 1
                    if remaining[code] == 0:
                        finish(code)
            self.stats["not_started"] = n_patients - n_done
            spent = time.monotonic() - start - paused_total
            self.log(f"脱敏耗时 {_fmt_secs(time.monotonic() - start)}"
                     + (f"（平均每秒 {written / spent:.0f} 个文件，{self.workers} 个并行进程）"
                        if written and spent > 1 else ""))

        if disk_full:
            ok = False
            self.log("\n[严重] 输出盘空间已满，处理未完成。")
            self.log("请腾出空间（或换一个更大的硬盘），然后用【同一个输出文件夹、同一个映射表】再点一次开始，")
            self.log("会从中断处继续：已完成的文件直接跳过，不会重复处理，也不会覆盖。")
        if ctl.stopped and not disk_full:
            ok = False
            self.log("\n已按要求停止。已写出的文件都是完整的。")
            self.log("以后用【同一个输出文件夹、同一个映射表】再点开始，会从停下的地方继续。")
        if disk_full:
            result = "未完成（磁盘已满）"
        elif ctl.stopped:
            result = "未完成（已停止）"
        elif ok or (all(k.startswith("完成") and "警告" not in k for k in status_counts)
                    and not self.stats["not_started"]):
            # 只是有文件夹里没有 DICOM（比如只有图片）而跳过，患者本身都处理好了，也算全部完成
            result = "全部完成" + (f"（{len(skipped)} 个文件夹没有 DICOM，已跳过）" if skipped else "")
        else:
            result = "完成（有警告或错误）"
        ok = result.startswith("全部完成")  # 界面、命令行和处理记录用同一个判断
        self.log("\n== 结束 ==")
        if disk_full:
            self.log("处理未完成（输出盘已满），请按上面的提示续跑。")
        elif ctl.stopped:
            self.log("处理未完成（已停止）。")
        elif ok:
            self.log("全部完成，校验通过。" + (f"（{len(skipped)} 个文件夹里没有 DICOM，已跳过，详见上面的记录）"
                                         if skipped else ""))
        else:
            self.log("处理完成，但有警告或错误，请查看上面的记录。")
        self.log(f"映射表已更新：{self.mapping_path}")
        self.log(f"日志：{self.log_path}")
        self.log("提醒：映射表和日志含患者信息，只能留在本地，不要随数据发出。")
        self._flush_log()
        return ok, result
