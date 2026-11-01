"""生成用于测试的假患者 CT 数据（不含任何真实信息）。

用法: python tools/make_fake_data.py <输出目录>

故意覆盖各种难点：中文姓名(GB18030)、拼音姓名、私有字段里的姓名、嵌套序列里的姓名、
自由文本里的姓名、医院/医生信息、多层目录、DICOMDIR、非 DICOM 文件、
没有 128 字节前导的老格式文件、剂量报告截图序列、同一患者分在两个文件夹。
"""

import copy
import sys
from pathlib import Path

import pydicom.examples
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian

ROOT = "1.2.840.113619.2.55.3.604688119"  # 仿 GE 医院 UID 前缀
_counter = [0]


def uid():
    _counter[0] += 1
    return f"{ROOT}.{_counter[0]}"


def make_slice(patient, study_uid, series_uid, series_no, inst_no, desc, sop_class=None, burned=False, color=False):
    ds = copy.deepcopy(pydicom.examples.ct)
    ds.SpecificCharacterSet = patient.get("charset", "ISO_IR 100")
    ds.PatientName = patient["name"]
    ds.PatientID = patient["id"]
    ds.PatientBirthDate = patient["birth"]
    ds.PatientSex = patient["sex"]
    if "PatientAge" in ds:
        del ds.PatientAge
    ds.OtherPatientIDs = patient["id"] + "X"
    ds.PatientAddress = "某市某区某路 1 号"
    ds.PatientTelephoneNumbers = "13800000000"
    ds.InstitutionName = "某某市第一人民医院"
    ds.InstitutionAddress = "某市某路 100 号"
    ds.ReferringPhysicianName = "DR^WANG"
    ds.OperatorsName = "TECH^LIU"
    ds.PerformingPhysicianName = "DR^CHEN"
    ds.StationName = "CT01"
    ds.DeviceSerialNumber = "SN123456"
    ds.AccessionNumber = patient["acc"]
    ds.StudyID = "1001"
    ds.StudyDate = ds.SeriesDate = ds.AcquisitionDate = ds.ContentDate = patient["date"]
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = uid()
    ds.FrameOfReferenceUID = study_uid + ".9"
    ds.SeriesNumber = series_no
    ds.InstanceNumber = inst_no
    ds.SeriesDescription = desc
    ds.ImageComments = f"patient {patient['plain']} scanned"
    ds.ImagePositionPatient = [0, 0, -inst_no * 5.0]
    # 嵌套序列里的姓名
    req = Dataset()
    req.RequestedProcedureID = patient["acc"]
    req.ScheduledProcedureStepDescription = "CT CHEST"
    ds.RequestAttributesSequence = Sequence([req])
    ref = Dataset()
    ref.ReferencedSOPClassUID = ds.SOPClassUID
    ref.ReferencedSOPInstanceUID = study_uid + ".77"
    ref.PatientName = patient["name"]  # 不规范但真实存在的写法
    ds.ReferencedImageSequence = Sequence([ref])
    # 私有字段里的姓名
    block = ds.private_block(0x0029, "SIEMENS CSA HEADER", create=True)
    block.add_new(0x10, "LO", patient["plain"])
    if sop_class:
        ds.SOPClassUID = sop_class
    if burned:
        ds.BurnedInAnnotation = "YES"
        ds.ImageType = ["DERIVED", "SECONDARY", "SCREEN SAVE"]
    if color:  # 三维渲染截图：CT 图像类型，但像素是彩色的（真实设备常见），没有任何“烧录”标记
        ds.SamplesPerPixel = 3
        ds.PhotometricInterpretation = "RGB"
        ds.PlanarConfiguration = 0
        ds.BitsAllocated = ds.BitsStored = 8
        ds.HighBit = 7
        ds.PixelRepresentation = 0
        ds.PixelData = bytes(ds.Rows * ds.Columns * 3)
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.SourceApplicationEntityTitle = "HOSPITAL_PACS"
    return ds


def write_series(folder, patient, study_uid, series_no, n, desc, name_fmt, **kw):
    series_uid = uid()
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(1, n + 1):
        ds = make_slice(patient, study_uid, series_uid, series_no, i, desc, **kw)
        ds.save_as(folder / name_fmt.format(i=i, name=patient["plain"].replace(" ", "")), enforce_file_format=True)


def main(out):
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} 不是空文件夹，为避免覆盖已有文件，已停止。")
    zhang = dict(name="ZHANG^SAN=张三", plain="ZHANG SAN", id="HZ00123456", birth="19560315",
                 sex="M", acc="A20240101001", date="20240101", charset="GB18030")
    li = dict(name="LI^SIWEI", plain="LI SIWEI", id="000789", birth="19800720",
              sex="F", acc="A20240202002", date="20240202", charset="ISO_IR 192")
    wang = dict(name="WANG^WU", plain="WANG WU", id="P5566778", birth="20231101",
                sex="M", acc="A20240303003", date="20240303", charset="GB18030")

    # 患者 1：中文姓名文件夹，多层目录 + DICOMDIR + 报告 txt + 剂量报告截图
    p1 = out / "张三_20240101"
    st = uid()
    write_series(p1 / "DICOM" / "ST0" / "SE0", zhang, st, 2, 6, "Chest 1.25mm", "IM{i}")
    write_series(p1 / "DICOM" / "ST0" / "SE1", zhang, st, 1, 1, "Dose Report", "IM{i}",
                 sop_class="1.2.840.10008.5.1.4.1.1.7", burned=True)
    write_series(p1 / "DICOM" / "ST0" / "SE2", zhang, st, 3, 2, "Volume Rendering No cut", "IM{i}", color=True)
    dd = Dataset()
    dd.file_meta = FileMetaDataset()
    dd.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.1.3.10"
    dd.file_meta.MediaStorageSOPInstanceUID = uid()
    dd.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dd.FileSetID = "ZHANGSAN"
    dd.DirectoryRecordSequence = Sequence([])
    dd.save_as(p1 / "DICOMDIR", enforce_file_format=True)
    (p1 / "诊断报告.txt").write_text("患者：张三  诊断：……", encoding="utf-8")

    # 患者 2：拼音文件夹，文件名带姓名，其中一个文件没有 128 字节前导
    p2 = out / "LI_SIWEI"
    st = uid()
    write_series(p2, li, st, 3, 4, "Abdomen", "{name}_{i}.dcm")
    write_series(p2 / "recon", li, st, 4, 3, "Abdomen 5mm", "{name}_r{i}.dcm")
    raw = make_slice(li, st, uid(), 5, 1, "Scout", )
    raw.preamble = None
    raw.file_meta = FileMetaDataset()
    raw.save_as(p2 / "old_format_noheader", implicit_vr=True, little_endian=True)
    (p2 / "Thumbs.db").write_bytes(b"junk")

    # 患者 3：婴儿（年龄按月），和 患者 3 第二次检查（同 ID，分在另一个文件夹）
    write_series(out / "WANG_WU_1", wang, uid(), 1, 3, "Head", "{i}.dcm")
    wang2 = dict(wang, acc="A20240404004", date="20240404")
    write_series(out / "WANG_WU_2", wang2, uid(), 1, 2, "Head follow-up", "{i}.dcm")

    # 没有 DICOM 的文件夹
    (out / "空文件夹_只有图片").mkdir(parents=True, exist_ok=True)
    (out / "空文件夹_只有图片" / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    print(f"已生成测试数据：{out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "test_data/input")
