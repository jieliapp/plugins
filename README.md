# Jieli Plugins

Plugins for syncing AI coding sessions to [Jieli](https://jieli.app).

## Claude Code

Install the Claude Code plugin:

```bash
curl -fsSL https://raw.githubusercontent.com/burugo/jieli-plugins/main/install-claude-code.sh | bash
```

Then configure your API key:

```bash
export JIELI_API_KEY="your-jieli-api-key"
```

You can get an API key from [https://jieli.app](https://jieli.app).

For self-hosted Jieli:

```bash
export JIELI_BASE_URL="https://your-jieli.example.com"
export JIELI_API_KEY="your-jieli-api-key"
```

Manual install:

```bash
git clone https://github.com/burugo/jieli-plugins.git ~/.jieli/plugins/jieli-plugins
claude plugin install ~/.jieli/plugins/jieli-plugins/claude-code
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
