import hashlib
import re
import sys
from pathlib import Path

import pydicom
import pytest
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import make_fake_data  # noqa: E402
from deid import rules  # noqa: E402
from deid.mapping import MappingTable  # noqa: E402
from deid.pipeline import Job  # noqa: E402
from pydicom.datadict import tag_for_keyword  # noqa: E402

LEAKS = [b"ZHANG", b"SIWEI", b"WANG", b"HZ00123456", b"000789", b"A2024", b"1.2.840.113619",
         b"HOSPITAL", b"DR^", b"TECH^", b"SIEMENS", b"SN123456", "张三".encode("utf-8"),
         "张三".encode("gb18030"), "某某".encode("utf-8"), "某某".encode("gb18030")]


def snapshot(folder):
    """文件夹内每个文件的 路径 → (大小, 修改时间, SHA256)。"""
    return {str(p.relative_to(folder)): (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(Path(folder).rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    base = tmp_path_factory.mktemp("deid")
    make_fake_data.main(base / "input")
    before = snapshot(base / "input")
    mapping = base / "local" / "map.xlsx"
    ok = Job(base / "input", base / "output", mapping, log=lambda m: None).run()
    assert snapshot(base / "input") == before
    return base, mapping, ok, before


def test_original_data_never_modified(run, tmp_path):
    base, mapping, _, before = run
    # 再跑两次（一次输出到已存在的目录，一次输出到新目录），原始数据仍逐字节不变
    Job(base / "input", base / "output", mapping, log=lambda m: None).run()
    Job(base / "input", tmp_path / "o", tmp_path / "m.xlsx", log=lambda m: None).run()
    assert snapshot(base / "input") == before


def test_no_delete_calls_in_source():
    pattern = re.compile(r"\.unlink\(|rmtree|os\.remove\(|\.rmdir\(|os\.unlink|shutil\.move|send2trash")
    for py in list((ROOT / "deid").glob("*.py")) + [ROOT / "app.py", ROOT / "tools" / "make_fake_data.py"]:
        assert not pattern.search(py.read_text(encoding="utf-8")), py


def test_existing_output_files_not_overwritten(run):
    base, mapping, _, _ = run
    f = base / "output" / "P0001" / "T1" / "S01" / "0001.dcm"
    stat = (f.stat().st_mtime_ns, f.read_bytes())
    Job(base / "input", base / "output", mapping, log=lambda m: None).run()
    assert (f.stat().st_mtime_ns, f.read_bytes()) == stat


def test_mapping_backed_up_each_run(run):
    base, mapping, _, _ = run
    n = len(list((mapping.parent / "映射表备份").glob("*.xlsx")))
    Job(base / "input", base / "output", mapping, log=lambda m: None).run()
    assert len(list((mapping.parent / "映射表备份").glob("*.xlsx"))) == n + 1


def outputs(base):
    return sorted((base / "output").rglob("*.dcm"))


def test_rule_keywords_exist():
    for kw in rules.REMOVE | rules.EMPTY | rules.FILE_META_REMOVE | rules.KEEP_UID_KEYWORDS:
        assert tag_for_keyword(kw) is not None, kw


def test_structure(run):
    base, _, _, _ = run
    assert sorted(p.name for p in (base / "output").iterdir()) == ["P0001", "P0002", "P0003"]
    assert len(outputs(base)) == 19   # 张三的剂量报告截图(1)和彩色三维渲染(2)已排除
    # 输出里只有 .dcm，没有 DICOMDIR、txt、jpg
    assert all(p.suffix == ".dcm" for p in (base / "output").rglob("*") if p.is_file())


def test_no_identifier_bytes_anywhere(run):
    base, _, _, _ = run
    for p in outputs(base):
        data = p.read_bytes()
        for leak in LEAKS:
            assert leak not in data, (p, leak)


def test_tags(run):
    base, _, _, _ = run
    for p in outputs(base):
        ds = pydicom.dcmread(p)
        code = p.parents[2].name
        assert ds.PatientName == code and ds.PatientID == code
        assert ds.PatientIdentityRemoved == "YES"
        assert ds.PatientSex in ("M", "F")
        assert ds.PatientAge.endswith(("Y", "M"))
        assert ds.StudyDate == "" and ds.PatientBirthDate == "" and ds.AccessionNumber == ""
        assert not any(e.tag.is_private for e in ds.iterall())
        assert all(not str(e.value).startswith(make_fake_data.ROOT) for e in ds.iterall() if e.VR == "UI")
        assert ds.file_meta.MediaStorageSOPInstanceUID == ds.SOPInstanceUID
        for kw in ("InstitutionName", "OperatorsName", "StationName", "RequestAttributesSequence",
                   "OtherPatientIDs", "PatientAddress", "ImageComments"):
            assert kw not in ds, kw


def test_age_computed(run):
    base, _, _, _ = run
    ages = {}
    for p in outputs(base):
        ages.setdefault(p.parents[2].name, set()).add(pydicom.dcmread(p, stop_before_pixels=True).PatientAge)
    assert ages["P0003"] == {"067Y"}   # 1956-03-15 → 2024-01-01
    assert ages["P0001"] == {"043Y"}
    assert ages["P0002"] == {"004M", "005M"}  # 婴儿两次检查，按月计


def test_pixels_and_geometry_unchanged(run):
    base, _, _, _ = run
    src = {}
    for p in (base / "input").rglob("*"):
        if p.is_file() and p.name != "DICOMDIR":
            try:
                ds = pydicom.dcmread(p, force=True)
                if "PixelData" in ds:
                    src.setdefault((str(ds.PatientID), str(ds.SeriesDescription), int(ds.InstanceNumber)), ds)
            except Exception:
                pass
    checked = 0
    for p in outputs(base):
        out = pydicom.dcmread(p)
        cands = [d for (pid, desc, n), d in src.items() if desc == out.SeriesDescription and n == out.InstanceNumber]
        assert cands
        assert any(d.PixelData == out.PixelData for d in cands)
        assert any(list(d.ImagePositionPatient) == list(out.ImagePositionPatient) for d in cands)
        assert out.SliceThickness and out.PixelSpacing
        checked += 1
    assert checked == 19


def test_uid_consistency_within_study(run):
    base, _, _, _ = run
    studies = {}
    for p in (base / "output" / "P0001").rglob("*.dcm"):
        ds = pydicom.dcmread(p, stop_before_pixels=True)
        studies.setdefault(ds.StudyInstanceUID, set()).add(ds.FrameOfReferenceUID)
    assert len(studies) == 1  # 三个序列仍属同一个检查
    assert len(next(iter(studies.values()))) == 1


def test_mapping_table(run):
    _, mapping, _, _ = run
    m = MappingTable(mapping)
    rows = list(m.rows())
    assert {r["原文件夹"]: r["编号"] for r in rows} == {
        "LI_SIWEI": "P0001", "WANG_WU_1": "P0002", "WANG_WU_2": "P0002", "张三_20240101": "P0003"}
    zhang = next(r for r in rows if r["编号"] == "P0003")
    assert "张三" in zhang["患者姓名"] and zhang["患者ID"] == "HZ00123456"
    assert "已排除" in zhang["疑似烧录文字的序列"] and "Volume Rendering" in zhang["疑似烧录文字的序列"]
    assert zhang["DICOM文件数"] == "9" and zhang["序列数"] == "1"
    assert zhang["时间点"] == "T1=2024-01-01"
    wang = [r for r in rows if r["编号"] == "P0002"]
    assert all(r["时间点"] == "T1=2024-03-03; T2=2024-04-04" for r in wang)
    li = next(r for r in rows if r["编号"] == "P0001")
    assert li["患者ID"] == "000789"  # 前导零没有丢
    assert load_workbook(mapping)["_key"].sheet_state == "hidden"


def test_rerun_reuses_codes_and_uids(run, tmp_path):
    base, mapping, _, _ = run
    import shutil
    m2 = tmp_path / "map.xlsx"
    shutil.copy(mapping, m2)
    Job(base / "input", tmp_path / "out2", m2, log=lambda m: None).run()
    assert sorted(p.name for p in (tmp_path / "out2").iterdir()) == ["P0001", "P0002", "P0003"]
    a = pydicom.dcmread(base / "output/P0003/T1/S01/0001.dcm", stop_before_pixels=True)
    b = pydicom.dcmread(tmp_path / "out2/P0003/T1/S01/0001.dcm", stop_before_pixels=True)
    assert a.SOPInstanceUID == b.SOPInstanceUID


def test_rerun_same_output_skips_finished_files(run):
    base, mapping, _, _ = run
    logs = []
    Job(base / "input", base / "output", mapping, log=logs.append).run()
    text = "\n".join(logs)
    assert "写出 0 个文件，8 个上次已完成" in text   # P0001 全部跳过，不重写


def _scan_one(base, folder):
    from deid.pipeline import scan_patient
    return scan_patient(str(base / "input" / folder))


def test_disk_full_then_resume(run, tmp_path, monkeypatch):
    import builtins
    import errno
    from deid import pipeline
    base, _, _, _ = run
    s = _scan_one(base, "LI_SIWEI")
    writes = {"n": 0}

    def fake_open(path, mode="r", *a, **kw):
        if "w" in mode and str(path).endswith(".part"):
            writes["n"] += 1
            if writes["n"] > 3:
                f = builtins.open(path, mode, *a, **kw)
                f.write(b"half")
                f.close()
                raise OSError(errno.ENOSPC, "No space left on device")
        return builtins.open(path, mode, *a, **kw)

    monkeypatch.setattr(pipeline, "open", fake_open, raising=False)
    r = pipeline.process_patient("P0009", s.files, s.needles, str(tmp_path), "secret")
    assert r["disk_full"] and r["written"] == 3
    assert len(r["errors"]) == 1 and "空间已满" in r["errors"][0]
    assert len(list(tmp_path.rglob("*.part"))) == 1

    monkeypatch.undo()
    r2 = pipeline.process_patient("P0009", s.files, s.needles, str(tmp_path), "secret")
    assert not r2["disk_full"] and r2["existing"] == 3 and r2["written"] == len(s.files) - 3
    assert not list(tmp_path.rglob("*.part"))            # 上次的半截临时文件已被补写完整
    for f in tmp_path.rglob("*.dcm"):
        assert pipeline.verify_file(f, "P0009", s.needles) == []


def test_refuses_foreign_patient_in_output_dir(run, tmp_path):
    from deid import pipeline
    base, _, _, _ = run
    a, b = _scan_one(base, "LI_SIWEI"), _scan_one(base, "WANG_WU_1")
    pipeline.process_patient("P0001", a.files, a.needles, str(tmp_path), "secret")
    n_before = len(list(tmp_path.rglob("*.dcm")))
    r = pipeline.process_patient("P0001", b.files, b.needles, str(tmp_path), "secret")
    assert r["written"] == 0 and "其他患者" in r["errors"][0]
    assert len(list(tmp_path.rglob("*.dcm"))) == n_before


def test_cleaned_fields_are_named(run):
    from deid import pipeline
    base, _, _, _ = run
    s = _scan_one(base, "LI_SIWEI")
    ds = pipeline.read_full(s.files[0].path)
    ds.StudyDescription = "CT for li siwei"
    from deid.anonymizer import UidMapper, anonymize
    cleaned = anonymize(ds, "P0001", UidMapper("x"), s.needles)
    assert dict(cleaned) == {"StudyDescription": 1}   # PatientID 本身不计入
    assert ds.StudyDescription == "" and ds.PatientID == "P0001"


@pytest.mark.parametrize("out,mp,msg", [
    ("input/sub", "local/m.xlsx", "互相包含"),
    ("output", "output/m.xlsx", "映射表不能放在输出"),
    ("output", "input/m.xlsx", "原始数据文件夹"),
])
def test_path_checks(run, out, mp, msg):
    base, _, _, _ = run
    err = Job(base / "input", base / out, base / mp, log=lambda m: None).check_paths()
    assert err and msg in err


def test_probe_fast_path_matches_and_leaks_nothing(run):
    from deid.probe import probe
    base, _, _, before = run
    text = probe(base / "input", log=lambda m: None)
    assert "文件夹001: DICOM 8 个, 其他 0 个, 序列 3 个" in text      # 含无前导老格式文件
    assert "文件夹004: DICOM 9 个, 其他 1 个, 序列 3 个" in text      # DICOMDIR 和 txt 不算 DICOM
    leak_section = text.split("== 原始数据中含患者姓名")[1].split("==")[1]
    assert "ImageComments" in leak_section and "私有(0029,1110)" in leak_section
    residual = text.split("== 模拟脱敏后仍残留的字段（应为空） ==")[1].split("==")[0]
    assert "（无）" in residual
    for leak in ["ZHANG", "SIWEI", "WANG", "HZ00123456", "000789", "张三", "某某", "A2024"]:
        assert leak not in text, leak
    assert snapshot(base / "input") == before   # 探查不改原始数据


def test_looks_like_dicom(tmp_path):
    from deid.pipeline import looks_like_dicom
    (tmp_path / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\0" * 200)
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4" + b"\0" * 200)
    (tmp_path / "c.dcm").write_bytes(b"\0" * 128 + b"DICM" + b"\0" * 10)
    (tmp_path / "d").write_bytes(b"\x08\x00\x05\x00" + b"\0" * 10)
    (tmp_path / "e").write_bytes(b"")
    assert [looks_like_dicom(tmp_path / n) for n in "a.jpg b.pdf c.dcm d e".split()] == [False, False, True, True, False]


# ---------------------------------------------------------------- 暂停 / 继续 / 停止

def test_job_pause_and_resume_across_processes(tmp_path):
    import threading
    import time
    from deid.pipeline import Control
    make_fake_data.main(tmp_path / "input")
    ctl = Control()

    def confirm(_text):
        ctl.pause()          # 一确认就暂停
        return True

    result = {}
    t = threading.Thread(target=lambda: result.setdefault(
        "ok", Job(tmp_path / "input", tmp_path / "out", tmp_path / "m.xlsx", log=lambda m: None,
                  confirm=confirm, control=ctl, workers=2).run()))
    t.start()
    time.sleep(2.5)
    assert t.is_alive()
    assert not list((tmp_path / "out").rglob("*.dcm"))   # 暂停期间一个文件都不写
    ctl.resume()
    t.join(60)
    assert not t.is_alive()
    assert len(list((tmp_path / "out").rglob("*.dcm"))) == 19


def test_job_stop_then_continue(tmp_path):
    from deid.pipeline import Control
    make_fake_data.main(tmp_path / "input")
    before = snapshot(tmp_path / "input")
    ctl = Control()
    logs = []

    def confirm(_text):
        ctl.stop()
        return True

    ok = Job(tmp_path / "input", tmp_path / "out", tmp_path / "m.xlsx", log=logs.append,
             confirm=confirm, control=ctl, workers=2).run()
    assert ok is False and any("已按要求停止" in line for line in logs)
    assert not list((tmp_path / "out").rglob("*.dcm"))
    assert not list((tmp_path / "out").rglob("*.part"))
    # 再跑一次（新的 Control），编号不变、全部完成
    logs2 = []
    Job(tmp_path / "input", tmp_path / "out", tmp_path / "m.xlsx", log=logs2.append, workers=2).run()
    assert len(list((tmp_path / "out").rglob("*.dcm"))) == 19
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["P0001", "P0002", "P0003"]
    assert snapshot(tmp_path / "input") == before


def test_stop_mid_patient_leaves_only_complete_files(run, tmp_path, monkeypatch):
    import builtins
    from deid import pipeline
    base, _, _, _ = run
    s = _scan_one(base, "LI_SIWEI")
    ctl = pipeline.Control()
    pipeline._init_worker(None, ctl.run_event, ctl.stop_event)
    writes = {"n": 0}

    def fake_open(path, mode="r", *a, **kw):
        if "w" in mode and str(path).endswith(".part"):
            writes["n"] += 1
            if writes["n"] == 3:
                ctl.stop()   # 写第 3 个文件时用户点了停止
        return builtins.open(path, mode, *a, **kw)

    monkeypatch.setattr(pipeline, "open", fake_open, raising=False)
    try:
        r = pipeline.process_patient("P0001", s.files, s.needles, str(tmp_path), "secret")
    finally:
        pipeline._init_worker()
    assert r["stopped"] and r["written"] == 3 and not r["errors"]
    assert not list(tmp_path.rglob("*.part"))   # 当前文件写完才停，没有半截文件


def test_probe_stop_returns_none(run):
    from deid.pipeline import Control
    from deid.probe import probe
    base, _, _, _ = run
    ctl = Control()
    ctl.stop()
    assert probe(base / "input", log=lambda m: None, control=ctl) is None


# ---------------------------------------------------------------- 时间点 T1/T2

def test_timepoint_layout(run):
    base, _, _, _ = run
    out = base / "output"
    # 每个患者都有 T1；同一患者两次检查（WANG_WU 两个文件夹）分成 T1、T2，按日期先后
    for code in ("P0001", "P0002", "P0003"):
        assert (out / code / "T1").is_dir()
    assert sorted(p.name for p in (out / "P0002").iterdir()) == ["T1", "T2"]
    t1 = {pydicom.dcmread(p, stop_before_pixels=True).SeriesDescription for p in (out / "P0002" / "T1").rglob("*.dcm")}
    t2 = {pydicom.dcmread(p, stop_before_pixels=True).SeriesDescription for p in (out / "P0002" / "T2").rglob("*.dcm")}
    assert t1 == {"Head"} and t2 == {"Head follow-up"}
    assert len(list((out / "P0002" / "T1").rglob("*.dcm"))) == 3
    assert len(list((out / "P0002" / "T2").rglob("*.dcm"))) == 2
    # 同一天的不同序列在同一个时间点下
    assert sorted(p.name for p in (out / "P0001" / "T1").iterdir()) == ["S01", "S02", "S03"]
    # 输出里没有日期
    for p in (out / "P0002").rglob("*.dcm"):
        ds = pydicom.dcmread(p, stop_before_pixels=True)
        assert ds.StudyDate == "" and ds.SeriesDate == ""


def test_plan_output_same_day_and_missing_date():
    from deid.pipeline import FileRec, plan_output
    mk = lambda study, series, key, n: FileRec(path=f"/{study}/{series}/{n}", study_uid=study, series_uid=series,
                                               study_key=key, series_number=1, instance_number=n,
                                               series_desc="", burned_in="")
    files = [mk("A", "a1", "20240505080000", 1), mk("B", "b1", "20240505093000", 1),   # 同一天两个检查
             mk("C", "c1", "20230101080000", 1),                                       # 更早
             mk("D", "d1", "", 1)]                                                     # 无日期
    plan, tps = plan_output(files)
    assert tps == [("T1", "2023-01-01"), ("T2", "2024-05-05"), ("T3", "日期未知")]
    assert [d for d, _ in plan] == ["T1/S01", "T2/S01", "T2/S02", "T3/S01"]


def test_refuses_old_layout_output_dir(run, tmp_path):
    from deid import pipeline
    base, _, _, _ = run
    s = _scan_one(base, "LI_SIWEI")
    (tmp_path / "P0001" / "S01").mkdir(parents=True)          # 旧版本留下的结构
    r = pipeline.process_patient("P0001", s.files, s.needles, str(tmp_path), "secret")
    assert r["written"] == 0 and "旧版本的目录结构" in r["errors"][0]
    assert not (tmp_path / "P0001" / "T1").exists()


def test_old_mapping_header_upgraded(tmp_path):
    from openpyxl import Workbook
    from deid.mapping import COLUMNS, OLD_COLUMNS
    wb = Workbook()
    ws = wb.active
    ws.title = "映射表"
    ws.append(OLD_COLUMNS)
    ws.append(["P0007", "old_folder", "X", "PID7"] + [""] * (len(OLD_COLUMNS) - 4))
    wb.save(tmp_path / "old.xlsx")
    m = MappingTable(tmp_path / "old.xlsx")
    assert [c.value for c in m.ws[1]] == COLUMNS
    assert m.code_by_patient_id() == {"PID7": "P0007"} and m.max_number() == 7


def test_v6_mapping_header_upgraded(tmp_path):
    from openpyxl import Workbook
    from deid.mapping import COLUMNS, read_rows
    wb = Workbook()
    ws = wb.active
    ws.title = "映射表"
    ws.append(COLUMNS[:17])                       # v6：到“时间点”为止
    ws.append(["P0003", "f"] + [""] * 15)
    wb.save(tmp_path / "v6.xlsx")
    m = MappingTable(tmp_path / "v6.xlsx")
    m.append({"编号": "P0004", "原文件夹": "g", "批次": "B1"})
    rows = read_rows(tmp_path / "v6.xlsx")
    assert [r["编号"] for r in rows] == ["P0003", "P0004"] and rows[1]["批次"] == "B1"


# ---------------------------------------------------------------- 处理记录

def _run_record(run_id):
    from deid.history import load_runs
    return next(r for r in load_runs() if r["id"] == run_id)


def test_history_records_job_and_batch(tmp_path):
    from deid.history import folder_status, history_path
    from deid.mapping import read_rows
    make_fake_data.main(tmp_path / "input")
    job = Job(tmp_path / "input", tmp_path / "out", tmp_path / "m.xlsx", log=lambda m: None, workers=2)
    job.run()
    r = _run_record(job.run_id)
    assert r["kind"] == "脱敏" and r["end"] and r["result"] == "全部完成（1 个文件夹没有 DICOM，已跳过）"
    assert r["input"] == str((tmp_path / "input").resolve()) and r["mapping"] == str((tmp_path / "m.xlsx").resolve())
    assert (r["folders"], r["patients"], r["skipped"], r["files_total"], r["written"]) == (5, 3, 1, 19, 19)
    assert r["status_counts"] == {"完成": 3} and r["not_started"] == 0
    assert Path(r["log"]).exists()

    # 映射表里每一行都带批次，跳过的文件夹也有一行
    rows = [x for x in read_rows(tmp_path / "m.xlsx") if x["批次"] == job.run_id]
    assert sorted(x["原文件夹"] for x in rows) == sorted(p.name for p in (tmp_path / "input").iterdir() if p.is_dir())
    assert all(x["原始数据文件夹"] == r["input"] for x in rows)

    # 处理记录里没有任何患者信息
    text = history_path().read_text(encoding="utf-8")
    for s in ("张三", "ZHANG", "LI_SIWEI", "WANG", "HZ00123456", "P5566778", "空文件夹"):
        assert s not in text

    # 核对原始数据文件夹：新加一个文件夹 → 从未处理
    (tmp_path / "input" / "新患者").mkdir()
    st = dict(folder_status(tmp_path / "input", tmp_path / "m.xlsx"))
    assert st["新患者"] is None
    assert st["LI_SIWEI"]["状态"] == "完成" and st["空文件夹_只有图片"]["状态"].startswith("跳过")


def test_history_records_stop_and_unfinished(tmp_path):
    from deid.history import load_runs, new_run_id, record
    from deid.pipeline import Control
    make_fake_data.main(tmp_path / "input")
    ctl = Control()

    def confirm(_text):
        ctl.stop()
        return True

    job = Job(tmp_path / "input", tmp_path / "out", tmp_path / "m.xlsx", log=lambda m: None,
              confirm=confirm, control=ctl, workers=2)
    job.run()
    r = _run_record(job.run_id)
    assert r["result"] == "未完成（已停止）"
    assert r["status_counts"].get("未完成（已停止）", 0) + r["not_started"] == 3 and r["written"] == 0

    # 程序中途被强制关掉：只有开始记录 → 显示“未正常结束”；断电留下的半行被忽略
    rid = new_run_id()
    record(rid, "start", kind="脱敏", input="X")
    from deid.history import history_path
    with open(history_path(), "a", encoding="utf-8") as f:
        f.write('{"id": "broken", "eve')
    runs = load_runs()
    assert _run_record(rid)["result"].startswith("未正常结束")
    assert runs[0]["id"] == rid   # 最新的在前


def test_user_cancel_not_recorded(tmp_path):
    from deid.history import load_runs
    make_fake_data.main(tmp_path / "input")
    n = len(load_runs())
    Job(tmp_path / "input", tmp_path / "out", tmp_path / "m.xlsx", log=lambda m: None, confirm=lambda t: False).run()
    assert len(load_runs()) == n


def test_probe_recorded_and_refuses_existing_report(run, tmp_path):
    from deid.history import load_runs
    from deid.probe import probe_to_file
    base, _, _, _ = run
    rep = tmp_path / "r.txt"
    assert probe_to_file(base / "input", rep, log=lambda m: None)
    r = load_runs()[0]
    assert r["kind"] == "结构探查" and r["result"] == "完成" and r["report"] == str(rep.resolve())
    with pytest.raises(FileExistsError):
        probe_to_file(base / "input", rep, log=lambda m: None)


def test_skipped_folder_still_counts_as_all_done(run):
    """只有“没有 DICOM 的文件夹被跳过”时，返回值、日志和处理记录都应是“全部完成”，三者一致。"""
    from deid.history import load_runs
    base, _, ok, _ = run
    assert ok is True
    r = next(x for x in load_runs() if x.get("kind") == "脱敏" and x.get("output") == str(base / "output"))
    assert r["result"].startswith("全部完成") and r["skipped"] == 1
    log = next((base / "local").glob("脱敏日志_*.txt")).read_text(encoding="utf-8")
    assert "全部完成，校验通过。（1 个文件夹里没有 DICOM" in log


def test_probe_refuses_full_disk_before_starting(run, tmp_path, monkeypatch):
    import shutil
    from deid import probe as probe_mod
    base, _, _, _ = run
    calls = []
    monkeypatch.setattr(probe_mod, "probe", lambda *a, **k: calls.append(1) or "x")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100, 100, 1024))
    with pytest.raises(OSError) as ei:
        probe_mod.probe_to_file(base / "input", tmp_path / "r.txt", log=lambda m: None)
    assert ei.value.errno == 28 and "别的盘" in str(ei.value)
    assert not calls and not (tmp_path / "r.txt").exists()  # 没开始探查，也没留下文件


def test_probe_keeps_report_when_save_fails(run, tmp_path, monkeypatch):
    from deid import probe as probe_mod
    from deid.history import load_runs
    base, _, _, _ = run

    def full(text, report):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(probe_mod, "save_report", full)
    with pytest.raises(probe_mod.ReportSaveError) as ei:
        probe_mod.probe_to_file(base / "input", tmp_path / "r.txt", log=lambda m: None)
    assert "结构探查报告" in ei.value.text and "磁盘空间不足" in str(ei.value)
    assert "保存失败" in load_runs()[0]["result"]
    monkeypatch.undo()
    other = tmp_path / "elsewhere.txt"
    probe_mod.save_report(ei.value.text, other)  # 换个位置能存下，内容完整
    assert other.read_text(encoding="utf-8-sig") == ei.value.text
    with pytest.raises(FileExistsError):
        probe_mod.save_report("x", other)  # 不覆盖



# ---------------------------------------------------------------- 并行切块

def test_chunks_split_by_series_and_size():
    from deid.pipeline import chunks
    tasks = [("a", "P1/T1/S01/0001.dcm"), ("b", "P1/T1/S01/0002.dcm"), ("c", "P1/T1/S01/0003.dcm"),
             ("d", "P1/T1/S02/0001.dcm")]
    assert [[t[0] for t in c] for c in chunks(tasks, 2)] == [["a", "b"], ["c"], ["d"]]


def test_tiny_chunks_give_identical_output(run, tmp_path, monkeypatch):
    """把扫描和脱敏都切成极小的块（多进程交错处理），结果必须和正常运行逐字节一致。"""
    from deid import pipeline
    base, mapping, _, _ = run
    monkeypatch.setattr(pipeline, "CHUNK_FILES", 1)
    monkeypatch.setattr(pipeline, "SCAN_CHUNK", 2)
    Job(base / "input", tmp_path / "o", mapping, log=lambda m: None, workers=3).run()
    ref = {p.relative_to(base / "output"): p.read_bytes() for p in outputs(base)}
    got = {p.relative_to(tmp_path / "o"): p.read_bytes() for p in sorted((tmp_path / "o").rglob("*.dcm"))}
    assert got == ref


def test_stop_with_chunks_then_resume(tmp_path, monkeypatch):
    import threading
    from deid import pipeline
    from deid.pipeline import Control
    monkeypatch.setattr(pipeline, "CHUNK_FILES", 1)
    make_fake_data.main(tmp_path / "input")
    ctl = Control()

    def confirm(_text):
        ctl.pause()
        threading.Timer(0.3, ctl.resume).start()  # 先暂停再继续
        return True

    def log(line):
        if line.startswith("[1/"):  # 第一个患者处理完就停止（不靠计时，机器再快也一定停在中途）
            ctl.stop()

    job = Job(tmp_path / "input", tmp_path / "out", tmp_path / "m.xlsx", log=log,
              confirm=confirm, control=ctl, workers=2)
    assert job.run() is False
    assert not list((tmp_path / "out").rglob("*.part"))
    st = job.stats
    assert sum(st.get("status_counts", {}).values()) + st["not_started"] == 3
    # 续跑：全部完成，编号不变
    job2 = Job(tmp_path / "input", tmp_path / "out", tmp_path / "m.xlsx", log=lambda m: None, workers=2)
    job2.run()
    assert len(list((tmp_path / "out").rglob("*.dcm"))) == 19
    assert job2.stats["written"] + job2.stats["existing"] == 19


# ---------------------------------------------------------------- 排除疑似烧录文字的序列

def test_burned_in_check_detects_color_without_markers():
    from pydicom.dataset import Dataset
    from deid.pipeline import burned_in_check
    ds = Dataset()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"   # 普通 CT 图像类型
    ds.Modality = "CT"
    ds.PhotometricInterpretation = "RGB"
    ds.SeriesDescription = "Processed Images"
    assert burned_in_check(ds) == ("彩色图像(RGB)", True)
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.ImageType = ["DERIVED", "SECONDARY", "VOLREND"]
    assert burned_in_check(ds)[1] is True
    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
    ds.SeriesDescription = "Dose Report"
    assert burned_in_check(ds) == ("描述含“DOSE”", False)
    ds.SeriesDescription = "CTA 0.625mm"
    assert burned_in_check(ds) == ("", False)


def test_split_burned_in_protects_main_series():
    from deid.pipeline import FileRec, split_burned_in

    def series(uid, n, reason="", strong=False, no=1):
        return [FileRec(path=f"/{uid}/{i}", study_uid="S", series_uid=uid, study_key="20240101080000",
                        series_number=no, instance_number=i, series_desc=uid, burned_in=reason,
                        burned_strong=strong) for i in range(n)]

    main = series("main", 300, "描述含“REPORT”", no=2)          # 弱信号但张数最多：保留
    dose = series("dose", 1, "描述含“DOSE”", no=9)               # 弱信号、不是主序列：排除
    vr = series("vr", 8, "彩色图像(RGB)", True, no=3)            # 强信号：排除
    kept, excluded = split_burned_in(main + dose + vr)
    assert {f.series_uid for f in kept} == {"main"}
    assert len(excluded) == 2 and any("vr" in e and "8 个文件" in e for e in excluded)
    # 强信号即使是最大的序列也排除
    kept, excluded = split_burned_in(series("big_rgb", 500, "彩色图像(RGB)", True) + series("ct", 10))
    assert {f.series_uid for f in kept} == {"ct"}


def test_keep_burned_in_option_and_resume_refuses_mismatch(tmp_path):
    make_fake_data.main(tmp_path / "input")
    logs = []
    Job(tmp_path / "input", tmp_path / "out", tmp_path / "m.xlsx", log=logs.append,
        exclude_burned_in=False).run()
    assert len(list((tmp_path / "out").rglob("*.dcm"))) == 22                 # 不排除：截图也输出
    assert any("已保留" in line or "请人工检查" in line for line in logs)
    before = sorted((tmp_path / "out").rglob("*"))
    # 再用默认设置（排除）续跑同一个输出文件夹：P0003 里有截图序列、编号也对不上 → 拒绝，不写任何文件
    logs2 = []
    Job(tmp_path / "input", tmp_path / "out", tmp_path / "m.xlsx", log=logs2.append).run()
    assert sorted((tmp_path / "out").rglob("*")) == before
    assert any("P0003" in line and "失败" in line for line in logs2)
    assert any("对不上" in line for line in logs2)


def test_patient_with_only_screenshots(tmp_path):
    from deid.mapping import read_rows
    make_fake_data.main(tmp_path / "all")
    inp = tmp_path / "input"
    (inp / "only_shots").mkdir(parents=True)
    src = tmp_path / "all" / "张三_20240101" / "DICOM" / "ST0"
    for sub in ("SE1", "SE2"):  # 只拷剂量报告截图和三维渲染
        for f in (src / sub).iterdir():
            (inp / "only_shots" / f"{sub}_{f.name}").write_bytes(f.read_bytes())
    job = Job(inp, tmp_path / "out", tmp_path / "m.xlsx", log=lambda m: None)
    assert job.run() is True
    row = [r for r in read_rows(tmp_path / "m.xlsx") if r["原文件夹"] == "only_shots"][0]
    assert row["状态"].startswith("完成（全部是截图类图像") and "已排除" in row["疑似烧录文字的序列"]
    assert not list((tmp_path / "out").rglob("*.dcm"))
