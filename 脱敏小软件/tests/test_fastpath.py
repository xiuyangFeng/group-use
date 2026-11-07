"""快速编码路径（deid/fastpath.py）的等价性与回退测试。

核心要求：快速路径的输出必须和原来的慢路径（pydicom Dataset 重建）逐字节一致；
拿不准的文件必须返回 None，让调用方自动走慢路径；快速路径真正出错时不能写出坏文件。
"""

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import make_fake_data  # noqa: E402

from deid import fastpath  # noqa: E402
from deid.anonymizer import UidMapper, anonymize  # noqa: E402
from deid.mapping import read_rows  # noqa: E402
from deid.pipeline import Job, encode_dicom, read_full, scan_patient  # noqa: E402


# ---------------------------------------------------------------- 工具

def slow_bytes(path, code, uid_map, needles):
    """原来的慢路径：完整解析成 Dataset → 脱敏 → 编码。"""
    ds = read_full(path)
    anonymize(ds, code, uid_map, needles)
    return encode_dicom(ds)


def all_files(folder):
    scan = scan_patient(folder)
    return scan.needles, [Path(f.path) for f in scan.files]


def write_implicit(src, dst):
    ds = read_full(src)
    ds.file_meta.TransferSyntaxUID = "1.2.840.10008.1.2"  # Implicit VR Little Endian
    ds.save_as(dst, implicit_vr=True, little_endian=True, enforce_file_format=True)


def write_big_endian(src, dst):
    from pydicom.filewriter import dcmwrite
    ds = read_full(src)
    ds.file_meta.TransferSyntaxUID = "1.2.840.10008.1.2.2"  # Explicit VR Big Endian
    dcmwrite(dst, ds, implicit_vr=False, little_endian=False, enforce_file_format=True)


# ---------------------------------------------------------------- 1. 假数据逐字节一致

@pytest.fixture(scope="module")
def data(tmp_path_factory):
    base = tmp_path_factory.mktemp("fastpath")
    make_fake_data.main(base / "input")
    return base


@pytest.mark.parametrize("folder", ["LI_SIWEI", "WANG_WU_1", "WANG_WU_2", "张三_20240101", "空文件夹_只有图片"])
def test_fastpath_byte_identical_on_fake_data(data, folder):
    inp = data / "input" / folder
    needles, files = all_files(inp)
    if not files:
        pytest.skip("这个文件夹里没有 DICOM")
    slow = {p: slow_bytes(p, "P0001", UidMapper("secret"), needles) for p in files}
    for p in files:
        out = fastpath.encode_file(p.read_bytes(), "P0001", UidMapper("secret"), needles)
        if out is None:      # 快速路径拿不准时允许回退慢路径（例如没有 DICM 文件头的老格式）
            continue
        data_out, cleaned = out
        assert data_out == slow[p], f"{p} 快速路径输出和慢路径不一致"
        assert cleaned is not None


def test_fastpath_falls_back_for_old_format_without_header(data):
    """没有 128 字节前导的老格式文件必须回退慢路径。"""
    p = data / "input" / "LI_SIWEI" / "old_format_noheader"
    needles, _ = all_files(data / "input" / "LI_SIWEI")
    assert fastpath.encode_file(p.read_bytes(), "P0001", UidMapper("secret"), needles) is None


# ---------------------------------------------------------------- 2. 特殊传输语法/异常输入

@pytest.mark.parametrize("writer", [write_implicit, write_big_endian])
def test_fastpath_transfer_syntax(data, tmp_path, writer):
    src = data / "input" / "LI_SIWEI" / "LISIWEI_1.dcm"
    dst = tmp_path / "converted.dcm"
    writer(src, dst)
    needles, _ = all_files(data / "input" / "LI_SIWEI")
    raw = dst.read_bytes()
    fast = fastpath.encode_file(raw, "P0001", UidMapper("secret"), needles)
    slow = slow_bytes(dst, "P0001", UidMapper("secret"), needles)
    if writer is write_implicit:
        # 隐式 VR 小端：快速路径应该支持，且逐字节一致
        assert fast is not None
        assert fast[0] == slow
    else:
        # 大端：快速路径不支持，必须回退，慢路径结果本身仍是合法 DICOM
        assert fast is None
        assert slow[:132][128:132] == b"DICM"


