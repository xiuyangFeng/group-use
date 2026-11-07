"""快速编码路径：直接按“原始字段”脱敏并写出 DICOM 字节，跳过 pydicom 的 Dataset 重建/整体重编码。

为什么快：普通路径要把整个文件解析成 pydicom Dataset、逐字段修改，再把整个 Dataset 重新编码
（每个字段都要走一遍 pydicom 的 Python 层封装）。快速路径按字段只做“保留原字节 / 删除 / 清空 /
换 UID”四种决定，没改动的字段直接拷贝原文件的字节，只对改过的字段调用 pydicom 的编码函数。

安全保证：
1. 只能在结构完全认得的情况下启用（有 DICM 文件头、显式/隐式 VR 小端、各字段长度正常、
   有像素数据且 Field 长度一致等）；任何拿不准的情况返回 None，调用方自动退回原来的慢路径，
   所以未知/异常数据不会被写坏。
2. 快速路径的输出必须和慢路径**逐字节一致**，自动测试（tests/test_fastpath.py）会用假数据
   的全部文件做对比；一旦哪天 pydicom 行为变化导致不一致，测试会直接失败。
3. 写盘之前仍然会走原来的 verify_bytes() 独立校验（重新解析完整字节、检查有没有残留患者信息），
   校验不通过的文件不会写出。
4. 可以用环境变量 DEID_FASTPATH=0 整体关闭快速路径（排查问题时用）。

支持范围：CT/MR 等常规图像（显式或隐式 VR 小端）；压缩像素数据、Deflated、大端、没有像素数据、
结构异常的文件自动走慢路径。
"""

import io
from collections import Counter

from pydicom.charset import convert_encodings
from pydicom.datadict import dictionary_VR
from pydicom.dataelem import DataElement, convert_raw_data_element
from pydicom.dataset import Dataset
from pydicom.filebase import DicomBytesIO
from pydicom.filereader import (_read_file_meta_info, data_element_generator,
                                data_element_offset_to_value, read_preamble)
from pydicom.filewriter import write_data_element, write_file_meta_info
from pydicom.multival import MultiValue
from pydicom.tag import Tag
from pydicom.values import convert_value

from . import rules
from .anonymizer import (DEID_METHOD, TEXT_VRS, _EMPTY_TAGS, _REMOVE_TAGS, _keep_uid, _keyword,
                         clean_dataset, compute_age, is_dropped_group)

PIXEL_TAG = Tag(0x7FE0, 0x0010)
PATIENT_NAME_TAG = Tag(0x0010, 0x0010)
PATIENT_ID_TAG = Tag(0x0010, 0x0020)
IDENTITY_REMOVED_TAG = Tag(0x0012, 0x0062)
DEID_METHOD_TAG = Tag(0x0012, 0x0063)
PATIENT_AGE_TAG = Tag(0x0010, 0x1010)
BIRTH_TAG = Tag(0x0010, 0x0030)
STUDY_DATE_TAGS = (Tag(0x0008, 0x0020), Tag(0x0008, 0x0021), Tag(0x0008, 0x0022), Tag(0x0008, 0x0023))
CHARSET_TAG = Tag(0x0008, 0x0005)
OVERWRITE_TAGS = {PATIENT_NAME_TAG, PATIENT_ID_TAG, IDENTITY_REMOVED_TAG, DEID_METHOD_TAG}
DATASET_REMOVE_VRS = {"PN", "DA", "DT", "AE"}

# 特殊字段分派：用 dict 查表，避免每个字段都做 pydicom 的 Tag 比较（Tag.__eq__ 是 Python 函数，很慢）
_SPECIAL = {int(PIXEL_TAG): "pixel", int(CHARSET_TAG): "charset",
            int(PATIENT_AGE_TAG): "age", int(BIRTH_TAG): "birth"}
for _t in STUDY_DATE_TAGS:
    _SPECIAL[int(_t)] = "date"
for _t in OVERWRITE_TAGS:
    _SPECIAL[int(_t)] = "overwrite"

# 每个字段该做什么：按 (字段号, VR) 缓存，同一个患者的几千张图重复命中，省掉大量分支判断
_RAW, _SKIP, _EMPTY, _UI, _TEXT, _SQ = range(6)
_ACTION_CACHE = {}


def enabled():
    """快速路径默认开启；DEID_FASTPATH=0 时关闭（排查问题时用）。"""
    import os
    return os.environ.get("DEID_FASTPATH", "1") != "0"


