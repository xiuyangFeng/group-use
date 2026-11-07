"""脱敏小软件 性能基准工具（用户可用）。

可以做两件事：

1) 生成一份合成的 CT 数据（不含任何真实患者信息，512x512 16bit，约 0.5 MB/张），
   用来在自己的电脑上测吞吐、挑并行进程数：

       python tools/benchmark_deid.py --generate /tmp/bench --patients 4 --slices 400

   会在 /tmp/bench 下生成病人文件夹（不会覆盖任何已有文件；目标文件夹已存在就报错退出）。

2) 对同一份数据，用不同的并行进程数各跑一遍完整的脱敏（扫描 → 脱敏 → 校验 → 写映射表），
   打印每个进程数的文件数、耗时、文件/秒、MB/s、CPU 利用率，并推荐吞吐最高的进程数：

       python tools/benchmark_deid.py --input /tmp/bench --out /tmp/bench_result --workers 4 6 8 10

   也可以让工具用软件当前的默认进程数跑：--workers 省略即可。
   每档重复跑多次：--runs 3（默认 1 次；本机有其他任务在跑时建议 3 次取中位数）。

说明（重要）：
- 所有输出都写在 --out 下面：output/<进程数>/run<N>/ 是脱敏结果，mapping/ 是映射表，
  benchmark_summary.json 是本次测量数据。工具不会删除任何文件，也不会覆盖已有输出；
  --out 已经存在就直接报错，请换一个新路径（想重测就换路径，或自己删掉旧结果）。
- 处理记录写到 --out/.deid_home，不会碰你本机的真实处理记录。
- 测速时不要同时跑别的重任务；同一台机器上多次测量的结果才互相可比。
- Windows / macOS 通用。只依赖本仓库的 requirements（pydicom、openpyxl），不需要 pytest。
"""

