#!/bin/sh
# ============================================================================
#  Zotero PDF2zh Server 便携包启动脚本（Linux / macOS）
#  需要与 runtime/、site-packages/、server.py 位于同一目录。
#  用法：./start_server.sh [server.py 的参数]
# ============================================================================
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$DIR"

if [ ! -x "$DIR/runtime/bin/python3" ]; then
    echo "[错误] 未找到可执行的 runtime/bin/python3。" >&2
    echo "       当前目录不是完整的 Server 便携包，或解压时丢失了 runtime 目录。" >&2
    exit 1
fi
if [ ! -f "$DIR/server.py" ]; then
    echo "[错误] 未找到 server.py，当前目录不是 Server 便携包根目录。" >&2
    exit 1
fi

PYTHONPATH="$DIR/site-packages:$DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
export PYTHONUTF8=1
export PYTHONUNBUFFERED=1
export PYTHONIOENCODING=utf-8

echo "[启动] 运行时: $DIR/runtime/bin/python3"
echo "[启动] 依赖目录: $DIR/site-packages"
echo

exec "$DIR/runtime/bin/python3" "$DIR/server.py" "$@"