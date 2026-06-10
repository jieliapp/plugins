---
name: jieli
description: "Read Jieli /threads/T-<uuid> links or raw T-<uuid> thread ids via .md exports, or search Jieli threads as Claude Code context."
---

# Jieli

## When to Use

Use this skill immediately when:

- The user pastes or references a Jieli thread URL whose path contains `/threads/<thread_id>`, including relative links like `/threads/T-...` and export links like `/threads/<thread_id>.md` or `/threads/<thread_id>.json`.
- The user references a raw provider thread id, such as `T-...`, or asks to apply, summarize, continue, compare, or reuse work from a previous Jieli thread.
- The user asks to find/search previous Jieli threads, asks which thread mentioned something, or gives a keyword/file/repo clue instead of a known thread id.

Do not use this skill for the current conversation when the needed context is already visible.

## Inputs

- A Jieli URL such as `https://jieli.example.com/threads/<provider_thread_id>`, `https://jieli.example.com/threads/<provider_thread_id>.md`, or a raw provider thread id.
- The user's question or goal for reading the thread. If none is provided, summarize the recent goal, decisions, touched files, commands, verification, and open follow-ups.
- Jieli credentials from `JIELI_API_KEY`, plugin user config exported as `CLAUDE_PLUGIN_OPTION_API_KEY`, or `~/.config/jieli/settings.json`.
- Optional base URL from `JIELI_BASE_URL`, `CLAUDE_PLUGIN_OPTION_BASE_URL`, or `~/.config/jieli/settings.json`. If omitted, use `https://jieli.app`.

## Environment

Use the `jieli-read-thread` command for thread reads. It is installed from the plugin `bin/` directory and resolves the plugin scripts path itself. Do not call `python3 "$CLAUDE_PLUGIN_ROOT/scripts/read_thread.py"` from Bash, and do not guess cache paths such as `skills/jieli/scripts/read_thread.py`.

Prefer these shell variables in examples:

```bash
BASE_URL="${JIELI_BASE_URL:-${CLAUDE_PLUGIN_OPTION_BASE_URL:-https://jieli.app}}"
API_KEY="${JIELI_API_KEY:-$CLAUDE_PLUGIN_OPTION_API_KEY}"
```

If `API_KEY` is missing, ask the user to configure the plugin, export `JIELI_API_KEY`, or write `~/.config/jieli/settings.json`. Only ask for `JIELI_BASE_URL` when the user uses a self-hosted Jieli instance.

To configure hosted Jieli, ask the user to sign in at `https://jieli.app`, copy an API key, and either:

- Paste the key into the current agent chat and ask the agent to set it.
- Configure the plugin `api_key` option, which Claude Code exports as `CLAUDE_PLUGIN_OPTION_API_KEY`.
- Write `~/.config/jieli/settings.json`.
- Export it manually:

```bash
export JIELI_API_KEY="<api-key-from-jieli.app>"
```

For self-hosted Jieli, also set `JIELI_BASE_URL` to the deployment URL.

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

## Search Procedure

Use this when the user asks to find/search previous Jieli threads, asks which thread mentioned something, or gives a keyword/file/repo clue instead of a known thread id.

Call the plugin thread list endpoint:

```bash
curl -fsSL -G \
  -H "Authorization: Bearer $API_KEY" \
  --data-urlencode "search=<query>" \
  --data-urlencode "provider=claude_code" \
  --data-urlencode "page_size=10" \
  --data-urlencode "sort=updated" \
  "$BASE_URL/plugin/threads"
```

Guidance:

- Use `provider=claude_code` when the user is asking about Claude Code synced sessions.
- Omit `provider` to search all Jieli providers, or set a specific provider such as `amp` or future providers like `codex`.
- Optional filters supported by the endpoint include `repo`, `label`, `visibility`, `archived`, `page`, `page_size`, and `sort`.
- Read results from `data.threads`. The stable thread id for follow-up reads is `provider_thread_id`.
- Return a compact ranked list with title, provider, repo/branch when present, updated time, message count, preview, and the read URL.
- If one result clearly answers the user's request, read it with the Read Procedure before answering.

## Output

Return a concise context summary with:

- Thread title/id.
- Key user goals.
- Important implementation decisions.
- Files, commands, and verification evidence mentioned.
- Remaining open questions or follow-up tasks.

Do not expose API keys, secrets, or private tokens if they appear in the retrieved transcript.
