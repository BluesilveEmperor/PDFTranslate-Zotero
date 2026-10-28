#!/bin/bash
# ============================================================================
#  Zotero PDF2zh Server 便携包构建脚本
#
#  产物：server/dist/server.zip
#      zip 根 = Server 目录内容（无外层 server/ 前缀）
#      ├── server.py / utils/ / tests/ / config/（仅 *.example）
#      ├── index.html / favicon.svg / bo.mp3 / notice.json
#      ├── requirements.txt / manage_packages.py / update_packages.py
#      ├── runtime/          Windows: python.exe + DLLs + Lib；
#      │                     Unix: python-build-standalone（bin/python3）
#      ├── site-packages/    flask/toml/pypdf/PyMuPDF/packaging + pdf2zh fork
#      ├── start_server.bat   Windows 启动器
#      └── start_server.sh    Unix 启动器
#
#  用法：
#      bash server/build_portable.sh            # 增量构建（复用 runtime / 依赖）
#      FORCE=1 bash server/build_portable.sh    # 全量重建 runtime 与 site-packages
#      UPDATE_DEPS=1 bash ...                   # 仅强制重装依赖
#      BUILD_PYTHON=/path/to/python3.12 bash ...# 无 uv 时指定构建解释器（必须 3.12）
#      COMPILE=1 bash ...                       # 额外预编译 .pyc（减小冷启动耗时）
#
#  环境变量：
#      PY_VER         Windows embed 版本，默认 3.12.9
#      PDF2ZH_SPEC    pdf2zh 安装源，默认 fork 的 gh-proxy git 地址
# ============================================================================
set -euo pipefail

SERVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="$SERVER_DIR/dist"
CACHE_DIR="$DIST_DIR/.cache"
STAGE_DIR="$DIST_DIR/server"
RUNTIME_DIR="$STAGE_DIR/runtime"
SITE_DIR="$STAGE_DIR/site-packages"
ZIP_PATH="$DIST_DIR/server.zip"

PY_VER="${PY_VER:-3.12.9}"
PY_EMBED_URL="https://www.python.org/ftp/python/${PY_VER}/python-${PY_VER}-embed-amd64.zip"
PDF2ZH_SPEC="${PDF2ZH_SPEC:-pdf2zh @ git+https://gh-proxy.com/https://github.com/BluesilveEmperor/PDFtranslate.git@main}"
GH_PROXY_PREFIX="https://gh-proxy.com/https://github.com/"
GH_PROXY_UPSTREAM="https://github.com/"
FORK_NAME="pdf2zh"

# 服务端运行必需、必须随 zip 分发的源码 / 静态文件。
SOURCE_FILES=(
    server.py
    notice.json
    requirements.txt
    manage_packages.py
    update_packages.py
    index.html
    favicon.svg
    bo.mp3
)
SOURCE_DIRS=(
    utils
    tests
)
# 启动脚本本身放在 server/ 下（入库），构建时复制进 zip 根。
LAUNCHER_FILES=(
    start_server.bat
    start_server.sh
)

log()  { printf '\n\033[36m[%s]\033[0m %s\n' "$1" "$2"; }
warn() { printf '\033[33m[警告]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }
now()  { date +%s; }

FORCE="${FORCE:-0}"
UPDATE_DEPS="${UPDATE_DEPS:-0}"
COMPILE="${COMPILE:-0}"

# ---------------------------------------------------------------------------
# 平台探测
# ---------------------------------------------------------------------------
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) PLATFORM="windows" ;;
    Linux*)               PLATFORM="linux" ;;
    Darwin*)              PLATFORM="macos" ;;
    *) die "不支持的平台: $(uname -s)" ;;
esac

case "$PY_VER" in
    3.12.*) : ;;
    *) die "Windows embed 版本只支持 3.12.x（当前 PY_VER=$PY_VER），_pth 与 pdf2zh 依赖按 3.12 校验。" ;;
esac

