# K3s

This replaces the old Docker Swarm workflow with a lightweight K3s cluster.

## Run

```bash
ansible-playbook -i playbooks/k3s/inventory/hosts.ini playbooks/k3s.yml
```

The playbook installs the first server with `--cluster-init`, joins the remaining servers to the embedded etcd cluster, then joins the agents.

K3s writes the join token to `/var/lib/rancher/k3s/server/node-token` on the first server. The playbook reads it with `no_log: true` and uses it only for the join tasks.

## Inventory

Edit `playbooks/k3s/inventory/hosts.ini` before running if the node names or IPs changed from the old Kubernetes lab range.

Server and agent options can be added through these inventory variables:

```ini
k3s_server_args=--write-kubeconfig-mode=644
k3s_agent_args=
```
