"""组内共用密码的规则（CLI 与 GUI 共用，避免两边写得不一样）。

规则（用户确认）：
- 至少 8 位（不再要求 12 位）；
- 必须同时包含数字、大写英文字母、小写英文字母；
- 其它字符（符号、中文等）可以加，但不强制。
"""

from __future__ import annotations

from . import constants as C
from .errors import VaultError


def password_rules(password: str) -> list[tuple[str, bool]]:
    """返回 [(规则文案, 是否满足)]，给 GUI 做实时勾选提示。"""
    return [
        (f"至少 {C.MIN_PASSWORD_LEN} 位", len(password) >= C.MIN_PASSWORD_LEN),
        ("包含数字 0-9", any("0" <= ch <= "9" for ch in password)),
        ("包含大写字母 A-Z", any("A" <= ch <= "Z" for ch in password)),
        ("包含小写字母 a-z", any("a" <= ch <= "z" for ch in password)),
    ]


def password_rule_text() -> str:
    """一行说明，给 CLI 提示、GUI 副标题、文档共用。"""
    return f"至少 {C.MIN_PASSWORD_LEN} 位，且同时含数字 0-9、大写 A-Z、小写 a-z"


def password_problems(password: str) -> list[str]:
    """返回未满足的规则文案；空列表表示密码合格。"""
    return [text for text, ok in password_rules(password) if not ok]


def password_ok(password: str) -> bool:
    return not password_problems(password)


def validate_password(password: str, *, field: str = "密码") -> None:
    problems = password_problems(password)
    if problems:
        raise VaultError(f"{field}不符合要求：{'、'.join(problems)}")
