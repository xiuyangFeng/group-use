"""独立验证“单个患者”模式：用户直接把一个患者文件夹选上时，软件按一个患者处理。

覆盖：
1. detect_input_mode：批量 / 平铺单患者 / 嵌套单患者（仍按批量但输出正确）/ 空 / 混散落文件
2. Job 自动模式：选患者文件夹直接跑，输出结构和映射表正确，原始数据逐字节不变
3. Job 手动 single_patient=True/False 覆盖；多患者文件夹在单患者模式下被拒绝（不能混编号）
4. 单患者模式续跑：编号不变、不覆盖、全部“上次已完成”
5. 与批量共享映射表：沿用编号 / 新分配编号，不混患者
6. probe / probe_to_file 单患者模式：报告含“单个患者”、不含姓名/ID/文件夹名；处理记录字段
7. history.folder_status(single_patient=True)、summarize 的“（单患者模式）”
8. CLI：自动识别 / --single-patient / --batch / --probe
9. deid.gui.check_paths 自动判断与手动覆盖的提示
10. 回归：批量文件夹自动判断后仍是原来的批量行为（输出逐字节一致）

只读原始数据；所有输出/映射表/日志都写在 pytest 临时目录里。处理记录由 conftest 指到临时 DEID_HOME。
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pydicom
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import make_fake_data  # noqa: E402
from deid import pipeline  # noqa: E402
from deid.anonymizer import UidMapper  # noqa: E402
from deid.gui import check_paths  # noqa: E402
from deid.history import folder_status, load_runs, summarize  # noqa: E402
from deid.mapping import MappingTable, read_rows  # noqa: E402
from deid.pipeline import BATCH, EMPTY, SINGLE, InputLayout, Job, detect_input_mode  # noqa: E402
from deid.probe import probe, probe_to_file  # noqa: E402

# 单患者输出里不能再出现的字节（患者 3 WANG^WU 的各种标识）
LEAKS = [b"WANG", b"P5566778", b"A20240303003", b"1.2.840.113619", b"HOSPITAL", b"DR^", b"TECH^",
         b"SIEMENS", b"SN123456", b"HZ00123456", b"000789", b"ZHANG", b"SIWEI", b"A2024",
         "张三".encode("utf-8"), "张三".encode("gb18030"), "某某".encode("utf-8"), "某某".encode("gb18030")]


def snapshot(folder):
    """文件夹内每个文件：相对路径 → (大小, 修改时间, SHA256)。用来证明原始数据逐字节未变、输出没被覆盖。"""
    return {str(p.relative_to(folder)): (p.stat().st_size, p.stat().st_mtime_ns,
                                         hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(Path(folder).rglob("*")) if p.is_file()}


def read_bytes(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(Path(folder).rglob("*.dcm"))}


def job_run(inp, out, mp, **kwargs):
    logs = []
    ok = Job(inp, out, mp, log=logs.append, workers=2, **kwargs).run()
    return ok, "\n".join(logs)


def copy_run(src_out, src_map, dest):
    """复制一份已有的输出/映射表再操作，避免影响模块级 fixture。"""
    dest = Path(dest)
    shutil.copytree(src_out, dest / "out")
    shutil.copy2(src_map, dest / "map.xlsx")
    return dest / "out", dest / "map.xlsx"


def make_flat_mixed(dest, input_dir, n_a=21):
    """平铺目录：n_a 个患者 A（WANG_WU_1）的 DICOM 复制 + 1 个患者 B（LI_SIWEI）的 DICOM 排最后。"""
    dest = Path(dest)
    dest.mkdir(parents=True)
    a_files = sorted((Path(input_dir) / "WANG_WU_1").glob("*.dcm"))
    b_file = sorted((Path(input_dir) / "LI_SIWEI").glob("*.dcm"))[0]
    k = 0
    while k < n_a:
        for f in a_files:
            shutil.copy2(f, dest / f"a{k:03d}.dcm")
            k += 1
            if k >= n_a:
                break
    shutil.copy2(b_file, dest / "zz_other_patient.dcm")
    return dest


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    """生成一次假数据（含各种难点：中文/拼音姓名、嵌套目录、DICOMDIR、非 DICOM、无前导老格式等）。"""
    base = tmp_path_factory.mktemp("single_patient")
    make_fake_data.main(base / "input")
    return base


@pytest.fixture(scope="module")
def input_snapshot(data):
    return snapshot(data / "input")


@pytest.fixture(scope="module")
def wang_single(data):
    """选 WANG_WU_1（根目录直接放 DICOM）自动判断 → 单患者模式跑一次。"""
    inp = data / "input" / "WANG_WU_1"
    out, mp = data / "run_wang" / "out", data / "run_wang" / "map.xlsx"
    before = snapshot(inp)
    job = Job(inp, out, mp, log=lambda m: None, workers=2)
    logs = []
    job._log_cb = logs.append
    ok = job.run()
    assert ok is True, "\n".join(logs)
    return {"input": inp, "out": out, "map": mp, "job": job, "logs": "\n".join(logs),
            "before": before, "after": snapshot(inp), "rows": read_rows(mp),
            "outputs": read_bytes(out)}


@pytest.fixture(scope="module")
def zhang_single(data):
    """张三_20240101 是“患者/检查/序列/文件”多层结构：自动判断按批量，但手动指定单患者也必须正确。"""
    inp = data / "input" / "张三_20240101"
    out, mp = data / "run_zhang" / "out", data / "run_zhang" / "map.xlsx"
    before = snapshot(inp)
    logs = []
    ok = Job(inp, out, mp, log=logs.append, workers=2, single_patient=True).run()
    assert ok is True, "\n".join(logs)
    return {"input": inp, "out": out, "map": mp, "logs": "\n".join(logs),
            "before": before, "after": snapshot(inp), "rows": read_rows(mp),
            "outputs": read_bytes(out)}


@pytest.fixture(scope="module")
def batch_run(data):
    """批量的基线输出：自动判断（None）跑整个 test_data/input。"""
    inp = data / "input"
    out, mp = data / "run_batch" / "out", data / "run_batch" / "map.xlsx"
    before = snapshot(inp)
    logs = []
    job = Job(inp, out, mp, log=logs.append, workers=2)
    ok = job.run()
    assert ok is True, "\n".join(logs)
    return {"input": inp, "out": out, "map": mp, "job": job, "logs": "\n".join(logs),
            "before": before, "after": snapshot(inp), "rows": read_rows(mp),
            "outputs": read_bytes(out)}


# ---------------------------------------------------------------- 1. 自动判断文件夹结构

def test_detect_batch_folder(data):
    layout = detect_input_mode(data / "input")
    assert layout == InputLayout(BATCH, 5, 0, "")
    assert layout.mode == BATCH and layout.children == 5 and layout.root_dicoms == 0


@pytest.mark.parametrize("folder,children,root_dicoms", [
    ("WANG_WU_1", 0, 3),    # 平铺：根目录直接放 3 个 DICOM，没有子文件夹
    ("LI_SIWEI", 1, 5),     # 根目录 4 个 DICOM + 无前导老格式 1 个，另有 recon/ 子文件夹
])
def test_detect_flat_patient_folder_is_single(data, folder, children, root_dicoms):
    layout = detect_input_mode(data / "input" / folder)
    assert layout == InputLayout(SINGLE, children, root_dicoms, "root_dicoms")


def test_detect_nested_single_patient_stays_batch(data):
    """多层目录（患者/检查/序列/文件）自动判断仍按批量（批量也能合并成同一个编号），但会给出提示。"""
    layout = detect_input_mode(data / "input" / "张三_20240101")
    assert layout == InputLayout(BATCH, 1, 0, "same_patient")


@pytest.mark.parametrize("kind", ["empty_dir", "only_images", "only_text", "missing"])
def test_detect_empty_inputs(data, tmp_path, kind):
    if kind == "empty_dir":
        p = tmp_path / "e"
        p.mkdir()
    elif kind == "only_images":
        p = data / "input" / "空文件夹_只有图片"
    elif kind == "only_text":
        p = tmp_path / "t"
        p.mkdir()
        (p / "a.txt").write_text("不是 DICOM", encoding="utf-8")
    else:
        p = tmp_path / "does_not_exist"
    layout = detect_input_mode(p)
    assert layout.mode == EMPTY
    assert layout.children == 0 and layout.root_dicoms == 0
    assert layout.reason in ("no_pid", "no_dicom")


def test_detect_mixed_root_batch_folder(data, tmp_path):
    """批量文件夹里混了散落 DICOM：根目录 DICOM 和子文件夹里的不是同一个患者 → 批量，散落文件照旧忽略。"""
    mixed = tmp_path / "input"
    shutil.copytree(data / "input", mixed)
    shutil.copy2(data / "input" / "WANG_WU_1" / "1.dcm", mixed / "stray.dcm")
    layout = detect_input_mode(mixed)
    assert layout.mode == BATCH and layout.reason == "mixed_root"
    assert layout.children == 5 and layout.root_dicoms == 1


def test_detect_flat_multi_patient_is_not_single(data, tmp_path):
    """平铺文件夹里有两个患者的 DICOM：绝不能判成单个患者（否则会把两个人混进同一个编号）。"""
    flat = make_flat_mixed(tmp_path / "flat", data / "input")
    layout = detect_input_mode(flat)
    assert layout.mode == BATCH
    assert layout.reason == "mixed_root"
    assert layout.root_dicoms == 22   # 21 个患者 A + 1 个患者 B，全部计入（不再只看前 20 个）


def test_detect_uniform_sampling_covers_files_after_60(data, tmp_path):
    """根目录文件超过抽样上限（60）时，排在最后面的另一个患者也必须被抽到。"""
    flat = make_flat_mixed(tmp_path / "flat_big", data / "input", n_a=80)
    layout = detect_input_mode(flat)
    assert layout.mode == BATCH and layout.reason == "mixed_root"
    assert layout.root_dicoms == 60   # 抽样上限（首尾都覆盖，所以排在最后的患者 B 一定被抽到）


def test_detect_is_read_only(data, input_snapshot, tmp_path):
    before = snapshot(data / "input")
    for p in (data / "input", data / "input" / "WANG_WU_1", data / "input" / "LI_SIWEI",
              data / "input" / "张三_20240101", data / "input" / "空文件夹_只有图片",
              tmp_path / "does_not_exist"):
        detect_input_mode(p)
    assert snapshot(data / "input") == before == input_snapshot


def test_detect_flat_single_patient_over_20_files(data, tmp_path):
    """平铺单患者文件夹超过 20 个文件时仍要判成单个患者（不能因为抽样变成批量而把一个人拆开）。"""
    flat = tmp_path / "flat_one_patient"
    flat.mkdir()
    files = sorted((data / "input" / "WANG_WU_1").glob("*.dcm"))
    for i in range(24):
        shutil.copy2(files[i % len(files)], flat / f"{i:02d}.dcm")
    assert detect_input_mode(flat) == InputLayout(SINGLE, 0, 24, "root_dicoms")


# ---------------------------------------------------------------- 2. Job 自动单患者

def test_job_auto_single_patient_output_structure(wang_single):
    out = wang_single["out"]
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.dcm"))
    assert files == ["P0001/T1/S01/0001.dcm", "P0001/T1/S01/0002.dcm", "P0001/T1/S01/0003.dcm"]
    # 只有 P0001 一个患者编号，没有多出来的目录/文件
    assert [p.name for p in out.iterdir()] == ["P0001"]
    assert all(p.suffix == ".dcm" for p in out.rglob("*") if p.is_file())
    assert not list(out.rglob("*.part"))   # 没有写了一半的临时文件
    text = wang_single["logs"]
    assert "模式：单个患者" in text and "扫描（单个患者）" in text
    assert "散落" not in text          # 单患者模式不会把根目录 DICOM 当散落文件忽略
    assert wang_single["before"] == wang_single["after"] == snapshot(wang_single["input"])


def test_job_auto_single_patient_mapping_row(wang_single):
    rows = wang_single["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["编号"] == "P0001"
    assert row["原文件夹"] == "WANG_WU_1"                 # 映射表“原文件夹”= 选中的文件夹本身
    assert row["患者ID"] == "P5566778"
    assert row["DICOM文件数"] == "3"
    assert row["状态"] == "完成"
    assert row["时间点"] == "T1=2024-03-03"
    assert row["原始数据文件夹"] == str(wang_single["input"].resolve())
    assert row["批次"] == wang_single["job"].run_id


def test_job_auto_single_patient_output_has_no_identifier(wang_single):
    for name, data in wang_single["outputs"].items():
        for leak in LEAKS:
            assert leak not in data, (name, leak)
        ds = pydicom.dcmread(wang_single["out"] / name)
        assert ds.PatientName == "P0001" and ds.PatientID == "P0001"
        assert ds.PatientIdentityRemoved == "YES"
        assert ds.StudyDate == "" and ds.PatientBirthDate == "" and ds.AccessionNumber == ""
        assert not any(e.tag.is_private for e in ds.iterall())
        # 像素和几何信息不能被改动
        assert "PixelData" in ds and ds.SliceThickness and ds.PixelSpacing


def test_job_auto_single_preserves_pixels(wang_single):
    src_pixels, src_pos = set(), set()
    for f in (wang_single["input"]).glob("*.dcm"):
        ds = pydicom.dcmread(f)
        src_pixels.add(ds.PixelData)
        src_pos.add(tuple(ds.ImagePositionPatient))
    out_pixels, out_pos = set(), set()
    for p in wang_single["out"].rglob("*.dcm"):
        ds = pydicom.dcmread(p)
        out_pixels.add(ds.PixelData)
        out_pos.add(tuple(ds.ImagePositionPatient))
    assert out_pixels == src_pixels
    assert out_pos == src_pos
    # 每个输出文件都能通过独立的残留校验
    needles = pipeline.scan_patient(str(wang_single["input"])).needles
    assert needles
    for p in wang_single["out"].rglob("*.dcm"):
        assert pipeline.verify_file(p, "P0001", needles) == []


def test_job_auto_single_root_dicoms_and_subdir(data, tmp_path):
    """根目录既有 DICOM 又有子文件夹（LI_SIWEI = 平铺 + recon/）：单患者模式必须把两处都输出，不能丢数据。"""
    inp = data / "input" / "LI_SIWEI"
    out, mp = tmp_path / "out", tmp_path / "map.xlsx"
    before = snapshot(inp)
    ok, text = job_run(inp, out, mp)
    assert ok is True
    assert "模式：单个患者" in text and "散落" not in text
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.dcm"))
    assert files == [f"P0001/T1/S0{s}/{i:04d}.dcm"
                     for s, n in ((1, 4), (2, 3), (3, 1)) for i in range(1, n + 1)]
    row = read_rows(mp)[0]
    assert (row["编号"], row["原文件夹"], row["DICOM文件数"], row["状态"]) == \
           ("P0001", "LI_SIWEI", "8", "完成")
    assert row["时间点"] == "T1=2024-02-02"
    assert snapshot(inp) == before


# ---------------------------------------------------------------- 3. 手动指定 / 多患者拒绝

def test_job_force_batch_on_single_patient_folder_refused(data, tmp_path):
    """手动 --batch 作用在患者文件夹上：没有子文件夹 → 拒绝，不写任何文件。"""
    inp = data / "input" / "WANG_WU_1"
    out, mp = tmp_path / "out", tmp_path / "map.xlsx"
    ok, text = job_run(inp, out, mp, single_patient=False)
    assert ok is False
    assert "没有子文件夹" in text
    assert not out.exists()
    assert not mp.exists()


def test_job_force_single_on_nested_patient(zhang_single):
    out = zhang_single["out"]
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.dcm"))
    assert files == [f"P0001/T1/S01/{i:04d}.dcm" for i in range(1, 7)]   # 剂量报告截图/彩色渲染已排除
    row = zhang_single["rows"][0]
    assert (row["编号"], row["原文件夹"], row["患者ID"]) == ("P0001", "张三_20240101", "HZ00123456")
    assert row["DICOM文件数"] == "9" and row["跳过的非DICOM文件数"] == "1"
    assert row["状态"] == "完成" and row["时间点"] == "T1=2024-01-01"
    assert "已排除" in row["疑似烧录文字的序列"] and "Volume Rendering" in row["疑似烧录文字的序列"]
    for name, data_bytes in zhang_single["outputs"].items():
        for leak in LEAKS:
            assert leak not in data_bytes, (name, leak)
    assert zhang_single["before"] == zhang_single["after"] == snapshot(zhang_single["input"])


def test_job_force_single_matches_auto_batch_output(data, tmp_path):
    """同一个嵌套患者文件夹：自动判断（批量）和手动单患者，输出应逐字节一致（说明单患者模式没丢/多文件）。"""
    inp = data / "input" / "张三_20240101"
    mp = tmp_path / "map.xlsx"
    out_auto, out_single = tmp_path / "auto", tmp_path / "single"
    assert Job(inp, out_auto, mp, log=lambda m: None, workers=2).run() is True       # 自动 = 批量
    assert Job(inp, out_single, mp, log=lambda m: None, workers=2,
               single_patient=True).run() is True                                     # 手动单患者
    assert read_bytes(out_auto) == read_bytes(out_single)
    assert read_bytes(out_auto)


def test_job_forced_single_on_batch_folder_refused(data, tmp_path):
    """手动勾选“只处理一个患者”却选了批量根目录：扫描到多个患者 ID → 拒绝，不写任何文件、不加映射表行。"""
    inp = data / "input"
    out, mp = tmp_path / "out", tmp_path / "map.xlsx"
    before = snapshot(inp)
    ok, text = job_run(inp, out, mp, single_patient=True)
    assert ok is False
    assert "个不同的患者 ID" in text, text
    assert "000789" in text and "HZ00123456" in text and "P5566778" in text
    assert re.search(r"（\d+ 个文件）", text)
    assert "为避免把不同患者混进同一个编号，已停止，没有写出任何文件。" in text
    assert not out.exists()
    assert not mp.exists()
    assert snapshot(inp) == before


def test_job_auto_flat_multi_patient_refused(data, tmp_path):
    """平铺文件夹里混了两个患者：自动判断是批量/混合根目录（没有子文件夹）→ 拒绝，不写文件、不建映射表。"""
    flat = make_flat_mixed(tmp_path / "flat", data / "input")
    assert detect_input_mode(flat).mode == BATCH
    assert detect_input_mode(flat).reason == "mixed_root"
    out, mp = tmp_path / "out", tmp_path / "map.xlsx"
    ok, text = job_run(flat, out, mp)
    assert ok is False
    # mixed_root 专用提示（不再是“输入文件夹下没有子文件夹”那条泛化提示）
    assert "根目录下直接放着多个患者的 DICOM 文件" in text, text
    assert not out.exists()
    assert not mp.exists()


def test_single_guard_catches_detection_miss(data, tmp_path):
    """对抗用例：自动判断可能看走眼（嵌套子文件夹里混了别的患者）——兜底必须拒绝，不能混编号。"""
    flat = tmp_path / "single_but_contaminated"
    flat.mkdir()
    wang = sorted((data / "input" / "WANG_WU_1").glob("*.dcm"))
    li = sorted((data / "input" / "LI_SIWEI").glob("*.dcm"))[0]
    for i, f in enumerate(wang):
        shutil.copy2(f, flat / f"{i:02d}.dcm")
    child = flat / "recon"
    child.mkdir()
    shutil.copy2(wang[0], child / "0.dcm")       # 同一患者，排序在前 → 自动判断只看它
    shutil.copy2(li, child / "z_other.dcm")      # 另一患者，排序在后 → 自动判断看不到
    layout = detect_input_mode(flat)
    assert layout.mode == SINGLE and layout.reason == "root_dicoms"   # 自动判断确实可能看走眼
    out, mp = tmp_path / "out", tmp_path / "map.xlsx"
    ok, text = job_run(flat, out, mp)
    assert ok is False
    assert "个不同的患者 ID" in text
    assert "P5566778" in text and "000789" in text
    assert "为避免把不同患者混进同一个编号，已停止，没有写出任何文件。" in text
    assert not out.exists() and not mp.exists()


def test_single_patient_with_two_studies_is_allowed(data, tmp_path):
    """同一个患者有两次检查（不同 StudyInstanceUID / 日期）合成一个平铺文件夹：不能误拒，输出 T1/T2。"""
    flat = tmp_path / "one_patient_two_studies"
    flat.mkdir()
    for prefix, folder in (("a", "WANG_WU_1"), ("b", "WANG_WU_2")):
        for i, f in enumerate(sorted((data / "input" / folder).glob("*.dcm"))):
            shutil.copy2(f, flat / f"{prefix}{i}.dcm")
    assert detect_input_mode(flat) == InputLayout(SINGLE, 0, 5, "root_dicoms")
    out, mp = tmp_path / "out", tmp_path / "map.xlsx"
    ok, text = job_run(flat, out, mp)
    assert ok is True
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.dcm"))
    assert files == ([f"P0001/T1/S01/{i:04d}.dcm" for i in range(1, 4)]
                     + [f"P0001/T2/S01/{i:04d}.dcm" for i in range(1, 3)])
    row = read_rows(mp)[0]
    assert row["患者ID"] == "P5566778" and row["DICOM文件数"] == "5"
    assert row["时间点"] == "T1=2024-03-03; T2=2024-04-04"


# ---------------------------------------------------------------- 4. 单患者模式续跑

def test_job_single_resume_skips_finished_files(data, wang_single, tmp_path):
    """同一输出 + 同一映射再跑一次：全部“上次已完成”、编号不变、文件逐字节和修改时间都不变。"""
    inp = data / "input" / "WANG_WU_1"
    out, mp = copy_run(wang_single["out"], wang_single["map"], tmp_path)
    before = snapshot(out)
    mp_before_codes = [r["编号"] for r in read_rows(mp)]
    ok, text = job_run(inp, out, mp)
    assert ok is True
    assert "写出 0 个文件，3 个上次已完成" in text
    assert snapshot(out) == before
    assert [r["编号"] for r in read_rows(mp)] == mp_before_codes + ["P0001"]
    assert set(mp_before_codes) == {"P0001"}


# ---------------------------------------------------------------- 5. 与批量共享映射表

def test_single_after_batch_reuses_patient_code(data, batch_run, tmp_path):
    """先批量跑，再把 LI_SIWEI 复制到新目录单独跑：沿用现有编号、不重复写文件、不混患者。"""
    out, mp = copy_run(batch_run["out"], batch_run["map"], tmp_path)
    out_before = snapshot(out)
    new_patient = tmp_path / "new_patient_folder"
    shutil.copytree(data / "input" / "LI_SIWEI", new_patient)
    ok, text = job_run(new_patient, out, mp)
    assert ok is True
    assert "沿用已有编号" in text and "P0001" in text
    assert "写出 0 个文件" in text and "8 个上次已完成" in text
    assert snapshot(out) == out_before              # 一个字节都没覆盖
    rows = read_rows(mp)
    assert [(r["原文件夹"], r["编号"]) for r in rows] == [
        ("空文件夹_只有图片", None), ("LI_SIWEI", "P0001"), ("WANG_WU_1", "P0002"),
        ("WANG_WU_2", "P0002"), ("张三_20240101", "P0003"), ("new_patient_folder", "P0001")]
    assert {r["编号"] for r in rows if r["原文件夹"] == "new_patient_folder"} == {"P0001"}


def test_single_new_patient_gets_new_code(data, batch_run, tmp_path):
    """批量跑过之后单独跑一个真正的新患者（复制 LI_SIWEI 并改 PatientID）：分配新编号，旧编号不受影响。"""
    out, mp = copy_run(batch_run["out"], batch_run["map"], tmp_path)
    old_before = {d: read_bytes(out / d) for d in ("P0001", "P0002", "P0003")}
    new_patient = tmp_path / "new_patient_folder"
    shutil.copytree(data / "input" / "LI_SIWEI", new_patient)
    for p in sorted(new_patient.rglob("*")):
        if not p.is_file() or p.name == "Thumbs.db":
            continue
        try:
            ds = pydicom.dcmread(p, force=True)
        except Exception:
            continue
        if "SOPInstanceUID" not in ds:
            continue
        ds.PatientID = "NEWPAT001"
        ds.PatientName = "NEW^PAT"
        ds.save_as(p, enforce_file_format=True)
    ok, text = job_run(new_patient, out, mp)
    assert ok is True
    rows = read_rows(mp)
    new_row = next(r for r in rows if r["原文件夹"] == "new_patient_folder")
    assert new_row["编号"] == "P0004" and new_row["患者ID"] == "NEWPAT001"
    assert "P0004/T1/S01" in text or (out / "P0004").is_dir()
    files = sorted(p.relative_to(out / "P0004").as_posix() for p in (out / "P0004").rglob("*.dcm"))
    assert files == [f"T1/S0{s}/{i:04d}.dcm" for s, n in ((1, 4), (2, 3), (3, 1)) for i in range(1, n + 1)]
    # 新患者的数据只有一个检查，且旧患者编号的输出没被改动
    studies = {str(pydicom.dcmread(p, stop_before_pixels=True).StudyInstanceUID)
               for p in (out / "P0004").rglob("*.dcm")}
    assert len(studies) == 1
    for d, before in old_before.items():
        assert read_bytes(out / d) == before


def test_single_refuses_foreign_patient_in_output_code(data, tmp_path):
    """输出目录里已有同编号的其他患者数据：拒绝，绝不覆盖。"""
    inp = data / "input" / "WANG_WU_1"
    out, mp = tmp_path / "out", tmp_path / "map.xlsx"
    (out / "P0001" / "T1" / "S01").mkdir(parents=True)
    foreign = data / "input" / "LI_SIWEI" / "LISIWEI_1.dcm"
    shutil.copy2(foreign, out / "P0001" / "T1" / "S01" / "0001.dcm")
    before = snapshot(out)
    ok, text = job_run(inp, out, mp)
    assert ok is False
    assert "已有其他患者" in text
    assert snapshot(out) == before
    rows = read_rows(mp)
    assert rows and rows[0]["编号"] == "P0001" and rows[0]["状态"] == "失败"


def test_single_after_batch_same_output_refused(data, batch_run, tmp_path):
    """批量跑过的输出里同一个编号有多个时间点（T1/T2）：单独跑其中一个文件夹时拒绝，不混/不覆盖。"""
    out, mp = copy_run(batch_run["out"], batch_run["map"], tmp_path)
    before = snapshot(out)
    ok, text = job_run(data / "input" / "WANG_WU_1", out, mp)
    assert ok is False
    assert ("对不上" in text or "已有其他患者" in text), text
    assert snapshot(out) == before


# ---------------------------------------------------------------- 6. probe

def test_probe_single_patient_report(data, tmp_path):
    inp = data / "input" / "WANG_WU_1"
    before = snapshot(inp)
    text = probe(inp, log=lambda m: None, workers=2)
    assert "模式：单个患者" in text
    assert "文件夹001: DICOM 3 个, 其他 0 个, 序列 1 个" in text
    assert "子文件夹数" not in text
    for leak in ("WANG", "WU", "P5566778", "WANG_WU_1", "2024-03-03", "20240303", str(data)):
        assert leak not in text, leak
    assert snapshot(inp) == before


def test_probe_nested_patient_explicit_single(data):
    """手动指定单患者 + 多层目录：报告按一个患者统计，且不含姓名/ID/日期/文件夹名。"""
    inp = data / "input" / "张三_20240101"
    text = probe(inp, log=lambda m: None, workers=2, single_patient=True)
    assert "模式：单个患者" in text
    assert "文件夹001: DICOM 9 个, 其他 1 个, 序列 3 个" in text
    for leak in ("张三", "ZHANG", "HZ00123456", "_20240101", "2024-01-01", "20240101"):
        assert leak not in text, leak


def test_probe_to_file_single_patient_record(data, tmp_path):
    inp = data / "input" / "WANG_WU_1"
    report = tmp_path / "报告.txt"
    text = probe_to_file(inp, report, log=lambda m: None, workers=2)
    assert text and "模式：单个患者" in text
    assert report.read_text(encoding="utf-8-sig") == text
    rec = next(r for r in load_runs() if r.get("report") == str(report.resolve()))
    assert rec["kind"] == "结构探查" and rec["result"] == "完成"
    assert rec["single_patient"] is True
    with pytest.raises(FileExistsError):
        probe_to_file(inp, report, log=lambda m: None, workers=2)


def test_probe_batch_auto_keeps_batch_report(data):
    text = probe(data / "input", log=lambda m: None, workers=2)
    assert "模式：单个患者" not in text
    assert "子文件夹数：5" in text
    assert "根目录散落文件数：0" in text
    assert "文件夹004: DICOM 9 个, 其他 1 个, 序列 3 个" in text


# ---------------------------------------------------------------- 7. 处理记录 / 核对

def test_history_single_run_record_and_summarize(wang_single):
    rec = next(r for r in load_runs() if r["id"] == wang_single["job"].run_id)
    assert rec["kind"] == "脱敏" and rec["result"] == "全部完成"
    assert rec["single_patient"] is True
    assert (rec["folders"], rec["patients"], rec["skipped"]) == (1, 1, 0)
    assert (rec["files_total"], rec["written"], rec["existing"]) == (3, 3, 0)
    text = summarize(rec)
    assert "1 个文件夹 → 1 个患者编号（单患者模式）" in text
    assert "共 3 个文件：本次写出 3，之前已完成 0" in text


def test_history_batch_summarize_has_no_single_marker(batch_run):
    rec = next(r for r in load_runs() if r["id"] == batch_run["job"].run_id)
    assert rec["single_patient"] is False
    text = summarize(rec)
    assert "（单患者模式）" not in text
    assert "5 个文件夹 → 3 个患者编号" in text
    assert "1 个文件夹跳过（没有 DICOM）" in text


def test_history_folder_status_single_patient(wang_single, data, batch_run):
    items = folder_status(wang_single["input"], wang_single["map"], single_patient=True)
    assert [name for name, _ in items] == ["WANG_WU_1"]
    row = items[0][1]
    assert row is not None and row["编号"] == "P0001" and row["原文件夹"] == "WANG_WU_1"
    # 批量语义下这个文件夹没有子文件夹 → 核对列表为空（所以需要 single_patient=True）
    assert folder_status(wang_single["input"], wang_single["map"]) == []
    # 用批量根目录核对时，单患者模式跑出来的行不会冒充某个子文件夹
    assert all(r is None for _, r in folder_status(data / "input", wang_single["map"]))
    # 批量映射表本身仍能正常核对
    status = dict(folder_status(data / "input", batch_run["map"]))
    assert status["LI_SIWEI"]["编号"] == "P0001" and status["张三_20240101"]["编号"] == "P0003"


# ---------------------------------------------------------------- 8. 命令行

def cli(*args):
    """跑 app.py 命令行。Windows 上子进程输出走管道时默认用系统代码页，中文会编码失败，
    所以这里统一让子进程按 UTF-8 输出、父进程按 UTF-8 解码。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    return subprocess.run([sys.executable, str(ROOT / "app.py")] + [str(a) for a in args],
                          cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300, env=env)


