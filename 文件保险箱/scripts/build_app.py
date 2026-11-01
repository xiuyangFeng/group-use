"""打包脚本：生成 macOS .app / Windows 文件夹版，并打成 zip + 校验和。

用法（在项目根目录）：
    .venv/bin/python scripts/build_app.py            # 构建 + 打包 + 冒烟
    .venv/bin/python scripts/build_app.py --no-clean
    .venv/bin/python scripts/build_app.py --skip-smoke

产物（dist/）：
    文件保险箱.app                                macOS 应用（双击即用）
    文件保险箱-0.3.0-macOS-arm64.zip               给同事的压缩包
    文件保险箱/SHA256SUMS.txt

Windows 上运行会生成 dist/文件保险箱/ 文件夹版，打成 -Windows-x64.zip。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from core.console import setup_utf8_stdio  # noqa: E402

APP_NAME = "文件保险箱"
DIST = PROJECT / "dist"
BUILD = PROJECT / ".build"
ASSETS = PROJECT / "assets"


def log(msg: str) -> None:
    print(f"[打包] {msg}", flush=True)


def version() -> str:
    from gui import __version__

    return __version__


def ensure_icons() -> dict[str, Path]:
    """用程序自己的绘制代码生成图标（png / icns / ico），避免额外设计资源。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from gui.style import make_app_icon

    app = QApplication.instance() or QApplication([])  # noqa: F841
    icon = make_app_icon()
    ASSETS.mkdir(parents=True, exist_ok=True)
    out = {
        "png": ASSETS / "icon-1024.png",
        "ico": ASSETS / f"{APP_NAME}.ico",
    }
    icon.pixmap(1024, 1024).save(str(out["png"]))
    icon.pixmap(256, 256).save(str(out["ico"]))
    for p in out.values():
        if not p.exists() or p.stat().st_size == 0:
            raise SystemExit(f"图标生成失败：{p}")
    log(f"图标已生成：{', '.join(str(p.name) for p in out.values())}")
    return out


def run(cmd: list[str], **kwargs) -> None:
    log("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(PROJECT), **kwargs)


def build_iconset() -> None:
    """macOS 多尺寸 iconset（.icns 用 iconutil 生成最稳）。"""
    if sys.platform != "darwin":
        return
    iset = BUILD / f"{APP_NAME}.iconset"
    shutil.rmtree(iset, ignore_errors=True)
    iset.mkdir(parents=True)
    from PySide6.QtWidgets import QApplication

    from gui.style import make_app_icon

    app = QApplication.instance() or QApplication([])  # noqa: F841
    icon = make_app_icon()
    for size, name in [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]:
        icon.pixmap(size, size).save(str(iset / name))
    subprocess.run(
        ["iconutil", "-c", "icns", str(iset), "-o", str(ASSETS / f"{APP_NAME}.icns")],
        check=True,
        cwd=str(PROJECT),
    )


def pyinstaller(icons: dict[str, Path], clean: bool) -> Path:
    env = dict(os.environ)
    env["PYINSTALLER_CONFIG_DIR"] = str(BUILD / "pyinstaller-cache")
    if sys.platform == "darwin":
        icon = ASSETS / f"{APP_NAME}.icns"  # 由 build_iconset() 用 iconutil 生成
        if not icon.exists():
            raise SystemExit(f"没有生成 {icon}（iconutil 失败？）")
    else:
        icon = icons["ico"]
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--windowed",
        "--name",
        APP_NAME,
        "--icon",
        str(icon),
        "--workpath",
        str(BUILD / "pyinstaller"),
        "--specpath",
        str(BUILD),
        "--distpath",
        str(DIST),
        "run_gui.py",
    ]
    if clean:
        cmd.insert(3, "--clean")
    if sys.platform == "darwin":
        cmd += ["--osx-bundle-identifier", "local.fdvault.gui"]
    run(cmd, env=env)

    if sys.platform == "darwin":
        app = DIST / f"{APP_NAME}.app"
        if not app.exists():
            raise SystemExit(f"没有生成 {app}")
        return app
    folder = DIST / APP_NAME
    if not folder.exists():
        raise SystemExit(f"没有生成 {folder}")
    return folder


