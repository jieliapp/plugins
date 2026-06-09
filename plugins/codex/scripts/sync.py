#!/usr/bin/env python3
"""Sync a Codex session transcript to Jieli."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

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
ATTACHMENT_CACHE_FILE = "codex-attachments.json"
IMAGE_PLACEHOLDER_RE = re.compile(r"\[Image:\s*source:\s*([^\]]+)\]")
IMAGE_LABEL_RE = re.compile(r"\[Image\s+#\d+\]")
LOCAL_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((/[^)\n]+)\)")
HANDOFF_SUMMARY_RE = re.compile(r"^(?:#{1,6}\s*)?\*{0,2}Handoff Summary\*{0,2}\s*(?:\n|$)", re.IGNORECASE)
CODEX_GIT_DIRECTIVE_RE = re.compile(r"(?m)^[ \t]*::git-[A-Za-z0-9_-]+\{[^\n]*\}[ \t]*\n?")
SUPPORTED_IMAGE_MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
COMPACTION_PLACEHOLDER = (
    "[Context compacted - earlier conversation summarized to continue past the context window]"
)

ImageUploader = Callable[[Path], str]
DataImageUploader = Callable[[bytes, str], str]


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


def build_payload_from_hook(
    hook_data: dict[str, Any],
    base_url: str | None = None,
    image_uploader: ImageUploader | None = None,
    data_image_uploader: DataImageUploader | None = None,
) -> dict[str, Any]:
    transcript_path = resolve_transcript_path(hook_data)
    transcript = parse_transcript(
        transcript_path,
        fallback_session_id=str(hook_data.get("session_id") or ""),
        image_uploader=image_uploader,
        data_image_uploader=data_image_uploader,
    )
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
        "repo_url": repo_url_from_cwd(cwd),
        "branch": transcript.get("branch") or git_branch(cwd),
        "source_url": f"{base}/threads/{provider_thread_id}",
        "labels": [],
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


def parse_transcript(
    path: Path,
    fallback_session_id: str = "",
    image_uploader: ImageUploader | None = None,
    data_image_uploader: DataImageUploader | None = None,
) -> dict[str, Any]:
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
            if entry_type == "event_msg":
                item = message_from_user_event(payload, line_number, image_uploader)
                if item is not None and not is_duplicate_user_event(messages, item):
                    if not title:
                        title = text_from_content(item.get("content")).strip()[:80]
                    messages.append(item)
                continue
            if entry_type != "response_item":
                continue
            item = message_from_response_item(payload, line_number, image_uploader, data_image_uploader)
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


def message_from_response_item(
    payload: dict[str, Any],
    line_number: int,
    image_uploader: ImageUploader | None = None,
    data_image_uploader: DataImageUploader | None = None,
) -> dict[str, Any] | None:
    item_type = payload.get("type")
    if item_type == "reasoning":
        return None
    if item_type == "message":
        return normalize_response_message(payload, line_number, image_uploader, data_image_uploader)
    if item_type == "function_call":
        return normalize_function_call(payload, line_number)
    if item_type == "custom_tool_call":
        return normalize_custom_tool_call(payload, line_number)
    if item_type == "function_call_output":
        return normalize_function_output(payload, line_number)
    if item_type == "custom_tool_call_output":
        return normalize_function_output(payload, line_number)
    return None


def message_from_user_event(
    payload: dict[str, Any],
    line_number: int,
    image_uploader: ImageUploader | None = None,
) -> dict[str, Any] | None:
    if payload.get("type") != "user_message":
        return None
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        return None
    content = normalize_user_event_content(message, payload.get("local_images"), image_uploader)
    if content is None:
        return None
    return {"role": "user", "content": content, "message_id": f"user-event-{line_number}"}


def normalize_response_message(
    payload: dict[str, Any],
    line_number: int,
    image_uploader: ImageUploader | None = None,
    data_image_uploader: DataImageUploader | None = None,
) -> dict[str, Any] | None:
    role = str(payload.get("role") or "")
    if role in {"system", "developer"}:
        return None
    content = normalize_content_blocks(payload.get("content"), role, image_uploader, data_image_uploader)
    if content is None:
        return None
    if is_handoff_summary_text(text_from_content(content)):
        content = COMPACTION_PLACEHOLDER
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


def normalize_content_blocks(
    raw_content: Any,
    role: str,
    image_uploader: ImageUploader | None = None,
    data_image_uploader: DataImageUploader | None = None,
) -> Any | None:
    if isinstance(raw_content, str):
        return normalize_text_with_images(raw_content, image_uploader)
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
            append_blocks(blocks, normalize_text_blocks(text, image_uploader))
            continue
        if block_type == "input_image":
            image_block = image_block_from_data_url(str(block.get("image_url") or ""), data_image_uploader)
            if image_block:
                blocks.append(image_block)
            elif not has_existing_image_label(blocks):
                blocks.append({"type": "text", "text": "[Image unavailable]"})
            continue
        if block_type == "image":
            image_block = image_block_from_path(image_path_from_block(block), image_uploader)
            if image_block:
                blocks.append(image_block)
            elif not has_existing_image_label(blocks):
                blocks.append({"type": "text", "text": "[Image unavailable]"})
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


def normalize_user_event_content(message: str, local_images: Any, image_uploader: ImageUploader | None = None) -> Any | None:
    blocks = normalize_text_blocks(message, image_uploader)
    if isinstance(local_images, list):
        for image_path in local_images:
            if not isinstance(image_path, str):
                continue
            image_block = image_block_from_path(image_path, image_uploader)
            if image_block:
                blocks.append(image_block)
            elif not has_existing_image_label(blocks):
                blocks.append({"type": "text", "text": "[Image unavailable]"})
    return collapse_text_only_blocks(blocks)


def normalize_text_with_images(text: str, image_uploader: ImageUploader | None = None) -> Any | None:
    return collapse_text_only_blocks(normalize_text_blocks(text, image_uploader))


def normalize_text_blocks(text: str, image_uploader: ImageUploader | None = None) -> list[dict[str, Any]]:
    stripped = text.strip()
    if stripped.startswith("<image name=") or stripped == "</image>":
        return []
    blocks: list[dict[str, Any]] = []
    position = 0
    for match in IMAGE_PLACEHOLDER_RE.finditer(text):
        append_text_block(blocks, text[position:match.start()])
        image_block = image_block_from_path(match.group(1), image_uploader)
        if image_block:
            blocks.append(image_block)
        elif not has_existing_image_label(blocks):
            blocks.append({"type": "text", "text": "[Image unavailable]"})
        position = match.end()
    append_text_block(blocks, text[position:])
    return blocks


def append_text_block(blocks: list[dict[str, Any]], text: str) -> None:
    value = clean_codex_text(redact_text(file_url_local_markdown_link_targets(text))).strip()
    if value:
        blocks.append({"type": "text", "text": value})


def append_blocks(blocks: list[Any], next_blocks: list[dict[str, Any]]) -> None:
    blocks.extend(next_blocks)


def has_existing_image_label(blocks: list[Any]) -> bool:
    for block in reversed(blocks):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and IMAGE_LABEL_RE.search(text):
            return True
    return False


def collapse_text_only_blocks(blocks: list[Any]) -> Any | None:
    if not blocks:
        return None
    if all(isinstance(block, dict) and block.get("type") == "text" for block in blocks):
        return "\n\n".join(str(block.get("text", "")) for block in blocks if block.get("text"))
    return blocks


def image_path_from_block(block: dict[str, Any]) -> str:
    source = block.get("source")
    if isinstance(source, str):
        return source
    if isinstance(source, dict):
        for key in ("path", "file_path", "sourcePath", "url"):
            value = source.get(key)
            if isinstance(value, str) and value.startswith("/"):
                return value
    for key in ("sourcePath", "path", "file_path"):
        value = block.get(key)
        if isinstance(value, str):
            return value
    return ""


def image_block_from_path(raw_path: str, image_uploader: ImageUploader | None = None) -> dict[str, Any] | None:
    if not raw_path or image_uploader is None:
        return None
    path = Path(raw_path.strip())
    media_type = media_type_for_image(path)
    if not media_type:
        return None
    try:
        url = image_uploader(path)
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if not url:
        return None
    return {"type": "image", "source": {"url": url, "type": media_type}}


def image_block_from_data_url(raw_url: str, data_image_uploader: DataImageUploader | None = None) -> dict[str, Any] | None:
    if not raw_url.startswith("data:") or data_image_uploader is None:
        return None
    parsed = parse_image_data_url(raw_url)
    if parsed is None:
        return None
    media_type, data = parsed
    try:
        url = data_image_uploader(data, media_type)
    except (OSError, ValueError, urllib.error.URLError, KeyError):
        return None
    if not url:
        return None
    return {"type": "image", "source": {"url": url, "type": media_type}}


def parse_image_data_url(raw_url: str) -> tuple[str, bytes] | None:
    header, separator, encoded = raw_url.partition(",")
    if not separator or ";base64" not in header:
        return None
    media_type = header.removeprefix("data:").split(";", 1)[0].lower()
    if media_type not in SUPPORTED_IMAGE_MEDIA_TYPES:
        return None
    try:
        return media_type, base64.b64decode(encoded, validate=True)
    except ValueError:
        return None


def media_type_for_image(path: Path) -> str:
    media_type, _ = mimetypes.guess_type(path.name)
    return media_type if media_type in SUPPORTED_IMAGE_MEDIA_TYPES else ""


def is_duplicate_user_event(messages: list[dict[str, Any]], item: dict[str, Any]) -> bool:
    if item.get("role") != "user":
        return False
    current = normalized_content_text(item.get("content"))
    if not current:
        return False
    for previous in reversed(messages[-2:]):
        if previous.get("role") != "user":
            continue
        previous_text = normalized_content_text(previous.get("content"))
        if previous_text == current or previous_text.endswith(current) or current.endswith(previous_text):
            return True
    return False


def normalized_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"].strip())
        return "\n\n".join(part for part in parts if part).strip()
    return ""


def should_skip_user_message(content: Any) -> bool:
    text = text_from_content(content).lstrip()
    if not text:
        return True
    skipped_prefixes = (
        "<codex_internal_context",
        "<skill>",
        "Base directory for this skill:",
        "# AGENTS.md instructions for ",
    )
    return any(text.startswith(prefix) for prefix in skipped_prefixes)


def is_handoff_summary_text(text: str) -> bool:
    return bool(HANDOFF_SUMMARY_RE.match(text.lstrip()))


def clean_codex_text(text: str) -> str:
    cleaned = CODEX_GIT_DIRECTIVE_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def file_url_local_markdown_link_targets(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        label = match.group(1).strip()
        target = match.group(2).strip()
        if target.startswith("/Users/") or target.startswith("/var/") or target.startswith("/private/") or target.startswith("/tmp/"):
            return f"[{label}]({Path(target).as_uri()})"
        return match.group(0)

    return LOCAL_MARKDOWN_LINK_RE.sub(replace, text)


def normalize_function_call(payload: dict[str, Any], line_number: int) -> dict[str, Any] | None:
    call_id = str(payload.get("call_id") or payload.get("id") or f"call-{line_number}")
    name = str(payload.get("name") or "")
    if not name:
        return None
    input_value = parse_tool_arguments(payload.get("arguments"))
    return {
        "role": "assistant",
        "message_id": call_id,
        "content": [
            {
                "type": "tool_use",
                "id": call_id,
                "name": normalize_tool_name(name),
                "input": normalize_tool_input(name, input_value),
            }
        ],
    }


def normalize_custom_tool_call(payload: dict[str, Any], line_number: int) -> dict[str, Any] | None:
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
                "input": parse_custom_tool_input(name, payload.get("input")),
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
    exit_code = exit_code_from_tool_output(content)
    return {
        "role": "tool",
        "message_id": call_id,
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": content,
                "run": {
                    "status": "completed",
                    "result": {
                        "output": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
                        "exitCode": exit_code,
                    },
                },
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


def parse_custom_tool_input(name: str, value: Any) -> Any:
    if isinstance(value, str):
        text = redact_text(value)
        if name.strip().lower() == "apply_patch":
            return {"patch_text": text}
        return {"input": text}
    return redact_json(value)


def normalize_tool_name(name: str) -> str:
    if name.strip().lower() == "exec_command":
        return "shell_command"
    return name


def normalize_tool_input(name: str, input_value: Any) -> Any:
    if name.strip().lower() != "exec_command" or not isinstance(input_value, dict):
        return input_value
    command = input_value.get("cmd")
    cwd = input_value.get("workdir")
    return {
        "command": command if isinstance(command, str) else "",
        "cwd": cwd if isinstance(cwd, str) else "",
    }


def exit_code_from_tool_output(output: Any) -> int | None:
    if not isinstance(output, str):
        return None
    match = re.search(r"(?:Process exited with code|Exit code:)\s*(-?\d+)", output)
    return int(match.group(1)) if match else None


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
    if repo_url_from_cwd(cwd):
        return ""
    parts = [part for part in Path(cwd).parts if part not in {"/", ""}]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[0] if parts else ""


def repo_url_from_cwd(cwd: str) -> str:
    return git_config(cwd, "remote.origin.url")


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
    branch = git_config(cwd, "--abbrev-ref", "HEAD", git_command="rev-parse")
    return branch


def git_config(cwd: str, *args: str, git_command: str = "config") -> str:
    try:
        result = subprocess.run(
            ["git", git_command, *args],
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


def upload_attachment(path: Path, base_url: str, api_key: str) -> str:
    media_type = media_type_for_image(path)
    if not media_type:
        raise ValueError("unsupported image media type")
    return upload_attachment_data(path.read_bytes(), media_type, base_url, api_key)


def upload_attachment_data(data_bytes: bytes, media_type: str, base_url: str, api_key: str) -> str:
    if media_type not in SUPPORTED_IMAGE_MEDIA_TYPES:
        raise ValueError("unsupported image media type")
    encoded = base64.b64encode(data_bytes).decode("ascii")
    data = json.dumps({"data": encoded, "mediaType": media_type}).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/plugin/attachments",
        data=data,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = json.loads(response.read().decode("utf-8"))
    url = body.get("url") if isinstance(body, dict) else ""
    if not isinstance(url, str) or not url:
        raise ValueError("attachment upload response missing url")
    return url


def upload_attachment_cached(path: Path, base_url: str, api_key: str, home: Path | None = None) -> str:
    media_type = media_type_for_image(path)
    if not media_type:
        raise ValueError("unsupported image media type")
    return upload_attachment_data_cached(path.read_bytes(), media_type, base_url, api_key, home=home)


def upload_attachment_data_cached(
    data_bytes: bytes,
    media_type: str,
    base_url: str,
    api_key: str,
    home: Path | None = None,
) -> str:
    if media_type not in SUPPORTED_IMAGE_MEDIA_TYPES:
        raise ValueError("unsupported image media type")
    digest = hashlib.sha256(data_bytes).hexdigest()
    cache_key = "|".join([base_url.rstrip("/"), media_type, digest])
    cache = load_attachment_cache(home)
    cached_url = cache.get(cache_key)
    if isinstance(cached_url, str) and cached_url:
        return cached_url
    url = upload_attachment_data(data_bytes, media_type, base_url, api_key)
    cache[cache_key] = url
    write_attachment_cache(cache, home)
    return url


def attachment_cache_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".jieli" / ATTACHMENT_CACHE_FILE


def load_attachment_cache(home: Path | None = None) -> dict[str, str]:
    path = attachment_cache_path(home)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, str) and value}


def write_attachment_cache(cache: dict[str, str], home: Path | None = None) -> None:
    path = attachment_cache_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(cache, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)
    path.chmod(0o600)


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
            payload = build_payload_from_hook(
                hook_data,
                base_url=base_url,
                image_uploader=lambda path: upload_attachment_cached(path, base_url, api_key),
                data_image_uploader=lambda data, media_type: upload_attachment_data_cached(data, media_type, base_url, api_key),
            )
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
