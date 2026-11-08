/* 文件保险箱 C++ 加解密引擎 —— C 接口（Python 用 ctypes 调用）。
 *
 * 密文格式与 core/transform.py 完全一致（FDB1）：
 *   magic "FDB1" | version(1) | chunk_size(u32 LE) | plaintext_size(u64 LE) | chunk_count(u64 LE)
 *   | chunk_0 | chunk_1 | ...       每块 = ChaCha20-Poly1305(明文块) || tag(16)
 *   nonce = 0x00000000 || 块号(u64 BE)
 *   AAD   = "fdv1|chunk|" || 文件 uuid(16) || 块号(u64 BE) || 是否末块(1)
 *
 * fdv_run 一次处理一批文件：多文件并行，大文件内部按块并行（读 / 加解密 / 写同时进行），
 * 只有明文 SHA-256 按顺序计算。
 */
#ifndef FDV_NATIVE_H
#define FDV_NATIVE_H

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#define FDV_API __declspec(dllexport)
#else
#define FDV_API __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define FDV_ABI_VERSION 2

enum fdv_op {
    FDV_OP_ENCRYPT = 1, /* src 明文 -> dst 密文（dst 新建/截断，fsync） */
    FDV_OP_DECRYPT = 2, /* src 密文 -> dst 明文（校验 tag + 可选 sha256，fsync；失败时删掉 dst） */
    FDV_OP_VERIFY = 3,  /* 只解密校验，不落盘 */
    FDV_OP_HASH = 4,    /* 计算 src 的 sha256 */
    FDV_OP_STAT = 5     /* 文件指纹：out_size=大小，out_sha 依次放 4 个 u64 LE：
                           设备号、inode / 文件编号、修改时间、状态改变时间（ctime / ChangeTime）。
                           内容或属性一改，状态改变时间就会变，普通程序改不回去 */
};

enum fdv_status {
    FDV_OK = 0,
    FDV_E_IO = 1,        /* sys_error = errno / GetLastError，where = 出错的步骤 */
    FDV_E_CANCELLED = 2, /* 用户取消 */
    FDV_E_SKIPPED = 3,   /* 前面有文件出错，后面的没做 */
    FDV_E_NOMEM = 4,
    FDV_E_HDR_INCOMPLETE = 10, /* 密文文件头不完整 */
    FDV_E_BAD_FORMAT = 11,     /* 密文格式不对 */
    FDV_E_BAD_CHUNK = 12,      /* 块大小不合法 */
    FDV_E_SIZE_MISMATCH = 13,  /* 密文大小与记录不一致 */
    FDV_E_COUNT = 14,          /* 密文块数不对 */
    FDV_E_TRUNCATED = 15,      /* 密文被截断 */
    FDV_E_TAG = 16,            /* 认证失败（损坏或篡改） */
    FDV_E_TRAILING = 17,       /* 密文尾部有多余数据 */
    FDV_E_HASH = 18,           /* 解密后哈希与记录不一致 */
    FDV_E_SRC_SHRANK = 19,     /* 加密时读取源文件意外结束 */
    FDV_E_ENC_SIZE = 20        /* 加密结果大小不对 */
};

enum fdv_where {
    FDV_W_NONE = 0,
    FDV_W_OPEN_SRC = 1,
    FDV_W_OPEN_DST = 2,
    FDV_W_READ = 3,
    FDV_W_WRITE = 4,
    FDV_W_SYNC = 5,
    FDV_W_STAT = 6
};

typedef struct fdv_job {
    /* 输入 */
    const char* src;          /* UTF-8（Windows 为 WTF-8） */
    const char* dst;          /* ENCRYPT/DECRYPT 需要；其它为 NULL */
    uint8_t key[32];          /* 文件子密钥 */
    uint8_t uuid[16];         /* 文件 uuid（进 AAD） */
    uint8_t expected_sha[32]; /* DECRYPT：has_expected_sha 时校验 */
    int64_t expected_size;    /* DECRYPT/VERIFY：>=0 时校验明文大小；ENCRYPT：大小提示（排序用） */
    uint32_t chunk_size;      /* ENCRYPT：分块大小 */
    int32_t has_expected_sha;
    /* 输出 */
    int64_t out_size;     /* 明文大小 */
    uint8_t out_sha[32];  /* 明文 sha256 */
    int32_t status;       /* fdv_status */
    int32_t sys_error;    /* FDV_E_IO 时的系统错误码 */
    int32_t where;        /* fdv_where */
    int32_t reserved;
} fdv_job;

/* 进度回调：只在调用 fdv_run 的线程上调用（约每 50ms 一次，结束时再调一次）。
 * 返回 0 继续，1 取消，2 暂停（之后照常回调，返回 0 即继续）。 */
typedef int (*fdv_progress_fn)(uint64_t done_bytes, uint64_t done_files, void* user);

/* threads <= 0 表示用全部逻辑核。stop_on_error：一个文件出错后不再开始新文件。
 * 返回 0 全部成功，1 有文件失败，2 已取消，-1 参数错误。 */
FDV_API int fdv_run(int op, fdv_job* jobs, size_t njobs, int threads, int stop_on_error, fdv_progress_fn cb,
                    void* user);

FDV_API int fdv_abi_version(void);
FDV_API size_t fdv_job_size(void);
FDV_API int fdv_default_threads(void);
FDV_API const char* fdv_version(void);
FDV_API const char* fdv_impl_info(void); /* 例如 "chacha20=neon poly1305=64bit sha256=armv8-sha2" */

/* 自检（已知答案 + SIMD 与可移植实现交叉比对）。返回 0 通过；不通过的 SIMD 实现会被自动停用。 */
FDV_API int fdv_selftest(char* report, size_t report_len);

/* 单块原语，给测试用 */
FDV_API void fdv_aead_seal(const uint8_t key[32], const uint8_t nonce[12], const uint8_t* aad, size_t aadlen,
                           uint8_t* buf, size_t len, uint8_t tag[16]);
FDV_API int fdv_aead_open(const uint8_t key[32], const uint8_t nonce[12], const uint8_t* aad, size_t aadlen,
                          uint8_t* buf, size_t len, const uint8_t tag[16]);
FDV_API void fdv_sha256(const uint8_t* data, size_t len, uint8_t out[32]);
FDV_API void fdv_set_force_portable(int on);

#ifdef __cplusplus
}
#endif

#endif
