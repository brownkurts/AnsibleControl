# AnsibleControl Runbook

This runbook covers the central Ansible control repo.

## Purpose

Use this repo for inventory, VM creation, bootstrap orchestration, maintenance, and cross-host tasks. Do not move desktop-local or server-local ansible-pull baselines here unless they need central orchestration.

## Repository Boundaries

| Repo | Owns |
| --- | --- |
| `AnsibleControl` | Inventory, Proxmox builds, central playbooks, maintenance, cross-host orchestration |
| `ansible_pull_servers` | Server self-management baseline |
| `ansible_pull_desktop` | Desktop/admin workstation self-management baseline |
| `fleet` | K3s manifests and GitOps |
| `home-lab-command-center` | Command Center app and full homelab runbook |

## Inventory Updates

When adding a host:

1. Add it to `inventory/hosts.ini`.
2. Add or update group vars under `inventory/group_vars/`.
3. Decide whether it should receive the server or desktop ansible-pull baseline.
4. Add Remmina entries in `ansible_pull_desktop` when desktops should reach it.
5. Add DNS, Uptime Kuma, backup/snapshot expectations, and Command Center entries when relevant.

## Tactical RMM Inventory Audit

Run the Tactical reconciliation after adding hosts and as a regular review:

```bash
python3 scripts/audit_tactical_inventory.py
```

The script reads the `command-center/tactical-rmm-credentials` K3s secret at
runtime, filters Tactical agents to the `homelab` client, and compares them to
`inventory/hosts.ini` by hostname or IP address. It reports:

- inventory hosts that may still need a Tactical agent;
- Tactical endpoints that may need a central Ansible inventory entry;
- intentionally ignored entries from `inventory/tactical-audit.json`.

Use `--strict` for a scheduled check after reviewed exceptions are recorded.
Do not commit Tactical API keys or enrollment auth keys. The API key used for
the audit is not an enrollment key. The targeted onboarding playbook reads
the encrypted `auth_key` from `inventory/group_vars/all/vault_tac.yml`:

```bash
ansible-playbook -i inventory/hosts.ini playbooks/tactical-onboard.yml -e tactical_onboard_target=<host>
```

Workstations and family endpoints can remain Tactical-only. Add them to
central Ansible inventory only when they need central orchestration.

## VM Build Flow

Use Proxmox build playbooks for repeatable VM creation. Current admin desktop work is represented by:

```text
playbooks/build_admin_desktop_vms.yml
playbooks/templates/admin-desktop-user-data.yml.j2
```

After a VM is built:

- Confirm guest IP and hostname.
- Confirm SSH access.
- Apply the correct ansible-pull baseline.
- Add desktop Remmina entries when useful.
- Add DNS/Uptime checks if it is an operational endpoint.

## Server ansible-pull Onboarding

The preferred way to onboard servers to `ansible_pull_servers` is from this repo:

```bash
ansible-playbook -i inventory/hosts.ini playbooks/server-bootstrap.yml
```

By default, this targets Linux server groups:

```text
control:ubuntu:nextcloud:tactical_hosts:docker:k3s_cluster:pihole
```

Target a single host or custom group with:

```bash
ansible-playbook -i inventory/hosts.ini playbooks/server-bootstrap.yml -e ansible_pull_target=nextcloud-01
ansible-playbook -i inventory/hosts.ini playbooks/server-bootstrap.yml -e ansible_pull_target=k3s_workers
```

The GitHub token for private repo access belongs in Ansible vault as `vault_github_pat`, loaded from:

```text
inventory/group_vars/all/vault_ansible_pull.yml
```

Do not paste this token into README files, shell history, or unencrypted variables. The playbook writes `/root/.git-credentials` on target servers with mode `0600` and configures root Git credential storage so recurring `ansible-pull` can clone the server pull repo.

## Kubernetes/K3s Work

Use this repo for node build/prep and Fleet for manifests.

Common checks from the Ansible server:

```bash
kubectl --server=https://192.168.201.51:6443 get nodes
kubectl --server=https://192.168.201.51:6443 get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded
```

If a K3s workload is changed, update Fleet, not this repo.

## Notifications

Ansible alerting uses Rocket.Chat:

```text
https://chat.kbtech.org
#alerts
```

The shared variables are in:

```text
inventory/group_vars/all/vars.yml
```

ntfy has been retired and should stay removed unless explicitly requested.

## Validation

Install collections:

```bash
ansible-galaxy collection install -r collections/requirements.yml
```

Syntax check examples:

```bash
ansible-playbook --syntax-check -i inventory/hosts.ini playbooks/main.yml
ansible-playbook --syntax-check -i inventory/hosts.ini playbooks/task/diskspace.yml
ansible-playbook --syntax-check -i inventory/hosts.ini playbooks/build_admin_desktop_vms.yml
```

Use `--check` only when the playbook supports check mode safely.

## Safety Notes

- Do not commit vault plaintext, `.vault_pass`, private keys, passwords, auth keys, or personal access tokens.
- Confirm inventory target groups before running playbooks that change packages, reboots, storage, or Proxmox settings.
- Docker Swarm is being retired; do not add new Swarm dependencies.
- Proxmox Ceph still exists and is not being retired.
