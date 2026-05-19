#!/usr/bin/env bash
set -Eeuo pipefail

INVENTORY="${1:-inventory/host}"
VAULT_OPT="${VAULT_OPT:---ask-vault-pass}"

if [[ ! -f "$INVENTORY" ]]; then
  echo "Inventory file not found: $INVENTORY"
  exit 1
fi

echo "==> Using inventory: $INVENTORY"

echo "==> Building K3s VMs"
ansible-playbook -i "$INVENTORY" playbooks/build_k3s_vms.yml $VAULT_OPT

echo "==> Waiting 90 seconds for first boot/cloud-init"
sleep 90

echo "==> Preparing Ubuntu nodes"
ansible-playbook -i "$INVENTORY" playbooks/prepare_k3s_nodes.yml $VAULT_OPT

echo "==> Installing K3s cluster"
ansible-playbook -i "$INVENTORY" playbooks/install_k3s.yml $VAULT_OPT

echo "==> Installing MetalLB"
ansible-playbook -i "$INVENTORY" playbooks/install_metallb.yml $VAULT_OPT

echo "==> Installing Longhorn"
ansible-playbook -i "$INVENTORY" playbooks/install_longhorn.yml $VAULT_OPT

echo "==> Validating cluster"
ansible-playbook -i "$INVENTORY" playbooks/validate_cluster.yml $VAULT_OPT

echo
echo "Done."
echo "Kubeconfig should be at: ~/.kube/config-k3s-homelab"
echo "API VIP should be: https://192.168.201.50:6443"