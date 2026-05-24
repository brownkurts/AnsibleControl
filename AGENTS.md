# KBTECH Ansible Agent Rules

This repository manages homelab servers, desktops, bootstrap, and repeatable system configuration.

For the full homelab operating playbook, see:

```text
C:\Users\KURT\Documents\GitHub\home-lab-command-center\docs\HOMELAB-RUNBOOK.md
```

## Primary Repos

- Command Center app: `C:\Users\KURT\Documents\GitHub\home-lab-command-center`
- Fleet GitOps repo: `C:\Users\KURT\Documents\GitHub\fleet`
- Ansible/control repo: `C:\Users\KURT\Documents\GitHub\AnsibleControl`

## K3s Access

Use the Ansible server for Kubernetes access:

```powershell
ssh 192.168.2.30 "kubectl --server=https://192.168.201.51:6443 ..."
```

## Server Rules

When adding a server or VM, update:

- Ansible inventory
- ansible-pull server vars
- DNS
- Uptime Kuma
- Command Center when user-facing or operationally important
- backup/snapshot expectations
- standard users, SSH, packages, and security baseline

Servers should receive the server ansible-pull baseline unless explicitly excluded.

## Desktop/Admin Box Rules

Desktop/admin workstations should receive the desktop ansible-pull baseline.

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

This is on the Unraid box and has the ISO share.

Goals:

- Ubuntu Desktop autoinstall
- Ubuntu Server autoinstall
- ansible-pull bootstrap for desktop/server roles
- keep vars updated for known servers and admin desktops

## Cross-Repo Rules

When Ansible creates or changes something user-facing, update the related systems:

- Fleet/K3s if a service is deployed there
- Command Center after it is live/reachable
- Uptime Kuma
- DNS
- docs/runbooks

Docker Swarm is being retired. Do not reintroduce Swarm dependencies unless explicitly requested.
