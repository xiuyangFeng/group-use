"""保险箱生命周期：创建 / 解锁 / 上锁 / 恢复 / 校验 / 改密码。

安全不变量（任何一步断电都必须成立）：
1. 只有「另一份副本已经 fsync 且落进 manifest」之后，才允许删除当前副本；
2. manifest 的每次提交都是一次 SQLite 事务（WAL + synchronous=FULL）；
3. 恢复逻辑不依赖 journal：以 manifest + 文件系统实际状态为准，逐条把
   文件带到目标状态（明文或密文），可重复执行（幂等）。

因此正常操作和恢复共用同一套 _finalize_lock / _finalize_unlock 代码，
日常路径本身就是恢复路径，降低「没测到的分支」风险。
"""

from __future__ import annotations

import functools
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable, Iterable

from cryptography.exceptions import InvalidTag

from . import constants as C
from . import crypto, fsutil, inuse, passwords, protect, transform
from . import questions as Q
from .audit import AuditLog
from .control import OperationControl
from .errors import (
    AlreadyVaultError,
    AnswerError,
    DiskSpaceError,
    InUseError,
    IntegrityError,
    NotAVaultError,
    PasswordError,
    RecoveryKeyError,
    RecoveryNeededError,
    VaultError,
    VaultStateError,
)
from .fsutil import FsEntry, long_path
from .header import Header, load_header, now_iso, save_header
from .journal import Journal
from .lockfile import VaultLock
from .manifest import FileEntry, Kept, Manifest

ProgressFn = Callable[[dict], None]
Answers = "list[str | None] | None"  # 和 header 里的密保问题一一对应；空的跳过
BATCH = 512  # 每批最多文件数（一批内由 C++ 引擎并行处理）
BATCH_MIN_BYTES = 64 * 1024 * 1024
BATCH_MAX_BYTES = 2 * 1024 * 1024 * 1024
KEEP_MARGIN_BYTES = 1024 * 1024 * 1024  # 解锁时保留密文所需的额外余量
IN_PROGRESS = (C.LOCKING, C.UNLOCKING, C.INCONSISTENT)


def locked(method):
    """给修改类操作加跨进程互斥锁，并在进入时重新读取 header。

    上锁状态的保险箱有防移动 / 防删除保护（core/protect.py），拿到互斥锁后先撤掉，
    操作结束时如果还是「已上锁」再加回去（不管操作成功还是失败、密码对不对）。"""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        self._acquire()
        try:
            self._header = None  # 重新读，避免缓存过期
            protect.release(self.root)
            try:
                return method(self, *args, **kwargs)
            finally:
                self._reprotect()
        finally:
            self._release()

    return wrapper


