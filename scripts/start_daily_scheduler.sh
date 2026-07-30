#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
CRONTAB_FILE="${PROJECT_DIR}/config/daily.crontab"
SESSION_NAME="trendradar-daily"

SUPERCRONIC="${SUPERCRONIC:-$(command -v supercronic || true)}"
UV_BIN="${UV_BIN:-$(command -v uv || true)}"

if [[ -z "${SUPERCRONIC}" || ! -x "${SUPERCRONIC}" ]]; then
    echo "未找到可执行的 supercronic；请安装后加入 PATH，或设置 SUPERCRONIC。" >&2
    exit 1
fi

if [[ -z "${UV_BIN}" || ! -x "${UV_BIN}" ]]; then
    echo "未找到可执行的 uv；请安装后加入 PATH，或设置 UV_BIN。" >&2
    exit 1
fi

if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
    echo "TrendRadar 每日任务已经运行：${SESSION_NAME}"
    exit 0
fi

"${SUPERCRONIC}" -test "${CRONTAB_FILE}"

printf -v scheduler_command \
    'exec env TZ=%q PROJECT_DIR=%q UV_BIN=%q %q %q' \
    "Asia/Shanghai" "${PROJECT_DIR}" "${UV_BIN}" \
    "${SUPERCRONIC}" "${CRONTAB_FILE}"

tmux new-session -d -s "${SESSION_NAME}" "${scheduler_command}"

sleep 1
if ! tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
    echo "TrendRadar 每日任务启动失败" >&2
    exit 1
fi

echo "TrendRadar 每日任务已启动：${SESSION_NAME}"
echo "计划：每天 11:30（Asia/Shanghai）"
