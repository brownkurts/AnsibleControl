# AnsibleControl

Central Ansible control repository for KBTECH homelab inventory, VM builds, maintenance playbooks, and cross-host orchestration.

Use the ansible-pull repos for host self-management:

- Servers: `C:\Users\KURT\Documents\GitHub\ansible_pull_servers`
- Desktops/admin workstations: `C:\Users\KURT\Documents\GitHub\ansible_pull_desktop`

Use Fleet for Kubernetes manifests:

```text
C:\Users\KURT\Documents\GitHub\fleet
```

## Important Files

| Path | Purpose |
| --- | --- |
| `inventory/hosts.ini` | Main static inventory |
| `inventory/group_vars/` | Group and shared variables; vault-backed secrets stay encrypted |
| `playbooks/` | Build, bootstrap, maintenance, and service playbooks |
| `playbooks/roles/` | Reusable roles |
| `collections/requirements.yml` | Galaxy collection requirements |
| `RUNBOOK.md` | Operating and troubleshooting notes |
| `AGENTS.md` | Repo-specific agent rules |

## Current Focus Areas

- Production Stargate K3s inventory: `prometheus`, `odyssey`, `apollo`,
  `abydos`, `chulak`, and `dakara`
- Proxmox hosts including home, NOC, and remote site nodes
- Ubuntu server inventory including `admin-01`, `nextcloud-01`, Kasm, Destiny, Docker hosts, and Tactical RMM
- Pi-hole HA and remote replicas
- Windows/AD servers reachable through SSH where configured
- Rocket.Chat notifications for Ansible alerting

## K3s Access

Cluster access is normally performed from the Ansible server:

```powershell
ssh 192.168.2.30 "kubectl --server=https://192.168.201.51:6443 get nodes"
```

## Common Commands

Install collections:

```bash
ansible-galaxy collection install -r collections/requirements.yml
```

Syntax check:

```bash
ansible-playbook --syntax-check -i inventory/hosts.ini playbooks/main.yml
```

Run disk-space alert check:

```bash
ansible-playbook -i inventory/hosts.ini playbooks/task/diskspace.yml
```

Bootstrap ansible-pull on managed Linux servers:

```bash
ansible-playbook -i inventory/hosts.ini playbooks/server-bootstrap.yml
```

Target one host or group:

```bash
ansible-playbook -i inventory/hosts.ini playbooks/server-bootstrap.yml -e ansible_pull_target=nextcloud-01
```

## Documentation

- Repo runbook: `RUNBOOK.md`
- Agent rules: `AGENTS.md`
- Full homelab runbook: `C:\Users\KURT\Documents\GitHub\home-lab-command-center\docs\HOMELAB-RUNBOOK.md`
