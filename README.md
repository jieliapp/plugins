# Jieli Plugins

Plugins for syncing AI coding sessions to [Jieli](https://jieli.app).

## Claude Code Install

Add the marketplace in Claude Code:

```text
/plugin marketplace add jieliapp/plugins
```

Install the plugin:

```text
/plugin install jieli@jieliapp
```

Then get an API key from [https://jieli.app](https://jieli.app) and configure it for Claude Code.

When Claude Code opens the plugin configuration screen, set:

```text
Jieli API key = your-jieli-api-key
```

Reload plugins:

```text
/reload-plugins
```

You can also set it through your shell environment before starting Claude Code:

```bash
export JIELI_API_KEY="your-jieli-api-key"
```

## Codex Install

Add the marketplace in Codex:

```bash
codex plugin marketplace add jieliapp/plugins
```

Install the plugin:

```bash
codex plugin add jieli@jieliapp
```

Then enable the plugin and trust its hooks with `/hooks`.

Configure the API key. Recommended for Codex and Claude Code: write `~/.config/jieli/settings.json`, which works even after the agent is already running:

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

You can also use environment variables before starting Codex:

```bash
export JIELI_API_KEY="your-jieli-api-key"
```

## What It Does

- Syncs Claude Code sessions to Jieli threads.
- Syncs local Codex sessions to Jieli threads.
- Uploads pasted local images as Jieli attachments.
- Adds `Claude-Code-Thread-ID` trailers to Claude-created git commits.
- Adds best-effort `Jieli-Thread` trailers to simple Codex-created `git commit` commands.
- Provides the `jieli-read` skill for reading known Jieli thread links or ids.
- Provides the `jieli-find` skill for searching synced Jieli threads by keywords, repo, file, topic, or clues.
- Redacts common secrets before upload.

## Development

```bash
make test
make validate
```
