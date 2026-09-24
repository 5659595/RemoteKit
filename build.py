"""把 remotekit.py + dashboard.html 打成单文件可执行程序。

用法:  python build.py          # 当前平台单文件
       python build.py --app    # macOS 额外产出 dist/RemoteKit.app（Finder 双击）
产物:  dist/RemoteKit.exe (Windows) / dist/RemoteKit + RemoteKit.app (Mac) / dist/RemoteKit (Linux)
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SEP = ";" if os.name == "nt" else ":"
IS_MAC = sys.platform == "darwin"


def python_with_pyinstaller() -> str:
    """优先用当前解释器；装不上（如 PEP 668 受管环境）就在 .venv-build 里装。"""
    try:
        import PyInstaller  # noqa: F401
        return sys.executable
    except ImportError:
        pass
    if subprocess.call([sys.executable, "-m", "pip", "install", "-q", "pyinstaller>=6.0"]) == 0:
        return sys.executable
    venv = os.path.join(HERE, ".venv-build")
    py = os.path.join(venv, "Scripts" if os.name == "nt" else "bin", "python.exe" if os.name == "nt" else "python")
    if not os.path.exists(py):
        subprocess.check_call([sys.executable, "-m", "venv", venv])
    subprocess.check_call([py, "-m", "pip", "install", "-q", "pyinstaller>=6.0"])
    return py


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows CI 控制台默认非 UTF-8
    except Exception:  # noqa: BLE001
        pass
    py = python_with_pyinstaller()
    base = [py, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
            "--name", "RemoteKit", "--add-data", f"dashboard.html{SEP}."]
    if IS_MAC and "--app" in sys.argv:
        # 先打 .app（onedir + windowed；onefile+windowed 在 PyInstaller 7 起会报错），
        # 再打单文件控制台版——两者都叫 RemoteKit，会互相覆盖，所以分先后并删掉 onedir 中间产物
        subprocess.check_call([a for a in base if a != "--onefile"] + ["--windowed", "remotekit.py"], cwd=HERE)
        shutil.rmtree(os.path.join(HERE, "dist", "RemoteKit"), ignore_errors=True)
        print("\nApp 打包完成:", os.path.join(HERE, "dist", "RemoteKit.app"))
    subprocess.check_call(base + ["--console", "remotekit.py"], cwd=HERE)
    out = os.path.join(HERE, "dist", "RemoteKit.exe" if os.name == "nt" else "RemoteKit")
    print("\n打包完成:", out, f"({os.path.getsize(out) // 1024 // 1024} MB)")


if __name__ == "__main__":
    main()
