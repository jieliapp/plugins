#!/usr/bin/env python3
"""Read a Jieli thread export with local output limits."""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_MAX_CHARS = 20000
DEFAULT_BASE_URL = "https://jieli.app"
EXPORT_TIMEOUT_SECONDS = 20


def validate_thread_id(thread_id: str) -> str:
    value = thread_id.strip()
    if not value:
        raise ValueError("thread_id is required")
    if "://" in value or "/" in value or "\\" in value:
        raise ValueError("pass only the provider thread id, not a /threads/... URL")
    if value.endswith(".md") or value.endswith(".json"):
        raise ValueError("pass the provider thread id without .md or .json")
    if any(char.isspace() for char in value):
        raise ValueError("thread_id must not contain whitespace")
    return value


def fetch_thread_export(
    thread_id: str,
    base_url: str,
    api_key: str,
    export_format: str = "md",
    truncate_tool_results: bool = False,
) -> str:
    clean_id = validate_thread_id(thread_id)
    if export_format not in {"md", "json"}:
        raise ValueError("export_format must be md or json")
    quoted_id = urllib.parse.quote(clean_id, safe="")
    url = f"{base_url.rstrip('/')}/threads/{quoted_id}.{export_format}"
    if export_format == "md" and truncate_tool_results:
        url += "?truncate_tool_results=1"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=EXPORT_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", errors="replace")


def limit_output(
    content: str,
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int | None = DEFAULT_MAX_CHARS,
) -> str:
    if start_line is not None and start_line < 1:
        raise ValueError("--start-line must be >= 1")
    if end_line is not None and end_line < 1:
        raise ValueError("--end-line must be >= 1")
    if start_line is not None and end_line is not None and end_line < start_line:
        raise ValueError("--end-line must be >= --start-line")
    if max_chars is not None and max_chars < 0:
        raise ValueError("--max-chars must be >= 0")

    selected = content
    if start_line is not None or end_line is not None:
        lines = content.splitlines(keepends=True)
        start_index = (start_line or 1) - 1
        end_index = end_line if end_line is not None else len(lines)
        selected = "".join(lines[start_index:end_index])

    if max_chars and len(selected) > max_chars:
        selected = selected[:max_chars]
        selected += (
            f"\n\n[Content truncated at {max_chars} chars; "
            "rerun with --start-line/--end-line or increase --max-chars.]"
        )
    return selected


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Read a Jieli thread export.")
    parser.add_argument("thread_id", help="Provider thread id only, for example T-abc123")
    parser.add_argument("--format", choices=("md", "json"), default="md")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--start-line", type=int)
    parser.add_argument("--end-line", type=int)
    parser.add_argument(
        "--truncate-tool-results",
        action="store_true",
        help="Ask the markdown export to replace tool_result blocks with a short placeholder.",
    )
    args = parser.parse_args()

    try:
        base_url = optional_env("JIELI_BASE_URL", "CLAUDE_PLUGIN_OPTION_BASE_URL") or DEFAULT_BASE_URL
        api_key = required_env("JIELI_API_KEY", "CLAUDE_PLUGIN_OPTION_API_KEY")
        content = fetch_thread_export(args.thread_id, base_url, api_key, args.format, args.truncate_tool_results)
        sys.stdout.write(limit_output(content, args.start_line, args.end_line, args.max_chars))
        return 0
    except (KeyError, ValueError, urllib.error.URLError) as error:
        print(f"read_thread failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
