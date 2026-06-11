#!/usr/bin/env python3
"""PreToolUse hook for adding Jieli thread trailers to Codex git commits."""

from __future__ import annotations

import argparse
import base64
import json
import shlex
import sys
import urllib.error
from pathlib import Path
from typing import Any

import sync


TRAILER_KEY = "Jieli-Thread"
HANDOFF_CONTEXT_ENV = "JIELI_HANDOFF_CONTEXT_B64"
HANDOFF_HELPER_COMMAND = "jieli-handoff-info"
HANDOFF_HELPER_SCRIPT_COMMANDS = {
    "${CLAUDE_PLUGIN_ROOT}/scripts/handoff_info.py",
    "$CLAUDE_PLUGIN_ROOT/scripts/handoff_info.py",
    "${PLUGIN_ROOT}/scripts/handoff_info.py",
    "$PLUGIN_ROOT/scripts/handoff_info.py",
    "${PLUGIN_ROOT:-$CLAUDE_PLUGIN_ROOT}/scripts/handoff_info.py",
}
AMBIGUOUS_TOKENS = ["||", ";", "\n", "$(", "`", "<<", "|"]


def build_hook_response(hook_data: dict[str, Any], home: Path | None = None) -> dict[str, Any]:
    if hook_data.get("tool_name") != "Bash":
        return {}
    command = (hook_data.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str) or not command:
        return {}
    home = home or Path.home()
    session_id = str(hook_data.get("session_id") or "")
    updated = updated_handoff_command(command, hook_data)
    if not updated:
        ensure_session_mapping(hook_data, home)
        updated = updated_commit_command(command, session_id, home)
    if not updated:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {"command": updated},
        }
    }


def updated_handoff_command(command: str, hook_data: dict[str, Any]) -> str:
    helper_command = resolved_handoff_helper_command(command)
    if not helper_command:
        return ""
    context = {
        "session_id": str(hook_data.get("session_id") or ""),
        "transcript_path": str(hook_data.get("transcript_path") or hook_data.get("session_path") or ""),
        "cwd": str(hook_data.get("cwd") or ""),
    }
    encoded = base64.b64encode(json.dumps(context).encode("utf-8")).decode("ascii")
    return f"{HANDOFF_CONTEXT_ENV}={shlex.quote(encoded)} {helper_command}"


def resolved_handoff_helper_command(command: str) -> str:
    if HANDOFF_CONTEXT_ENV in command:
        return ""
    if any(token in command for token in AMBIGUOUS_TOKENS):
        return ""
    try:
        parts = shlex.split(command)
    except ValueError:
        return ""
    if len(parts) == 1 and parts[0] == HANDOFF_HELPER_COMMAND:
        script = Path(__file__).with_name("handoff_info.py")
        return f"python3 {shlex.quote(str(script))}"
    if len(parts) == 2 and parts[0] == "python3" and parts[1] in HANDOFF_HELPER_SCRIPT_COMMANDS:
        return command
    return ""


