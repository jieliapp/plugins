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
- Jieli credentials from `JIELI_API_KEY`, plugin user config exported as `CLAUDE_PLUGIN_OPTION_API_KEY`, or `~/.config/jieli/settings.json`.
- Jieli uses `https://jieli.app` by default; no base URL configuration is needed for hosted Jieli.

## Environment

Use the `jieli-read-thread` command for thread reads. It is installed from the plugin `bin/` directory and resolves the plugin scripts path itself. Do not call plugin scripts by cache path, and do not guess paths such as `skills/jieli/scripts/read_thread.mjs`.

If the API key is missing, ask the user to configure the plugin, export `JIELI_API_KEY`, or write `~/.config/jieli/settings.json`.

For hosted Jieli, ask the user to sign in at `https://jieli.app`, copy an API key, and either:

- Paste the key into the current agent chat and ask the agent to set it.
- Configure the plugin `api_key` option, which Claude Code exports as `CLAUDE_PLUGIN_OPTION_API_KEY`.
- Write `~/.config/jieli/settings.json`.
- Export it manually:

```bash
export JIELI_API_KEY="<api-key-from-jieli.app>"
```

## Read Procedure

1. Resolve the thread id from the URL path or use the raw id. Strip a trailing `.md` or `.json` extension from the last path segment.
2. Pass only the provider thread id to the helper script. Do not pass `/threads/<thread_id>`, a full URL, `.md`, or `.json` to the script.
3. Treat a plain page/share URL such as `/threads/<thread_id>` as a reference to the same thread. The helper reads the agent-friendly markdown export at `/threads/<thread_id>.md`.
4. Start with a small bounded read for the main content. Do not pull the whole transcript on the first pass:

```bash
jieli-read-thread "<thread_id>" --truncate-tool-results --max-chars 12000
```

5. Only when details are missing, do focused follow-up reads with 1-based inclusive line ranges:

```bash
jieli-read-thread "<thread_id>" --truncate-tool-results --start-line 120 --end-line 220 --max-chars 12000
```

6. Treat the markdown response as the canonical readable thread source. Answer the user's specific question first; do not paste the full transcript unless explicitly requested.
7. Use `--truncate-tool-results` for the first pass on long threads. This keeps the transcript readable by shortening verbose tool outputs.
8. The helper still applies local `--max-chars`, `--start-line`, and `--end-line` limits.
9. Avoid first-pass reads such as `--max-chars 30000` or `--max-chars 80000` unless the user explicitly asks for a full transcript. For "main points" or "what happened" requests, prefer the small first read plus line-range follow-ups.
10. If full tool outputs are needed, rerun without `--truncate-tool-results`, preferably with a focused line range.
11. If structured JSON fields are required, call the helper with `--format json` and the same local output limits.
12. If the helper fails, report that the thread could not be loaded and include the non-secret error message.

## Output

Return a concise context summary with:

- Thread title/id.
- Key user goals.
- Important implementation decisions.
- Files, commands, and verification evidence mentioned.
- Remaining open questions or follow-up tasks.

Do not expose API keys, secrets, or private tokens if they appear in the retrieved transcript.
