#!/usr/bin/env python3
"""Periodic media-stack watchdog for rescue-01.

Checks the Arr stack, downloaders, and media servers, keeps a small amount of
state between runs, and alerts only when something changes or starts stalling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("media-watchdog")

RC_URL = os.environ["RC_URL"]
RC_BOT_TOKEN = os.environ["RC_BOT_TOKEN"]
RC_BOT_USER_ID = os.environ["RC_BOT_USER_ID"]
NOTIFY_USER = os.environ.get("NOTIFY_USER", "kbrown")
ALERTS_CHANNEL = os.environ.get("ALERTS_CHANNEL", "#alerts")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:1b")
STATE_FILE = Path(os.environ.get("MEDIA_WATCHDOG_STATE_FILE", "/opt/rescue-bot/media-watchdog-state.json"))
SERIES_SPECS = [
    item.strip()
    for item in os.environ.get("MEDIA_WATCHDOG_SERIES_NAMES", "Paw Patrol:41").split(",")
    if item.strip()
]
ISSUE_STALL_RUNS = int(os.environ.get("MEDIA_WATCHDOG_STALL_RUNS", "3"))
COMMAND_LOOKBACK_HOURS = int(os.environ.get("MEDIA_WATCHDOG_COMMAND_LOOKBACK_HOURS", "12"))
# Single transient command failures (e.g. one RefreshMonitoredDownloads hiccup) are
# normal arr churn — only raise an issue when failures accumulate.
FAILED_COMMAND_ISSUE_MIN = int(os.environ.get("MEDIA_WATCHDOG_FAILED_COMMAND_ISSUE_MIN", "3"))

SONARR_URL = os.environ.get("SONARR_URL", "").rstrip("/")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")
RADARR_URL = os.environ.get("RADARR_URL", "").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
PROWLARR_URL = os.environ.get("PROWLARR_URL", "").rstrip("/")
PROWLARR_API_KEY = os.environ.get("PROWLARR_API_KEY", "")
SABNZBD_URL = os.environ.get("SABNZBD_URL", "").rstrip("/")
SABNZBD_API_KEY = os.environ.get("SABNZBD_API_KEY", "")
QBITTORRENT_URL = os.environ.get("QBITTORRENT_URL", "").rstrip("/")
QBITTORRENT_USERNAME = os.environ.get("QBITTORRENT_USERNAME", "")
QBITTORRENT_PASSWORD = os.environ.get("QBITTORRENT_PASSWORD", "")
PLEX_URL = os.environ.get("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


@dataclass(slots=True)
class Issue:
    key: str
    severity: str
    message: str


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        log.warning("State file is invalid JSON: %s", exc)
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


async def rc_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(verify=False, timeout=20) as client:
        response = await client.post(
            f"{RC_URL}/api/v1/{path}",
            json=payload,
            headers={"X-Auth-Token": RC_BOT_TOKEN, "X-User-Id": RC_BOT_USER_ID},
        )
        response.raise_for_status()
        return response.json()


async def send_telegram(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        )
        response.raise_for_status()


async def notify(text: str) -> None:
    await rc_post("chat.postMessage", {"channel": ALERTS_CHANNEL, "text": text})
    await rc_post("chat.postMessage", {"channel": f"@{NOTIFY_USER}", "text": text})
    try:
        await send_telegram(text)
    except Exception as exc:
        log.warning("Telegram notify failed: %s", exc)


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[bool, Any]:
    try:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()
        return True, response.json()
    except Exception as exc:
        return False, str(exc)


async def get_text(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    try:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()
        return True, response.text.strip()
    except Exception as exc:
        return False, str(exc)


def english_audio_state(episode_file: dict[str, Any]) -> str:
    media = episode_file.get("mediaInfo") or {}
    tokens: list[str] = []
    for key in ("audioLanguages", "audioLanguage"):
        value = media.get(key)
        if value:
            tokens.append(str(value).lower())
    for track in media.get("audioTracks") or []:
        for key in ("language", "languageCode", "title"):
            value = track.get(key)
            if value:
                tokens.append(str(value).lower())
    text = " ".join(tokens)
    if not text.strip():
        return "unknown"
    if "eng" in text or "english" in text:
        return "english"
    return "non_english"


async def collect_arr_health(
    name: str,
    base_url: str,
    api_key: str,
    api_version: str,
    series_specs: list[str] | None = None,
) -> dict[str, Any]:
    if not base_url or not api_key:
        return {"configured": False, "name": name}

    headers = {"X-Api-Key": api_key}
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        status_ok, system_status = await get_json(client, f"{base_url}/api/{api_version}/system/status", headers=headers)
        queue_ok, queue_status = await get_json(client, f"{base_url}/api/{api_version}/queue/status", headers=headers)
        commands_ok, commands = await get_json(client, f"{base_url}/api/{api_version}/command", headers=headers)

        snapshot: dict[str, Any] = {
            "configured": True,
            "name": name,
            "up": status_ok,
            "status": system_status if status_ok else None,
            "error": None if status_ok else system_status,
            "queue_status": queue_status if queue_ok else {},
            "queue_status_error": None if queue_ok else queue_status,
            "recent_failed_command_ids": [],
            "recent_completed_command_ids": [],
            "series": {},
        }

        if commands_ok:
            cutoff = utc_now() - timedelta(hours=COMMAND_LOOKBACK_HOURS)
            for command in commands:
                started = command.get("startedOn")
                started_at = None
                if isinstance(started, str):
                    try:
                        started_at = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    except ValueError:
                        started_at = None
                if started_at and started_at < cutoff:
                    continue
                status = str(command.get("status", "")).lower()
                command_id = command.get("id")
                if command_id is None:
                    continue
                if status == "failed":
                    snapshot["recent_failed_command_ids"].append(int(command_id))
                elif status == "completed":
                    snapshot["recent_completed_command_ids"].append(int(command_id))

        if series_specs and name == "sonarr" and status_ok:
            unresolved_names: list[str] = []
            resolved_specs: list[tuple[str, int]] = []

            for raw_spec in series_specs:
                if ":" in raw_spec:
                    label, _, raw_id = raw_spec.partition(":")
                    label = label.strip() or raw_spec.strip()
                    raw_id = raw_id.strip()
                    if raw_id.isdigit():
                        resolved_specs.append((label, int(raw_id)))
                        continue
                unresolved_names.append(raw_spec)

            if unresolved_names:
                series_ok, series_list = await get_json(client, f"{base_url}/api/{api_version}/series", headers=headers)
                if series_ok:
                    by_name = {item.get("title", "").strip().lower(): item for item in series_list}
                    for series_name in unresolved_names:
                        series = by_name.get(series_name.lower())
                        if not series:
                            snapshot["series"][series_name] = {"found": False}
                            continue
                        resolved_specs.append((series_name, int(series["id"])))
                else:
                    for series_name in unresolved_names:
                        snapshot["series"][series_name] = {"found": False}

            for series_name, series_id in resolved_specs:
                    episodes_ok, episodes = await get_json(
                        client,
                        f"{base_url}/api/{api_version}/episode",
                        headers=headers,
                        params={"seriesId": series_id},
                    )
                    files_ok, episode_files = await get_json(
                        client,
                        f"{base_url}/api/{api_version}/episodeFile",
                        headers=headers,
                        params={"seriesId": series_id},
                    )
                    series_snapshot = {
                        "found": True,
                        "series_id": series_id,
                        "missing_episodes": 0,
                        "episode_file_count": 0,
                        "english_files": 0,
                        "unknown_audio_files": 0,
                        "non_english_files": 0,
                    }
                    if episodes_ok:
                        series_snapshot["missing_episodes"] = sum(
                            1 for episode in episodes if not episode.get("hasFile")
                        )
                    if files_ok:
                        series_snapshot["episode_file_count"] = len(episode_files)
                        for episode_file in episode_files:
                            state = english_audio_state(episode_file)
                            if state == "english":
                                series_snapshot["english_files"] += 1
                            elif state == "unknown":
                                series_snapshot["unknown_audio_files"] += 1
                            else:
                                series_snapshot["non_english_files"] += 1
                    snapshot["series"][series_name] = series_snapshot

        return snapshot


async def collect_prowlarr_health() -> dict[str, Any]:
    if not PROWLARR_URL or not PROWLARR_API_KEY:
        return {"configured": False, "name": "prowlarr"}
    headers = {"X-Api-Key": PROWLARR_API_KEY}
    async with httpx.AsyncClient(verify=False, timeout=20) as client:
        ok, data = await get_json(client, f"{PROWLARR_URL}/api/v1/system/status", headers=headers)
    return {
        "configured": True,
        "name": "prowlarr",
        "up": ok,
        "status": data if ok else None,
        "error": None if ok else data,
    }


async def collect_sab_health() -> dict[str, Any]:
    if not SABNZBD_URL or not SABNZBD_API_KEY:
        return {"configured": False, "name": "sabnzbd"}
    params = {"mode": "queue", "output": "json", "apikey": SABNZBD_API_KEY}
    async with httpx.AsyncClient(timeout=20) as client:
        ok, data = await get_json(client, f"{SABNZBD_URL}/api", params=params)
    queue = data.get("queue", {}) if ok else {}
    warnings = queue.get("warnings", [])
    if isinstance(warnings, str):
        warnings = [warnings] if warnings else []
    return {
        "configured": True,
        "name": "sabnzbd",
        "up": ok,
        "status": queue if ok else None,
        "error": None if ok else data,
        "paused": bool(queue.get("paused")) if ok else None,
        "queue_size": int(queue.get("noofslots_total", 0)) if ok else None,
        "download_speed_kbps": float(queue.get("kbpersec", 0.0)) if ok else None,
        "warnings_count": len(warnings),
    }


async def collect_qbit_health() -> dict[str, Any]:
    if not QBITTORRENT_URL or not QBITTORRENT_USERNAME or not QBITTORRENT_PASSWORD:
        return {"configured": False, "name": "qbittorrent"}
    base_headers = {"Referer": QBITTORRENT_URL}
    async with httpx.AsyncClient(verify=False, timeout=25, auth=(QBITTORRENT_USERNAME, QBITTORRENT_PASSWORD)) as client:
        try:
            await client.post(
                f"{QBITTORRENT_URL}/api/v2/auth/login",
                data={"username": QBITTORRENT_USERNAME, "password": QBITTORRENT_PASSWORD},
                headers=base_headers,
            )
        except Exception:
            pass
        version_ok, version = await get_text(client, f"{QBITTORRENT_URL}/api/v2/app/version", headers=base_headers)
        transfer_ok, transfer = await get_json(client, f"{QBITTORRENT_URL}/api/v2/transfer/info", headers=base_headers)
        torrents_ok, torrents = await get_json(
            client,
            f"{QBITTORRENT_URL}/api/v2/torrents/info",
            headers=base_headers,
            params={"filter": "all"},
        )
    if not version_ok or not transfer_ok:
        return {
            "configured": True,
            "name": "qbittorrent",
            "up": False,
            "status": None,
            "error": version if not version_ok else transfer,
        }
    torrents = torrents if torrents_ok and isinstance(torrents, list) else []
    downloading = [item for item in torrents if "down" in str(item.get("state", "")).lower()]
    metadata = [
        item
        for item in torrents
        if str(item.get("state", "")).lower() in {"metadl", "forcedmetadl", "checkingdl"}
    ]
    return {
        "configured": True,
        "name": "qbittorrent",
        "up": True,
        "version": version,
        "transfer": transfer,
        "downloading_count": len(downloading),
        "metadata_count": len(metadata),
        "active_count": len([item for item in torrents if item.get("dlspeed", 0) or item.get("upspeed", 0)]),
    }


async def collect_plex_health() -> dict[str, Any]:
    if not PLEX_URL or not PLEX_TOKEN:
        return {"configured": False, "name": "plex"}
    headers = {"X-Plex-Token": PLEX_TOKEN}
    async with httpx.AsyncClient(timeout=20) as client:
        ok, text = await get_text(client, f"{PLEX_URL}/identity", headers=headers)
    return {
        "configured": True,
        "name": "plex",
        "up": ok,
        "error": None if ok else text,
    }


async def collect_jellyfin_health() -> dict[str, Any]:
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        return {"configured": False, "name": "jellyfin"}
    headers = {"X-Emby-Token": JELLYFIN_API_KEY}
    async with httpx.AsyncClient(verify=False, timeout=20) as client:
        ok, data = await get_json(client, f"{JELLYFIN_URL}/System/Info", headers=headers)
    return {
        "configured": True,
        "name": "jellyfin",
        "up": ok,
        "status": data if ok else None,
        "error": None if ok else data,
    }


def build_issues(snapshot: dict[str, Any], previous: dict[str, Any]) -> tuple[list[Issue], dict[str, int]]:
    counters = {
        "qbit_zero_speed_runs": int(previous.get("counters", {}).get("qbit_zero_speed_runs", 0)),
        "sab_zero_speed_runs": int(previous.get("counters", {}).get("sab_zero_speed_runs", 0)),
    }
    issues: list[Issue] = []

    for service_name in ("sonarr", "radarr", "prowlarr", "plex", "jellyfin", "qbittorrent", "sabnzbd"):
        service = snapshot.get(service_name, {})
        if service.get("configured") and not service.get("up", False):
            issues.append(Issue(f"{service_name}:down", "critical", f"{service_name} is unreachable or returning an error"))

    sonarr = snapshot.get("sonarr", {})
    if sonarr.get("configured") and sonarr.get("up"):
        queue_status = sonarr.get("queue_status", {})
        if queue_status.get("warnings"):
            issues.append(
                Issue(
                    "sonarr:queue-warnings",
                    "warning",
                    f"Sonarr queue has warnings with {queue_status.get('totalCount', 0)} items",
                )
            )
        if len(sonarr.get("recent_failed_command_ids", [])) >= FAILED_COMMAND_ISSUE_MIN:
            issues.append(
                Issue(
                    "sonarr:failed-commands",
                    "warning",
                    f"Sonarr has {len(sonarr['recent_failed_command_ids'])} failed commands in the last {COMMAND_LOOKBACK_HOURS}h",
                )
            )

    radarr = snapshot.get("radarr", {})
    if radarr.get("configured") and radarr.get("up"):
        queue_status = radarr.get("queue_status", {})
        if queue_status.get("warnings"):
            issues.append(
                Issue(
                    "radarr:queue-warnings",
                    "warning",
                    f"Radarr queue has warnings with {queue_status.get('totalCount', 0)} items",
                )
            )
        if len(radarr.get("recent_failed_command_ids", [])) >= FAILED_COMMAND_ISSUE_MIN:
            issues.append(
                Issue(
                    "radarr:failed-commands",
                    "warning",
                    f"Radarr has {len(radarr['recent_failed_command_ids'])} failed commands in the last {COMMAND_LOOKBACK_HOURS}h",
                )
            )

    qbit = snapshot.get("qbittorrent", {})
    if qbit.get("configured") and qbit.get("up"):
        transfer = qbit.get("transfer", {})
        downloading = int(qbit.get("downloading_count", 0))
        metadata = int(qbit.get("metadata_count", 0))
        speed = int(transfer.get("dl_info_speed", 0))
        if (downloading > 0 or metadata > 0) and speed == 0:
            counters["qbit_zero_speed_runs"] += 1
        else:
            counters["qbit_zero_speed_runs"] = 0
        if counters["qbit_zero_speed_runs"] >= ISSUE_STALL_RUNS:
            issues.append(
                Issue(
                    "qbittorrent:stalled",
                    "warning",
                    f"qBittorrent has {downloading + metadata} active items but download speed has been 0 B/s for {counters['qbit_zero_speed_runs']} checks",
                )
            )
        if transfer.get("connection_status") not in {"connected", None}:
            issues.append(Issue("qbittorrent:connection", "warning", "qBittorrent reports a degraded tracker connection state"))

    sab = snapshot.get("sabnzbd", {})
    if sab.get("configured") and sab.get("up"):
        queue_size = int(sab.get("queue_size", 0) or 0)
        speed = float(sab.get("download_speed_kbps", 0.0) or 0.0)
        if queue_size > 0 and speed == 0:
            counters["sab_zero_speed_runs"] += 1
        else:
            counters["sab_zero_speed_runs"] = 0
        if sab.get("paused"):
            issues.append(Issue("sabnzbd:paused", "warning", "SABnzbd is paused"))
        if int(sab.get("warnings_count", 0)) > 0:
            issues.append(
                Issue(
                    "sabnzbd:warnings",
                    "warning",
                    f"SABnzbd reports {sab.get('warnings_count', 0)} warning entries",
                )
            )
        if counters["sab_zero_speed_runs"] >= ISSUE_STALL_RUNS:
            issues.append(
                Issue(
                    "sabnzbd:stalled",
                    "warning",
                    f"SABnzbd has {queue_size} queued jobs but 0 KB/s for {counters['sab_zero_speed_runs']} checks",
                )
            )

    for series_name, series in snapshot.get("sonarr", {}).get("series", {}).items():
        if not series.get("found"):
            issues.append(Issue(f"series:{series_name}:missing", "warning", f"{series_name} is not present in Sonarr"))
            continue
        if int(series.get("non_english_files", 0)) > 0:
            issues.append(
                Issue(
                    f"series:{series_name}:non-english",
                    "critical",
                    f"{series_name} still has {series.get('non_english_files', 0)} files without English audio",
                )
            )

    return issues, counters


def summarize_changes(snapshot: dict[str, Any], previous: dict[str, Any]) -> list[str]:
    changes: list[str] = []
    previous_services = previous.get("services", {})
    current_services = {
        name: snapshot[name]
        for name in ("sonarr", "radarr", "prowlarr", "plex", "jellyfin", "qbittorrent", "sabnzbd")
        if name in snapshot
    }
    for name, service in current_services.items():
        if not service.get("configured"):
            continue
        prev_up = previous_services.get(name, {}).get("up")
        curr_up = service.get("up")
        if prev_up is None:
            continue
        if prev_up != curr_up:
            state = "recovered" if curr_up else "went down"
            changes.append(f"{name} {state}")

    prev_series = previous.get("series", {})
    for series_name, current in snapshot.get("sonarr", {}).get("series", {}).items():
        prev = prev_series.get(series_name, {})
        if not current.get("found"):
            continue
        if prev and current.get("missing_episodes") != prev.get("missing_episodes"):
            delta = int(prev.get("missing_episodes", 0)) - int(current.get("missing_episodes", 0))
            direction = "down" if delta > 0 else "up"
            changes.append(
                f"{series_name} missing episodes moved {direction} to {current.get('missing_episodes')} ({delta:+d})"
            )
        if prev and current.get("episode_file_count") != prev.get("episode_file_count"):
            delta = int(current.get("episode_file_count", 0)) - int(prev.get("episode_file_count", 0))
            changes.append(f"{series_name} file count changed by {delta:+d} to {current.get('episode_file_count')}")
        if prev and current.get("unknown_audio_files") != prev.get("unknown_audio_files"):
            delta = int(current.get("unknown_audio_files", 0)) - int(prev.get("unknown_audio_files", 0))
            changes.append(f"{series_name} unknown-audio files changed by {delta:+d} to {current.get('unknown_audio_files')}")

    return changes


def should_notify(previous: dict[str, Any], issues: list[Issue], changes: list[str]) -> bool:
    previous_issue_keys = set(previous.get("active_issue_keys", []))
    current_issue_keys = {issue.key for issue in issues}
    if not previous:
        return True
    if current_issue_keys != previous_issue_keys:
        return True
    # Changes (download progress, file-count deltas) ride along in the message
    # when an issue transition fires, but never trigger a notification themselves
    # — otherwise active downloads ping every cycle.
    return False


async def summarize_with_ollama(snapshot: dict[str, Any], issues: list[Issue], changes: list[str]) -> str:
    issue_lines = "\n".join(f"- {issue.severity}: {issue.message}" for issue in issues) or "- none"
    change_lines = "\n".join(f"- {change}" for change in changes) or "- none"
    prompt = (
        "You are summarizing a homelab media-stack watchdog update for one human operator.\n"
        "Write 3 short sentences, plain English, no markdown bullets.\n"
        "Mention the most important problem first, then any meaningful progress, then whether action is needed now.\n\n"
        f"Issues:\n{issue_lines}\n\nChanges since last check:\n{change_lines}\n\n"
        f"Snapshot:\n{json.dumps(snapshot, sort_keys=True)[:5000]}"
    )
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "system": "Keep it concise, operational, and grounded in the supplied facts only.",
                    "prompt": prompt,
                    "stream": False,
                    "keep_alive": -1,
                    "options": {"temperature": 0.1, "num_predict": 180},
                },
            )
            response.raise_for_status()
            return response.json().get("response", "").strip()
    except Exception as exc:
        log.warning("Ollama summary failed: %s", exc)
        if issues:
            return issues[0].message
        if changes:
            return changes[0]
        return "Media stack is stable."


def build_notification(snapshot: dict[str, Any], issues: list[Issue], changes: list[str], summary: str) -> str:
    lines = [":satellite: Media watchdog update", summary]
    if issues:
        lines.append("Issues: " + "; ".join(issue.message for issue in issues[:4]))
    sonarr = snapshot.get("sonarr", {})
    if sonarr.get("up"):
        queue = sonarr.get("queue_status", {})
        lines.append(
            f"Sonarr queue: {queue.get('totalCount', 0)} total, warnings={queue.get('warnings', False)}."
        )
    qbit = snapshot.get("qbittorrent", {})
    if qbit.get("up"):
        transfer = qbit.get("transfer", {})
        lines.append(
            f"qBittorrent: {qbit.get('downloading_count', 0)} downloading, {qbit.get('metadata_count', 0)} metadata, {transfer.get('dl_info_speed', 0)} B/s."
        )
    sab = snapshot.get("sabnzbd", {})
    if sab.get("up"):
        lines.append(
            f"SABnzbd: queue={sab.get('queue_size', 0)}, paused={sab.get('paused', False)}, warnings={sab.get('warnings_count', 0)}."
        )
    for series_name, series in sonarr.get("series", {}).items():
        if series.get("found"):
            lines.append(
                f"{series_name}: files={series.get('episode_file_count', 0)}, missing={series.get('missing_episodes', 0)}, non-English={series.get('non_english_files', 0)}, unknown-audio={series.get('unknown_audio_files', 0)}."
            )
    if changes:
        lines.append("Changes: " + "; ".join(changes[:5]))
    return "\n".join(lines)


async def main() -> int:
    previous = load_state()
    sonarr, radarr, prowlarr, sab, qbit, plex, jellyfin = await asyncio.gather(
        collect_arr_health("sonarr", SONARR_URL, SONARR_API_KEY, "v3", SERIES_SPECS),
        collect_arr_health("radarr", RADARR_URL, RADARR_API_KEY, "v3"),
        collect_prowlarr_health(),
        collect_sab_health(),
        collect_qbit_health(),
        collect_plex_health(),
        collect_jellyfin_health(),
    )

    snapshot = {
        "checked_at": iso_now(),
        "sonarr": sonarr,
        "radarr": radarr,
        "prowlarr": prowlarr,
        "sabnzbd": sab,
        "qbittorrent": qbit,
        "plex": plex,
        "jellyfin": jellyfin,
    }
    issues, counters = build_issues(snapshot, previous)
    changes = summarize_changes(snapshot, previous)

    if should_notify(previous, issues, changes):
        summary = await summarize_with_ollama(snapshot, issues, changes)
        message = build_notification(snapshot, issues, changes, summary)
        await notify(message)
        log.info("Notification sent")
    else:
        log.info("No meaningful media-stack change detected")

    state = {
        "last_checked_at": snapshot["checked_at"],
        "active_issue_keys": sorted(issue.key for issue in issues),
        "counters": counters,
        "services": {
            name: {"up": snapshot[name].get("up"), "configured": snapshot[name].get("configured")}
            for name in ("sonarr", "radarr", "prowlarr", "plex", "jellyfin", "qbittorrent", "sabnzbd")
        },
        "sonarr_recent_failed_command_ids": snapshot["sonarr"].get("recent_failed_command_ids", []),
        "radarr_recent_failed_command_ids": snapshot["radarr"].get("recent_failed_command_ids", []),
        "series": snapshot["sonarr"].get("series", {}),
    }
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