def ensure_session_mapping(hook_data: dict[str, Any], home: Path) -> None:
    session_id = str(hook_data.get("session_id") or "")
    if not session_id or read_mapping(home).get(session_id):
        return
    try:
        base_url = (sync.optional_env("JIELI_BASE_URL") or sync.DEFAULT_BASE_URL).rstrip("/")
        api_key = sync.required_env("JIELI_API_KEY")
        with sync.SyncLock(home=home, session_id=session_id) as lock:
            if not lock.acquired:
                return
            payload = sync.build_payload_from_hook(hook_data, base_url=base_url)
            sync.upload_payload(payload, base_url, api_key)
            provider_thread_id = payload["thread"]["id"]
            transcript_path = hook_data.get("transcript_path")
            sync.write_session_mapping(
                sync.codex_session_id(provider_thread_id),
                base_url,
                home=home,
                provider_thread_id=provider_thread_id,
                session_path=transcript_path if isinstance(transcript_path, str) else "",
            )
    except (KeyError, ValueError, OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        sync.log_hook_error(f"commit_trailer sync: {type(error).__name__}: {error}", home=home)


def updated_commit_command(command: str, session_id: str, home: Path) -> str:
    if not command or not session_id:
        return ""
    if any(token in command for token in AMBIGUOUS_TOKENS):
        return ""
    mapping = read_mapping(home)
    session = mapping.get(session_id)
    if not isinstance(session, dict):
        return ""
    base_url = str(session.get("base_url", "")).rstrip("/")
    provider_thread_id = normalize_thread_id(str(session.get("provider_thread_id") or session_id))
    if not base_url or not provider_thread_id:
        return ""
    trailer = f"{TRAILER_KEY}: {base_url}/threads/{provider_thread_id}"
    return inject_trailer(command, trailer)


def inject_trailer(command: str, trailer: str) -> str:
    parts = split_top_level_and_chain(command)
    if not parts:
        return ""
    updated_parts: list[str] = []
    updated_count = 0
    for part in parts:
        if part == "&&":
            updated_parts.append(part)
            continue
        updated_part = append_trailer_to_commit_segment(part, trailer)
        if updated_part:
            updated_count += 1
            if updated_count > 1:
                return ""
            updated_parts.append(updated_part)
        else:
            updated_parts.append(part)
    return "".join(updated_parts) if updated_count == 1 else ""


def split_top_level_and_chain(command: str) -> list[str]:
    parts: list[str] = []
    start = 0
    quote = ""
    escaped = False
    i = 0
    while i < len(command):
        char = command[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if char == "\\":
            escaped = True
            i += 1
            continue
        if quote:
            if char == quote:
                quote = ""
            i += 1
            continue
        if char in ("'", '"'):
            quote = char
            i += 1
            continue
        if command.startswith("&&", i):
            parts.append(command[start:i])
            parts.append("&&")
            i += 2
            start = i
            continue
        if char == "&":
            return []
        i += 1
    if quote or escaped:
        return []
    parts.append(command[start:])
    return parts


def append_trailer_to_commit_segment(segment: str, trailer: str) -> str:
    try:
        parts = shlex.split(segment)
    except ValueError:
        return ""
    if len(parts) < 2 or parts[0] != "git" or parts[1] != "commit":
        return ""
    if any(TRAILER_KEY in part for part in parts):
        return ""
    pathspec_index = find_standalone_double_dash(segment)
    if pathspec_index >= 0:
        prefix = segment[:pathspec_index].rstrip()
        suffix = segment[pathspec_index:].lstrip()
        return f'{prefix} --trailer "{trailer}" {suffix}'
    return f'{segment} --trailer "{trailer}"'


def find_standalone_double_dash(command: str) -> int:
    quote = ""
    escaped = False
    i = 0
    while i < len(command):
        char = command[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if char == "\\":
            escaped = True
            i += 1
            continue
        if quote:
            if char == quote:
                quote = ""
            i += 1
            continue
        if char in ("'", '"'):
            quote = char
            i += 1
            continue
        if command.startswith("--", i):
            before = i == 0 or command[i - 1].isspace()
            after_index = i + 2
            after = after_index == len(command) or command[after_index].isspace()
            if before and after:
                return i
            i += 2
            continue
        i += 1
    return -1


def normalize_thread_id(thread_id: str) -> str:
    value = thread_id.strip()
    if not value:
        return ""
    return value if value.startswith("T-") else f"T-{value}"


def read_mapping(home: Path) -> dict[str, Any]:
    path = home / ".jieli" / sync.SESSION_MAPPING_FILE
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jieli-hook", action="store_true")
    parser.parse_args()
    raw = sys.stdin.read()
    try:
        hook_data = json.loads(raw) if raw.strip() else {}
        response = build_hook_response(hook_data if isinstance(hook_data, dict) else {})
    except json.JSONDecodeError:
        response = {}
    if response:
        print(json.dumps(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