class Vault:
    def __init__(self, root: str | os.PathLike, *, audit_dir: str | os.PathLike | None = None):
        self.root = Path(os.path.expanduser(os.fspath(root)))
        self.vault_dir = self.root / C.VAULT_DIRNAME
        self.header_path = self.vault_dir / C.HEADER_NAME
        self.manifest_path = self.vault_dir / C.MANIFEST_NAME
        self.journal_path = self.vault_dir / C.JOURNAL_NAME
        self.blob_dir = self.vault_dir / C.BLOBS_DIRNAME
        self.tmp_dir = self.vault_dir / C.TMP_DIRNAME
        self.marker_path = self.root / C.MARKER_NAME

        self._audit_dir = Path(audit_dir).expanduser() if audit_dir else fsutil.platform_data_dir()
        self._header: Header | None = None
        self._key: bytes | None = None
        self._manifest: Manifest | None = None
        self._journal: Journal | None = None
        self._flock: VaultLock | None = None

    # ---------------------------------------------------------------- 互斥锁

    def _acquire(self) -> None:
        if self._flock is None:
            self._flock = VaultLock(self.vault_dir / "instance.lock")
        self._flock.acquire()

    def _release(self) -> None:
        if self._flock is not None:
            self._flock.release()

    # ================================================================ 防移动 / 防删除

    def _reprotect(self) -> None:
        """处于「已上锁」就加保护；其它状态（解锁着、中断待恢复）保持可改。"""
        try:
            h = load_header(self.header_path) if self.header_path.exists() else None
        except Exception:
            return
        if h is not None and h.state == C.LOCKED:
            protect.apply(self.root)
            protect.hold(self.root)  # Windows 图形界面：占用句柄，程序开着时谁都挪不走

    def ensure_protected(self) -> bool:
        """旧版本上锁的保险箱没有保护：补上。返回现在是否受保护。"""
        if not protect.enabled() or not self.is_vault():
            return False
        if protect.is_protected(self.root):
            return True
        if self.header.state != C.LOCKED:
            return False
        try:
            self._acquire()
        except VaultError:
            return False  # 另一个窗口正在操作，它结束时会自己加上
        try:
            self._header = None
            self._reprotect()
        finally:
            self._release()
        return protect.is_protected(self.root)

    # ================================================================ 基础

    def is_vault(self) -> bool:
        return self.header_path.exists()

    @property
    def header(self) -> Header:
        if self._header is None:
            self._header = load_header(self.header_path)
        return self._header

    def _save_header(self) -> None:
        assert self._header is not None
        save_header(self.header_path, self._header)

    def _audit(self) -> AuditLog:
        return AuditLog(self._audit_dir / "audit.jsonl")

    def _record(self, event: str, *, result: str = "ok", detail: str = "") -> None:
        try:
            self._audit().record(
                event,
                vault_path=str(self.root),
                vault_id=self._header.vault_id if self._header else "",
                result=result,
                detail=detail,
            )
        except Exception:
            # 审计失败不能影响数据操作
            pass

    def _require_root(self) -> None:
        if not self.root.exists():
            raise NotAVaultError(f"文件夹不存在：{self.root}")
        if not self.root.is_dir():
            raise NotAVaultError(f"不是文件夹：{self.root}")

    def _require_vault(self) -> None:
        self._require_root()
        if not self.is_vault():
            raise NotAVaultError(f"这个文件夹不是保险箱：{self.root}")

    def _chunk_size(self) -> int:
        try:
            return max(64 * 1024, int(os.environ.get("FDVAULT_CHUNK_SIZE", C.DEFAULT_CHUNK_SIZE)))
        except ValueError:
            return C.DEFAULT_CHUNK_SIZE

    def _plain_path(self, e: FileEntry) -> Path:
        return self.root.joinpath(*e.path.split("/"))

    def _blob_path(self, e: FileEntry) -> Path:
        return self.blob_dir / (e.uuid.hex() + ".blob")

    def _file_key(self, e: FileEntry) -> bytes:
        assert self._key is not None
        return crypto.file_key(self._key, e.uuid)

    def _ensure_space(self, needed: int) -> None:
        free = fsutil.free_bytes(self.root)
        if free < needed:
            raise DiskSpaceError(
                f"磁盘空间不够：需要约 {fsutil.human_bytes(needed)}，实际只剩 {fsutil.human_bytes(free)}。"
                " 建议先把其它大文件挪走，或把保险箱拆小一点。"
            )

    def _open_manifest(self) -> Manifest:
        if self._manifest is None:
            self._manifest = Manifest(self.manifest_path, self._key).open()
        return self._manifest

    def _open_journal(self) -> Journal:
        if self._journal is None:
            self._journal = Journal(self.journal_path).open()
        return self._journal

    def _open_session(self) -> None:
        self._open_manifest()
        self._open_journal()

    def _close_session(self) -> None:
        if self._manifest is not None:
            self._manifest.close()
            self._manifest = None
        if self._journal is not None:
            self._journal.close()
            self._journal = None

    def close(self) -> None:
        self._close_session()
        self._key = None
        self._header = None

    def abort_create(self) -> bool:
        """撤销「还没开始首次上锁」的创建（GUI 创建向导取消时用）。

        只在状态 LOCKING 且还没有任何 manifest / 密文时允许：此时文件夹里全是明文，
        删掉 .vault 和说明文件就等于回到从未创建过。
        """
        self._header = None
        if not self.is_vault():
            return False
        h = self.header
        if h.state != C.LOCKING:
            return False
        if self.manifest_path.exists():
            return False
        if self.blob_dir.exists() and any(self.blob_dir.iterdir()):
            return False
        self._acquire()
        try:
            if self.manifest_path.exists():
                return False
            if self.blob_dir.exists() and any(self.blob_dir.iterdir()):
                return False
        finally:
            self._release()
        try:
            if self.marker_path.exists():
                os.unlink(long_path(self.marker_path))
            shutil.rmtree(long_path(self.vault_dir))
        except OSError:
            return False
        self._header = None
        return True

    # ---------------------------------------------------------------- 密钥

    def _open_key(
        self,
        password: str | None = None,
        recovery_key: bytes | None = None,
        answers: "list[str | None] | None" = None,
    ) -> bytes:
        if self._key is not None:
            return self._key
        h = self.header
        if answers is not None:
            self._key = self._key_from_answers(h, answers)
        elif recovery_key is not None:
            if not h.has_recovery_key:
                raise RecoveryKeyError("这个保险箱没有恢复密钥，请用密保问题找回")
            kek = crypto.recovery_kek(recovery_key, h.vault_id)
            try:
                self._key = crypto.unwrap_keys(h.wrapped_rec, kek, h.vault_id, "recovery")
            except InvalidTag:
                raise RecoveryKeyError("恢复密钥不对")
        elif password:
            kek = crypto.derive_kek(password, h.kdf_salt, h.kdf)
            try:
                self._key = crypto.unwrap_keys(h.wrapped_pw, kek, h.vault_id, "password")
            except InvalidTag:
                raise PasswordError("密码不对")
        else:
            raise PasswordError("需要密码")
        return self._key

    # ---------------------------------------------------------------- 密保问题

    @staticmethod
    def _question_purpose(index: int) -> str:
        return f"question:{index}"

    def _wrap_questions(self, key: bytes, vault_id: str, pairs: list[tuple[str, str]], kdf_params: dict) -> list[dict]:
        """每个答案各派生一把钥匙包裹 vault_key：答对任意一个都能解开。"""
        out = []
        for i, (q, a) in enumerate(pairs):
            q = q.strip()
            kind = Q.kind_of(q)
            salt = os.urandom(crypto.SALT_LEN)
            kdf = {k: v for k, v in kdf_params.items() if k != "salt"}
            kdf["salt"] = crypto.b64e(salt)
            kek = crypto.derive_kek(Q.normalize_answer(kind, a), salt, kdf)
            out.append(
                {
                    "q": q,
                    "kind": kind,
                    "kdf": kdf,
                    "wrapped": crypto.wrap_keys(key, kek, vault_id, self._question_purpose(i)),
                }
            )
        return out

    def _key_from_answers(self, h: Header, answers: "list[str | None]") -> bytes:
        if not h.questions:
            raise AnswerError("这个保险箱还没有设置密保问题")
        tried = False
        for i, q in enumerate(h.questions):
            a = answers[i] if i < len(answers) else None
            if not a or not a.strip():
                continue
            tried = True
            kdf = q["kdf"]
            kek = crypto.derive_kek(Q.normalize_answer(q.get("kind", Q.KIND_TEXT), a), crypto.b64d(kdf["salt"]), kdf)
            try:
                return crypto.unwrap_keys(q["wrapped"], kek, h.vault_id, self._question_purpose(i))
            except InvalidTag:
                continue
        if not tried:
            raise AnswerError("请至少回答一个密保问题")
        raise AnswerError("密保答案不对")

    # ---------------------------------------------------------------- 进度

    @staticmethod
    def _emit(
        progress: ProgressFn | None,
        phase: str,
        *,
        done_files: int = 0,
        total_files: int = 0,
        done_bytes: int = 0,
        total_bytes: int = 0,
        message: str = "",
    ) -> None:
        if progress:
            progress(
                {
                    "phase": phase,
                    "done_files": done_files,
                    "total_files": total_files,
                    "done_bytes": done_bytes,
                    "total_bytes": total_bytes,
                    "message": message,
                }
            )

    # ---------------------------------------------------------------- 状态展示

    def status(self) -> dict:
        self._require_vault()
        h = self.header
        return {
            "root": str(self.root),
            "state": h.state,
            "state_since": h.state_since,
            "vault_id": h.vault_id,
            "created_at": h.created_at,
            "file_count": h.file_count,
            "total_bytes": h.total_bytes,
            "recovery_fingerprint": h.recovery_fingerprint,
            "has_recovery_key": h.has_recovery_key,
            "questions": h.question_texts,
            "needs_recovery": h.state in (C.LOCKING, C.UNLOCKING, C.INCONSISTENT),
            "protected": protect.is_protected(self.root),
            "in_use_by": inuse.describe(inuse.active_users(self.root)),
        }

    # ================================================================ 创建

    def create(
        self,
        password: str,
        questions: list[tuple[str, str]],
        progress: ProgressFn | None = None,
        control: OperationControl | None = None,
    ) -> dict:
        """一步创建（CLI / 测试用）：生成密钥、设密保并首次上锁。"""
        prepared = self.prepare_new(password, questions)
        stats = self.initial_lock(progress=progress, control=control)
        return {"questions": prepared["questions"], "stats": stats}

    def prepare_new(self, password: str, questions: list[tuple[str, str]]) -> dict:
        """第一步：校验 + 生成 header（含 3 个密保问题，几秒）。之后调用 initial_lock。"""
        self._header = None
        self._require_root()
        if self.is_vault():
            raise AlreadyVaultError("这个文件夹已经是保险箱了")
        if self.vault_dir.exists():
            leftovers = [p.name for p in self.vault_dir.iterdir() if p.name != "instance.lock"]
            if leftovers:
                raise AlreadyVaultError("文件夹里有 .vault 但缺少 header.json，可能上次创建没完成；请先备份整个文件夹再排查")
        passwords.validate_password(password)
        pairs = [(str(q), str(a)) for q, a in questions]
        Q.validate_questions(pairs)

        scan, total_bytes, skipped = fsutil.scan_tree(self.root, {C.VAULT_DIRNAME, C.MARKER_NAME})
        max_file = max((e.size for e in scan if not e.is_dir), default=0)
        self._ensure_space(2 * max_file + C.SPACE_RESERVE_BYTES)

        salt = os.urandom(crypto.SALT_LEN)
        kdf = crypto.choose_kdf_params()
        kdf["salt"] = crypto.b64e(salt)
        kek = crypto.derive_kek(password, salt, kdf)
        key = os.urandom(crypto.KEY_LEN)
        vault_id = str(uuid.uuid4())
        wrapped_pw = crypto.wrap_keys(key, kek, vault_id, "password")
        wrapped_q = self._wrap_questions(key, vault_id, pairs, kdf)

        ts = now_iso()
        h = Header(
            vault_id=vault_id,
            created_at=ts,
            kdf=kdf,
            wrapped_pw=wrapped_pw,
            wrapped_rec={},
            recovery_fingerprint="",
            recovery_generated_at="",
            state=C.LOCKING,
            state_since=ts,
            questions=wrapped_q,
        )

        self._acquire()
        try:
            if self.is_vault():
                raise AlreadyVaultError("这个文件夹已经是保险箱了")
            fsutil.ensure_dir(self.vault_dir, 0o700)
            fsutil.ensure_dir(self.blob_dir, 0o700)
            fsutil.ensure_dir(self.tmp_dir, 0o700)
            save_header(self.header_path, h)
            self._header = h
            self._key = key
            if not self.marker_path.exists():
                fsutil.atomic_write_bytes(self.marker_path, C.MARKER_TEXT.encode("utf-8"), mode=0o644)
            from . import crash

            crash.point("create:after_header")

            return {
                "questions": h.question_texts,
                "files": len([e for e in scan if not e.is_dir]),
                "dirs": len([e for e in scan if e.is_dir]),
                "bytes": total_bytes,
                "skipped": skipped,
            }
        finally:
            self._release()

    @locked
    def initial_lock(self, progress: ProgressFn | None = None, control: OperationControl | None = None) -> dict:
        """第二步：首次上锁（把明文全部加密）。"""
        self._require_vault()
        h = self.header
        if h.state != C.LOCKING:
            raise VaultStateError(f"当前状态不能完成首次上锁：{h.state}")
        self._open_session()
        try:
            self._record("CREATE", detail="创建保险箱并首次上锁")
            return self._lock_impl(progress, control=control)
        finally:
            self._close_session()

    # ================================================================ 上锁

    @locked
    def lock(
        self,
        password: str | None = None,
        progress: ProgressFn | None = None,
        control: OperationControl | None = None,
        *,
        recovery_key: bytes | None = None,
        answers: "list[str | None] | None" = None,
    ) -> dict:
        self._require_vault()
        h = self.header
        if h.state == C.LOCKED:
            raise VaultStateError("这个保险箱目前已经锁着")
        if h.state in IN_PROGRESS:
            raise RecoveryNeededError(h.state)
        if h.state != C.UNLOCKED:
            raise VaultStateError(f"当前状态不能上锁：{h.state}")

        self._precheck_in_use()
        self._open_key(password=password, recovery_key=recovery_key, answers=answers)
        prescan = self._precheck_busy()
        self._open_session()
        try:
            h.set_state(C.LOCKING)
            self._save_header()
            self._journal.append("begin_lock")
            stats = self._lock_impl(progress, control=control, prescan=prescan)
            self._record("LOCK", detail=f"{stats['files']} 个文件 / {fsutil.human_bytes(stats['bytes'])}")
            return stats
        finally:
            self._close_session()

    def _precheck_in_use(self) -> None:
        """上锁前看有没有外部程序（如 CT 脱敏工具）声明正在用这个保险箱里的文件。

        脱敏这类长任务只在读写的一瞬间打开文件，_precheck_busy 查不到；中途上锁会让
        它读不到原始数据、或把映射表重新以明文写回已上锁的文件夹，所以单独拦住。"""
        users = inuse.active_users(self.root)
        if not users:
            return
        raise InUseError(
            f"{inuse.describe(users)}正在使用这个保险箱里的文件，现在不能上锁。\n"
            "请等它处理完（或在那边点「停止」）再上锁。"
            f"如果那个程序已经关掉了，{C.INUSE_STALE_SECONDS // 60} 分钟后标记会自动失效。\n"
            "保险箱没有改动，文件还是原样。"
        )

    def _precheck_busy(self):
        """上锁前检查文件是否正被其它程序占用（Windows 共享冲突 / macOS lsof）。

        返回这次的目录扫描结果，上锁时直接复用（免得同一棵树扫两遍）；没扫时返回 None。"""
        if os.environ.get("FDVAULT_SKIP_BUSY_CHECK") == "1":
            return None
        try:
            result = fsutil.scan_tree(self.root, {C.VAULT_DIRNAME, C.MARKER_NAME})
        except OSError:
            return None
        scan = result[0]
        root_s = os.fspath(self.root)
        paths = [Path(os.path.join(root_s, fsutil.from_rel(e.rel))) for e in scan if not e.is_dir]
        busy = fsutil.busy_files(paths)
        if not busy:
            return result
        names = "、".join(f"「{p.name}」" for p in busy[:5])
        more = "" if len(busy) <= 5 else f" 等 {len(busy)} 个文件"
        raise VaultError(
            f"{names}{more}正在被其他程序使用，现在不能上锁。\n"
            "请先关闭打开这些文件的软件（比如 Excel、看图工具）再试。\n"
            "保险箱没有改动，文件还是原样。"
        )

    def _lock_impl(self, progress: ProgressFn | None, skipped: list[str] | None = None, control: OperationControl | None = None, prescan=None) -> dict:
        manifest = self._open_manifest()
        journal = self._open_journal()
        if prescan is not None:
            scan, total_bytes, skipped_now = prescan
        else:
            scan, total_bytes, skipped_now = fsutil.scan_tree(self.root, {C.VAULT_DIRNAME, C.MARKER_NAME})
        skipped = skipped_now if skipped is None else skipped
        max_file = max((e.size for e in scan if not e.is_dir), default=0)
        self._ensure_space(2 * max_file + C.SPACE_RESERVE_BYTES)

        existing = manifest.load_all()
        files = self._reconcile(scan, existing)
        journal.append("reconciled", files=len(files), dirs=len([e for e in scan if e.is_dir]))

        total_files = len(files)
        self._emit(progress, "正在加密文件", total_files=total_files, total_bytes=total_bytes)
        self._finalize_lock(files, total_bytes, progress, control=control)

        entries = manifest.load_all()
        self._remove_empty_dirs([e for e in entries if e.is_dir])
        files_n, dirs_n, total = manifest.count()
        h = self.header
        h.file_count = files_n
        h.total_bytes = total
        h.set_state(C.LOCKED)
        self._save_header()
        self._cleanup_tmp()
        journal.append("lock_done", files=files_n, dirs=dirs_n, bytes=total)
        self._emit(progress, "上锁完成", done_files=total_files, total_files=total_files, done_bytes=total_bytes, total_bytes=total_bytes)
        return {"files": files_n, "dirs": dirs_n, "bytes": total, "skipped": skipped}

    def _reconcile(self, scan: list[FsEntry], existing: list[FileEntry]) -> list[FileEntry]:
        """把 manifest 对齐到当前明文树（正常上锁路径）。返回按扫描顺序的文件条目。"""
        manifest = self._open_manifest()
        by_path = {e.path: e for e in existing}
        keep: set[str] = set()
        result: dict[str, FileEntry] = {}
        changed: list[FileEntry] = []
        for se in scan:
            keep.add(se.rel)
            e = by_path.get(se.rel)
            if e is None or e.is_dir != se.is_dir:
                if e is not None:
                    manifest.delete(e.uuid)
                e = FileEntry(
                    uuid=crypto.new_uuid(),
                    path=se.rel,
                    is_dir=se.is_dir,
                    size=0 if se.is_dir else se.size,
                    mtime_ns=se.mtime_ns,
                    mode=se.mode,
                    sha256=b"",
                    state=C.ST_DIR if se.is_dir else C.ST_PENDING_LOCK,
                )
                manifest.insert(e)
            else:
                e.size = 0 if se.is_dir else se.size
                e.mtime_ns = se.mtime_ns
                e.mode = se.mode
                e.sha256 = b""
                e.state = C.ST_DIR if se.is_dir else C.ST_PENDING_LOCK
                changed.append(e)
            result[se.rel] = e
        manifest.update_meta_many(changed)  # 路径没变，不用重新加密路径
        gone = [e.uuid for path, e in by_path.items() if path not in keep]
        manifest.delete_many(gone)
        manifest.delete_kept_many(gone)
        manifest.commit()
        return [result[se.rel] for se in scan if not se.is_dir]

    # ---------------------------------------------------------------- 上锁核心

    def _io_batches(self, files: list[FileEntry]) -> Iterable[list[FileEntry]]:
        """把文件分批交给 C++ 引擎并行处理。

        一批内的文件同时加 / 解密；批与批之间提交 manifest、删掉旧副本。一批里新旧两份副本
        同时占盘，所以每批的总字节数受剩余空间限制（至少放一个文件）。"""
        try:
            free = fsutil.free_bytes(self.root)
        except OSError:
            free = 0
        limit = (free - C.SPACE_RESERVE_BYTES) // 2
        limit = max(BATCH_MIN_BYTES, min(BATCH_MAX_BYTES, limit))
        batch: list[FileEntry] = []
        size = 0
        for e in files:
            if batch and (len(batch) >= BATCH or size + e.size > limit):
                yield batch
                batch, size = [], 0
            batch.append(e)
            size += e.size
        if batch:
            yield batch

    def _encrypt_entries(
        self,
        entries: list[FileEntry],
        on_progress: transform.BatchProgressFn | None,
        control: OperationControl | None,
    ) -> list[tuple[int, bytes]]:
        """并行加密到 .vault/tmp，fsync 后逐个 rename 成正式密文，最后 fsync 一次 blobs 目录。"""
        from . import crash

        assert self._key is not None
        chunk = self._chunk_size()
        tmp_dir = os.fspath(self.tmp_dir)
        blob_dir = os.fspath(self.blob_dir)
        tmps = [long_path(os.path.join(tmp_dir, e.uuid.hex() + ".part")) for e in entries]
        jobs = [
            transform.EncryptJob(self._plain_path(e), tmp, self._file_key(e), e.uuid, chunk, size_hint=e.size)
            for e, tmp in zip(entries, tmps)
        ]
        renamed = 0
        try:
            results = transform.encrypt_many(jobs, progress=on_progress, control=control)
            for e, tmp in zip(entries, tmps):
                crash.point("lock:before_blob_rename")
                os.replace(tmp, long_path(os.path.join(blob_dir, e.uuid.hex() + ".blob")))
                renamed += 1
                crash.point("lock:after_blob_rename")
            fsutil.fsync_dir(self.blob_dir)
            return results
        finally:
            for tmp in tmps[renamed:]:  # 已经改名的不用再删
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _finalize_lock(self, files: list[FileEntry], total_bytes: int, progress: ProgressFn | None, control: OperationControl | None = None) -> None:
        """把一批文件带到「只有密文」状态。明文是唯一可信来源；密文不可信就重加密。"""
        from . import crash

        manifest = self._open_manifest()
        journal = self._open_journal()
        total_files = len(files)
        done_files = 0
        done_bytes = 0
        kept = manifest.load_kept()
        reuse = self._reusable_blobs(files, kept)
        for batch in self._io_batches(files):
            ready: list[FileEntry] = []
            to_encrypt: list[FileEntry] = []
            updated: list[FileEntry] = []
            for e in batch:
                if control is not None:
                    control.check()
                k = reuse.get(e.uuid)
                if k is not None:
                    # 解锁后明文、密文都没动过：保留的密文就是这份明文加密的结果，直接用，不用重新加密
                    e.size = k.size
                    e.sha256 = k.sha256
                    e.state = C.ST_PENDING_LOCK
                    updated.append(e)
                    ready.append(e)
                    done_files += 1
                    done_bytes += k.size
                    continue
                plain = self._plain_path(e)
                blob = self._blob_path(e)
                p_exists = os.path.isfile(long_path(plain))
                b_exists = os.path.exists(long_path(blob))

                if p_exists:
                    if b_exists and e.uuid in kept:
                        # 解锁时保留的密文，但明文改过（或密文被动过）：以明文为准重新加密，新密文原子替换旧的
                        to_encrypt.append(e)
                        continue
                    if b_exists:
                        # 上次中断留下的密文：能对上就直接用，对不上就重加密
                        try:
                            b_size, b_sha = transform.verify_blob(
                                blob, self._file_key(e), e.uuid, os.path.getsize(long_path(plain))
                            )
                        except VaultError:
                            b_size, b_sha = None, None
                        if b_sha is not None:
                            p_sha = transform.sha256_file(plain)
                            if p_sha == b_sha:
                                e.size = b_size
                                e.sha256 = b_sha
                                e.state = C.ST_PENDING_LOCK
                                updated.append(e)
                                ready.append(e)
                                done_files += 1
                                done_bytes += b_size
                                continue
                        try:
                            os.unlink(long_path(blob))
                        except OSError:
                            pass
                    to_encrypt.append(e)
                elif b_exists:
                    # 明文已经删掉：密文必须能校验通过，否则就是数据损坏
                    try:
                        size, sha = transform.verify_blob(blob, self._file_key(e), e.uuid, e.size or None)
                    except VaultError as exc:
                        raise IntegrityError(f"文件「{e.path}」的密文损坏，且明文已不在：{exc}") from exc
                    if e.sha256 and sha != e.sha256:
                        raise IntegrityError(f"文件「{e.path}」的密文与记录不一致，明文也已不在")
                    e.size = size
                    e.sha256 = sha
                    e.state = C.ST_SEALED
                    updated.append(e)
                    journal.append("sealed_existing", uuid=e.uuid.hex())
                    done_files += 1
                    done_bytes += size
                else:
                    raise IntegrityError(f"文件「{e.path}」的明文和密文都找不到，无法上锁")

            if to_encrypt:
                base_files, base_bytes = done_files, done_bytes

                def on_progress(b: int, f: int, base_files=base_files, base_bytes=base_bytes) -> None:
                    self._emit(
                        progress,
                        "正在加密文件",
                        done_files=base_files + f,
                        total_files=total_files,
                        done_bytes=base_bytes + b,
                        total_bytes=total_bytes,
                    )

                results = self._encrypt_entries(to_encrypt, on_progress, control)
                for e, (size, sha) in zip(to_encrypt, results):
                    e.size = size
                    e.sha256 = sha
                    e.state = C.ST_PENDING_LOCK
                    updated.append(e)
                    ready.append(e)
                    done_files += 1
                    done_bytes += size

            manifest.update_meta_many(updated)
            crash.point("lock:before_manifest_commit")
            if control is not None:
                control.check()  # 提交前最后一道安全检查：取消则留下可恢复的中间态
            manifest.commit()
            crash.point("lock:after_manifest_commit")

            parents = self._unlink_plaintexts(ready)
            for e in ready:
                e.state = C.ST_SEALED
            manifest.set_state_many([e.uuid for e in ready], C.ST_SEALED)
            manifest.commit()
            for p in parents:
                fsutil.fsync_dir(p)

            self._emit(
                progress,
                "正在加密文件",
                done_files=done_files,
                total_files=total_files,
                done_bytes=done_bytes,
                total_bytes=total_bytes,
            )
        if kept:
            manifest.clear_kept()  # 全部是密文了，指纹记录不再需要
            manifest.commit()
        self._remove_orphan_blobs()
        journal.append("lock_files_done", count=total_files)

    def _unlink_plaintexts(self, ready: list[FileEntry]) -> set[str]:
        """删掉已经有密文的明文（密文已 fsync 且 manifest 已提交）。文件多时多线程删。返回涉及的目录。"""
        from . import crash

        root_s = os.fspath(self.root)

        def one(e: FileEntry):
            crash.point("lock:before_unlink_plaintext")
            plain = os.path.join(root_s, fsutil.from_rel(e.path))
            try:
                os.unlink(long_path(plain))
            except FileNotFoundError:
                return None
            except PermissionError as exc:
                return exc
            crash.point("lock:after_unlink_plaintext")
            return os.path.dirname(plain)

        if len(ready) < 64:
            results = [one(e) for e in ready]
        else:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=8) as ex:
                results = list(ex.map(one, ready, chunksize=64))
        for e, r in zip(ready, results):
            if isinstance(r, PermissionError):
                raise VaultError(
                    f"文件「{e.path}」删不掉，可能正被其他程序占用（比如 Excel、看图软件）。\n"
                    "请关闭那个程序后再点上锁；保险箱现在是可恢复的中间状态，不会丢数据。"
                ) from r
        return {r for r in results if isinstance(r, str)}

    def _reusable_blobs(self, files: list[FileEntry], kept: dict[bytes, Kept]) -> dict[bytes, Kept]:
        """解锁后明文和保留的密文都没被动过的文件（两边指纹和解锁时记的完全一样）。"""
        cand = [e for e in files if e.uuid in kept]
        if not cand:
            return {}
        fps = transform.stat_many([self._plain_path(e) for e in cand] + [self._blob_path(e) for e in cand])
        p_fps, b_fps = fps[: len(cand)], fps[len(cand) :]
        out: dict[bytes, Kept] = {}
        for e, pf, bf in zip(cand, p_fps, b_fps):
            k = kept[e.uuid]
            if pf is not None and bf is not None and pf == k.plain_fp and bf == k.blob_fp:
                out[e.uuid] = k
        return out

    def _remove_orphan_blobs(self) -> None:
        """删掉 manifest 里已经没有对应条目的密文（解锁期间被删掉 / 改名 / 换类型的文件留下的）。
        这些条目的删除已经提交，密文不会再被用到。"""
        live = {u.hex() for u in self._open_manifest().file_uuids()}
        try:
            names = os.listdir(long_path(self.blob_dir))
        except OSError:
            return
        removed = 0
        for n in names:
            if n.endswith(".blob") and n[: -len(".blob")] not in live:
                try:
                    os.unlink(long_path(os.path.join(os.fspath(self.blob_dir), n)))
                    removed += 1
                except OSError:
                    pass
        if removed:
            fsutil.fsync_dir(self.blob_dir)

    def _remove_empty_dirs(self, dirs: list[FileEntry]) -> None:
        for e in sorted(dirs, key=lambda x: x.path.count("/"), reverse=True):
            try:
                os.rmdir(long_path(self._plain_path(e)))
            except OSError:
                pass

    # ================================================================ 解锁

    def _throttle_unlock(self) -> None:
        """连续输错密码后强制等待，防止有人守着界面暴力猜。计数存在 header 里，重启不清零。"""
        attempts = int(self.header.failed_attempts or 0)
        if attempts < C.MAX_FREE_ATTEMPTS:
            return
        delay = min(
            C.THROTTLE_BASE_SECONDS + (attempts - C.MAX_FREE_ATTEMPTS) * 5,
            C.THROTTLE_MAX_SECONDS,
        )
        if os.environ.get("FDVAULT_KDF_FAST") == "1":
            delay = 0.02  # 测试模式：只验证逻辑，不真的等 10 秒
        if delay > 0:
            time.sleep(delay)

    @locked
    def unlock(
        self,
        password: str | None = None,
        progress: ProgressFn | None = None,
        recovery_key: bytes | None = None,
        control: OperationControl | None = None,
        *,
        answers: "list[str | None] | None" = None,
    ) -> dict:
        self._require_vault()
        h = self.header
        if h.state in IN_PROGRESS:
            raise RecoveryNeededError(h.state)
        if h.state == C.UNLOCKED:
            raise VaultStateError("这个保险箱已经解锁了")
        if h.state != C.LOCKED:
            raise VaultStateError(f"当前状态不能解锁：{h.state}")
        if not self.manifest_path.exists():
            raise IntegrityError("manifest.db 丢失：保险箱的元数据不见了，请从备份恢复，不要继续操作")

        if recovery_key is None:
            # 6 位密码和密保答案都容易被猜：连续输错同样要等（恢复密钥是 160 位随机数，不用）
            self._throttle_unlock()
        try:
            self._open_key(password=password, recovery_key=recovery_key, answers=answers)
        except (PasswordError, RecoveryKeyError, AnswerError) as exc:
            if isinstance(exc, (PasswordError, AnswerError)):
                h.failed_attempts = (h.failed_attempts or 0) + 1
                h.last_failed_at = now_iso()
                try:
                    self._save_header()
                except OSError:
                    pass
            self._record("UNLOCK_FAILED", result="fail", detail=str(exc))
            raise
        if h.failed_attempts:
            h.failed_attempts = 0
            h.last_failed_at = ""
            self._save_header()

        self._open_session()
        try:
            manifest = self._open_manifest()
            entries = manifest.load_all()
            files = [e for e in entries if not e.is_dir]
            files_n, _, _ = manifest.count()
            if h.file_count and files_n != h.file_count:
                raise IntegrityError(
                    f"manifest 记录 {files_n} 个文件，header 记录 {h.file_count} 个，不一致；"
                    "保险箱可能被外部改动过，请先从备份恢复"
                )
            self._precheck_blobs(files)
            max_size = max((e.size for e in files), default=0)
            self._ensure_space(2 * max_size + C.SPACE_RESERVE_BYTES)

            h.set_state(C.UNLOCKING)
            self._save_header()
            self._open_journal().append("begin_unlock", files=len(files))
            self._cleanup_partials()

            total_bytes = sum(e.size for e in files)
            self._emit(progress, "正在解密文件", total_files=len(files), total_bytes=total_bytes)
            self._finalize_unlock(entries, progress, total_bytes, control=control)

            files_n, dirs_n, total = manifest.count()
            h.file_count = files_n
            h.total_bytes = total
            h.set_state(C.UNLOCKED)
            self._save_header()
            self._cleanup_tmp()
            self._open_journal().append("unlock_done", files=files_n, dirs=dirs_n)
            self._record("UNLOCK", detail=f"{files_n} 个文件 / {fsutil.human_bytes(total)}")
            self._emit(progress, "解锁完成", done_files=len(files), total_files=len(files), done_bytes=total_bytes, total_bytes=total_bytes)
            return {"files": files_n, "dirs": dirs_n, "bytes": total}
        finally:
            self._close_session()

    def _precheck_blobs(self, files: list[FileEntry]) -> None:
        missing = [e.path for e in files if not os.path.exists(long_path(self._blob_path(e)))]
        if missing:
            head = "、".join(missing[:5])
            more = "" if len(missing) <= 5 else f" 等 {len(missing)} 个"
            raise IntegrityError(f"缺少密文文件：{head}{more}。保险箱可能被外部改动过，请先从备份恢复。")

    def _decrypt_entries(
        self,
        entries: list[FileEntry],
        on_progress: transform.BatchProgressFn | None,
        control: OperationControl | None,
    ) -> list[tuple[int, bytes]]:
        """并行解密到各自目录下的 *.fdv-part（引擎里已校验 tag、大小和 sha256 并 fsync），
        再逐个 rename 成正式文件、恢复时间和权限，最后 fsync 涉及的目录。"""
        from . import crash

        plains = [self._plain_path(e) for e in entries]
        parts = [p.parent / (p.name + ".fdv-part") for p in plains]
        for p in {str(x.parent) for x in plains}:
            Path(p).mkdir(parents=True, exist_ok=True)
        jobs = [
            transform.DecryptJob(
                self._blob_path(e), part, self._file_key(e), e.uuid, e.size if e.size else None, e.sha256
            )
            for e, part in zip(entries, parts)
        ]
        renamed = 0
        try:
            results = transform.decrypt_many(jobs, progress=on_progress, control=control)
            parents: set[str] = set()
            for e, plain, part, (_size, sha) in zip(entries, plains, parts, results):
                crash.point("unlock:after_blob_read_verify")
                if e.sha256 and sha != e.sha256:
                    raise IntegrityError(f"文件「{e.path}」解密后校验不一致")
                crash.point("unlock:before_rename")
                plain_s = long_path(plain)
                os.replace(long_path(part), plain_s)
                renamed += 1
                parents.add(os.path.dirname(os.fspath(plain)))
                fsutil.set_mtime_ns(plain_s, e.mtime_ns)
                fsutil.set_mode(plain_s, e.mode)
                crash.point("unlock:after_rename")
            for p in parents:
                fsutil.fsync_dir(p)
            return results
        finally:
            for part in parts[renamed:]:  # 已经改名的不用再删
                try:
                    os.unlink(long_path(part))
                except OSError:
                    pass

    def _finalize_unlock(self, entries: list[FileEntry], progress: ProgressFn | None, total_bytes: int, control: OperationControl | None = None) -> None:
        from . import crash

        manifest = self._open_manifest()
        journal = self._open_journal()
        dirs = [e for e in entries if e.is_dir]
        for e in sorted(dirs, key=lambda x: x.path.count("/")):
            self._plain_path(e).mkdir(parents=True, exist_ok=True)

        files = [e for e in entries if not e.is_dir]
        total_files = len(files)
        done_files = 0
        done_bytes = 0
        keep = self._keep_ciphertext(files)
        for batch in self._io_batches(files):
            ready: list[FileEntry] = []
            delete_blobs: list[FileEntry] = []
            matched: set[bytes] = set()  # 已有明文且和记录的哈希一致（密文就是它的加密结果）
            plan: list[tuple[FileEntry, bool, bool]] = []  # (条目, 明文在, 密文在)
            for e in batch:
                if control is not None:
                    control.check()
                p_exists = os.path.isfile(long_path(self._plain_path(e)))
                b_exists = os.path.exists(long_path(self._blob_path(e)))
                if not p_exists and not b_exists:
                    raise IntegrityError(f"文件「{e.path}」的密文找不到，无法解锁")
                plan.append((e, p_exists, b_exists))

            # 上次中断留下的明文：先并行算哈希，对不上的重新解密
            have_plain = [e for e, p, _b in plan if p]
            hashes = dict(
                zip(
                    (e.uuid for e in have_plain),
                    transform.sha256_many([self._plain_path(e) for e in have_plain], control=control),
                )
            )
            to_decrypt: list[FileEntry] = []
            for e, p_exists, b_exists in plan:
                if p_exists:
                    p_sha = hashes[e.uuid]
                    if e.sha256 and p_sha != e.sha256:
                        if not b_exists:
                            raise IntegrityError(f"文件「{e.path}」内容与记录不一致，且没有密文可恢复")
                        to_decrypt.append(e)
                    elif e.sha256:
                        matched.add(e.uuid)
                    else:
                        e.sha256 = p_sha
                        e.size = os.path.getsize(long_path(self._plain_path(e)))
                else:
                    to_decrypt.append(e)

            decrypted: dict[bytes, tuple[int, bytes]] = {}
            if to_decrypt:
                base_files, base_bytes = done_files, done_bytes

                def on_progress(b: int, f: int, base_files=base_files, base_bytes=base_bytes) -> None:
                    self._emit(
                        progress,
                        "正在解密文件",
                        done_files=base_files + f,
                        total_files=total_files,
                        done_bytes=base_bytes + b,
                        total_bytes=total_bytes,
                    )

                results = self._decrypt_entries(to_decrypt, on_progress, control)
                decrypted = {e.uuid: r for e, r in zip(to_decrypt, results)}

            for e, p_exists, b_exists in plan:
                if e.uuid in decrypted and not e.sha256:
                    e.size, e.sha256 = decrypted[e.uuid]
                e.state = C.ST_PENDING_UNLOCK
                ready.append(e)
                if b_exists:
                    delete_blobs.append(e)
                if not p_exists:
                    journal.append("opened", uuid=e.uuid.hex())
                done_files += 1
                done_bytes += e.size
            manifest.update_meta_many(ready)
            if keep:
                # 保留密文：记下明文和密文此刻的指纹，上锁时都没变就不用重新加密
                known = [e for e in delete_blobs if e.uuid in decrypted or e.uuid in matched]
                self._record_kept(known)
            else:
                manifest.delete_kept_many([e.uuid for e in delete_blobs])

            crash.point("unlock:before_manifest_commit")
            if control is not None:
                control.check()
            manifest.commit()
            crash.point("unlock:after_manifest_commit")

            if not keep:
                # 磁盘不够同时放明文和密文：和以前一样，明文落盘后删掉密文
                for e in delete_blobs:
                    crash.point("unlock:before_unlink_blob")
                    try:
                        os.unlink(long_path(self._blob_path(e)))
                    except FileNotFoundError:
                        pass
                    crash.point("unlock:after_unlink_blob")
                if delete_blobs:
                    fsutil.fsync_dir(self.blob_dir)
            for e in ready:
                e.state = C.ST_OPEN
            manifest.set_state_many([e.uuid for e in ready], C.ST_OPEN)
            manifest.commit()

            self._emit(
                progress,
                "正在解密文件",
                done_files=done_files,
                total_files=total_files,
                done_bytes=done_bytes,
                total_bytes=total_bytes,
            )
        journal.append("unlock_files_done", count=total_files)

    def _keep_ciphertext(self, files: list[FileEntry]) -> bool:
        """解锁时保留密文（下次上锁没改过的文件就不用重新加密）。

        要多占一份空间：剩余空间放得下全部明文（再留 1 GB 余量）才保留，否则按老办法边解边删。
        FDVAULT_KEEP_CIPHERTEXT=0 可关闭。"""
        if os.environ.get("FDVAULT_KEEP_CIPHERTEXT", "1") == "0":
            return False
        need = sum(e.size for e in files) + C.SPACE_RESERVE_BYTES + KEEP_MARGIN_BYTES
        try:
            return fsutil.free_bytes(self.root) >= need
        except OSError:
            return False

    def _record_kept(self, entries: list[FileEntry]) -> None:
        """在明文已经改名到位、时间和权限也恢复之后取指纹（改时间 / 权限本身会变状态改变时间）。"""
        if not entries:
            return
        fps = transform.stat_many([self._plain_path(e) for e in entries] + [self._blob_path(e) for e in entries])
        p_fps, b_fps = fps[: len(entries)], fps[len(entries) :]
        rows = [
            (e.uuid, Kept(e.sha256, e.size, pf, bf))
            for e, pf, bf in zip(entries, p_fps, b_fps)
            if pf is not None and bf is not None and e.sha256
        ]
        self._open_manifest().put_kept_many(rows)

    def _restore_dirs(self, dirs: list[FileEntry]) -> None:
        for e in sorted(dirs, key=lambda x: x.path.count("/"), reverse=True):
            p = self._plain_path(e)
            if p.is_dir():
                fsutil.set_mode(p, e.mode)
                fsutil.set_mtime_ns(p, e.mtime_ns)

    # ================================================================ 恢复

    @locked
    def recover(
        self,
        password: str | None = None,
        progress: ProgressFn | None = None,
        rollback: bool = False,
        recovery_key: bytes | None = None,
        control: OperationControl | None = None,
        *,
        answers: "list[str | None] | None" = None,
    ) -> dict:
        self._require_vault()
        h = self.header
        if h.state not in IN_PROGRESS:
            raise VaultStateError("当前没有需要恢复的操作")
        if h.state == C.INCONSISTENT:
            raise VaultStateError("保险箱处于异常状态，当前版本不能自动恢复，请先备份并联系维护者")
        if h.state == C.UNLOCKING and not self.manifest_path.exists():
            raise IntegrityError("manifest.db 丢失：无法恢复解锁操作，请从备份恢复")

        self._open_key(password=password, recovery_key=recovery_key, answers=answers)
        self._open_session()
        try:
            manifest = self._open_manifest()
            journal = self._open_journal()
            journal.append("begin_recover", state=h.state, rollback=rollback)
            self._cleanup_partials()

            scan, total_bytes, skipped = fsutil.scan_tree(self.root, {C.VAULT_DIRNAME, C.MARKER_NAME})
            self._reconcile_loose(scan)
            entries = manifest.load_all()

            # 继续原来的方向，或回滚到另一个方向
            target_lock = (h.state == C.LOCKING) != rollback
            h.set_state(C.LOCKING if target_lock else C.UNLOCKING)
            self._save_header()

            if target_lock:
                files = [e for e in entries if not e.is_dir]
                total_bytes = sum(e.size for e in files)
                self._emit(progress, "正在恢复（继续上锁）", total_files=len(files), total_bytes=total_bytes)
                self._finalize_lock(files, total_bytes, progress, control=control)
                entries = manifest.load_all()
                self._remove_empty_dirs([e for e in entries if e.is_dir])
                files_n, dirs_n, total = manifest.count()
                h.file_count = files_n
                h.total_bytes = total
                h.set_state(C.LOCKED)
                self._save_header()
                self._cleanup_tmp()
                action = "继续上锁"
            else:
                self._emit(progress, "正在恢复（继续解锁）", total_files=len(entries), total_bytes=total_bytes)
                self._finalize_unlock(entries, progress, sum(e.size for e in entries if not e.is_dir), control=control)
                entries = manifest.load_all()
                self._restore_dirs([e for e in entries if e.is_dir])
                files_n, dirs_n, total = manifest.count()
                h.file_count = files_n
                h.total_bytes = total
                h.set_state(C.UNLOCKED)
                self._save_header()
                self._cleanup_tmp()
                action = "继续解锁"

            journal.append("recover_done", target=h.state)
            self._record(
                "RECOVER",
                detail=f"{action}（原状态 {h.state_since} 时中断）",
            )
            self._emit(progress, "恢复完成", done_files=0, total_files=0, done_bytes=0, total_bytes=0)
            return {"files": files_n, "dirs": dirs_n, "bytes": total, "state": h.state, "action": action, "skipped": skipped}
        finally:
            self._close_session()

    def _reconcile_loose(self, scan: list[FsEntry]) -> None:
        manifest = self._open_manifest()
        existing = {e.path: e for e in manifest.load_all()}
        added = 0
        for se in scan:
            e = existing.get(se.rel)
            if e is None:
                e = FileEntry(
                    uuid=crypto.new_uuid(),
                    path=se.rel,
                    is_dir=se.is_dir,
                    size=0 if se.is_dir else se.size,
                    mtime_ns=se.mtime_ns,
                    mode=se.mode,
                    sha256=b"",
                    state=C.ST_DIR if se.is_dir else C.ST_PENDING_LOCK,
                )
                manifest.insert(e)
                added += 1
            elif e.is_dir != se.is_dir:
                raise IntegrityError(f"路径「{se.rel}」的类型和记录不一致")
        manifest.commit()
        if added:
            self._open_journal().append("reconcile_loose", added=added)

    # ================================================================ 校验

    @locked
    def verify(
        self,
        password: str | None = None,
        progress: ProgressFn | None = None,
        recovery_key: bytes | None = None,
        control: OperationControl | None = None,
        *,
        answers: "list[str | None] | None" = None,
    ) -> dict:
        self._require_vault()
        h = self.header
        if h.state in IN_PROGRESS:
            raise RecoveryNeededError(h.state)
        if h.state != C.LOCKED:
            raise VaultStateError("只有上锁状态才能做完整性校验（解锁时文件可被正常修改，校验没意义）")
        if not self.manifest_path.exists():
            raise IntegrityError("manifest.db 丢失：保险箱的元数据不见了，请从备份恢复")
        self._open_key(password=password, recovery_key=recovery_key, answers=answers)
        self._open_session()
        try:
            manifest = self._open_manifest()
            entries = manifest.load_all()
            files = [e for e in entries if not e.is_dir]
            total_bytes = sum(e.size for e in files)
            problems: list[tuple[int, dict]] = []
            present: list[tuple[int, FileEntry]] = []
            for idx, e in enumerate(files):
                if control is not None:
                    control.check()
                if not os.path.exists(long_path(self._blob_path(e))):
                    problems.append((idx, {"path": e.path, "reason": "密文缺失"}))
                    continue
                present.append((idx, e))

            def on_progress(b: int, f: int) -> None:
                self._emit(progress, "正在校验", done_files=f, total_files=len(files), done_bytes=b, total_bytes=total_bytes)

            results = transform.verify_many(
                [
                    transform.DecryptJob(self._blob_path(e), None, self._file_key(e), e.uuid, e.size or None)
                    for _idx, e in present
                ],
                progress=on_progress,
                control=control,
            )
            for (idx, e), r in zip(present, results):
                if isinstance(r, VaultError):
                    problems.append((idx, {"path": e.path, "reason": str(r)}))
                    continue
                _size, sha = r
                if e.sha256 and sha != e.sha256:
                    problems.append((idx, {"path": e.path, "reason": "内容哈希不一致"}))
            problems = [p for _idx, p in sorted(problems, key=lambda x: x[0])]
            ok = not problems
            self._record("VERIFY", result="ok" if ok else "fail", detail=f"{len(files)} 个文件，{len(problems)} 个问题")
            return {"files": len(files), "bytes": total_bytes, "problems": problems, "ok": ok}
        finally:
            self._close_session()

    # ================================================================ 密码 / 恢复密钥

    def _require_stable_state(self) -> None:
        if self.header.state in IN_PROGRESS:
            raise RecoveryNeededError(self.header.state)

    def _set_new_password(self, key: bytes, new_password: str) -> None:
        h = self.header
        salt = os.urandom(crypto.SALT_LEN)
        kdf = crypto.choose_kdf_params()
        kdf["salt"] = crypto.b64e(salt)
        kek_new = crypto.derive_kek(new_password, salt, kdf)
        h.kdf = kdf
        h.wrapped_pw = crypto.wrap_keys(key, kek_new, h.vault_id, "password")
        h.failed_attempts = 0
        h.last_failed_at = ""

    @locked
    def change_password(self, old_password: str, new_password: str) -> None:
        self._require_vault()
        self._require_stable_state()
        passwords.validate_password(new_password, field="新密码")
        h = self.header
        kek = crypto.derive_kek(old_password, h.kdf_salt, h.kdf)
        try:
            key = crypto.unwrap_keys(h.wrapped_pw, kek, h.vault_id, "password")
        except InvalidTag:
            raise PasswordError("原密码不对")
        self._set_new_password(key, new_password)
        self._save_header()
        self._record("CHANGE_PASSWORD")

    @locked
    def reset_with_answers(self, answers: "list[str | None]", new_password: str) -> None:
        """忘记密码：答对任意一个密保问题，设新密码。"""
        self._require_vault()
        self._require_stable_state()
        passwords.validate_password(new_password, field="新密码")
        h = self.header
        if h.failed_attempts:
            self._throttle_unlock()
        try:
            key = self._key_from_answers(h, answers)
        except AnswerError as exc:
            h.failed_attempts = (h.failed_attempts or 0) + 1
            h.last_failed_at = now_iso()
            try:
                self._save_header()
            except OSError:
                pass
            self._record("RESET_FAILED", result="fail", detail=str(exc))
            raise
        self._set_new_password(key, new_password)
        self._save_header()
        self._record("RESET_BY_QUESTIONS")

    @locked
    def reset_with_recovery_key(self, recovery_key: bytes, new_password: str) -> None:
        """旧版本建的保险箱还有恢复密钥：仍然可以用它重置密码。"""
        self._require_vault()
        self._require_stable_state()
        passwords.validate_password(new_password, field="新密码")
        h = self.header
        if not h.has_recovery_key:
            raise RecoveryKeyError("这个保险箱没有恢复密钥，请用密保问题找回")
        kek = crypto.recovery_kek(recovery_key, h.vault_id)
        try:
            key = crypto.unwrap_keys(h.wrapped_rec, kek, h.vault_id, "recovery")
        except InvalidTag:
            raise RecoveryKeyError("恢复密钥不对")
        self._set_new_password(key, new_password)
        self._save_header()
        self._record("RESET_BY_RECOVERY")

    @locked
    def set_security_questions(self, password: str, questions: list[tuple[str, str]]) -> dict:
        """设置 / 更换 3 个密保问题（要验证当前密码）。旧版本留下的恢复密钥随之作废。"""
        self._require_vault()
        self._require_stable_state()
        pairs = [(str(q), str(a)) for q, a in questions]
        Q.validate_questions(pairs)
        h = self.header
        kek = crypto.derive_kek(password, h.kdf_salt, h.kdf)
        try:
            key = crypto.unwrap_keys(h.wrapped_pw, kek, h.vault_id, "password")
        except InvalidTag:
            raise PasswordError("密码不对")
        params = {k: v for k, v in h.kdf.items() if k != "salt"}
        h.questions = self._wrap_questions(key, h.vault_id, pairs, params)
        dropped = h.has_recovery_key
        h.wrapped_rec = {}
        h.recovery_fingerprint = ""
        h.recovery_generated_at = ""
        self._save_header()
        self._record("SET_QUESTIONS", detail="旧的恢复密钥已作废" if dropped else "")
        return {"questions": h.question_texts, "dropped_recovery_key": dropped}

    # ================================================================ 解除保险箱

    def dissolve(
        self,
        password: str | None = None,
        progress: ProgressFn | None = None,
        control: OperationControl | None = None,
        *,
        recovery_key: bytes | None = None,
        answers: "list[str | None] | None" = None,
    ) -> dict:
        """解除保险箱：以后不再加密，文件夹变回普通文件夹。

        锁着的先解锁（和平时解锁完全一样，中途可以取消 / 回滚）；解锁着的要验证一次密码
        （或密保 / 旧版恢复密钥）。然后删掉 .vault 和说明文件。密码、密保随之作废。
        删的顺序：先删 header —— 从那一刻起这个文件夹就不再是保险箱；之后哪一步失败，
        剩下的也只是用不上的残留，明文文件不受影响。"""
        self._require_vault()
        self._header = None
        h = self.header
        if h.state in IN_PROGRESS:
            raise RecoveryNeededError(h.state)
        if h.state == C.LOCKED:
            self._precheck_in_use()
            self.unlock(password, progress, recovery_key, control, answers=answers)
        elif h.state != C.UNLOCKED:
            raise VaultStateError(f"当前状态不能解除：{h.state}")

        self._acquire()
        try:
            self._header = None
            h = self.header
            if h.state != C.UNLOCKED:
                raise VaultStateError("保险箱刚被别的窗口上锁了，请刷新后再试")
            self._precheck_in_use()
            try:
                self._open_key(password=password, recovery_key=recovery_key, answers=answers)
            except (PasswordError, RecoveryKeyError, AnswerError) as exc:
                self._record("DISSOLVE_FAILED", result="fail", detail=str(exc))
                raise
            stats = {"files": h.file_count, "bytes": h.total_bytes}
            self._record("DISSOLVE", detail=f"{h.file_count} 个文件，以后不再加密")
            protect.release(self.root)
            os.unlink(long_path(self.header_path))
            if self.marker_path.exists():
                os.unlink(long_path(self.marker_path))
            lock_file = self.vault_dir / "instance.lock"
            for child in list(self.vault_dir.iterdir()):
                if child == lock_file:
                    continue  # 自己还拿着它（Windows 上删不掉打开着的文件），放锁后再删
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(long_path(child), ignore_errors=True)
                else:
                    try:
                        os.unlink(long_path(child))
                    except OSError:
                        pass
        finally:
            self._release()
        self.close()
        self._flock = None
        shutil.rmtree(long_path(self.vault_dir), ignore_errors=True)
        self._emit(progress, "已解除", done_files=stats["files"], total_files=stats["files"])
        return stats

    # ================================================================ 维护

    def _cleanup_tmp(self) -> None:
        try:
            if self.tmp_dir.exists():
                shutil.rmtree(long_path(self.tmp_dir))
            fsutil.ensure_dir(self.tmp_dir, 0o700)
        except OSError:
            pass

    def _cleanup_partials(self) -> None:
        """清掉上次中断留下的半成品（*.fdv-part / *.fdv-tmp）。"""
        self._cleanup_tmp()
        root_s = str(self.root)
        for dirpath, dirnames, filenames in os.walk(root_s, topdown=True, followlinks=False):
            if os.path.abspath(dirpath) == os.path.abspath(str(self.vault_dir)):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d != C.VAULT_DIRNAME]
            for f in filenames:
                if f.endswith(C.TMP_SUFFIXES):
                    try:
                        os.unlink(long_path(os.path.join(dirpath, f)))
                    except OSError:
                        pass
