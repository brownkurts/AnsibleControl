# KBTECH Ansible Agent Rules

This repository manages homelab servers, desktops, bootstrap, and repeatable system configuration.

For the full homelab operating playbook, see:

```text
C:\Users\KURT\Documents\GitHub\home-lab-command-center\docs\HOMELAB-RUNBOOK.md
```

Repo-local runbook:

```text
C:\Users\KURT\Documents\GitHub\AnsibleControl\RUNBOOK.md
```

## Primary Repos

- Command Center app: `C:\Users\KURT\Documents\GitHub\home-lab-command-center`
- Fleet GitOps repo: `C:\Users\KURT\Documents\GitHub\fleet`
- Ansible/control repo: `C:\Users\KURT\Documents\GitHub\AnsibleControl`
- Server ansible-pull baseline: `C:\Users\KURT\Documents\GitHub\ansible_pull_servers`
- Desktop ansible-pull baseline: `C:\Users\KURT\Documents\GitHub\ansible_pull_desktop`

## K3s Access

Use the Ansible server for Kubernetes access:

```powershell
ssh 192.168.2.30 "kubectl --server=https://192.168.201.51:6443 ..."
```

## Server Rules

When adding a server or VM, update:

- Ansible inventory
- ansible-pull server vars in `ansible_pull_servers`
- desktop Remmina connections in `C:\Users\KURT\Documents\GitHub\ansible_pull_desktop\group_vars\all.yml` when the host should be reachable from managed desktops
- DNS
- Uptime Kuma
- Command Center when user-facing or operationally important
- backup/snapshot expectations
- standard users, SSH, packages, and security baseline

Servers should receive the server ansible-pull baseline unless explicitly excluded.

Server self-management belongs in `ansible_pull_servers`; this repo owns inventory, Proxmox/VM build playbooks, one-off maintenance playbooks, and cross-host orchestration.

## Desktop/Admin Box Rules

Desktop/admin workstations should receive the desktop ansible-pull baseline.

Ubuntu Desktop/admin workstations should onboard to Tactical RMM so RustDesk is installed and available for console-style remote desktop access. XRDP is only a fallback because it starts a separate Linux login session rather than the same session shown in Proxmox noVNC.

When adding a desktop/admin workstation, also update `desktop_remmina_connections` in:

```text
C:\Users\KURT\Documents\GitHub\ansible_pull_desktop\group_vars\all.yml
```

Add RDP and/or SSH entries so existing managed desktops automatically receive launchable Remmina connections for the new host.

Desktop self-management belongs in `ansible_pull_desktop`; this repo owns build/orchestration playbooks such as `playbooks/build_admin_desktop_vms.yml`.

Desired admin/dev box direction:

- Ubuntu Desktop LTS VM on Proxmox
- reachable from other devices
- Git, VS Code, kubectl, Helm, Ansible, SSH keys, GitHub auth
- all homelab repos checked out in a standard path
- used as the main homelab control workstation so the current Windows desktop can be reimaged

## PXE/Autoinstall Context

PXE/inventory host:

```text
192.168.2.21
```

This is the Unraid NAS and runs the PXE/iVentory service. It has the ISO share and the NAS shares; if PXE/autoinstall needs persistent files, seed ISOs, installers, cloud-init snippets, logs, or other shared storage, use the appropriate Unraid network share, such as the Media share when that is the right target.

Goals:

- Ubuntu Desktop autoinstall
- Ubuntu Server autoinstall
- ansible-pull bootstrap for desktop/server roles
- keep vars updated for known servers and admin desktops

## Remote Jump Box Context

Build lightweight Ubuntu Desktop/admin workstation jump boxes after `admin-01` is stable:

- NOC/colo: `pve-noc` / inventory host `Proxmox-NOC`, IP `74.91.16.2`
- Farm/Moms: `PVE3` / `proxmoxmomx`, IP `192.168.1.200`

Jump boxes should receive the desktop ansible-pull baseline, RDP/SSH access, DNS, Uptime Kuma checks, Remmina entries, and backup/snapshot expectations.

## Cross-Repo Rules

When Ansible creates or changes something user-facing, update the related systems:

- Fleet/K3s if a service is deployed there
- Command Center after it is live/reachable
- Uptime Kuma
- DNS
- docs/runbooks

Alerts should go to Rocket.Chat `#alerts`. ntfy has been retired and should not be reintroduced unless explicitly requested.

Docker Swarm is being retired. Do not reintroduce Swarm dependencies unless explicitly requested.

## Safety Rules

- Do not commit vault plaintext, `.vault_pass`, private keys, passwords, Tactical RMM auth keys, or GitHub tokens.
- Do not run broad destructive playbooks without confirming target hosts and backups.
- Preserve Proxmox Ceph references; Docker Swarm Ceph references should stay retired.
- Prefer targeted inventory groups over `all` unless the playbook is explicitly designed for all hosts.
- Run `python3 scripts/audit_tactical_inventory.py` after inventory changes to
  review Tactical RMM onboarding and central inventory gaps. Keep reviewed
  exceptions in `inventory/tactical-audit.json`.
- Repair missing Linux Mesh agents with `scripts/install_tactical_mesh.py`,
  then verify with `playbooks/verify-tactical-mesh.yml`. Do not print the
  Tactical Mesh installer response because it contains a sensitive binding.

## Validation

Use syntax checks before pushing playbook changes:

```powershell
ansible-playbook --syntax-check -i inventory/hosts.ini playbooks/main.yml
ansible-playbook --syntax-check -i inventory/hosts.ini playbooks/task/diskspace.yml
```

If Ansible is not installed in the local Windows shell, run validation on the Ansible server or state that validation was not available.
