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
  ];

  for (const file of files) {
    const content = readFileSync(join(pluginRoot, file), "utf8");
    assert.doesNotMatch(content, /\bpython(?:3)?\b|\bpy -3\b/, file);
  }
  assert.doesNotMatch(readFileSync(join(pluginRoot, "scripts", "jieli_node.mjs"), "utf8"), /AbortSignal\.timeout/);
});

test("helper command runtime contract is stable across OS shells", async () => {
  const tmp = makeTempDir();
  await withEnv({ HOME: tmp, JIELI_API_KEY: "jieli_test", JIELI_BASE_URL: "https://jieli.example.test/" }, async () => {
    const readCommand = runtime.createCommandRuntime("read-thread", ["T-1", "--format=json", "--max-chars", "200"]);
    assert.equal(readCommand.name, "read-thread");
    assert.equal(readCommand.baseUrl, "https://jieli.example.test");
    assert.equal(readCommand.apiKey, "jieli_test");
    assert.deepEqual(readCommand.opts._, ["T-1"]);
    assert.equal(readCommand.opts.format, "json");
    assert.equal(readCommand.opts.maxChars, "200");

    const findCommand = runtime.createCommandRuntime("find-threads", ["windows paths", "--repo", "jieli/app"]);
    assert.equal(findCommand.name, "find-threads");
    assert.equal(findCommand.baseUrl, "https://jieli.example.test");
    assert.equal(findCommand.apiKey, "jieli_test");
    assert.deepEqual(findCommand.opts._, ["windows paths"]);
    assert.equal(findCommand.opts.repo, "jieli/app");

    const contextB64 = Buffer.from(JSON.stringify({ session_id: "codex-1", transcript_path: "/tmp/session.jsonl", cwd: "/repo" }), "utf8").toString("base64");
    const handoffCommand = runtime.createCommandRuntime("handoff-info", ["--context-b64", contextB64]);
    assert.equal(handoffCommand.name, "handoff-info");
    assert.equal(handoffCommand.baseUrl, "https://jieli.example.test");
    assert.equal(handoffCommand.apiKey, "");
    assert.equal(handoffCommand.handoffContextB64, contextB64);
  });
});

test("shell hook contract normalizes macOS and Windows command inputs", () => {
  const codexMac = runtime.normalizeShellHook({ session_id: "codex-mac", transcript_path: "/tmp/a.jsonl", cwd: "/repo", tool_name: "exec_command", tool_input: { cmd: "jieli-handoff-info" } });
  assert.equal(codexMac.commandKey, "cmd");
  assert.equal(codexMac.command, "jieli-handoff-info");
  assert.deepEqual(runtime.buildUpdatedHookInput(codexMac, "node helper"), { cmd: "node helper" });

  const codexWindows = runtime.normalizeShellHook({ session_id: "codex-win", session_path: "C:\\Users\\Administrator\\.codex\\sessions\\rollout.jsonl", cwd: "C:\\repo", tool_name: "Shell", tool_input: { command: "& 'C:\\Users\\Administrator\\.codex\\plugins\\cache\\jieliapp\\jieli\\bin\\jieli-handoff-info.cmd'" } });
  assert.equal(codexWindows.commandKey, "command");
  assert.match(codexWindows.command, /jieli-handoff-info\.cmd/);
  assert.equal(codexWindows.transcriptPath, "C:\\Users\\Administrator\\.codex\\sessions\\rollout.jsonl");
});

test("redaction covers Codex payload secrets and malformed URL regressions", () => {
  const fakeKey = `jieli_${"a".repeat(30)}`;
  const redacted = runtime.redactText(`connect https://user:secret@example.com:notaport/path?token=query-secret and ${fakeKey}`);
  assert.doesNotMatch(redacted, /user:secret|query-secret|jieli_a{30}/);
  assert.match(redacted, /\[REDACTED:url-userinfo\]@example\.com:notaport/);
  assert.match(redacted, /token=\[REDACTED:url-query-token\]/);
  assert.match(redacted, /\[REDACTED:jieli-api-key\]/);

  const json = runtime.redactJson({ config: { redis_url: "redis://:redis-secret@localhost:6379/0", wsToken: "ws-secret" }, usage: { totalInputTokens: 10 } });
  assert.equal(json.config.redis_url, "[REDACTED:redis-url]");
  assert.equal(json.config.wsToken, "[REDACTED:wstoken]");
  assert.equal(json.usage.totalInputTokens, 10);
});

