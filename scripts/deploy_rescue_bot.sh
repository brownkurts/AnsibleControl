#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSET_DIR="${SCRIPT_DIR}/../playbooks/files/rescue-bot"

RESCUE_HOST="${RESCUE_HOST:-kurt@192.168.2.32}"
REMOTE_ENV_PATH="/etc/rescue-bot/rescue-bot.env"
REMOTE_REMEDIATOR_ENV_PATH="/etc/rescue-remediator/remediator.env"
REMOTE_KUBECONFIG_PATH="/opt/rescue-bot/kubeconfig"
REMOTE_SSH_KEY_PATH="/ssh/id_ed25519"
LOCAL_KUBECONFIG="${RESCUE_BOT_KUBECONFIG:-${HOME}/.kube/config}"
LOCAL_SSH_KEY="${RESCUE_BOT_SSH_KEY:-${HOME}/.ssh/id_ed25519}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

require_file() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    echo "Required file not found: ${path}" >&2
    exit 1
  fi
}

fetch_remote_file() {
  local remote_path="$1"
  local local_path="$2"
  ssh -n "${RESCUE_HOST}" "sudo cat '${remote_path}'" > "${local_path}"
}

upsert_env() {
  local file="$1"
  local key="$2"
  local value="$3"
  local escaped_value="${value//\\/\\\\}"
  escaped_value="${escaped_value//&/\\&}"

  if grep -q "^${key}=" "${file}"; then
    sed -i "s|^${key}=.*$|${key}=${escaped_value}|" "${file}"
  else
    printf '%s=%s\n' "${key}" "${value}" >> "${file}"
  fi
}

for asset in \
  bot.py \
  weather_report.py \
  knowledge_base.json \
  service_map.json \
  auto-approve.json \
  rescue-bot.service \
  rescue-weather-report.service \
  rescue-weather-report.timer
do
  require_file "${ASSET_DIR}/${asset}"
  cp "${ASSET_DIR}/${asset}" "${TMP_DIR}/${asset}"
done

if [[ -n "${RESCUE_BOT_ENV_FILE:-}" ]]; then
  require_file "${RESCUE_BOT_ENV_FILE}"
  cp "${RESCUE_BOT_ENV_FILE}" "${TMP_DIR}/rescue-bot.env"
elif ssh -n "${RESCUE_HOST}" "test -f '${REMOTE_ENV_PATH}'"; then
  fetch_remote_file "${REMOTE_ENV_PATH}" "${TMP_DIR}/rescue-bot.env"
else
  : "${RC_BOT_TOKEN:?Set RC_BOT_TOKEN or RESCUE_BOT_ENV_FILE before the first deploy.}"
  : "${RC_BOT_USER_ID:?Set RC_BOT_USER_ID or RESCUE_BOT_ENV_FILE before the first deploy.}"
  cat > "${TMP_DIR}/rescue-bot.env" <<EOF
RC_URL=https://chat.kbtech.org
RC_BOT_TOKEN=${RC_BOT_TOKEN}
RC_BOT_USER_ID=${RC_BOT_USER_ID}
EOF
fi

upsert_env "${TMP_DIR}/rescue-bot.env" "RC_URL" "https://chat.kbtech.org"
upsert_env "${TMP_DIR}/rescue-bot.env" "NOTIFY_USER" "kbrown"
upsert_env "${TMP_DIR}/rescue-bot.env" "ALERTS_CHANNEL" "#alerts"
upsert_env "${TMP_DIR}/rescue-bot.env" "WATCHDOG_INTERVAL" "300"
upsert_env "${TMP_DIR}/rescue-bot.env" "OLLAMA_URL" "http://127.0.0.1:11434"
upsert_env "${TMP_DIR}/rescue-bot.env" "OLLAMA_MODEL" "llama3.2:1b"
upsert_env "${TMP_DIR}/rescue-bot.env" "KUBECONFIG" "/opt/rescue-bot/kubeconfig"
upsert_env "${TMP_DIR}/rescue-bot.env" "WEATHER_CHANNEL" "#homelab"
upsert_env "${TMP_DIR}/rescue-bot.env" "WEATHER_LOCATION" "Washington,OK"
upsert_env "${TMP_DIR}/rescue-bot.env" "WEATHER_LOCATION_LABEL" "\"Washington, OK\""

if ssh -n "${RESCUE_HOST}" "test -f '${REMOTE_REMEDIATOR_ENV_PATH}'"; then
  fetch_remote_file "${REMOTE_REMEDIATOR_ENV_PATH}" "${TMP_DIR}/remediator.env"
  upsert_env "${TMP_DIR}/remediator.env" "WATCHDOG_URL" "http://192.168.2.33:8769"
