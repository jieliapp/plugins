# Jieli Codex Sync

Sync local Codex sessions to Jieli threads, redact common secrets before upload, provide a `jieli` skill for reading synced threads, and add best-effort `Codex-Thread-ID` trailers to Codex-created git commits.

## Configuration

Recommended: write `~/.jieli/settings.json`. This works even after Codex is already running:

```bash
mkdir -p ~/.jieli
python3 - <<'PY'
import json
from pathlib import Path

path = Path.home() / ".jieli/settings.json"
settings = {"api_key": "your-jieli-api-key", "base_url": "https://jieli.app"}
path.write_text(json.dumps(settings, indent=2) + "\n")
path.chmod(0o600)
PY
```

Hosted Jieli defaults to `https://jieli.app`. For self-hosted Jieli, set `base_url` to your deployment URL.

You can also use environment variables before starting Codex; environment variables override `settings.json`:

```bash
export JIELI_API_KEY="your-jieli-api-key"
export JIELI_BASE_URL="https://your-jieli.example.com"
```

## Hooks

Codex discovers plugin hooks from `hooks/hooks.json`. After installing or enabling the plugin, review and trust the bundled hooks with `/hooks`.

The plugin syncs on:

- `SessionStart`
- `UserPromptSubmit`
- `PreCompact`
- `PostCompact`
- `Stop`

The `PreToolUse(Bash)` hook attempts to rewrite simple `git commit` commands by appending:

```text
--trailer "Codex-Thread-ID: https://jieli.app/threads/T-..."
```

It rewrites commands that contain exactly one top-level `git commit`, including common `&&` chains such as `git status && git add ... && git commit ...`. It does not rewrite commands containing pipes, heredocs, subshells, backgrounding, multiple commits, or multiple lines. It does not install Git hooks and does not affect commits made outside Codex.

## Local State

Session mappings are stored at:

```text
~/.jieli/codex-sessions.json
```

Plugin settings are read from:

```text
~/.jieli/settings.json
```

Hook errors are appended to:

```text
~/.jieli/hooks.log
```

## Development

```bash
python3 -m unittest plugins/codex/tests/test_plugin_scripts.py
python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/codex
```
