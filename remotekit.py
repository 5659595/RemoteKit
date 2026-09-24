# -*- coding: utf-8 -*-
"""RemoteKit - 单文件远程控制启动器

一个进程做完所有事：
  * 文件 / 命令服务      /api/*        读写文件、搜索、执行命令、截图
  * CoAgent 反向代理     /coagent/*    转发到本机 Hermes CoAgent (Windows 桌面控制)
  * Windows-MCP 反向代理 /mcp/*        转发到本机 windows-mcp (UIA 控件树，不用截图)
  * 隧道管理             cpolar / ngrok 自动识别、自动拉起、解析公网地址
  * HTML 控制面板        /             启动后自动在浏览器打开

只依赖 Python 标准库，可用 PyInstaller 打成单文件。
"""
import atexit
import base64
import http.client
import json
import locale
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import webbrowser
import zipfile
import io
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "1.0.0"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
OS_NAME = "windows" if IS_WIN else ("mac" if IS_MAC else "linux")

COAGENT_ZIP_URL = "https://github.com/Predator04/Hermes-CoAgent/archive/refs/heads/main.zip"
CPOLAR_URL_RE = re.compile(r"Tunnel established at (https?://\S+)")
NGROK_URL_RE = re.compile(r"url=(https?://\S+)")
GENERIC_URL_RE = re.compile(r"(https://[A-Za-z0-9.\-]+\.(?:cpolar\.(?:cn|top|com)|ngrok(?:-free)?\.(?:app|dev)|ngrok\.io))")


# --------------------------------------------------------------------------- 路径 / 配置
def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


APP_DIR = app_dir()
CONFIG_PATH = os.path.join(APP_DIR, "remotekit.json")
LOG_DIR = os.path.join(APP_DIR, "logs")
KNOWLEDGE_PATH = os.path.join(APP_DIR, "knowledge.md")

# 随"连接信息"一起发给远程 AI 的操作规范，面板里可改
DEFAULT_KNOWLEDGE = """\
# RemoteKit 操作规范（远程 AI 必读）

## 通道说明
- 文件 / 命令：/api/*（read / write / exec / search / batch / list / screenshot）。
- 搜索用 /api/search 即时全仓扫描，不要逐个拉文件；多文件操作用 /api/batch 合并成一次往返。

## 桌面操作优先级（重要）
- **第一选择永远是 /mcp（Windows-MCP）**：先 `Snapshot` 抓 UIA 控件树，按控件名+坐标用
  `Click / Type / Scroll / Shortcut` 直接操作——不截图、速度快、坐标精确。
- **只有 MCP 搞不定才退到截图流**：`Snapshot` 拿不到目标控件（自绘界面 / Electron 残缺树 / 游戏）、
  需要肉眼确认画面、或 MCP 工具连续失败时，才用 `/api/screenshot`（或 windows-mcp 的
  `Screenshot` 工具）+ `/coagent/*` 按像素坐标操作。
- 每步操作后再 `Snapshot` 验证结果，不要盲信上一步成功；浏览器场景优先 `Snapshot` 带
  `use_dom=true`，只取网页 DOM 更干净。

## 安全红线（任何情况下不许违反）
- 文件读写只许在工作区根目录（connection.workspace）以内；不碰系统目录
  （C:\\Windows、C:\\Program Files、注册表、驱动等）。
- 禁止破坏性命令：格式化 / 分区 / 关机重启 / 递归删除系统路径 / 结束系统进程 /
  删注册表 / 改用户账号密码（服务端也会直接拦截这类命令）。
- 禁止 git commit / push / reset --hard / clean -f：本地改动由用户自己决定何时提交，
  否则用户 git pull 时会冲突。
- 删除文件前先跟用户确认；装软件、改系统设置、动服务前也要先征得同意。

## 工作方式
- 改动最小化，只动与需求相关的文件，遵循项目现有代码风格与注释语言。
- 改完运行项目自带的编译 / lint / 测试验证后再交付。
- 不要在工作区留临时脚本 / 临时文件；临时验证用 /api/exec 直接跑命令。
"""


def load_knowledge() -> str:
    try:
        with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        try:
            with open(KNOWLEDGE_PATH, "w", encoding="utf-8", newline="") as f:
                f.write(DEFAULT_KNOWLEDGE)
        except OSError:
            pass
        return DEFAULT_KNOWLEDGE


def save_knowledge(content: str) -> None:
    with open(KNOWLEDGE_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(content)

DEFAULT_CONFIG = {
    "port": 8765,
    "token": "",
    "root": "~",
    "open_browser": True,
    "tunnel": {"provider": "auto", "ngrok_domain": "", "cpolar_region": ""},
    "coagent": {"enabled": IS_WIN, "dir": "", "port": 9123, "python": "", "auto_install": True},
    "wmcp": {"enabled": IS_WIN, "port": 9124, "exe": "", "auto_install": True},
}


def deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as exc:  # noqa: BLE001
            log(f"配置文件读取失败，使用默认值: {exc}")
    cfg = deep_merge(DEFAULT_CONFIG, cfg)
    if not cfg.get("token"):
        cfg["token"] = secrets.token_hex(16)
    save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- 日志
_LOG_LOCK = threading.Lock()
_LOG_LINES: list = []


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S ") + msg
    with _LOG_LOCK:
        _LOG_LINES.append(line)
        del _LOG_LINES[:-500]
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "remotekit.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def tail_log(n: int = 200) -> list:
    with _LOG_LOCK:
        return _LOG_LINES[-n:]


# --------------------------------------------------------------------------- 工具
def which(*names: str, extra_dirs=()) -> str:
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    for d in extra_dirs:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return ""


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_get(url: str, headers=None, timeout: float = 3.0):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def decode_output(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        enc = locale.getpreferredencoding(False) or "utf-8"
        return raw.decode(enc, "replace")


def hidden_popen_kwargs() -> dict:
    kw = {}
    if IS_WIN:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000  # CREATE_NO_WINDOW
    return kw


class WinJob:
    """Windows 作业对象：本程序以任何方式退出（关窗口 / 被杀）时，隧道和 CoAgent 子进程一起被系统杀掉。"""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as wt

        class BasicLimit(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                      "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.k32 = ctypes.windll.kernel32
        self.handle = self.k32.CreateJobObjectW(None, None)
        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self.k32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info))

    def assign(self, pid: int) -> None:
        hp = self.k32.OpenProcess(0x0001 | 0x0100, False, pid)  # TERMINATE | SET_QUOTA
        if hp:
            self.k32.AssignProcessToJobObject(self.handle, hp)
            self.k32.CloseHandle(hp)


WIN_JOB = None
if IS_WIN:
    try:
        WIN_JOB = WinJob()
    except Exception:  # noqa: BLE001
        WIN_JOB = None


def spawn_child(argv: list, **kw) -> subprocess.Popen:
    p = subprocess.Popen(argv, **kw, **hidden_popen_kwargs())
    if WIN_JOB:
        WIN_JOB.assign(p.pid)
    return p


def kill_proc(p) -> None:
    if not p or p.poll() is not None:
        return
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, **hidden_popen_kwargs())
        else:
            p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()
    except Exception:  # noqa: BLE001
        pass


