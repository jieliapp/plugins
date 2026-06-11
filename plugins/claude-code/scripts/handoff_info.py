#!/usr/bin/env python3
"""Emit handoff metadata for the current Claude Code session."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import sync


PROVIDER = "claude_code"
CONTEXT_ENV = "JIELI_HANDOFF_CONTEXT_B64"


def build_info(environ: dict[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    context = decode_context(env.get(CONTEXT_ENV, ""))
    if not context:
        return missing_info("missing hook context")

    session_id = str(context.get("session_id") or "").strip()
    if not session_id:
        return missing_info("missing session_id in hook context")

    cwd = str(context.get("cwd") or "").strip()
    transcript_path = str(context.get("transcript_path") or "").strip()
    base_url = (sync.optional_env("JIELI_BASE_URL", "CLAUDE_PLUGIN_OPTION_BASE_URL") or sync.DEFAULT_BASE_URL).rstrip("/")
    thread_id = sync.jieli_thread_id(session_id)
    return {
        "confidence": "high",
        "provider": PROVIDER,
        "session_id": session_id,
        "thread_id": thread_id,
        "url": f"{base_url}/threads/{thread_id}" if base_url and thread_id else "",
        "base_url": base_url,
        "cwd": cwd,
        "transcript_path": transcript_path,
        "repo_url": sync.repo_url_from_cwd(cwd),
        "branch": sync.git_branch(cwd),
        "worktree_status": worktree_status(cwd),
        "reason": "hook context injected by PreToolUse",
    }


def decode_context(encoded: str) -> dict[str, Any]:
    if not encoded.strip():
        return {}
    try:
        raw = base64.b64decode(encoded).decode("utf-8")
        value = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def missing_info(reason: str) -> dict[str, Any]:
    return {
        "confidence": "missing",
        "provider": PROVIDER,
        "session_id": "",
        "thread_id": "",
        "url": "",
        "base_url": "",
        "cwd": "",
        "transcript_path": "",
        "repo_url": "",
        "branch": "",
        "worktree_status": "unknown",
        "reason": reason,
    }


def worktree_status(cwd: str) -> str:
    if not cwd:
        return "unknown"
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return "dirty" if result.stdout.strip() else "clean"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    print(json.dumps(build_info(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