test("builds Codex payload from JSONL while redacting and skipping private items", async () => {
  const tmp = makeTempDir();
  const transcript = join(tmp, "rollout-2026-06-08T00-00-00-codex-1.jsonl");
  writeJsonl(transcript, [
    {
      type: "session_meta",
      timestamp: "2026-06-08T00:00:00.000Z",
      payload: {
        id: "codex-1",
        cwd: "/Users/alice/work/jieli",
        git: { branch: "plugin/codex" },
        base_instructions: "do not upload this",
      },
    },
    { type: "turn_context", payload: { model: "gpt-5.5", cwd: "/Users/alice/work/jieli", summary: "hidden summary" } },
    { type: "response_item", payload: { type: "message", role: "developer", content: [{ type: "input_text", text: "developer instructions" }] } },
    {
      type: "response_item",
      timestamp: "2026-06-08T00:00:01.000Z",
      payload: { type: "message", role: "user", content: [{ type: "input_text", text: "sync OPENAI_API_KEY=sk-ant-secret-value" }] },
    },
    { type: "response_item", payload: { type: "reasoning", encrypted_content: "encrypted", summary: [{ text: "private reasoning" }] } },
    {
      type: "response_item",
      timestamp: "2026-06-08T00:00:02.000Z",
      payload: { type: "message", role: "assistant", phase: "final", content: [{ type: "output_text", text: "done Authorization: Bearer abc.def.ghi" }] },
    },
    { type: "response_item", payload: { type: "function_call", call_id: "call-1", name: "exec_command", arguments: JSON.stringify({ cmd: "git status", token: "tool-secret" }) } },
    { type: "response_item", payload: { type: "function_call_output", call_id: "call-1", output: "Authorization: Bearer tool.secret" } },
  ]);

  const payload = await runtime.buildPayloadFromHook(
    { session_id: "codex-1", transcript_path: transcript, cwd: "/Users/alice/work/jieli" },
    "https://jieli.example.test",
  );

  assert.equal(payload.provider, "codex");
  assert.equal(payload.repo, "");
  assert.equal(payload.branch, "plugin/codex");
  assert.equal(payload.source_url, "https://jieli.example.test/threads/T-codex-1");
  assert.equal(payload.thread.id, "T-codex-1");
  assert.equal(payload.thread.model, "gpt-5.5");
  assert.equal(payload.thread.title, "sync OPENAI_API_KEY=[REDACTED:openai-api-key]");
  assert.deepEqual(payload.thread.messages.map((message) => message.role), ["user", "assistant", "assistant", "tool"]);
  assert.deepEqual(payload.thread.messages[2].content[0], { type: "tool_use", id: "call-1", name: "shell_command", input: { command: "git status", cwd: "" } });
  assert.equal(payload.thread.messages[3].content[0].content, "");
  assert.equal(payload.thread.messages[3].content[0].run.result.output, "Authorization: Bearer [REDACTED:authorization-bearer]");
  const raw = JSON.stringify(payload);
  assert.match(raw, /\[REDACTED:/);
  assert.doesNotMatch(raw, /sk-ant-secret-value|abc\.def\.ghi|tool\.secret|tool-secret|developer instructions|private reasoning|encrypted/);
});

test("Codex transcript session id wins over hook session id and can be found from CODEX_HOME", async () => {
  const tmp = makeTempDir();
  const transcript = join(tmp, "rollout-2026-06-08T00-00-00-stable-id.jsonl");
  writeJsonl(transcript, [
    { type: "session_meta", timestamp: "2026-06-08T00:00:00.000Z", payload: { id: "stable-transcript-id", cwd: "/Users/alice/work/jieli", git: { branch: "plugin/codex" } } },
    { type: "response_item", timestamp: "2026-06-08T00:00:01.000Z", payload: { type: "message", role: "user", content: [{ type: "input_text", text: "hello" }] } },
  ]);
  const payload = await runtime.buildPayloadFromHook({ session_id: "hook-rotated-id", transcript_path: transcript }, "https://jieli.example.test");
  assert.equal(payload.thread.id, "T-stable-transcript-id");
  assert.equal(payload.source_url, "https://jieli.example.test/threads/T-stable-transcript-id");

  const codexHome = join(tmp, "codex-home");
  const sessionDir = join(codexHome, "sessions", "2026", "06", "08");
  mkdirSync(sessionDir, { recursive: true });
  const named = join(sessionDir, "rollout-2026-06-08T00-00-00-find-me.jsonl");
  writeJsonl(named, [{ type: "session_meta", payload: { id: "find-me" } }]);
  const contentOnly = join(sessionDir, "rollout-2026-06-08T00-00-00-random.jsonl");
  writeJsonl(contentOnly, [{ type: "session_meta", payload: { id: "content-only" } }]);
  await withEnv({ CODEX_HOME: codexHome }, async () => {
    assert.equal(runtime.findSessionTranscript("find-me"), named);
    assert.equal(runtime.findSessionTranscript("content-only"), contentOnly);
  });
});

test("normalizes Codex apply_patch, exec_command, and nonzero tool exits", async () => {
  const tmp = makeTempDir();
  const patchText = "*** Begin Patch\n*** Update File: route_test.go\n@@\n-old\n+new\n*** End Patch\n";
  const transcript = join(tmp, "session.jsonl");
  writeJsonl(transcript, [
    { type: "session_meta", payload: { id: "codex-tools", cwd: "/Users/alice/work/jieli" } },
    { type: "response_item", payload: { type: "message", role: "user", content: [{ type: "input_text", text: "fix route test" }] } },
    { type: "response_item", payload: { type: "custom_tool_call", status: "completed", call_id: "call-patch", name: "apply_patch", input: patchText } },
    { type: "response_item", payload: { type: "custom_tool_call_output", call_id: "call-patch", output: "Exit code: 0\nOutput:\nSuccess. Updated route_test.go\n" } },
    {
      type: "response_item",
      payload: {
        type: "function_call",
        call_id: "call-terminal",
        name: "exec_command",
        arguments: JSON.stringify({ cmd: "git status --short --branch", workdir: "/Users/alice/work/jieli", yield_time_ms: 10000, max_output_tokens: 4000 }),
      },
    },
    { type: "response_item", payload: { type: "function_call_output", call_id: "call-terminal", output: "Chunk ID: abc\nProcess exited with code 1\nOutput:\n" } },
  ]);

  const payload = await runtime.buildPayloadFromHook({ session_id: "codex-tools", transcript_path: transcript }, "https://jieli.example.test");
  const patchUse = payload.thread.messages[1].content[0];
  assert.deepEqual(patchUse, { type: "tool_use", id: "call-patch", name: "apply_patch", input: { patch_text: patchText } });
  const patchResult = payload.thread.messages[2].content[0];
  assert.equal(patchResult.tool_use_id, "call-patch");
  assert.equal(patchResult.content, "");
  assert.match(patchResult.run.result.output, /Success\. Updated route_test\.go/);
  const shellUse = payload.thread.messages[3].content[0];
  assert.deepEqual(shellUse.input, { command: "git status --short --branch", cwd: "/Users/alice/work/jieli" });
  assert.equal(shellUse.name, "shell_command");
  const shellResult = payload.thread.messages[4].content[0];
  assert.equal(shellResult.content, "");
  assert.equal(shellResult.run.status, "error");
  assert.equal(shellResult.run.result.exitCode, 1);
});

test("filters Codex handoff summaries, git directives, internal context, loaded instructions, and file mention prefixes", async () => {
  const tmp = makeTempDir();
  const longSummary = "**Handoff Summary**\n\n**Current task**\n" + "do not upload this summary\n".repeat(20);
  const automaticCompactSummary =
    "Another language model started to solve this problem and produced a summary of its thinking process. " +
    "You also have access to the state of the tools that were used by that language model. " +
    "Use this to build on the work that has already been done and avoid duplicating work. " +
    "Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:\n" +
    "Current progress:\n\n" +
    "- Repo: `/Users/alice/work/jieli`.\n" +
    "- This compacted implementation detail should not be uploaded.\n".repeat(20);
  const finalText = '已提交：`abc1234 fix sync`\n\n::git-stage{cwd="/Users/alice/work/jieli"}\n::git-commit{cwd="/Users/alice/work/jieli"}\n';
  const userText = "# Files mentioned by the user:\n\n## codex-clipboard-ba43.png: /var/folders/T/codex-clipboard-ba43.png\n\n## My request for Codex:\nthreads list, hidden branch name, just show repo";
  const agentsBlock = "# AGENTS.md instructions\n\n<INSTRUCTIONS>\n# AI AGENT PROTOCOLS v2.0\n</INSTRUCTIONS>";
  const skillBlock = "<skill>\n<name>claude-code-setup:spec-driven-planning</name>\n<path>/Users/alice/skills/spec-driven-planning/SKILL.md</path>\n# Spec-Driven Planning\n</skill>";
  const localLink = "use [$claude-code-setup:spec-driven-planning](/Users/alice/Library/Mobile Documents/com~apple~CloudDocs/dotfiles/config/claude/skills/spec-driven-planning/SKILL.md)";
  const windowsEnvironmentContext =
    "<environment_context>\n" +
    "<cwd>C:\\Users\\Administrator\\.codex</cwd>\n" +
    "<shell>powershell</shell>\n" +
    "<current_date>2026-06-13</current_date>\n" +
    "<timezone>Asia/Shanghai</timezone>\n" +
    '<filesystem><workspace_roots><root>C:\\Users\\Administrator\\.codex</root></workspace_roots><permission_profile type="managed"><file_system type="restricted"><entry access="read"><special>:root</special></entry><entry access="write"><path>C:\\Users\\Administrator\\.codex</path></entry></file_system></permission_profile></filesystem>\n' +
    "</environment_context>";
  const transcript = join(tmp, "session.jsonl");
  writeJsonl(transcript, [
    { type: "session_meta", payload: { id: "codex-filter", cwd: "/Users/alice/work/jieli" } },
    { type: "response_item", payload: { type: "message", role: "user", content: [{ type: "input_text", text: "<codex_internal_context>resume</codex_internal_context>" }] } },
    { type: "response_item", payload: { type: "message", role: "user", content: [{ type: "input_text", text: "<turn_aborted></turn_aborted>" }] } },
    { type: "response_item", payload: { type: "message", role: "user", content: [{ type: "input_text", text: "<command-name>/plugin</command-name>\n<command-message>plugin</command-message>\n<command-args></command-args>" }] } },
    { type: "response_item", payload: { type: "message", role: "user", content: [{ type: "input_text", text: "<local-command-stdout>(no content)</local-command-stdout>" }] } },
    { type: "response_item", payload: { type: "message", role: "user", content: [{ type: "input_text", text: windowsEnvironmentContext }] } },
    { type: "response_item", payload: { type: "message", role: "user", content: [{ type: "input_text", text: agentsBlock }] } },
    { type: "response_item", payload: { type: "message", role: "user", content: [{ type: "input_text", text: skillBlock }] } },
    { type: "response_item", payload: { type: "message", role: "user", content: [{ type: "input_text", text: userText }] } },
    { type: "response_item", payload: { type: "message", role: "user", content: [{ type: "input_text", text: localLink }] } },
    { type: "response_item", payload: { type: "message", role: "assistant", content: [{ type: "output_text", text: longSummary }] } },
    { type: "response_item", payload: { type: "message", role: "user", content: [{ type: "input_text", text: automaticCompactSummary }] } },
    { type: "response_item", payload: { type: "message", role: "assistant", content: [{ type: "output_text", text: finalText }] } },
  ]);

  const payload = await runtime.buildPayloadFromHook({ session_id: "codex-filter", transcript_path: transcript }, "https://jieli.example.test");
  assert.deepEqual(payload.thread.messages.map((message) => message.role), ["user", "user", "assistant", "user", "assistant"]);
  assert.equal(payload.thread.messages[0].content, "threads list, hidden branch name, just show repo");
  assert.equal(
    payload.thread.messages[1].content,
    "use [$claude-code-setup:spec-driven-planning](file:///Users/alice/Library/Mobile%20Documents/com~apple~CloudDocs/dotfiles/config/claude/skills/spec-driven-planning/SKILL.md)",
  );
  assert.equal(payload.thread.messages[2].content, runtime.COMPACTION_PLACEHOLDER);
  assert.equal(payload.thread.messages[3].content, runtime.COMPACTION_PLACEHOLDER);
  assert.equal(payload.thread.messages[4].content, "已提交：`abc1234 fix sync`");
  assert.equal(payload.thread.title, "threads list, hidden branch name, just show repo");
  assert.equal(Object.hasOwn(payload.thread, "metadata"), false);
  const raw = JSON.stringify(payload);
  assert.doesNotMatch(raw, /<environment_context>|codex_internal_context|turn_aborted|command-name|command-message|command-args|local-command-stdout|AI AGENT PROTOCOLS|Spec-Driven Planning|Files mentioned by the user|codex-clipboard|do not upload this summary|compacted implementation detail|::git-/);
});

test("normalizes Codex repo metadata, data URL images, local image events, and attachment cache", async () => {
  const tmp = makeTempDir();
  const repo = join(tmp, "repo");
  mkdirSync(repo);
  spawnSync("git", ["init"], { cwd: repo, stdio: "ignore" });
  spawnSync("git", ["remote", "add", "origin", "git@home.pika12.com:guoyb/jieli.git"], { cwd: repo, stdio: "ignore" });
  const imageData = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x00]);
  const imageUrl = `data:image/png;base64,${imageData.toString("base64")}`;
  const imagePath = join(tmp, "1.png");
  writeFileSync(imagePath, imageData);
  const transcript = join(tmp, "session.jsonl");
  writeJsonl(transcript, [
    { type: "session_meta", payload: { id: "codex-images", cwd: repo } },
    {
      type: "response_item",
      payload: {
        type: "message",
        role: "user",
        content: [
          { type: "input_text", text: "<image name=[Image #1]>" },
          { type: "input_image", image_url: imageUrl, detail: "high" },
          { type: "input_text", text: "</image>" },
          { type: "input_text", text: "[Image #1] what is this?" },
        ],
      },
    },
    { type: "event_msg", payload: { type: "user_message", message: "ok [Image #2] what is this?", images: [], local_images: [imagePath], text_elements: [{ placeholder: "[Image #2]" }] } },
  ]);

  const dataUploads = [];
  const pathUploads = [];
  const payload = await runtime.buildPayloadFromHook(
    { session_id: "codex-images", transcript_path: transcript },
    "https://jieli.example.test",
    async (path) => {
      pathUploads.push(path);
      return "https://jieli.example.test/attachments/local.png";
    },
    async (data, mediaType) => {
      dataUploads.push({ data, mediaType });
      return "https://jieli.example.test/attachments/data.png";
    },
  );

  assert.equal(payload.repo, "");
  assert.equal(payload.repo_url, "git@home.pika12.com:guoyb/jieli.git");
  assert.deepEqual(dataUploads, [{ data: imageData, mediaType: "image/png" }]);
  assert.deepEqual(pathUploads, [imagePath]);
  assert.deepEqual(payload.thread.messages[0].content, [
    { type: "image", source: { url: "https://jieli.example.test/attachments/data.png", type: "image/png" } },
    { type: "text", text: "[Image #1] what is this?" },
  ]);
  assert.deepEqual(payload.thread.messages[1].content, [
    { type: "text", text: "ok [Image #2] what is this?" },
    { type: "image", source: { url: "https://jieli.example.test/attachments/local.png", type: "image/png" } },
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

    const failingCalls = [];
    const failingUpload = async (path) => {
      failingCalls.push(path);
      throw new Error("backend is down");
    };
    const otherImage = join(tmp, "2.png");
    writeFileSync(otherImage, Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a]));
    await assert.rejects(() => runtime.uploadAttachmentCached(otherImage, "https://jieli.example.test/", "secret", failingUpload), /backend is down/);
    await assert.rejects(() => runtime.uploadAttachmentCached(otherImage, "https://jieli.example.test/", "secret", failingUpload), /backend is down/);
    assert.deepEqual(failingCalls, [otherImage, otherImage]);
  });
});

