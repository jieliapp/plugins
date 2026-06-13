import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  mkdirSync,
  readdirSync,
  readFileSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import * as runtime from "../scripts/jieli_node.mjs";
import {
  close,
  createMockJieliServer,
  decodeHandoffContext,
  listen,
  makeTempDir,
  runNode,
  withEnv,
  writeJsonl,
} from "../../test_helpers/runtime.mjs";

const pluginRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const repoRoot = dirname(dirname(pluginRoot));

test("runtime entrypoints do not invoke Python", () => {
  const files = [
    "hooks/hooks.json",
    ...readdirSync(join(pluginRoot, "bin")).map((name) => `bin/${name}`),
    "skills/jieli-read/SKILL.md",
    "skills/jieli-find/SKILL.md",
  ];

  for (const file of files) {
    const content = readFileSync(join(pluginRoot, file), "utf8");
    assert.doesNotMatch(content, /\bpython(?:3)?\b|\bpy -3\b/, file);
  }
});

test("redaction covers API keys, database credentials, vendor tokens, URLs, JSON keys, and invisible tag chars", () => {
  const fakeJieliKey = "jieli_uTN9dHsMCoOgMPBLRnQq_1JkfimaKU2ZfP";
  const text = [
    "standalone key sk-ant-abcdefghijklmnopqrstuvwxyz123456",
    'REDIS_PASSWORD="redis-secret"',
    "REDISCLI_AUTH=redis-cli-secret",
    "password = spaced-secret",
    "OPENAI_API_KEY = pk-secret",
    "redis-cli -a redis-flag-secret ping",
    "redis-cli --pass redis-pass-secret ping",
    "redis-server --requirepass redis-server-secret",
    "redis://:redis-url-secret@localhost:6379/0",
    "mongodb://user:mongo-secret@localhost:27017/db",
    "postgresql://user:pg-secret@localhost:5432/db",
    "mysql://user:mysql-secret@localhost:3306/db",
    "https://user:pass@example.com/path?access_token=abc&ok=1#frag",
    "wss://default:pk_xxx@example.com/actors/gateway?rvt-token=secret&sessionid=sid",
    "connect https://user:secret@example.com:notaport/path?token=query-secret",
    "github_pat_" + "a".repeat(22) + "_" + "b".repeat(59),
    "gho_" + "a".repeat(36),
    "ghu_" + "b".repeat(36),
    "ghr_" + "c".repeat(76),
    "glpat-" + "d".repeat(20),
    "hf_" + "e".repeat(20),
    "AIza" + "f".repeat(35),
    "xoxb-" + "1".repeat(12),
    "https://hooks.slack.com/services/T000/B000/" + "g".repeat(24),
    "sk_live_" + "h".repeat(20),
    "pypi-" + "i".repeat(20),
    "lin_api_" + "j".repeat(20),
    "ASIA" + "K".repeat(16),
    "AGE-SECRET-KEY-1" + "L".repeat(20),
    "eyJ" + "a".repeat(12) + ".eyJ" + "b".repeat(12) + "." + "c".repeat(12),
    fakeJieliKey,
  ].join("\n");

  const redacted = runtime.redactText(`${text}\nsk-\u{E0000}ant-secret-value`);

  for (const secret of [
    "sk-ant-abcdefghijklmnopqrstuvwxyz123456",
    "redis-secret",
    "redis-cli-secret",
    "spaced-secret",
    "pk-secret",
    "redis-flag-secret",
    "redis-pass-secret",
    "redis-server-secret",
    "redis-url-secret",
    "mongo-secret",
    "pg-secret",
    "mysql-secret",
    "user:pass",
    "user:secret",
    "query-secret",
    "pk_xxx",
    "#frag",
    fakeJieliKey,
  ]) {
    assert.doesNotMatch(redacted, new RegExp(secret.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")), secret);
  }
  for (const marker of [
    "[REDACTED:openai-api-key]",
    "[REDACTED:redis-password]",
    "[REDACTED:rediscli-auth]",
    "[REDACTED:redis-cli-password]",
    "[REDACTED:url-userinfo]@example.com:notaport",
    "token=[REDACTED:url-query-token]",
    "[REDACTED:github-token]",
    "[REDACTED:gitlab-token]",
    "[REDACTED:huggingface-token]",
    "[REDACTED:google-api-key]",
    "[REDACTED:slack-token]",
    "[REDACTED:slack-webhook]",
    "[REDACTED:stripe-secret-key]",
    "[REDACTED:pypi-token]",
    "[REDACTED:linear-token]",
    "[REDACTED:aws-access-key]",
    "[REDACTED:age-secret-key]",
    "[REDACTED:jwt-token]",
    "[REDACTED:jieli-api-key]",
  ]) {
    assert.match(redacted, new RegExp(marker.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")), marker);
  }

  const json = runtime.redactJson({
    config: { password: "plain-secret", redis_url: "redis://:redis-secret@localhost:6379/0", nested: [{ wsToken: "ws-secret" }] },
    image: { type: "base64", data: "sk-ant-not-redacted-in-image-data" },
    inline: { isImage: true, content: "sk-ant-image-content", url: "https://x.test/?token=url-secret" },
  });
  assert.equal(json.config.password, "[REDACTED:password]");
  assert.equal(json.config.redis_url, "[REDACTED:redis-url]");
  assert.equal(json.config.nested[0].wsToken, "[REDACTED:wstoken]");
  assert.equal(json.image.data, "sk-ant-not-redacted-in-image-data");
  assert.equal(json.inline.content, "sk-ant-image-content");
  assert.equal(json.inline.url, "https://x.test/?token=[REDACTED:url-query-token]");

  for (const path of [".env", ".env.local", "env.production", "foo.secret", "foo.credentials", ".envrc", "nested/.env"]) {
    assert.equal(runtime.isSecretFilePath(path), true, path);
  }
  for (const path of [".env.example", ".env.sample", "README.md", "env.example"]) {
    assert.equal(runtime.isSecretFilePath(path), false, path);
  }
});

test("builds Claude payload from JSONL while redacting secrets and preserving tool result metadata", async () => {
  const tmp = makeTempDir();
  const transcript = join(tmp, "session.jsonl");
  writeJsonl(transcript, [
    { type: "queue-operation", sessionId: "cc-1" },
    {
      type: "user",
      uuid: "u-1",
      sessionId: "cc-1",
      cwd: "/Users/alice/work/jieli",
      gitBranch: "plugin/sync",
      timestamp: "2026-06-06T09:00:00.000Z",
      message: { role: "user", content: [{ type: "text", text: "use ANTHROPIC_API_KEY=sk-ant-secret-value" }] },
    },
    {
      type: "assistant",
      uuid: "a-1",
      sessionId: "cc-1",
      timestamp: "2026-06-06T09:00:03.000Z",
      message: {
        role: "assistant",
        model: "claude-opus-4-1",
        usage: { totalInputTokens: 25, maxInputTokens: 100 },
        content: [{ type: "text", text: "done with Authorization: Bearer abc.def.ghi" }],
      },
    },
    {
      type: "assistant",
      uuid: "tool-use-1",
      sessionId: "cc-1",
      message: { role: "assistant", content: [{ type: "tool_use", id: "tool-1", name: "Bash", input: { command: "npm test" } }] },
    },
    {
      type: "user",
      uuid: "tool-result-1",
      sessionId: "cc-1",
      message: { role: "user", content: [{ type: "tool_result", tool_use_id: "tool-1", content: "Authorization: Bearer tool.secret", is_error: true }] },
    },
  ]);

  const payload = await runtime.buildPayloadFromHook(
    { session_id: "cc-1", transcript_path: transcript, cwd: "/Users/alice/work/jieli" },
    "https://jieli.example.test",
  );

  assert.equal(payload.provider, "claude_code");
  assert.equal(payload.repo, "");
  assert.equal(payload.repo_url, "");
  assert.equal(payload.branch, "plugin/sync");
  assert.equal(payload.source_url, "https://jieli.example.test/threads/T-cc-1");
  assert.equal(payload.thread.id, "T-cc-1");
  assert.equal(payload.thread.model, "claude-opus-4-1");
  assert.deepEqual(payload.thread.messages.map((message) => message.role), ["user", "assistant", "assistant", "tool"]);
  assert.deepEqual(payload.thread.messages[1].usage, { totalInputTokens: 25, maxInputTokens: 100 });
  const toolResult = payload.thread.messages[3].content[0];
  assert.equal(toolResult.tool_use_id, "tool-1");
  assert.equal(toolResult.run.status, "error");
  assert.equal(toolResult.run.result.output, "Authorization: Bearer [REDACTED:authorization-bearer]");
  const raw = JSON.stringify(payload);
  assert.match(raw, /\[REDACTED:/);
  assert.doesNotMatch(raw, /sk-ant-secret-value|abc\.def\.ghi|tool\.secret/);
});

test("builds repo metadata from git remote without inferring repo from local folders", async () => {
  const tmp = makeTempDir();
  const repo = join(tmp, "repo");
  mkdirSync(repo);
  spawnSync("git", ["init"], { cwd: repo, stdio: "ignore" });
  spawnSync("git", ["remote", "add", "origin", "git@home.pika12.com:guoyb/jieli.git"], { cwd: repo, stdio: "ignore" });
  const transcript = join(tmp, "session.jsonl");
  writeJsonl(transcript, [
    { type: "user", uuid: "u-remote", sessionId: "cc-remote", cwd: repo, gitBranch: "plugin/sync", message: { role: "user", content: "sync this repo" } },
  ]);

  const payload = await runtime.buildPayloadFromHook({ session_id: "cc-remote", transcript_path: transcript }, "https://jieli.example.test");
  assert.equal(payload.repo, "");
  assert.equal(payload.repo_url, "git@home.pika12.com:guoyb/jieli.git");

  const local = join(tmp, "github", "plugins");
  mkdirSync(local, { recursive: true });
  const localTranscript = join(tmp, "local.jsonl");
  writeJsonl(localTranscript, [
    { type: "user", uuid: "u-local", sessionId: "cc-local", cwd: local, gitBranch: "plugin/sync", message: { role: "user", content: "sync local folder" } },
  ]);
  const localPayload = await runtime.buildPayloadFromHook({ session_id: "cc-local", transcript_path: localTranscript }, "https://jieli.example.test");
  assert.equal(localPayload.repo, "");
  assert.equal(localPayload.repo_url, "");
});

test("normalizes Claude images and attachment cache behavior", async () => {
  const tmp = makeTempDir();
  const imagePath = join(tmp, "1.png");
  writeFileSync(imagePath, Buffer.from([0x89, 0x50, 0x4e, 0x47]));
  const transcript = join(tmp, "session.jsonl");
  writeJsonl(transcript, [
    {
      type: "user",
      uuid: "u-image",
      sessionId: "cc-image",
      message: { role: "user", content: [{ type: "text", text: `ok\n[Image: source: ${imagePath}]\n这是什么 logo?` }] },
    },
  ]);

  const uploaded = [];
  const payload = await runtime.buildPayloadFromHook(
    { session_id: "cc-image", transcript_path: transcript, cwd: "/Users/alice/work/jieli" },
    "https://jieli.example.test",
    async (path) => {
      uploaded.push(path);
      return "https://jieli.example.test/attachments/img.png";
    },
  );
  assert.deepEqual(uploaded, [imagePath]);
  assert.deepEqual(payload.thread.messages[0].content, [
    { type: "text", text: "ok" },
    { type: "image", source: { url: "https://jieli.example.test/attachments/img.png", type: "image/png" } },
    { type: "text", text: "这是什么 logo?" },
  ]);

  await withEnv({ HOME: tmp }, async () => {
    const calls = [];
    const first = await runtime.uploadAttachmentCached(imagePath, "https://jieli.example.test/", "secret", async (path) => {
      calls.push(path);
      return "https://jieli.example.test/attachments/cached.png";
    });
    const second = await runtime.uploadAttachmentCached(imagePath, "https://jieli.example.test/", "secret", async (path) => {
      calls.push(path);
      return "unused";
    });
    assert.equal(first, "https://jieli.example.test/attachments/cached.png");
    assert.equal(second, "https://jieli.example.test/attachments/cached.png");
    assert.deepEqual(calls, [imagePath]);
  });
});

test("handles Claude model aliases, local command noise, loaded skills, and split assistant messages", async () => {
  const tmp = makeTempDir();
  const transcript = join(tmp, "session.jsonl");
  writeJsonl(transcript, [
    { type: "user", uuid: "u-caveat", sessionId: "skill-session", message: { role: "user", content: "<local-command-caveat>Caveat</local-command-caveat>" } },
    { type: "user", uuid: "u-command", sessionId: "skill-session", message: { role: "user", content: "<command-message>caveman</command-message>\n<command-name>/caveman</command-name>" } },
    {
      type: "user",
      uuid: "u-skill-body",
      sessionId: "skill-session",
      message: { role: "user", content: [{ type: "text", text: "Base directory for this skill: /Users/alice/.claude/skills/caveman\n\nSkill body." }] },
    },
    {
      type: "assistant",
      uuid: "a-thinking",
      sessionId: "skill-session",
      message: { id: "resp-1", role: "assistant", model: "gpt-5.4-mini-2026-03-17", content: [{ type: "thinking", thinking: "Switching style.", signature: "large-signature" }] },
    },
    {
      type: "assistant",
      uuid: "a-text",
      sessionId: "skill-session",
      message: { id: "resp-1", role: "assistant", model: "gpt-5.4-mini-2026-03-17", content: [{ type: "text", text: "好。已切 **caveman**。后面都短说。" }] },
    },
  ]);

  const payload = await withEnv({ ANTHROPIC_DEFAULT_HAIKU_MODEL: "gpt-5.4-mini" }, () =>
    runtime.buildPayloadFromHook({ session_id: "skill-session", transcript_path: transcript, cwd: "/Users/alice/work/jieli" }, "https://jieli.example.test"),
  );
  assert.equal(payload.thread.model, "gpt-5.4-mini");
  assert.equal(payload.thread.resolved_model, "gpt-5.4-mini-2026-03-17");
  assert.deepEqual(payload.thread.messages.map((message) => message.role), ["user", "assistant"]);
  assert.equal(payload.thread.messages[0].content, "/caveman");
  assert.deepEqual(payload.thread.messages[1].content, [
    { type: "thinking", thinking: "Switching style." },
    { type: "text", text: "好。已切 **caveman**。后面都短说。" },
  ]);
  const raw = JSON.stringify(payload);
  assert.doesNotMatch(raw, /Base directory|large-signature|<local-command-caveat>/);
});

test("replaces compaction summaries and renders Claude bash transcript tags", async () => {
  const tmp = makeTempDir();
  const fakeKey = `jieli_${"a".repeat(30)}`;
  const prose = "我用 `!rm -rf x` 执行了：<bash-input>rm -rf x</bash-input><bash-stdout>(Bash completed with no output)</bash-stdout> 你看下";
  const transcript = join(tmp, "session.jsonl");
  writeJsonl(transcript, [
    { type: "user", uuid: "u-real", sessionId: "cc-bash", cwd: "/Users/alice/work/jieli", message: { role: "user", content: "原始第一条消息" } },
    { type: "user", uuid: "u-compact", sessionId: "cc-bash", isCompactSummary: true, isVisibleInTranscriptOnly: true, message: { role: "user", content: "This session is being continued. " + "x".repeat(5000) } },
    { type: "user", uuid: "u-bash-in", sessionId: "cc-bash", cwd: "/Users/alice/work/jieli", message: { role: "user", content: "<bash-input>echo $JIELI_API_KEY</bash-input>" } },
    { type: "user", uuid: "u-bash-out", sessionId: "cc-bash", message: { role: "user", content: `<bash-stdout>${fakeKey}</bash-stdout><bash-stderr></bash-stderr>` } },
    { type: "user", uuid: "u-rm", sessionId: "cc-bash", message: { role: "user", content: "<bash-input>rm -rf /tmp/x</bash-input><bash-stdout>(Bash completed with no output)</bash-stdout><bash-stderr></bash-stderr>" } },
    { type: "user", uuid: "u-prose", sessionId: "cc-bash", cwd: "/Users/alice/work/jieli", message: { role: "user", content: prose } },
  ]);

  const payload = await runtime.buildPayloadFromHook({ session_id: "cc-bash", transcript_path: transcript }, "https://jieli.example.test");
  assert.equal(payload.thread.title, "原始第一条消息");
  assert.equal(payload.thread.messages[1].content, runtime.COMPACTION_PLACEHOLDER);
  assert.equal(payload.thread.messages[2].content, "```console\n$ echo $JIELI_API_KEY\n[REDACTED:jieli-api-key]\n```");
  assert.equal(payload.thread.messages[3].content, "```console\n$ rm -rf /tmp/x\n# (no output)\n```");
  assert.equal(payload.thread.messages[4].content, prose);
  const raw = JSON.stringify(payload);
  assert.doesNotMatch(raw, new RegExp(fakeKey));
  assert.doesNotMatch(raw, /x{5000}/);
});

test("configuration, upload, lock, and transcript flush helpers match hook behavior", async () => {
  const tmp = makeTempDir();
  await withEnv({ HOME: tmp, JIELI_API_KEY: undefined, CLAUDE_PLUGIN_OPTION_API_KEY: "plugin-key", JIELI_BASE_URL: undefined }, async () => {
    assert.deepEqual(runtime.missingConfigVars(), []);
    assert.equal(runtime.requiredEnv("JIELI_API_KEY", "CLAUDE_PLUGIN_OPTION_API_KEY"), "plugin-key");
    assert.equal(runtime.optionalEnv("JIELI_BASE_URL", "CLAUDE_PLUGIN_OPTION_BASE_URL"), "");
  });

  await withEnv({ HOME: tmp, JIELI_API_KEY: undefined, CLAUDE_PLUGIN_OPTION_API_KEY: undefined }, async () => {
    const response = runtime.buildMissingConfigHookResponse("stop", runtime.missingConfigVars());
    assert.equal(response.continue, true);
    assert.match(response.systemMessage, /create an API key/);
    assert.doesNotMatch(response.systemMessage, /self-hosted/);
    assert.deepEqual(runtime.buildMissingConfigHookResponse("userpromptsubmit", ["JIELI_API_KEY"]), {});
  });

  const { server, state } = createMockJieliServer();
  const baseUrl = await listen(server);
  state.baseUrl = baseUrl;
  try {
    await runtime.uploadPayload({ provider: "claude_code" }, baseUrl, "secret");
    assert.equal(state.uploads[0].path, "/plugin/threads/upload");
  } finally {
    await close(server);
  }

  const bad = createMockJieliServer({ uploadStatus: 400, uploadResponse: { error: "unsupported provider", api_key: "jieli_" + "a".repeat(30) } });
  const badBase = await listen(bad.server);
  try {
    await assert.rejects(() => runtime.uploadPayload({ provider: "claude_code" }, badBase, "secret"), /unsupported provider/);
    await assert.rejects(() => runtime.uploadPayload({ provider: "claude_code" }, badBase, "secret"), (error) => {
      assert.doesNotMatch(error.message, /jieli_a{30}/);
      assert.match(error.message, /\[REDACTED:jieli-api-key\]/);
      return true;
    });
  } finally {
    await close(bad.server);
  }

  await withEnv({ HOME: tmp }, async () => {
    const lockA = runtime.acquireSyncLock("sess-A");
    assert.equal(lockA.acquired, true);
    const lockB = runtime.acquireSyncLock("sess-B");
    assert.equal(lockB.acquired, true);
    const lockA2 = runtime.acquireSyncLock("sess-A");
    assert.equal(lockA2.acquired, false);
    runtime.releaseSyncLock(lockB);
    runtime.releaseSyncLock(lockA);
  });

  const flushPath = join(tmp, "flush.jsonl");
  writeFileSync(flushPath, "first\n", "utf8");
  setTimeout(() => writeFileSync(flushPath, "first\nsecond\n", "utf8"), 10);
  await runtime.waitForTranscriptFlush(flushPath);
  assert.equal(readFileSync(flushPath, "utf8"), "first\nsecond\n");
});

test("sync CLI reports missing config and skips missing transcripts without failing", async () => {
  const home = makeTempDir();
  const missingConfig = spawnSync("node", [join(pluginRoot, "scripts", "sync.mjs"), "--trigger", "stop", "--jieli-hook"], {
    encoding: "utf8",
    env: { HOME: home, PATH: process.env.PATH },
  });
  assert.equal(missingConfig.status, 0);
  assert.match(missingConfig.stdout, /Jieli Claude Code Sync is not configured/);

  const transcriptHome = makeTempDir();
  const missingTranscript = await runNode([join(pluginRoot, "scripts", "sync.mjs"), "--trigger", "sessionstart", "--jieli-hook"], {
    input: JSON.stringify({ transcript_path: join(transcriptHome, "projects", "missing.jsonl"), session_id: "cc-not-flushed", cwd: "/Users/alice/work/jieli" }),
    env: { HOME: transcriptHome, PATH: process.env.PATH, JIELI_API_KEY: "secret" },
  });
  assert.equal(missingTranscript.status, 0);
  assert.throws(() => statSync(join(transcriptHome, ".jieli", "hooks.log")));
});

test("read-thread and find-threads helpers validate ids, shape requests, truncate output, and format markdown", async () => {
  assert.throws(() => runtime.validateThreadId("/threads/T-abc123"), /provider thread id/);
  assert.throws(() => runtime.validateThreadId("https://jieli.example.test/threads/T-abc123"), /provider thread id/);
  assert.throws(() => runtime.validateThreadId("T-abc123.md"), /without .md/);
  assert.equal(runtime.validateThreadId("T-abc123"), "T-abc123");

  const limited = runtime.limitOutput("line 1\nline 2\nline 3\nline 4\n", 2, 3, 9);
  assert.match(limited, /^line 2\nli/);
  assert.match(limited, /\[Content truncated at 9 chars/);
  assert.doesNotMatch(limited, /line 1|line 4/);

  const { server, state } = createMockJieliServer({
    threadBody: "thread body",
    searchResponse: { data: { threads: [{ provider_thread_id: "T-1", title: "Fix checkout", provider: "codex", repo: "shop/app", branch: "bugfix", updated_at: "2026-06-13T10:00:00Z", message_count: 7, preview: "checkout failed on coupon path" }] } },
  });
  const baseUrl = await listen(server);
  try {
    assert.equal(await runtime.fetchThreadExport("T-abc123", `${baseUrl}/`, "secret"), "thread body");
    assert.equal(state.threadReads[0].url.pathname, "/threads/T-abc123.md");
    assert.equal(state.threadReads[0].headers.authorization, "Bearer secret");
    await runtime.fetchThreadExport("T-abc123", `${baseUrl}/`, "secret", "md", true);
    assert.equal(state.threadReads[1].url.search, "?truncate_tool_results=1");

    const search = await runtime.fetchThreads("payment bug", `${baseUrl}/`, "secret", {});
    assert.equal(search.data.threads.length, 1);
    assert.equal(state.searches[0].url.searchParams.get("search"), "payment bug");
    assert.equal(state.searches[0].url.searchParams.get("page_size"), "10");
    assert.equal(state.searches[0].url.searchParams.has("provider"), false);
    await runtime.fetchThreads("payment bug", baseUrl, "secret", { provider: "codex" });
    assert.equal(state.searches[1].url.searchParams.get("provider"), "codex");

    const markdown = runtime.formatThreadsMarkdown(search, "https://jieli.example.test");
    assert.match(markdown, /1\. Fix checkout/);
    assert.match(markdown, /thread_id: T-1/);
    assert.match(markdown, /repo: shop\/app@bugfix/);
    assert.match(markdown, /read_url: https:\/\/jieli\.example\.test\/threads\/T-1/);
  } finally {
    await close(server);
  }
});

test("handoff info and commit trailer helpers inject Node-based context and trailers", async () => {
  const tmp = makeTempDir();
  const context = { session_id: "cc-1", transcript_path: join(tmp, "session.jsonl"), cwd: tmp };
  const encoded = Buffer.from(JSON.stringify(context), "utf8").toString("base64");
  const info = await withEnv({ JIELI_HANDOFF_CONTEXT_B64: encoded, JIELI_BASE_URL: "https://jieli.example.test" }, () => runtime.buildHandoffInfo());
  assert.equal(info.confidence, "high");
  assert.equal(info.provider, "claude_code");
  assert.equal(info.thread_id, "T-cc-1");
  assert.equal(info.url, "https://jieli.example.test/threads/T-cc-1");

  const handoff = runtime.buildHookResponse({ session_id: "cc-handoff", transcript_path: "/tmp/claude-session.jsonl", cwd: "/repo", tool_name: "Bash", tool_input: { command: "jieli-handoff-info" } });
  const handoffCommand = handoff.hookSpecificOutput.updatedInput.command;
  assert.match(handoffCommand, /JIELI_HANDOFF_CONTEXT_B64=/);
  assert.match(handoffCommand, /node .*jieli_node\.mjs handoff-info/);
  assert.deepEqual(decodeHandoffContext(handoffCommand), { session_id: "cc-handoff", transcript_path: "/tmp/claude-session.jsonl", cwd: "/repo" });
  assert.deepEqual(runtime.buildHookResponse({ session_id: "cc-handoff", tool_name: "Bash", tool_input: { command: "jieli-handoff-info | cat" } }), {});

  await withEnv({ HOME: tmp }, async () => {
    mkdirSync(join(tmp, ".jieli"), { recursive: true });
    writeFileSync(join(tmp, ".jieli", "claude-sessions.json"), JSON.stringify({ "cc-chain": { provider_thread_id: "cc-chain", base_url: "https://jieli.example.test/" } }), "utf8");
    const command = 'git status --short && git add file.txt && git commit -m "ship" -- docs/test.md';
    const response = runtime.buildHookResponse({ session_id: "cc-chain", tool_name: "Bash", tool_input: { command } });
    assert.equal(
      response.hookSpecificOutput.updatedInput.command,
      'git status --short && git add file.txt && git commit -m "ship" --trailer "Jieli-Thread: https://jieli.example.test/threads/T-cc-chain" -- docs/test.md',
    );
    assert.deepEqual(runtime.buildHookResponse({ session_id: "cc-chain", tool_name: "Bash", tool_input: { command: 'git commit -m "x" | git status' } }), {});
  });
});

test("plugin wrappers, skills, docs, manifests, and hooks describe the split Jieli tools", () => {
  for (const [wrapperName, scriptName] of Object.entries({
    "jieli-handoff-info.cmd": "handoff_info.mjs",
    "jieli-read-thread.cmd": "read_thread.mjs",
    "jieli-find-threads.cmd": "find_threads.mjs",
  })) {
    const content = readFileSync(join(pluginRoot, "bin", wrapperName), "utf8");
    assert.match(content, /set "PLUGIN_ROOT=%BIN_DIR%\.\."/);
    assert.match(content, new RegExp(`scripts\\\\${scriptName}`));
    assert.match(content, /node /);
    assert.doesNotMatch(content, /py -3/);
  }

  for (const wrapper of ["jieli-read-thread", "jieli-find-threads", "jieli-handoff-info"]) {
    const result = spawnSync(join(pluginRoot, "bin", wrapper), ["--help"], { env: {}, encoding: "utf8", timeout: 5000 });
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /Jieli|usage:/);
  }

  const readSkill = readFileSync(join(pluginRoot, "skills", "jieli-read", "SKILL.md"), "utf8");
  const findSkill = readFileSync(join(pluginRoot, "skills", "jieli-find", "SKILL.md"), "utf8");
  const handoffSkill = readFileSync(join(pluginRoot, "skills", "handoff", "SKILL.md"), "utf8");
  assert.match(readSkill, /name: jieli-read/);
  assert.match(readSkill, /jieli-read-thread/);
  assert.match(findSkill, /name: jieli-find/);
  assert.match(findSkill, /Do not pass --provider/);
  assert.match(handoffSkill, /`jieli-read` skill/);
  assert.match(handoffSkill, /OUT="\/tmp\/handoff-\$THREAD_ID\.md"/);

  const docs = [
    readFileSync(join(repoRoot, "README.md"), "utf8"),
    readFileSync(join(pluginRoot, ".claude-plugin", "plugin.json"), "utf8"),
  ].join("\n");
  assert.match(docs, /`jieli-read`/);
  assert.match(docs, /`jieli-find`/);
  assert.doesNotMatch(docs, /https:\/\/your-jieli\.example\.com|self-hosted|Provides the `jieli` skill/);

  const manifest = JSON.parse(readFileSync(join(pluginRoot, ".claude-plugin", "plugin.json"), "utf8"));
  assert.equal(manifest.name, "jieli");
  assert.equal(manifest.displayName, "Jieli");
  assert.ok(manifest.userConfig);
  assert.equal("hooks" in manifest, false);

  const hooks = JSON.parse(readFileSync(join(pluginRoot, "hooks", "hooks.json"), "utf8"));
  assert.equal("UserPromptSubmit" in hooks.hooks, false);
  const commands = Object.values(hooks.hooks).flatMap((configs) => configs.flatMap((config) => (config.hooks || []).map((hook) => hook.command)));
  assert.ok(commands.length > 0);
  for (const command of commands) {
    assert.match(command, /node "\$\{CLAUDE_PLUGIN_ROOT\}\/scripts\/.*\.mjs"/);
    assert.doesNotMatch(command, /python3/);
  }
});
