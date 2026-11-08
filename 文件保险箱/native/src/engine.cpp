// 批量文件加解密引擎。
//
// 调度模型（一把全局互斥锁 + 条件变量；工作单位是「块」，锁开销相对 64KB~4MB 的块可以忽略）：
//   小文件（≤1 块）：一个工作线程从头做到尾（读 → 哈希 → 加密 → 写 → fsync）。
//   大文件：拆成三段流水线，所有空闲线程都可以参与——
//     P1  读块（解密 / 校验时顺带认证 + 解密）        可并行
//     S   按顺序更新明文 SHA-256                    每个文件同一时刻只有一个线程
//     P2  加密 + 写出 / 写出明文（校验没有这一步）       可并行，按块号顺序领取，
//         所以写盘基本是顺序的（乱序写新文件在 APFS/NTFS 上明显更慢）
//   同时在途的块受内存预算和每文件环形窗口（64 块）限制。
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>

#include "aead.h"
#include "engine.h"
#include "fdv_native.h"
#include "fileio.h"
#include "platform.h"
#include "sha256.h"

namespace fdv {

namespace {

constexpr size_t HDR = 25;  // struct "<4sBIQQ"
constexpr size_t TAG = 16;
constexpr size_t RING = 64;
constexpr uint8_t VERSION = 1;
constexpr size_t DEFAULT_UNIT = 4u * 1024 * 1024;

void nonce_for(uint8_t n[12], uint64_t idx) {
    std::memset(n, 0, 4);
    store64_be(n + 4, idx);
}

void aad_for(uint8_t a[36], const uint8_t uuid[16], uint64_t idx, bool final) {
    std::memcpy(a, "fdv1|chunk|", 11);
    std::memcpy(a + 11, uuid, 16);
    store64_be(a + 27, idx);
    a[35] = final ? 1 : 0;
}

void seal_chunk(const fdv_job& j, uint64_t idx, bool final, uint8_t* buf, size_t len) {
    uint8_t nonce[12], aad[36];
    nonce_for(nonce, idx);
    aad_for(aad, j.uuid, idx, final);
    aead_seal(j.key, nonce, aad, sizeof(aad), buf, len, buf + len);
}

bool open_chunk(const fdv_job& j, uint64_t idx, bool final, uint8_t* buf, size_t len) {
    uint8_t nonce[12], aad[36];
    nonce_for(nonce, idx);
    aad_for(aad, j.uuid, idx, final);
    return aead_open(j.key, nonce, aad, sizeof(aad), buf, len, buf + len);
}

void write_header(uint8_t* p, uint32_t chunk, uint64_t plain, uint64_t count) {
    std::memcpy(p, "FDB1", 4);
    p[4] = VERSION;
    store32_le(p + 5, chunk);
    store64_le(p + 9, plain);
    store64_le(p + 17, count);
}

struct Res {
    int st = FDV_OK;
    int sys = 0;
    int where = FDV_W_NONE;
    static Res io(int w, const IoError& e) {
        Res r;
        r.st = FDV_E_IO;
        r.sys = e.code;
        r.where = w;
        return r;
    }
    static Res of(int st) {
        Res r;
        r.st = st;
        return r;
    }
    bool ok() const { return st == FDV_OK; }
};

struct Buf {
    uint8_t* p = nullptr;
    size_t cap = 0;
};

enum SlotState : uint8_t { S_FREE = 0, S_P1, S_P1DONE, S_P2, S_DONE };

struct Slot {
    Buf buf;
    uint32_t len = 0;
    uint8_t state = S_FREE;
};

struct Task {
    fdv_job* job = nullptr;
    File src, dst;
    bool dst_created = false;
    uint64_t plain = 0, n = 0;
    uint32_t chunk = 0;
    Sha256 sha;
    uint64_t next_p1 = 0, hashed = 0, next_p2 = 0, window = 0, completed = 0;
    bool hash_busy = false, finalizing = false;
    int inflight = 0;
    Res err;  // 只在持锁时读写
    Slot slots[RING];

