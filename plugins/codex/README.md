# Jieli Codex Sync

Sync local Codex sessions to Jieli threads, redact common secrets before upload, provide `jieli-read` and `jieli-find` skills for reading/searching synced threads, and add best-effort `Jieli-Thread` trailers to Codex-created git commits.

## Configuration

Recommended: write `~/.config/jieli/settings.json`. This works even after Codex is already running:

```bash
mkdir -p ~/.config/jieli
node - <<'JS'
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const settingsPath = path.join(os.homedir(), ".config", "jieli", "settings.json");
fs.writeFileSync(
  settingsPath,
  JSON.stringify({ api_key: "your-jieli-api-key", base_url: "https://jieli.app" }, null, 2) + "\n",
  { mode: 0o600 },
);
JS
```

Jieli defaults to `https://jieli.app`.

You can also use environment variables before starting Codex; environment variables override `settings.json`:

```bash
export JIELI_API_KEY="your-jieli-api-key"
```

## Hooks

Codex discovers plugin hooks from `hooks/hooks.json`. After installing or enabling the plugin, review and trust the bundled hooks with `/hooks`.

The plugin syncs on:

- `SessionStart`
- `PreCompact`
- `Stop`

The shell `PreToolUse` hook attempts to rewrite simple `git commit` commands by appending:

```text
--trailer "Jieli-Thread: https://jieli.app/threads/T-..."
```

It rewrites commands that contain exactly one top-level `git commit`, including common `&&` chains such as `git status && git add ... && git commit ...`. It does not rewrite commands containing pipes, heredocs, subshells, backgrounding, multiple commits, or multiple lines. It does not install Git hooks and does not affect commits made outside Codex.

## Local State

Session mappings are stored at:

```text
~/.jieli/codex-sessions.json
```

Plugin settings are read from:

```text
~/.config/jieli/settings.json
```

Hook errors are appended to:

```text
~/.jieli/hooks.log
```

## Development

```bash
node --test plugins/codex/tests/runtime-node.test.mjs
python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/codex
```
