"""把 CT_Deid.exe 和 使用说明.txt 打成 CT_Deid_Windows.zip。

zipfile 遇到非 ASCII 文件名会自动设置 UTF-8 标记，Windows 资源管理器解压时中文不乱码。
用法：python tools/pack_windows.py dist/CT_Deid.exe release/CT_Deid_Windows.zip
"""

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(exe, out):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "x", zipfile.ZIP_DEFLATED) as z:  # "x"：已存在就报错，不覆盖
        z.write(exe, "CT_Deid_Windows/CT_Deid.exe")
        z.write(ROOT / "使用说明.txt", "CT_Deid_Windows/使用说明.txt")
    print(f"done: {out}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
