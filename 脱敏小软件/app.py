"""CT 数据脱敏工具入口。

不带参数运行：打开图形界面
命令行：python app.py --input 原始数据 --output 脱敏输出 --mapping 映射表.xlsx [--yes]
结构探查：python app.py --probe 原始数据 --report 报告.txt
处理记录：python app.py --history
"""

import argparse
import multiprocessing
import sys


def main():
    multiprocessing.freeze_support()  # 打包成 exe 后多进程需要
    if len(sys.argv) == 1:
        from deid.gui import run_gui
        run_gui()
        return

    from deid.pipeline import Job

    ap = argparse.ArgumentParser(description="CT DICOM 数据脱敏")
    ap.add_argument("--input", help="原始数据文件夹（批量：每个子文件夹一个患者；只处理一个患者时直接选那个患者文件夹）")
    ap.add_argument("--output", help="脱敏后输出文件夹")
    ap.add_argument("--mapping", help="映射表 .xlsx 路径（留在本地）")
    ap.add_argument("--yes", action="store_true", help="不询问，直接处理")
    ap.add_argument("--probe", help="只做结构探查，不修改任何数据")
    ap.add_argument("--report", default="结构探查报告.txt", help="结构探查报告保存位置")
    ap.add_argument("--workers", type=int, default=None,
                    help="并行进程数（默认用满本机核心，最多 12）")
    ap.add_argument("--history", action="store_true", help="查看以前的处理记录")
    ap.add_argument("--keep-burned-in", action="store_true",
                    help="保留疑似图像内烧录文字的序列（默认排除：截图、三维渲染图、剂量报告等）")
    layout = ap.add_mutually_exclusive_group()
    layout.add_argument("--single-patient", action="store_true",
                        help="选中的文件夹本身就是一个患者（默认自动判断）")
    layout.add_argument("--batch", action="store_true",
                        help="每个子文件夹是一个患者（默认自动判断）")
    args = ap.parse_args()
    single = True if args.single_patient else False if args.batch else None  # None = 自动判断

    if args.history:
        from deid.history import history_path, load_runs, summarize
        runs = load_runs()
        print(f"处理记录：{history_path()}（共 {len(runs)} 次）\n")
        for r in runs:
            print(summarize(r) + "\n")
        return
    if args.probe:
        from deid.probe import ReportSaveError, probe_to_file
        try:
            text = probe_to_file(args.probe, args.report, workers=args.workers,
                                 single_patient=single)  # 报告已存在则报错，不覆盖
        except ReportSaveError as e:
            print(e.text)
            print(f"\n{e}\n报告内容已打印在上面，可以复制保存，或换个 --report 位置重跑。")
            sys.exit(1)
        print(text)
        print(f"\n报告已保存：{args.report}")
        return
    if not (args.input and args.output and args.mapping):
        ap.error("需要 --input、--output、--mapping")

    def confirm(_text):
        return args.yes or input("\n确认开始处理？[y/N] ").strip().lower() == "y"

    ok = Job(args.input, args.output, args.mapping, confirm=confirm, workers=args.workers,
             exclude_burned_in=not args.keep_burned_in, single_patient=single).run()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
