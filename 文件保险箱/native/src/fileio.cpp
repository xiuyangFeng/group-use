#include "fileio.h"

#include <cerrno>
#include <climits>
#include <string>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace fdv {

#ifdef _WIN32

namespace {

// Python 端用 utf-8 + surrogatepass 编码（WTF-8），这里解回 UTF-16，单独的代理项也能还原
bool wtf8_to_wide(const char* s, std::wstring& out) {
    out.clear();
    const unsigned char* p = reinterpret_cast<const unsigned char*>(s);
    while (*p) {
        uint32_t c = *p;
        int extra;
        if (c < 0x80) {
            extra = 0;
        } else if ((c & 0xE0) == 0xC0) {
            c &= 0x1F;
            extra = 1;
        } else if ((c & 0xF0) == 0xE0) {
            c &= 0x0F;
            extra = 2;
        } else if ((c & 0xF8) == 0xF0) {
            c &= 0x07;
            extra = 3;
        } else {
            return false;
        }
        ++p;
        for (int i = 0; i < extra; ++i) {
            if ((*p & 0xC0) != 0x80) return false;
            c = (c << 6) | (*p & 0x3F);
            ++p;
        }
        if (c >= 0x10000) {
            c -= 0x10000;
            out.push_back(wchar_t(0xD800 + (c >> 10)));
            out.push_back(wchar_t(0xDC00 + (c & 0x3FF)));
        } else {
            out.push_back(wchar_t(c));
        }
    }
    return true;
}

// 每个线程一个手动复位事件，给重叠 I/O 等待用
struct ThreadEvent {
    HANDLE ev = nullptr;
    ~ThreadEvent() {
        if (ev) CloseHandle(ev);
    }
    HANDLE get() {
        if (!ev) ev = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        return ev;
    }
};
thread_local ThreadEvent t_event;

HANDLE H(fdv_handle_t h) { return h ? reinterpret_cast<HANDLE>(h) : INVALID_HANDLE_VALUE; }

constexpr DWORD MAX_IO = 1u << 30;  // 单次 ReadFile/WriteFile 不超过 1 GB

}  // namespace

File::~File() { close(); }

bool File::is_open() const { return h_ != nullptr; }

