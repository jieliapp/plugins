---
name: jieli-find
description: "Search prior Jieli threads by keywords, repo, file, topic, or other clues, then optionally read a matching thread."
---

# Jieli Find

## Inputs

- Search keywords or clue text.
- Optional explicit filters from the user, such as repo, label, page size, page, sort, or provider.
- Jieli credentials from `JIELI_API_KEY` or `~/.config/jieli/settings.json`.
- Jieli uses `https://jieli.app` by default; no base URL configuration is needed for hosted Jieli.

## Environment

Use the `jieli-find-threads` command for thread search. It is installed from the plugin `bin/` directory and resolves the plugin scripts path itself. If the command is not on `PATH`, resolve `../../scripts/jieli_helper.mjs` relative to this `SKILL.md` file and run `node <resolved-skill-dir>/../../scripts/jieli_helper.mjs find-threads ...`. Do not duplicate raw `curl` calls unless you are diagnosing the helper, and do not enumerate installed helpers or choose wrapper files in this skill.

If the API key is missing, ask the user for a Jieli API key and write it to `~/.config/jieli/settings.json` with mode `600`.

## Search Procedure

1. Build the shortest useful search query from the user's clue. Keep file paths, repo names, branch names, error text, or topic words intact.
2. Search all providers by default:

```bash
jieli-find-threads "<query>" --page-size 10 --sort updated
```

3. Do not pass --provider based on the current agent. Do not set `--provider codex` merely because you are Codex, and do not set `--provider claude_code` merely because the clue looks like a Claude Code task.
4. Pass `--provider` only when the user explicitly asks for one, such as "only Codex", "only Claude Code", or an explicit provider argument:

```bash
jieli-find-threads "<query>" --provider codex --page-size 10 --sort updated
jieli-find-threads "<query>" --provider claude_code --page-size 10 --sort updated
```

5. Add optional filters only when they are explicit or strongly implied by the user's clue:

```bash
jieli-find-threads "<query>" --repo "<repo>" --label "<label>" --page-size 10 --page 2 --sort updated
```

6. Read results from the helper output. The stable thread id for follow-up reads is `thread_id`/`provider_thread_id`; the read URL is printed as `read_url`.
7. Return a compact ranked list with title, provider, repo/branch when present, updated time, message count, preview, and read URL.
8. If one result clearly answers the user's request, call `jieli-read`/`jieli-read-thread` on that result's thread id before answering. Otherwise ask the user which result to inspect.
9. If the helper fails, report that search could not be completed and include the non-secret error message.

## Output

For search-only requests, return a concise ranked list. For requests that need thread content, read the selected result and summarize:

- Thread title/id.
- Why it matched the clue.
- Key user goals or decisions from the thread.
- Files, commands, and verification evidence mentioned.
- Remaining open questions or follow-up tasks.

Do not expose API keys, secrets, or private tokens if they appear in retrieved results or transcripts.
