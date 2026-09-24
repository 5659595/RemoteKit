# RemoteKit

> 一个文件、一个端口、一条隧道 —— 把整台电脑交给 AI 远程操作。

RemoteKit 是给远程 AI Agent（Devin 等）准备的"电脑遥控器"：在被控机上**双击运行**，
自动拉起内网穿透隧道，把文件读写 / 命令执行 / 桌面控制 / 截屏暴露成一个带 Token 鉴权的
HTTPS 入口。面板一键复制连接信息发给 AI，它就能直接搜索代码、改文件、跑命令、点桌面。

```
RemoteKit.exe（单文件，双击即用，目标机零依赖）
├── /api/*        文件读写 / 搜索 / 批量操作 / 执行命令 / 截屏      （Windows / Mac / Linux）
├── /coagent/*    反向代理到 Hermes CoAgent：鼠标键盘、UIA 控件、浏览器 （仅 Windows）
├── /mcp          反向代理到官方 windows-mcp：UIA 控件树识别 + 键鼠，不依赖截图（仅 Windows）
├── 隧道管理       自动识别并拉起 cpolar 或 ngrok，解析出公网地址
├── 知识库         knowledge.md：随连接信息一起发给 AI 的操作规范（面板可编辑）
├── 安全拦截       /api/exec 服务端硬拦截格式化、删系统目录、关机、git push 等危险命令
└── HTML 控制面板  启动后自动在浏览器打开，显示公网地址 / Token / 状态，一键复制给 Devin
```

所有接口只认一个 Token（`Authorization: Bearer <token>`），公网只需暴露 RemoteKit 一个端口。

## 特性