test("does not infer Codex repo metadata from local folder names", async () => {
  const tmp = makeTempDir();
  const local = join(tmp, "2026-06-14", "python");
  mkdirSync(local, { recursive: true });
  const transcript = join(tmp, "local-session.jsonl");
  writeJsonl(transcript, [
    { type: "session_meta", payload: { id: "codex-local", cwd: local, git: { branch: "main" } } },
    { type: "response_item", payload: { type: "message", role: "user", content: [{ type: "input_text", text: "写一个python版的二分排序算法" }] } },
  ]);

  const payload = await runtime.buildPayloadFromHook({ session_id: "codex-local", transcript_path: transcript }, "https://jieli.example.test");

  assert.equal(payload.repo, "");
  assert.equal(payload.repo_url, "");
  assert.equal(payload.thread.cwd, local);
});

test("keeps the existing image label when the Codex uploader fails instead of inserting a placeholder", async () => {
  const tmp = makeTempDir();
  const imagePath = join(tmp, "1.png");
  writeFileSync(imagePath, Buffer.from([0x89, 0x50, 0x4e, 0x47]));
  const transcript = join(tmp, "session.jsonl");
  writeJsonl(transcript, [
    { type: "session_meta", payload: { id: "codex-image-fallback", cwd: "/Users/alice/work/jieli" } },
    { type: "event_msg", payload: { type: "user_message", message: "ok [Image #1] what is this?", images: [], local_images: [imagePath], text_elements: [{ placeholder: "[Image #1]" }] } },
  ]);

  const payload = await runtime.buildPayloadFromHook(
    { session_id: "codex-image-fallback", transcript_path: transcript },
    "https://jieli.example.test",
    async () => {
      throw new Error("backend is down");
    },
  );

  assert.equal(payload.thread.messages.length, 1);
  assert.equal(payload.thread.messages[0].content, "ok [Image #1] what is this?");
  assert.doesNotMatch(JSON.stringify(payload), /\[Image unavailable\]/);
});