# ---------------------------------------------------------------------------
# 工具查找
# ---------------------------------------------------------------------------
find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    local candidate
    for candidate in \
        "$HOME/.local/bin/uv" "$HOME/.local/bin/uv.exe" \
        "$HOME/AppData/Roaming/uv/uv.exe" \
        "$HOME/.cargo/bin/uv" "$HOME/.cargo/bin/uv.exe"; do
        if [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

find_build_python() {
    if [ -n "${BUILD_PYTHON:-}" ]; then
        [ -x "$BUILD_PYTHON" ] || die "BUILD_PYTHON=$BUILD_PYTHON 不可执行"
        echo "$BUILD_PYTHON"
        return 0
    fi
    local candidate
    for candidate in python python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

file_hash() {
    # 依赖指纹：requirements + fork 地址 + Python 版本。
    local text
    text="$(cat "$SERVER_DIR/requirements.txt"; echo "---"; echo "$PDF2ZH_SPEC"; echo "py=$PY_VER")"
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s' "$text" | sha256sum | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        printf '%s' "$text" | shasum -a 256 | cut -d' ' -f1
    else
        printf '%s' "$text" | "${BUILD_PY:-python}" -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.read().encode()).hexdigest())'
    fi
}

# 让 git / 子模块也走 gh-proxy。保留调用方已有的 GIT_CONFIG_COUNT 条目，
# 只追加一条 insteadOf（value 里的 @main 等不会误伤）。
with_git_proxy() {
    local index="${GIT_CONFIG_COUNT:-0}"
    if ! [[ "$index" =~ ^[0-9]+$ ]]; then
        index=0
    fi
    export GIT_CONFIG_COUNT=$((index + 1))
    export "GIT_CONFIG_KEY_${index}=url.${GH_PROXY_PREFIX}.insteadOf"
    export "GIT_CONFIG_VALUE_${index}=${GH_PROXY_UPSTREAM}"
    export GIT_TERMINAL_PROMPT=0
    "$@"
}

# ---------------------------------------------------------------------------
# 1. runtime
# ---------------------------------------------------------------------------
prepare_runtime_windows() {
    local embed_zip="$CACHE_DIR/python-${PY_VER}-embed-amd64.zip"
    local pth

    if [ "$FORCE" = "1" ]; then
        rm -rf "$RUNTIME_DIR"
    fi

    if [ -f "$RUNTIME_DIR/python.exe" ]; then
        log "runtime" "复用已解压的 Python $PY_VER embed: $RUNTIME_DIR"
    else
        log "runtime" "下载 Python $PY_VER embed（缓存: $embed_zip）"
        mkdir -p "$CACHE_DIR" "$RUNTIME_DIR"
        if [ ! -s "$embed_zip" ]; then
            curl -L --fail --retry 3 --retry-delay 3 \
                -o "$embed_zip.tmp" "$PY_EMBED_URL" \
                || die "下载 embed Python 失败: $PY_EMBED_URL"
            mv "$embed_zip.tmp" "$embed_zip"
        fi
        unzip -q -o "$embed_zip" -d "$RUNTIME_DIR" || die "解压 embed Python 失败"
    fi

    [ -f "$RUNTIME_DIR/python.exe" ] || die "runtime 中缺少 python.exe"

    # Windows embed 存在 *_pth 时进入 isolated 模式，会忽略 PYTHONPATH；
    # 因此必须把 site-packages 与程序根写进 _pth。顺序：zip → runtime → site-packages → 程序根。
    pth="$RUNTIME_DIR/python312._pth"
    [ -f "$pth" ] || pth="$(find "$RUNTIME_DIR" -maxdepth 1 -name 'python*._pth' | head -n 1)"
    [ -n "$pth" ] && [ -f "$pth" ] || die "runtime 中缺少 python*._pth"

    # 整文件重写，避免 CRLF / 重复追加导致 isolated 模式下路径缺失。
    {
        printf 'python312.zip\r\n'
        printf '.\r\n'
        printf '..\\site-packages\r\n'
        printf '..\r\n'
        printf 'import site\r\n'
    } > "$pth"

    log "runtime" "已更新 $pth："
    sed 's/^/    /' "$pth"
}

