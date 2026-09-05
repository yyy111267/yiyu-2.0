#!/bin/zsh
# 把 prototype/ 前端同步到 yiyu-agent/web/（后端同域托管目录）。
# 用法：在 以渔2.0/ 目录下执行  ./sync-web.sh
# 提示：web/ 是部署产物，真正要改前端请改 prototype/，再跑本脚本。

set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DST="$SRC/yiyu-agent/web"

rm -rf "$DST"
mkdir -p "$DST"
cp -R "$SRC/prototype/." "$DST/"
echo "已同步 prototype/ → yiyu-agent/web/"
echo "重启后端生效：docker compose up -d --build（或本地重启 uvicorn）"