import argparse
import json
import os
import platform
import statistics
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent            # 脱敏小软件/tools
REPO = HERE.parent                                # 脱敏小软件
for _p in (str(REPO), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:  # Windows 上没有 resource 模块
    import resource
except ImportError:  # pragma: no cover
    resource = None


# ---------------------------------------------------------------- 合成数据
# 每张图 512x512、16bit = 512*512*2 = 524288 字节像素，加上文件头约 0.5 MB。
PIXEL_ROWS = 512
PIXEL_COLS = 512
_PIXEL_BYTES = PIXEL_ROWS * PIXEL_COLS * 2


def _pixel_template():
    """生成 0.5 MB 的一整块像素数据（所有切片共用同一块 immutab 数据，生成更快）。"""
    import random
    rng = random.Random(20240101)  # 固定种子：每次生成的数据一致，结果可复现
    return bytes(rng.getrandbits(8) for _ in range(_PIXEL_BYTES))


def _enlarge_pixels(ds, template):
    """把 pydicom 自带的 128x128 小图放大成 512x512 16bit（约 0.5 MB）的合成 CT。"""
    ds.Rows = PIXEL_ROWS
    ds.Columns = PIXEL_COLS
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PixelData = template


def _make_patient(index, total):
    """一个合成患者（姓名/ID 都是假的，BENCH 开头一看就知道是基准数据）。"""
    return {
        "name": f"BENCH^{index:04d}",
        "plain": f"BENCH {index:04d}",
        "id": f"BENCH{index:04d}",
        "birth": f"1970{(index % 12) + 1:02d}{(index % 27) + 1:02d}",
        "sex": "M" if index % 2 else "F",
        "acc": f"B{index:08d}",
        "date": f"2024{(index % 12) + 1:02d}{(index % 27) + 1:02d}",
        "charset": "GB18030",  # 合成字段里有中文，用 GB18030 避免编码告警
    }


def generate(out_dir, patients, slices, series_count):
    """生成合成 CT 数据：out_dir/P0001/S01/*.dcm 这样的结构（每个病人一个文件夹）。"""
    import make_fake_data  # 同目录的 tools/make_fake_data.py（复用其造数据逻辑）

    out = Path(out_dir)
    if out.exists():
        sys.exit(f"[错误] {out} 已经存在。为不覆盖已有文件，已停止；请换一个新的目标路径。")

    series_count = max(1, min(series_count, slices))
    per_series = [slices // series_count] * series_count
    for i in range(slices - sum(per_series)):
        per_series[-1] += 1
    total = patients * slices
    template = _pixel_template()

    print(f"生成合成 CT：{patients} 个患者 x {slices} 张 = {total} 个文件"
          f"（每个患者 {series_count} 个序列，512x512 16bit，约 0.5 MB/张）")
    print(f"目标：{out}")
    out.mkdir(parents=True)
    started = time.perf_counter()
    done = 0
    try:
        for p in range(1, patients + 1):
            patient = _make_patient(p, patients)
            study_uid = make_fake_data.uid()
            patient_dir = out / f"P{p:04d}"
            inst = 0
            for s in range(1, series_count + 1):
                series_uid = make_fake_data.uid()
                series_dir = patient_dir / f"S{s:02d}"
                series_dir.mkdir(parents=True)
                for _ in range(per_series[s - 1]):
                    inst += 1
                    ds = make_fake_data.make_slice(
                        patient, study_uid, series_uid, s, inst,
                        f"Bench {s}",
                    )
                    _enlarge_pixels(ds, template)
                    ds.save_as(series_dir / f"IM{inst:04d}.dcm", enforce_file_format=True)
                    done += 1
                    if done % 500 == 0:
                        rate = done / max(time.perf_counter() - started, 1e-9)
                        print(f"  已生成 {done}/{total}（{rate:.0f} 张/秒）")
    except OSError as e:
        sys.exit(f"[错误] 生成数据时写文件失败：{e}\n（已生成的文件没有删除，请手动清理 {out}）")

    elapsed = time.perf_counter() - started
    files, total_bytes = _scan_input(out)
    print(f"完成：{files} 个文件，{total_bytes / 1e9:.2f} GB，用时 {elapsed:.1f} 秒")
    print(f"\n下一步可以测吞吐，例如：\n"
          f"  python tools/benchmark_deid.py --input {out} --out {out}_result --workers 4 6 8 10")
    return out


# ---------------------------------------------------------------- 测量环境

def _load_average():
    """本机 1 分钟平均负载（Windows 没有，返回 None）。负载高说明有别的东西在抢 CPU。"""
    try:
        return os.getloadavg()[0]
    except (AttributeError, OSError):
        return None


def _cpu_seconds():
    """本进程 + 已结束子进程累计消耗的 CPU 秒数（Windows 返回 None，不统计 CPU%）。

    ProcessPoolExecutor 的子进程在 Job.run() 结束时会 shutdown(wait=True) 被回收，
    所以两次取值之差就是这个阶段（扫描 + 脱敏 + 校验）真正用掉的 CPU 时间。
    """
    if resource is None:
        return None
    usage = resource.getrusage(resource.RUSAGE_SELF)
    total = usage.ru_utime + usage.ru_stime
    try:
        kids = resource.getrusage(resource.RUSAGE_CHILDREN)
        total += kids.ru_utime + kids.ru_stime
    except (ValueError, OSError):  # pragma: no cover
        pass
    return total


def _looks_like_dicom(path):
    try:
        with open(path, "rb") as f:
            head = f.read(132)
    except OSError:
        return False
    if head[128:132] == b"DICM":
        return True
    return head[:2] in (b"\x02\x00", b"\x08\x00")


def _scan_input(folder):
    """统计输入数据：文件总数、DICOM 文件数、DICOM 总字节数（用于算 MB/s）。"""
    total = dicoms = 0
    dicom_bytes = 0
    for path, size in _walk_files(Path(folder)):
        total += 1
        if _looks_like_dicom(path):
            dicoms += 1
            dicom_bytes += size
    if dicoms == 0:
        return total, dicom_bytes
    return dicoms, dicom_bytes


def _walk_files(folder):
    stack = [folder]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            if e.is_file(follow_symlinks=False):
                try:
                    yield e.path, e.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
            elif e.is_dir(follow_symlinks=False):
                stack.append(Path(e.path))


# ---------------------------------------------------------------- 跑一次完整脱敏

def run_one(input_dir, out_root, workers, run_no, verbose=False):
    """跑一遍完整流程，返回本次测量结果 dict。"""
    from deid.pipeline import Job

    label = "default" if workers is None else f"w{workers}"
    out_dir = out_root / "output" / label / f"run{run_no}"
    map_dir = out_root / "mapping"
    mapping_path = map_dir / f"{label}_run{run_no}.xlsx"
    if out_dir.exists() or mapping_path.exists():
        sys.exit(f"[错误] 本次输出/映射表已存在（{out_dir} 或 {mapping_path}），"
                 f"为不覆盖已有结果，已停止。请换一个 --out 路径。")

    log_lines = []
    cpu0 = _cpu_seconds()
    load0 = _load_average()
    started = time.perf_counter()
    ok = False
    error = ""
    job = Job(
        input_dir, out_dir, mapping_path,
        log=log_lines.append,
        workers=workers,
        confirm=lambda text: True,  # 基准模式：不询问，直接开始
    )
    try:
        ok = job.run()
    except Exception:
        error = traceback.format_exc()
    elapsed = time.perf_counter() - started
    cpu1 = _cpu_seconds()
    load1 = _load_average()

    stats = dict(job.stats or {})
    written = int(stats.get("written") or 0)
    files_total = int(stats.get("files_total") or 0)
    files = written if written else files_total
    cpu_pct = cpu_seconds = None
    if cpu0 is not None and cpu1 is not None:
        cpu_seconds = cpu1 - cpu0
        if elapsed > 0:
            cpu_pct = cpu_seconds / elapsed * 100.0

    result = {
        "workers": workers,
        "run": run_no,
        "ok": bool(ok),
        "error": error,
        "seconds": round(elapsed, 3),
        "files_total": files_total,
        "written": written,
        "existing": int(stats.get("existing") or 0),
        "excluded_files": int(stats.get("excluded_files") or 0),
        "files": files,
        "files_per_s": round(files / elapsed, 2) if elapsed > 0 else None,
        "cpu_pct": round(cpu_pct, 1) if cpu_pct is not None else None,
        "cpu_seconds": round(cpu_seconds, 3) if cpu_seconds is not None else None,
        "cpu_seconds_per_file": (round(cpu_seconds / files, 4)
                                 if (cpu_seconds is not None and files) else None),
        "load_before": round(load0, 2) if load0 is not None else None,
        "load_after": round(load1, 2) if load1 is not None else None,
    }
    if not ok or verbose:
        for line in log_lines:
            print(line)
    return result, (written / elapsed if elapsed > 0 else 0), log_lines


def _fmt(value, spec="{:.2f}", dash="   -"):
    return dash if value is None else spec.format(value)


def benchmark(input_dir, out_dir, worker_list, runs, verbose=False):
    input_dir = Path(input_dir).resolve()
    out_root = Path(out_dir).resolve()
    if not input_dir.is_dir():
        sys.exit(f"[错误] 输入文件夹不存在：{input_dir}")
    if out_root.exists():
        sys.exit(f"[错误] --out {out_root} 已经存在。为不覆盖已有结果，已停止；请换一个新的路径"
                 f"（想重测就先换个目录名，或自己清理旧结果）。")

    # 处理记录写到 --out 下，不碰本机真实的处理记录
    os.environ.setdefault("DEID_HOME", str(out_root / ".deid_home"))
    out_root.mkdir(parents=True)
    (out_root / "mapping").mkdir()
    (out_root / "output").mkdir()

    files, data_bytes = _scan_input(input_dir)
    data_mb = data_bytes / 1e6

    from deid.pipeline import MAX_WORKERS  # 软件当前的默认并行进程数
    if not worker_list:
        worker_list = [None]
    labels = "、".join("默认" if w is None else str(w) for w in worker_list)
    print("=" * 76)
    print("脱敏小软件 性能基准")
    print("=" * 76)
    print(f"机器：{platform.machine()}，{os.cpu_count()} 个逻辑核心，{platform.system()} {platform.release()}")
    print(f"Python：{platform.python_version()}")
    print(f"软件默认并行进程数：{MAX_WORKERS}"
          + ("（本次不指定 --workers，用默认值）" if worker_list == [None] else ""))
    print(f"输入：{input_dir}")
    print(f"       {files} 个 DICOM 文件，{data_mb / 1e3:.2f} GB")
    print(f"输出：{out_root}（不会覆盖已有文件）")
    print(f"参数：进程数 {labels}；每档重复 {runs} 次")
    print("-" * 76)

    results = []
    try:
        for workers in worker_list:
            name = "默认" if workers is None else f"workers={workers}"
            for run_no in range(1, runs + 1):
                load0 = _load_average()
                print(f"[{name} 第 {run_no}/{runs} 次] 开始……", flush=True)
                result, _, _ = run_one(input_dir, out_root, workers, run_no, verbose=verbose)
                result["data_mb"] = round(data_mb, 1)
                result["mb_per_s"] = round(data_mb / result["seconds"], 2) if result["seconds"] else None
                results.append(result)
                print(f"[{name} 第 {run_no}/{runs} 次] "
                      f"{'成功' if result['ok'] else '失败'}：{result['files']} 个文件，"
                      f"{_fmt(result['seconds'], '{:.1f}')} 秒，"
                      f"{_fmt(result['files_per_s'], '{:.1f}')} 文件/秒，"
                      f"{_fmt(result['mb_per_s'], '{:.2f}')} MB/s，"
                      f"CPU {_fmt(result['cpu_pct'], '{:.0f}%')}"
                      f"（{_fmt(result['cpu_seconds_per_file'], '{:.3f}')} 秒CPU/文件），"
                      f"负载 {_fmt(load0, '{:.2f}')}→{_fmt(result['load_after'], '{:.2f}')}",
                      flush=True)
                if not result["ok"] and result["error"]:
                    print(result["error"])
                if not result["ok"]:
                    print("……该档有失败，后面的测量继续。")
    except KeyboardInterrupt:
        print("\n已中断。已写出的结果没有删除。")
        _write_summary(out_root, input_dir, files, data_mb, worker_list, runs, results)
        return 1

    _write_summary(out_root, input_dir, files, data_mb, worker_list, runs, results)
    return 0 if all(r["ok"] for r in results) else 1


def _write_summary(out_root, input_dir, files, data_mb, worker_list, runs, results):
    """打印汇总表（每个进程数的中位数/最小值），并写 benchmark_summary.json。"""
    by_workers = {}
    for r in results:
        by_workers.setdefault(r["workers"], []).append(r)

    rows = []
    print("-" * 76)
    print("汇总（同一档跑多次时取中位数；min 是最快的一次）")
    print(f"{'进程数':>6}  {'次数':>4}  {'文件/秒':>10}  {'最快文件/秒':>12}  {'MB/s':>9}  "
          f"{'耗时中位(s)':>10}  {'CPU':>6}  {'CPU秒/文件':>10}")
    for workers in worker_list:
        rs = [r for r in by_workers.get(workers, []) if r["ok"]]
        if not rs:
            label = "默认" if workers is None else str(workers)
            print(f"{label:>6}  {len(by_workers.get(workers, [])):>4}  （失败，无数据）")
            continue
        fps = [r["files_per_s"] for r in rs]
        mbps = [r["mb_per_s"] for r in rs]
        secs = [r["seconds"] for r in rs]
        cpus = [r["cpu_pct"] for r in rs if r["cpu_pct"] is not None]
        cpfs = [r["cpu_seconds_per_file"] for r in rs if r.get("cpu_seconds_per_file") is not None]
        row = {
            "workers": workers,
            "runs_ok": len(rs),
            "files_per_s_median": round(statistics.median(fps), 2),
            "files_per_s_min": round(min(fps), 2),
            "files_per_s_max": round(max(fps), 2),
            "mb_per_s_median": round(statistics.median(mbps), 2),
            "seconds_median": round(statistics.median(secs), 2),
            "seconds_min": round(min(secs), 2),
            "cpu_pct_median": round(statistics.median(cpus), 1) if cpus else None,
            "cpu_seconds_per_file_median": round(statistics.median(cpfs), 4) if cpfs else None,
        }
        rows.append(row)
        label = "默认" if workers is None else str(workers)
        print(f"{label:>6}  {len(rs):>4}  {row['files_per_s_median']:>10.1f}  "
              f"{row['files_per_s_max']:>12.1f}  {row['mb_per_s_median']:>9.2f}  "
              f"{row['seconds_median']:>10.1f}  {_fmt(row['cpu_pct_median'], '{:.0f}%'):>6}  "
              f"{_fmt(row['cpu_seconds_per_file_median'], '{:.4f}'):>10}")

    recommendation = None
    if rows:
        best = max(rows, key=lambda r: r["files_per_s_median"])
        recommendation = best["workers"]
        label = "默认" if best["workers"] is None else f"workers={best['workers']}"
        fastest = min(rows, key=lambda r: r["seconds_median"])
        print("-" * 76)
        print(f"推荐：{label}（中位 {best['files_per_s_median']:.1f} 文件/秒，"
              f"{best['mb_per_s_median']:.2f} MB/s；最快一档是 "
              f"{'默认' if fastest['workers'] is None else 'workers=' + str(fastest['workers'])}）")
        if len(rows) > 1 and rows[0]["workers"] != best["workers"]:
            base = rows[0]["files_per_s_median"]
            if base:
                print(f"对比第一档：{(best['files_per_s_median'] / base - 1) * 100:+.0f}% 吞吐"
                      f"（第一档 = {'默认' if rows[0]['workers'] is None else 'workers=' + str(rows[0]['workers'])}）")
        if any(r["load_before"] is not None and (r["load_before"] or 0) > os.cpu_count() * 0.5
               for r in results):
            print("注意：测量时本机负载偏高（有其他程序在抢 CPU），结果偏低；"
                  "建议关掉别的程序后重测。")
        print("提示：进程数不是越多越好；机械硬盘/网络盘上边读边写，进程太多反而更慢。")

    payload = {
        "工具": "稳健性基准（合成数据，非真实患者）",
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "machine": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
        },
        "input": str(input_dir),
        "input_files": files,
        "input_mb": round(data_mb, 1),
        "runs_per_worker": runs,
        "results": results,
        "summary": rows,
        "recommended_workers": recommendation,
    }
    path = out_root / "benchmark_summary.json"
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"测量数据已保存：{path}")
    except OSError as e:  # pragma: no cover
        print(f"测量数据保存失败（不影响测试）：{e}")
    return payload