def _action_for(tag, vr):
    cached = _ACTION_CACHE.get(tag)
    if cached is not None and cached[0] == vr:
        return cached[1]
    if is_dropped_group(tag.group) or tag in _REMOVE_TAGS or vr == "UN":
        action = _SKIP
    elif tag in _EMPTY_TAGS or vr in DATASET_REMOVE_VRS:
        action = _EMPTY
    elif vr == "UI":
        action = _UI
    elif vr == "SQ":
        action = _SQ
    elif vr in TEXT_VRS:
        action = _TEXT
    else:
        action = _RAW
    _ACTION_CACHE[tag] = (vr, action)
    return action


def _raw_empty(value):
    """不解码判断原始字段是否为空（只有空格/空字节也算空），和 anonymizer.raw_is_empty 一致。"""
    return not (value or b"").strip(b" \0")


def encode_file(data, code, uid_map, needles):
    """快速路径：返回 (脱敏后的完整文件字节, 被清空的字段 Counter)；不适合时返回 None。

    返回 None 表示调用方应该退回原来的慢路径（read_full + anonymize + encode_dicom）。
    只读传入的字节，不修改任何文件。
    """
    try:
        return _encode(data, code, uid_map, needles)
    except Exception:  # 任何解析/编码异常都退回慢路径，不把不认识的数据写出去
        return None


