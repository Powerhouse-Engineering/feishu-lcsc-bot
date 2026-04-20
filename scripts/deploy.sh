#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-feishu-lcsc-bot}"
APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
APP_USER="${APP_USER:-$USER}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
REQ_FILE="${REQ_FILE:-$APP_DIR/requirements.txt}"
SERVICE_TEMPLATE="${SERVICE_TEMPLATE:-$APP_DIR/deploy/systemd/feishu-lcsc-bot.service}"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}.service"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"

echo "[deploy] service: ${SERVICE_NAME}"
echo "[deploy] app dir: ${APP_DIR}"
echo "[deploy] app user: ${APP_USER}"

if [[ ! -f "${APP_DIR}/app.py" ]]; then
  echo "[deploy] error: app.py not found in ${APP_DIR}" >&2
  exit 1
fi

if [[ ! -f "${APP_DIR}/.env" ]]; then
  echo "[deploy] error: ${APP_DIR}/.env not found" >&2
  echo "[deploy] copy your env file before running deploy." >&2
  exit 1
fi

if [[ ! -f "${REQ_FILE}" ]]; then
  echo "[deploy] error: requirements file not found: ${REQ_FILE}" >&2
  exit 1
fi

if [[ ! -f "${SERVICE_TEMPLATE}" ]]; then
  echo "[deploy] error: service template not found: ${SERVICE_TEMPLATE}" >&2
  exit 1
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "[deploy] error: python binary not found: ${PYTHON_BIN}" >&2
  exit 1
fi

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  echo "[deploy] creating virtualenv at ${VENV_DIR}"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

echo "[deploy] installing python dependencies"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r "${REQ_FILE}"

tmp_service="$(mktemp)"
trap 'rm -f "$tmp_service"' EXIT

sed \
  -e "s|{{APP_USER}}|${APP_USER}|g" \
  -e "s|{{APP_DIR}}|${APP_DIR}|g" \
  "${SERVICE_TEMPLATE}" > "${tmp_service}"

echo "[deploy] installing systemd unit to ${SERVICE_DST}"
sudo install -m 0644 "${tmp_service}" "${SERVICE_DST}"

echo "[deploy] reloading systemd"
sudo systemctl daemon-reload

echo "[deploy] enabling and restarting ${SERVICE_NAME}"
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"

echo "[deploy] status:"
sudo systemctl --no-pager --full status "${SERVICE_NAME}" || true

echo "[deploy] done."
echo "[deploy] follow logs with:"
echo "  journalctl -u ${SERVICE_NAME} -f"