def test_fastpath_returns_none_for_garbage():
    for blob in (b"", b"not a dicom file " * 100, b"\x00" * 200):
        assert fastpath.encode_file(blob, "P0001", UidMapper("x"), set()) is None


def test_fastpath_returns_none_for_truncated_file(data):
    p = data / "input" / "LI_SIWEI" / "LISIWEI_1.dcm"
    raw = p.read_bytes()
    needles, _ = all_files(data / "input" / "LI_SIWEI")
    for cut in (len(raw) // 2, len(raw) - 100):
        assert fastpath.encode_file(raw[:cut], "P0001", UidMapper("secret"), needles) is None


# ---------------------------------------------------------------- 3. Job 端到端：开关快速路径输出一致

def _run_job(inp, out, mp, monkeypatch, fast):
    monkeypatch.setenv("DEID_FASTPATH", "1" if fast else "0")
    logs = []
    ok = Job(inp, out, mp, log=logs.append, workers=2).run()
    assert ok is True, "\n".join(logs)
    return {str(p.relative_to(out)): p.read_bytes() for p in sorted(Path(out).rglob("*.dcm"))}


def test_job_fastpath_output_identical_to_slowpath(data, tmp_path, monkeypatch):
    import shutil
    inp = data / "input"
    slow_out = tmp_path / "slow_out"
    slow_map = tmp_path / "slow_map.xlsx"
    slow_files = _run_job(inp, slow_out, slow_map, monkeypatch, fast=False)

    fast_out = tmp_path / "fast_out"
    fast_map = tmp_path / "fast_map.xlsx"
    shutil.copy2(slow_map, fast_map)      # 同一个映射表密钥 → 生成的新 UID 一致
    fast_files = _run_job(inp, fast_out, fast_map, monkeypatch, fast=True)

    assert slow_files.keys() == fast_files.keys()
    for name in slow_files:
        assert slow_files[name] == fast_files[name], f"{name} 快速路径与慢路径不一致"
    # 映射表语义列一致（时间戳、批次列不比）：慢路径那批 = 前 N 行，快速路径那批 = 新追加的行
    keys = ("编号", "原文件夹", "患者ID", "DICOM文件数", "时间点", "状态")
    a = [{k: r.get(k) for k in keys} for r in read_rows(slow_map)]
    all_rows = read_rows(fast_map)
    b = [{k: r.get(k) for k in keys} for r in all_rows[len(a):]]
    assert len(a) == len(b) and len(a) > 0
    assert a == b


def _process_inline(inp, out, monkeypatch, code="P0001"):
    """进程内跑 process_patient（不走进程池），这样 monkeypatch 才能生效。"""
    from deid import pipeline
    scan = scan_patient(inp)
    r = pipeline.process_patient(code, scan.files, scan.needles, str(out), "secret", exclude_burned_in=False)
    files = {str(p.relative_to(out)): p.read_bytes() for p in sorted(Path(out).rglob("*.dcm"))}
    return r, files


def test_slowpath_fallback_produces_identical_output(data, tmp_path, monkeypatch):
    """快速路径整体失效（返回 None）时必须自动退回慢路径，结果与快速路径逐字节一致。"""
    inp = data / "input" / "WANG_WU_1"
    normal_r, normal = _process_inline(inp, tmp_path / "a", monkeypatch)
    assert normal_r["errors"] == [] and normal_r["written"] > 0
    monkeypatch.setattr(fastpath, "_encode", lambda *a, **k: None)  # 模拟快速路径全部失败
    fallback_r, fallback = _process_inline(inp, tmp_path / "b", monkeypatch)
    assert fallback_r["errors"] == []
    assert normal == fallback


def test_fastpath_bad_bytes_are_caught_by_verify(data, tmp_path, monkeypatch):
    """快速路径返回错误字节时，verify_bytes 应该拦住、不写坏文件。"""
    from deid import pipeline
    monkeypatch.setattr(fastpath, "encode_file", lambda *a, **k: (b"this is not dicom", {}))
    inp = data / "input" / "WANG_WU_1"
    scan = scan_patient(inp)
    out = tmp_path / "out"
    r = pipeline.process_patient("P0001", scan.files, scan.needles, str(out), "secret",
                                 exclude_burned_in=False)
    assert r["verify"] and r["written"] == 0
    assert not list(out.rglob("*.dcm"))
