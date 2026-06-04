#!/bin/bash
# Tailscale pre-auth key renewal — runs every 80 days
# Generates a new enrollment key before the old one expires (90-day validity)

set -euo pipefail

API_KEY="REDACTED_ROTATED_2026-06-09"
REPO_PATH="/home/kurt/ansible/../AnsibleControl"
GROUP_VARS_FILE="$REPO_PATH/group_vars/all.yml"

# Function to send Telegram notification
notify_telegram() {
    local message="$1"
    curl -s "https://api.telegram.org/bot8336332018:AAGG15vg2hkCrXW7J_mvaqoBX_EXxsVkRfk/sendMessage" \
        -d "chat_id=8083167980&text=$message" > /dev/null 2>&1 || true
}

# Function to generate new pre-auth key
generate_key() {
    local response
    response=$(curl -s -X POST "https://api.tailscale.com/api/v2/tailnet/-/keys" \
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

    echo "$response"
}

notify_telegram "🔄 Tailscale pre-auth key renewal starting..."

# Generate new key
KEY_RESPONSE=$(generate_key)
NEW_KEY=$(echo "$KEY_RESPONSE" | jq -r '.key // empty')
NEW_EXPIRY=$(echo "$KEY_RESPONSE" | jq -r '.expires // empty')

if [ -z "$NEW_KEY" ]; then
    notify_telegram "❌ Tailscale key renewal FAILED: $(echo "$KEY_RESPONSE" | jq -r '.message // "unknown error"')"
    exit 1
fi

# Update group_vars file in ansible_pull_desktop
PULLDESKTOP_FILE="/home/kurt/GitHub/ansible_pull_desktop/group_vars/all.yml"
sed -i "s/^desktop_tailscale_preauth_key:.*/desktop_tailscale_preauth_key: \"$NEW_KEY\"/" "$PULLDESKTOP_FILE"
sed -i "s/^desktop_tailscale_preauth_expiry:.*/desktop_tailscale_preauth_expiry: \"$NEW_EXPIRY\"/" "$PULLDESKTOP_FILE"

# Commit and push ansible_pull_desktop
cd "/home/kurt/GitHub/ansible_pull_desktop"
git add group_vars/all.yml
git commit -m "chore: renew Tailscale pre-auth key (expires $NEW_EXPIRY)"
git push origin master

notify_telegram "✅ Tailscale pre-auth key renewed. Expires: $NEW_EXPIRY"