def pid_listening(port: int):
    """返回监听指定端口的 pid（找不到返回 None）。"""
    try:
        if IS_WIN:
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, **hidden_popen_kwargs()).stdout
            for line in decode_output(out).splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
                    return int(parts[4])
        else:
            out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True).stdout
            s = out.decode().strip().split()
            if s:
                return int(s[0])
    except Exception:  # noqa: BLE001
        return None
    return None


def kill_pid(pid: int) -> None:
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, **hidden_popen_kwargs())
        else:
            os.kill(pid, 9)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- 危险命令拦截
# exec 不是沙箱（命令本身可访问全机），这里拦掉明确的破坏性命令；误伤宁可少拦。
_DANGEROUS = [
    (r"\bformat\s+[a-zA-Z]:\s*(/|\b|$)|\b(diskpart|bcdedit|bootrec|mkfs)\b|\bcipher\s+/w|\bvssadmin\s+delete|\bwbadmin\s+delete|\bdd\s+[^;]*of=/dev/", "磁盘 / 分区 / 引导操作"),
    (r"\b(shutdown|poweroff|reboot|halt)\s+[-/a-zA-Z0-9]|\b(Restart|Stop)-Computer\b", "关机 / 重启"),
    (r"\b(reg\s+delete|Remove-Item(Property)?\b.*-Recurse)\s+.*(HKLM|HKCR|HKU|HKEY_(LOCAL_MACHINE|CLASSES_ROOT|USERS))", "删除注册表系统项"),
    (r"\brd\s+/s\b[^;|&]*([a-zA-Z]:\\[\s\"']*$|\\(windows|program files|users)\b)", "递归删除系统目录"),
    (r"\bdel\s+/[a-zA-Z]*s[a-zA-Z]*\s[^;|&]*([a-zA-Z]:\\[\s\"']*$|\\(windows|program files)\b)", "批量删除系统文件"),
    (r"Remove-Item\b(?=[^;|&]*-Recurse)(?=[^;|&]*([a-zA-Z]:\\(?=[\s\"']|$)|\\(Windows|Program Files)\b|\$env:(SystemRoot|windir|ProgramFiles|USERPROFILE)\b))", "PowerShell 递归删除系统路径"),
    (r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*\s+)+(sudo\s+)?(/|/\*|~[/\s]*|\$HOME[/\s]*)\s*$|rm\s+-[a-zA-Z]*f[a-zA-Z]*\s+-[a-zA-Z]*r", "rm 递归删除根 / 家目录"),
    (r"\btaskkill\b[^;|&]*\b(csrss|smss|wininit|winlogon|lsass|services)\b|\bStop-Process\b[^;|&]*\b(csrss|smss|wininit|winlogon|lsass)\b", "结束系统关键进程"),
    (r"\bnet\s+(user|localgroup|accounts)\b[^;|&]*/(add|delete)\b", "改用户 / 用户组"),
    (r"\bgit\s+(push|reset\s+--hard|clean\s+-[a-zA-Z]*f)", "git 不可逆操作（本地改动由用户自己提交）"),
    (r"\bchmod\s+-R\s+[^;|&]*\s+/\s*$|\bchown\s+-R\b[^;|&]*\s+/\s*$", "改根目录权限"),
]
DANGEROUS_RES = [(re.compile(p, re.I), label) for p, label in _DANGEROUS]


def dangerous_cmd(cmd: str) -> str:
    for rx, label in DANGEROUS_RES:
        if rx.search(cmd):
            return label
    return ""