prepare_runtime_unix() {
    local uv="$1"
    local py_exe prefix

    if [ "$FORCE" = "1" ]; then
        rm -rf "$RUNTIME_DIR"
    fi
    if [ -x "$RUNTIME_DIR/bin/python3" ]; then
        log "runtime" "复用已复制的 Python 便携运行时: $RUNTIME_DIR"
        return 0
    fi

    log "runtime" "用 uv 下载 python-build-standalone 3.12 → runtime/"
    "$uv" python install 3.12
    py_exe="$("$uv" python find 3.12)"
    [ -n "$py_exe" ] || die "uv 未能提供 Python 3.12"
    prefix="$(cd "$(dirname "$(dirname "$py_exe")")" && pwd)"

    mkdir -p "$RUNTIME_DIR"
    # -L 实体化 uv 管理的符号链接，保证脱离本机后仍可用。
    cp -rL "$prefix"/. "$RUNTIME_DIR"/
    [ -x "$RUNTIME_DIR/bin/python3" ] || die "runtime/bin/python3 缺失"
}

# ---------------------------------------------------------------------------
# 2. site-packages
# ---------------------------------------------------------------------------
deps_ready() {
    local stamp="$SITE_DIR/.portable-deps.sha256"
    [ -f "$stamp" ] || return 1
    [ "$(cat "$stamp")" = "$(file_hash)" ] || return 1
    [ -d "$SITE_DIR/$FORK_NAME" ] || return 1
    [ -d "$SITE_DIR/flask" ] || return 1
    return 0
}

install_deps_uv() {
    local uv="$1"
    log "deps" "uv pip install → site-packages（优先 uv，Python 3.12 环境）"
    # --python 3.12：uv 会下载/复用受管理的 3.12，保证下到的是 cp312-win_amd64
    # wheel，而不是构建机（3.14 等）的 ABI。
    with_git_proxy "$uv" pip install \
        --python 3.12 \
        --target "$SITE_DIR" \
        --link-mode copy \
        -r "$SERVER_DIR/requirements.txt" \
        "$PDF2ZH_SPEC"
}

install_deps_pip() {
    local build_py="$1"
    local build_version
    build_version="$("$build_py" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"

    if [ "$build_version" != "3.12" ]; then
        die "未找到 uv，且构建机 Python 为 $build_version（需要 3.12）。
  pip --target 会安装构建机 ABI 的二进制 wheel，与 runtime 的 3.12.9 不兼容。
  请任选其一：
    1) 安装 uv：curl -LsSf https://astral.sh/uv/install.sh | sh
    2) BUILD_PYTHON=/path/to/python3.12 bash server/build_portable.sh"
    fi

    log "deps" "uv 不可用，回退 python -m pip install → site-packages（构建机 Python $build_version）"
    with_git_proxy "$build_py" -m pip install \
        --disable-pip-version-check \
        --upgrade \
        --target "$SITE_DIR" \
        -r "$SERVER_DIR/requirements.txt" \
        "$PDF2ZH_SPEC"
}

prepare_site_packages_windows() {
    local uv="$1"
    local build_py="$2"

    if [ "$FORCE" = "1" ] || [ "$UPDATE_DEPS" = "1" ]; then
        log "deps" "清理旧的 site-packages（FORCE=$FORCE UPDATE_DEPS=$UPDATE_DEPS）"
        rm -rf "$SITE_DIR"
    fi

    if deps_ready; then
        log "deps" "复用已安装依赖（指纹一致）: $SITE_DIR"
    else
        mkdir -p "$SITE_DIR"
        if [ -n "$uv" ]; then
            install_deps_uv "$uv"
        else
            install_deps_pip "$build_py"
        fi
        file_hash > "$SITE_DIR/.portable-deps.sha256"
    fi

    # uv --target 会在 site-packages/bin 生成 console script，其 shebang 指向
    # 构建机的受管理 Python 路径，脱离构建机后不可用；便携包统一用
    # runtime python -m pdf2zh，删除以免误导。
    if [ -d "$SITE_DIR/bin" ]; then
        rm -rf "$SITE_DIR/bin"
    fi

    # fork 版 pdf2zh 没有 __main__.py，但便携包依赖 python -m pdf2zh。
    if [ -d "$SITE_DIR/pdf2zh" ] && [ ! -f "$SITE_DIR/pdf2zh/__main__.py" ]; then
        log "deps" "生成 site-packages/pdf2zh/__main__.py（支持 python -m pdf2zh）"
        cat > "$SITE_DIR/pdf2zh/__main__.py" <<'PYEOF'
"""便携运行时的 pdf2zh 入口，支持 ``python -m pdf2zh``。

上游 fork 只声明了 console_scripts（pdf2zh.exe），Windows embed 便携包没有
Scripts 目录，因此补齐模块入口。
"""

import sys

from pdf2zh.pdf2zh import main

if __name__ == "__main__":
    sys.exit(main())
PYEOF
    fi
}

