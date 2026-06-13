---
name: jieli-read
description: "Read a specific known Jieli /threads/T-<uuid> link or raw T-<uuid> thread id via the markdown/json export."
---

# Jieli Read

## When to Use

Use this skill immediately when:

- The user pastes or references a Jieli thread URL whose path contains `/threads/<thread_id>`, including relative links like `/threads/T-...` and export links like `/threads/<thread_id>.md` or `/threads/<thread_id>.json`.
- The user references a raw provider thread id, such as `T-...`.
- The user asks to apply, summarize, continue, compare, or reuse work from a specific known Jieli thread.

Do not use this skill when the user only gives keywords, repo names, file names, or vague clues. Use `jieli-find` first for search/discovery. Do not use this skill for the current conversation when the needed context is already visible.

## Inputs

- A Jieli URL such as `https://jieli.app/threads/<provider_thread_id>`, `https://jieli.app/threads/<provider_thread_id>.md`, or a raw provider thread id.
- The user's question or goal for reading the thread. If none is provided, summarize the recent goal, decisions, touched files, commands, verification, and open follow-ups.
- Jieli credentials from `JIELI_API_KEY` or `~/.config/jieli/settings.json`.
- Jieli uses `https://jieli.app` by default; no base URL configuration is needed for hosted Jieli.

## Environment

Use the `jieli-read-thread` command for thread reads. It is installed from the plugin `bin/` directory and resolves the plugin scripts path itself. Do not call plugin scripts by cache path, and do not guess paths such as `skills/jieli/scripts/read_thread.mjs`.

If the API key is missing, ask the user for a Jieli API key and write it to `~/.config/jieli/settings.json` with mode `600`.

To configure hosted Jieli, ask the user to sign in at `https://jieli.app`, copy an API key, and then write:

```json
{
  "api_key": "<api-key-from-jieli.app>",
  "base_url": "https://jieli.app"
}
```

Use:

```bash
mkdir -p ~/.config/jieli
node - <<'JS'
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const settingsPath = path.join(os.homedir(), ".config", "jieli", "settings.json");
fs.writeFileSync(
  settingsPath,
  JSON.stringify({ api_key: "<api-key-from-jieli.app>", base_url: "https://jieli.app" }, null, 2) + "\n",
  { mode: 0o600 },
);
JS
```

Replace the placeholder before writing the file.

## Read Procedure

1. Resolve the thread id from the URL path or use the raw id. Strip a trailing `.md` or `.json` extension from the last path segment.
2. Pass only the provider thread id to the helper script. Do not pass `/threads/<thread_id>`, a full URL, `.md`, or `.json` to the script.
3. Treat a plain page/share URL such as `/threads/<thread_id>` as a reference to the same thread. The helper reads the agent-friendly markdown export at `/threads/<thread_id>.md`.
4. Start with a bounded read to avoid loading the whole transcript into context:

```bash
jieli-read-thread "<thread_id>" --truncate-tool-results --max-chars 20000
```

5. For focused follow-up reads, use 1-based inclusive line ranges:

```bash
jieli-read-thread "<thread_id>" --truncate-tool-results --start-line 120 --end-line 220 --max-chars 20000
```

6. Treat the markdown response as the canonical readable thread source. Answer the user's specific question first; do not paste the full transcript unless explicitly requested.
7. Use `--truncate-tool-results` for the first pass on long threads. This keeps the transcript readable by shortening verbose tool outputs.
8. The helper still applies local `--max-chars`, `--start-line`, and `--end-line` limits.
9. If full tool outputs are needed, rerun without `--truncate-tool-results`, preferably with a focused line range.
10. If structured JSON fields are required, call the helper with `--format json` and the same local output limits.
11. If the helper fails, report that the thread could not be loaded and include the non-secret error message.

## Output

Return a concise context summary with:

- Thread title/id.
- Key user goals.
- Important implementation decisions.
- Files, commands, and verification evidence mentioned.
- Remaining open questions or follow-up tasks.

Do not expose API keys, secrets, or private tokens if they appear in the retrieved transcript.
