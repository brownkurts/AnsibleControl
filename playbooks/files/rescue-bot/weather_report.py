#!/usr/bin/env python3
"""Post the daily Washington, OK weather report to Rocket.Chat."""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


WTTR_URL = "https://wttr.in/{location}?format=j1"
DEFAULT_CHANNEL = "#homelab"
DEFAULT_LOCATION = "Washington,OK"
DEFAULT_LOCATION_LABEL = "Washington, OK"
REPORT_TZ = "America/Chicago"


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def fetch_weather(location: str) -> dict:
    url = WTTR_URL.format(location=urllib.parse.quote(location, safe=""))
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "kbtech-weather-reporter/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def post_message(rc_url: str, token: str, user_id: str, channel: str, text: str) -> None:
    payload = json.dumps({"channel": channel, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        f"{rc_url.rstrip('/')}/api/v1/chat.postMessage",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Auth-Token": token,
            "X-User-Id": user_id,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = json.load(response)
    if not body.get("success"):
        raise RuntimeError(f"Rocket.Chat post failed: {body}")


def pick_icon(description: str) -> str:
    desc = description.lower()
    if any(word in desc for word in ("rain", "storm", "thunder")):
        return ":thunder_cloud_and_rain:"
    if any(word in desc for word in ("cloud", "overcast")):
        return ":cloud:"
    if any(word in desc for word in ("snow", "blizzard")):
        return ":snowflake:"
    if any(word in desc for word in ("fog", "mist")):
        return ":fog:"
    return ":sunny:"


def max_chance(day: dict, key: str) -> int:
    values = []
    for hour in day.get("hourly", []):
        raw = hour.get(key)
        try:
            values.append(int(raw))
        except (TypeError, ValueError):
            continue
    return max(values, default=0)


def midday_description(day: dict) -> str:
    hourly = day.get("hourly", [])
    if not hourly:
        return "Unavailable"
    index = min(4, len(hourly) - 1)
    desc = hourly[index].get("weatherDesc", [])
    if desc and isinstance(desc[0], dict):
        return desc[0].get("value", "Unavailable")
    return "Unavailable"


def build_report(payload: dict, location_label: str) -> str:
    current = payload["current_condition"][0]
    days = payload["weather"][:3]
    if len(days) < 3:
        raise RuntimeError("wttr.in did not return a 3-day forecast")

    tz = ZoneInfo(REPORT_TZ)
    now = datetime.now(tz)
    day_names = [(now + timedelta(days=offset)).strftime("%a") for offset in range(3)]
    date_label = now.strftime("%A, %B %d").replace(" 0", " ")

    description = current["weatherDesc"][0]["value"]
    icon = pick_icon(description)

    alerts = []
    thunder = max_chance(days[0], "chanceofthunder")
    rain = max_chance(days[0], "chanceofrain")
    snow = max_chance(days[0], "chanceofsnow")
    if thunder >= 30:
        alerts.append(f":zap: Thunder chance: {thunder}%")
    if rain >= 50:
        alerts.append(f":umbrella: Rain chance: {rain}%")
    if snow >= 30:
        alerts.append(f":snowflake: Snow chance: {snow}%")

    lines = [
        f"{icon} *Good morning! {date_label} - {location_label}*",
        "",
        (
            f"*Now:* {description} {current['temp_F']}F "
            f"(feels {current['FeelsLikeF']}F) | {current['humidity']}% humidity | "
            f"{current['winddir16Point']} {current['windspeedMiles']} mph"
        ),
        (
            f"*Today:* High *{days[0]['maxtempF']}F* / Low *{days[0]['mintempF']}F* | "
            f"Sunrise {days[0]['astronomy'][0]['sunrise']} | "
            f"Sunset {days[0]['astronomy'][0]['sunset']}"
        ),
    ]
    if alerts:
        lines.append("  ".join(alerts))
    lines.extend(
        [
            "*3-Day Outlook:*",
            f"{day_names[0]}: {midday_description(days[0])} - Hi {days[0]['maxtempF']}F / Lo {days[0]['mintempF']}F",
            f"{day_names[1]}: {midday_description(days[1])} - Hi {days[1]['maxtempF']}F / Lo {days[1]['mintempF']}F",
            f"{day_names[2]}: {midday_description(days[2])} - Hi {days[2]['maxtempF']}F / Lo {days[2]['mintempF']}F",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    try:
        rc_url = required_env("RC_URL")
        token = required_env("RC_BOT_TOKEN")
        user_id = required_env("RC_BOT_USER_ID")
        channel = os.environ.get("WEATHER_CHANNEL", DEFAULT_CHANNEL).strip() or DEFAULT_CHANNEL
        location = os.environ.get("WEATHER_LOCATION", DEFAULT_LOCATION).strip() or DEFAULT_LOCATION
        location_label = (
            os.environ.get("WEATHER_LOCATION_LABEL", DEFAULT_LOCATION_LABEL).strip()
            or DEFAULT_LOCATION_LABEL
        )
        report = build_report(fetch_weather(location), location_label)
        post_message(rc_url, token, user_id, channel, report)
    except Exception as exc:
        print(f"weather report failed: {exc}", file=sys.stderr)
        return 1
    print("weather report posted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
