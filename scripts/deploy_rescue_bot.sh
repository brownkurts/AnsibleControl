#!/usr/bin/env bash
set -euo pipefail

RESCUE_HOST="${RESCUE_HOST:-kurt@192.168.2.32}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

kubectl -n chat get configmap claude-bot-script -o jsonpath='{.data.bot\.py}' \
  > "${TMP_DIR}/bot.py"
kubectl -n chat get configmap claude-bot-config -o jsonpath='{.data.auto-approve\.json}' \
  > "${TMP_DIR}/auto-approve.json"
kubectl -n chat get secret claude-bot-ssh -o jsonpath='{.data.id_ed25519}' \
  | base64 --decode > "${TMP_DIR}/id_ed25519"

RC_BOT_TOKEN="$(
  kubectl -n chat get secret claude-bot-secrets -o jsonpath='{.data.RC_BOT_TOKEN}' \
    | base64 --decode
)"
RC_BOT_USER_ID="$(
  kubectl -n chat get secret claude-bot-secrets -o jsonpath='{.data.RC_BOT_USER_ID}' \
    | base64 --decode
)"

cat > "${TMP_DIR}/rescue-bot.env" <<EOF
RC_URL=https://chat.kbtech.org
RC_BOT_TOKEN=${RC_BOT_TOKEN}
RC_BOT_USER_ID=${RC_BOT_USER_ID}
NOTIFY_USER=kbrown
ALERTS_CHANNEL=#alerts
WATCHDOG_INTERVAL=300
OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=llama3.2:1b
KUBECONFIG=/opt/rescue-bot/kubeconfig
EOF

cp "${HOME}/.kube/config" "${TMP_DIR}/kubeconfig"
chmod 0600 "${TMP_DIR}/id_ed25519" "${TMP_DIR}/kubeconfig" "${TMP_DIR}/rescue-bot.env"

ssh "${RESCUE_HOST}" "mkdir -p /tmp/rescue-bot-deploy"
scp \
  "${TMP_DIR}/bot.py" \
  "${TMP_DIR}/auto-approve.json" \
  "${TMP_DIR}/id_ed25519" \
  "${TMP_DIR}/kubeconfig" \
  "${TMP_DIR}/rescue-bot.env" \
  "${RESCUE_HOST}:/tmp/rescue-bot-deploy/"

ssh "${RESCUE_HOST}" 'sudo install -o kurt -g kurt -m 0750 /tmp/rescue-bot-deploy/bot.py /opt/rescue-bot/bot.py
sudo install -o kurt -g kurt -m 0640 /tmp/rescue-bot-deploy/auto-approve.json /config/auto-approve.json
sudo install -o kurt -g kurt -m 0600 /tmp/rescue-bot-deploy/id_ed25519 /ssh/id_ed25519
sudo install -o kurt -g kurt -m 0600 /tmp/rescue-bot-deploy/kubeconfig /opt/rescue-bot/kubeconfig
sudo install -o root -g root -m 0600 /tmp/rescue-bot-deploy/rescue-bot.env /etc/rescue-bot/rescue-bot.env
rm -rf /tmp/rescue-bot-deploy
sudo tee /etc/systemd/system/rescue-bot.service >/dev/null <<'"'"'EOF'"'"'
[Unit]
Description=KBTech out-of-cluster rescue bot
After=network-online.target ollama.service
Wants=network-online.target
Requires=ollama.service

[Service]
Type=simple
User=kurt
WorkingDirectory=/opt/rescue-bot
EnvironmentFile=/etc/rescue-bot/rescue-bot.env
ExecStart=/opt/rescue-bot/venv/bin/python /opt/rescue-bot/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now rescue-bot
if systemctl list-unit-files rescue-remediator.service >/dev/null 2>&1; then
  sudo systemctl restart rescue-remediator
fi'

for _ in $(seq 1 20); do
  if curl -fsS "http://${RESCUE_HOST#*@}:8080/health"; then
    echo
    exit 0
  fi
  sleep 2
done

echo "rescue-bot did not become healthy" >&2
exit 1