# --------------------------------------------------------------------------- 文件 / 命令服务
class FileService:
    SKIP_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", "dist", "build"}

    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root or "~"))

    def safe(self, rel: str) -> str:
        p = os.path.abspath(os.path.join(self.root, rel or "."))
        if not (p == self.root or p.startswith(self.root + os.sep)):
            raise ValueError("Path escapes workspace")
        return p

    def read(self, rel: str) -> dict:
        with open(self.safe(rel), "rb") as f:
            data = f.read()
        try:
            return {"content": data.decode("utf-8")}
        except UnicodeDecodeError:
            return {"contentBase64": base64.b64encode(data).decode()}

    def write(self, rel: str, content: str, b64: str = "") -> dict:
        p = self.safe(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if b64:
            data = base64.b64decode(b64)
            with open(p, "wb") as f:
                f.write(data)
            return {"written": len(data)}
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return {"written": len(content)}

    def list(self, rel: str) -> dict:
        p = self.safe(rel)
        entries = []
        for name in sorted(os.listdir(p)):
            full = os.path.join(p, name)
            is_dir = os.path.isdir(full)
            try:
                size = 0 if is_dir else os.path.getsize(full)
            except OSError:
                size = 0
            entries.append({"name": name, "dir": is_dir, "size": size})
        return {"entries": entries}

    def delete(self, rel: str) -> dict:
        p = self.safe(rel)
        if os.path.isdir(p):
            shutil.rmtree(p)
        else:
            os.remove(p)
        return {"deleted": rel}

    def mkdir(self, rel: str) -> dict:
        os.makedirs(self.safe(rel), exist_ok=True)
        return {"created": rel}

    def search(self, q: str, rel: str, glob: str, ci: bool, fixed: bool, limit: int) -> dict:
        rx = re.compile(re.escape(q) if fixed else q, re.I if ci else 0)
        root = self.safe(rel)
        grx = re.compile(glob.replace(".", r"\.").replace("*", ".*") + "$") if glob else None
        hits = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS]
            for name in filenames:
                if grx and not grx.search(name):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    if os.path.getsize(full) > 4 * 1024 * 1024:
                        continue
                    with open(full, "r", encoding="utf-8", errors="ignore") as f:
                        for i, line in enumerate(f, 1):
                            if rx.search(line):
                                hits.append({"file": os.path.relpath(full, self.root), "line": i,
                                             "text": line.rstrip()[:400]})
                                if len(hits) >= limit:
                                    return {"hits": hits, "truncated": True}
                except OSError:
                    continue
        return {"hits": hits, "truncated": False}

    def exec(self, cmd: str, timeout_ms: int, cwd: str, shell: str = "") -> dict:
        workdir = self.safe(cwd) if cwd else self.root
        bad = dangerous_cmd(cmd)
        if bad:
            return {"ok": False, "exitCode": -1, "stdout": "",
                    "stderr": f"已拦截危险命令（{bad}），如确需执行请让本机用户手动操作",
                    "timedOut": False, "cwd": workdir}
        if shell == "powershell" and IS_WIN:
            enc = base64.b64encode(cmd.encode("utf-16-le")).decode()
            argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc]
            use_shell = False
        else:
            argv = cmd
            use_shell = True
        try:
            proc = subprocess.run(argv, shell=use_shell, cwd=workdir, capture_output=True,
                                  timeout=max(1, timeout_ms) / 1000.0, **hidden_popen_kwargs())
            return {"ok": True, "exitCode": proc.returncode, "stdout": decode_output(proc.stdout),
                    "stderr": decode_output(proc.stderr), "timedOut": False, "cwd": workdir}
        except subprocess.TimeoutExpired as exc:
            return {"ok": False, "exitCode": -1, "stdout": decode_output(exc.stdout or b""),
                    "stderr": "timeout", "timedOut": True, "cwd": workdir}

    def run_op(self, op: dict) -> dict:
        kind = op.get("op")
        try:
            if kind == "read":
                return {"ok": True, **self.read(op["path"])}
            if kind == "write":
                return {"ok": True, **self.write(op["path"], op.get("content", ""), op.get("contentBase64", ""))}
            if kind == "list":
                return {"ok": True, **self.list(op.get("path", "."))}
            if kind == "delete":
                return {"ok": True, **self.delete(op["path"])}
            if kind == "mkdir":
                return {"ok": True, **self.mkdir(op["path"])}
            return {"ok": False, "error": "unknown op"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}


def take_screenshot() -> bytes:
    """全屏截图，返回 PNG 字节（不依赖第三方库）。"""
    tmp = os.path.join(LOG_DIR, "shot.png")
    os.makedirs(LOG_DIR, exist_ok=True)
    if IS_MAC:
        subprocess.run(["screencapture", "-x", "-t", "png", tmp], check=True, timeout=20)
    elif IS_WIN:
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
            "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
            "$g=[System.Drawing.Graphics]::FromImage($bmp);"
            "$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size);"
            f"$bmp.Save('{tmp}',[System.Drawing.Imaging.ImageFormat]::Png)"
        )
        enc = base64.b64encode(ps.encode("utf-16-le")).decode()
        subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", enc], check=True, timeout=30,
                       capture_output=True, **hidden_popen_kwargs())
    else:
        for argv in (["gnome-screenshot", "-f", tmp], ["import", "-window", "root", tmp], ["scrot", tmp]):
            if shutil.which(argv[0]):
                subprocess.run(argv, check=True, timeout=20)
                break
        else:
            raise RuntimeError("no screenshot tool (gnome-screenshot / import / scrot)")
    with open(tmp, "rb") as f:
        return f.read()


