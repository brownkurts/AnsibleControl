#!/usr/bin/env python3
"""Run Tactical's configured Linux Mesh installer for reviewed agents."""

import argparse
import json
import sys
from urllib import request

from audit_tactical_inventory import tactical_api_key


def api_request(args, path, method="GET", payload=None):
    headers = {"X-API-KEY": tactical_api_key(args)}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    api_request = request.Request(
        args.api_url.rstrip("/") + path,
        headers=headers,
        data=data,
        method=method,
    )
    with request.urlopen(api_request, timeout=args.timeout + 30) as response:
        return response.status, response.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hostnames", nargs="+", help="Tactical agent hostnames to repair.")
    parser.add_argument("--api-url", default="https://api.kbtech.org")
    parser.add_argument("--client", default="homelab")
    parser.add_argument("--script-id", type=int, default=141)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--kube-server", default="https://192.168.201.51:6443")
    parser.add_argument("--secret-namespace", default="command-center")
    parser.add_argument("--secret-name", default="tactical-rmm-credentials")
    args = parser.parse_args()

    _, body = api_request(args, "/agents/")
    agents = json.loads(body)
    by_hostname = {
        str(agent.get("hostname", "")).lower(): agent
        for agent in agents
        if str(agent.get("client_name", "")).lower() == args.client.lower()
    }

    missing = [hostname for hostname in args.hostnames if hostname.lower() not in by_hostname]
    if missing:
        print("Missing Tactical agents: " + ", ".join(missing), file=sys.stderr)
        return 1

    payload = {
        "output": "wait",
        "emails": [],
        "emailMode": "default",
        "custom_field": None,
        "save_all_output": False,
        "script": args.script_id,
        "args": [],
        "env_vars": [],
        "run_as_user": False,
        "timeout": args.timeout,
    }
    failed = []
    for hostname in args.hostnames:
        agent = by_hostname[hostname.lower()]
        try:
            status, _ = api_request(
                args,
                f"/agents/{agent['agent_id']}/runscript/",
                method="POST",
                payload=payload,
            )
            print(f"{hostname}: Mesh installer completed (HTTP {status})")
        except Exception as exc:
            failed.append(hostname)
            print(f"{hostname}: Mesh installer failed: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
