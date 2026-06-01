#!/usr/bin/env python3
"""Compare central Ansible inventory with one Tactical RMM client."""

import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "inventory" / "hosts.ini"
DEFAULT_EXCEPTIONS = ROOT / "inventory" / "tactical-audit.json"


def normalize(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def parse_ips(value):
    ips = set()
    for part in re.split(r"[,\s]+", str(value or "")):
        candidate = part.split("/", 1)[0]
        try:
            ips.add(str(ipaddress.ip_address(candidate)))
        except ValueError:
            pass
    return ips


def parse_inventory(path):
    hosts = {}
    section = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section.endswith((":vars", ":children")):
            continue
        parts = line.split()
        if not parts or "=" in parts[0]:
            continue
        name = parts[0]
        ansible_host = ""
        for part in parts[1:]:
            if part.startswith("ansible_host="):
                ansible_host = part.split("=", 1)[1]
        item = hosts.setdefault(
            normalize(name),
            {"hostname": name, "ips": set(), "groups": set()},
        )
        item["groups"].add(section)
        item["ips"].update(parse_ips(ansible_host))
    return list(hosts.values())


def load_exceptions(path):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def tactical_api_key(args):
    env_key = os.environ.get("TACTICAL_RMM_API_KEY", "").strip()
    if env_key:
        return env_key
    command = [
        "kubectl",
        "--server",
        args.kube_server,
        "-n",
        args.secret_namespace,
        "get",
        "secret",
        args.secret_name,
        "-o",
        "jsonpath={.data.TACTICAL_RMM_API_KEY}",
    ]
    try:
        encoded = subprocess.check_output(command, text=True).strip()
        return base64.b64decode(encoded).decode("utf-8")
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as exc:
        raise RuntimeError(
            "Set TACTICAL_RMM_API_KEY or run where kubectl can read the "
            "command-center Tactical RMM secret."
        ) from exc


def tactical_agents(args):
    request = urllib.request.Request(
        args.api_url.rstrip("/") + "/agents/",
        headers={"X-API-KEY": tactical_api_key(args)},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
    return [
        {
            "hostname": agent.get("hostname", ""),
            "ips": parse_ips(agent.get("local_ips", "")),
            "status": agent.get("status", "unknown"),
        }
        for agent in data
        if str(agent.get("client_name", "")).lower() == args.client.lower()
    ]


def matches(left, right):
    return (
        normalize(left["hostname"]) == normalize(right["hostname"])
        or bool(left["ips"] & right["ips"])
    )


def format_ips(item):
    return ", ".join(sorted(item["ips"])) or "no IPv4/IPv6 reported"


def print_items(title, items, detail):
    print(f"\n{title}: {len(items)}")
    for item in sorted(items, key=lambda value: value["hostname"].lower()):
        print(f"  - {item['hostname']}: {detail(item)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--exceptions", type=Path, default=DEFAULT_EXCEPTIONS)
    parser.add_argument("--api-url", default=os.environ.get("TACTICAL_RMM_API_URL", "https://api.kbtech.org"))
    parser.add_argument("--client", default=os.environ.get("TACTICAL_RMM_CLIENT_NAME", "homelab"))
    parser.add_argument("--kube-server", default=os.environ.get("K3S_API_SERVER", "https://192.168.201.51:6443"))
    parser.add_argument("--secret-namespace", default="command-center")
    parser.add_argument("--secret-name", default="tactical-rmm-credentials")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when unresolved gaps remain.")
    args = parser.parse_args()

    inventory = parse_inventory(args.inventory)
    agents = tactical_agents(args)
    exceptions = load_exceptions(args.exceptions)
    ignored_groups = set(exceptions.get("ignored_inventory_groups", []))
    ignored_inventory = {normalize(name) for name in exceptions.get("ignored_inventory_hosts", {})}
    ignored_agents = {normalize(name) for name in exceptions.get("ignored_tactical_hosts", {})}

    inventory_gaps = [
        item for item in inventory
        if not any(matches(item, agent) for agent in agents)
        and normalize(item["hostname"]) not in ignored_inventory
        and not (item["groups"] & ignored_groups)
    ]
    agent_gaps = [
        agent for agent in agents
        if not any(matches(agent, item) for item in inventory)
        and normalize(agent["hostname"]) not in ignored_agents
    ]
    ignored_inventory_items = [
        item for item in inventory
        if normalize(item["hostname"]) in ignored_inventory or item["groups"] & ignored_groups
    ]

    print(f"Tactical client: {args.client}")
    print(f"Ansible inventory hosts: {len(inventory)}")
    print(f"Tactical agents: {len(agents)}")
    print_items(
        "Inventory hosts without a matching Tactical agent",
        inventory_gaps,
        lambda item: f"groups={','.join(sorted(item['groups']))}; ips={format_ips(item)}",
    )
    print_items(
        "Tactical agents outside central Ansible inventory",
        agent_gaps,
        lambda item: f"status={item['status']}; ips={format_ips(item)}",
    )
    print_items(
        "Ignored inventory entries",
        ignored_inventory_items,
        lambda item: f"groups={','.join(sorted(item['groups']))}",
    )

    if args.strict and (inventory_gaps or agent_gaps):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Audit failed: {exc}", file=sys.stderr)
        sys.exit(2)
