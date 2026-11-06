"""组内共用密码的规则（CLI 与 GUI 共用，避免两边写得不一样）。

规则（用户确认）：任意 6 位数字，例如 135790。
忘记密码靠密保问题找回（见 core/questions.py），所以密码本身不再要求复杂。

旧版本建的保险箱用的是复杂密码，照样能解锁；只有「设新密码」时才按这里的规则检查。
"""

from __future__ import annotations

from . import constants as C
from .errors import VaultError


def password_rules(password: str) -> list[tuple[str, bool]]:
    """返回 [(规则文案, 是否满足)]，给 GUI 做实时勾选提示。"""
    return [
        (f"正好 {C.PASSWORD_LEN} 位", len(password) == C.PASSWORD_LEN),
        ("只含数字 0-9", bool(password) and all("0" <= ch <= "9" for ch in password)),
    ]


def password_rule_text() -> str:
    """一行说明，给 CLI 提示、GUI 副标题、文档共用。"""
    return f"任意 {C.PASSWORD_LEN} 位数字"


def password_problems(password: str) -> list[str]:
    """返回未满足的规则文案；空列表表示密码合格。"""
    return [text for text, ok in password_rules(password) if not ok]


def password_ok(password: str) -> bool:
    return not password_problems(password)


def validate_password(password: str, *, field: str = "密码") -> None:
    problems = password_problems(password)
    if problems:
        raise VaultError(f"{field}不符合要求：需要{password_rule_text()}")
