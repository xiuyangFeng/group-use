"""manifest.db：SQLite 元数据。文件名在这里是密文，明文文件名不会出现在磁盘上。"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import constants as C
from . import crypto


@dataclass
class FileEntry:
    uuid: bytes
    path: str
    is_dir: bool
    size: int
    mtime_ns: int
    mode: int
    sha256: bytes
    state: str

    @property
    def uuid_hex(self) -> str:
        return self.uuid.hex()


SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid      BLOB NOT NULL UNIQUE,
    path_ct   BLOB NOT NULL,
    path_nonce BLOB NOT NULL,
    is_dir    INTEGER NOT NULL,
    size      INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    mode      INTEGER NOT NULL,
    sha256    BLOB NOT NULL,
    state     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_state ON files(state);
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Manifest:
    """一个保险箱的元数据表。打开前必须已经拿到 vault key（用于加解密路径）。"""

    def __init__(self, db_path: str | Path, vault_key: bytes):
        self.path = Path(db_path)
        self._key = vault_key
        self._name_key = crypto.name_key(vault_key)
        self._conn: sqlite3.Connection | None = None

    # ---------------------------------------------------------------- 生命周期

    def open(self) -> "Manifest":
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.executescript(SCHEMA)
        conn.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._conn = conn
        return self

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.commit()
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Manifest":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Manifest 尚未打开")
        return self._conn

    def commit(self) -> None:
        self.conn.commit()

    # ---------------------------------------------------------------- 路径加解密

    def _enc_path(self, path: str, entry_uuid: bytes) -> tuple[bytes, bytes]:
        nonce, ct = crypto.aead_seal(
            self._name_key,
            path.encode("utf-8"),
            b"fdv1|name|" + entry_uuid,
        )
        return ct, nonce

    def _dec_path(self, ct: bytes, nonce: bytes, entry_uuid: bytes) -> str:
        raw = crypto.aead_open(self._name_key, nonce, ct, b"fdv1|name|" + entry_uuid)
        return raw.decode("utf-8")

    # ---------------------------------------------------------------- 写

    def insert(self, e: FileEntry) -> None:
        ct, nonce = self._enc_path(e.path, e.uuid)
        self.conn.execute(
            "INSERT INTO files(uuid,path_ct,path_nonce,is_dir,size,mtime_ns,mode,sha256,state) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (e.uuid, ct, nonce, int(e.is_dir), e.size, e.mtime_ns, e.mode, e.sha256, e.state),
        )

    def insert_many(self, entries: list[FileEntry]) -> None:
        rows = []
        for e in entries:
            ct, nonce = self._enc_path(e.path, e.uuid)
            rows.append((e.uuid, ct, nonce, int(e.is_dir), e.size, e.mtime_ns, e.mode, e.sha256, e.state))
        self.conn.executemany(
            "INSERT INTO files(uuid,path_ct,path_nonce,is_dir,size,mtime_ns,mode,sha256,state) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            rows,
        )

    def update(self, e: FileEntry) -> None:
        ct, nonce = self._enc_path(e.path, e.uuid)
        self.conn.execute(
            "UPDATE files SET path_ct=?, path_nonce=?, is_dir=?, size=?, mtime_ns=?, mode=?, "
            "sha256=?, state=? WHERE uuid=?",
            (ct, nonce, int(e.is_dir), e.size, e.mtime_ns, e.mode, e.sha256, e.state, e.uuid),
        )

    def set_state(self, entry_uuid: bytes, state: str) -> None:
        self.conn.execute("UPDATE files SET state=? WHERE uuid=?", (state, entry_uuid))

    def delete(self, entry_uuid: bytes) -> None:
        self.conn.execute("DELETE FROM files WHERE uuid=?", (entry_uuid,))

    def delete_many(self, uuids: list[bytes]) -> None:
        self.conn.executemany("DELETE FROM files WHERE uuid=?", [(u,) for u in uuids])

    # ---------------------------------------------------------------- 读

    def load_all(self) -> list[FileEntry]:
        rows = self.conn.execute(
            "SELECT uuid,path_ct,path_nonce,is_dir,size,mtime_ns,mode,sha256,state FROM files "
            "ORDER BY is_dir DESC, id"
        ).fetchall()
        out: list[FileEntry] = []
        for row in rows:
            u = bytes(row[0])
            out.append(
                FileEntry(
                    uuid=u,
                    path=self._dec_path(bytes(row[1]), bytes(row[2]), u),
                    is_dir=bool(row[3]),
                    size=int(row[4]),
                    mtime_ns=int(row[5]),
                    mode=int(row[6]),
                    sha256=bytes(row[7]),
                    state=str(row[8]),
                )
            )
        return out

    def count(self) -> tuple[int, int, int]:
        """返回 (文件数, 目录数, 总字节)。"""
        row = self.conn.execute(
            "SELECT SUM(CASE WHEN is_dir=0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN is_dir=1 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN is_dir=0 THEN size ELSE 0 END) FROM files"
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
