"""Local settings helpers for Jieli Codex Sync."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


DEFAULT_BASE_URL = "https://jieli.app"
SETTINGS_FILE_NAME = "settings.json"


def settings_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".jieli" / SETTINGS_FILE_NAME


def load_settings(home: Path | None = None) -> dict[str, Any]:
    try:
        value = json.loads(settings_path(home).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def get_api_key(environ: Mapping[str, str] | None = None, home: Path | None = None) -> str:
    env = os.environ if environ is None else environ
    value = env.get("JIELI_API_KEY", "").strip()
    if value:
        return value
    settings = load_settings(home)
    for key in ("api_key", "JIELI_API_KEY"):
        setting = settings.get(key)
        if isinstance(setting, str) and setting.strip():
            return setting.strip()
    return ""


def get_base_url(environ: Mapping[str, str] | None = None, home: Path | None = None) -> str:
    env = os.environ if environ is None else environ
    value = env.get("JIELI_BASE_URL", "").strip()
    if value:
        return value.rstrip("/")
    settings = load_settings(home)
    for key in ("base_url", "JIELI_BASE_URL"):
        setting = settings.get(key)
        if isinstance(setting, str) and setting.strip():
            return setting.strip().rstrip("/")
    return DEFAULT_BASE_URL