    Slot& slot(uint64_t i) { return slots[i % RING]; }
    uint32_t plen(uint64_t i) const {
        return i + 1 < n ? chunk : uint32_t(plain - uint64_t(chunk) * (n - 1));
    }
    size_t slot_bytes() const { return size_t(chunk < plain ? chunk : plain) + TAG; }
};

enum WorkKind { W_WAIT, W_EXIT, W_OPEN, W_P1, W_HASH, W_P2, W_FINAL };

struct Work {
    WorkKind kind = W_WAIT;
    Task* t = nullptr;
    uint64_t i = 0;
    size_t job = 0;
    Res res;
    Task* opened = nullptr;
};

class Engine {
  public:
    Engine(int op, fdv_job* jobs, size_t njobs, int threads, bool stop_on_error)
        : op_(op), jobs_(jobs), njobs_(njobs), threads_(threads), stop_on_error_(stop_on_error) {}

    ~Engine() {
        for (Buf& b : pool_) std::free(b.p);
    }

    int run(fdv_progress_fn cb, void* user);

  private:
    void worker();
    Work pick();
    void complete(Work& w);
    void mark_done(Task* t, uint64_t i);
    void release_slots(Task* t);
    Buf acquire(size_t need);
    void release(Buf b);
    void fail(Task* t, const Res& r);

    Task* do_open(size_t idx, std::vector<uint8_t>& scratch);
    Res do_p1(Task* t, uint64_t i);
    Res do_p2(Task* t, uint64_t i);
    void do_final(Task* t);

    Res inline_hash(fdv_job& j, File& src, std::vector<uint8_t>& scratch);
    Res inline_encrypt(fdv_job& j, File& src, uint64_t size, uint32_t chunk, uint64_t n, std::vector<uint8_t>& s);
    Res inline_decrypt(fdv_job& j, File& src, uint64_t plain, uint64_t n, std::vector<uint8_t>& s);
    void finish_job(fdv_job& j, const Res& r);

    const int op_;
    fdv_job* const jobs_;
    const size_t njobs_;
    int threads_;
    const bool stop_on_error_;

