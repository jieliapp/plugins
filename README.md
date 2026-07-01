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

Get an API key from [https://jieli.app](https://jieli.app). You can use an environment variable before starting the agent:

```bash
export JIELI_API_KEY="your-jieli-api-key"
```

Recommended for both Claude Code and Codex: write this file, which works even after the agent is already running:

Path: `~/.config/jieli/settings.json`

```json
{
  "api_key": "your-jieli-api-key",
  "commit_trailer": true
}
```

`commit_trailer` is optional and defaults to `true`. Set it to `false` to disable the automatic `Jieli-Thread` trailer added by the `PreToolUse` hook.

## What It Does

- Syncs Claude Code sessions to Jieli threads.
- Syncs local Codex sessions to Jieli threads.
- Uploads pasted local images as Jieli attachments.
- Adds best-effort `Jieli-Thread` trailers to simple Claude Code and Codex `git commit` commands.
- Provides the `jieli-read-thread` skill for reading known Jieli thread links or ids.
- Provides the `jieli-find-thread` skill for searching synced Jieli threads by keywords, repo, file, topic, or clues.
- Provides the `handoff` skill for creating a paste-ready continuation prompt for a fresh agent, including the current Jieli thread id and URL when available.
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
