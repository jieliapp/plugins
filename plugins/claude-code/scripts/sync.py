#!/usr/bin/env python3
"""Sync a Claude Code transcript to Jieli."""

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
from pathlib import Path
from typing import Any, Callable, Mapping

from redact import redact_json, redact_text


PROVIDER = "claude_code"
DEFAULT_LABELS = ["claude-code"]
DEFAULT_BASE_URL = "https://jieli.app"
LOCK_TTL_SECONDS = 60
TRANSCRIPT_FLUSH_TRIGGERS = {"stop", "sessionend", "precompact"}
TRANSCRIPT_QUIET_SECONDS = 0.25
TRANSCRIPT_FLUSH_TIMEOUT_SECONDS = 1.5
ATTACHMENT_CACHE_FILE = "claude-attachments.json"
CONFIG_ENV_GROUPS = (
    ("JIELI_API_KEY", ("JIELI_API_KEY", "CLAUDE_PLUGIN_OPTION_API_KEY")),
)
MODEL_ALIAS_ENV_NAMES = (
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
)
IMAGE_PLACEHOLDER_RE = re.compile(r"\[Image:\s*source:\s*([^\]]+)\]")
IMAGE_LABEL_RE = re.compile(r"\[Image\s+#\d+\]")
SUPPORTED_IMAGE_MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

ImageUploader = Callable[[Path], str]


