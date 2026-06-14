---
name: handoff
description: "Create a paste-ready handoff/compress prompt for continuing the current Codex session in a fresh agent. Use when the user says handoff, hand over, compress, compact, summarize context, continue in a new session, or pass work to another agent."
---

# Handoff

Write a handoff document so a fresh agent can continue the work. If a Jieli thread is available, include its id and URL for optional transcript lookup.

## Inputs

- Optional next goal: what the next agent should do first. If the user did not give one, derive it from the most recent request and any open follow-ups in this conversation.
- Current session metadata from the `jieli-handoff-info` helper. It returns the current Jieli thread metadata without needing an API key.

## Procedure

### 1. Resolve current session metadata

Resolve `../../scripts/jieli_helper.mjs` relative to this `SKILL.md` file and run:

```bash
node <resolved-skill-dir>/../../scripts/jieli_helper.mjs handoff-info
```

If that fails or prints invalid JSON, try:

```bash
jieli-handoff-info
```

Do not enumerate plugin cache directories, choose wrapper files, or sort installed helpers in this skill.

Expected fields include `thread_id`, `url`, `cwd`, `repo`, `repo_url`, `branch`, and `worktree_status`.

Include Jieli thread lines only when both `thread_id` and `url` are present. If metadata is missing or invalid, continue without a thread id and do not guess from recent rollouts.

### 2. Compose the handoff context

You already hold the full conversation, so write the context yourself; do not call a model or read the full transcript. If the user passed arguments, treat them as the next session focus and tailor the handoff accordingly.

Use helper metadata such as `cwd`, `repo`, `repo_url`, `branch`, and `worktree_status` only when useful. Prioritize facts, files, decisions, verification, risks, and next steps that help the next agent continue.

Do not duplicate content already captured in other artifacts such as PRDs, plans, ADRs, issues, commits, or diffs. Reference them by path or URL instead.

Consider what would be useful for the next agent based on `My request`. Questions that may be relevant:

- What was just done, implemented, changed, reviewed, or diagnosed?
- What instructions are still relevant, such as following existing codebase patterns or user preferences?
- What files or directories were identified as important, actively edited, or likely next touch points?
- Was there a plan, spec, design decision, or acceptance criterion that should carry forward?
- What libraries, APIs, commands, patterns, constraints, or conventions matter for continuing?
- What technical details were discovered, including edge cases, failure modes, data shapes, or integration behavior?
- What caveats, limitations, unresolved questions, or verification gaps remain?
- What should the next agent do first for the requested goal?

Extract only what matters for the request. Focus on capabilities and behavior, not file-by-file changes. Avoid excessive implementation details unless critical.

Format: plain-text bullets, no markdown headers, no code fences. Include suggested skills only when the next agent should invoke them. Use workspace-relative paths only. List at most 10 relevant files or directories, most important first. Never invent files. Never include API keys, secrets, tokens, cookies, `.env` contents, full transcripts, large raw logs, or sensitive private data.

My request:
<next goal / text after the handoff request, or the inferred open follow-up>

### 3. Assemble, write, and print

If the current thread was identified, assemble:

```text
Continuing work from Jieli thread <THREAD_ID>.
When you lack specific information, use the jieli-read skill to read the thread.

Relevant files: <path1>, <path2>, <path3>
Suggested skills: <skill1>, <skill2>

<goal-filtered relevant information bullets>

My request: <next goal / text after the handoff request, or the inferred open follow-up>
```

If the thread was not resolved, start with:

```text
You are continuing work from a previous Codex session.
```

Write the full handoff prompt to a temp file, but do not print it in your reply. Save to Node's `os.tmpdir()` as `handoff-<THREAD_ID>.md` when a thread id exists; otherwise use a short filename-safe slug from the next goal, falling back to `handoff.md`. Use a safe writer such as Node so prompt content cannot break shell quoting.

Reply only with the saved path, whether a thread id was included, the relevant files count/list, and the next goal. Print the full handoff only if the user explicitly asks.

## Output

- Save the full ready-to-paste handoff prompt to the temp path.
- Reply with a brief summary and the saved path, not the full handoff content.

## Notes & Safety

- The current turn is uploaded to Jieli by the sync hooks after this turn ends, so reading the thread may cover history only up to the previous sync. The handoff prompt itself must carry the key current context.
- A wrong thread is worse than no thread. If the helper cannot identify the current thread, do not guess.
