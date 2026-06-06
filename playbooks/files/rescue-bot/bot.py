#!/usr/bin/env python3
"""rescue-bot: Level-1 IT ops assistant for KBTech homelab.

Triage flow for DOWN alerts:
  1. Parse service name from alert text
  2. Look up in service_map.json -> host + container/unit
  3. SSH to host, get status + last 60 log lines
  4. Match knowledge_base.json patterns -> auto-fix if known
  5. Fall back to Ollama with full context if unknown
  6. Offer to save new patterns to knowledge base

Knowledge base: /opt/rescue-bot/knowledge_base.json
Service map:    /opt/rescue-bot/service_map.json

Commands (in #alerts):
  !kb list                           - list all KB entries
  !kb auto <id> on|off               - toggle auto-fix for a KB entry
  !logs <service> [lines]            - fetch recent logs
  !status <service>                  - get service status
  !svcadd <name> <host> <container>  - add service to map
  !restart <service>                 - manually restart (asks confirm)
  !help                              - show this list
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, Request, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rescue-bot")

# ── Config ────────────────────────────────────────────────────────────────────
RC_URL         = os.environ["RC_URL"]
RC_BOT_TOKEN   = os.environ["RC_BOT_TOKEN"]
RC_BOT_USER_ID = os.environ["RC_BOT_USER_ID"]
NOTIFY_USER    = os.environ.get("NOTIFY_USER", "kbrown")
ALERTS_CHANNEL = os.environ.get("ALERTS_CHANNEL", "#alerts")
OLLAMA_URL     = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL   = os.environ.get("OLLAMA_MODEL", "llama3.2:1b")
SSH_KEY        = "/ssh/id_ed25519"
KB_FILE        = "/opt/rescue-bot/knowledge_base.json"
SVC_MAP_FILE   = "/opt/rescue-bot/service_map.json"

def is_scheduled_weather_report(text: str) -> bool:
    normalized = text.lower()
    return (
        "good morning!" in normalized
        and "3-day outlook" in normalized
        and "sunrise" in normalized
        and "sunset" in normalized
        and "*now:*" in normalized
        and "*today:*" in normalized
    )

# ── SSH ───────────────────────────────────────────────────────────────────────
SSH_BASE = [
    "ssh", "-i", SSH_KEY,
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
]

def ssh_exec(command: str, host: str, timeout: int = 60) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            SSH_BASE + [host, command],
            capture_output=True, text=True, timeout=timeout,
        )
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out[:5000]
    except subprocess.TimeoutExpired:
        return False, "SSH command timed out"
    except Exception as e:
        return False, str(e)

def host_exec(host_ip: str, command: str, timeout: int = 30) -> tuple[bool, str]:
    """Run a command directly on a homelab host via SSH."""
    return ssh_exec(command, host=f"kurt@{host_ip}", timeout=timeout)

# ── Docker helpers ────────────────────────────────────────────────────────────
def docker_status(host_ip: str, container: str) -> str:
    _, out = host_exec(host_ip,
        f"docker inspect --format '{{{{.State.Status}}}} exit={{{{.State.ExitCode}}}} oom={{{{.State.OOMKilled}}}}'"
        f" {container} 2>&1")
    return out.strip()

def docker_logs(host_ip: str, container: str, lines: int = 60) -> str:
    _, out = host_exec(host_ip, f"docker logs {container} --tail {lines} 2>&1", timeout=20)
    return out

def docker_restart(host_ip: str, container: str) -> tuple[bool, str]:
    return host_exec(host_ip, f"docker restart {container} 2>&1", timeout=60)

# ── Systemd helpers ───────────────────────────────────────────────────────────
def systemd_status(host_ip: str, unit: str) -> str:
    _, out = host_exec(host_ip, f"systemctl status {unit} --no-pager -l 2>&1 | head -30")
    return out

def systemd_logs(host_ip: str, unit: str, lines: int = 60) -> str:
    _, out = host_exec(host_ip, f"journalctl -u {unit} -n {lines} --no-pager 2>&1", timeout=20)
    return out

def systemd_restart(host_ip: str, unit: str) -> tuple[bool, str]:
    return host_exec(host_ip, f"sudo systemctl restart {unit} 2>&1", timeout=60)

# ── NFS ───────────────────────────────────────────────────────────────────────
def bounce_nfs(host_ip: str) -> tuple[bool, str]:
    return host_exec(host_ip, "sudo mount -a 2>&1", timeout=30)

# ── Knowledge base ────────────────────────────────────────────────────────────
DEFAULT_KB: list[dict] = [
    {
        "id": "intentional_stop",
        "name": "Service manually stopped",
        "description": "Exit code 0 or logs show explicit stop command — do not restart",
        "log_patterns": [
            "Stopped by",
            "ExecStop=",
            "systemctl stop",
            "Deactivated successfully",
            "Stopping.*service",
        ],
        "status_patterns": ["exit=0"],
        "fix": "none",
        "auto": True,
        "message": "Service was manually stopped — no automated action taken.",
    },
    {
        "id": "nfs_stale",
        "name": "NFS mount stale or missing",
        "description": "Container cannot access NFS paths — remount then restart fixes it",
        "log_patterns": [
            "Transport endpoint is not connected",
            "Stale file handle",
            "No such file or directory.*/mnt",
            "Input/output error",
            "cannot access.*/mnt",
            "failed to mount",
            "nfs: server.*not responding",
            "mount.*nfs.*failed",
        ],
        "status_patterns": [],
        "fix": "bounce_nfs_restart",
        "auto": True,
        "message": "NFS mount stale or missing — remounting and restarting service.",
    },
    {
        "id": "oom_killed",
        "name": "OOM killed",
        "description": "Container killed by kernel OOM killer — restart and flag memory concern",
        "log_patterns": [
            "OOMKilled",
            "out of memory",
            "Cannot allocate memory",
            "Killed.*oom",
        ],
        "status_patterns": ["oom=true"],
        "fix": "restart",
        "auto": True,
        "message": "OOM killed — restarting. :warning: Consider increasing container memory limit.",
    },
    {
        "id": "port_conflict",
        "name": "Port already in use",
        "description": "Another process holds the port — needs manual resolution",
        "log_patterns": [
            "address already in use",
            "EADDRINUSE",
            "bind.*failed.*address",
        ],
        "status_patterns": [],
        "fix": "none",
        "auto": False,
        "message": "Port conflict — another process holds the port. Manual intervention needed.",
    },
    {
        "id": "db_conn_refused",
        "name": "Database connection refused",
        "description": "DB container not ready or down — check DB first, not the app",
        "log_patterns": [
            "ECONNREFUSED.*5432",
            "ECONNREFUSED.*3306",
            "could not connect to.*postgres",
            "connection refused.*3306",
            "ECONNREFUSED.*27017",
        ],
        "status_patterns": [],
        "fix": "none",
        "auto": False,
        "message": "Database connection refused — check if the DB container is healthy first.",
    },
    {
        "id": "crash_loop",
        "name": "Crash loop / restarting",
        "description": "Container keeps crashing — needs log investigation before action",
        "log_patterns": [],
        "status_patterns": ["restarting"],
        "fix": "none",
        "auto": False,
        "message": "Container is in a crash loop. Logs attached — manual investigation needed.",
    },
]

def load_kb() -> list[dict]:
    try:
        with open(KB_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        save_kb(DEFAULT_KB)
        return list(DEFAULT_KB)
    except Exception as e:
        log.error(f"KB load error: {e}")
        return list(DEFAULT_KB)

def save_kb(kb: list[dict]) -> None:
    try:
        with open(KB_FILE, "w") as f:
            json.dump(kb, f, indent=2)
    except Exception as e:
        log.error(f"KB save error: {e}")

def match_kb(status: str, logs: str) -> dict | None:
    combined = (status + "\n" + logs).lower()
    for entry in load_kb():
        for pat in entry.get("log_patterns", []):
            if re.search(pat.lower(), combined):
                return entry
        for pat in entry.get("status_patterns", []):
            if re.search(pat.lower(), status.lower()):
                return entry
    return None

def add_kb_entry(entry: dict) -> str:
    kb = load_kb()
    kb = [e for e in kb if e.get("id") != entry.get("id")]
    kb.append(entry)
    save_kb(kb)
    return entry["id"]

# ── Service map ───────────────────────────────────────────────────────────────
# type: "docker" uses container=, "systemd"/"vm" uses unit=
DEFAULT_SVC_MAP: dict[str, dict] = {
    "rocketchat":          {"type": "docker",  "host": "192.168.201.70", "container": "rocketchat"},
    "rocket.chat":         {"type": "docker",  "host": "192.168.201.70", "container": "rocketchat"},
    "authentik":           {"type": "docker",  "host": "192.168.201.70", "container": "authentik-server"},
    "grafana":             {"type": "docker",  "host": "192.168.201.70", "container": "grafana"},
    "traefik":             {"type": "docker",  "host": "192.168.201.70", "container": "traefik"},
    "openwebui":           {"type": "docker",  "host": "192.168.201.70", "container": "openwebui"},
    "open-webui":          {"type": "docker",  "host": "192.168.201.70", "container": "openwebui"},
    "uptime-kuma":         {"type": "docker",  "host": "192.168.201.70", "container": "uptime-kuma"},
    "uptime kuma":         {"type": "docker",  "host": "192.168.201.70", "container": "uptime-kuma"},
    "netbox":              {"type": "docker",  "host": "192.168.201.70", "container": "netbox"},
    "homepage":            {"type": "docker",  "host": "192.168.201.70", "container": "homepage"},
    "jellystat":           {"type": "docker",  "host": "192.168.201.70", "container": "jellystat"},
    "alertmanager":        {"type": "docker",  "host": "192.168.201.70", "container": "alertmanager"},
    "orbital-sync":        {"type": "docker",  "host": "192.168.201.70", "container": "orbital-sync"},
    "command-center":      {"type": "docker",  "host": "192.168.201.70", "container": "command-center"},
    "jellyfin":            {"type": "docker",  "host": "192.168.201.71", "container": "jellyfin"},
    "prowlarr":            {"type": "docker",  "host": "192.168.201.71", "container": "prowlarr"},
    "frigate":             {"type": "docker",  "host": "192.168.201.71", "container": "frigate"},
    "watchstate":          {"type": "docker",  "host": "192.168.201.71", "container": "watchstate"},
    "seerr":               {"type": "docker",  "host": "192.168.201.71", "container": "seerr"},
    "nextcloud":           {"type": "vm",      "host": "192.168.201.57", "unit": "apache2"},
    "plex":                {"type": "vm",      "host": "192.168.201.10", "unit": "plexmediaserver"},
    "hammond-monitor":     {"type": "systemd", "host": "192.168.2.33",   "unit": "hammond-monitor"},
    "proxmox-alert-relay": {"type": "systemd", "host": "192.168.2.33",   "unit": "proxmox-alert-relay"},
    "tautulli-webhook":    {"type": "systemd", "host": "192.168.2.33",   "unit": "tautulli-webhook"},
}

def load_svc_map() -> dict:
    try:
        with open(SVC_MAP_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        save_svc_map(DEFAULT_SVC_MAP)
        return dict(DEFAULT_SVC_MAP)
    except Exception as e:
        log.error(f"Service map load error: {e}")
        return dict(DEFAULT_SVC_MAP)

def save_svc_map(svc_map: dict) -> None:
    try:
        with open(SVC_MAP_FILE, "w") as f:
            json.dump(svc_map, f, indent=2)
    except Exception as e:
        log.error(f"Service map save error: {e}")

def lookup_service(name: str) -> dict | None:
    svc_map = load_svc_map()
    key = name.lower().strip()
    if key in svc_map:
        return svc_map[key]
    for k, v in svc_map.items():
        if k in key or key in k:
            return v
    return None

# ── Alert parser ──────────────────────────────────────────────────────────────
def parse_alert(text: str) -> tuple[str | None, bool]:
    """Return (service_name, is_down). is_down=False means recovery — no action needed."""
    is_up = bool(re.search(
        r'🟢|✅|\bup\b|\brecovered\b|\bback online\b|\bback up\b|\brestored\b',
        text, re.IGNORECASE))

    # *bold* service name (Hammond monitor / Uptime Kuma format)
    m = re.search(r'\*([^*]+)\*', text)
    if m:
        return m.group(1).strip(), not is_up

    # "[Down] ServiceName" or "[Up] ServiceName"
    m = re.search(r'\[(?:Down|Up)\]\s+([^\s(]+)', text, re.IGNORECASE)
    if m:
        return m.group(1).strip(), not is_up

    # "service X is down/up"
    m = re.search(r'service\s+(\S+)\s+is', text, re.IGNORECASE)
    if m:
        return m.group(1).strip(), not is_up

    return None, not is_up

# ── RC helpers ────────────────────────────────────────────────────────────────
async def rc_post(path: str, payload: dict) -> dict:
    async with httpx.AsyncClient(verify=False, timeout=15) as c:
        r = await c.post(
            f"{RC_URL}/api/v1/{path}", json=payload,
            headers={"X-Auth-Token": RC_BOT_TOKEN, "X-User-Id": RC_BOT_USER_ID},
        )
        r.raise_for_status()
        return r.json()

async def send_msg(room_id: str, text: str, tmid: str | None = None) -> dict:
    body: dict[str, Any] = {"roomId": room_id, "text": text}
    if tmid:
        body["tmid"] = tmid
    return await rc_post("chat.postMessage", body)

async def send_msg_channel(channel: str, text: str) -> dict:
    return await rc_post("chat.postMessage", {"channel": channel, "text": text})

async def dm(text: str) -> None:
    try:
        await rc_post("chat.postMessage", {"channel": f"@{NOTIFY_USER}", "text": text})
    except Exception as e:
        log.warning(f"DM failed: {e}")

async def ensure_bot_in_room(room_id: str) -> None:
    try:
        await rc_post("channels.join", {"roomId": room_id})
    except Exception:
        pass

# ── Fix executor ──────────────────────────────────────────────────────────────
async def apply_fix(fix: str, svc: dict) -> str:
    host      = svc["host"]
    svc_type  = svc.get("type", "docker")
    container = svc.get("container", "")
    unit      = svc.get("unit", container)

    if fix == "none":
        return ""

    if fix == "bounce_nfs_restart":
        ok_mount, mount_out = await asyncio.to_thread(bounce_nfs, host)
        await asyncio.sleep(3)
        if svc_type == "docker":
            ok_svc, svc_out = await asyncio.to_thread(docker_restart, host, container)
        else:
            ok_svc, svc_out = await asyncio.to_thread(systemd_restart, host, unit)
        return (
            f"mount -a: {'OK' if ok_mount else 'FAILED'} — {mount_out[:200]}\n"
            f"restart:  {'OK' if ok_svc else 'FAILED'} — {svc_out[:200]}"
        )

    if fix == "restart":
        if svc_type == "docker":
            ok, out = await asyncio.to_thread(docker_restart, host, container)
        else:
            ok, out = await asyncio.to_thread(systemd_restart, host, unit)
        return f"{'OK' if ok else 'FAILED'}: {out[:400]}"

    return f"unknown fix type: {fix}"

# ── Ollama ────────────────────────────────────────────────────────────────────
OLLAMA_SYSTEM = """You are rescue-bot, the Level-1 ops assistant for the KBTech homelab.
Platform: Docker containers on Proxmox VMs. K8s/K3s is RETIRED — never suggest kubectl.