def test_cli_auto_single_patient(data, tmp_path):
    inp = data / "input" / "WANG_WU_1"
    out, mp = tmp_path / "out", tmp_path / "map.xlsx"
    r = cli("--input", inp, "--output", out, "--mapping", mp, "--yes", "--workers", "2")
    assert r.returncode == 0, r.stderr[-2000:]
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.dcm"))
    assert files == ["P0001/T1/S01/0001.dcm", "P0001/T1/S01/0002.dcm", "P0001/T1/S01/0003.dcm"]
    assert [(x["编号"], x["原文件夹"]) for x in read_rows(mp)] == [("P0001", "WANG_WU_1")]
    assert "模式：单个患者" in r.stdout


def test_cli_batch_override_refused(data, tmp_path):
    inp = data / "input" / "WANG_WU_1"
    out, mp = tmp_path / "out", tmp_path / "map.xlsx"
    r = cli("--input", inp, "--output", out, "--mapping", mp, "--yes", "--batch", "--workers", "2")
    assert r.returncode == 1
    assert "没有子文件夹" in r.stdout
    assert not out.exists() and not mp.exists()


def test_cli_single_override(data, tmp_path):
    inp = data / "input" / "张三_20240101"
    out, mp = tmp_path / "out", tmp_path / "map.xlsx"
    r = cli("--input", inp, "--output", out, "--mapping", mp, "--yes", "--single-patient", "--workers", "2")
    assert r.returncode == 0, r.stderr[-2000:]
    assert len(list(out.rglob("*.dcm"))) == 6
    assert [(x["编号"], x["原文件夹"]) for x in read_rows(mp)] == [("P0001", "张三_20240101")]