# --------------------------------------------------------------------------- 隧道
class TunnelManager:
    def __init__(self, cfg: dict, local_port: int):
        self.cfg = cfg
        self.local_port = local_port
        self.proc = None
        self.provider = ""
        self.binary = ""
        self.public_url = ""
        self.state = "stopped"  # stopped / starting / online / error
        self.error = ""
        self.lines: list = []
        self._lock = threading.Lock()

    # ---- 探测
    def find_binary(self, provider: str) -> str:
        extra = [os.path.join(APP_DIR, "bin"), APP_DIR]
        if IS_WIN:
            extra += [r"C:\Program Files\cpolar", r"C:\Program Files (x86)\cpolar",
                      os.path.join(os.environ.get("LOCALAPPDATA", ""), "Ngrok"),
                      os.path.join(os.environ.get("LOCALAPPDATA", ""), "cpolar")]
            names = {"cpolar": ["cpolar.exe"], "ngrok": ["ngrok.exe"]}
        else:
            extra += ["/usr/local/bin", "/opt/homebrew/bin", os.path.expanduser("~/bin")]
            names = {"cpolar": ["cpolar"], "ngrok": ["ngrok"]}
        return which(*names[provider], extra_dirs=extra)

    def detect(self) -> tuple:
        want = (self.cfg.get("tunnel") or {}).get("provider", "auto")
        order = ["cpolar", "ngrok"] if IS_WIN else ["ngrok", "cpolar"]
        if want in ("cpolar", "ngrok"):
            order = [want]
        for p in order:
            b = self.find_binary(p)
            if b:
                return p, b
        return "", ""

    def available(self) -> dict:
        return {p: self.find_binary(p) for p in ("cpolar", "ngrok")}

    # ---- 启停
    def start(self) -> None:
        with self._lock:
            self.stop()
            want = (self.cfg.get("tunnel") or {}).get("provider", "auto")
            if want == "none":
                self.state = "stopped"
                self.error = "隧道已在配置中关闭"
                return
            self.provider, self.binary = self.detect()
            if not self.binary:
                self.state = "error"
                self.error = "没有找到 cpolar 或 ngrok，请先安装（见面板说明）"
                log("[tunnel] " + self.error)
                return
            tcfg = self.cfg.get("tunnel") or {}
            if self.provider == "cpolar":
                argv = [self.binary, "http", str(self.local_port), "-log", "stdout", "-log-level", "info"]
                if tcfg.get("cpolar_region"):
                    argv += ["-region", tcfg["cpolar_region"]]
            else:
                argv = [self.binary, "http", str(self.local_port), "--log", "stdout", "--log-format", "logfmt"]
                if tcfg.get("ngrok_domain"):
                    argv += ["--url", tcfg["ngrok_domain"]]
            self.public_url = ""
            self.error = ""
            self.lines = []
            self.state = "starting"
            log(f"[tunnel] 启动 {self.provider}: {' '.join(argv[1:])}")
            try:
                self.proc = spawn_child(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=APP_DIR)
            except Exception as exc:  # noqa: BLE001
                self.state = "error"
                self.error = f"启动失败: {exc}"
                log("[tunnel] " + self.error)
                return
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
            if self.provider == "ngrok":
                threading.Thread(target=self._poll_ngrok_api, args=(self.proc,), daemon=True).start()

    def stop(self) -> None:
        if self.proc:
            kill_proc(self.proc)
            self.proc = None
        self.state = "stopped"
        self.public_url = ""

    def _pump(self, proc) -> None:
        for raw in iter(proc.stdout.readline, b""):
            line = decode_output(raw).rstrip()
            if not line:
                continue
            self.lines.append(line)
            del self.lines[:-80]
            m = CPOLAR_URL_RE.search(line) or NGROK_URL_RE.search(line) or GENERIC_URL_RE.search(line)
            if m:
                url = m.group(1).strip('"')
                if url.startswith("https://") or not self.public_url:
                    self.public_url = url
                    self.state = "online"
                    log(f"[tunnel] 公网地址: {url}")
            low = line.lower()
            meaningful = re.sub(r"^\s*(ERROR|ERR|error)\s*:?\s*", "", line).strip()
            if ("error" in low or "err_ngrok" in low) and len(meaningful) > 12 and not self.error:
                self.error = meaningful[-300:]
        code = proc.wait()
        if self.proc is proc:
            self.state = "error"
            self.error = self.error or f"{self.provider} 已退出 (code {code})：" + " | ".join(self.lines[-3:])
            self.public_url = ""
            log(f"[tunnel] 进程退出 code={code}")

    def _poll_ngrok_api(self, proc) -> None:
        for _ in range(60):
            if proc.poll() is not None or self.public_url:
                return
            for port in range(4040, 4050):
                try:
                    st, body = http_get(f"http://127.0.0.1:{port}/api/tunnels", timeout=1)
                    data = json.loads(body)
                    for t in data.get("tunnels", []):
                        addr = str(t.get("config", {}).get("addr", ""))
                        if addr.endswith(f":{self.local_port}") and t.get("public_url", "").startswith("https"):
                            self.public_url = t["public_url"]
                            self.state = "online"
                            log(f"[tunnel] 公网地址(api): {self.public_url}")
                            return
                except Exception:  # noqa: BLE001
                    continue
            time.sleep(1)

    def set_authtoken(self, provider: str, token: str) -> dict:
        b = self.find_binary(provider)
        if not b:
            return {"ok": False, "error": f"未找到 {provider}"}
        argv = [b, "authtoken", token] if provider == "cpolar" else [b, "config", "add-authtoken", token]
        r = subprocess.run(argv, capture_output=True, timeout=30, **hidden_popen_kwargs())
        out = decode_output(r.stdout) + decode_output(r.stderr)
        return {"ok": r.returncode == 0, "output": out[-500:]}

    def status(self) -> dict:
        return {"provider": self.provider, "binary": self.binary, "state": self.state,
                "public_url": self.public_url, "error": self.error, "recent": self.lines[-8:],
                "available": self.available()}


