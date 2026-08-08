#!/usr/bin/env bash
# ============================================================
# A2A SDK sidecar 启动脚本 (Linux/macOS)
# ============================================================
# 用途：启动标准 A2A 接入层（默认 :10000），面向第三方标准 A2A agent
# 前提：已安装 a2a-sdk（pip install 'hermes-codex-bridge[a2a]'）
# 默认：不启动（现有桥 :9998 照常工作，本组件是可选增强）
#
# 用法示例：
#   ./start_a2a_sidecar.sh                          # 本地最小（HTTP :10000）
#   ./start_a2a_sidecar.sh --db sidecar_tasks.db --grpc-port 50051 --push
#   ./start_a2a_sidecar.sh --require-token --sidecar-token <你的token>
#
# 对应 Windows 版：start_a2a_sidecar.ps1
# ⚠️ 本脚本是 best-effort 移植，未经 macOS/Linux 实测——欢迎 PR 修正
# ============================================================
set -euo pipefail

PORT="${PORT:-10000}"
DB="${DB:-}"
GRPC_PORT="${GRPC_PORT:-0}"
PUSH="${PUSH:-0}"
REQUIRE_TOKEN="${REQUIRE_TOKEN:-0}"
SIDECAR_TOKEN="${SIDECAR_TOKEN:-}"
BRIDGE_URL="${BRIDGE_URL:-http://127.0.0.1:9998}"
HOST="${HOST:-127.0.0.1}"

# 长参数解析（--db/--grpc-port/--push/--require-token/--sidecar-token/--bridge-url）
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --db) DB="$2"; shift 2 ;;
        --grpc-port) GRPC_PORT="$2"; shift 2 ;;
        --push) PUSH=1; shift ;;
        --require-token) REQUIRE_TOKEN=1; shift ;;
        --sidecar-token) SIDECAR_TOKEN="$2"; shift 2 ;;
        --bridge-url) BRIDGE_URL="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        -h|--help) sed -n '1,30p' "$0"; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
done

# 找 python（优先 sidecar 专用 venv，其次系统）
PYTHON=""
for v in "$(dirname "$0")/.a2a-venv/bin/python" "$(dirname "$0")/../.a2a-venv/bin/python"; do
    [ -x "$v" ] && PYTHON="$v" && break
done
[ -z "$PYTHON" ] && PYTHON="$(command -v python3 || command -v python || true)"
[ -z "$PYTHON" ] && { echo "找不到 python。需要 a2a-sdk 环境：pip install 'hermes-codex-bridge[a2a]'" >&2; exit 1; }

SIDECAR="$(dirname "$0")/a2a_sidecar.py"
[ -f "$SIDECAR" ] || { echo "找不到 a2a_sidecar.py（$SIDECAR）" >&2; exit 1; }

ARGS=(--port "$PORT" --host "$HOST" --bridge-url "$BRIDGE_URL")
[ -n "$DB" ] && ARGS+=(--db "$DB")
[ "$GRPC_PORT" -gt 0 ] && ARGS+=(--grpc-port "$GRPC_PORT")
[ "$PUSH" = "1" ] && ARGS+=(--push)
[ "$REQUIRE_TOKEN" = "1" ] && { ARGS+=(--require-token); [ -n "$SIDECAR_TOKEN" ] && ARGS+=(--sidecar-token "$SIDECAR_TOKEN"); }

echo "[a2a-sidecar] 启动 $HOST:$PORT -> $BRIDGE_URL (python: $PYTHON)"
exec "$PYTHON" "$SIDECAR" "${ARGS[@]}"