test("configuration, upload, lock, session mapping, and missing transcript behavior match Codex hooks", async () => {
  const tmp = makeTempDir();
  await withEnv({ HOME: tmp, JIELI_API_KEY: undefined, JIELI_BASE_URL: undefined }, async () => {
    const response = runtime.buildMissingConfigHookResponse("stop", runtime.missingConfigVars());
    assert.equal(response.continue, true);
    assert.match(response.systemMessage, /settings\.json/);
    assert.match(response.systemMessage, /chmod it to 600/);
    assert.deepEqual(runtime.buildMissingConfigHookResponse("userpromptsubmit", ["JIELI_API_KEY"]), {});
  });

  await withEnv({ HOME: tmp, JIELI_API_KEY: undefined, JIELI_BASE_URL: undefined }, async () => {
    mkdirSync(join(tmp, ".config", "jieli"), { recursive: true });
    writeFileSync(join(tmp, ".config", "jieli", "settings.json"), JSON.stringify({ api_key: "settings-key", base_url: "https://jieli.example.test" }), "utf8");
    assert.deepEqual(runtime.missingConfigVars(), []);
    assert.equal(runtime.requiredEnv("JIELI_API_KEY"), "settings-key");
    assert.equal(runtime.optionalEnv("JIELI_BASE_URL"), "https://jieli.example.test");
  });

  const bomHome = makeTempDir();
  await withEnv({ HOME: bomHome, JIELI_API_KEY: undefined, JIELI_BASE_URL: undefined }, async () => {
    mkdirSync(join(bomHome, ".config", "jieli"), { recursive: true });
    writeFileSync(
      join(bomHome, ".config", "jieli", "settings.json"),
      `\uFEFF${JSON.stringify({ api_key: "bom-settings-key", base_url: "https://bom.example.test/" })}`,
      "utf8",
    );
    assert.deepEqual(runtime.missingConfigVars(), []);
    assert.equal(runtime.requiredEnv("JIELI_API_KEY"), "bom-settings-key");
    assert.equal(runtime.optionalEnv("JIELI_BASE_URL"), "https://bom.example.test");
  });

  const malformedHome = makeTempDir();
  await withEnv({ HOME: malformedHome, JIELI_API_KEY: undefined, JIELI_BASE_URL: undefined }, async () => {
    mkdirSync(join(malformedHome, ".config", "jieli"), { recursive: true });
    writeFileSync(join(malformedHome, ".config", "jieli", "settings.json"), '{"api_key":', "utf8");
    assert.throws(
      () => runtime.requiredEnv("JIELI_API_KEY"),
      (error) => {
        assert.match(error.message, /JIELI_API_KEY/);
        assert.match(error.message, /failed to parse ~\/\.config\/jieli\/settings\.json/);
        assert.doesNotMatch(error.message, /api_key/);
        return true;
      },
    );
  });

  await withEnv({ HOME: tmp, JIELI_API_KEY: "env-key", JIELI_BASE_URL: "https://env.example.test/" }, async () => {
    assert.equal(runtime.requiredEnv("JIELI_API_KEY"), "env-key");
    assert.equal(runtime.optionalEnv("JIELI_BASE_URL"), "https://env.example.test");
  });

  await withEnv({ HOME: tmp }, async () => {
    runtime.writeSessionMapping("codex-map", "https://jieli.example.test/", "T-codex-map", "/tmp/session.jsonl");
    const path = join(tmp, ".jieli", "codex-sessions.json");
    const mapping = JSON.parse(readFileSync(path, "utf8"));
    assert.equal(mapping["codex-map"].provider_thread_id, "T-codex-map");
    assert.equal(mapping["codex-map"].base_url, "https://jieli.example.test");
    assert.equal(mapping["codex-map"].session_path, "/tmp/session.jsonl");
    assert.equal(statSync(path).mode & 0o777, 0o600);

    const lockA = runtime.acquireSyncLock("sess-A");
    const lockB = runtime.acquireSyncLock("sess-B");
    const lockA2 = runtime.acquireSyncLock("sess-A");
    assert.equal(lockA.acquired, true);
    assert.equal(lockB.acquired, true);
    assert.equal(lockA2.acquired, false);
    runtime.releaseSyncLock(lockB);
    runtime.releaseSyncLock(lockA);
  });

  const { server, state } = createMockJieliServer();
  const baseUrl = await listen(server);
  try {
    await runtime.uploadPayload({ provider: "codex" }, `${baseUrl}/`, "secret");
    assert.equal(state.uploads[0].path, "/plugin/threads/upload");
  } finally {
    await close(server);
  }

  const bad = createMockJieliServer({ uploadStatus: 400, uploadResponse: { error: "unsupported provider", api_key: "jieli_" + "a".repeat(30) } });
  const badBase = await listen(bad.server);
  try {
    await assert.rejects(() => runtime.uploadPayload({ provider: "codex" }, badBase, "secret"), (error) => {
      assert.match(error.message, /unsupported provider/);
      assert.match(error.message, /\[REDACTED:jieli-api-key\]/);
      assert.doesNotMatch(error.message, /jieli_a{30}/);
      return true;
    });
  } finally {
    await close(bad.server);
  }

  const transcriptHome = makeTempDir();
  const missingTranscript = await runNode([join(pluginRoot, "scripts", "sync.mjs"), "--trigger", "sessionstart", "--jieli-hook"], {
    input: JSON.stringify({ transcript_path: join(transcriptHome, "sessions", "missing.jsonl"), session_id: "codex-not-flushed", cwd: "/Users/alice/work/jieli" }),
    env: { HOME: transcriptHome, PATH: process.env.PATH, JIELI_API_KEY: "secret" },
  });
  assert.equal(missingTranscript.status, 0);
  assert.throws(() => statSync(join(transcriptHome, ".jieli", "hooks.log")));
});