prepare_site_packages_unix() {
    local uv="$1"

    if [ "$FORCE" = "1" ] || [ "$UPDATE_DEPS" = "1" ]; then
        rm -rf "$SITE_DIR"
    fi
    if deps_ready; then
        log "deps" "复用已安装依赖（指纹一致）: $SITE_DIR"
    else
        mkdir -p "$SITE_DIR"
        log "deps" "uv pip install → site-packages（Unix 便携运行时）"
        with_git_proxy "$uv" pip install \
            --python 3.12 \
            --target "$SITE_DIR" \
            --link-mode copy \
            -r "$SERVER_DIR/requirements.txt" \
            "$PDF2ZH_SPEC"
        file_hash > "$SITE_DIR/.portable-deps.sha256"
    fi
    if [ -d "$SITE_DIR/bin" ]; then
        rm -rf "$SITE_DIR/bin"
    fi
    if [ -d "$SITE_DIR/pdf2zh" ] && [ ! -f "$SITE_DIR/pdf2zh/__main__.py" ]; then
        cat > "$SITE_DIR/pdf2zh/__main__.py" <<'PYEOF'
import sys

from pdf2zh.pdf2zh import main

if __name__ == "__main__":
    sys.exit(main())
PYEOF
    fi
}

# ---------------------------------------------------------------------------
# 3. sitecustomize.py（BitStream 兼容层，与主项目 PDFMathTranslate 一致）
# ---------------------------------------------------------------------------
write_sitecustomize() {
    log "sitecustomize" "写入 BitStream 兼容层"
    cat > "$SITE_DIR/sitecustomize.py" <<'PYEOF'
# 由 server/build_portable.sh 生成。
# 部分 pdf2zh / babeldoc 依赖仍按旧 bitstring API 使用 BitStream；
# 新版 bitstring 移除该别名时回退到 BitArray，避免翻译启动即 ImportError。
try:
    from bitstring import BitStream
except ImportError:
    from bitstring import BitArray
    import bitstring
    bitstring.BitStream = BitArray
PYEOF
}