def test_cli_probe_single_patient(data, tmp_path):
    inp = data / "input" / "WANG_WU_1"
    report = tmp_path / "报告.txt"
    r = cli("--probe", inp, "--report", report)
    assert r.returncode == 0, r.stderr[-2000:]
    text = report.read_text(encoding="utf-8-sig")
    assert "模式：单个患者" in text
    for leak in ("WANG", "P5566778", "WANG_WU_1"):
        assert leak not in text, leak


def test_cli_single_override_refuses_batch_folder(data, tmp_path):
    inp = data / "input"
    out, mp = tmp_path / "out", tmp_path / "map.xlsx"
    r = cli("--input", inp, "--output", out, "--mapping", mp, "--yes", "--single-patient", "--workers", "2")
    assert r.returncode == 1
    assert re.search(r"个不同的患者\s*ID", r.stdout), r.stdout[-2000:]
    assert not out.exists() and not mp.exists()


# ---------------------------------------------------------------- 9. 界面路径检查

def test_gui_check_paths_auto_detect(data, tmp_path):
    res, layout, single = check_paths(str(data / "input" / "WANG_WU_1"), "", "")
    assert single is True and layout == InputLayout(SINGLE, 0, 3, "root_dicoms")
    assert res[0][0] == "ok"
    assert "单个患者模式" in res[0][1] and "自动判断：文件夹里直接有 DICOM 文件" in res[0][1]

    res, layout, single = check_paths(str(data / "input" / "LI_SIWEI"), "", "")
    assert single is True and layout.reason == "root_dicoms" and res[0][0] == "ok"

    res, layout, single = check_paths(str(data / "input"), "", "")
    assert single is False and layout.mode == BATCH
    assert res[0][0] == "ok" and "找到 5 个子文件夹" in res[0][1]

    res, layout, single = check_paths(str(data / "input" / "张三_20240101"), "", "")
    assert single is False and layout.reason == "same_patient"
    assert res[0][0] == "warn" and "勾选" in res[0][1]

    res, layout, single = check_paths(str(data / "input" / "空文件夹_只有图片"), "", "")
    assert single is False and layout.mode == EMPTY
    assert res[0][0] == "warn" and "没有找到 DICOM" in res[0][1]

    res, layout, single = check_paths(str(tmp_path / "nope"), "", "")
    assert single is False and layout is None and res[0][0] == "error"


