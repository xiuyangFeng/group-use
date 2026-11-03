"""文件保险箱 —— 核心常量与格式定义。

修改格式相关常量时必须同时升级 FORMAT_VERSION，并在 core/header.py 中处理旧版本。
"""

FORMAT = "fdv1"
FORMAT_VERSION = 1

# 保险箱内部目录（位于用户选择的文件夹根目录下）
VAULT_DIRNAME = ".vault"
HEADER_NAME = "header.json"
MANIFEST_NAME = "manifest.db"
JOURNAL_NAME = "journal.jsonl"
BLOBS_DIRNAME = "blobs"
TMP_DIRNAME = "tmp"

# 给同事看的说明文件（不含敏感信息，不参与加密）
MARKER_NAME = "如何解锁.txt"
MARKER_TEXT = """这个文件夹已经用「文件保险箱」上锁。

里面的 .vault 文件夹是加密数据，删掉它等于删掉数据，请不要动。

需要用的时候：
1. 打开「文件保险箱」软件；
2. 选中这个文件夹；
3. 输入密码解锁（或点「用恢复密钥解锁」）。

如果你不是这个文件夹的主人，请联系它的主人，不要尝试删除或移动里面的东西。
"""

# 外部程序"正在使用"标记（见 core/inuse.py）：在 .vault 里，不加密
INUSE_GLOB = "in-use-*.json"
INUSE_STALE_SECONDS = 180      # 超过 3 分钟没刷新心跳，视为外部程序已不在
INUSE_CLOCK_SKEW_SECONDS = 60

# 临时文件后缀（扫描时跳过）
TMP_SUFFIXES = (".fdv-part", ".fdv-tmp")

DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB
MIN_PASSWORD_LEN = 8

# 磁盘空间预留
SPACE_RESERVE_BYTES = 32 * 1024 * 1024

# 密码连续输错后的延迟（防止有人守着界面暴力猜；真正的防线仍是 Argon2 的耗时）
MAX_FREE_ATTEMPTS = 5          # 前 5 次不额外等待
THROTTLE_BASE_SECONDS = 10     # 第 6 次起每次至少等 10 秒
THROTTLE_MAX_SECONDS = 60      # 最多等 60 秒

# 文件在 manifest 中的状态
ST_OPEN = "OPEN"              # 已解锁：明文存在且校验通过
ST_SEALED = "SEALED"          # 已上锁：密文存在且校验通过
ST_PENDING_UNLOCK = "PENDING_UNLOCK"  # 正在解锁
ST_PENDING_LOCK = "PENDING_LOCK"      # 正在上锁
ST_DIR = "DIR"                # 目录条目

ALL_STATES = {ST_OPEN, ST_SEALED, ST_PENDING_UNLOCK, ST_PENDING_LOCK, ST_DIR}

# header 状态
UNLOCKING = "UNLOCKING"   # 正在解锁
LOCKING = "LOCKING"       # 正在上锁
UNLOCKED = "UNLOCKED"     # 已解锁：明文可见
LOCKED = "LOCKED"         # 已上锁：只有密文
INCONSISTENT = "INCONSISTENT"