bool File::open_read(const char* path, IoError& err) {
    close();
    std::wstring w;
    if (!wtf8_to_wide(path, w)) {
        err.code = ERROR_INVALID_NAME;
        return false;
    }
    // 共享方式和 Python open() 一致（允许别人同时读写）
    HANDLE h = CreateFileW(w.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
                           FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED | FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (h == INVALID_HANDLE_VALUE) {
        err.code = int(GetLastError());
        return false;
    }
    h_ = h;
    return true;
}

bool File::create_write(const char* path, IoError& err) {
    close();
    std::wstring w;
    if (!wtf8_to_wide(path, w)) {
        err.code = ERROR_INVALID_NAME;
        return false;
    }
    HANDLE h = CreateFileW(w.c_str(), GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                           CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED, nullptr);
    if (h == INVALID_HANDLE_VALUE) {
        err.code = int(GetLastError());
        return false;
    }
    h_ = h;
    return true;
}

bool File::read_at(void* buf, size_t len, uint64_t off, size_t* got, IoError& err) {
    size_t total = 0;
    uint8_t* p = static_cast<uint8_t*>(buf);
    while (total < len) {
        DWORD want = DWORD((len - total) > MAX_IO ? MAX_IO : (len - total));
        OVERLAPPED ov = {};
        uint64_t pos = off + total;
        ov.Offset = DWORD(pos & 0xFFFFFFFFu);
        ov.OffsetHigh = DWORD(pos >> 32);
        ov.hEvent = t_event.get();
        DWORD n = 0;
        BOOL ok = ReadFile(H(h_), p + total, want, nullptr, &ov);
        if (!ok) {
            DWORD e = GetLastError();
            if (e == ERROR_IO_PENDING) {
                ok = GetOverlappedResult(H(h_), &ov, &n, TRUE);
                if (!ok) e = GetLastError();
            }
            if (!ok) {
                if (e == ERROR_HANDLE_EOF) break;
                err.code = int(e);
                return false;
            }
        } else if (!GetOverlappedResult(H(h_), &ov, &n, TRUE)) {
            DWORD e = GetLastError();
            if (e == ERROR_HANDLE_EOF) break;
            err.code = int(e);
            return false;
        }
        if (n == 0) break;
        total += n;
    }
    *got = total;
    return true;
}

bool File::write_at(const void* buf, size_t len, uint64_t off, IoError& err) {
    size_t total = 0;
    const uint8_t* p = static_cast<const uint8_t*>(buf);
    while (total < len) {
        DWORD want = DWORD((len - total) > MAX_IO ? MAX_IO : (len - total));
        OVERLAPPED ov = {};
        uint64_t pos = off + total;
        ov.Offset = DWORD(pos & 0xFFFFFFFFu);
        ov.OffsetHigh = DWORD(pos >> 32);
        ov.hEvent = t_event.get();
        DWORD n = 0;
        BOOL ok = WriteFile(H(h_), p + total, want, nullptr, &ov);
        if (!ok) {
            DWORD e = GetLastError();
            if (e == ERROR_IO_PENDING) {
                ok = GetOverlappedResult(H(h_), &ov, &n, TRUE);
                if (!ok) e = GetLastError();
            }
            if (!ok) {
                err.code = int(e);
                return false;
            }
        } else if (!GetOverlappedResult(H(h_), &ov, &n, TRUE)) {
            err.code = int(GetLastError());
            return false;
        }
        if (n == 0) {
            err.code = ERROR_WRITE_FAULT;
            return false;
        }
        total += n;
    }
    return true;
}

bool File::size(uint64_t* out, IoError& err) {
    LARGE_INTEGER li;
    if (!GetFileSizeEx(H(h_), &li)) {
        err.code = int(GetLastError());
        return false;
    }
    *out = uint64_t(li.QuadPart);
    return true;
}

bool File::sync(IoError& err) {
    if (!FlushFileBuffers(H(h_))) {
        err.code = int(GetLastError());
        return false;
    }
    return true;
}

void File::close() {
    if (h_) {
        CloseHandle(H(h_));
        h_ = nullptr;
    }
}

bool remove_file(const char* path) {
    std::wstring w;
    if (!wtf8_to_wide(path, w)) return false;
    return DeleteFileW(w.c_str()) != 0;
}

bool stat_fingerprint(const char* path, uint64_t* size, uint64_t fp[4], IoError& err) {
    std::wstring w;
    if (!wtf8_to_wide(path, w)) {
        err.code = ERROR_INVALID_NAME;
        return false;
    }
    // 只要属性访问：不参与共享检查，不会和别人打开的句柄冲突，也不触发杀毒扫描
    HANDLE h = CreateFileW(w.c_str(), FILE_READ_ATTRIBUTES, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           nullptr, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (h == INVALID_HANDLE_VALUE) {
        err.code = int(GetLastError());
        return false;
    }
    BY_HANDLE_FILE_INFORMATION bi;
    FILE_BASIC_INFO fb;
    bool ok = GetFileInformationByHandle(h, &bi) &&
              GetFileInformationByHandleEx(h, FileBasicInfo, &fb, sizeof(fb));
    if (!ok) err.code = int(GetLastError());
    CloseHandle(h);
    if (!ok) return false;
    *size = (uint64_t(bi.nFileSizeHigh) << 32) | bi.nFileSizeLow;
    fp[0] = bi.dwVolumeSerialNumber;
    fp[1] = (uint64_t(bi.nFileIndexHigh) << 32) | bi.nFileIndexLow;
    fp[2] = uint64_t(fb.LastWriteTime.QuadPart);
    fp[3] = uint64_t(fb.ChangeTime.QuadPart);
    return true;
}

#else  // POSIX

File::~File() { close(); }

bool File::is_open() const { return fd_ >= 0; }

bool File::open_read(const char* path, IoError& err) {
    close();
    int fd;
    do {
        fd = ::open(path, O_RDONLY | O_CLOEXEC);
    } while (fd < 0 && errno == EINTR);
    if (fd < 0) {
        err.code = errno;
        return false;
    }
    fd_ = fd;
    return true;
}

bool File::create_write(const char* path, IoError& err) {
    close();
    int fd;
    do {
        fd = ::open(path, O_RDWR | O_CREAT | O_TRUNC | O_CLOEXEC, 0666);
    } while (fd < 0 && errno == EINTR);
    if (fd < 0) {
        err.code = errno;
        return false;
    }
    fd_ = fd;
    return true;
}

bool File::read_at(void* buf, size_t len, uint64_t off, size_t* got, IoError& err) {
    size_t total = 0;
    uint8_t* p = static_cast<uint8_t*>(buf);
    while (total < len) {
        size_t want = len - total;
        if (want > (size_t(1) << 30)) want = size_t(1) << 30;
        ssize_t n = ::pread(fd_, p + total, want, off_t(off + total));
        if (n < 0) {
            if (errno == EINTR) continue;
            err.code = errno;
            return false;
        }
        if (n == 0) break;
        total += size_t(n);
    }
    *got = total;
    return true;
}

bool File::write_at(const void* buf, size_t len, uint64_t off, IoError& err) {
    size_t total = 0;
    const uint8_t* p = static_cast<const uint8_t*>(buf);
    while (total < len) {
        size_t want = len - total;
        if (want > (size_t(1) << 30)) want = size_t(1) << 30;
        ssize_t n = ::pwrite(fd_, p + total, want, off_t(off + total));
        if (n < 0) {
            if (errno == EINTR) continue;
            err.code = errno;
            return false;
        }
        if (n == 0) {
            err.code = EIO;
            return false;
        }
        total += size_t(n);
    }
    return true;
}

bool File::size(uint64_t* out, IoError& err) {
    struct stat st;
    if (::fstat(fd_, &st) != 0) {
        err.code = errno;
        return false;
    }
    *out = uint64_t(st.st_size);
    return true;
}

bool File::sync(IoError& err) {
    // 与 Python os.fsync 相同（macOS 上是 fsync，不是 F_FULLFSYNC）
    int r;
    do {
        r = ::fsync(fd_);
    } while (r != 0 && errno == EINTR);
    if (r != 0) {
        err.code = errno;
        return false;
    }
    return true;
}

void File::close() {
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }
}

bool remove_file(const char* path) { return ::unlink(path) == 0; }

bool stat_fingerprint(const char* path, uint64_t* size, uint64_t fp[4], IoError& err) {
    struct stat st;
    if (::lstat(path, &st) != 0) {
        err.code = errno;
        return false;
    }
#if defined(__APPLE__)
    const struct timespec& mt = st.st_mtimespec;
    const struct timespec& ct = st.st_ctimespec;
#else
    const struct timespec& mt = st.st_mtim;
    const struct timespec& ct = st.st_ctim;
#endif
    *size = uint64_t(st.st_size);
    fp[0] = uint64_t(st.st_dev);
    fp[1] = uint64_t(st.st_ino);
    fp[2] = uint64_t(mt.tv_sec) * 1000000000ULL + uint64_t(mt.tv_nsec);
    fp[3] = uint64_t(ct.tv_sec) * 1000000000ULL + uint64_t(ct.tv_nsec);
    return true;
}

#endif

}  // namespace fdv