def test_gui_check_paths_batch_with_stray_dicoms(data, tmp_path):
    """批量文件夹根目录混了散落 DICOM：界面仍按批量，但提示这些文件会被忽略（可改单患者）。"""
    mixed = tmp_path / "input"
    shutil.copytree(data / "input", mixed)
    shutil.copy2(data / "input" / "WANG_WU_1" / "1.dcm", mixed / "stray.dcm")
    res, layout, single = check_paths(str(mixed), "", "")
    assert single is False and layout.mode == BATCH and layout.reason == "mixed_root"
    assert res[0][0] == "warn" and "批量模式下会被忽略" in res[0][1]


def test_gui_check_paths_mixed_root_error(data, tmp_path):
    """根目录混了多个患者的 DICOM：界面直接标红，不能显示成“单个患者模式”。"""
    flat = make_flat_mixed(tmp_path / "flat", data / "input")
    res, layout, single = check_paths(str(flat), "", "")
    assert single is False and layout.reason == "mixed_root"
    assert res[0][0] == "error" and "多个患者 ID" in res[0][1]
    # 手动勾选“只处理一个患者”时同样不能放行
    res, layout, single = check_paths(str(flat), str(tmp_path / "out"), str(tmp_path / "m.xlsx"),
                                      single_override=True)
    assert single is True and res[0][0] == "error" and "多个患者 ID" in res[0][1]