def _encode(data, code, uid_map, needles):
    fp = io.BytesIO(data)
    try:
        preamble = read_preamble(fp, force=False)
    except Exception:
        return None          # 没有 DICM 文件头（老格式）→ 慢路径
    if preamble is None:
        return None
    meta = _read_file_meta_info(fp)
    ts = getattr(meta, "TransferSyntaxUID", None)
    if ts is None or ts.is_deflated or not ts.is_little_endian:
        return None          # 不认识的传输语法 → 慢路径
    implicit = ts.is_implicit_VR
    view = memoryview(data)
    needles_lower = [n.lower() for n in needles]
    need_needles = bool(needles_lower)
    cleaned = Counter()
    items = []               # [(tag, bytes 原样拷贝 | DataElement 改过的字段)]
    encoding = None
    age_present = False
    age_raw = None
    birth = ""
    study_dates = []
    pixel = None
    seen = set()
    offset = data_element_offset_to_value
    action_for = _action_for

    for elem in data_element_generator(fp, implicit, True):
        tag = elem.tag
        if tag in seen:
            return None      # 重复字段（不规范文件）：Dataset 只会保留最后一个，快速路径不猜 → 慢路径
        seen.add(tag)
        special = _SPECIAL.get(int(tag))
        if special is None and tag.element == 0 and tag.group > 6:
            continue         # 和 write_dataset 一样：不写已废弃的组长度
        if special == "charset":
            if items:
                return None  # 字符集字段不在最前面：解码规则拿不准 → 慢路径
            try:
                encoding = convert_encodings(str(convert_value("CS", elem, encoding)))
            except Exception:
                return None
            start = elem.value_tell - offset(implicit, "CS")
            items.append((tag, view[start:elem.value_tell + elem.length]))
            continue

        vr = elem.VR
        if vr is None:
            try:
                vr = dictionary_VR(tag)
            except KeyError:
                vr = "UN"
        if not isinstance(elem, DataElement) and getattr(elem, "length", 0) not in (0xFFFFFFFF, 0, None) \
                and elem.value_tell + elem.length > len(data):
            return None      # 文件被截断/长度字段异常 → 慢路径
        action = action_for(tag, vr)
        if action in (_RAW, _EMPTY, _UI, _TEXT) and not isinstance(elem, DataElement) \
                and getattr(elem, "length", 0) == 0xFFFFFFFF:
            return None      # 长度未定义的非序列字段：慢路径更稳妥

        if special == "pixel":
            if isinstance(elem, DataElement) or getattr(elem, "length", 0) == 0xFFFFFFFF:
                return None  # 压缩像素（分段）→ 慢路径
            start = elem.value_tell - offset(implicit, vr)
            end = elem.value_tell + elem.length
            if end > len(data):
                return None
            pixel = view[start:end]
            items.append((tag, pixel))
            continue

        # 年龄：没有 PatientAge 时按原规则算（需要出生日期和检查日期）
        if special == "age":
            age_present = not _raw_empty(elem.value)
            start = elem.value_tell - offset(implicit, vr)
            age_raw = (tag, view[start:elem.value_tell + elem.length])
            if age_present:
                items.append(age_raw)   # 原来就有年龄：原样保留
            continue                    # 空值暂不写：算出来再补，算不出来再补原样空字段
        if special in ("birth", "date"):
            try:
                val = str(convert_value("DA", elem, encoding)).strip() if elem.value else ""
            except Exception:
                return None
            if special == "birth":
                birth = val
            else:
                study_dates.append(val)

        if special == "overwrite":
            continue         # PatientName / PatientID 等最后统一重写

        if action == _SKIP:
            continue
        if action == _SQ:
            if not isinstance(elem, DataElement):
                try:
                    elem = convert_raw_data_element(elem, encoding=encoding)
                except Exception:
                    return None
            for item in elem.value:
                clean_dataset(item, uid_map, needles_lower, cleaned)
            items.append((tag, elem))
            continue
        if isinstance(elem, DataElement):
            return None      # 除序列外不该出现已解析的字段 → 慢路径

        start = elem.value_tell - offset(implicit, vr)
        end = elem.value_tell + elem.length
        if action == _RAW:
            items.append((tag, view[start:end]))
        elif action == _EMPTY:
            items.append((tag, view[start:end]) if _raw_empty(elem.value)
                         else (tag, DataElement(tag, vr, "")))
        elif action == _UI:
            try:
                val = convert_value(vr, elem, encoding) if elem.value else ""
            except Exception:
                return None
            if val and not _keep_uid(_keyword(tag), val):
                new = [uid_map(v) for v in val] if isinstance(val, (list, MultiValue)) else uid_map(val)
                items.append((tag, DataElement(tag, vr, new)))
            else:
                items.append((tag, view[start:end]))
        else:  # _TEXT：含患者姓名/ID 的自由文本清空
            if need_needles:
                try:
                    low = str(convert_value(vr, elem, encoding)).lower()
                except Exception:
                    return None
                if any(n in low for n in needles_lower):
                    cleaned[_keyword(tag) or str(tag)] += 1
                    items.append((tag, DataElement(tag, vr, "")))
                else:
                    items.append((tag, view[start:end]))
            else:
                items.append((tag, view[start:end]))

    if pixel is None:
        return None          # 没有像素数据（报告/结构化文档）→ 慢路径

    # 原来没有 PatientAge 时按同样规则补一个（和 anonymize/compute_age 一致）
    if not age_present:
        tmp = Dataset()
        if birth:
            tmp.PatientBirthDate = birth
        for d in study_dates:
            if d:
                tmp.StudyDate = d
                break
        compute_age(tmp)
        age = str(tmp.get("PatientAge", "") or "").strip()
        if age:
            items.append((PATIENT_AGE_TAG, DataElement(PATIENT_AGE_TAG, "AS", age)))
        elif age_raw is not None:
            items.append(age_raw)       # 算不出来：保留原来的空字段，和慢路径一致

    items.append((PATIENT_NAME_TAG, DataElement(PATIENT_NAME_TAG, "PN", code)))
    items.append((PATIENT_ID_TAG, DataElement(PATIENT_ID_TAG, "LO", code)))
    items.append((IDENTITY_REMOVED_TAG, DataElement(IDENTITY_REMOVED_TAG, "CS", "YES")))
    items.append((DEID_METHOD_TAG, DataElement(DEID_METHOD_TAG, "LO", DEID_METHOD)))

    # 文件头：删掉要删的字段、重映射 MediaStorageSOPInstanceUID（每条都是新解析出来的，可以直接改）
    for kw in rules.FILE_META_REMOVE:
        if kw in meta:
            del meta[kw]
    if "MediaStorageSOPInstanceUID" in meta:
        meta.MediaStorageSOPInstanceUID = uid_map(meta.MediaStorageSOPInstanceUID)

    out = DicomBytesIO()
    out.write(bytes(preamble))
    out.write(b"DICM")
    write_file_meta_info(out, meta, enforce_standard=True)
    out.is_implicit_VR = implicit
    out.is_little_endian = True
    for _tag, item in sorted(items, key=lambda x: x[0]):
        if isinstance(item, (bytes, memoryview)):
            out.write(item)
        else:
            write_data_element(out, item, encoding)
    return out.getvalue(), cleaned
