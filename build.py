# -*- coding: utf-8 -*-
"""一键打包脚本：python build.py
在哪个平台上跑就出哪个平台的单文件可执行程序（Windows -> dist/FreeProxyPanel.exe，
macOS/Linux -> dist/FreeProxyPanel）。产物绑平台和 glibc，跨平台发布请用
.github/workflows/build.yml（Linux 在 manylinux2014 容器里构建，兼容 CentOS 7+）。
"""
import platform
import subprocess
import sys
from pathlib import Path

# Windows 控制台默认 cp1252/cp936，下面的中文输出会 UnicodeEncodeError，先强制 UTF-8
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent

try:
    import PyInstaller  # noqa: F401
except ImportError:
    print("缺少 PyInstaller，先装：pip install pyinstaller")
    sys.exit(1)

# --add-data 的分隔符 Windows 是分号，其余是冒号
sep = ";" if platform.system() == "Windows" else ":"
machine = platform.machine().lower()
arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",            # 单文件，双击即用
    "--clean", "--noconfirm",
    "--name", "FreeProxyPanel",
    # index.html 打进可执行文件；cache/、proxy.json 运行时落在 exe 旁边（见 server.py 的 ASSET_DIR/PANEL_DIR）
    "--add-data", f"index.html{sep}.",
    "--exclude-module", "selftest",   # 自检脚本不进产物
    str(ROOT / "server.py"),
]
print(" ".join(cmd))
subprocess.run(cmd, check=True, cwd=ROOT)

ext = ".exe" if platform.system() == "Windows" else ""
# macOS 上 platform.system() 是 "Darwin"，统一叫 macos，和 CI 上传路径 / README 的文件名一致
system = {"Darwin": "macos"}.get(platform.system(), platform.system().lower())
out = ROOT / "dist" / f"FreeProxyPanel{ext}"
final = ROOT / "dist" / f"FreeProxyPanel-{system}-{arch}{ext}"
out.replace(final)
print(f"\n打包完成 -> {final}")
