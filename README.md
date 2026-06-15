# Jieli Plugins

Plugins for syncing AI coding sessions to [Jieli](https://jieli.app).

## Claude Code Install

Run these in your shell:

```bash
claude plugin marketplace add jieliapp/plugins
claude plugin install jieli@jieliapp
```

## Codex Install

Run these in your shell:

```bash
codex plugin marketplace add jieliapp/plugins
codex plugin add jieli@jieliapp
```

Then enable the plugin and trust its hooks with `/hooks`.

## Configure the API key

Get an API key from [https://jieli.app](https://jieli.app). Recommended for both Claude Code and Codex: write `~/.config/jieli/settings.json`, which works even after the agent is already running:

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

You can also use an environment variable before starting the agent:

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

## Redaction

Secrets are redacted locally before anything is uploaded. Each match is replaced
with a typed marker like `[REDACTED:openai-api-key]`, so the conversation stays
readable while the secret value is gone.

Covered: vendor API keys and tokens, private keys, JWTs, `Bearer` headers,
credentials in connection URLs, and sensitive `KEY=value` assignments in
env/JSON/YAML. Base64 image data is left intact.

This is best-effort pattern matching, not a guarantee. See the patterns in
`plugins/*/scripts/jieli_node.mjs` (covered by `make test`).

## Development

```bash
make test
make validate
```
