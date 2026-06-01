#!/usr/bin/env bash
set -euo pipefail

RC_URL="${RC_URL:-https://chat.kbtech.org}"
RESCUE_WEBHOOK_URL="${RESCUE_WEBHOOK_URL:-http://192.168.2.32:8080/webhook}"

RC_ADMIN_USER="$(
  kubectl -n chat get secret rocketchat-admin -o jsonpath='{.data.username}' \
    | base64 --decode
)"
RC_ADMIN_PASSWORD="$(
  kubectl -n chat get secret rocketchat-admin -o jsonpath='{.data.password}' \
    | base64 --decode
)"

LOGIN="$(
  curl -fsS "${RC_URL}/api/v1/login" \
    -H "Content-Type: application/json" \
    --data "$(jq -nc --arg user "${RC_ADMIN_USER}" --arg password "${RC_ADMIN_PASSWORD}" \
      '{user:$user,password:$password}')"
)"
AUTH_TOKEN="$(jq -er '.data.authToken' <<< "${LOGIN}")"
USER_ID="$(jq -er '.data.userId' <<< "${LOGIN}")"

INTEGRATIONS="$(
  curl -fsS "${RC_URL}/api/v1/integrations.list?type=webhook-outgoing&count=100" \
    -H "X-Auth-Token: ${AUTH_TOKEN}" \
    -H "X-User-Id: ${USER_ID}"
)"

MATCHES="$(
  jq '[.integrations[] | select(any(.urls[]?; contains("claude-bot.chat.svc.cluster.local")))]' \
    <<< "${INTEGRATIONS}"
)"
MATCH_COUNT="$(jq 'length' <<< "${MATCHES}")"
if [[ "${MATCH_COUNT}" == "0" ]]; then
  echo "No in-cluster claude-bot outgoing webhooks were found." >&2
  exit 1
fi

while IFS= read -r integration; do
  INTEGRATION_TOKEN="$(openssl rand -hex 24)"
  UPDATE_BODY="$(
    jq --arg url "${RESCUE_WEBHOOK_URL}" --arg token "${INTEGRATION_TOKEN}" '
      {
          type,
          name,
          enabled,
          username,
          scriptEnabled,
          channel: (.channel | join(",")),
          integrationId: ._id,
          urls: [$url],
          event,
          triggerWords,
          token: $token,
          alias,
          avatar,
          emoji,
          script
        }
      | with_entries(select(.value != null))
    ' <<< "${integration}"
  )"

  curl -fsS -X PUT "${RC_URL}/api/v1/integrations.update" \
    -H "Content-Type: application/json" \
    -H "X-Auth-Token: ${AUTH_TOKEN}" \
    -H "X-User-Id: ${USER_ID}" \
    --data "${UPDATE_BODY}" \
    | jq -e '{success, error, integration: {name: .integration.name, urls: .integration.urls}}'
done < <(jq -c '.[]' <<< "${MATCHES}")
