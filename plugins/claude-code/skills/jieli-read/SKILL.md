---
name: jieli-read
description: "Read a specific known Jieli /threads/T-<uuid> link or raw T-<uuid> thread id via the markdown/json export."
---

# Jieli Read

## Environment

Resolve `../../scripts/jieli_helper.mjs` relative to this `SKILL.md` file and run it directly for thread reads:

```bash
node <resolved-skill-dir>/../../scripts/jieli_helper.mjs read-thread ...
```

Do not depend on plugin `bin/` commands being on `PATH`; agent app shells may not include the plugin `bin/` directory. Do not call plugin scripts by cache path, enumerate installed helpers, or choose wrapper files in this skill.

If the API key is missing, ask the user to configure the plugin, export `JIELI_API_KEY`, or write `~/.config/jieli/settings.json`. Jieli uses `https://jieli.app` by default.

## Read Procedure

1. Resolve the provider thread id from the URL or raw id, stripping `/threads/`, full URLs, and trailing `.md` or `.json`.
2. Start with a bounded markdown read:

```bash
node <resolved-skill-dir>/../../scripts/jieli_helper.mjs read-thread "<thread_id>" --truncate-tool-results --max-chars 12000
```

3. For missing details, use focused 1-based inclusive line ranges:

```bash
node <resolved-skill-dir>/../../scripts/jieli_helper.mjs read-thread "<thread_id>" --truncate-tool-results --start-line 120 --end-line 220 --max-chars 12000
```

4. Answer the user's question first. Do not paste the full transcript unless explicitly requested.
5. If full tool outputs or structured fields are needed, rerun a focused range without `--truncate-tool-results` or with `--format json`.
6. If the helper fails, report the non-secret error message.

## Output

Return a concise summary with the thread title/id, key goals or decisions, relevant files/commands/verification, and open follow-ups. Redact secrets.
