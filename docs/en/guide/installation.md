# Installation Guide

This guide applies to Zotero PDF2zh **v1.1.0**. The Server is now an extract-and-run portable package: `server.zip` bundles a Python 3.12 runtime and all dependencies, so **normal users do not need Python, uv or conda**.

::: tip Before you begin
- **Zotero 7 / 8 / 9 / 10**
- **Server portable package `server.zip`** (download from Releases)
- Python / uv / conda are only needed for `pdf2zh_next` or source mode (see [Advanced: run from source](#source-mode))
:::

::: warning Extract completely
`server.zip` is about 296MB with 21,244 entries. Wait until extraction finishes before launching; missing `runtime/` or `site-packages/` causes startup or translation failures.
:::

## 1. Download the Server portable package

Download `server.zip` from the latest GitHub Release. Do not use a static zip from the repository `main` branch or a source archive:

```text
https://github.com/BluesilveEmperor/PDFTranslate-Zotero/releases/latest/download/server.zip
```

Extract it into an empty folder (preferably a non-system drive), for example `D:\zotero-pdf2zh\server`:

```shell
# macOS / Linux
mkdir -p ~/zotero-pdf2zh/server
unzip server.zip -d ~/zotero-pdf2zh/server
```

On Windows, right-click `server.zip` → **Extract All…** → choose the target folder.

## 2. Check the folder layout

The zip has no outer `server/` prefix, so files land directly in the target folder. The layout should be:

```text
D:\zotero-pdf2zh\server\
├── start_server.bat        # Windows launcher
├── start_server.sh         # Linux / macOS launcher
├── server.py
├── runtime\                 # bundled Python 3.12 (~22MB)
├── site-packages\           # all dependencies (~813MB extracted)
├── config\
└── ...
```

**Verify the extraction**:

- Windows: `runtime\python.exe` exists;
- Linux / macOS: `runtime/bin/python3` exists and is executable;
- `site-packages\` is not empty;
- If you see `server/server/`, move the inner content up one level.

## 3. Start the Server

**Windows**

Double-click `start_server.bat`. To pass options, open `cmd` in the package root:

```bat
start_server.bat --port 9999
```

**Linux / macOS**

```shell
cd ~/zotero-pdf2zh/server
chmod +x start_server.sh
./start_server.sh
```

The launcher sets `PYTHONPATH` to the bundled `site-packages/` and runs `server.py` with the bundled Python. These lines confirm the portable runtime is active:

```text
📦 便携模式: 使用内置 Python 运行时
   Python: <package dir>\runtime\python.exe
```

::: warning Keep the window open
Translation depends on this process. Do not close the window while translating.
:::

## 4. Verify the service

Default address:

```text
http://127.0.0.1:8890
```

Health check:

```shell
curl http://127.0.0.1:8890/health
```

It returns something like `{"status":"ok",...}`. Open `/api/config`; `"bundled": true` means the bundled runtime is in use. The port follows your `--port` value (default `8890`); the release verification used `--port 8899`, so keep the plugin URL on the same port.

The first cold start takes about **7.8 seconds** to answer `/health` with 200; later warm starts take about **4 seconds**.

The Server listens on `127.0.0.1` by default. Only expose it when other devices must reach it:

```shell
./start_server.sh --host 0.0.0.0
```

::: warning Remote access
`--host 0.0.0.0` exposes the service to other devices on the current network. Use it only when you understand the firewall/network implications.
:::

## Portable package notes

| Item | Value / description |
|---|---|
| Download size | about **296MB** |
| Extracted size | `site-packages/` about **813MB**, `runtime/` about **22MB** |
| Zip entries | **21,244** |
| Extraction time | about **85 seconds** (typically 1–2 minutes) |
| First cold start to `/health` 200 | about **7.8 seconds** |
| Second warm start | about **4 seconds** |
| Bundled engine | `pdf2zh` fork **1.9.12** |
| Not bundled | `pdf2zh_next` |

- **The first real translation still needs the network**: babeldoc model/font assets are not pre-seeded; the first translation downloads them and may stall on a restricted network. Later translations reuse the local cache.
- **`pdf2zh_next` is not bundled**: configure an environment via [Advanced: run from source](#source-mode) or use the [standalone exe](#standalone-exe).
- **Platform-bound**: `runtime/` is platform-specific. Use the official Releases package on the platform it was built for; to run on Linux/macOS, build the package on that platform (the code supports `runtime/bin/python3`, but the published package must be built per platform; cross-platform runs have not been verified).
- **Not a Docker package**: Docker deployment is documented in the repo [docker/](https://github.com/BluesilveEmperor/PDFTranslate-Zotero/tree/main/docker) and [docker2/](https://github.com/BluesilveEmperor/PDFTranslate-Zotero/tree/main/docker2) folders.

## Troubleshooting

### `start_server.bat` flashes and closes

1. Open `cmd` in that folder and run `start_server.bat` to read the full error.
2. `[ERROR] runtime\python.exe not found.`: incomplete extraction or wrong level; make sure `runtime\python.exe` sits next to the launcher.
3. `[ERROR] server.py not found.`: you are not in the package root.
4. No output at all: check whether antivirus software quarantined `runtime\python.exe`.

### Port already in use

The terminal prints `❌ 端口 8890 已被占用！`. Start on another port and change the plugin URL accordingly:

```bat
start_server.bat --port 9999
```

```shell
./start_server.sh --port 9999
```

### Antivirus interference

Add the whole Server folder to the trusted/allowed list and re-extract; do not restore only individual quarantined files.

### Interrupted extraction

`runtime/` or `site-packages/` may be incomplete. Delete the folder and extract again; do not extract only part of the package.

## 5. What happens on the first translation?

The portable package already bundles `pdf2zh` and its runtime dependencies. There is no `pdf2zh_next` install and no `update_packages.py` step.

The first real translation still downloads babeldoc model/font assets; later translations reuse the local cache. On a restricted network, confirm GitHub and the related asset servers are reachable first.

## 6. Install the Zotero plugin

Latest XPI (v1.1.0):

```text
https://github.com/BluesilveEmperor/PDFTranslate-Zotero/releases/latest/download/zotero-pdf-2-zh.xpi
```

Steps:

1. Zotero → **Tools → Add-ons**;
2. drag the downloaded `.xpi` into the Add-ons window;
3. restart Zotero after installation;
4. open **Tools → PDF2zh Preferences**;
5. keep the Python Server URL at `http://127.0.0.1:8890` unless you intentionally changed it.

## 7. Package updates

**Plugin**: use "Check for updates" in Zotero, or enable auto-update.

**Server portable package**:

- On startup the Server checks GitHub/Gitee and asks when a new version is found; answering `y` downloads `server.zip` from Releases and syncs it in place. Restart the launcher afterwards.
- Manual update: download the new `server.zip` and extract it over the old folder; keep `config/pdf-cache/` and `translated/`.
- The bundled `pdf2zh` updates with the Server package. No `update_packages.py`, uv or conda is involved.

## 8. Default startup parameters

The launcher forwards its arguments to `server.py`:

| Parameter | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Localhost-only by default |
| `--port` | `8890` | Server port |
| `--check_update` | `True` | Check Server updates on startup |
| `--update_source` | `gitee` | Preferred update source; both GitHub and Gitee are tried |
| `--enable_mirror` | `True` | Enable package-download optimization (source / uv / conda mode) |
| `--enable_venv` | `True` | Enable managed translation environments (skipped in portable mode) |
| `--env_tool` | `auto` | Keep existing uv/conda; prefer uv for a fresh environment (skipped in portable mode) |
| `--skip_install` | `False` | Disable automatic environment creation/repair (source mode) |

Examples:

```bat
start_server.bat --port 9999
start_server.bat --check_update=False
```

```shell
./start_server.sh --port 9999
./start_server.sh --check_update=False
./start_server.sh --host 0.0.0.0
```

## 9. Advanced: run from source (uv/conda) {#source-mode}

Only for developing/debugging the Server, non-portable packages, or the `pdf2zh_next` engine.

### 9.1 Prepare

- **Python 3.12**: [download](https://www.python.org/downloads/)
- Get the source: `git clone https://github.com/BluesilveEmperor/PDFTranslate-Zotero.git`, then enter the `server` folder.

### 9.2 With uv

```shell
# macOS / Linux
wget -qO- https://astral.sh/uv/install.sh | sh
# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Verify
uv --version

# Start; uv prepares requirements.txt (downloads packages on first run)
uv run --python 3.12 --with-requirements requirements.txt server.py
```

Do not move or rename `server` after a uv install (environment paths break).

### 9.3 With conda

```shell
# Verify
conda --version

# First time only: create the environment
conda create -n zotero-pdf2zh-server python=3.12 -y
conda activate zotero-pdf2zh-server
# First time only: install Server dependencies
pip install -r requirements.txt
# Start and force conda
python server.py --env_tool=conda
```

On Windows, Conda Python is at the environment root: `<conda>\envs\zotero-pdf2zh-server\python.exe`. uv / venv use `Scripts\python.exe`.

### 9.4 Without a managed environment

Only when you intentionally maintain the Python environment yourself:

```shell
python server.py --enable_venv=False
```

In this mode the Server does not update your system Python environment.

### 9.5 Translation environments in source mode

- The default `env_tool=auto` keeps an existing uv/conda setup; a fresh setup prefers uv.
- On the first `pdf2zh_next` translation, the Server probes download sources, creates/reuses an environment, and installs `pdf2zh_next >= 2.9.0,<3.0.0`.
- To update, run `python update_packages.py` in the `server` folder; do not create staging / backup environments manually.
- Existing uv/conda users are asked once whether to update on startup; press Enter to choose `Y`.

## 10. Windows standalone exe (advanced) {#standalone-exe}

A Windows standalone `pdf2zh_next` executable can also be used:

```shell
python server.py --enable_winexe=True --winexe_path="<path-to-pdf2zh.exe>"
```

The standalone executable must be maintained separately. This mode still needs a local Python to run `server.py`.

## Notes

- Keep the Server process running while translating (for the portable package, do not close the launcher window).
- Do not move `runtime/` or `site-packages/` out of the package folder.
- In source mode, do not use `--no-deps` to force incompatible BabelDOC / PyMuPDF / pdf2zh_next combinations.
- Some malformed PDFs may still trigger upstream BabelDOC / PyMuPDF parser failures; Zotero PDF2zh does not automatically rewrite or rasterize every user PDF.

## Next Steps

- [Configuration](/en/guide/configuration)
- [Translation Options](/en/guide/translation-options)
- [Extra Parameters](/en/guide/extra-params)
- [Translation Environment Updates](/en/guide/package-update)
- [FAQ](/en/guide/faq/)