# --------------------------------------------------------------------------- CoAgent (Windows)
class CoAgentManager:
    def __init__(self, cfg: dict, token: str):
        self.cfg = cfg
        self.token = token
        self.proc = None
        self.state = "disabled" if not (cfg.get("coagent") or {}).get("enabled") else "stopped"
        self.error = ""
        self.dir = ""
        self.python = ""
        self.installing = False
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        return int((self.cfg.get("coagent") or {}).get("port") or 9123)

    def find_dir(self) -> str:
        c = self.cfg.get("coagent") or {}
        cands = [c.get("dir", ""), os.path.join(APP_DIR, "Hermes-CoAgent"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Hermes CoAgent")]
        for d in cands:
            if d and os.path.isfile(os.path.join(d, "hermes_coagent.py")):
                return d
        return ""

    def find_python(self) -> str:
        c = self.cfg.get("coagent") or {}
        if c.get("python") and os.path.isfile(c["python"]):
            return c["python"]
        if not getattr(sys, "frozen", False) and sys.executable:
            return sys.executable
        cands = []
        for v in ("313", "312", "311", "310"):
            cands += [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", f"Python{v}", "python.exe"),
                      rf"C:\Program Files\Python{v}\python.exe", rf"C:\Python{v}\python.exe"]
        for p in cands:
            if os.path.isfile(p):
                return p
        return which("python.exe", "python3.exe", "python", "python3")

    def ping(self) -> bool:
        try:
            st, body = http_get(f"http://127.0.0.1:{self.port}/ping", timeout=2)
            return st == 200 and b"pong" in body
        except Exception:  # noqa: BLE001
            return False

    def auth_ok(self) -> bool:
        """仅当 CoAgent 明确回 401/403 才判定 token 不同；超时/忙碌时重试而不误判。"""
        for _ in range(3):
            try:
                http_get(f"http://127.0.0.1:{self.port}/health/memory",
                         headers={"Authorization": "Bearer " + self.token}, timeout=8)
                return True
            except urllib.error.HTTPError as exc:
                return exc.code not in (401, 403)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)
        return False

    def install(self) -> None:
        """下载 CoAgent 源码到 APP_DIR/Hermes-CoAgent 并安装依赖。"""
        self.installing = True
        self.state = "installing"
        try:
            target = os.path.join(APP_DIR, "Hermes-CoAgent")
            if not os.path.isfile(os.path.join(target, "hermes_coagent.py")):
                log("[coagent] 下载 Hermes CoAgent 源码...")
                req = urllib.request.Request(COAGENT_ZIP_URL, headers={"User-Agent": "RemoteKit"})
                with urllib.request.urlopen(req, timeout=300) as r:
                    data = r.read()
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    names = zf.namelist()
                    prefix = names[0].split("/")[0] + "/"
                    for n in names:
                        if not n.startswith(prefix) or n.endswith("/"):
                            continue
                        dest = os.path.join(target, *n[len(prefix):].split("/"))
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with zf.open(n) as src, open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                log(f"[coagent] 源码已解压到 {target}")
            self.dir = target
            self.patch_threads(target)
            py = self.find_python()
            if not py:
                raise RuntimeError("没有找到 Python，请安装 Python 3.11+ 并勾选 Add to PATH")
            log(f"[coagent] 安装依赖 (pip)... 使用 {py}")
            req_file = os.path.join(target, "requirements.txt")
            r = subprocess.run([py, "-m", "pip", "install", "-r", req_file, "-q"], capture_output=True,
                               timeout=1800, cwd=target, **hidden_popen_kwargs())
            if r.returncode != 0:
                log("[coagent] pip 有报错: " + decode_output(r.stderr)[-600:])
            subprocess.run([py, "-m", "pip", "install", "pystray", "-q"], capture_output=True, timeout=600,
                           **hidden_popen_kwargs())
            subprocess.run([py, "-m", "playwright", "install", "chromium"], capture_output=True, timeout=1800,
                           **hidden_popen_kwargs())
            log("[coagent] 安装完成")
            self.state = "stopped"
        except Exception as exc:  # noqa: BLE001
            self.state = "error"
            self.error = f"安装失败: {exc}"
            log("[coagent] " + self.error)
        finally:
            self.installing = False

    @staticmethod
    def patch_threads(d: str) -> None:
        """waitress 多线程与 Playwright 同步 API 冲突，改成单线程。"""
        p = os.path.join(d, "hermes_coagent.py")
        try:
            with open(p, "r", encoding="utf-8") as f:
                s = f.read()
            if "threads=8" in s:
                with open(p, "w", encoding="utf-8", newline="") as f:
                    f.write(s.replace("threads=8", "threads=1"))
                log("[coagent] 已修补 waitress threads=1")
        except Exception as exc:  # noqa: BLE001
            log(f"[coagent] 修补失败: {exc}")

    def start(self) -> None:
        if not (self.cfg.get("coagent") or {}).get("enabled"):
            self.state = "disabled"
            return
        if not IS_WIN:
            self.state = "disabled"
            self.error = "CoAgent 仅支持 Windows"
            return
        threading.Thread(target=self._start_impl, daemon=True).start()

    def _start_impl(self) -> None:
        with self._lock:
            try:
                self.dir = self.find_dir()
                if not self.dir:
                    if (self.cfg.get("coagent") or {}).get("auto_install", True):
                        self.install()
                        self.dir = self.find_dir()
                    if not self.dir:
                        self.state = "error"
                        self.error = self.error or "未找到 CoAgent 目录"
                        return
                self.patch_threads(self.dir)
                # 已有实例且 token 一致 -> 直接复用
                if self.ping():
                    if self.auth_ok():
                        self.state = "online"
                        self.error = ""
                        log(f"[coagent] 已在运行 (端口 {self.port})，token 一致，直接复用")
                        return
                    pid = pid_listening(self.port)
                    log(f"[coagent] 端口 {self.port} 上已有实例但 token 不同，重启它 (pid {pid})")
                    if pid:
                        kill_pid(pid)
                        time.sleep(2)
                self.python = self.find_python()
                if not self.python:
                    self.state = "error"
                    self.error = "没有找到 Python，无法启动 CoAgent"
                    return
                try:
                    with open(os.path.join(self.dir, ".token"), "w", encoding="utf-8", newline="") as f:
                        f.write(self.token)
                except OSError:
                    pass
                argv = [self.python, "hermes_coagent.py", "--secure", "--allow-external",
                        f"--token={self.token}", "--port", str(self.port)]
                os.makedirs(LOG_DIR, exist_ok=True)
                out = open(os.path.join(LOG_DIR, "coagent.log"), "ab")
                env = dict(os.environ)
                env["PYTHONIOENCODING"] = "utf-8"
                self.state = "starting"
                self.proc = spawn_child(argv, cwd=self.dir, stdout=out, stderr=subprocess.STDOUT, env=env)
                log(f"[coagent] 已启动 pid={self.proc.pid} ({self.python})")
                for _ in range(60):
                    time.sleep(1)
                    if self.proc.poll() is not None:
                        self.state = "error"
                        self.error = f"CoAgent 进程退出 code={self.proc.returncode}，见 logs/coagent.log"
                        log("[coagent] " + self.error)
                        return
                    if self.ping():
                        self.state = "online"
                        self.error = ""
                        log("[coagent] 就绪")
                        return
                self.state = "error"
                self.error = "CoAgent 60 秒内未就绪，见 logs/coagent.log"
            except Exception as exc:  # noqa: BLE001
                self.state = "error"
                self.error = f"{exc}"
                log("[coagent] 启动异常: " + traceback.format_exc()[-800:])

    def stop(self) -> None:
        if self.proc:
            kill_proc(self.proc)
            self.proc = None
        pid = pid_listening(self.port)
        if pid:
            kill_pid(pid)
        self.state = "stopped" if (self.cfg.get("coagent") or {}).get("enabled") else "disabled"

    def status(self) -> dict:
        online = self.ping() if IS_WIN and self.state not in ("disabled",) else False
        if online and self.state in ("online", "starting", "stopped"):
            self.state = "online"
        elif not online and self.state == "online":
            self.state = "stopped"
        return {"state": self.state, "error": self.error, "dir": self.dir or self.find_dir(),
                "python": self.python or (self.find_python() if IS_WIN else ""), "port": self.port,
                "installing": self.installing}


# --------------------------------------------------------------------------- Windows-MCP (Windows)
def detect_python() -> str:
    """找本机 Python（仅用于安装 uv；windows-mcp 本体由 uv 管理 Python 3.14）。"""
    if not getattr(sys, "frozen", False) and sys.executable:
        return sys.executable
    cands = []
    for v in ("314", "313", "312", "311", "310"):
        cands += [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", f"Python{v}", "python.exe"),
                  rf"C:\Program Files\Python{v}\python.exe", rf"C:\Python{v}\python.exe"]
    for p in cands:
        if os.path.isfile(p):
            return p
    return which("python.exe", "python3.exe", "python", "python3")


class WindowsMCPManager:
    """官方 windows-mcp 的进程管理器：uv 安装、streamable-http 拉起、/mcp 反代。不重复实现任何功能。"""

    UV_DIRS = [os.path.join(APP_DIR, "bin"),
               os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin"),
               os.path.join(os.environ.get("USERPROFILE", ""), ".cargo", "bin")]

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.proc = None
        self.state = "disabled" if not (cfg.get("wmcp") or {}).get("enabled") else "stopped"
        self.error = ""
        self.exe = ""
        self.installing = False
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        return int((self.cfg.get("wmcp") or {}).get("port") or 9124)

    def find_uv(self) -> str:
        return which("uv.exe", "uv", extra_dirs=self.UV_DIRS)

    def find_uvx(self) -> str:
        return which("uvx.exe", "uvx", extra_dirs=self.UV_DIRS)

    def find_exe(self) -> str:
        c = self.cfg.get("wmcp") or {}
        if c.get("exe") and os.path.isfile(c["exe"]):
            return c["exe"]
        return which("windows-mcp.exe", "windows-mcp", extra_dirs=self.UV_DIRS)

    def ping(self) -> bool:
        """只认"像 MCP 服务"的响应（json / event-stream / jsonrpc 字样）；
        端口上被无关 HTTP 服务占用时返回 False，不误判 online。"""
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/mcp",
                                     headers={"Accept": "application/json, text/event-stream"})
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                ctype = r.headers.get("Content-Type", "")
                body = r.read(4096)
        except urllib.error.HTTPError as exc:
            ctype = exc.headers.get("Content-Type", "") if exc.headers else ""
            body = exc.read(4096)
        except Exception:  # noqa: BLE001
            return False
        if "json" in ctype or "event-stream" in ctype:
            return True
        return b"jsonrpc" in body.lower()

    UV_RELEASE = {
        ("windows", "AMD64"): "uv-x86_64-pc-windows-msvc.zip",
        ("windows", "ARM64"): "uv-aarch64-pc-windows-msvc.zip",
        ("mac", "arm64"): "uv-aarch64-apple-darwin.tar.gz",
        ("mac", "x86_64"): "uv-x86_64-apple-darwin.tar.gz",
        ("linux", "x86_64"): "uv-x86_64-unknown-linux-gnu.tar.gz",
        ("linux", "aarch64"): "uv-aarch64-unknown-linux-gnu.tar.gz",
    }

    def download_uv(self) -> str:
        """下载 standalone uv/uvx 到 APP_DIR/bin —— uv 是独立二进制，不依赖本机 Python。"""
        name = self.UV_RELEASE.get((OS_NAME, platform.machine()))
        if not name:
            return ""
        url = "https://github.com/astral-sh/uv/releases/latest/download/" + name
        bindir = self.UV_DIRS[0]
        os.makedirs(bindir, exist_ok=True)
        log(f"[wmcp] 下载 uv: {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "RemoteKit"})
        with urllib.request.urlopen(req, timeout=300) as r:
            data = r.read()
        want = {"uv", "uvx", "uv.exe", "uvx.exe"}
        if name.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for n in zf.namelist():
                    base = os.path.basename(n)
                    if base in want:
                        with zf.open(n) as src, open(os.path.join(bindir, base), "wb") as dst:
                            shutil.copyfileobj(src, dst)
        else:
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                for m in tf.getmembers():
                    base = os.path.basename(m.name)
                    if base in want:
                        src = tf.extractfile(m)
                        if src:
                            with open(os.path.join(bindir, base), "wb") as dst:
                                shutil.copyfileobj(src, dst)
        for n in want:
            p = os.path.join(bindir, n)
            if os.path.isfile(p):
                os.chmod(p, 0o755)
        return self.find_uv()

    def install(self) -> None:
        """uv tool install windows-mcp；没有 uv 就先下载 standalone uv（uv 会自带 Python 3.14）。"""
        self.installing = True
        self.state = "installing"
        try:
            uv = self.find_uv()
            if not uv:
                uv = self.download_uv()
            if not uv:
                py = detect_python()
                if not py:
                    raise RuntimeError("没有找到 uv，自动下载失败，请手动安装：https://docs.astral.sh/uv/")
                log(f"[wmcp] uv 下载失败，改用 pip 安装 uv ({py})")
                subprocess.run([py, "-m", "pip", "install", "-q", "uv"], capture_output=True,
                               timeout=600, **hidden_popen_kwargs())
                uv = self.find_uv()
                if not uv:
                    r = subprocess.run([py, "-c", "import sysconfig;print(sysconfig.get_path('scripts'))"],
                                       capture_output=True, timeout=15, **hidden_popen_kwargs())
                    cand = os.path.join(decode_output(r.stdout).strip(), "uv.exe")
                    if os.path.isfile(cand):
                        uv = cand
                if not uv:
                    raise RuntimeError("uv 安装失败，请手动安装 uv 后重试")
            log("[wmcp] uv tool install windows-mcp ...")
            r = subprocess.run([uv, "tool", "install", "windows-mcp"], capture_output=True,
                               timeout=1800, **hidden_popen_kwargs())
            if r.returncode != 0:
                raise RuntimeError("uv tool install 失败: " + decode_output(r.stderr)[-600:])
            log("[wmcp] 安装完成")
            self.state = "stopped"
        except Exception as exc:  # noqa: BLE001
            self.state = "error"
            self.error = f"安装失败: {exc}"
            log("[wmcp] " + self.error)
        finally:
            self.installing = False

    def start(self) -> None:
        if not (self.cfg.get("wmcp") or {}).get("enabled") or not IS_WIN:
            self.state = "disabled"
            return
        threading.Thread(target=self._start_impl, daemon=True).start()

    def _start_impl(self) -> None:
        with self._lock:
            try:
                self.exe = self.find_exe()
                uvx = self.find_uvx()
                if not self.exe and not uvx:
                    if (self.cfg.get("wmcp") or {}).get("auto_install", True):
                        self.install()
                        self.exe = self.find_exe()
                        uvx = self.find_uvx()
                    if not self.exe and not uvx:
                        self.state = "error"
                        self.error = self.error or "未找到 windows-mcp，请先安装 uv 或 Python"
                        return
                if self.ping():
                    self.state = "online"
                    self.error = ""
                    log(f"[wmcp] 端口 {self.port} 已有服务在跑，直接复用")
                    return
                serve = ["serve", "--transport", "streamable-http",
                         "--host", "127.0.0.1", "--port", str(self.port)]
                argv = ([self.exe] if self.exe else [uvx, "windows-mcp"]) + serve
                os.makedirs(LOG_DIR, exist_ok=True)
                out = open(os.path.join(LOG_DIR, "wmcp.log"), "ab")
                env = dict(os.environ)
                env["PYTHONIOENCODING"] = "utf-8"
                self.state = "starting"
                self.proc = spawn_child(argv, cwd=APP_DIR, stdout=out, stderr=subprocess.STDOUT, env=env)
                log(f"[wmcp] 已启动 pid={self.proc.pid}")
                for _ in range(120):
                    time.sleep(1)
                    if self.proc.poll() is not None:
                        self.state = "error"
                        self.error = f"windows-mcp 进程退出 code={self.proc.returncode}，见 logs/wmcp.log"
                        log("[wmcp] " + self.error)
                        return
                    if self.ping():
                        self.state = "online"
                        self.error = ""
                        log("[wmcp] 就绪")
                        return
                self.state = "error"
                self.error = "windows-mcp 120 秒内未就绪（uvx 首次运行要下载 Python 3.14，可再点启动），见 logs/wmcp.log"
            except Exception as exc:  # noqa: BLE001
                self.state = "error"
                self.error = f"{exc}"
                log("[wmcp] 启动异常: " + traceback.format_exc()[-800:])

    def stop(self) -> None:
        if self.proc:
            kill_proc(self.proc)
            self.proc = None
        pid = pid_listening(self.port)
        if pid:
            kill_pid(pid)
        self.state = "stopped" if (self.cfg.get("wmcp") or {}).get("enabled") else "disabled"

    def status(self) -> dict:
        if IS_WIN and self.state not in ("disabled", "installing"):
            if self.ping():
                if self.state in ("online", "starting", "stopped", "error"):
                    self.state = "online"
            elif self.state == "online":
                self.state = "stopped"
        return {"state": self.state, "error": self.error, "exe": self.exe or self.find_exe(),
                "port": self.port, "installing": self.installing}


# --------------------------------------------------------------------------- HTTP 服务
class App:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.port = int(cfg["port"])
        self.token = cfg["token"]
        self.files = FileService(cfg.get("root", "~"))
        self.tunnel = TunnelManager(cfg, self.port)
        self.coagent = CoAgentManager(cfg, self.token)
        self.wmcp = WindowsMCPManager(cfg)
        self.started = time.time()

    def connection_info(self) -> dict:
        base = self.tunnel.public_url or f"http://127.0.0.1:{self.port}"
        return {
            "name": f"RemoteKit@{platform.node()}",
            "os": OS_NAME,
            "tunnel_url": base,
            "token": "Bearer " + self.token,
            "api_ping": base + "/api/ping",
            "api_files": base + "/api/list?path=.",
            "api_exec": base + "/api/exec",
            "api_search": base + "/api/search?q=...&path=.",
            "api_batch": base + "/api/batch",
            "api_screenshot": base + "/api/screenshot",
            "coagent": (base + "/coagent") if self.coagent.state == "online" else "",
            "windows_mcp": (base + "/mcp") if self.wmcp.state == "online" else "",
            "knowledge": load_knowledge(),
            "workspace": self.files.root,
        }

    def status(self) -> dict:
        return {
            "ok": True, "version": VERSION, "os": OS_NAME, "host": platform.node(),
            "port": self.port, "root": self.files.root, "uptime": int(time.time() - self.started),
            "tunnel": self.tunnel.status(), "coagent": self.coagent.status(),
            "wmcp": self.wmcp.status(),
            "connection": self.connection_info(),
            "config": {k: v for k, v in self.cfg.items() if k != "token"},
        }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    app: App = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # noqa: D401
        pass

    # ---- helpers
    def reply(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def reply_raw(self, data: bytes, ctype: str, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def bearer(self) -> str:
        h = self.headers.get("Authorization", "")
        if h.startswith("Bearer "):
            return h[7:].strip()
        return self.query().get("token", "")

    def authed(self) -> bool:
        if secrets.compare_digest(self.bearer(), self.app.token):
            return True
        self.reply({"ok": False, "error": "unauthorized"}, 401)
        return False

    def query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def raw_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # ---- routing
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self.serve_dashboard()
            if path == "/api/ping":
                return self.reply({"ok": True, "os": OS_NAME, "root": self.app.files.root,
                                   "port": self.app.port, "version": VERSION, "agent": "RemoteKit"})
            if path.startswith("/coagent/") or path == "/coagent":
                return self.proxy_coagent()
            if path == "/mcp" or path.startswith("/mcp/"):
                return self.proxy_wmcp()
            if not self.authed():
                return None
            q = self.query()
            if path == "/api/status":
                return self.reply(self.app.status())
            if path == "/api/connection":
                return self.reply(self.app.connection_info())
            if path == "/api/logs":
                return self.reply({"ok": True, "lines": tail_log(int(q.get("n", "200")))})
            if path == "/api/read":
                return self.reply({"ok": True, **self.app.files.read(q.get("path", ""))})
            if path == "/api/download":
                p = self.app.files.safe(q.get("path", ""))
                with open(p, "rb") as f:
                    return self.reply_raw(f.read(), "application/octet-stream")
            if path in ("/api/list", "/api/files"):
                return self.reply({"ok": True, **self.app.files.list(q.get("path", "."))})
            if path == "/api/search":
                return self.reply({"ok": True, **self.app.files.search(
                    q.get("q", ""), q.get("path", "."), q.get("glob", ""),
                    q.get("ci") == "1", q.get("fixed") == "1", int(q.get("max", "200")))})
            if path == "/api/knowledge":
                return self.reply({"ok": True, "content": load_knowledge()})
            if path == "/api/screenshot":
                png = take_screenshot()
                if q.get("format") == "base64":
                    return self.reply({"ok": True, "png_base64": base64.b64encode(png).decode()})
                return self.reply_raw(png, "image/png")
        except Exception as exc:  # noqa: BLE001
            return self.reply({"ok": False, "error": str(exc)}, 500)
        return self.reply({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/coagent/") or path == "/coagent":
            return self.proxy_coagent()
        if path == "/mcp" or path.startswith("/mcp/"):
            return self.proxy_wmcp()
        if not self.authed():
            return None
        try:
            data = self.body()
            app = self.app
            if path == "/api/exec":
                return self.reply(app.files.exec(data.get("cmd", ""), int(data.get("timeout", 120000)),
                                                 data.get("cwd", ""), data.get("shell", "")))
            if path == "/api/batch":
                ops = data.get("ops") or []
                return self.reply({"ok": True, "results": [app.files.run_op(o) for o in ops[:200]]})
            if path == "/api/write":
                return self.reply({"ok": True, **app.files.write(data["path"], data.get("content", ""),
                                                                 data.get("contentBase64", ""))})
            if path == "/api/tunnel/restart":
                threading.Thread(target=app.tunnel.start, daemon=True).start()
                return self.reply({"ok": True})
            if path == "/api/tunnel/stop":
                app.tunnel.stop()
                return self.reply({"ok": True})
            if path == "/api/tunnel/authtoken":
                return self.reply(app.tunnel.set_authtoken(data.get("provider", ""), data.get("authtoken", "")))
            if path == "/api/coagent/start":
                app.coagent.start()
                return self.reply({"ok": True})
            if path == "/api/coagent/stop":
                app.coagent.stop()
                return self.reply({"ok": True})
            if path == "/api/coagent/install":
                threading.Thread(target=app.coagent.install, daemon=True).start()
                return self.reply({"ok": True})
            if path == "/api/wmcp/start":
                app.wmcp.start()
                return self.reply({"ok": True})
            if path == "/api/wmcp/stop":
                app.wmcp.stop()
                return self.reply({"ok": True})
            if path == "/api/wmcp/install":
                threading.Thread(target=app.wmcp.install, daemon=True).start()
                return self.reply({"ok": True})
            if path == "/api/knowledge":
                save_knowledge(str(data.get("content", "")))
                return self.reply({"ok": True})
            if path == "/api/knowledge/reset":
                save_knowledge(DEFAULT_KNOWLEDGE)
                return self.reply({"ok": True, "content": DEFAULT_KNOWLEDGE})
            if path == "/api/config":
                allowed = {"root", "open_browser", "tunnel", "coagent", "wmcp"}
                patch = {k: v for k, v in data.items() if k in allowed}
                app.cfg.update(deep_merge(app.cfg, patch))
                save_config(app.cfg)
                if "root" in patch:
                    app.files = FileService(app.cfg["root"])
                return self.reply({"ok": True, "config": {k: v for k, v in app.cfg.items() if k != "token"}})
            if path == "/api/open":
                webbrowser.open(f"http://127.0.0.1:{app.port}/?token={app.token}")
                return self.reply({"ok": True})
        except Exception as exc:  # noqa: BLE001
            return self.reply({"ok": False, "error": str(exc)}, 500)
        return self.reply({"ok": False, "error": "not found"}, 404)

    do_PUT = do_POST
    do_DELETE = do_POST

    # ---- dashboard
    def serve_dashboard(self):
        try:
            with open(resource_path("dashboard.html"), "rb") as f:
                html = f.read()
        except OSError:
            html = b"<h1>RemoteKit</h1><p>dashboard.html missing</p>"
        self.reply_raw(html, "text/html; charset=utf-8")

    # ---- CoAgent 反向代理
    def proxy_coagent(self):
        if not self.authed():
            return None
        target_path = self.path[len("/coagent"):] or "/"
        body = self.raw_body() if self.command in ("POST", "PUT", "DELETE") else None
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "content-length", "connection", "accept-encoding")}
        headers["Authorization"] = "Bearer " + self.app.token
        if body is not None:
            headers["Content-Length"] = str(len(body))
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.app.coagent.port, timeout=180)
            conn.request(self.command, target_path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            conn.close()
        except Exception as exc:  # noqa: BLE001
            self.reply({"ok": False, "error": f"CoAgent 不可达: {exc}", "coagent": self.app.coagent.status()}, 502)

    # ---- Windows-MCP 反向代理（streamable-http，SSE 流式转发，路径 1:1 透传）
    def proxy_wmcp(self):
        if not self.authed():
            return None
        body = self.raw_body() if self.command in ("POST", "PUT", "DELETE") else None
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "content-length", "connection", "accept-encoding")}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        responded = False
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.app.wmcp.port, timeout=300)
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            sent_len = None
            out_headers = []
            for k, v in resp.getheaders():
                lk = k.lower()
                if lk == "content-length":
                    sent_len = v
                    continue
                if lk in ("transfer-encoding", "connection"):
                    continue
                if lk == "location":
                    v = re.sub(r"^https?://[^/]+", "", v)
                out_headers.append((k, v))
            self.send_response(resp.status)
            responded = True
            for k, v in out_headers:
                self.send_header(k, v)
            if sent_len is not None:
                self.send_header("Content-Length", sent_len)
                self.end_headers()
                remaining = int(sent_len)
                while remaining > 0:
                    chunk = resp.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            else:
                # SSE / chunked：长度未知，流式转发直到上游关闭
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            conn.close()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        except Exception as exc:  # noqa: BLE001
            if not responded:
                self.reply({"ok": False, "error": f"Windows-MCP 不可达: {exc}", "wmcp": self.app.wmcp.status()}, 502)


