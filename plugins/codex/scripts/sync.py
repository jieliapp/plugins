#!/usr/bin/env python3
"""Sync a Codex session transcript to Jieli."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import jieli_config
from redact import redact_json, redact_text


PROVIDER = "codex"
DEFAULT_BASE_URL = jieli_config.DEFAULT_BASE_URL
LOCK_TTL_SECONDS = 60
TRANSCRIPT_FLUSH_TRIGGERS = {"stop", "precompact", "postcompact"}
TRANSCRIPT_QUIET_SECONDS = 0.25
TRANSCRIPT_FLUSH_TIMEOUT_SECONDS = 1.5
TOOL_OUTPUT_MAX_CHARS = 20000
SESSION_MAPPING_FILE = "codex-sessions.json"
COMPACTION_PLACEHOLDER = (
    "[Context compacted - earlier conversation summarized to continue past the context window]"
)


def missing_config_vars(environ: Mapping[str, str] | None = None) -> list[str]:
    return [] if jieli_config.get_api_key(environ) else ["JIELI_API_KEY or ~/.jieli/settings.json api_key"]


def build_missing_config_hook_response(trigger: str, missing: list[str]) -> dict[str, Any]:
    if trigger.lower() != "userpromptsubmit" or not missing:
        return {}
    missing_text = ", ".join(missing)
    return {
        "continue": True,
        "systemMessage": (
            "Jieli Codex Sync is not configured. "
            f"Missing: {missing_text}. "
            f"Go to {DEFAULT_BASE_URL}, register or sign in, create an API key. "
            "Then either set JIELI_API_KEY before starting Codex, or ask the agent to write "
            "`~/.jieli/settings.json` with `{\"api_key\":\"<key>\",\"base_url\":\"https://jieli.app\"}` "
            "and chmod it to 600. "
            "Sync will stay disabled until configured."
        ),
    }


def load_hook_stdin() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


def build_payload_from_hook(hook_data: dict[str, Any], base_url: str | None = None) -> dict[str, Any]:
    transcript_path = resolve_transcript_path(hook_data)
    transcript = parse_transcript(transcript_path, fallback_session_id=str(hook_data.get("session_id") or ""))
    cwd = transcript.get("cwd") or hook_data.get("cwd") or os.getcwd()
    session_id = str(hook_data.get("session_id") or transcript.get("id") or "").strip()
    if not session_id:
        raise ValueError("session_id is required")

    base = (base_url or jieli_config.get_base_url()).rstrip("/")
    provider_thread_id = jieli_thread_id(session_id)
    messages = transcript["messages"]
    thread_payload = {
        "id": provider_thread_id,
        "title": transcript.get("title") or title_from_messages(messages),
        "model": transcript.get("model", ""),
        "cwd": cwd,
        "created_ms": transcript.get("created_ms", 0),
        "updated_ms": transcript.get("updated_ms", 0),
        "messages": messages,
    }
    return {
        "provider": PROVIDER,
        "repo": repo_from_cwd(cwd),
        "branch": transcript.get("branch") or git_branch(cwd),
        "source_url": f"{base}/threads/{provider_thread_id}",
        "labels": ["codex"],
        "thread": thread_payload,
    }


def resolve_transcript_path(hook_data: dict[str, Any]) -> Path:
    for key in ("transcript_path", "session_path"):
        raw = hook_data.get(key)
        if isinstance(raw, str) and raw:
            path = Path(raw).expanduser()
            if path.exists():
                return path
            raise ValueError(f"{key} does not exist: {path}")
    session_id = str(hook_data.get("session_id") or "").strip()
    path = find_session_transcript(session_id)
    if path is None:
        raise ValueError("transcript_path is required")
    return path


def find_session_transcript(session_id: str, home: Path | None = None) -> Path | None:
    candidates: list[Path] = []
    fallback_candidates: list[Path] = []
    for root in codex_session_roots(home):
        if not root.exists():
            continue
        if session_id:
            candidates.extend(root.rglob(f"*{session_id}*.jsonl"))
            fallback_candidates.extend(root.rglob("rollout-*.jsonl"))
        else:
            candidates.extend(root.rglob("rollout-*.jsonl"))
    existing = unique_existing_paths(candidates)
    if session_id and not existing:
        existing = unique_existing_paths(fallback_candidates)
    if not existing:
        return None
    if session_id:
        exact = [path for path in existing if session_id in path.name]
        if exact:
            return max(exact, key=lambda path: path.stat().st_mtime_ns)
        for path in sorted(existing, key=lambda item: item.stat().st_mtime_ns, reverse=True):
            if transcript_has_session_id(path, session_id):
                return path
    return max(existing, key=lambda path: path.stat().st_mtime_ns)


def unique_existing_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    existing: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen or not path.is_file():
            continue
        existing.append(path)
        seen.add(key)
    return existing


def codex_session_roots(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    roots: list[Path] = []
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        roots.append(Path(codex_home).expanduser() / "sessions")
    roots.extend(
        [
            home / "Library" / "Application Support" / "Codex" / "sessions",
            home / ".codex" / "sessions",
        ]
    )
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = str(root)
        if key not in seen:
            unique.append(root)
            seen.add(key)
    return unique


def transcript_has_session_id(path: Path, session_id: str) -> bool:
    if not session_id:
        return False
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = entry.get("payload")
                if isinstance(payload, dict) and payload.get("id") == session_id:
                    return True
    except OSError:
        return False
    return False


def parse_transcript(path: Path, fallback_session_id: str = "") -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    session_id = fallback_session_id
    cwd = ""
    branch = ""
    model = ""
    created_ms = 0
    updated_ms = 0
    title = ""

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                continue
            stamp_ms = timestamp_ms(entry.get("timestamp") or payload.get("timestamp"))
            if stamp_ms:
                created_ms = created_ms or stamp_ms
                updated_ms = stamp_ms

            entry_type = entry.get("type")
            if entry_type == "session_meta":
                session_id = session_id or str(payload.get("id") or "")
                cwd = cwd or str(payload.get("cwd") or "")
                git = payload.get("git")
                if isinstance(git, dict):
                    branch = branch or str(git.get("branch") or "")
                continue
            if entry_type == "turn_context":
                cwd = cwd or str(payload.get("cwd") or "")
                model = model or str(payload.get("model") or "")
                continue
            if entry_type != "response_item":
                continue
            item = message_from_response_item(payload, line_number)
            if item is None:
                continue
            if item.get("role") == "user" and not title:
                title = text_from_content(item.get("content")).strip()[:80]
            messages.append(item)

    return {
        "id": session_id,
        "cwd": cwd,
        "branch": branch,
        "model": model,
        "title": title,
        "created_ms": created_ms,
        "updated_ms": updated_ms or created_ms,
        "messages": messages,
    }


def message_from_response_item(payload: dict[str, Any], line_number: int) -> dict[str, Any] | None:
    item_type = payload.get("type")
    if item_type == "reasoning":
        return None
    if item_type == "message":
        return normalize_response_message(payload, line_number)
    if item_type == "function_call":
        return normalize_function_call(payload, line_number)
    if item_type == "function_call_output":
        return normalize_function_output(payload, line_number)
    return None


def normalize_response_message(payload: dict[str, Any], line_number: int) -> dict[str, Any] | None:
    role = str(payload.get("role") or "")
    if role in {"system", "developer"}:
        return None
    content = normalize_content_blocks(payload.get("content"), role)
    if content is None:
        return None
    if role == "user" and should_skip_user_message(content):
        return None
    item: dict[str, Any] = {
        "role": role or "assistant",
        "content": content,
        "message_id": str(payload.get("id") or f"line-{line_number}"),
    }
    phase = payload.get("phase")
    if isinstance(phase, str) and phase:
        item["phase"] = phase
    return item


def normalize_content_blocks(raw_content: Any, role: str) -> Any | None:
    if isinstance(raw_content, str):
        text = redact_text(raw_content).strip()
        return text or None
    if not isinstance(raw_content, list):
        if raw_content is None:
            return None
        return redact_json(raw_content)

    blocks: list[dict[str, str]] = []
    for block in raw_content:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        text = block.get("text")
        if block_type in {"input_text", "output_text", "text"} and isinstance(text, str):
            value = redact_text(text).strip()
            if value:
                blocks.append({"type": "text", "text": value})
            continue
        if block_type in {"encrypted_content", "reasoning"}:
            continue
        redacted = redact_json(block)
        if isinstance(redacted, dict):
            blocks.append(redacted)
    if not blocks:
        return None
    if all(block.get("type") == "text" for block in blocks):
        return "\n\n".join(block["text"] for block in blocks if block.get("text"))
    return blocks


def should_skip_user_message(content: Any) -> bool:
    text = text_from_content(content).lstrip()
    if not text:
        return True
    skipped_prefixes = (
        "<codex_internal_context",
        "Base directory for this skill:",
    )
    return any(text.startswith(prefix) for prefix in skipped_prefixes)


def normalize_function_call(payload: dict[str, Any], line_number: int) -> dict[str, Any] | None:
    call_id = str(payload.get("call_id") or payload.get("id") or f"call-{line_number}")
    name = str(payload.get("name") or "")
    if not name:
        return None
    return {
        "role": "assistant",
        "message_id": call_id,
        "content": [
            {
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": parse_tool_arguments(payload.get("arguments")),
            }
        ],
    }


def normalize_function_output(payload: dict[str, Any], line_number: int) -> dict[str, Any] | None:
    call_id = str(payload.get("call_id") or payload.get("id") or f"output-{line_number}")
    output = payload.get("output")
    if output is None:
        return None
    if isinstance(output, str):
        content = truncate_tool_output(redact_text(output))
    else:
        content = redact_json(output)
    return {
        "role": "tool",
        "message_id": call_id,
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": content,
            }
        ],
    }


def parse_tool_arguments(value: Any) -> Any:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return redact_text(value)
        return redact_json(parsed)
    return redact_json(value)


def truncate_tool_output(text: str, max_chars: int = TOOL_OUTPUT_MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    original_chars = len(text)
    original_lines = len(text.splitlines())
    return (
        text[:max_chars]
        + f"\n\n[Tool output truncated at {max_chars} chars; "
        + f"original_chars={original_chars}; original_lines={original_lines}.]"
    )


def text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, dict) and isinstance(block.get("content"), str):
                parts.append(block["content"])
        return "\n\n".join(parts)
    return ""


def title_from_messages(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") != "user":
            continue
        text = text_from_content(message.get("content")).strip()
        if text and text != COMPACTION_PLACEHOLDER:
            return text[:80]
    return "Codex session"


def jieli_thread_id(session_id: str) -> str:
    value = session_id.strip()
    if not value:
        return value
    return value if value.startswith("T-") else f"T-{value}"


def codex_session_id(provider_thread_id: str) -> str:
    value = provider_thread_id.strip()
    return value[2:] if value.startswith("T-") else value


def repo_from_cwd(cwd: str) -> str:
    parts = [part for part in Path(cwd).parts if part not in {"/", ""}]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[0] if parts else ""


def timestamp_ms(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value:
        return 0
    try:
        normalized = value.replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalized).timestamp() * 1000)
    except ValueError:
        return 0


def git_branch(cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd or None,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


class SyncLock:
    def __init__(self, home: Path | None = None, session_id: str = ""):
        self.home = home or Path.home()
        safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "")
        self.path = self.home / ".jieli" / (f"codex-sync-{safe}.lock" if safe else "codex-sync.lock")
        self.acquired = False

    def __enter__(self) -> "SyncLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and time.time() - self.path.stat().st_mtime > LOCK_TTL_SECONDS:
            self.path.unlink(missing_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return self
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "timestamp": time.time()}))
        self.acquired = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)


def upload_payload(payload: dict[str, Any], base_url: str, api_key: str) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/plugin/threads/upload",
        data=data,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def format_hook_error(error: BaseException) -> str:
    message = f"{type(error).__name__}: {error}"
    if isinstance(error, urllib.error.HTTPError):
        try:
            body = error.read(4096).decode("utf-8", errors="replace").strip()
        except OSError:
            body = ""
        if body:
            message += f"; body={redact_text(body)}"
    return message


def session_mapping_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".jieli" / SESSION_MAPPING_FILE


def write_session_mapping(
    session_id: str,
    base_url: str,
    home: Path | None = None,
    provider_thread_id: str | None = None,
    session_path: str | None = None,
) -> None:
    path = session_mapping_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                mapping = raw
        except json.JSONDecodeError:
            mapping = {}
    mapping[session_id] = {
        "provider_thread_id": provider_thread_id or jieli_thread_id(session_id),
        "base_url": base_url.rstrip("/"),
        "session_path": session_path or "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)
    path.chmod(0o600)


def log_hook_error(message: str, home: Path | None = None) -> None:
    home = home or Path.home()
    path = home / ".jieli" / "hooks.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {message}\n")


def wait_for_transcript_flush(
    path: Path,
    quiet_seconds: float = TRANSCRIPT_QUIET_SECONDS,
    timeout_seconds: float = TRANSCRIPT_FLUSH_TIMEOUT_SECONDS,
) -> None:
    previous = transcript_signature(path)
    if previous is None:
        return
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        time.sleep(quiet_seconds)
        current = transcript_signature(path)
        if current is None:
            return
        if current == previous:
            return
        previous = current


def transcript_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


def required_env(*names: str) -> str:
    if names and names[0] == "JIELI_API_KEY":
        value = jieli_config.get_api_key()
        if value:
            return value
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    raise KeyError(names[0])


def optional_env(*names: str) -> str:
    if names and names[0] == "JIELI_BASE_URL":
        return jieli_config.get_base_url()
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trigger", default="")
    parser.add_argument("--jieli-hook", action="store_true")
    args = parser.parse_args()
    try:
        missing = missing_config_vars()
        if missing:
            response = build_missing_config_hook_response(args.trigger, missing)
            if response:
                print(json.dumps(response))
            raise KeyError(", ".join(missing))
        hook_data = load_hook_stdin()
        session_id = str(hook_data.get("session_id") or "")
        with SyncLock(session_id=session_id) as lock:
            if not lock.acquired:
                return 0
            transcript_path = hook_data.get("transcript_path")
            if isinstance(transcript_path, str) and transcript_path and not Path(transcript_path).exists():
                return 0
            if args.trigger.lower() in TRANSCRIPT_FLUSH_TRIGGERS and isinstance(transcript_path, str) and transcript_path:
                wait_for_transcript_flush(Path(transcript_path))
            base_url = jieli_config.get_base_url().rstrip("/")
            api_key = required_env("JIELI_API_KEY")
            payload = build_payload_from_hook(hook_data, base_url=base_url)
            upload_payload(payload, base_url, api_key)
            provider_thread_id = payload["thread"]["id"]
            write_session_mapping(
                codex_session_id(provider_thread_id),
                base_url,
                provider_thread_id=provider_thread_id,
                session_path=transcript_path if isinstance(transcript_path, str) else "",
            )
    except (KeyError, ValueError, OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        log_hook_error(f"sync {args.trigger}: {format_hook_error(error)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