fi

SCP_FILES=(
  "${TMP_DIR}/bot.py"
  "${TMP_DIR}/weather_report.py"
  "${TMP_DIR}/knowledge_base.json"
  "${TMP_DIR}/service_map.json"
  "${TMP_DIR}/auto-approve.json"
  "${TMP_DIR}/rescue-bot.env"
  "${TMP_DIR}/rescue-bot.service"
  "${TMP_DIR}/rescue-weather-report.service"
  "${TMP_DIR}/rescue-weather-report.timer"
)

if [[ -f "${TMP_DIR}/remediator.env" ]]; then
  chmod 0644 "${TMP_DIR}/remediator.env"
  SCP_FILES+=("${TMP_DIR}/remediator.env")
fi

if [[ -f "${LOCAL_KUBECONFIG}" ]]; then
  cp "${LOCAL_KUBECONFIG}" "${TMP_DIR}/kubeconfig"
  chmod 0600 "${TMP_DIR}/kubeconfig"
  SCP_FILES+=("${TMP_DIR}/kubeconfig")
fi

if [[ -f "${LOCAL_SSH_KEY}" ]]; then
  cp "${LOCAL_SSH_KEY}" "${TMP_DIR}/id_ed25519"
  chmod 0600 "${TMP_DIR}/id_ed25519"
  SCP_FILES+=("${TMP_DIR}/id_ed25519")
fi

chmod 0600 "${TMP_DIR}/rescue-bot.env"

ssh -n "${RESCUE_HOST}" "mkdir -p /tmp/rescue-bot-deploy"
scp -B "${SCP_FILES[@]}" "${RESCUE_HOST}:/tmp/rescue-bot-deploy/"

ssh -n "${RESCUE_HOST}" 'sudo install -o kurt -g kurt -m 0750 /tmp/rescue-bot-deploy/bot.py /opt/rescue-bot/bot.py
sudo install -o kurt -g kurt -m 0750 /tmp/rescue-bot-deploy/weather_report.py /opt/rescue-bot/weather_report.py
sudo install -o kurt -g kurt -m 0644 /tmp/rescue-bot-deploy/knowledge_base.json /opt/rescue-bot/knowledge_base.json
sudo install -o kurt -g kurt -m 0644 /tmp/rescue-bot-deploy/service_map.json /opt/rescue-bot/service_map.json
sudo install -o kurt -g kurt -m 0640 /tmp/rescue-bot-deploy/auto-approve.json /config/auto-approve.json
sudo install -o root -g root -m 0600 /tmp/rescue-bot-deploy/rescue-bot.env /etc/rescue-bot/rescue-bot.env
if [ -f /tmp/rescue-bot-deploy/remediator.env ]; then
  sudo install -o root -g root -m 0644 /tmp/rescue-bot-deploy/remediator.env /etc/rescue-remediator/remediator.env
fi
if [ -f /tmp/rescue-bot-deploy/kubeconfig ]; then
  sudo install -o kurt -g kurt -m 0600 /tmp/rescue-bot-deploy/kubeconfig /opt/rescue-bot/kubeconfig
fi
if [ -f /tmp/rescue-bot-deploy/id_ed25519 ]; then
  sudo install -o kurt -g kurt -m 0600 /tmp/rescue-bot-deploy/id_ed25519 /ssh/id_ed25519
fi
sudo install -o root -g root -m 0644 /tmp/rescue-bot-deploy/rescue-bot.service /etc/systemd/system/rescue-bot.service
sudo install -o root -g root -m 0644 /tmp/rescue-bot-deploy/rescue-weather-report.service /etc/systemd/system/rescue-weather-report.service
sudo install -o root -g root -m 0644 /tmp/rescue-bot-deploy/rescue-weather-report.timer /etc/systemd/system/rescue-weather-report.timer
rm -rf /tmp/rescue-bot-deploy
sudo systemctl daemon-reload
sudo systemctl enable --now rescue-bot
sudo systemctl enable --now rescue-weather-report.timer
if systemctl list-unit-files rescue-remediator.service >/dev/null 2>&1; then
  sudo systemctl enable --now rescue-remediator
fi'

for _ in $(seq 1 20); do
  if ssh -n "${RESCUE_HOST}" "curl -fsS --max-time 5 http://127.0.0.1:8080/health"; then
    echo
    exit 0
  fi
  sleep 2
done

echo "rescue-bot did not become healthy" >&2
exit 1