def missing_config_vars(environ: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    return [primary for primary, names in CONFIG_ENV_GROUPS if not any(env.get(name) for name in names)]


def build_missing_config_hook_response(trigger: str, missing: list[str]) -> dict[str, Any]:
    if trigger != "userpromptsubmit" or not missing:
        return {}
    missing_text = ", ".join(missing)
    return {
        "continue": True,
        "systemMessage": (
            "Jieli Claude Code Sync is not configured. "
            f"Missing: {missing_text}. "
            f"Go to {DEFAULT_BASE_URL}, register or sign in, create an API key, then configure the plugin api_key option "
            "or set JIELI_API_KEY in your environment. "
            "You can paste the API key into this chat and ask the agent to configure it for you. "
            "Sync will stay disabled until configured."
        ),
    }


def load_hook_stdin() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def build_payload_from_hook(hook_data: dict[str, Any], base_url: str | None = None, image_uploader: ImageUploader | None = None) -> dict[str, Any]:
    transcript_path = hook_data.get("transcript_path")
    if not transcript_path:
        raise ValueError("transcript_path is required")
    transcript = parse_transcript(Path(transcript_path), fallback_session_id=hook_data.get("session_id"), image_uploader=image_uploader)
    cwd = transcript.get("cwd") or hook_data.get("cwd") or os.getcwd()
    branch = transcript.get("branch") or git_branch(cwd)
    session_id = hook_data.get("session_id") or transcript.get("id")
    if not session_id:
        raise ValueError("session_id is required")
    provider_thread_id = jieli_thread_id(session_id)
    base = (base_url or optional_env("JIELI_BASE_URL", "CLAUDE_PLUGIN_OPTION_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    source_url = f"{base}/threads/{provider_thread_id}" if base else ""
    messages = transcript["messages"]
    title = transcript.get("title") or title_from_messages(messages)
    resolved_model = transcript.get("model", "")
    display_model = display_model_name(resolved_model)
    thread_payload = {
        "id": provider_thread_id,
        "title": title,
        "model": display_model,
        "cwd": cwd,
        "created_ms": transcript.get("created_ms", 0),
        "updated_ms": transcript.get("updated_ms", 0),
        "messages": messages,
    }
    if resolved_model and resolved_model != display_model:
        thread_payload["resolved_model"] = resolved_model
    return {
        "provider": PROVIDER,
        "repo": repo_from_cwd(cwd),
        "branch": branch,
        "source_url": source_url,
        "labels": DEFAULT_LABELS,
        "thread": thread_payload,
    }


def parse_transcript(path: Path, fallback_session_id: str | None = None, image_uploader: ImageUploader | None = None) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    merge_sources: list[str] = []
    session_id = fallback_session_id or ""
    cwd = ""
    branch = ""
    model = ""
    created_ms = 0
    updated_ms = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") not in {"user", "assistant"}:
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            content = normalize_content(message.get("content"), image_uploader=image_uploader)
            if content is None:
                continue
            role = normalized_role(message.get("role") or entry.get("type"), content)
            content = normalize_local_command_message(role, content)
            if content is None or is_loaded_skill_body_message(role, content):
                continue
            source_message_id = message.get("id") or ""
            item = {
                "role": role,
                "content": content,
                "message_id": entry.get("uuid") or source_message_id or message.get("message_id") or "",
            }
            usage = message.get("usage")
            if isinstance(usage, dict):
                item["usage"] = redact_json(usage)
            protocol_id = message.get("protocolMessageID") or message.get("protocol_message_id")
            if protocol_id:
                item["protocol_message_id"] = protocol_id
            if is_duplicate_unavailable_image_message(messages, item):
                continue
            append_transcript_message(messages, merge_sources, item, source_message_id)
            session_id = session_id or entry.get("sessionId") or entry.get("session_id") or ""
            cwd = cwd or entry.get("cwd") or ""
            branch = branch or entry.get("gitBranch") or entry.get("git_branch") or ""
            if not model and role == "assistant":
                model = message.get("model") or ""
            stamp_ms = timestamp_ms(entry.get("timestamp"))
            if stamp_ms:
                if not created_ms:
                    created_ms = stamp_ms
                updated_ms = stamp_ms
    return {
        "id": session_id,
        "cwd": cwd,
        "branch": branch,
        "model": model,
        "created_ms": created_ms,
        "updated_ms": updated_ms or created_ms,
        "messages": messages,
    }


def display_model_name(resolved_model: str, environ: Mapping[str, str] | None = None) -> str:
    if not resolved_model:
        return ""
    env = os.environ if environ is None else environ
    for raw_model, display_name in configured_model_aliases(env):
        if model_matches_alias(resolved_model, raw_model):
            return display_name
    return resolved_model


def configured_model_aliases(environ: Mapping[str, str]) -> list[tuple[str, str]]:
    aliases: list[tuple[str, str]] = []
    for name in MODEL_ALIAS_ENV_NAMES:
        model = environ.get(name, "").strip()
        if model:
            aliases.append((model, model))
    custom_model = environ.get("ANTHROPIC_CUSTOM_MODEL_OPTION", "").strip()
    if custom_model:
        custom_name = environ.get("ANTHROPIC_CUSTOM_MODEL_OPTION_NAME", "").strip() or custom_model
        aliases.append((custom_model, custom_name))
    return aliases


def model_matches_alias(resolved_model: str, alias: str) -> bool:
    if resolved_model == alias:
        return True
    return re.fullmatch(re.escape(alias) + r"-\d{4}-\d{2}-\d{2}", resolved_model) is not None


def append_transcript_message(messages: list[dict[str, Any]], merge_sources: list[str], item: dict[str, Any], source_message_id: str) -> None:
    if (
        source_message_id
        and messages
        and merge_sources[-1] == source_message_id
        and messages[-1].get("role") == item.get("role") == "assistant"
    ):
        messages[-1]["content"] = merge_content(messages[-1].get("content"), item.get("content"))
        if item.get("usage"):
            messages[-1]["usage"] = item["usage"]
        if item.get("protocol_message_id"):
            messages[-1]["protocol_message_id"] = item["protocol_message_id"]
        return
    messages.append(item)
    merge_sources.append(source_message_id)


def merge_content(first: Any, second: Any) -> Any:
    return content_blocks(first) + content_blocks(second)


def content_blocks(content: Any) -> list[Any]:
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if content is None:
        return []
    return [content]


def jieli_thread_id(session_id: str) -> str:
    value = session_id.strip()
    if not value:
        return value
    return value if value.startswith("T-") else f"T-{value}"


def claude_session_id(provider_thread_id: str) -> str:
    value = provider_thread_id.strip()
    return value[2:] if value.startswith("T-") else value


def normalize_local_command_message(role: str, content: Any) -> Any | None:
    if role != "user":
        return content
    text = text_from_normalized_content(content).strip()
    if text.startswith("<command-message>"):
        command_name = tag_text(text, "command-name")
        return command_name or None
    skippable_local_command_prefixes = (
        "<local-command-caveat>",
        "<command-name>",
        "<local-command-stdout>",
        "<local-command-stderr>",
    )
    if any(text.startswith(prefix) for prefix in skippable_local_command_prefixes):
        return None
    return content


def tag_text(text: str, tag_name: str) -> str:
    match = re.search(rf"<{tag_name}>\s*(.*?)\s*</{tag_name}>", text, re.DOTALL)
    if not match:
        return ""
    return match.group(1).strip()


def is_loaded_skill_body_message(role: str, content: Any) -> bool:
    if role != "user":
        return False
    text = text_from_normalized_content(content).lstrip()
    return text.startswith("Base directory for this skill:")


def text_from_normalized_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n\n".join(parts)
    return ""


def normalize_content(content: Any, image_uploader: ImageUploader | None = None) -> Any | None:
    if isinstance(content, str):
        return normalize_text_with_images(content, image_uploader)
    if isinstance(content, list):
        blocks: list[Any] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_result":
                blocks.append(redact_json(block))
                continue
            if block_type == "text":
                text = block.get("text", "")
                if text:
                    append_blocks(blocks, normalize_text_blocks(str(text), image_uploader))
                continue
            if block_type == "thinking":
                thinking = block.get("thinking", "")
                if thinking:
                    blocks.append({"type": "thinking", "thinking": redact_text(str(thinking))})
                continue
            if block_type == "image":
                image_block = image_block_from_path(image_path_from_block(block), image_uploader)
                if image_block:
                    blocks.append(image_block)
                elif not has_existing_image_label(blocks):
                    blocks.append({"type": "text", "text": "[Image unavailable]"})
                continue
            blocks.append(redact_json(block))
        return collapse_text_only_blocks(blocks)
    if content is None:
        return None
    return redact_json(content)


def normalized_role(role: Any, content: Any) -> str:
    value = str(role or "")
    blocks = content if isinstance(content, list) else []
    if blocks and all(isinstance(block, dict) and block.get("type") == "tool_result" for block in blocks):
        return "tool"
    return value


def normalize_text_with_images(text: str, image_uploader: ImageUploader | None = None) -> Any | None:
    return collapse_text_only_blocks(normalize_text_blocks(text, image_uploader))


def normalize_text_blocks(text: str, image_uploader: ImageUploader | None = None) -> list[dict[str, Any]]:
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
    value = redact_text(text).strip()
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


def is_duplicate_unavailable_image_message(messages: list[dict[str, Any]], item: dict[str, Any]) -> bool:
    if item.get("role") != "user" or normalized_content_text(item.get("content")) != "[Image unavailable]":
        return False
    if not messages or messages[-1].get("role") != "user":
        return False
    previous_text = normalized_content_text(messages[-1].get("content"))
    return "[Image unavailable]" in previous_text or IMAGE_LABEL_RE.search(previous_text) is not None


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


def media_type_for_image(path: Path) -> str:
    media_type, _ = mimetypes.guess_type(path.name)
    return media_type if media_type in SUPPORTED_IMAGE_MEDIA_TYPES else ""


def title_from_messages(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()[:80]
    return "Claude Code session"


def repo_from_cwd(cwd: str) -> str:
    parts = [part for part in Path(cwd).parts if part not in {"/", ""}]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return parts[0] if parts else ""


def timestamp_ms(value: Any) -> int:
    if not isinstance(value, str) or not value:
        return 0
    try:
        from datetime import datetime

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
    def __init__(self, home: Path | None = None):
        self.home = home or Path.home()
        self.path = self.home / ".jieli" / "sync.lock"
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
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
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
    digest = file_sha256(path)
    cache_key = "|".join([base_url.rstrip("/"), media_type, digest])
    cache = load_attachment_cache(home)
    cached_url = cache.get(cache_key)
    if isinstance(cached_url, str) and cached_url:
        return cached_url
    url = upload_attachment(path, base_url, api_key)
    cache[cache_key] = url
    write_attachment_cache(cache, home)
    return url


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def write_session_mapping(session_id: str, base_url: str, home: Path | None = None, provider_thread_id: str | None = None) -> None:
    home = home or Path.home()
    path = home / ".jieli" / "claude-sessions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, Any] = {}
    if path.exists():
        try:
            mapping = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            mapping = {}
    mapping[session_id] = {"provider_thread_id": provider_thread_id or jieli_thread_id(session_id), "base_url": base_url.rstrip("/")}
    path.write_text(json.dumps(mapping, indent=2, sort_keys=True), encoding="utf-8")
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trigger", default="")
    parser.add_argument("--hook-version", default="")
    parser.add_argument("--jieli-hook", action="store_true")
    args = parser.parse_args()
    try:
        missing = missing_config_vars()
        if missing:
            response = build_missing_config_hook_response(args.trigger, missing)
            if response:
                print(json.dumps(response))
            raise KeyError(", ".join(missing))
        with SyncLock() as lock:
            if not lock.acquired:
                return 0
            hook_data = load_hook_stdin()
            transcript_path = hook_data.get("transcript_path")
            if args.trigger.lower() in TRANSCRIPT_FLUSH_TRIGGERS and transcript_path:
                wait_for_transcript_flush(Path(transcript_path))
            base_url = (optional_env("JIELI_BASE_URL", "CLAUDE_PLUGIN_OPTION_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
            api_key = required_env("JIELI_API_KEY", "CLAUDE_PLUGIN_OPTION_API_KEY")
            payload = build_payload_from_hook(
                hook_data,
                base_url=base_url,
                image_uploader=lambda path: upload_attachment_cached(path, base_url, api_key),
            )
            upload_payload(payload, base_url, api_key)
            provider_thread_id = payload["thread"]["id"]
            write_session_mapping(claude_session_id(provider_thread_id), base_url, provider_thread_id=provider_thread_id)
    except (KeyError, ValueError, OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        log_hook_error(f"sync {args.trigger}: {type(error).__name__}: {error}")
    return 0


def required_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    raise KeyError(names[0])


def optional_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
