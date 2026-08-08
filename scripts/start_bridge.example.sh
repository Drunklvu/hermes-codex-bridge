#!/usr/bin/env bash
# ============================================================
# Bridge start script (Linux/macOS) — TEMPLATE
# ============================================================
# 用法：复制本文件为 start_bridge.sh，按你的环境改下面的路径，然后：
#   chmod +x start_bridge.sh && ./start_bridge.sh
#
# 对应 Windows 版：scripts/start_bridge.example.ps1
# ⚠️ 本脚本是 best-effort 移植，未经 macOS/Linux 实测——欢迎 PR 修正
# ============================================================
set -euo pipefail

# ==== EDIT THESE PATHS ====
PYTHON="${PYTHON:-python3}"                 # Python 3.10+ 解释器
BRIDGE="${BRIDGE:-/path/to/codex_a2a_bridge.py}"
WORKSPACE="${WORKSPACE:-/path/to/workspace}"
PORT="${PORT:-9998}"
STATE_DIR="${STATE_DIR:-$WORKSPACE/tools/.codex-a2a}"
CODEX="${CODEX:-}"                          # 留空则自动从 PATH 找 codex
# ==== END EDIT ====

[ -f "$BRIDGE" ] || { echo "Bridge not found: $BRIDGE" >&2; exit 1; }
[ -d "$WORKSPACE" ] || { echo "Workspace not found: $WORKSPACE" >&2; exit 1; }
command -v "$PYTHON" >/dev/null || { echo "Python not found: $PYTHON" >&2; exit 1; }

mkdir -p "$STATE_DIR"

# 生成/复用桥 token
TOKEN_FILE="$STATE_DIR/bridge.token"
if [ -f "$TOKEN_FILE" ]; then
    TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
else
    TOKEN="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen || openssl rand -hex 32)"
    printf '%s' "$TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
fi

# 生成/复用 inbound token（反向链路）
INBOUND_FILE="$STATE_DIR/inbound.token"
if [ -f "$INBOUND_FILE" ]; then
    INBOUND_TOKEN="$(tr -d '\r\n' < "$INBOUND_FILE")"
else
    INBOUND_TOKEN="$(cat /proc/sys/kernel/random/uuid 2>/dev/null || uuidgen || openssl rand -hex 32)"
    printf '%s' "$INBOUND_TOKEN" > "$INBOUND_FILE"
    chmod 600 "$INBOUND_FILE"
fi

# 端口占用检查（health 确认是本桥再退出）
if curl -sf -m 3 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q codex-a2a-bridge; then
    echo "Bridge already running on :$PORT"
    exit 0
fi

ARGS=(
    --port "$PORT"
    --workspace "$WORKSPACE"
    --state-dir "$STATE_DIR"
    --token-file "$TOKEN_FILE"
    --inbound-token "$INBOUND_TOKEN"
)
[ -n "$CODEX" ] && ARGS+=(--codex "$CODEX")

echo "[bridge] starting on 127.0.0.1:$PORT (python: $PYTHON)"
exec "$PYTHON" "$BRIDGE" "${ARGS[@]}"
