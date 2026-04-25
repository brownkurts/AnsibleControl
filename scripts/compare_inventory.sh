#!/bin/bash

REPORT="reports/discovery/servers-found.txt"
INVENTORY="inventory/host"

echo "==== Extracting discovered IPs ===="
DISCOVERED=$(grep -oP 'Host: \K[0-9.]+' "$REPORT" | sort -u)

echo "==== Extracting inventory IPs ===="
INVENTORY_IPS=$(grep -oP 'ansible_host=\K[0-9.]+' "$INVENTORY" | sort -u)

echo
echo "===== MISSING FROM INVENTORY ====="
comm -23 <(echo "$DISCOVERED") <(echo "$INVENTORY_IPS")

echo
echo "===== IN INVENTORY BUT NOT FOUND ====="
comm -13 <(echo "$DISCOVERED") <(echo "$INVENTORY_IPS")
