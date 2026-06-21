#!/bin/bash
# Tailscale pre-auth key renewal — runs every 80 days via systemd timer
# Uses API key to generate a new device enrollment pre-auth key
# NOTE: The API key itself cannot be auto-renewed — it must be manually rotated
#       in the Tailscale admin console before expiry. This script will alert you.
#
# Secrets are stored in /etc/tailscale-renewal.env (not in git):
#   API_KEY=tskey-api-...
#   API_KEY_EXPIRY=YYYY-MM-DDTHH:MM:SSZ
#   TG_TOKEN=<telegram bot token>
#   TG_CHAT=<telegram chat id>

set -euo pipefail

ENV_FILE="/etc/tailscale-renewal.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found. Create it with API_KEY and API_KEY_EXPIRY." >&2
    exit 1
fi
# shellcheck source=/dev/null
source "$ENV_FILE"

PULLDESKTOP_REPO="/home/kurt/GitHub/ansible_pull_desktop"
GROUP_VARS_FILE="$PULLDESKTOP_REPO/group_vars/all.yml"

notify_telegram() {
    curl -s "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        -d "chat_id=${TG_CHAT}&text=$1" > /dev/null 2>&1 || true
}

# Check API key expiry — warn if < 30 days remaining
API_EXPIRY_EPOCH=$(date -d "$API_KEY_EXPIRY" +%s 2>/dev/null || date -j -f "%Y-%m-%dT%H:%M:%SZ" "$API_KEY_EXPIRY" +%s)
NOW_EPOCH=$(date +%s)
DAYS_LEFT=$(( (API_EXPIRY_EPOCH - NOW_EPOCH) / 86400 ))

if [ "$DAYS_LEFT" -lt 0 ]; then
    notify_telegram "🚨 TAILSCALE API KEY EXPIRED ${DAYS_LEFT#-} days ago! Pre-auth renewal will FAIL. Go to https://login.tailscale.com/admin/settings/keys and create a new API key. Update $ENV_FILE on Hammond."
    exit 1
elif [ "$DAYS_LEFT" -lt 30 ]; then
    notify_telegram "⚠️ Tailscale API key expires in $DAYS_LEFT days (${API_KEY_EXPIRY%T*}). Go to https://login.tailscale.com/admin/settings/keys to rotate it. Update $ENV_FILE on Hammond when done."
fi

# Generate new pre-auth key (90-day validity)
KEY_RESPONSE=$(curl -s -X POST "https://api.tailscale.com/api/v2/tailnet/-/keys" \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{
        "capabilities": {
            "devices": {
                "create": {
                    "reusable": false,
                    "ephemeral": false,
                    "preauthorized": true
                }
            }
        },
        "expirySeconds": 7776000
    }')

NEW_KEY=$(echo "$KEY_RESPONSE" | jq -r '.key // empty')
NEW_EXPIRY=$(echo "$KEY_RESPONSE" | jq -r '.expires // empty')

if [ -z "$NEW_KEY" ]; then
    notify_telegram "❌ Tailscale pre-auth key renewal FAILED: $(echo "$KEY_RESPONSE" | jq -r '.message // "unknown error"')"
    exit 1
fi

# Update ansible_pull_desktop group_vars
sed -i "s|^desktop_tailscale_preauth_key:.*|desktop_tailscale_preauth_key: \"$NEW_KEY\"|" "$GROUP_VARS_FILE"
sed -i "s|^desktop_tailscale_preauth_expiry:.*|desktop_tailscale_preauth_expiry: \"$NEW_EXPIRY\"|" "$GROUP_VARS_FILE"

# Commit and push
cd "$PULLDESKTOP_REPO"
git pull origin master --quiet
git add group_vars/all.yml
git commit -m "chore: renew Tailscale pre-auth key (expires $NEW_EXPIRY)"
git push origin master

MSG="✅ Tailscale pre-auth key renewed. Expires: ${NEW_EXPIRY%T*}."
if [ "$DAYS_LEFT" -lt 30 ]; then
    MSG="$MSG ⚠️ API key expires in $DAYS_LEFT days — manual rotation needed!"
fi
notify_telegram "$MSG"
