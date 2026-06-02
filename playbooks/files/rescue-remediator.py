#!/usr/bin/env python3
"""Guarded auto-remediation for the KBTECH Stargate K3s VMs."""

import argparse
import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from kubernetes import client, config

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))
FAILURE_THRESHOLD = int(os.environ.get("FAILURE_THRESHOLD", "3"))
POST_ACTION_WAIT = int(os.environ.get("POST_ACTION_WAIT", "180"))
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "1800"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "2"))
ATTEMPT_WINDOW_SECONDS = int(os.environ.get("ATTEMPT_WINDOW_SECONDS", "21600"))
AUTO_REMEDIATE = os.environ.get("AUTO_REMEDIATE", "true").lower() == "true"
WATCHDOG_URL = os.environ.get("WATCHDOG_URL", "http://192.168.2.30:8769")
ANSIBLE_HOST = os.environ.get("ANSIBLE_HOST", "kurt@192.168.2.30")
SSH_KEY = os.environ.get("SSH_KEY", "/ssh/id_ed25519")
KUBECONFIG = os.environ.get("KUBECONFIG", "/opt/rescue-bot/kubeconfig")
RESCUE_BOT_ENV = os.environ.get("RESCUE_BOT_ENV", "/etc/rescue-bot/rescue-bot.env")
STATE_FILE = Path(os.environ.get("STATE_FILE", "/var/lib/rescue-remediator/state.json"))
STATUS_FILE = Path(os.environ.get("STATUS_FILE", "/var/lib/rescue-remediator/status.json"))
AUDIT_FILE = Path(os.environ.get("AUDIT_FILE", "/var/log/rescue-remediator/actions.jsonl"))

NODES = {
    "prometheus": {"ip": "192.168.201.51", "vmid": 2201, "service": "k3s"},
    "odyssey": {"ip": "192.168.201.52", "vmid": 2202, "service": "k3s"},
    "apollo": {"ip": "192.168.201.53", "vmid": 2203, "service": "k3s"},
    "abydos": {"ip": "192.168.201.54", "vmid": 2204, "service": "k3s-agent"},
    "chulak": {"ip": "192.168.201.55", "vmid": 2205, "service": "k3s-agent"},
    "dakara": {"ip": "192.168.201.56", "vmid": 2206, "service": "k3s-agent"},
}

PROXMOX_NODES = {
    "PROXMOX-01": "192.168.2.201",
    "proxmox03": "192.168.2.212",
    "proxmox05": "192.168.2.215",
    "pve-4": "192.168.2.204",
}
PROXMOX_CLUSTER_MONITOR = "192.168.2.201"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rescue-remediator")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    with tmp.open("w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def load_json(path, default):
    try:
        with path.open() as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except Exception as error:
        log.warning("Could not load %s: %s", path, error)
        return default


def audit(event, **details):
    entry = {"time": now_iso(), "event": event, **details}
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_FILE.open("a") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    log.info("%s %s", event, details)


def run(args, timeout=30):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        output = result.stdout.strip()
        if result.returncode != 0:
            output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except Exception as error:
        return False, str(error)


def broker_run(args, timeout=45):
    remote_command = shlex.join(args)
    return run([
        "ssh", "-i", SSH_KEY,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        ANSIBLE_HOST, remote_command,
    ], timeout=timeout)


def nested_ssh(target, command, timeout=45):
    return broker_run([
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        target,
        shlex.join(command),
    ], timeout=timeout)


