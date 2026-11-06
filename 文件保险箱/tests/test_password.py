"""密码（6 位数字）/ 密保问题 / 旧版恢复密钥 / 改密相关测试。"""

from __future__ import annotations

import json

import pytest

from core import constants as C, crypto, passwords
from core import questions as Q
from core.errors import AnswerError, PasswordError, RecoveryKeyError, VaultError
from core import protect
from core.header import load_header, save_header
from core.vault import Vault
from tests.conftest import PASSWORD, QUESTIONS, make_tree

NEW_PW = "135790"


def setup(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    v = Vault(root, audit_dir=audit_dir)
    res = v.create(PASSWORD, QUESTIONS)
    v.close()
    return root, res


def make_legacy(root) -> str:
    """把保险箱改成旧版本（0.5.x）的样子：有恢复密钥、没有密保问题。返回恢复密钥字符串。"""
    protect.release(root)
    hp = root / ".vault" / "header.json"
    h = load_header(hp)
    key = crypto.unwrap_keys(h.wrapped_pw, crypto.derive_kek(PASSWORD, h.kdf_salt, h.kdf), h.vault_id, "password")
    rec = crypto.new_recovery_key()
    h.wrapped_rec = crypto.wrap_keys(key, crypto.recovery_kek(rec, h.vault_id), h.vault_id, "recovery")
    h.recovery_fingerprint = crypto.recovery_fingerprint(rec)
    h.recovery_generated_at = h.created_at
    h.questions = []
    save_header(hp, h)
    protect.apply(root)
    return crypto.recovery_key_str(rec)


# ---------------------------------------------------------------- 6 位数字密码

@pytest.mark.parametrize("pw", ["123456", "000000", "987654"])
def test_pin_ok(pw):
    assert passwords.password_ok(pw)


@pytest.mark.parametrize("pw", ["", "12345", "1234567", "12345a", "abcdef", "１２３４５６", " 123456"])
def test_pin_rejected(pw):
    assert not passwords.password_ok(pw)


def test_create_rejects_complex_password(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    with pytest.raises(VaultError, match="6 位数字"):
        Vault(root, audit_dir=audit_dir).create("Test-Password-1234", QUESTIONS)
    assert not (root / ".vault" / "header.json").exists()


def test_change_password(tmp_path, audit_dir):
    root, _ = setup(tmp_path, audit_dir)
    Vault(root, audit_dir=audit_dir).change_password(PASSWORD, NEW_PW)

    v = Vault(root, audit_dir=audit_dir)
    with pytest.raises(PasswordError):
        v.unlock(password=PASSWORD)
    v = Vault(root, audit_dir=audit_dir)
    stats = v.unlock(password=NEW_PW)
    v.lock(password=NEW_PW)
    v.close()
    assert stats["files"] == 6


def test_change_password_wrong_old(tmp_path, audit_dir):
    root, _ = setup(tmp_path, audit_dir)
    with pytest.raises(PasswordError):
        Vault(root, audit_dir=audit_dir).change_password("000000", NEW_PW)


def test_change_password_new_must_be_pin(tmp_path, audit_dir):
    root, _ = setup(tmp_path, audit_dir)
    with pytest.raises(VaultError, match="6 位数字"):
        Vault(root, audit_dir=audit_dir).change_password(PASSWORD, "New-Password-5678")


# ---------------------------------------------------------------- 密保问题：设置

def test_create_requires_three_distinct_questions(tmp_path, audit_dir):
    root = tmp_path / "data"
    make_tree(root)
    bad_sets = [
        QUESTIONS[:2],
        [QUESTIONS[0], QUESTIONS[0], QUESTIONS[1]],
        [QUESTIONS[0], QUESTIONS[1], ("母亲的生日", "")],
        [("我的生日", "下个月"), QUESTIONS[1], QUESTIONS[2]],
    ]
    for qs in bad_sets:
        with pytest.raises(VaultError, match="密保"):
            Vault(root, audit_dir=audit_dir).create(PASSWORD, qs)
        assert not (root / ".vault" / "header.json").exists()


def test_answers_not_stored_in_header(tmp_path, audit_dir):
    root, _ = setup(tmp_path, audit_dir)
    raw = (root / ".vault" / "header.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["version"] == C.FORMAT_VERSION
    assert [q["q"] for q in data["security_questions"]] == [q for q, _a in QUESTIONS]
    assert PASSWORD not in raw
    for _q, a in QUESTIONS:
        assert a not in raw and Q.normalize_answer(Q.kind_of(_q), a) not in raw


# ---------------------------------------------------------------- 密保问题：答对任意一个

@pytest.mark.parametrize(
    "answers",
    [
        ["1998-03-12", None, None],
        ["", "北京", ""],
        [None, None, "1970-01-02"],
        ["错的", "错的", "1970/1/2"],  # 答错的不要紧，有一个对就行
    ],
)
def test_unlock_with_any_one_answer(tmp_path, audit_dir, answers):
    root, _ = setup(tmp_path, audit_dir)
    v = Vault(root, audit_dir=audit_dir)
    v.unlock(answers=answers)
    assert Vault(root).status()["state"] == C.UNLOCKED
    v.lock()
    v.close()


def test_wrong_answers_rejected_and_counted(tmp_path, audit_dir):
    root, _ = setup(tmp_path, audit_dir)
    with pytest.raises(AnswerError, match="不对"):
        Vault(root, audit_dir=audit_dir).unlock(answers=["1998-03-13", "上海", "1970-01-03"])
    with pytest.raises(AnswerError, match="至少"):
        Vault(root, audit_dir=audit_dir).unlock(answers=["", "  ", None])
    # 答错和输错密码一样计入连续失败次数（触发等待）
    assert Vault(root).header.failed_attempts == 2
    Vault(root, audit_dir=audit_dir).unlock(answers=[None, "北京", None])
    assert Vault(root).header.failed_attempts == 0


@pytest.mark.parametrize(
    "q, set_as, answer",
    [
        ("我的生日", "1998-03-12", "19980312"),
        ("我的生日", "1998-03-12", "1998年3月12日"),
        ("我的生日", "1998/3/12", "１９９８．０３．１２"),
        ("我出生的城市", "北京市", "北京"),
        ("我出生的城市", "Beijing", " bei jing "),
        ("我小时候的小名", "豆 豆", "豆豆。"),
    ],
)
def test_answer_normalization(q, set_as, answer):
    kind = Q.kind_of(q)
    assert Q.normalize_answer(kind, set_as) == Q.normalize_answer(kind, answer)


def test_answer_normalization_distinguishes():
    assert Q.normalize_answer(Q.KIND_DATE, "1998-03-12") != Q.normalize_answer(Q.KIND_DATE, "1998-12-03")
    assert Q.normalize_answer(Q.KIND_CITY, "北京") != Q.normalize_answer(Q.KIND_CITY, "南京")


def test_lock_verify_recover_accept_answers(tmp_path, audit_dir):
    root, _ = setup(tmp_path, audit_dir)
    assert Vault(root, audit_dir=audit_dir).verify(answers=[None, "北京", None])["ok"]
    Vault(root, audit_dir=audit_dir).unlock(password=PASSWORD)
    Vault(root, audit_dir=audit_dir).lock(answers=["1998-03-12"])
    assert Vault(root).status()["state"] == C.LOCKED


# ---------------------------------------------------------------- 忘记密码：答对一个就重置

def test_reset_with_answers(tmp_path, audit_dir):
    root, _ = setup(tmp_path, audit_dir)
    Vault(root, audit_dir=audit_dir).reset_with_answers(["", "北京市", ""], NEW_PW)

    with pytest.raises(PasswordError):
        Vault(root, audit_dir=audit_dir).unlock(password=PASSWORD)
    v = Vault(root, audit_dir=audit_dir)
    v.unlock(password=NEW_PW)
    v.lock()
    v.close()
    # 密保问题不受影响，还能继续用
    assert Vault(root).status()["questions"] == [q for q, _a in QUESTIONS]
    Vault(root, audit_dir=audit_dir).verify(answers=["19980312"])


def test_reset_with_wrong_answers(tmp_path, audit_dir):
    root, _ = setup(tmp_path, audit_dir)
    with pytest.raises(AnswerError):
        Vault(root, audit_dir=audit_dir).reset_with_answers(["2000-01-01", "广州", ""], NEW_PW)
    Vault(root, audit_dir=audit_dir).unlock(password=PASSWORD)  # 原密码还能用


def test_set_security_questions_replaces_old(tmp_path, audit_dir):
    root, _ = setup(tmp_path, audit_dir)
    new_qs = [("父亲的生日", "1966-06-06"), ("我小时候的小名", "豆豆"), ("我第一只宠物的名字", "旺财")]
    with pytest.raises(PasswordError):
        Vault(root, audit_dir=audit_dir).set_security_questions("000000", new_qs)
    res = Vault(root, audit_dir=audit_dir).set_security_questions(PASSWORD, new_qs)
    assert res == {"questions": [q for q, _a in new_qs], "dropped_recovery_key": False}
    with pytest.raises(AnswerError):
        Vault(root, audit_dir=audit_dir).verify(answers=["1998-03-12", "北京", "1970-01-02"])
    assert Vault(root, audit_dir=audit_dir).verify(answers=[None, None, "旺财"])["ok"]


# ---------------------------------------------------------------- 旧版本保险箱（有恢复密钥）

def test_legacy_vault_still_works_with_recovery_key(tmp_path, audit_dir):
    root, _ = setup(tmp_path, audit_dir)
    rec = make_legacy(root)
    # 旧版本的 header 仍按版本 1 写，旧版软件也能打开
    assert json.loads((root / ".vault" / "header.json").read_text())["version"] == C.FORMAT_VERSION_LEGACY
    st = Vault(root).status()
    assert st["has_recovery_key"] and st["questions"] == []

    key = crypto.recovery_key_parse(rec)
    v = Vault(root, audit_dir=audit_dir)
    v.unlock(recovery_key=key)
    v.lock()
    v.close()
    Vault(root, audit_dir=audit_dir).reset_with_recovery_key(key, NEW_PW)
    Vault(root, audit_dir=audit_dir).unlock(password=NEW_PW)
    assert json.loads((root / ".vault" / "header.json").read_text())["version"] == C.FORMAT_VERSION_LEGACY


def test_legacy_vault_wrong_recovery_key(tmp_path, audit_dir):
    root, _ = setup(tmp_path, audit_dir)
    make_legacy(root)
    bogus = crypto.recovery_key_parse("A" * 32)
    with pytest.raises(RecoveryKeyError):
        Vault(root, audit_dir=audit_dir).reset_with_recovery_key(bogus, NEW_PW)
    with pytest.raises(AnswerError, match="没有设置密保"):
        Vault(root, audit_dir=audit_dir).unlock(answers=["1998-03-12"])


def test_legacy_vault_upgrade_to_questions_drops_recovery_key(tmp_path, audit_dir):
    root, _ = setup(tmp_path, audit_dir)
    rec = crypto.recovery_key_parse(make_legacy(root))
    res = Vault(root, audit_dir=audit_dir).set_security_questions(PASSWORD, QUESTIONS)
    assert res["dropped_recovery_key"] is True
    data = json.loads((root / ".vault" / "header.json").read_text())
    assert data["version"] == C.FORMAT_VERSION
    assert data["wrapped_vault_key_recovery"] == {} and data["recovery_fingerprint"] == ""
    with pytest.raises(RecoveryKeyError):
        Vault(root, audit_dir=audit_dir).unlock(recovery_key=rec)
    Vault(root, audit_dir=audit_dir).unlock(answers=[None, "北京", None])


def test_new_vault_has_no_recovery_key(tmp_path, audit_dir):
    root, _ = setup(tmp_path, audit_dir)
    with pytest.raises(RecoveryKeyError, match="没有恢复密钥"):
        Vault(root, audit_dir=audit_dir).reset_with_recovery_key(crypto.new_recovery_key(), NEW_PW)


def test_change_password_blocked_when_unlock_incomplete(tmp_path, audit_dir):
    """手工把状态改成 UNLOCKING 后，改密应被拒绝，避免改坏正在进行的操作。"""
    root, _ = setup(tmp_path, audit_dir)
    from core.errors import RecoveryNeededError

    protect.release(root)  # 模拟绕过保护手工改 header
    h = load_header(root / ".vault" / "header.json")
    h.set_state(C.UNLOCKING)
    save_header(root / ".vault" / "header.json", h)
    with pytest.raises(RecoveryNeededError):
        Vault(root, audit_dir=audit_dir).change_password(PASSWORD, NEW_PW)
    with pytest.raises(RecoveryNeededError):
        Vault(root, audit_dir=audit_dir).reset_with_answers(["1998-03-12"], NEW_PW)
