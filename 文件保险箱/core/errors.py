"""异常类型。CLI / GUI 根据这些类型给出人话提示。"""


class VaultError(Exception):
    """所有保险箱错误的基类。"""


class NotAVaultError(VaultError):
    """目标文件夹不是保险箱（或 .vault 结构损坏）。"""


class AlreadyVaultError(VaultError):
    """目标文件夹已经是保险箱。"""


class PasswordError(VaultError):
    """密码错误。"""


class RecoveryKeyError(VaultError):
    """恢复密钥错误。"""


class VaultStateError(VaultError):
    """当前状态不允许这个操作（例如没解锁就上锁）。"""


class RecoveryNeededError(VaultStateError):
    """上次操作被中断，需要先执行恢复。"""

    def __init__(self, state: str):
        self.state = state
        super().__init__(f"上次操作没有完成（状态 {state}），需要先恢复")


class InUseError(VaultStateError):
    """外部程序（如 CT 脱敏工具）正在使用保险箱里的文件，现在不能上锁。"""


class IntegrityError(VaultError):
    """数据完整性校验失败 / 文件缺失。"""


class DiskSpaceError(VaultError):
    """磁盘剩余空间不足。"""


class Cancelled(VaultError):
    """用户取消。取消后可调用 recover(rollback=True) 回滚到操作前状态。"""


class ScanError(VaultError):
    """扫描文件夹时出错（符号链接、特殊文件、权限等）。"""
