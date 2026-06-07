# Jieli Plugins

Plugins for syncing AI coding sessions to [Jieli](https://jieli.app).

## Install

Add the marketplace in Claude Code:

```text
/plugin marketplace add burugo/jieli-plugins
```

Install the plugin:

```text
/plugin install claude-code@jieli
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

For self-hosted Jieli, also set:

```bash
export JIELI_BASE_URL="https://your-jieli.example.com"
```

## What It Does

- Syncs Claude Code sessions to Jieli threads.
- Uploads pasted local images as Jieli attachments.
- Adds `Claude-Code-Thread-ID` trailers to Claude-created git commits.
- Provides the `jieli` skill for reading Jieli thread links from Claude Code.
- Redacts common secrets before upload.

## Development

```bash
make test
make validate
```