def test_gui_check_paths_manual_override(data):
    res, layout, single = check_paths(str(data / "input"), "", "", single_override=True)
    assert single is True and layout.mode == BATCH
    assert res[0][0] == "ok" and "手动指定" in res[0][1] and "拒绝" in res[0][1]

    res, layout, single = check_paths(str(data / "input" / "WANG_WU_1"), "", "", single_override=False)
    assert single is False and res[0][0] == "warn" and "里面没有子文件夹" in res[0][1]


# ---------------------------------------------------------------- 10. 空/无 DICOM/只剩截图等刁钻情况

@pytest.mark.parametrize("which", ["empty_dir", "only_images"])
def test_job_auto_empty_folder_fails_safely(data, tmp_path, which):
    inp = tmp_path / "empty" if which == "empty_dir" else data / "input" / "空文件夹_只有图片"
    if which == "empty_dir":
        inp.mkdir()
    out, mp = tmp_path / "out", tmp_path / "map.xlsx"
    ok, text = job_run(inp, out, mp)
    assert ok is False
    assert "没有子文件夹" in text
    assert not out.exists()
    assert not mp.exists()


def test_job_force_single_empty_folder_fails_safely(tmp_path):
    inp = tmp_path / "empty"
    inp.mkdir()
    out, mp = tmp_path / "out", tmp_path / "map.xlsx"
    ok, text = job_run(inp, out, mp, single_patient=True)
    assert ok is False
    assert "没有可处理的数据" in text
    assert not out.exists() and not mp.exists()