- **零依赖单文件**：PyInstaller 打包，内嵌 Python 运行时，拷到任何机器双击即跑
- **双桌面控制通道**：
  - `/mcp` → 官方 [windows-mcp](https://github.com/CursorTouch/Windows-MCP)，走 UIA 控件树，
    `Snapshot` 直接返回控件名+坐标，不用截图，动作间延迟约 0.2~0.5s
  - `/coagent/*` → Hermes CoAgent（REST），鼠标键盘 / 浏览器自动化
- **全自动安装链**：点"下载安装"自动下载 standalone uv（独立二进制，不要 Python），
  uv 再自动带 Python 3.14 装 windows-mcp —— 目标机什么环境都不用准备
- **内网穿透**：自动识别 cpolar / ngrok，面板里粘 authtoken 一键登录，出公网地址
- **知识库**：可编辑的操作规范随连接信息一起发给 AI（安全红线、常用路径、软件说明）
- **安全防护**：Token 鉴权 + 文件接口限制在工作区内 + exec 危险命令硬拦截
- **给 AI 优化的接口**：`/api/search` 即时全仓搜索、`/api/batch` 多操作合并一次往返

## 快速开始

### Windows

1. 把 `RemoteKit.exe` 拷到任意文件夹（比如 `D:\RemoteKit\`），**双击运行**。
   - 第一次运行会在同目录生成 `remotekit.json`（里面有随机 Token）和 `logs\`。
   - 浏览器会自动打开控制面板 `http://127.0.0.1:8765/`。
2. **隧道**：面板左上角"公网隧道"卡片。
   - 电脑上已装 cpolar（`C:\Program Files\cpolar\cpolar.exe`）或 ngrok → 自动使用，几秒后出公网地址。
   - 没装 → 展开"隧道设置 / 首次安装说明"，按提示装 cpolar（国内推荐）或 ngrok，把 authtoken 粘进去点"登录"即可（只需一次）。
3. **桌面控制**（可选，想让 AI 点鼠标 / 操作软件才需要）：
   - **Windows-MCP** 卡片（推荐）：官方 MCP 桌面控制服务，走 UIA 控件树（不截图、更快）。
     点"下载安装"即可，MCP 客户端填 `<公网地址>/mcp`。
   - **CoAgent** 卡片：需要本机装有 Python 3.10+（勾选 Add to PATH）。面板点"下载安装"，
     自动从 GitHub 下载 CoAgent 源码、安装依赖和 Playwright 浏览器（几分钟）。
     已有 CoAgent 目录的话把 `remotekit.json` 里 `coagent.dir` 指过去即可。
4. 面板底部"发给 Devin 的连接信息" → **一键复制**，整段粘贴给 Devin，它就能连上这台电脑。

关闭 RemoteKit 的黑色控制台窗口 = 停止全部服务（隧道、CoAgent、Windows-MCP 一起停）。

**开机自启（可选）**：`Win + R` 输入 `shell:startup` 回车，把 `RemoteKit.exe` 的快捷方式拖进去。

### Mac / Linux

Mac 上没有 CoAgent / Windows-MCP（都是 Windows 专用），但文件 / 命令 / 截屏 / 隧道 / 面板
全部可用，通过 `/api/exec` 跑 `osascript`、`screencapture` 等同样能操作桌面。

```bash
# 方式 A：直接跑源码（只需 Python 3.8+，无第三方依赖）
python3 remotekit.py

# 方式 B：打成单文件
python3 build.py        # 产物 dist/RemoteKit；Mac 加 --app 再出 dist/RemoteKit.app
./dist/RemoteKit
```

Mac 隧道 cpolar / ngrok 都行：装好后在面板里粘贴 authtoken 登录；ngrok 有固定域名的
（如 `xxx.ngrok-free.dev`）填到"ngrok 固定域名"，公网地址就不会变。

Mac 截屏要授权：**系统设置 → 隐私与安全性 → 屏幕录制**，勾选 `RemoteKit`（跑源码的话勾选终端），
然后重启本程序，否则 `/api/screenshot` 返回的是全黑图。

## 配置文件 `remotekit.json`

```jsonc
{
  "port": 8765,              // 本机监听端口
  "token": "…",              // 唯一的访问口令，泄露了就改一个然后重启
  "root": "~",               // 远程可访问的根目录（文件接口/命令 cwd 都被限制在此目录内）
  "open_browser": true,      // 启动时自动打开面板
  "tunnel": {
    "provider": "auto",      // auto | cpolar | ngrok | none
    "ngrok_domain": ""       // ngrok 固定域名，可选
  },
  "coagent": {
    "enabled": true,         // 非 Windows 上自动为 false
    "dir": "",               // 留空则自动找：程序目录\Hermes-CoAgent、%LOCALAPPDATA%\Hermes CoAgent
    "port": 9123,
    "python": "",            // 留空自动找 python
    "auto_install": true     // 找不到 CoAgent 时自动下载安装
  },
  "wmcp": {
    "enabled": true,         // 非 Windows 上自动为 false；与 CoAgent 可同时跑
    "port": 9124,            // 本机 windows-mcp 端口，对外是 <公网地址>/mcp
    "exe": "",               // 留空自动找 windows-mcp.exe / uvx
    "auto_install": true     // 找不到时自动 uv tool install windows-mcp（uv 会自带 Python 3.14）
  }
}
```

面板里改的设置会实时写回这个文件。

## 接口速查（给 AI 看的）

所有请求带 `Authorization: Bearer <token>`，路径为相对 `root` 的相对路径。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ping` | 存活探测（不需要 Token） |
| GET | `/api/status` | 隧道 / CoAgent / Windows-MCP / 系统状态 |
| GET | `/api/list?path=.` | 列目录 |
| GET | `/api/read?path=a/b.txt` | 读文本（二进制返 `contentBase64`） |
| GET | `/api/download?path=…` | 下载原始文件 |
| GET | `/api/search?q=<正则>&path=.&glob=*.py&ci=1&fixed=1&max=200` | 全文搜索 |
| GET | `/api/screenshot[?format=base64]` | 整屏截图 PNG |
| GET/POST | `/api/knowledge` | 读 / 写知识库（knowledge.md），随连接信息发给 AI |
| POST | `/api/write` `{path, content \| contentBase64}` | 写文件 |
| POST | `/api/exec` `{cmd, cwd?, timeout_ms?, shell?}` | 执行命令（危险命令会被拦截） |
| POST | `/api/batch` `{ops:[{op:read\|write\|delete\|mkdir\|list, path, content?}]}` | 批量操作，一次往返 |
| ANY | `/coagent/<CoAgent 原路径>` | 透传到 CoAgent，如 `/coagent/screen/jpeg`、`/coagent/mouse/click`、`/coagent/browser/navigate` |
| ANY | `/mcp` | 透传到 windows-mcp（streamable-http），在 MCP 客户端里配 `<公网地址>/mcp` + Bearer Token |

## 安全说明

- **Token 鉴权**：所有 `/api/*`、`/coagent/*`、`/mcp` 都校验 `Bearer <token>`，面板和日志除外（仅本机访问）。
- **工作区隔离**：文件接口（read/write/delete/list/search/download）和 exec 的 cwd 都被限制在 `root` 以内，路径逃逸直接拒绝。
- **危险命令拦截**：exec 服务端硬拦截格式化/分区、关机重启、递归删除系统路径、杀系统进程、
  删注册表、改账号、`git push`/`reset --hard`/`clean -f`、`rm -rf /` 等。注意这不是沙箱——
  shell 命令本身仍可访问全机，黑名单是兜底，Token 保密才是根本。
- **知识库红线**：默认 knowledge.md 里写明了"不许碰系统目录、不许推 Git、删文件先确认"，
  随连接信息一起发给 AI，作为第一层约束。

## 自己重新打包

```bash
python build.py          # Windows 出 dist\RemoteKit.exe；Mac/Linux 出 dist/RemoteKit
python build.py --app    # macOS 额外产出 dist/RemoteKit.app（Finder 双击，无控制台窗口）
```

打 Windows / Linux 包可以用 `.github/workflows/build.yml`：推到 GitHub 后
Actions 会同时产出三个平台的单文件（PyInstaller 不能跨平台编译）。

源码仅三个文件：`remotekit.py`（全部逻辑，纯标准库）+ `dashboard.html`（面板）+ `build.py`（打包）。

## License

MIT
