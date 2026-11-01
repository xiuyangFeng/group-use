"""单个 DICOM 数据集的脱敏逻辑。"""

import hashlib
import re
from collections import Counter
from datetime import date

from pydicom.datadict import dictionary_VR, keyword_for_tag
from pydicom.dataelem import DataElement
from pydicom.dataset import Dataset
from pydicom.multival import MultiValue
from pydicom.valuerep import PersonName

from . import rules

TEXT_VRS = {"LO", "SH", "LT", "ST", "UT", "UC"}
DEID_METHOD = "PS3.15 E.1 Basic Profile; Retain Patient Chars; Dates removed"

_CJK = re.compile(r"[㐀-鿿豈-﫿]")


def has_cjk(s):
    return bool(_CJK.search(s))


class UidMapper:
    """把原 UID 确定性地映射成新的 2.25.xxx UID。

    同一个 secret 下，同一个原 UID 永远得到同一个新 UID，因此同一患者内
    图像之间的引用关系不会被打断，重复处理也能得到一致的结果；
    没有 secret 则无法从新 UID 反推原 UID。
    """

    def __init__(self, secret):
        self.secret = secret
        self._cache = {}

    def __call__(self, uid):
        uid = str(uid).strip()
        if not uid:
            return uid
        new = self._cache.get(uid)
        if new is None:
            digest = hashlib.sha256(f"{self.secret}|{uid}".encode()).digest()
            new = "2.25." + str(int.from_bytes(digest[:16], "big"))
            self._cache[uid] = new
        return new


def _keep_uid(keyword, value):
    return (
        keyword in rules.KEEP_UID_KEYWORDS
        or keyword.endswith("ClassUID")
        or str(value).startswith("1.2.840.10008.")
    )


def name_variants(value):
    """从 PatientName 生成用于搜索残留的各种写法（拼音、中文、有无空格、姓名顺序）。"""
    if not value:
        return set()
    pn = value if isinstance(value, PersonName) else PersonName(str(value))
    out = set()
    for comp in (pn.alphabetic, pn.ideographic, pn.phonetic):
        comp = (comp or "").strip()
        if not comp:
            continue
        parts = [p.strip() for p in comp.split("^") if p.strip()]
        out.add(comp)
        out.add(" ".join(parts))
        out.add("".join(parts))
        if len(parts) >= 2:
            out.add(" ".join([parts[1], parts[0]]))
            out.add("".join([parts[1], parts[0]]))
    # 太短的写法（如单个拼音姓氏 "LI"）误伤太多，不作为搜索词
    return {v for v in out if (has_cjk(v) and len(v) >= 2) or len(v.replace(" ", "")) >= 4}


def collect_needles(ds):
    """从数据集中收集患者身份字符串，用于清理自由文本和事后校验。"""
    needles = set()
    for kw in ("PatientName", "OtherPatientNames"):
        val = ds.get(kw)
        if isinstance(val, (list, MultiValue)):
            for v in val:
                needles |= name_variants(v)
        else:
            needles |= name_variants(val)
    for kw in ("PatientID", "OtherPatientIDs", "AccessionNumber"):
        val = ds.get(kw)
        vals = val if isinstance(val, (list, MultiValue)) else [val]
        for v in vals:
            v = str(v or "").strip()
            if len(v) >= 4:
                needles.add(v)
    return needles


def _parse_da(value):
    s = str(value or "").strip()
    if len(s) >= 8 and s[:8].isdigit():
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None
    return None


def compute_age(ds):
    """如果没有 PatientAge，用出生日期和检查日期算出检查时年龄。必须在清空日期之前调用。"""
    if str(ds.get("PatientAge", "") or "").strip():
        return
    birth = _parse_da(ds.get("PatientBirthDate"))
    ref = None
    for kw in ("StudyDate", "SeriesDate", "AcquisitionDate", "ContentDate"):
        ref = _parse_da(ds.get(kw))
        if ref:
            break
    if not birth or not ref or ref < birth:
        return
    years = ref.year - birth.year - ((ref.month, ref.day) < (birth.month, birth.day))
    if years >= 1:
        ds.PatientAge = f"{min(years, 999):03d}Y"
    else:
        months = (ref.year - birth.year) * 12 + ref.month - birth.month - (ref.day < birth.day)
        ds.PatientAge = f"{max(months, 0):03d}M"


def element_vr(ds, tag):
    """不解码字段值就拿到 VR。隐式 VR 文件里原始字段没有 VR，查字典；查不到当作 UN。"""
    vr = ds.get_item(tag).VR
    if vr is None:
        try:
            vr = dictionary_VR(tag)
        except KeyError:
            vr = "UN"
    return vr


def is_dropped_group(group):
    """私有组（奇数组号）、Overlay（60xx）、Curve（50xx）整组删除。"""
    return group & 1 or 0x5000 <= group <= 0x50FF or 0x6000 <= group <= 0x60FF


def raw_is_empty(ds, tag):
    """不解码判断字段是否为空（只有空格/空字节也算空）。"""
    item = ds.get_item(tag)
    if item.is_raw:
        return not (item.value or b"").strip(b" \0")
    return item.is_empty


def _contains_needle(value, needles_lower):
    text = str(value).lower()
    return any(n in text for n in needles_lower)


def anonymize(ds: Dataset, code: str, uid_map: UidMapper, needles=()):
    """原地脱敏。返回 Counter：被清空的、含患者姓名/ID 的自由文本字段名 → 次数。"""
    needles_lower = [n.lower() for n in needles]
    cleaned = Counter()

    compute_age(ds)

    # 只按“字段号 + VR”做决定，绝大多数字段（数值、像素、私有大字段）不解码，原样写回或直接删除。
    # 只有 UID、自由文本、嵌套序列这些需要看内容的字段才解码。规则和逐个解码时完全一样。
    def clean(d):
        for tag in list(d.keys()):
            if is_dropped_group(tag.group):
                del d[tag]
                continue
            kw = keyword_for_tag(tag)
            vr = element_vr(d, tag)
            if kw in rules.REMOVE or vr == "UN":
                del d[tag]
            elif kw in rules.EMPTY or vr in ("PN", "DA", "DT", "AE"):
                if not raw_is_empty(d, tag):
                    d[tag] = DataElement(tag, vr, "")
            elif vr == "UI":
                elem = d[tag]
                if elem.value and not _keep_uid(kw, elem.value):
                    if isinstance(elem.value, (list, MultiValue)):
                        elem.value = [uid_map(v) for v in elem.value]
                    else:
                        elem.value = uid_map(elem.value)
            elif vr == "SQ":
                for item in d[tag].value:
                    clean(item)
            elif vr in TEXT_VRS and needles_lower and kw != "PatientID":  # PatientID 随后换成编号
                elem = d[tag]
                if elem.value and _contains_needle(elem.value, needles_lower):
                    cleaned[kw or str(tag)] += 1
                    elem.value = ""

    clean(ds)

    ds.PatientName = code
    ds.PatientID = code
    ds.PatientIdentityRemoved = "YES"
    ds.DeidentificationMethod = DEID_METHOD

    meta = getattr(ds, "file_meta", None)
    if meta is not None:
        for kw in rules.FILE_META_REMOVE:
            if kw in meta:
                delattr(meta, kw)
        if "MediaStorageSOPInstanceUID" in meta:
            meta.MediaStorageSOPInstanceUID = uid_map(meta.MediaStorageSOPInstanceUID)
    return cleaned