    std::mutex mu_;
    std::condition_variable cv_;
    std::condition_variable cv_done_;
    std::vector<size_t> order_;
    size_t next_job_ = 0;
    std::vector<Task*> active_;
    size_t opening_ = 0;
    size_t max_active_ = 1;
    int alive_ = 0;
    bool stopping_ = false;
    std::atomic<bool> cancelled_{false};  // 工作线程在锁外也会读
    bool paused_ = false;
    size_t mem_inflight_ = 0;
    size_t mem_budget_ = 0;
    std::vector<Buf> pool_;
    std::atomic<uint64_t> done_bytes_{0};
    std::atomic<uint64_t> done_files_{0};
};

// ---------------------------------------------------------------- 内存池

Buf Engine::acquire(size_t need) {
    for (size_t k = pool_.size(); k-- > 0;) {
        if (pool_[k].cap >= need && pool_[k].cap <= need * 2) {
            Buf b = pool_[k];
            pool_[k] = pool_.back();
            pool_.pop_back();
            return b;
        }
    }
    Buf b;
    b.p = static_cast<uint8_t*>(std::malloc(need));
    b.cap = b.p ? need : 0;
    return b;
}

void Engine::release(Buf b) {
    if (b.p) pool_.push_back(b);
}

void Engine::release_slots(Task* t) {
    for (Slot& s : t->slots) {
        if (s.buf.p) {
            mem_inflight_ -= s.buf.cap;
            release(s.buf);
            s.buf = Buf();
        }
    }
}

void Engine::fail(Task* t, const Res& r) {
    if (t->err.ok()) t->err = r;
    if (stop_on_error_) stopping_ = true;
}

void Engine::mark_done(Task* t, uint64_t i) {
    Slot& s = t->slot(i);
    mem_inflight_ -= s.buf.cap;
    release(s.buf);
    s.buf = Buf();
    s.state = S_DONE;
    t->completed++;
    while (t->window < t->next_p1 && t->slot(t->window).state == S_DONE) {
        t->slot(t->window).state = S_FREE;
        t->window++;
    }
}

// ---------------------------------------------------------------- 调度

Work Engine::pick() {
    Work w;
    // 1. 可以收尾的文件（全部块完成 / 出错 / 正在停止），且没有线程还在处理它
    for (Task* t : active_) {
        if (t->finalizing || t->inflight) continue;
        if (t->completed == t->n || !t->err.ok() || stopping_) {
            t->finalizing = true;
            release_slots(t);
            w.kind = W_FINAL;
            w.t = t;
            return w;
        }
    }
    if (stopping_) {
        w.kind = (active_.empty() && opening_ == 0) ? W_EXIT : W_WAIT;
        return w;
    }
    if (paused_) return w;  // W_WAIT

    // 2. 顺序哈希（每个文件的串行瓶颈，优先做）
    for (Task* t : active_) {
        if (t->finalizing || !t->err.ok() || t->hash_busy) continue;
        if (t->hashed < t->next_p1 && t->slot(t->hashed).state == S_P1DONE) {
            t->hash_busy = true;
            t->inflight++;
            w.kind = W_HASH;
            w.t = t;
            w.i = t->hashed;
            return w;
        }
    }
    // 3. 加密 / 写出（释放内存，次优先）
    if (op_ == FDV_OP_ENCRYPT || op_ == FDV_OP_DECRYPT) {
        for (Task* t : active_) {
            if (t->finalizing || !t->err.ok()) continue;
            if (t->next_p2 < t->hashed) {
                uint64_t i = t->next_p2++;
                t->slot(i).state = S_P2;
                t->inflight++;
                w.kind = W_P2;
                w.t = t;
                w.i = i;
                return w;
            }
        }
    }
    // 4. 读新块
    for (Task* t : active_) {
        if (t->finalizing || !t->err.ok()) continue;
        if (t->next_p1 < t->n && t->next_p1 - t->window < RING) {
            size_t need = t->slot_bytes();
            if (mem_inflight_ > 0 && mem_inflight_ + need > mem_budget_) continue;
            Buf b = acquire(need);
            if (!b.p) {
                fail(t, Res::of(FDV_E_NOMEM));
                continue;
            }
            uint64_t i = t->next_p1++;
            Slot& s = t->slot(i);
            s.buf = b;
            s.len = t->plen(i);
            s.state = S_P1;
            mem_inflight_ += b.cap;
            t->inflight++;
            w.kind = W_P1;
            w.t = t;
            w.i = i;
            return w;
        }
    }
    // 5. 开始新文件
    if (next_job_ < order_.size() && active_.size() + opening_ < max_active_) {
        opening_++;
        w.kind = W_OPEN;
        w.job = order_[next_job_++];
        return w;
    }
    if (next_job_ >= order_.size() && active_.empty() && opening_ == 0) w.kind = W_EXIT;
    return w;
}

void Engine::complete(Work& w) {
    switch (w.kind) {
        case W_OPEN:
            opening_--;
            if (w.opened) {
                active_.push_back(w.opened);
            } else {
                fdv_job& j = jobs_[w.job];
                if (j.status == FDV_OK) {
                    done_files_++;
                } else if (stop_on_error_ && j.status != FDV_E_CANCELLED && j.status != FDV_E_SKIPPED) {
                    stopping_ = true;
                }
            }
            break;
        case W_P1: {
            Task* t = w.t;
            t->inflight--;
            if (w.res.ok())
                t->slot(w.i).state = S_P1DONE;
            else
                fail(t, w.res);
            break;
        }
        case W_HASH: {
            Task* t = w.t;
            t->inflight--;
            t->hash_busy = false;
            t->hashed++;
            done_bytes_ += t->slot(w.i).len;
            if (op_ == FDV_OP_VERIFY) mark_done(t, w.i);
            break;
        }
        case W_P2: {
            Task* t = w.t;
            t->inflight--;
            if (w.res.ok())
                mark_done(t, w.i);
            else
                fail(t, w.res);
            break;
        }
        case W_FINAL: {
            Task* t = w.t;
            active_.erase(std::find(active_.begin(), active_.end(), t));
            fdv_job& j = *t->job;
            if (j.status == FDV_OK) {
                done_files_++;
            } else if (stop_on_error_ && j.status != FDV_E_CANCELLED && j.status != FDV_E_SKIPPED) {
                stopping_ = true;
            }
            delete t;
            break;
        }
        default:
            break;
    }
    cv_.notify_all();
}

void Engine::worker() {
    std::vector<uint8_t> scratch;
    std::unique_lock<std::mutex> lk(mu_);
    for (;;) {
        Work w = pick();
        if (w.kind == W_EXIT) break;
        if (w.kind == W_WAIT) {
            cv_.wait_for(lk, std::chrono::milliseconds(20));
            continue;
        }
        lk.unlock();
        switch (w.kind) {
            case W_OPEN:
                w.opened = do_open(w.job, scratch);
                break;
            case W_P1:
                w.res = do_p1(w.t, w.i);
                break;
            case W_HASH: {
                Slot& s = w.t->slot(w.i);
                w.t->sha.update(s.buf.p, s.len);
                break;
            }
            case W_P2:
                w.res = do_p2(w.t, w.i);
                break;
            case W_FINAL:
                do_final(w.t);
                break;
            default:
                break;
        }
        lk.lock();
        complete(w);
    }
    alive_--;
    cv_done_.notify_all();
}

// ---------------------------------------------------------------- 每个文件的步骤

void Engine::finish_job(fdv_job& j, const Res& r) {
    j.status = r.st;
    j.sys_error = r.sys;
    j.where = r.where;
    if (!r.ok()) {
        j.out_size = 0;
        std::memset(j.out_sha, 0, sizeof(j.out_sha));
    }
}

Res Engine::inline_hash(fdv_job& j, File& src, std::vector<uint8_t>& scratch) {
    IoError e;
    Sha256 sha;
    if (scratch.size() < DEFAULT_UNIT) scratch.resize(DEFAULT_UNIT);
    uint64_t off = 0;
    for (;;) {
        if (cancelled_) return Res::of(FDV_E_CANCELLED);
        size_t got = 0;
        if (!src.read_at(scratch.data(), scratch.size(), off, &got, e)) return Res::io(FDV_W_READ, e);
        if (got == 0) break;
        sha.update(scratch.data(), got);
        off += got;
        done_bytes_ += got;
    }
    sha.finish(j.out_sha);
    j.out_size = int64_t(off);
    return Res();
}

Res Engine::inline_encrypt(fdv_job& j, File& src, uint64_t size, uint32_t chunk, uint64_t n,
                           std::vector<uint8_t>& s) {
    (void)chunk;
    IoError e;
    size_t len = size_t(size);
    size_t total = HDR + len + (n ? TAG : 0);
    if (s.size() < total) s.resize(total);
    uint8_t* p = s.data();
    size_t got = 0;
    if (len && !src.read_at(p + HDR, len, 0, &got, e)) return Res::io(FDV_W_READ, e);
    if (got != len) return Res::of(FDV_E_SRC_SHRANK);
    Sha256 sha;
    sha.update(p + HDR, len);
    sha.finish(j.out_sha);
    if (n) seal_chunk(j, 0, true, p + HDR, len);
    write_header(p, j.chunk_size, size, n);

    File dst;
    if (!dst.create_write(j.dst, e)) return Res::io(FDV_W_OPEN_DST, e);
    Res r;
    uint64_t dsz = 0;
    if (!dst.write_at(p, total, 0, e))
        r = Res::io(FDV_W_WRITE, e);
    else if (!dst.sync(e))
        r = Res::io(FDV_W_SYNC, e);
    else if (!dst.size(&dsz, e))
        r = Res::io(FDV_W_STAT, e);
    else if (dsz != total)
        r = Res::of(FDV_E_ENC_SIZE);
    dst.close();
    if (!r.ok()) {
        remove_file(j.dst);
        return r;
    }
    j.out_size = int64_t(size);
    done_bytes_ += len;
    return r;
}

Res Engine::inline_decrypt(fdv_job& j, File& src, uint64_t plain, uint64_t n, std::vector<uint8_t>& s) {
    IoError e;
    size_t len = size_t(plain);
    size_t clen = n ? len + TAG : 0;
    if (s.size() < clen + 1) s.resize(clen + 1);
    uint8_t* p = s.data();
    size_t got = 0;
    if (clen && !src.read_at(p, clen, HDR, &got, e)) return Res::io(FDV_W_READ, e);
    if (got != clen) return Res::of(FDV_E_TRUNCATED);
    if (n && !open_chunk(j, 0, true, p, len)) return Res::of(FDV_E_TAG);
    uint8_t digest[32];
    Sha256 sha;
    sha.update(p, len);
    sha.finish(digest);
    if (j.has_expected_sha && std::memcmp(digest, j.expected_sha, 32) != 0) return Res::of(FDV_E_HASH);
    if (op_ == FDV_OP_DECRYPT) {
        File dst;
        if (!dst.create_write(j.dst, e)) return Res::io(FDV_W_OPEN_DST, e);
        Res r;
        if (len && !dst.write_at(p, len, 0, e))
            r = Res::io(FDV_W_WRITE, e);
        else if (!dst.sync(e))
            r = Res::io(FDV_W_SYNC, e);
        dst.close();
        if (!r.ok()) {
            remove_file(j.dst);
            return r;
        }
    }
    std::memcpy(j.out_sha, digest, 32);
    j.out_size = int64_t(plain);
    done_bytes_ += len;
    return Res();
}

// 打开文件、检查头部。小文件当场做完返回 nullptr；大文件返回流水线任务。
Task* Engine::do_open(size_t idx, std::vector<uint8_t>& scratch) {
    fdv_job& j = jobs_[idx];
    IoError e;
    std::unique_ptr<Task> t(new Task());
    t->job = &j;
    if (cancelled_) {
        finish_job(j, Res::of(FDV_E_CANCELLED));
        return nullptr;
    }
    if (op_ == FDV_OP_STAT) {
        uint64_t size = 0, fp[4] = {0, 0, 0, 0};
        if (!stat_fingerprint(j.src, &size, fp, e)) {
            finish_job(j, Res::io(FDV_W_STAT, e));
            return nullptr;
        }
        finish_job(j, Res());
        j.out_size = int64_t(size);
        for (int k = 0; k < 4; ++k) store64_le(j.out_sha + 8 * k, fp[k]);
        return nullptr;
    }
    if (!t->src.open_read(j.src, e)) {
        finish_job(j, Res::io(FDV_W_OPEN_SRC, e));
        return nullptr;
    }

    if (op_ == FDV_OP_HASH) {
        finish_job(j, inline_hash(j, t->src, scratch));
        return nullptr;
    }

    if (op_ == FDV_OP_ENCRYPT) {
        uint64_t size = 0;
        if (!t->src.size(&size, e)) {
            finish_job(j, Res::io(FDV_W_STAT, e));
            return nullptr;
        }
        uint32_t chunk = j.chunk_size;
        uint64_t n = (size + chunk - 1) / chunk;
        if (n <= 1) {
            finish_job(j, inline_encrypt(j, t->src, size, chunk, n, scratch));
            return nullptr;
        }
        t->plain = size;
        t->chunk = chunk;
        t->n = n;
        if (!t->dst.create_write(j.dst, e)) {
            finish_job(j, Res::io(FDV_W_OPEN_DST, e));
            return nullptr;
        }
        t->dst_created = true;
        uint8_t hdr[HDR];
        write_header(hdr, chunk, size, n);
        if (!t->dst.write_at(hdr, HDR, 0, e)) {
            t->dst.close();
            remove_file(j.dst);
            finish_job(j, Res::io(FDV_W_WRITE, e));
            return nullptr;
        }
        return t.release();
    }

    // DECRYPT / VERIFY：检查头部
    uint8_t hdr[HDR];
    size_t got = 0;
    if (!t->src.read_at(hdr, HDR, 0, &got, e)) {
        finish_job(j, Res::io(FDV_W_READ, e));
        return nullptr;
    }
    if (got != HDR) {
        finish_job(j, Res::of(FDV_E_HDR_INCOMPLETE));
        return nullptr;
    }
    if (std::memcmp(hdr, "FDB1", 4) != 0 || hdr[4] != VERSION) {
        finish_job(j, Res::of(FDV_E_BAD_FORMAT));
        return nullptr;
    }
    uint32_t chunk = load32_le(hdr + 5);
    uint64_t plain = load64_le(hdr + 9);
    uint64_t count = load64_le(hdr + 17);
    if (chunk == 0) {
        finish_job(j, Res::of(FDV_E_BAD_CHUNK));
        return nullptr;
    }
    if (j.expected_size >= 0 && plain != uint64_t(j.expected_size)) {
        finish_job(j, Res::of(FDV_E_SIZE_MISMATCH));
        return nullptr;
    }
    uint64_t expected_count = plain / chunk + (plain % chunk ? 1 : 0);
    if (count != expected_count) {
        finish_job(j, Res::of(FDV_E_COUNT));
        return nullptr;
    }
    uint64_t src_size = 0;
    if (!t->src.size(&src_size, e)) {
        finish_job(j, Res::io(FDV_W_STAT, e));
        return nullptr;
    }
    // 期望的密文总长；溢出说明头部是伪造的
    uint64_t want = HDR + plain;
    if (want < plain || count > (UINT64_MAX - want) / TAG) {
        finish_job(j, Res::of(FDV_E_TRUNCATED));
        return nullptr;
    }
    want += count * TAG;
    if (src_size < want) {
        finish_job(j, Res::of(FDV_E_TRUNCATED));
        return nullptr;
    }
    if (src_size > want) {
        finish_job(j, Res::of(FDV_E_TRAILING));
        return nullptr;
    }
    if (count <= 1) {
        finish_job(j, inline_decrypt(j, t->src, plain, count, scratch));
        return nullptr;
    }
    t->plain = plain;
    t->chunk = chunk;
    t->n = count;
    if (op_ == FDV_OP_DECRYPT) {
        if (!t->dst.create_write(j.dst, e)) {
            finish_job(j, Res::io(FDV_W_OPEN_DST, e));
            return nullptr;
        }
        t->dst_created = true;
    }
    return t.release();
}

Res Engine::do_p1(Task* t, uint64_t i) {
    const fdv_job& j = *t->job;
    Slot& s = t->slot(i);
    IoError e;
    size_t got = 0;
    if (op_ == FDV_OP_ENCRYPT) {
        if (!t->src.read_at(s.buf.p, s.len, i * t->chunk, &got, e)) return Res::io(FDV_W_READ, e);
        if (got != s.len) return Res::of(FDV_E_SRC_SHRANK);
        return Res();
    }
    size_t clen = size_t(s.len) + TAG;
    uint64_t off = HDR + i * (uint64_t(t->chunk) + TAG);
    if (!t->src.read_at(s.buf.p, clen, off, &got, e)) return Res::io(FDV_W_READ, e);
    if (got != clen) return Res::of(FDV_E_TRUNCATED);
    if (!open_chunk(j, i, i + 1 == t->n, s.buf.p, s.len)) return Res::of(FDV_E_TAG);
    return Res();
}

Res Engine::do_p2(Task* t, uint64_t i) {
    const fdv_job& j = *t->job;
    Slot& s = t->slot(i);
    IoError e;
    if (op_ == FDV_OP_DECRYPT) {
        if (!t->dst.write_at(s.buf.p, s.len, i * uint64_t(t->chunk), e)) return Res::io(FDV_W_WRITE, e);
        return Res();
    }
    seal_chunk(j, i, i + 1 == t->n, s.buf.p, s.len);
    uint64_t off = HDR + i * (uint64_t(t->chunk) + TAG);
    if (!t->dst.write_at(s.buf.p, size_t(s.len) + TAG, off, e)) return Res::io(FDV_W_WRITE, e);
    return Res();
}

void Engine::do_final(Task* t) {
    fdv_job& j = *t->job;
    IoError e;
    Res r;
    // finalizing 置位且 inflight==0 后其它线程不会再碰这个任务；err 在持锁时写入，这里读到的是最终值
    r = t->err;
    if (r.ok() && t->completed < t->n) r = Res::of(cancelled_ ? FDV_E_CANCELLED : FDV_E_SKIPPED);
    uint8_t digest[32];
    if (r.ok()) {
        t->sha.finish(digest);
        if (op_ == FDV_OP_ENCRYPT) {
            uint64_t dsz = 0;
            uint64_t want = HDR + t->plain + t->n * TAG;
            if (!t->dst.sync(e))
                r = Res::io(FDV_W_SYNC, e);
            else if (!t->dst.size(&dsz, e))
                r = Res::io(FDV_W_STAT, e);
            else if (dsz != want)
                r = Res::of(FDV_E_ENC_SIZE);
        } else {
            if (j.has_expected_sha && std::memcmp(digest, j.expected_sha, 32) != 0)
                r = Res::of(FDV_E_HASH);
            else if (op_ == FDV_OP_DECRYPT && !t->dst.sync(e))
                r = Res::io(FDV_W_SYNC, e);
        }
    }
    t->src.close();
    t->dst.close();
    if (!r.ok() && t->dst_created) remove_file(j.dst);
    finish_job(j, r);
    if (r.ok()) {
        std::memcpy(j.out_sha, digest, 32);
        j.out_size = int64_t(t->plain);
    }
}

// ---------------------------------------------------------------- 入口

int Engine::run(fdv_progress_fn cb, void* user) {
    uint64_t units = 0;
    for (size_t k = 0; k < njobs_; ++k) {
        fdv_job& j = jobs_[k];
        j.status = FDV_E_SKIPPED;
        j.sys_error = 0;
        j.where = 0;
        j.out_size = 0;
        std::memset(j.out_sha, 0, sizeof(j.out_sha));
        if (!j.src) return -1;
        if ((op_ == FDV_OP_ENCRYPT || op_ == FDV_OP_DECRYPT) && !j.dst) return -1;
        if (op_ == FDV_OP_ENCRYPT && j.chunk_size == 0) return -1;
        uint64_t unit = op_ == FDV_OP_ENCRYPT ? j.chunk_size : DEFAULT_UNIT;
        uint64_t size = j.expected_size >= 0 ? uint64_t(j.expected_size) : DEFAULT_UNIT;
        units += (op_ == FDV_OP_HASH || op_ == FDV_OP_STAT || size <= unit) ? 1 : (size + unit - 1) / unit;
    }
    if (njobs_ == 0) return 0;

    order_.resize(njobs_);
    for (size_t k = 0; k < njobs_; ++k) order_[k] = k;
    // 大文件先开始：它们的块可以分给所有线程，避免最后只剩一个大文件拖尾
    std::stable_sort(order_.begin(), order_.end(), [this](size_t a, size_t b) {
        return jobs_[a].expected_size > jobs_[b].expected_size;
    });

    if (threads_ <= 0) threads_ = fdv_default_threads();
    if (uint64_t(threads_) > units) threads_ = int(units);
    if (threads_ < 1) threads_ = 1;
    max_active_ = size_t(threads_) * 2;
    // 在途块的内存上限：每线程约 2 块，限制在 64 MB ~ 512 MB
    size_t budget = size_t(threads_) * 2 * (DEFAULT_UNIT + TAG);
    mem_budget_ = std::min<size_t>(std::max<size_t>(budget, 64u << 20), 512u << 20);

    // 开工前先问一次：已经取消就不动任何文件；已经暂停就从暂停状态开始
    if (cb) {
        int r = cb(0, 0, user);
        if (r == 1) {
            for (size_t k = 0; k < njobs_; ++k) jobs_[k].status = FDV_E_CANCELLED;
            return 2;
        }
        paused_ = (r == 2);
    }

    std::vector<std::thread> pool;
    {
        std::lock_guard<std::mutex> lk(mu_);
        alive_ = threads_;
    }
    pool.reserve(size_t(threads_));
    for (int k = 0; k < threads_; ++k) pool.emplace_back([this] { worker(); });

    {
        std::unique_lock<std::mutex> lk(mu_);
        while (alive_ > 0) {
            cv_done_.wait_for(lk, std::chrono::milliseconds(50));
            if (cb && alive_ > 0) {
                uint64_t b = done_bytes_.load(), f = done_files_.load();
                lk.unlock();
                int r = cb(b, f, user);
                lk.lock();
                if (r == 1 && !cancelled_.load()) {
                    cancelled_ = true;
                    stopping_ = true;
                    cv_.notify_all();
                }
                bool p = (r == 2);
                if (p != paused_) {
                    paused_ = p;
                    cv_.notify_all();
                }
            }
        }
    }
    for (std::thread& th : pool) th.join();
    if (cb) cb(done_bytes_.load(), done_files_.load(), user);

    bool any_fail = false;
    for (size_t k = 0; k < njobs_; ++k) {
        fdv_job& j = jobs_[k];
        if (cancelled_ && j.status == FDV_E_SKIPPED) j.status = FDV_E_CANCELLED;
        if (j.status != FDV_OK) any_fail = true;
    }
    if (cancelled_) return 2;
    return any_fail ? 1 : 0;
}

}  // namespace

int run_batch(int op, fdv_job* jobs, size_t njobs, int threads, int stop_on_error, fdv_progress_fn cb,
              void* user) {
    if (op < FDV_OP_ENCRYPT || op > FDV_OP_STAT) return -1;
    if (njobs && !jobs) return -1;
    Engine eng(op, jobs, njobs, threads, stop_on_error != 0);
    return eng.run(cb, user);
}

}  // namespace fdv
