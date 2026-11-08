// 跨平台文件读写：按偏移读写（多线程可同时读写同一个文件的不同位置）。
#pragma once
#include <cstddef>
#include <cstdint>

#ifdef _WIN32
typedef void* fdv_handle_t;
#endif

namespace fdv {

struct IoError {
    int code = 0;  // POSIX errno 或 Windows GetLastError()
};

class File {
  public:
    File() = default;
    ~File();
    File(const File&) = delete;
    File& operator=(const File&) = delete;

    bool open_read(const char* utf8_path, IoError& err);
    bool create_write(const char* utf8_path, IoError& err);  // 新建 / 截断
    bool is_open() const;
    // 从 off 读 len 字节；*got 返回实际读到的（遇到文件尾会小于 len）
    bool read_at(void* buf, size_t len, uint64_t off, size_t* got, IoError& err);
    bool write_at(const void* buf, size_t len, uint64_t off, IoError& err);
    bool size(uint64_t* out, IoError& err);
    bool sync(IoError& err);
    void close();

  private:
#ifdef _WIN32
    fdv_handle_t h_ = nullptr;
#else
    int fd_ = -1;
#endif
};

bool remove_file(const char* utf8_path);

// 文件指纹（不跟随符号链接）：fp = {设备号, 文件编号, 修改时间, 状态改变时间}
bool stat_fingerprint(const char* utf8_path, uint64_t* size, uint64_t fp[4], IoError& err);

}  // namespace fdv
