# 安装指南

本文档适用于 Zotero PDF2zh **v1.1.0**。Server 已改为「解压即用」便携包：`server.zip` 内置 Python 3.12 运行时与全部依赖，**普通用户不需要安装 Python、uv 或 conda**。

::: tip 开始之前
- **Zotero 7 / 8 / 9 / 10**
- **Server 便携包 `server.zip`**（从 Releases 下载）
- 不需要 Python / uv / conda；只有使用 `pdf2zh_next` 或源码运行时才需要（见[高级：源码运行](#source-mode)）
:::

::: warning 解压要完整
`server.zip` 约 296MB、包含 21,244 个条目。请等解压完全结束再启动；缺少 `runtime/` 或 `site-packages/` 会导致启动失败或翻译失败。
:::

## 1. 下载 Server 便携包

请从 GitHub Release 下载 `server.zip`，不要使用仓库 `main` 分支中的静态 zip，也不要用源码归档：

```text
https://github.com/BluesilveEmperor/PDFTranslate-Zotero/releases/latest/download/server.zip
```

解压到空文件夹（建议非系统盘），例如 `D:\zotero-pdf2zh\server`：

```shell
# macOS / Linux
mkdir -p ~/zotero-pdf2zh/server
unzip server.zip -d ~/zotero-pdf2zh/server
```

Windows 可右键 `server.zip` →「全部解压缩」→ 选择目标文件夹。

## 2. 检查目录结构

zip 根目录没有外层 `server/` 前缀，解压后文件直接落在目标文件夹。目录应为：

```text
D:\zotero-pdf2zh\server\
├── start_server.bat        # Windows 启动器
├── start_server.sh         # Linux / macOS 启动器
├── server.py
├── runtime\                 # 内置 Python 3.12（约 22MB）
├── site-packages\           # 全部依赖（解压后约 813MB）
├── config\
└── ...
```

**检查解压完整**：

- Windows：`runtime\python.exe` 存在；
- Linux / macOS：`runtime/bin/python3` 存在且可执行；
- `site-packages\` 不是空目录；
- 若出现 `server/server/` 嵌套，把内层内容上移一层。

## 3. 启动 Server

**Windows**

双击 `start_server.bat`。需要附加参数时，在包根目录打开 `cmd`：

```bat
start_server.bat --port 9999
```

**Linux / macOS**

```shell
cd ~/zotero-pdf2zh/server
chmod +x start_server.sh
./start_server.sh
```

启动脚本会设置 `PYTHONPATH` 指向包内 `site-packages/`，并用内置 Python 运行 `server.py`。看到下列输出说明便携运行时已生效：

```text
📦 便携模式: 使用内置 Python 运行时
   Python: <包目录>\runtime\python.exe
```

::: warning 保持窗口开启
翻译功能依赖该进程运行，使用翻译时不要关闭此窗口。
:::

## 4. 验证服务

默认地址：

```text
http://127.0.0.1:8890
```

健康检查：

```shell
curl http://127.0.0.1:8890/health
```

返回类似 `{"status":"ok",...}`。打开 `/api/config`，`"bundled": true` 表示正在使用内置运行时。端口以启动参数 `--port` 为准（默认 8890）；本次发布验证使用 `--port 8899` 启动，插件地址需与之一致。

首次冷启动到 `/health` 返回 200 约 **7.8 秒**，之后热启动约 **4 秒**。

Server 默认只监听 `127.0.0.1`。只有确定需要让其他设备访问时，才显式使用：

```shell
./start_server.sh --host 0.0.0.0
```

::: warning 远程访问
使用 `--host 0.0.0.0` 会把服务暴露给当前网络中的其他设备。只有在您理解网络/防火墙设置时才这样做。
:::

## 便携包说明

| 项目 | 数值 / 说明 |
|---|---|
| 下载包体积 | 约 **296MB** |
| 解压后体积 | `site-packages/` 约 **813MB**，`runtime/` 约 **22MB** |
| zip 条目数 | **21,244** |
| 解压耗时 | 约 **85 秒**（通常 1~2 分钟） |
| 首次冷启动到 `/health` 200 | 约 **7.8 秒** |
| 二次热启动 | 约 **4 秒** |
| 内置引擎 | `pdf2zh` fork **1.9.12** |
| 未内置 | `pdf2zh_next` |

- **首次真实翻译仍需联网**：便携包未预置 babeldoc 的模型/字体资产，第一次翻译会联网下载；网络受限时可能卡在下载阶段，资产缓存后可复用。
- **`pdf2zh_next` 未内置**：需要该引擎时请使用[高级：源码运行](#source-mode)自行配置环境，或使用 [standalone exe](#standalone-exe)。
- **平台绑定**：`runtime/` 与平台绑定。官方 Releases 的包按其构建平台使用；在 Linux/macOS 上用的包需要在对应平台构建（代码已支持 `runtime/bin/python3`，但发布包须对应平台构建，未做跨平台实跑验证）。
- **不是 Docker 包**：Docker 部署见[Docker 部署](/zh/guide/docker) 或仓库 `docker/`、`docker2/` 目录。

## 故障排查

### `start_server.bat` 闪退

1. 在该目录打开 `cmd` 执行 `start_server.bat`，查看完整报错。
2. `[ERROR] runtime\python.exe not found.`：解压不完整或层级不对，确认 `runtime\python.exe` 与启动脚本同级。
3. `[ERROR] server.py not found.`：当前目录不是包根目录。
4. 完全没有输出：检查杀毒软件是否隔离了 `runtime\python.exe`。

### 端口被占用

终端提示 `❌ 端口 8890 已被占用！`。换端口启动，并同步修改插件地址：

```bat
start_server.bat --port 9999
```

```shell
./start_server.sh --port 9999
```

### 杀毒软件拦截

把整个 Server 文件夹加入信任 / 白名单后重新解压；不要只恢复被隔离的单个文件。

### 解压中断

`runtime/` 或 `site-packages/` 可能不完整。删除目录后重新解压，不要只解压部分文件。

## 5. 第一次翻译时会发生什么？

便携包已经内置 `pdf2zh` 与运行依赖，无需安装 `pdf2zh_next`，也无需 `update_packages.py`。

第一次真正翻译时仍需联网下载 babeldoc 的模型/字体资产；资产缓存到本机后，后续翻译复用缓存。网络受限时请先确认能访问 GitHub/相关资源服务器。

## 6. 安装 Zotero 插件

最新 XPI（v1.1.0）：

```text
https://github.com/BluesilveEmperor/PDFTranslate-Zotero/releases/latest/download/zotero-pdf-2-zh.xpi
```

安装步骤：

1. Zotero →「工具」→「插件」；
2. 将下载的 `.xpi` 拖入插件窗口；
3. 安装后重启 Zotero；
4. 打开「工具 → PDF2zh 首选项」；
5. Python Server IP 保持 `http://127.0.0.1:8890`（除非您自己修改过地址）。

## 7. 包更新

**插件**：在 Zotero 中检查更新，或开启自动更新。

**Server 便携包**：

- 启动时 Server 会检查 GitHub/Gitee，发现新版本时询问；选择 `y` 会下载 Releases 的 `server.zip` 并就地同步，完成后重新启动启动脚本。
- 也可手动下载新 `server.zip` 覆盖解压；保留 `config/pdf-cache/` 与 `translated/`。
- 包内 `pdf2zh` 随 Server 包一起更新，不需要 `update_packages.py`、uv 或 conda。

## 8. 默认启动参数

启动脚本会把参数原样传给 `server.py`：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 默认只允许本机访问 |
| `--port` | `8890` | Server 端口 |
| `--check_update` | `True` | 启动时检查 Server 更新 |
| `--update_source` | `gitee` | 优先更新源；GitHub 与 Gitee 都会尝试 |
| `--enable_mirror` | `True` | 启用 Python 包下载源优化（源码 / uv / conda 模式） |
| `--enable_venv` | `True` | 启用托管翻译环境（便携模式跳过） |
| `--env_tool` | `auto` | 沿用已有 uv/conda；新环境优先 uv（便携模式跳过） |
| `--skip_install` | `False` | 是否禁止自动创建/修复翻译环境（源码模式） |

常见示例：

```bat
start_server.bat --port 9999
start_server.bat --check_update=False
```

```shell
./start_server.sh --port 9999
./start_server.sh --check_update=False
./start_server.sh --host 0.0.0.0
```

## 9. 高级：源码运行（uv/conda） {#source-mode}

仅用于开发 / 调试 Server、使用非便携包，或需要 `pdf2zh_next` 引擎。

### 9.1 准备

- **Python 3.12**：[下载链接](https://www.python.org/downloads/)
- 获取源码：`git clone https://github.com/BluesilveEmperor/PDFTranslate-Zotero.git`，进入 `server` 目录。

### 9.2 使用 uv

```shell
# macOS / Linux
wget -qO- https://astral.sh/uv/install.sh | sh
# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 验证
uv --version

# 启动；uv 会按 requirements.txt 准备依赖（首次联网安装）
uv run --python 3.12 --with-requirements requirements.txt server.py
```

uv 安装后不要移动 / 重命名 `server` 文件夹（会影响环境路径）。

### 9.3 使用 conda

```shell
# 验证
conda --version

# 仅首次：创建环境
conda create -n zotero-pdf2zh-server python=3.12 -y
conda activate zotero-pdf2zh-server
# 仅首次：安装 Server 依赖
pip install -r requirements.txt
# 启动并显式指定 conda
python server.py --env_tool=conda
```

Windows 上 Conda 的 Python 在环境根目录：`<conda>\envs\zotero-pdf2zh-server\python.exe`；uv / venv 才使用 `Scripts\python.exe`。

### 9.4 不使用托管虚拟环境

只有明确知道自己在维护哪个 Python 环境时才建议：

```shell
python server.py --enable_venv=False
```

此模式下 Server 不负责升级您的系统 Python 环境。

### 9.5 源码模式的翻译环境

- 默认 `env_tool=auto` 沿用已有 uv/conda；无旧环境时优先 uv。
- 第一次使用 `pdf2zh_next` 时，Server 会测试下载源、创建/复用环境并安装 `pdf2zh_next >= 2.9.0,<3.0.0`。
- 更新：在 `server` 目录运行 `python update_packages.py`；不要再手工创建 staging / backup 环境。
- 已有 uv/conda 环境的用户启动新版 Server 时会询问一次是否更新，回车选择 `Y` 即可。

## 10. Windows standalone exe（高级） {#standalone-exe}

Windows 用户也可以使用 `pdf2zh_next` 上游提供的 standalone exe，并通过：

```shell
python server.py --enable_winexe=True --winexe_path="<pdf2zh.exe 路径>"
```

standalone exe 的版本需要自行维护。该方式仍需本机 Python 来运行 `server.py`。

## 注意事项

- 翻译期间需要保持 Server 进程运行（便携包：不要关闭启动脚本窗口）。
- 不要把 `runtime/` 或 `site-packages/` 单独移出包目录。
- 源码模式下不要手工使用 `--no-deps` 强装 BabelDOC / PyMuPDF / pdf2zh_next。
- 个别结构异常 PDF 仍可能触发上游 BabelDOC / PyMuPDF 解析问题；本项目不会默认重写或栅格化所有用户 PDF。

## 下一步

- [配置说明](/zh/guide/configuration)
- [翻译选项](/zh/guide/translation-options)
- [额外参数](/zh/guide/extra-params)
- [翻译环境更新](/zh/guide/package-update)
- [常见问题](/zh/guide/faq/)