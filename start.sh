#!/usr/bin/env bash
set -euo pipefail

# 基于脚本所在位置动态定位根目录（严禁硬编码绝对路径）
cd "$(dirname "$0")"
APP_DIR="$(pwd)"
DATA_DIR="${APP_DIR}/data"
PID_FILE="${DATA_DIR}/rbook.pid"
LOG_FILE="${DATA_DIR}/rbook.log"
HOST="0.0.0.0"
PORT="8001"

mkdir -p "${DATA_DIR}"

if [ -x "${APP_DIR}/venv/bin/uvicorn" ]; then
  UVICORN_BIN="${APP_DIR}/venv/bin/uvicorn"
elif command -v uvicorn >/dev/null 2>&1; then
  UVICORN_BIN="$(command -v uvicorn)"
else
  echo "[ERROR] 未找到 uvicorn 可执行文件，请检查虚拟环境 ./venv" >&2
  exit 1
fi

stop_existing() {
  if [ -f "${PID_FILE}" ]; then
    OLD_PID="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [ -n "${OLD_PID}" ] && kill -0 "${OLD_PID}" 2>/dev/null; then
      kill "${OLD_PID}" 2>/dev/null || true
      sleep 0.5
    fi
    rm -f "${PID_FILE}"
  fi
}

case "${1:-}" in
  --daemon|-d|start)
    stop_existing
    echo "[R-Book] 正在后台启动服务 (${HOST}:${PORT}) ..."
    nohup "${UVICORN_BIN}" main:app --host "${HOST}" --port "${PORT}" >> "${LOG_FILE}" 2>&1 &
    NEW_PID=$!
    echo "${NEW_PID}" > "${PID_FILE}"
    sleep 1
    if kill -0 "${NEW_PID}" 2>/dev/null; then
      echo "[R-Book] 后台启动成功！PID=${NEW_PID}，日志=${LOG_FILE}"
    else
      echo "[ERROR] R-Book 启动失败，请查看日志: ${LOG_FILE}" >&2
      exit 1
    fi
    ;;
  stop)
    stop_existing
    echo "[R-Book] 服务已停止。"
    ;;
  status)
    if [ -f "${PID_FILE}" ] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
      echo "[R-Book] 运行中 (PID=$(cat "${PID_FILE}"), Port=${PORT})"
    else
      echo "[R-Book] 未运行"
      exit 1
    fi
    ;;
  *)
    stop_existing
    echo "[R-Book] 正在前台启动服务 (${HOST}:${PORT}) ..."
    exec "${UVICORN_BIN}" main:app --host "${HOST}" --port "${PORT}"
    ;;
esac
