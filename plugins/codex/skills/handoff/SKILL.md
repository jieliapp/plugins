---
name: handoff
description: "Create a paste-ready handoff/compress prompt for continuing the current Codex session in a fresh agent. Use when the user says handoff, hand over, compress, compact, summarize context, continue in a new session, or pass work to another agent."
---

# Handoff

Compress the current session into a self-contained handoff prompt that a fresh agent can start from. When the current Jieli thread can be identified, embed its thread id and URL so the next agent can use the `jieli` skill to read the full transcript if the summary is not enough.

## When to Use

Use this skill when:

- The user asks to hand off, hand over, or pass the work to another agent or a new session.
- The user asks to compress, compact, or summarize the context to keep working past the context window.
- The user wants a prompt they can paste into a fresh Codex (or other) session to continue this work.

Do not use this skill for ordinary status updates or to read/search other threads. Reading/searching threads is the `jieli` skill.

## Inputs

- Optional next goal: what the next agent should do first. If the user did not give one, derive it from the most recent request and any open follow-ups in this conversation.
- Current session metadata from the `jieli-handoff-info` helper. The helper receives hook context from the `PreToolUse` hook and returns JSON. No API key is needed; the helper only builds a thread URL and never calls the Jieli API.

## Procedure

### 1. Resolve current session metadata

Run the helper exactly as a plain command so the `PreToolUse` hook can inject the current `session_id`, `transcript_path`, and `cwd`:

```bash
jieli-handoff-info
```

Expected JSON shape:

```json
{
  "confidence": "high",
  "provider": "codex",
  "session_id": "...",
  "thread_id": "T-...",
  "url": "https://jieli.app/threads/T-...",
  "cwd": "...",
  "repo": "...",
  "repo_url": "...",
  "branch": "...",
  "worktree_status": "clean"
}
```

Rules:

- Treat `confidence: "high"` with a non-empty `thread_id` and `url` as the only case where the Jieli read-thread line may be included.
- If `confidence` is not `"high"`, or either `thread_id` or `url` is empty, do not guess from the newest rollout. Still produce the handoff prompt, but omit the Jieli thread/read-thread lines and mention that the current thread could not be identified.
- If the helper fails or prints invalid JSON, continue without a thread id and state that session metadata could not be resolved.

### 2. Compose the handoff context

You already hold the full conversation, so write the context yourself; do not call a model and do not read the full transcript. Treat any text after the handoff/compress request as `My request` and use it as the relevance filter. Prioritize facts, files, decisions, verification, and next steps that help that request; keep only global constraints and safety notes from unrelated work.

Use helper metadata such as `cwd`, `repo`, `repo_url`, `branch`, and `worktree_status` as source context, but do not force those fields into the final handoff unless they are useful for the request.

Consider what would be useful for the next agent based on `My request`. Questions that may be relevant:

- What was just done, implemented, changed, reviewed, or diagnosed?
- What instructions are still relevant, such as following existing codebase patterns or user preferences?
- What files or directories were identified as important, actively edited, or likely next touch points?
- Was there a plan, spec, design decision, or acceptance criterion that should carry forward?
- What libraries, APIs, commands, patterns, constraints, or conventions matter for continuing?
- What technical details were discovered, including edge cases, failure modes, data shapes, or integration behavior?
- What caveats, limitations, unresolved questions, or verification gaps remain?
- What should the next agent do first for the requested goal?

Extract what matters for the specific request below. Don't answer questions that aren't relevant. Pick an appropriate length based on the complexity of the request.

Focus on capabilities and behavior, not file-by-file changes. Avoid excessive implementation details (variable names, storage keys, constants) unless critical.

Format: Plain text with bullets. No markdown headers, no bold/italic, no code fences. Use workspace-relative paths for files.

Relevant files rules:

- Workspace-relative paths only. Maximum 10. Put the most important files first.
- Directories are allowed when several files under them matter.
- Do not invent files or use absolute paths.

Never include API keys, secrets, tokens, cookies, `.env` contents, full transcripts, large raw logs, or sensitive private data. Say that sensitive values were omitted when relevant.

My request:
<next goal / text after the handoff request, or the inferred open follow-up>

### 3. Assemble, write, and print

If the thread was resolved with high confidence, assemble the handoff prompt in this shape. The first two lines are fixed; the body is the goal-filtered plain-text bullet context from step 2. Include helper metadata such as repo/cwd/branch/status only when useful for the request.

```text
Continuing work from Jieli thread <THREAD_ID>.
When you lack specific information, use the jieli skill to read the thread.

Relevant files: <path1> <path2> <path3> ...

<goal-filtered relevant information bullets>

My request: <next goal / text after the handoff request, or the inferred open follow-up>
```

If the thread was not resolved, omit the first two lines and start with:

```text
You are continuing work from a previous Codex session.
Current Jieli thread could not be identified automatically; ask the user for the thread id or Jieli URL if full transcript access is needed.
```

Write the prompt to a temp file and print the same prompt in your reply:

- With a thread id: `OUT="${TMPDIR:-/tmp}/handoff-$THREAD_ID.md"`
- Without a thread id: `OUT="${TMPDIR:-/tmp}/handoff-unknown.md"`

Use a safe writer, for example Python, so prompt content cannot break a shell here-doc:

```bash
python3 - "$OUT" <<'PY'
import sys
from pathlib import Path
Path(sys.argv[1]).write_text("""<assembled handoff prompt>
""", encoding="utf-8")
PY
echo "Wrote handoff to $OUT"
```

Then print the same handoff prompt in a fenced block in your reply so the user can copy it directly, and tell them the saved file path.

## Output

- A ready-to-paste handoff prompt printed in the reply.
- The same prompt saved to `$OUT`.
- If a high-confidence thread id is available, the next agent can use the `jieli` skill to read `<THREAD_ID>` for the full transcript.

## Notes & Safety

- The current turn is uploaded to Jieli by the sync hooks after this turn ends, so reading the thread may cover history only up to the previous sync. The handoff prompt itself must carry the key current context.
- A wrong thread is worse than no thread. If the helper cannot identify the current thread with high confidence, do not guess.
- Never include API keys, secrets, tokens, cookies, `.env` contents, full transcripts, large raw logs, or sensitive private data in the handoff prompt.