# ---------------------------------------------------------------- 入口

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="脱敏小软件性能基准：生成合成 CT 数据 / 测不同并行进程数的吞吐。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  python tools/benchmark_deid.py --generate /tmp/bench --patients 4 --slices 400\n"
            "  python tools/benchmark_deid.py --input /tmp/bench --out /tmp/bench_result --workers 4 6 8 10\n"
            "  python tools/benchmark_deid.py --input /tmp/bench --out /tmp/bench_result --runs 3\n"
        ),
    )
    ap.add_argument("--generate", metavar="DIR", help="生成合成 CT 数据到这个目录（不能是已存在的目录）")
    ap.add_argument("--patients", type=int, default=4, help="合成数据的患者数（默认 4）")
    ap.add_argument("--slices", type=int, default=400, help="每个患者的切片数（默认 400）")
    ap.add_argument("--series", type=int, default=2, help="每个患者的序列数（默认 2）")
    ap.add_argument("--input", metavar="DIR", help="要测的 DICOM 数据文件夹（每个子文件夹一个患者）")
    ap.add_argument("--out", metavar="DIR", help="测速结果写到这里（必须是不存在的路径）")
    ap.add_argument("--workers", type=int, nargs="+", metavar="N",
                    help="要对比的并行进程数，如 4 6 8 10；不填则只测软件默认值")
    ap.add_argument("--runs", type=int, default=1, help="每个进程数重复跑几次（默认 1；建议 3 取中位数）")
    ap.add_argument("--verbose", action="store_true", help="打印每次运行的完整处理日志（调试用）")
    args = ap.parse_args(argv)

    if args.generate:
        if args.input or args.out:
            ap.error("--generate 和 --input/--out 不能同时使用")
        if args.patients < 1 or args.slices < 1 or args.series < 1:
            ap.error("--patients / --slices / --series 必须是正整数")
        generate(args.generate, args.patients, args.slices, args.series)
        return 0
    if not (args.input and args.out):
        ap.error("测速需要同时给出 --input 和 --out（或使用 --generate 生成测试数据）")
    if args.runs < 1:
        ap.error("--runs 必须是正整数")
    if args.workers:
        if any(w < 1 for w in args.workers):
            ap.error("--workers 必须是正整数")
        seen, unique = set(), []
        for w in args.workers:  # 去重、保持用户给的顺序
            if w not in seen:
                seen.add(w)
                unique.append(w)
        args.workers = unique
    return benchmark(args.input, args.out, args.workers, args.runs, verbose=args.verbose)


if __name__ == "__main__":
    if platform.system() == "Windows":  # 打包成 exe 后多进程需要（命令行脚本可有可无）
        try:
            import multiprocessing
            multiprocessing.freeze_support()
        except Exception:  # pragma: no cover
            pass
    sys.exit(main())