def smoke_test(bundle: Path) -> None:
    """离屏启动打包后的程序：能启动、能创建数据目录、2.5 秒后自动退出。"""
    fake_home = BUILD / "smoke-home"
    shutil.rmtree(fake_home, ignore_errors=True)
    fake_home.mkdir(parents=True)
    env = dict(os.environ)
    env.update(
        {
            "QT_QPA_PLATFORM": "offscreen",
            "FDVAULT_AUTO_QUIT_MS": "2500",
            "HOME": str(fake_home),
            "USERPROFILE": str(fake_home),
        }
    )
    if sys.platform == "darwin":
        binary = bundle / "Contents" / "MacOS" / APP_NAME
    else:
        binary = bundle / f"{APP_NAME}.exe"
    log(f"真实参数验收：{binary.name} --acceptance（默认 KDF/分块，端到端加密流程）")
    acc_env = dict(env)
    acc_env.pop("FDVAULT_KDF_FAST", None)
    acc_env.pop("FDVAULT_CHUNK_SIZE", None)
    acc_report = fake_home / "acceptance-report.txt"
    acc = subprocess.run(
        [str(binary), "--acceptance", "--report", str(acc_report)],
        env=acc_env, cwd=str(fake_home), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    acc_text = acc.stdout
    if acc_report.exists():
        acc_text += acc_report.read_text(encoding="utf-8", errors="replace")
    if acc.returncode != 0 or "ACCEPTANCE PASS" not in acc_text:
        raise SystemExit(
            f"打包程序真实参数验收失败（exit {acc.returncode}）\nSTDOUT:\n{acc.stdout[-4000:]}\nSTDERR:\n{acc.stderr[-4000:]}"
        )
    log("验收通过：" + next(line for line in acc_text.splitlines() if "ACCEPTANCE PASS" in line))

    log(f"自检启动：{binary.name} --selftest（离屏，验证加密核心 + 界面组件）")
    sel_report = fake_home / "selftest-report.txt"
    proc = subprocess.run(
        [str(binary), "--selftest", "--report", str(sel_report)],
        env=env, cwd=str(fake_home), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    sel_text = proc.stdout
    if sel_report.exists():
        sel_text += sel_report.read_text(encoding="utf-8", errors="replace")
    if proc.returncode != 0 or "SELFCHECK PASS" not in sel_text:
        raise SystemExit(
            f"打包程序自检失败（exit {proc.returncode}）\nSTDOUT:\n{proc.stdout[-4000:]}\nSTDERR:\n{proc.stderr[-4000:]}"
        )
    log("自检通过：" + next(line for line in sel_text.splitlines() if "SELFCHECK PASS" in line))

    log(f"冒烟启动：{binary.name}（离屏，2.5 秒自动退出）")
    proc = subprocess.run([str(binary)], env=env, cwd=str(fake_home), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if proc.returncode != 0:
        raise SystemExit(f"打包程序启动失败（exit {proc.returncode}）\nSTDOUT:\n{proc.stdout[-3000:]}\nSTDERR:\n{proc.stderr[-3000:]}")
    log("冒烟通过：打包程序可正常启动并退出")

    # 右键菜单/拖拽入口：带一个文件夹参数启动，应打开新建向导并预填（自动退出兜底）
    folder = fake_home / "待上锁文件夹"
    folder.mkdir(exist_ok=True)
    log("冒烟：带文件夹参数启动（右键菜单/拖拽入口）")
    proc = subprocess.run(
        [str(binary), str(folder)], env=env, cwd=str(fake_home), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"带文件夹参数启动失败（exit {proc.returncode}）\nSTDOUT:\n{proc.stdout[-3000:]}\nSTDERR:\n{proc.stderr[-3000:]}"
        )
    log("冒烟通过：带文件夹参数可打开向导并退出（未崩溃）")

    if sys.platform == "darwin":
        from scripts.check_no_network import check_binary

        log("运行时网络检查：启动程序，lsof 查看有没有 IP 连接/监听")
        net_report = PROJECT / "reports" / "no-network-runtime.json"
        check_binary(binary, net_report)
        log("运行时无网络检查通过：0 个 IP 连接/监听")


def archive(bundle: Path) -> list[Path]:
    ver = version()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    os_name = "macOS" if sys.platform == "darwin" else "Windows"
    out = DIST / f"{APP_NAME}-{ver}-{os_name}-{arch}.zip"
    if out.exists():
        out.unlink()
    if sys.platform == "darwin":
        # ditto 保留 .app 的符号链接 / 扩展属性，比 zipfile 更稳
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(bundle), str(out)], check=True)
    else:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in bundle.rglob("*"):
                if item.is_file():
                    zf.write(item, item.relative_to(DIST))
    log(f"压缩包：{out.name}（{out.stat().st_size / 1024 / 1024:.1f} MB）")
    made = [out]

    if sys.platform == "darwin":
        dmg = make_dmg(bundle)
        if dmg is not None:
            made.append(dmg)

    src = make_source_zip()
    made.append(src)
    return made


def verify_zip_artifact(zip_path: Path) -> None:
    """模拟用户拿到 zip 的安装方式：解压到临时目录，再从解压后的程序跑自检。"""
    work = BUILD / "zip-verify"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    log(f"验收 zip：解压 {zip_path.name} 并运行自检")
    if sys.platform == "darwin":
        subprocess.run(["ditto", "-x", "-k", str(zip_path), str(work)], check=True)
    else:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(work)
    if sys.platform == "darwin":
        bundle = work / f"{APP_NAME}.app"
        binary = bundle / "Contents" / "MacOS" / APP_NAME
    else:
        bundle = work / APP_NAME
        binary = bundle / f"{APP_NAME}.exe"
    if not binary.exists():
        raise SystemExit(f"zip 解压后找不到可执行文件：{binary}")
    fake_home = work / "home"
    fake_home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(fake_home)
    env["USERPROFILE"] = str(fake_home)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.pop("FDVAULT_KDF_FAST", None)
    env.pop("FDVAULT_CHUNK_SIZE", None)
    report = work / "selftest-report.txt"
    proc = subprocess.run(
        [str(binary), "--selftest", "--report", str(report)],
        env=env,
        cwd=str(work),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    text = proc.stdout
    if report.exists():
        text += report.read_text(encoding="utf-8", errors="replace")
    if proc.returncode != 0 or "SELFCHECK PASS" not in text:
        raise SystemExit(
            f"zip 解压后的程序自检失败（exit {proc.returncode}）\nSTDOUT:\n{proc.stdout[-3000:]}\nSTDERR:\n{proc.stderr[-3000:]}"
        )
    log("zip 解压后自检通过")

    folder = work / "待上锁文件夹"
    folder.mkdir(exist_ok=True)
    smoke_env = dict(env)
    smoke_env["FDVAULT_AUTO_QUIT_MS"] = "2500"
    proc = subprocess.run(
        [str(binary), str(folder)],
        env=smoke_env,
        cwd=str(work),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"zip 解压后带文件夹参数启动失败（exit {proc.returncode}）\nSTDOUT:\n{proc.stdout[-3000:]}\nSTDERR:\n{proc.stderr[-3000:]}"
        )
    log("zip 解压后文件夹参数冒烟通过")
    shutil.rmtree(work, ignore_errors=True)


def make_dmg(bundle: Path) -> Path | None:
    """macOS 磁盘映像：拖拽安装（应用 + Applications 快捷方式）。"""
    ver = version()
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    dmg = DIST / f"{APP_NAME}-{ver}-macOS-{arch}.dmg"
    if dmg.exists():
        dmg.unlink()
    stage = BUILD / "dmg-stage"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    log("制作 dmg（复制 .app，可能需要十几秒）…")
    subprocess.run(["ditto", str(bundle), str(stage / bundle.name)], check=True)
    (stage / "Applications").symlink_to("/Applications")

    # 注意：在受限沙箱/CI 里 hdiutil create -srcfolder 可能因为无法挂载临时卷而失败，
    # 所以用等价的 makehybrid 先做 HFS 镜像，再 convert 成压缩 dmg（不需要临时挂载）。
    raw = BUILD / "dmg-raw.dmg"  # 注意：hdiutil 会给没有扩展名的 -o 自动补 .dmg
    for p in (raw, dmg):
        if p.exists():
            p.unlink()
    mk = subprocess.run(
        ["hdiutil", "makehybrid", "-hfs", "-hfs-volume-name", APP_NAME, "-o", str(raw), str(stage)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if mk.returncode != 0 or not raw.exists():
        raise SystemExit(f"dmg 镜像生成失败：{mk.stdout[-2000:]}{mk.stderr[-2000:]}")
    conv = subprocess.run(
        ["hdiutil", "convert", str(raw), "-format", "UDZO", "-ov", "-o", str(dmg)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    raw.unlink(missing_ok=True)
    if conv.returncode != 0 or not dmg.exists():
        raise SystemExit(f"dmg 压缩失败：{conv.stdout[-2000:]}{conv.stderr[-2000:]}")
    log(f"dmg：{dmg.name}（{dmg.stat().st_size / 1024 / 1024:.1f} MB）")

    verify = subprocess.run(["hdiutil", "verify", str(dmg)], capture_output=True, text=True)
    if verify.returncode != 0:
        raise SystemExit(f"dmg 校验失败：{verify.stdout[-2000:]}{verify.stderr[-2000:]}")
    mnt = BUILD / "dmg-mnt"
    shutil.rmtree(mnt, ignore_errors=True)
    mnt.mkdir(parents=True)
    attach = subprocess.run(
        ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mnt), str(dmg)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if attach.returncode == 0:
        app_in_dmg = mnt / bundle.name
        if not app_in_dmg.exists():
            subprocess.run(["hdiutil", "detach", str(mnt)], capture_output=True)
            raise SystemExit("dmg 挂载后找不到 .app")
        binary = app_in_dmg / "Contents" / "MacOS" / APP_NAME
        fake_home = BUILD / "dmg-home"
        shutil.rmtree(fake_home, ignore_errors=True)
        fake_home.mkdir(parents=True)
        env = dict(os.environ)
        env.update({"HOME": str(fake_home), "USERPROFILE": str(fake_home), "QT_QPA_PLATFORM": "offscreen"})
        env.pop("FDVAULT_KDF_FAST", None)
        env.pop("FDVAULT_CHUNK_SIZE", None)
        log("dmg 挂载成功：从镜像内程序跑自检")
        report = BUILD / "dmg-selftest-report.txt"
        report.unlink(missing_ok=True)
        fail = None
        try:
            proc = subprocess.run(
                [str(binary), "--selftest", "--report", str(report)],
                env=env,
                cwd=str(fake_home),
                capture_output=True,
                text=True,
                timeout=300,
            )
            text = proc.stdout
            if report.exists():
                text += report.read_text(encoding="utf-8", errors="replace")
            if proc.returncode != 0 or "SELFCHECK PASS" not in text:
                fail = (
                    f"dmg 镜像内程序自检失败（exit {proc.returncode}）\n"
                    f"STDOUT:\n{proc.stdout[-3000:]}\nSTDERR:\n{proc.stderr[-3000:]}"
                )
            else:
                folder = fake_home / "待上锁文件夹"
                folder.mkdir(exist_ok=True)
                smoke_env = dict(env)
                smoke_env["FDVAULT_AUTO_QUIT_MS"] = "2500"
                proc2 = subprocess.run(
                    [str(binary), str(folder)], env=smoke_env, cwd=str(fake_home),
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                )
                if proc2.returncode != 0:
                    fail = (
                        f"dmg 镜像内程序带文件夹参数启动失败（exit {proc2.returncode}）\n"
                        f"STDOUT:\n{proc2.stdout[-3000:]}\nSTDERR:\n{proc2.stderr[-3000:]}"
                    )
        finally:
            subprocess.run(["hdiutil", "detach", str(mnt)], capture_output=True)
        if fail:
            raise SystemExit(fail)
        log("dmg 校验通过（verify + 挂载 + 镜像内自检/文件夹参数）")
    else:
        log("dmg 校验通过（verify；挂载检查因环境权限跳过）")
    return dmg


def make_source_zip() -> Path:
    """源码 + 文档 + 测试 + CI 配置（不含 .venv / 构建产物），方便换电脑复现。"""
    ver = version()
    out = DIST / f"{APP_NAME}-{ver}-源码.zip"
    if out.exists():
        out.unlink()
    skip_dirs = {".venv", ".build", "dist", "__pycache__", ".pytest_cache", ".git", ".stress", ".bench", ".idea"}
    include_roots = [
        "core", "cli", "gui", "tests", "scripts", "docs", "installers", "reports",
        "assets", ".github",
    ]
    include_files = [
        "README.md", "requirements.txt", "requirements-dev.txt", "pytest.ini",
        "run_gui.py", ".gitignore",
    ]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in include_files:
            p = PROJECT / name
            if p.exists():
                zf.write(p, f"{APP_NAME}-{ver}/{name}")
        for root_name in include_roots:
            root = PROJECT / root_name
            if not root.exists():
                continue
            for item in root.rglob("*"):
                if item.is_dir():
                    continue
                if any(part in skip_dirs for part in item.parts):
                    continue
                if item.suffix == ".pyc":
                    continue
                zf.write(item, f"{APP_NAME}-{ver}/{item.relative_to(PROJECT)}")
    log(f"源码包：{out.name}（{out.stat().st_size / 1024 / 1024:.1f} MB）")
    return out


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def checksums(files: list[Path]) -> Path:
    lines = [f"{sha256(p)}  {p.name}" for p in files]
    out = DIST / "SHA256SUMS.txt"
    out.write_text("\n".join(lines) + "\n", "utf-8")
    log(f"校验和：{out.name}")
    for line in lines:
        print("    " + line, flush=True)
    return out


def main() -> int:
    setup_utf8_stdio()
    parser = argparse.ArgumentParser(description="打包文件保险箱")
    parser.add_argument("--no-clean", action="store_true", help="不清理 PyInstaller 缓存")
    parser.add_argument("--skip-smoke", action="store_true", help="跳过打包后的启动冒烟")
    parser.add_argument("--skip-build", action="store_true", help="只重新打包现有产物")
    args = parser.parse_args()

    log(f"版本 {version()} · {platform.system()} {platform.machine()}")
    BUILD.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    icons = ensure_icons()
    if sys.platform == "darwin":
        build_iconset()
    if args.skip_build:
        bundle = DIST / (f"{APP_NAME}.app" if sys.platform == "darwin" else APP_NAME)
    else:
        bundle = pyinstaller(icons, clean=not args.no_clean)
    log(f"产物：{bundle}（{sum(p.stat().st_size for p in bundle.rglob('*') if p.is_file()) / 1024 / 1024:.0f} MB）")
    if not args.skip_smoke:
        smoke_test(bundle)
    artifacts = archive(bundle)
    verify_zip_artifact(next(p for p in artifacts if p.suffix == ".zip"))
    checksums(artifacts)
    from scripts.delivery_manifest import write_manifest

    manifest = write_manifest()
    log(f"交付清单：{manifest.name}")
    log("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