def test_job_force_single_only_burned_in_no_output(data, tmp_path):
    """单患者文件夹里全是截图/剂量报告类序列（会被排除）：正常结束、无输出、状态写清楚，不能报错。"""
    shots = tmp_path / "only_shots"
    shots.mkdir()
    src = data / "input" / "张三_20240101" / "DICOM" / "ST0"
    for sub in ("SE1", "SE2"):
        for f in (src / sub).iterdir():
            shutil.copy2(f, shots / f"{sub}_{f.name}")
    out, mp = tmp_path / "out", tmp_path / "map.xlsx"
    ok, text = job_run(shots, out, mp, single_patient=True)
    assert ok is True
    assert not list(out.rglob("*.dcm"))
    row = read_rows(mp)[0]
    assert row["编号"] == "P0001" and row["原文件夹"] == "only_shots"
    assert row["状态"].startswith("完成（全部是截图类图像")
    assert "已排除" in row["疑似烧录文字的序列"]


def test_mixed_root_batch_keeps_batch_behavior(data, batch_run, tmp_path):
    """批量文件夹里混了散落 DICOM：自动判断仍是批量；散落文件照旧忽略，输出和纯批量逐字节一致。"""
    mixed = tmp_path / "input"
    shutil.copytree(data / "input", mixed)
    shutil.copy2(data / "input" / "WANG_WU_1" / "1.dcm", mixed / "stray.dcm")
    before = snapshot(mixed)
    out, mp = tmp_path / "out", tmp_path / "map.xlsx"
    shutil.copy2(batch_run["map"], mp)          # 同一个密钥 → 同一套编号/UID，便于逐字节对比
    ok, text = job_run(mixed, out, mp)
    assert ok is True
    assert "散落文件" in text and "已忽略" in text
    assert read_bytes(out) == batch_run["outputs"]
    assert sorted(p.name for p in out.iterdir()) == ["P0001", "P0002", "P0003"]
    assert snapshot(mixed) == before