test("read-thread and find-threads helpers validate ids, shape requests, truncate output, and format markdown", async () => {
  assert.throws(() => runtime.validateThreadId("https://jieli.example.test/threads/T-1"), /provider thread id/);
  assert.throws(() => runtime.validateThreadId("T-1.md"), /without .md/);
  assert.equal(runtime.validateThreadId("T-1"), "T-1");
  assert.match(runtime.limitOutput("x".repeat(12001)), /\[Content truncated at 12000 chars/);

  const { server, state } = createMockJieliServer({
    threadBody: "thread markdown",
    searchResponse: { data: { threads: [{ provider_thread_id: "T-1", title: "Fix checkout", provider: "codex", repo: "shop/app", branch: "bugfix", updated_at: "2026-06-13T10:00:00Z", message_count: 7, preview: "checkout failed on coupon path" }] } },
  });
  const baseUrl = await listen(server);
  try {
    assert.equal(await runtime.fetchThreadExport("T-1", baseUrl, "secret"), "thread markdown");
    assert.equal(state.threadReads[0].headers.authorization, "Bearer secret");
    const search = await runtime.fetchThreads("payment bug", `${baseUrl}/`, "secret", {});
    assert.equal(state.searches[0].url.searchParams.get("search"), "payment bug");
    assert.equal(state.searches[0].url.searchParams.has("provider"), false);
    await runtime.fetchThreads("payment bug", baseUrl, "secret", { provider: "claude_code" });
    assert.equal(state.searches[1].url.searchParams.get("provider"), "claude_code");
    const markdown = runtime.formatThreadsMarkdown(search, "https://jieli.example.test");
    assert.match(markdown, /1\. Fix checkout/);
    assert.match(markdown, /provider: codex/);
    assert.match(markdown, /repo: shop\/app@bugfix/);
    assert.match(markdown, /read_url: https:\/\/jieli\.example\.test\/threads\/T-1/);
  } finally {
    await close(server);
  }
});

test("handoff info and commit trailer helpers support Codex shell aliases and Node-based helpers", async () => {
  const tmp = makeTempDir();
  const transcript = join(tmp, "rollout.jsonl");
  writeJsonl(transcript, [{ type: "session_meta", payload: { id: "stable-codex-id", cwd: tmp, git: { branch: "feature/handoff" } } }]);
  const encoded = Buffer.from(JSON.stringify({ session_id: "hook-id", transcript_path: transcript, cwd: "/wrong" }), "utf8").toString("base64");
  const info = await withEnv({ JIELI_HANDOFF_CONTEXT_B64: encoded, JIELI_BASE_URL: "https://jieli.example.test" }, () => runtime.buildHandoffInfo());
  assert.equal(info.confidence, "high");
  assert.equal(info.provider, "codex");
  assert.equal(info.session_id, "stable-codex-id");
  assert.equal(info.thread_id, "T-stable-codex-id");
  assert.equal(info.cwd, tmp);
  assert.equal(info.branch, "feature/handoff");

  for (const toolName of ["Bash", "Shell", "shell_command", "exec_command"]) {
    const response = runtime.buildHookResponse({ session_id: "codex-handoff", transcript_path: "/tmp/codex-session.jsonl", cwd: "/repo", tool_name: toolName, tool_input: { command: "jieli-handoff-info" } });
    const updated = response.hookSpecificOutput.updatedInput.command;
    assert.doesNotMatch(updated, /JIELI_HANDOFF_CONTEXT_B64=/);
    assert.match(updated, /node .*jieli_node\.mjs handoff-info --context-b64 /);
    assert.deepEqual(decodeHandoffContext(updated), { session_id: "codex-handoff", transcript_path: "/tmp/codex-session.jsonl", cwd: "/repo" });
  }
  const codexExecResponse = runtime.buildHookResponse({ session_id: "codex-exec", transcript_path: "/tmp/codex-exec.jsonl", cwd: "/repo", tool_name: "exec_command", tool_input: { cmd: "jieli-handoff-info" } });
  const codexExecCommand = codexExecResponse.hookSpecificOutput.updatedInput.cmd;
  assert.match(codexExecCommand, /node .*jieli_node\.mjs handoff-info --context-b64 /);
  assert.equal(codexExecResponse.hookSpecificOutput.updatedInput.command, undefined);
  assert.deepEqual(decodeHandoffContext(codexExecCommand), { session_id: "codex-exec", transcript_path: "/tmp/codex-exec.jsonl", cwd: "/repo" });
  for (const command of [
    "jieli-handoff-info.cmd",
    "jieli-handoff-info.exe",
    "C:\\Users\\Administrator\\.codex\\plugins\\cache\\jieliapp\\jieli\\bin\\jieli-handoff-info.cmd",
    "& 'C:\\Users\\Administrator\\.codex\\plugins\\cache\\jieliapp\\jieli\\bin\\jieli-handoff-info.cmd'",
  ]) {
    const response = runtime.buildHookResponse({ session_id: "codex-win", transcript_path: "C:\\Users\\Administrator\\.codex\\sessions\\rollout.jsonl", cwd: "C:\\repo", tool_name: "Shell", tool_input: { command } });
    const updated = response.hookSpecificOutput.updatedInput.command;
    assert.match(updated, /handoff-info --context-b64 /);
    assert.deepEqual(decodeHandoffContext(updated), { session_id: "codex-win", transcript_path: "C:\\Users\\Administrator\\.codex\\sessions\\rollout.jsonl", cwd: "C:\\repo" });
  }
  const cliInfo = await withEnv({ JIELI_HANDOFF_CONTEXT_B64: undefined, JIELI_BASE_URL: "https://jieli.example.test" }, () => runtime.buildHandoffInfo(process.env, encoded));
  assert.equal(cliInfo.thread_id, "T-stable-codex-id");
  assert.deepEqual(runtime.buildHookResponse({ session_id: "codex-handoff", tool_name: "Bash", tool_input: { command: "jieli-handoff-info | cat" } }), {});

  await withEnv({ HOME: tmp, JIELI_HANDOFF_CONTEXT_B64: undefined, JIELI_BASE_URL: "https://jieli.example.test" }, async () => {
    runtime.writeHandoffContext({ session_id: "hook-state", transcript_path: transcript, cwd: "/wrong" });
    const stateInfo = runtime.buildHandoffInfo();
    assert.equal(stateInfo.confidence, "high");
    assert.equal(stateInfo.session_id, "stable-codex-id");
    assert.equal(stateInfo.thread_id, "T-stable-codex-id");
    assert.equal(stateInfo.reason, "hook context persisted by Codex hook");
  });

  await withEnv({ HOME: tmp }, async () => {
    mkdirSync(join(tmp, ".jieli"), { recursive: true });
    writeFileSync(join(tmp, ".jieli", "codex-sessions.json"), JSON.stringify({ "codex-chain": { provider_thread_id: "T-codex-chain", base_url: "https://jieli.example.test" } }), "utf8");
    const command = 'git status --short && git add plugins/codex/scripts/commit_trailer.mjs && git commit -m "fix: add codex thread trailers" -- plugins/codex/scripts/commit_trailer.mjs';
    const response = runtime.buildHookResponse({ session_id: "codex-chain", tool_name: "shell_command", tool_input: { command } });
    assert.equal(
      response.hookSpecificOutput.updatedInput.command,
      'git status --short && git add plugins/codex/scripts/commit_trailer.mjs && git commit -m "fix: add codex thread trailers" --trailer "Jieli-Thread: https://jieli.example.test/threads/T-codex-chain" -- plugins/codex/scripts/commit_trailer.mjs',
    );
    assert.deepEqual(runtime.buildHookResponse({ session_id: "codex-chain", tool_name: "Bash", tool_input: { command: 'git commit -m "ship" | cat' } }), {});
    assert.deepEqual(runtime.buildHookResponse({ session_id: "codex-chain", tool_name: "Bash", tool_input: { command: 'git commit -m "ship" --trailer Jieli-Thread:old' } }), {});
  });
});

test("plugin wrappers, docs, manifests, and hooks describe the split Jieli tools", () => {
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

  const fallbackHelper = join(pluginRoot, "scripts", "jieli_helper.mjs");
  const fallbackHelperSource = readFileSync(fallbackHelper, "utf8");
  assert.match(fallbackHelperSource, /import \{ main \} from "\.\/jieli_node\.mjs"/);
  assert.doesNotMatch(fallbackHelperSource, /child_process|readdirSync|\.cmd/);
  for (const [command, expected] of [
    ["read-thread", /Read a Jieli thread export/],
    ["find-threads", /Find Jieli threads/],
    ["handoff-info", /usage: jieli-handoff-info/],
  ]) {
    const result = spawnSync(process.execPath, [fallbackHelper, command, "--help"], { env: {}, encoding: "utf8", timeout: 5000 });
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, expected);
  }
  const fallbackTmp = makeTempDir();
  const fallbackTranscript = join(fallbackTmp, "rollout.jsonl");
  writeJsonl(fallbackTranscript, [{ type: "session_meta", payload: { id: "skill-fallback-codex", cwd: "/repo", git: { branch: "skill-fallback" } } }]);
  const fallbackContext = Buffer.from(JSON.stringify({ session_id: "hook-id", transcript_path: fallbackTranscript, cwd: "/wrong" }), "utf8").toString("base64");
  const fallbackResult = spawnSync(process.execPath, [fallbackHelper, "handoff-info", "--context-b64", fallbackContext], {
    env: { HOME: fallbackTmp, JIELI_BASE_URL: "https://jieli.example.test" },
    encoding: "utf8",
    timeout: 5000,
  });
  assert.equal(fallbackResult.status, 0, fallbackResult.stderr);
  assert.equal(JSON.parse(fallbackResult.stdout).thread_id, "T-skill-fallback-codex");

  const docs = [
    readFileSync(join(repoRoot, "README.md"), "utf8"),
    readFileSync(join(pluginRoot, "README.md"), "utf8"),
    readFileSync(join(pluginRoot, ".codex-plugin", "plugin.json"), "utf8"),
  ].join("\n");
  assert.match(docs, /`jieli-read`/);
  assert.match(docs, /`jieli-find`/);
  assert.doesNotMatch(docs, /https:\/\/your-jieli\.example\.com|self-hosted|Jieli thread reading skill|Provides the `jieli` skill/);

  const manifest = JSON.parse(readFileSync(join(pluginRoot, ".codex-plugin", "plugin.json"), "utf8"));
  assert.equal(manifest.name, "jieli");
  assert.equal(manifest.interface.displayName, "Jieli Sync");
  assert.equal(manifest.interface.websiteURL, "https://jieli.app");
  assert.equal("displayName" in manifest, false);
  assert.equal("userConfig" in manifest, false);

  const hooks = JSON.parse(readFileSync(join(pluginRoot, "hooks", "hooks.json"), "utf8"));
  assert.equal("UserPromptSubmit" in hooks.hooks, false);
  assert.equal(hooks.hooks.PreToolUse[0].matcher, "^(Bash|Shell|shell_command|exec_command)$");
  const commands = Object.values(hooks.hooks).flatMap((configs) => configs.flatMap((config) => (config.hooks || []).map((hook) => hook.command)));
  assert.ok(commands.length > 0);
  for (const command of commands) {
    assert.match(command, /node "\$\{PLUGIN_ROOT\}\/scripts\/.*\.mjs"/);
    assert.doesNotMatch(command, /python3/);
  }
});