Docker hosts:
- prometheus (192.168.201.70): rocketchat, authentik, grafana, traefik, openwebui, uptime-kuma, netbox
- apollo (192.168.201.71): jellyfin, prowlarr, frigate, seerr
- hammond (192.168.2.33): systemd services — hammond-monitor, proxmox-alert-relay
- nextcloud VM (192.168.201.57): apache2 (systemd)
- plex VM destiny (192.168.201.10): plexmediaserver (systemd)
- NFS storage from Unraid at 192.168.2.21 (mounted at /mnt/... on docker hosts)

Given the alert, container status, and recent logs, respond with JSON only:
{
  "diagnosis": "one sentence root cause",
  "fix": "restart|bounce_nfs_restart|none",
  "confidence": "high|medium|low",
  "explanation": "2-3 sentences — what happened and recommended next steps",
  "kb_pattern": "a regex string that would match this in future logs, or null",
  "kb_id": "short_snake_case_id for this issue type, or null"
}

Rules:
- If exit code is 0 or logs mention systemctl stop / ExecStop: fix=none, say it was intentional.
- If logs show NFS/mount errors (Transport endpoint, Stale file handle, /mnt paths): fix=bounce_nfs_restart.
- If container crashed unexpectedly (non-zero exit, no clear cause): fix=restart.
- confidence=high only when logs clearly show the cause.
- Output valid JSON only — no markdown, no extra text."""

async def call_ollama(alert: str, status: str, logs: str) -> dict:
    prompt = f"Alert: {alert}\n\nStatus:\n{status}\n\nRecent logs (last 60 lines):\n{logs}"
    async with httpx.AsyncClient(timeout=300) as c:
        r = await c.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "system": OLLAMA_SYSTEM,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "keep_alive": -1,
                "options": {"temperature": 0.1, "num_predict": 512},
            },
        )
        r.raise_for_status()
        return json.loads(r.json()["response"])

# ── Pending confirmations ─────────────────────────────────────────────────────
pending: dict[str, dict] = {}

# ── Main triage ───────────────────────────────────────────────────────────────
async def triage(alert_text: str, room_id: str, thread_id: str | None) -> None:
    log.info(f"Triage: {alert_text[:100]}")
    await ensure_bot_in_room(room_id)

    service_name, is_down = parse_alert(alert_text)

    # Recovery alert — just acknowledge
    if not is_down:
        if service_name:
            await send_msg(room_id,
                f":white_check_mark: *{service_name}* is back up — no action needed.",
                tmid=thread_id)
        return

    if not service_name:
        await send_msg(room_id,
            ":mag: Alert received but couldn't parse a service name. Check manually.",
            tmid=thread_id)
        return

    svc = lookup_service(service_name)
    if not svc:
        await send_msg(room_id,
            f":warning: *{service_name}* is not in my service map — can't auto-investigate.\n"
            f"Add it with: `!svcadd {service_name} <host_ip> <container_name>`",
            tmid=thread_id)
        return

    host      = svc["host"]
    svc_type  = svc.get("type", "docker")
    container = svc.get("container", "")
    unit      = svc.get("unit", container)

    await send_msg(room_id,
        f":mag: *{service_name}* is down — checking `{host}`...",
        tmid=thread_id)

    # Gather status + logs
    if svc_type == "docker":
        status = await asyncio.to_thread(docker_status, host, container)
        logs   = await asyncio.to_thread(docker_logs, host, container)
    else:
        status = await asyncio.to_thread(systemd_status, host, unit)
        logs   = await asyncio.to_thread(systemd_logs, host, unit)

    log.info(f"[{service_name}] status: {status[:80]}")

    # ── Knowledge base match ──────────────────────────────────────────────────
    kb_match = match_kb(status, logs)
    if kb_match:
        fix  = kb_match.get("fix", "none")
        name = kb_match["name"]
        msg  = kb_match["message"]
        auto = kb_match.get("auto", False)

        if fix == "none":
            await send_msg(room_id,
                f":clipboard: *{name}*\n{msg}\n```\n{status}\n```",
                tmid=thread_id)
            return

        if auto:
            await send_msg(room_id, f":wrench: *{name}* — {msg}", tmid=thread_id)
            result = await apply_fix(fix, svc)
            await send_msg(room_id,
                f":white_check_mark: Fix applied (`{fix}`):\n```\n{result}\n```",
                tmid=thread_id)
            await dm(f"Auto-fix on *{service_name}*: {name}\n{result[:400]}")
            return

        # KB match but not auto — ask
        ask = await send_msg(room_id,
            f":clipboard: *{name}* — {msg}\n"
            f"```\nStatus: {status}\n```\n"
            f":wrench: Apply fix `{fix}`? Reply `yes` or `no`.",
            tmid=thread_id)
        root = thread_id or ask["message"]["_id"]
        pending[root] = {
            "type": "fix", "fix": fix, "svc": svc,
            "service_name": service_name,
            "room_id": room_id, "thread_id": thread_id,
            "asked_at": time.time(),
        }
        return

    # ── No KB match — call Ollama with full log context ───────────────────────
    await send_msg(room_id,
        ":brain: No known pattern — asking Ollama to analyze logs...",
        tmid=thread_id)

    try:
        result = await call_ollama(alert_text, status, logs)
    except Exception as e:
        log.error(f"Ollama error: {e}")
        await send_msg(room_id,
            f":x: Ollama error: {e}\n\nStatus: `{status}`\nLogs:\n```\n{logs[-800:]}\n```",
            tmid=thread_id)
        return

    diagnosis   = result.get("diagnosis", "Unknown issue")
    fix         = result.get("fix", "none")
    confidence  = result.get("confidence", "low")
    explanation = result.get("explanation", "")
    kb_pattern  = result.get("kb_pattern")
    kb_id       = result.get("kb_id")

    summary = f":mag: *{diagnosis}*\n{explanation}\n_Confidence: {confidence}_"

    if fix == "none" or confidence == "low":
        await send_msg(room_id,
            f"{summary}\n\nLogs:\n```\n{logs[-800:]}\n```",
            tmid=thread_id)
        return

    save_hint = "\nReply `yes save` to remember this fix for next time." if kb_pattern else ""
    ask = await send_msg(room_id,
        f"{summary}\n\n:wrench: Proposed fix: `{fix}` — apply? Reply `yes` or `no`.{save_hint}",
        tmid=thread_id)
    root = thread_id or ask["message"]["_id"]
    pending[root] = {
        "type": "fix", "fix": fix, "svc": svc,
        "service_name": service_name,
        "room_id": room_id, "thread_id": thread_id,
        "asked_at": time.time(),
        "kb_pattern": kb_pattern, "kb_id": kb_id,
        "diagnosis": diagnosis,
    }

# ── Confirmation reply handler ────────────────────────────────────────────────
async def handle_reply(room_id: str, tmid: str, text: str) -> None:
    entry = pending.get(tmid)
    if not entry:
        return
    answer   = text.strip().lower()
    save_it  = "save" in answer
    approved = any(w in answer for w in ("yes", "y", "go", "ok", "approve"))
    denied   = any(w in answer for w in ("no", "n", "skip", "cancel", "abort"))

    if approved:
        del pending[tmid]
        fix          = entry["fix"]
        svc          = entry["svc"]
        service_name = entry.get("service_name", "service")
        result = await apply_fix(fix, svc)
        await send_msg(room_id,
            f":white_check_mark: Fix applied (`{fix}`) on *{service_name}*:\n```\n{result}\n```",
            tmid=tmid)
        await dm(f"Fix applied: `{fix}` on *{service_name}*\n{result[:400]}")

        if save_it and entry.get("kb_pattern"):
            new_entry = {
                "id":           entry.get("kb_id") or f"learned_{int(time.time())}",
                "name":         entry.get("diagnosis", "Learned fix"),
                "description":  f"Learned from {service_name} incident — set auto=true to apply automatically",
                "log_patterns": [entry["kb_pattern"]],
                "status_patterns": [],
                "fix":          fix,
                "auto":         False,
                "message":      entry.get("diagnosis", "Applying learned fix."),
            }
            saved_id = add_kb_entry(new_entry)
            await send_msg(room_id,
                f":books: Pattern saved to knowledge base as `{saved_id}`.\n"
                f"Run `!kb auto {saved_id} on` to auto-apply next time.",
                tmid=tmid)

    elif denied:
        del pending[tmid]
        await send_msg(room_id, ":no_entry_sign: Skipped.", tmid=tmid)

# ── Command handler ───────────────────────────────────────────────────────────
async def handle_command(text: str, room_id: str, thread_id: str | None) -> None:
    t = text.strip()

    # !svcadd <name> <host> <container>
    m = re.match(r'!svcadd\s+(\S+)\s+(\S+)\s+(\S+)', t, re.IGNORECASE)
    if m:
        name, host, container = m.group(1), m.group(2), m.group(3)
        svc_map = load_svc_map()
        svc_map[name.lower()] = {"type": "docker", "host": host, "container": container}
        save_svc_map(svc_map)
        await send_msg(room_id,
            f":white_check_mark: Added `{name}` -> container `{container}` on `{host}` to service map.",
            tmid=thread_id)
        return

    # !kb list
    if re.match(r'!kb\s+list', t, re.IGNORECASE):
        kb = load_kb()
        lines = [
            f"* `{e['id']}` -- {e['name']}\n"
            f"  fix: `{e.get('fix','none')}` | auto: `{e.get('auto', False)}`"
            for e in kb
        ]
        await send_msg(room_id, ":books: *Knowledge base:*\n" + "\n".join(lines), tmid=thread_id)
        return

    # !kb auto <id> on|off
    m = re.match(r'!kb\s+auto\s+(\S+)\s+(on|off)', t, re.IGNORECASE)
    if m:
        kb_id, toggle = m.group(1), m.group(2).lower() == "on"
        kb = load_kb()
        updated = False
        for e in kb:
            if e.get("id") == kb_id:
                e["auto"] = toggle
                updated = True
        if updated:
            save_kb(kb)
            await send_msg(room_id,
                f":white_check_mark: `{kb_id}` auto-fix set to `{toggle}`.",
                tmid=thread_id)
        else:
            await send_msg(room_id, f":x: No KB entry with id `{kb_id}`.", tmid=thread_id)
        return

    # !logs <service> [lines]
    m = re.match(r'!logs\s+(\S+)(?:\s+(\d+))?', t, re.IGNORECASE)
    if m:
        service_name = m.group(1)
        lines = int(m.group(2)) if m.group(2) else 40
        svc = lookup_service(service_name)
        if not svc:
            await send_msg(room_id, f":x: `{service_name}` not in service map.", tmid=thread_id)
            return
        host = svc["host"]
        if svc.get("type") == "docker":
            logs = await asyncio.to_thread(docker_logs, host, svc["container"], lines)
        else:
            logs = await asyncio.to_thread(systemd_logs, host, svc.get("unit", ""), lines)
        await send_msg(room_id,
            f":clipboard: *{service_name}* logs (last {lines}):\n```\n{logs[-2000:]}\n```",
            tmid=thread_id)
        return

    # !status <service>
    m = re.match(r'!status\s+(\S+)', t, re.IGNORECASE)
    if m:
        service_name = m.group(1)
        svc = lookup_service(service_name)
        if not svc:
            await send_msg(room_id, f":x: `{service_name}` not in service map.", tmid=thread_id)
            return
        host = svc["host"]
        if svc.get("type") == "docker":
            status = await asyncio.to_thread(docker_status, host, svc["container"])
        else:
            status = await asyncio.to_thread(systemd_status, host, svc.get("unit", ""))
        await send_msg(room_id,
            f":bar_chart: *{service_name}* on `{host}`:\n```\n{status}\n```",
            tmid=thread_id)
        return

    # !restart <service>
    m = re.match(r'!restart\s+(\S+)', t, re.IGNORECASE)
    if m:
        service_name = m.group(1)
        svc = lookup_service(service_name)
        if not svc:
            await send_msg(room_id, f":x: `{service_name}` not in service map.", tmid=thread_id)
            return
        ask = await send_msg(room_id,
            f":wrench: Restart *{service_name}* on `{svc['host']}`? Reply `yes` or `no`.",
            tmid=thread_id)
        root = thread_id or ask["message"]["_id"]
        pending[root] = {
            "type": "fix", "fix": "restart", "svc": svc,
            "service_name": service_name,
            "room_id": room_id, "thread_id": thread_id,
            "asked_at": time.time(),
        }
        return

    # !help
    if re.match(r'!help', t, re.IGNORECASE):
        await send_msg(room_id, (
            ":robot: *rescue-bot commands:*\n"
            "* `!kb list` -- show knowledge base entries\n"
            "* `!kb auto <id> on|off` -- toggle auto-fix for a KB entry\n"
            "* `!logs <service> [lines]` -- fetch recent logs\n"
            "* `!status <service>` -- get service status\n"
            "* `!restart <service>` -- restart a service (asks confirm)\n"
            "* `!svcadd <name> <host> <container>` -- add service to map\n"
            "\nWhen I propose a fix, reply `yes save` to add the pattern to the KB."
        ), tmid=thread_id)
        return

# ── FastAPI ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        await send_msg_channel(ALERTS_CHANNEL,
            ":robot: rescue-bot online -- L1 triage active. Type `!help` for commands.")
    except Exception as e:
        log.warning(f"Startup announce failed: {e}")
    yield

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def webhook(request: Request, bg: BackgroundTasks) -> Response:
    try:
        data = await request.json()
    except Exception:
        return Response(status_code=200)

    user    = data.get("user_name", "")
    text    = (data.get("text") or "").strip()
    ch_name = data.get("channel_name", "")
    room_id = data.get("channel_id", "")
    msg_id  = data.get("message_id", "")
    tmid    = data.get("tmid")

    if user in ("rescue-bot", "rescue.bot") or not text or not room_id:
        return Response(status_code=200)

    if ch_name == "homelab" and is_scheduled_weather_report(text):
        return Response(status_code=200)

    # Expire stale pending after 10 min
    now = time.time()
    for k in [k for k, v in list(pending.items()) if now - v["asked_at"] > 600]:
        del pending[k]

    # Reply to pending confirmation
    if tmid and tmid in pending:
        bg.add_task(handle_reply, room_id, tmid, text)
        return Response(status_code=200)

    # Commands
    if text.startswith("!"):
        bg.add_task(handle_command, text, room_id, tmid or msg_id or None)
        return Response(status_code=200)

    # Alert channels
    if ch_name in ("alerts", "homelab") or "rescue-bot" in text.lower():
        bg.add_task(triage, text, room_id, tmid or msg_id or None)

    return Response(status_code=200)

@app.get("/health")
async def health():
    return {
        "ok": True,
        "kb_entries": len(load_kb()),
        "svc_map_entries": len(load_svc_map()),
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=8080, log_level="info")