def test_batch_auto_equals_forced_batch(data, batch_run, tmp_path):
    """自动判断（None）和手动 --batch 在批量文件夹上行为完全一致（逐字节）。"""
    out, mp = tmp_path / "out", tmp_path / "map.xlsx"
    shutil.copy2(batch_run["map"], mp)
    ok, text = job_run(data / "input", out, mp, single_patient=False)
    assert ok is True
    assert "模式：单个患者" not in text
    assert read_bytes(out) == batch_run["outputs"]
    assert len(batch_run["outputs"]) == 19
    assert batch_run["before"] == batch_run["after"] == snapshot(data / "input")


def test_flat_single_patient_over_20_files_processes_all(data, tmp_path):
    """平铺单患者文件夹 24 个文件：自动单患者处理，不能把同一个患者拆成多个编号。"""
    flat = tmp_path / "flat_one_patient"
    flat.mkdir()
    files = sorted((data / "input" / "WANG_WU_1").glob("*.dcm"))
    for i in range(24):
        shutil.copy2(files[i % len(files)], flat / f"{i:02d}.dcm")
    out, mp = tmp_path / "out", tmp_path / "map.xlsx"
    ok, text = job_run(flat, out, mp)
    assert ok is True
    assert sorted(p.name for p in out.iterdir()) == ["P0001"]
    assert len(list(out.rglob("*.dcm"))) == 24
    assert {r["编号"] for r in read_rows(mp)} == {"P0001"}
