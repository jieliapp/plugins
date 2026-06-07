#!/usr/bin/env python3
"""Merge/unmerge Jieli hooks into Claude Code settings."""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any


SYNC_EVENTS = {
    "SessionStart": "sessionstart",
    "UserPromptSubmit": "userpromptsubmit",
    "PreCompact": "precompact",
    "Stop": "stop",
    "SessionEnd": "sessionend",
}
JIELI_MARKER = "--jieli-hook"


def install_hooks(settings: dict[str, Any], plugin_root: str, version: str) -> dict[str, Any]:
    updated = deepcopy(settings)
    hooks = updated.setdefault("hooks", {})
    for event, trigger in SYNC_EVENTS.items():
        command = f'python3 "{plugin_root}/scripts/sync.py" --trigger {trigger} --hook-version {version} {JIELI_MARKER}'
        append_hook(hooks, event, "", command)
    command = f'python3 "{plugin_root}/scripts/commit_trailer.py" --hook-version {version} {JIELI_MARKER}'
    append_hook(hooks, "PreToolUse", "Bash", command)
    return updated


def uninstall_hooks(settings: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(settings)
    hooks = updated.get("hooks")
    if not isinstance(hooks, dict):
        return updated
    for event in list(hooks.keys()):
        configs = []
        for config in hooks[event]:
            commands = [hook for hook in config.get("hooks", []) if not is_jieli_hook(hook)]
            if commands:
                next_config = deepcopy(config)
                next_config["hooks"] = commands
                configs.append(next_config)
        if configs:
            hooks[event] = configs
        else:
            del hooks[event]
    if not hooks:
        updated.pop("hooks", None)
    return updated


def append_hook(hooks: dict[str, Any], event: str, matcher: str, command: str) -> None:
    configs = hooks.setdefault(event, [])
    for config in configs:
        if config.get("matcher", "") == matcher:
            config["hooks"] = [hook for hook in config.get("hooks", []) if not is_jieli_hook(hook)]
            config["hooks"].append({"type": "command", "command": command})
            return
    configs.append({"matcher": matcher, "hooks": [{"type": "command", "command": command}]})


def is_jieli_hook(hook: dict[str, Any]) -> bool:
    return JIELI_MARKER in str(hook.get("command", ""))


def settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "uninstall"])
    parser.add_argument("--plugin-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--version", default="0.1.1")
    parser.add_argument("--settings", default=str(settings_path()))
    args = parser.parse_args()
    path = Path(args.settings)
    settings: dict[str, Any] = {}
    if path.exists():
        settings = json.loads(path.read_text(encoding="utf-8"))
    updated = install_hooks(settings, args.plugin_root, args.version) if args.action == "install" else uninstall_hooks(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