def http_json(url, method="GET", body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def read_env(path):
    values = {}
    try:
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value
    except OSError as error:
        log.warning("Could not read %s: %s", path, error)
    return values


def notify(text):
    values = read_env(RESCUE_BOT_ENV)
    url = values.get("RC_URL")
    token = values.get("RC_BOT_TOKEN")
    user_id = values.get("RC_BOT_USER_ID")
    if not (url and token and user_id):
        log.warning("Rocket.Chat bot credentials are unavailable")
        return
    payload = json.dumps({"channel": "#alerts", "text": text}).encode()
    request = Request(f"{url}/api/v1/chat.postMessage", data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("X-Auth-Token", token)
    request.add_header("X-User-Id", user_id)
    try:
        with urlopen(request, timeout=15) as response:
            response.read()
    except Exception as error:
        log.warning("Rocket.Chat notification failed: %s", error)


def tcp_open(host, port, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def maintenance_state():
    try:
        global_state = http_json(f"{WATCHDOG_URL}/maintenance")
        devices = http_json(f"{WATCHDOG_URL}/maintenance/devices")
        return global_state, devices
    except (OSError, URLError, ValueError) as error:
        log.error("Watchdog maintenance API is unavailable; remediation disabled: %s", error)
        return None, None


def load_readiness():
    config.load_kube_config(config_file=KUBECONFIG)
    nodes = client.CoreV1Api().list_node(_request_timeout=10).items
    readiness = {}
    for node in nodes:
        ready = next((item for item in (node.status.conditions or []) if item.type == "Ready"), None)
        readiness[node.metadata.name] = bool(ready and ready.status == "True")
    return readiness


def node_ready(name):
    try:
        return load_readiness().get(name) is True
    except Exception:
        return False


def wait_until_ready(name, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if node_ready(name):
            return True
        time.sleep(10)
    return False


def proxmox_guest(vmid):
    ok, output = nested_ssh(
        f"root@{PROXMOX_CLUSTER_MONITOR}",
        ["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"],
        timeout=30,
    )
    if not ok:
        audit("proxmox-inventory-failed", vmid=vmid, output=output[:500])
        return None
    try:
        resources = json.loads(output)
    except json.JSONDecodeError:
        audit("proxmox-inventory-invalid-json", vmid=vmid, output=output[:500])
        return None
    return next((item for item in resources if item.get("type") == "qemu" and item.get("vmid") == vmid), None)


def pve_action(guest, command, dry_run):
    node = guest.get("node")
    vmid = int(guest["vmid"])
    address = PROXMOX_NODES.get(node)
    if not address:
        return False, f"unknown Proxmox node {node}"
    args = ["qm", command, str(vmid)]
    if command == "reboot":
        args.extend(["--timeout", "30"])
    if dry_run:
        return True, f"dry-run: root@{address} {shlex.join(args)}"
    return nested_ssh(f"root@{address}", args, timeout=50)


def restart_k3s_service(name, node, dry_run):
    target = f"kurt@{node['ip']}"
    command = ["sudo", "-n", "systemctl", "restart", node["service"]]
    if dry_run:
        return True, f"dry-run: {target} {shlex.join(command)}"
    return nested_ssh(target, command, timeout=45)


def prune_attempts(node_state):
    cutoff = time.time() - ATTEMPT_WINDOW_SECONDS
    node_state["attempts"] = [
        timestamp for timestamp in node_state.get("attempts", [])
        if timestamp >= cutoff
    ]


def recovery_allowed(name, node, state, global_maintenance, device_maintenance):
    node_state = state["nodes"].setdefault(name, {})
    prune_attempts(node_state)
    if global_maintenance.get("active"):
        return False, "global maintenance is active"
    if f"k3s-{name}" in device_maintenance or f"vm-home-{node['vmid']}" in device_maintenance:
        return False, "device maintenance is active"
    if len(node_state.get("attempts", [])) >= MAX_ATTEMPTS:
        return False, "attempt limit reached"
    last_attempt = node_state.get("last_attempt", 0)
    if time.time() - last_attempt < COOLDOWN_SECONDS:
        return False, "cooldown is active"
    return True, ""


def recover(name, node, state, dry_run=False):
    node_state = state["nodes"].setdefault(name, {})
    if not dry_run:
        timestamp = time.time()
        node_state.setdefault("attempts", []).append(timestamp)
        node_state["last_attempt"] = timestamp

    audit("recovery-start", node=name, vmid=node["vmid"], dry_run=dry_run)
    notify(f":ambulance: Rescue remediation started for `{name}` (VM `{node['vmid']}`).")

    if tcp_open(node["ip"], 22):
        ok, output = restart_k3s_service(name, node, dry_run)
        audit("k3s-service-restart", node=name, ok=ok, output=output[:500], dry_run=dry_run)
        if dry_run:
            return "dry-run-service-restart"
        if ok and wait_until_ready(name, POST_ACTION_WAIT):
            audit("recovery-complete", node=name, method="service-restart")
            notify(f":white_check_mark: Rescue remediation restored `{name}` by restarting `{node['service']}`.")
            return "service-restart"

    guest = proxmox_guest(node["vmid"])
    if not guest:
        audit("recovery-failed", node=name, reason="VM not found in Proxmox inventory")
        notify(f":warning: Rescue remediation could not find VM `{node['vmid']}` for `{name}`.")
        return "vm-not-found"

    if guest.get("status") == "stopped":
        action = "start"
    else:
        action = "reboot"

    ok, output = pve_action(guest, action, dry_run)
    audit("proxmox-action", node=name, vmid=node["vmid"], action=action, ok=ok,
          output=output[:500], dry_run=dry_run)
    if dry_run:
        return f"dry-run-{action}"

    if ok and wait_until_ready(name, POST_ACTION_WAIT):
        audit("recovery-complete", node=name, method=f"vm-{action}")
        notify(f":white_check_mark: Rescue remediation restored `{name}` with VM `{action}`.")
        return f"vm-{action}"

    if action == "reboot":
        ok, output = pve_action(guest, "reset", dry_run=False)
        audit("proxmox-action", node=name, vmid=node["vmid"], action="reset", ok=ok,
              output=output[:500], dry_run=False)
        if ok and wait_until_ready(name, POST_ACTION_WAIT):
            audit("recovery-complete", node=name, method="vm-reset")
            notify(f":white_check_mark: Rescue remediation restored `{name}` with a forced VM reset.")
            return "vm-reset"

    audit("recovery-failed", node=name, reason="node did not return Ready")
    notify(f":warning: Rescue remediation could not restore `{name}`. Manual review is required.")
    return "failed"


def cycle(state, dry_run=False):
    state["last_cycle"] = now_iso()
    global_maintenance, device_maintenance = maintenance_state()
    if global_maintenance is None:
        state["last_error"] = "watchdog maintenance API unavailable"
        return

    try:
        readiness = load_readiness()
        state["kubernetes_api"] = "available"
    except Exception as error:
        state["kubernetes_api"] = "unavailable"
        state["last_error"] = f"kubernetes API unavailable: {error}"
        audit("kubernetes-api-unavailable", output=str(error)[:500])
        return

    for name, node in NODES.items():
        node_state = state["nodes"].setdefault(name, {})
        is_ready = readiness.get(name) is True
        node_state["ready"] = is_ready
        node_state["last_check"] = now_iso()
        if is_ready:
            node_state["failures"] = 0
            continue

        node_state["failures"] = node_state.get("failures", 0) + 1
        audit("node-not-ready", node=name, failures=node_state["failures"])
        if node_state["failures"] < FAILURE_THRESHOLD:
            continue
        allowed, reason = recovery_allowed(name, node, state, global_maintenance, device_maintenance)
        if not AUTO_REMEDIATE and not dry_run:
            allowed, reason = False, "automatic remediation is disabled"
        if not allowed:
            audit("recovery-skipped", node=name, reason=reason)
            continue
        node_state["last_result"] = recover(name, node, state, dry_run=dry_run)
        node_state["failures"] = 0

    state["last_error"] = None


def self_test(state):
    global_maintenance, device_maintenance = maintenance_state()
    readiness = load_readiness()
    guest = proxmox_guest(NODES["prometheus"]["vmid"])
    result = {
        "auto_remediate": AUTO_REMEDIATE,
        "maintenance_api": global_maintenance is not None,
        "global_maintenance": global_maintenance,
        "device_maintenance_count": len(device_maintenance or {}),
        "kubernetes_nodes": readiness,
        "proxmox_inventory": bool(guest),
        "allowlisted_nodes": NODES,
        "state": state,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["maintenance_api"] and result["proxmox_inventory"] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one check cycle and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Log planned work without changing services or VMs.")
    parser.add_argument("--self-test", action="store_true", help="Verify dependencies without changing anything.")
    parser.add_argument("--status", action="store_true", help="Print the persisted status and exit.")
    args = parser.parse_args()

    state = load_json(STATE_FILE, {"nodes": {}})
    if args.status:
        print(json.dumps(load_json(STATUS_FILE, state), indent=2, sort_keys=True))
        return 0
    if args.self_test:
        return self_test(state)

    while True:
        try:
            cycle(state, dry_run=args.dry_run)
        except Exception as error:
            state["last_error"] = str(error)
            log.exception("Remediation cycle failed")
        atomic_json(STATE_FILE, state)
        atomic_json(STATUS_FILE, state)
        if args.once:
            return 0
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
