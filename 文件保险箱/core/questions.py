"""密保问题：新建保险箱时设 3 个，忘记密码时答对任意 1 个就能解锁 / 重置密码。

每个问题的答案各自经 Argon2id 派生出一把钥匙，包裹同一个 vault_key（和密码的做法一样），
所以答对哪一个都能解开；header 里只存问题原文和包裹后的密文，不存答案。

答案在派生前先「规范化」，让同一个答案的不同写法都能对上：
- 日期类（生日）：1998-03-12、1998/3/12、1998年3月12日、19980312 都算同一个；
- 城市类：去掉末尾的「市」，北京 = 北京市；
- 所有类：忽略空格、标点、全角半角、英文大小写。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from .errors import VaultError

QUESTION_COUNT = 3

KIND_DATE = "date"
KIND_CITY = "city"
KIND_TEXT = "text"

# (问题, 类型)。顺序就是下拉框里的顺序；新建向导默认选前三个不同的。
PRESETS: list[tuple[str, str]] = [
    ("我的生日", KIND_DATE),
    ("我出生的城市", KIND_CITY),
    ("父亲的生日", KIND_DATE),
    ("母亲的生日", KIND_DATE),
    ("配偶 / 伴侣的生日", KIND_DATE),
    ("父亲出生的城市", KIND_CITY),
    ("母亲出生的城市", KIND_CITY),
    ("我就读的小学", KIND_TEXT),
    ("我小时候的小名", KIND_TEXT),
    ("我第一只宠物的名字", KIND_TEXT),
]

PRESET_KINDS = dict(PRESETS)

PLACEHOLDERS = {
    KIND_DATE: "年月日，如 1998-03-12",
    KIND_CITY: "如 北京",
    KIND_TEXT: "答案",
}


def kind_of(question: str) -> str:
    return PRESET_KINDS.get(question, KIND_TEXT)


def placeholder(question: str) -> str:
    return PLACEHOLDERS[kind_of(question)]


# ---------------------------------------------------------------- 规范化

def _parse_date(text: str) -> str | None:
    """能认出来就返回 YYYYMMDD，认不出返回 None。只认「年在前」的写法。"""
    groups = re.findall(r"\d+", unicodedata.normalize("NFKC", text))
    if len(groups) == 1 and len(groups[0]) == 8:
        y, m, d = int(groups[0][:4]), int(groups[0][4:6]), int(groups[0][6:])
    elif len(groups) == 3 and len(groups[0]) == 4:
        y, m, d = (int(g) for g in groups)
    else:
        return None
    try:
        date(y, m, d)
    except ValueError:
        return None
    if not 1900 <= y <= 2100:
        return None
    return f"{y:04d}{m:02d}{d:02d}"


def _clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    # 只留字母、数字（含中文），空格和各种标点一律去掉
    return "".join(ch for ch in text if unicodedata.category(ch)[0] in ("L", "N"))


def normalize_answer(kind: str, answer: str) -> str:
    """把答案变成用来派生钥匙的规范形式。认不出的日期退回到「只留数字」，答错就是答错。"""
    if kind == KIND_DATE:
        parsed = _parse_date(answer)
        if parsed is not None:
            return parsed
        return "".join(re.findall(r"\d", unicodedata.normalize("NFKC", answer)))
    cleaned = _clean_text(answer)
    if kind == KIND_CITY and len(cleaned) > 1 and cleaned.endswith("市"):
        cleaned = cleaned[:-1]
    return cleaned


# ---------------------------------------------------------------- 设置时的校验

def answer_problem(question: str, answer: str) -> str | None:
    """设置密保时这个答案能不能用；能用返回 None，不能返回一句原因。"""
    kind = kind_of(question)
    if not answer.strip():
        return "还没填答案"
    if kind == KIND_DATE:
        if _parse_date(answer) is None:
            return "日期看不懂，请按 年-月-日 填，如 1998-03-12"
        return None
    if not normalize_answer(kind, answer):
        return "答案里要有文字或数字"
    return None


def questions_problems(pairs: list[tuple[str, str]]) -> list[str]:
    """返回所有问题；空列表表示这组密保可以用。"""
    problems: list[str] = []
    if len(pairs) != QUESTION_COUNT:
        return [f"需要正好 {QUESTION_COUNT} 个密保问题"]
    seen: set[str] = set()
    for i, (q, a) in enumerate(pairs, 1):
        q = (q or "").strip()
        if not q:
            problems.append(f"第 {i} 个问题还没选")
            continue
        if q in seen:
            problems.append(f"第 {i} 个问题和前面重复了")
        seen.add(q)
        p = answer_problem(q, a or "")
        if p:
            problems.append(f"第 {i} 个问题：{p}")
    return problems


def validate_questions(pairs: list[tuple[str, str]]) -> None:
    problems = questions_problems(pairs)
    if problems:
        raise VaultError("密保问题不符合要求：" + "；".join(problems))
