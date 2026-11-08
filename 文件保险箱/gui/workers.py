"""后台线程 + 单实例控制。加密操作一律在线程里跑，UI 不卡。"""

from __future__ import annotations

import traceback
from typing import Callable

from PySide6.QtCore import QThread, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from core.control import OperationControl
from core.errors import Cancelled, VaultError
from core.vault import Vault


class VaultOpWorker(QThread):
    """执行一次 core 操作（解锁/上锁/恢复/校验/首次上锁/取消新建/解除保险箱）。"""

    progressed = Signal(dict)
    succeeded = Signal(object)
    failed = Signal(str, str)
    cancelled = Signal()

    OPS = {"unlock", "lock", "initial_lock", "cancel_create", "recover", "verify", "dissolve"}

    def __init__(
        self,
        vault: Vault,
        op: str,
        *,
        password: str | None = None,
        recovery_key: bytes | None = None,
        answers: list[str] | None = None,
        rollback: bool = False,
        control: OperationControl | None = None,
        parent=None,
    ):
        super().__init__(parent)
        if op not in self.OPS:
            raise ValueError(op)
        self.vault = vault
        self.op = op
        self.password = password
        self.recovery_key = recovery_key
        self.answers = answers
        self.rollback = rollback
        self.control = control or OperationControl()

    def _progress(self, data: dict) -> None:
        self.progressed.emit(dict(data))

    def run(self) -> None:  # noqa: D401
        try:
            if self.op == "unlock":
                result = self.vault.unlock(
                    password=self.password,
                    recovery_key=self.recovery_key,
                    answers=self.answers,
                    progress=self._progress,
                    control=self.control,
                )
            elif self.op == "lock":
                result = self.vault.lock(
                    password=self.password,
                    recovery_key=self.recovery_key,
                    answers=self.answers,
                    progress=self._progress,
                    control=self.control,
                )
            elif self.op == "dissolve":
                result = self.vault.dissolve(
                    password=self.password,
                    recovery_key=self.recovery_key,
                    answers=self.answers,
                    progress=self._progress,
                    control=self.control,
                )
            elif self.op == "initial_lock":
                result = self.vault.initial_lock(progress=self._progress, control=self.control)
            elif self.op == "cancel_create":
                result = self.vault.cancel_create(progress=self._progress)
            elif self.op == "recover":
                result = self.vault.recover(
                    password=self.password,
                    recovery_key=self.recovery_key,
                    answers=self.answers,
                    rollback=self.rollback,
                    progress=self._progress,
                    control=self.control,
                )
            else:  # verify
                result = self.vault.verify(
                    password=self.password,
                    recovery_key=self.recovery_key,
                    answers=self.answers,
                    progress=self._progress,
                    control=self.control,
                )
        except Cancelled:
            self.cancelled.emit()
        except VaultError as exc:
            self.failed.emit(type(exc).__name__, str(exc))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.failed.emit("内部错误", f"{type(exc).__name__}: {exc}")
        else:
            self.succeeded.emit(result)


def make_worker(vault: Vault, op: str, **kwargs) -> VaultOpWorker:
    return VaultOpWorker(vault, op, **kwargs)


# ---------------------------------------------------------------- 单实例

class SingleInstance:
    """防止开两个窗口同时动同一个保险箱（GUI 层）。core 里还有文件锁兜底。"""

    def __init__(self, name: str = "fdvault-gui"):
        self.name = name
        self.server: QLocalServer | None = None
        self._on_activate: Callable[[], None] | None = None

    def activate_existing(self) -> bool:
        sock = QLocalSocket()
        sock.connectToServer(self.name)
        if not sock.waitForConnected(300):
            return False
        sock.write(b"activate")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True

    def start_server(self, on_activate: Callable[[], None]) -> bool:
        QLocalServer.removeServer(self.name)  # 清掉上次崩溃留下的
        self.server = QLocalServer()
        if not self.server.listen(self.name):
            return False
        self._on_activate = on_activate
        self.server.newConnection.connect(self._handle)
        return True

    def _handle(self) -> None:
        while self.server is not None and self.server.hasPendingConnections():
            conn = self.server.nextPendingConnection()
            if conn is None:
                continue
            # 连接本身就代表“第二个实例来了”，立刻唤起；不要等 readyRead——
            # Windows 命名管道下客户端可能已经写完后断开，readyRead 不会再触发。
            conn.readyRead.connect(conn.readAll)
            conn.disconnected.connect(conn.deleteLater)
            if self._on_activate is not None:
                self._on_activate()