# ---------------------------------------------------------------------------
# 4. 复制 Server 源码 / 静态资源 / 启动器
# ---------------------------------------------------------------------------
stage_sources() {
    log "stage" "复制 Server 源码到 $STAGE_DIR"
    mkdir -p "$STAGE_DIR"
    # 清掉上一次在 dist/server 里实跑留下的用户数据，避免打进发布包
    rm -rf "$STAGE_DIR/translated" "$STAGE_DIR/config/pdf-cache" "$STAGE_DIR/.pytest_cache"
    find "$STAGE_DIR" -maxdepth 2 -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

    local name
    for name in "${SOURCE_FILES[@]}"; do
        [ -f "$SERVER_DIR/$name" ] || die "缺少源文件: $SERVER_DIR/$name"
        cp -f "$SERVER_DIR/$name" "$STAGE_DIR/$name"
    done
    for name in "${SOURCE_DIRS[@]}"; do
        [ -d "$SERVER_DIR/$name" ] || die "缺少源目录: $SERVER_DIR/$name"
        rm -rf "$STAGE_DIR/$name"
        cp -r "$SERVER_DIR/$name" "$STAGE_DIR/$name"
    done
    for name in "${LAUNCHER_FILES[@]}"; do
        [ -f "$SERVER_DIR/$name" ] || die "缺少启动脚本: $SERVER_DIR/$name"
        cp -f "$SERVER_DIR/$name" "$STAGE_DIR/$name"
    done
    chmod +x "$STAGE_DIR/start_server.sh"

    # config 只带 *.example 模板；绝不把用户密钥 / 运行期配置打进 zip。
    rm -rf "$STAGE_DIR/config"
    mkdir -p "$STAGE_DIR/config"
    local example
    for example in "$SERVER_DIR"/config/*.example; do
        [ -f "$example" ] || continue
        cp -f "$example" "$STAGE_DIR/config/"
    done

    # 清理源码目录里的缓存/临时目录，避免把开发机状态打进发布包。
    find "$STAGE_DIR/utils" "$STAGE_DIR/tests" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find "$STAGE_DIR/utils" "$STAGE_DIR/tests" -name '*.pyc' -type f -delete 2>/dev/null || true
    for name in "${SOURCE_FILES[@]}"; do
        rm -f "$STAGE_DIR/$name.orig" "$STAGE_DIR/$name.rej"
    done
}

# ---------------------------------------------------------------------------
# 5. 校验（用便携 runtime 自己导入关键依赖）
# ---------------------------------------------------------------------------
verify_stage_windows() {
    local py="$RUNTIME_DIR/python.exe"
    log "verify" "用便携 runtime 校验关键依赖"
    [ -f "$py" ] || die "runtime 缺少 python.exe"

    ( cd "$STAGE_DIR" && \
      PYTHONPATH="$SITE_DIR;$STAGE_DIR" "$py" -c '
import sys
import flask
import fitz
import pdf2zh
import pypdf
import toml
import packaging
print("    Python:", sys.version.split()[0])
print("    flask:", flask.__version__ if hasattr(flask, "__version__") else "ok")
print("    PyMuPDF:", fitz.__doc__.splitlines()[0] if fitz.__doc__ else "ok")
print("    pdf2zh:", pdf2zh.__file__)
' )
    ( cd "$STAGE_DIR" && PYTHONPATH="$SITE_DIR;$STAGE_DIR" "$py" -m pdf2zh --version ) \
        || die "runtime python -m pdf2zh --version 失败"
}

verify_stage_unix() {
    local py="$RUNTIME_DIR/bin/python3"
    log "verify" "用便携 runtime 校验关键依赖"
    [ -x "$py" ] || die "runtime 缺少 bin/python3"
    ( cd "$STAGE_DIR" && \
      PYTHONPATH="$SITE_DIR:$STAGE_DIR" "$py" -c '
import flask, fitz, pdf2zh, pypdf, toml, packaging
print("pdf2zh:", pdf2zh.__file__)
' )
    ( cd "$STAGE_DIR" && PYTHONPATH="$SITE_DIR:$STAGE_DIR" "$py" -m pdf2zh --version ) \
        || die "runtime python -m pdf2zh --version 失败"
}

compile_bytecode_windows() {
    if [ "$COMPILE" != "1" ]; then
        return 0
    fi
    log "compile" "预编译 site-packages 字节码（COMPILE=1，耗时较长）"
    "$RUNTIME_DIR/python.exe" -m compileall -q "$SITE_DIR" >/dev/null 2>&1 || warn "compileall 部分失败，已忽略"
}

compile_bytecode_unix() {
    if [ "$COMPILE" != "1" ]; then
        return 0
    fi
    log "compile" "预编译 site-packages 字节码（COMPILE=1，耗时较长）"
    "$RUNTIME_DIR/bin/python3" -m compileall -q "$SITE_DIR" >/dev/null 2>&1 || warn "compileall 部分失败，已忽略"
}

# ---------------------------------------------------------------------------
# 6. 打包
# ---------------------------------------------------------------------------
package_zip() {
    log "zip" "打包 $ZIP_PATH"
    rm -f "$ZIP_PATH"
    if command -v zip >/dev/null 2>&1; then
        ( cd "$STAGE_DIR" && zip -q -r "$ZIP_PATH" . )
    else
        local py="${BUILD_PY:-python}"
        ( cd "$STAGE_DIR" && "$py" -m zipfile -c "$ZIP_PATH" . )
    fi
    [ -s "$ZIP_PATH" ] || die "zip 生成失败"
}

fix_zip_unix_modes() {
    # Windows 上的 Info-ZIP 只能记录 FAT 属性，start_server.sh 解压后会丢掉
    # 执行位。这里只原地修改 zip 的 central directory（external_attr 与
    # version-made-by），不重压缩数据，避免破坏 Info-ZIP 写入的 NTFS extra。
    local py="${BUILD_PY:-python}"
    log "zip" "修正 zip 内 Unix 权限位（start_server.sh 可执行）"
    "$py" - "$ZIP_PATH" <<'PYEOF'
import struct
import sys

path = sys.argv[1]
data = bytearray(open(path, "rb").read())

eocd = data.rfind(b"PK\x05\x06")
if eocd < 0:
    sys.exit("zip: EOCD not found")
total = struct.unpack_from("<H", data, eocd + 10)[0]
cd_offset = struct.unpack_from("<I", data, eocd + 16)[0]

pos = cd_offset
for _ in range(total):
    if data[pos:pos + 4] != b"PK\x01\x02":
        sys.exit(f"zip: bad central header at {pos}")
    name_len, extra_len, comment_len = struct.unpack_from("<HHH", data, pos + 28)
    name = bytes(data[pos + 46:pos + 46 + name_len]).decode("utf-8", "replace")
    if name.endswith("/"):
        mode = 0o040755
    elif name == "start_server.sh" or name.startswith("runtime/bin/"):
        mode = 0o100755
    else:
        mode = 0o100644
    data[pos + 5] = 3  # version made by: host system = Unix
    struct.pack_into("<I", data, pos + 38, mode << 16)
    pos += 46 + name_len + extra_len + comment_len

open(path, "wb").write(data)
PYEOF
}

report() {
    local zip_size site_size runtime_size
    zip_size="$(du -h "$ZIP_PATH" | cut -f1)"
    site_size="$(du -sh "$SITE_DIR" 2>/dev/null | cut -f1)"
    runtime_size="$(du -sh "$RUNTIME_DIR" 2>/dev/null | cut -f1)"
    log "完成" "产物: $ZIP_PATH"
    echo "    zip 体积:       $zip_size"
    echo "    site-packages:  $site_size"
    echo "    runtime:        $runtime_size"
    if [ "$PLATFORM" = "windows" ]; then
        echo "    本地验证:"
        echo "      mkdir -p /tmp/pdf2zh-portable && cd /tmp/pdf2zh-portable"
        echo "      unzip -q '$ZIP_PATH' && ./start_server.bat --host 127.0.0.1 --port 8899"
        echo "      curl http://127.0.0.1:8899/health"
    else
        echo "    本地验证:"
        echo "      mkdir -p /tmp/pdf2zh-portable && cd /tmp/pdf2zh-portable"
        echo "      unzip -q '$ZIP_PATH' && ./start_server.sh --host 127.0.0.1 --port 8899"
        echo "      curl http://127.0.0.1:8899/health"
    fi
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
BUILD_STARTED="$(now)"
log "平台" "$PLATFORM"
mkdir -p "$DIST_DIR"

UV="$(find_uv || true)"
BUILD_PY="$(find_build_python || true)"
if [ -n "$UV" ]; then
    log "工具" "uv: $UV"
else
    log "工具" "uv: 未找到（将回退 python -m pip，需要构建机 Python 3.12）"
fi
if [ -n "$BUILD_PY" ]; then
    log "工具" "构建机 Python: $BUILD_PY（$("$BUILD_PY" --version 2>&1 || true)）"
fi

case "$PLATFORM" in
    windows)
        prepare_runtime_windows
        prepare_site_packages_windows "$UV" "$BUILD_PY"
        stage_sources
        write_sitecustomize
        verify_stage_windows
        compile_bytecode_windows
        ;;
    linux|macos)
        [ -n "$UV" ] || die "Unix 便携包构建必须有 uv（python-build-standalone）。"
        prepare_runtime_unix "$UV"
        prepare_site_packages_unix "$UV"
        stage_sources
        write_sitecustomize
        verify_stage_unix
        compile_bytecode_unix
        ;;
esac

package_zip
fix_zip_unix_modes
BUILD_SECONDS=$(( $(now) - BUILD_STARTED ))
report
echo "    构建耗时:       ${BUILD_SECONDS}s"