# --------------------------------------------------------------------------- 主流程
def banner(app: App) -> None:
    print("=" * 62)
    print(f"  RemoteKit v{VERSION}  ({OS_NAME})   本地: http://127.0.0.1:{app.port}")
    print(f"  Token : {app.token}")
    print(f"  工作区: {app.files.root}")
    print(f"  配置  : {CONFIG_PATH}")
    print("  控制面板会自动在浏览器打开；关闭本窗口即停止全部服务。")
    print("=" * 62)


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        log(f"[http] {type(exc).__name__}: {exc}")


def main() -> None:
    if IS_WIN:
        try:
            os.system("chcp 65001 >nul")
            os.system("title RemoteKit")
        except Exception:  # noqa: BLE001
            pass
    cfg = load_config()
    app = App(cfg)
    Handler.app = app

    # 清理本工具上次遗留的实例（同端口）
    pid = pid_listening(app.port)
    if pid and pid != os.getpid():
        try:
            st, body = http_get(f"http://127.0.0.1:{app.port}/api/ping", timeout=2)
            if b"RemoteKit" in body:
                log(f"上一实例仍在运行 (pid {pid})，先结束它")
                kill_pid(pid)
                time.sleep(1)
        except Exception:  # noqa: BLE001
            pass

    server = QuietServer(("127.0.0.1", app.port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    banner(app)
    log(f"HTTP 服务已启动 127.0.0.1:{app.port}")

    def cleanup():
        app.tunnel.stop()
        if app.coagent.proc:
            kill_proc(app.coagent.proc)
        if app.wmcp.proc:
            kill_proc(app.wmcp.proc)

    atexit.register(cleanup)

    threading.Thread(target=app.tunnel.start, daemon=True).start()
    app.coagent.start()
    app.wmcp.start()

    if cfg.get("open_browser", True):
        try:
            webbrowser.open(f"http://127.0.0.1:{app.port}/?token={app.token}")
        except Exception:  # noqa: BLE001
            pass

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出")


if __name__ == "__main__":
    main()
