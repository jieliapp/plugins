#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, extname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const PROVIDER = "claude_code";
const DEFAULT_BASE_URL = "https://jieli.app";
const LOCK_TTL_SECONDS = 60;
const TRANSCRIPT_FLUSH_TRIGGERS = new Set(["stop", "precompact"]);
const MISSING_CONFIG_NOTICE_TRIGGERS = new Set(["stop", "precompact"]);
const TRANSCRIPT_QUIET_MS = 250;
const TRANSCRIPT_FLUSH_TIMEOUT_MS = 1500;
const ATTACHMENT_CACHE_FILE = "claude-attachments.json";
const SETTINGS_FILE_NAME = "settings.json";
const TRAILER_KEY = "Jieli-Thread";
const HANDOFF_CONTEXT_ENV = "JIELI_HANDOFF_CONTEXT_B64";
const HANDOFF_HELPER_COMMAND = "jieli-handoff-info";
const AMBIGUOUS_TOKENS = ["||", ";", "\n", "$(", "`", "<<", "|"];
const MODEL_ALIAS_ENV_NAMES = [
  "ANTHROPIC_DEFAULT_HAIKU_MODEL",
  "ANTHROPIC_DEFAULT_SONNET_MODEL",
  "ANTHROPIC_DEFAULT_OPUS_MODEL",
];
const COMPACTION_PLACEHOLDER =
  "[Context compacted - earlier conversation summarized to continue past the context window]";
const BASH_NO_OUTPUT_MARKERS = new Set(["", "(Bash completed with no output)"]);
const SUPPORTED_IMAGE_MEDIA_TYPES = new Map([
  [".png", "image/png"],
  [".jpg", "image/jpeg"],
  [".jpeg", "image/jpeg"],
  [".gif", "image/gif"],
  [".webp", "image/webp"],
]);

const pluginRoot = process.env.CLAUDE_PLUGIN_ROOT || join(dirname(fileURLToPath(import.meta.url)), "..");

function usage() {
  console.error("Usage: jieli_node.mjs <sync|commit-trailer|read-thread|find-threads|handoff-info> [args...]");
}

export async function main(forcedCommand = "") {
  const argv = process.argv.slice(2);
  const command = forcedCommand || argv.shift();
  const args = forcedCommand ? argv : argv;
  try {
    if (command === "sync") return await syncMain(args);
    if (command === "commit-trailer") return commitTrailerMain(args);
    if (command === "read-thread") return await readThreadMain(args);
    if (command === "find-threads") return await findThreadsMain(args);
    if (command === "handoff-info") return handoffInfoMain(args);
    usage();
    return 2;
  } catch (error) {
    console.error(`jieli failed: ${formatError(error)}`);
    return 1;
  }
}

function parseArgs(args, spec = {}) {
  const result = { _: [] };
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (!arg.startsWith("--")) {
      result._.push(arg);
      continue;
    }
    const [rawKey, inlineValue] = arg.slice(2).split("=", 2);
    const key = rawKey.replace(/-([a-z])/g, (_, char) => char.toUpperCase());
    if (spec.boolean?.has(rawKey)) {
      result[key] = true;
      continue;
    }
    const value = inlineValue ?? args[++i];
    result[key] = value ?? "";
  }
  return result;
}

function readStdin() {
  try {
    return readFileSync(0, "utf8");
  } catch {
    return "";
  }
}

function readJson(path, fallback = {}) {
  try {
    const value = JSON.parse(readFileSync(path, "utf8"));
    return value && typeof value === "object" && !Array.isArray(value) ? value : fallback;
  } catch {
    return fallback;
  }
}

function writeJsonAtomic(path, value, mode = 0o600) {
  mkdirSync(dirname(path), { recursive: true });
  const tmpPath = `${path}.tmp`;
  writeFileSync(tmpPath, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", mode });
  renameSync(tmpPath, path);
  try {
    writeFileSync(path, readFileSync(path), { mode });
  } catch {
    // chmod is best effort on Windows.
  }
}

function homeDir() {
  return homedir();
}

function settingsPath(home = homeDir()) {
  return join(home, ".config", "jieli", SETTINGS_FILE_NAME);
}

function settingsValue(...keys) {
  const settings = readJson(settingsPath(), {});
  for (const key of keys) {
    const value = settings[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function requiredEnv(...names) {
  for (const name of names) {
    if (process.env[name]) return process.env[name];
  }
  if (names[0] === "JIELI_API_KEY") {
    const value = settingsValue("api_key", "JIELI_API_KEY");
    if (value) return value;
  }
  throw new Error(names[0]);
}

function optionalEnv(...names) {
  for (const name of names) {
    if (process.env[name]) return process.env[name];
  }
  if (names[0] === "JIELI_BASE_URL") {
    const value = settingsValue("base_url", "JIELI_BASE_URL");
    if (value) return value.replace(/\/+$/, "");
  }
  return "";
}

function missingConfigVars() {
  if (process.env.JIELI_API_KEY || process.env.CLAUDE_PLUGIN_OPTION_API_KEY) return [];
  if (settingsValue("api_key", "JIELI_API_KEY")) return [];
  return ["JIELI_API_KEY"];
}

function buildMissingConfigHookResponse(trigger, missing) {
  if (!MISSING_CONFIG_NOTICE_TRIGGERS.has(String(trigger || "").toLowerCase()) || missing.length === 0) return {};
  return {
    continue: true,
    systemMessage:
      "Jieli Claude Code Sync is not configured. " +
      `Missing: ${missing.join(", ")}. ` +
      `Go to ${DEFAULT_BASE_URL}, register or sign in, create an API key, then configure the plugin api_key option, ` +
      "set JIELI_API_KEY in your environment, " +
      'or write `~/.config/jieli/settings.json` with `{"api_key":"<key>","base_url":"https://jieli.app"}`. ' +
      "You can paste the API key into this chat and ask the agent to configure it for you. " +
      "Sync will stay disabled until configured.",
  };
}

function redactText(value) {
  let text = String(value || "").replace(/[\u{E0000}-\u{E007F}]/gu, "");
  text = redactUrls(text);
  const rules = [
    ["private-key", /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g],
    ["authorization-bearer", /(Authorization\s*:\s*Bearer\s+)[^\s"']+/gi, (m, prefix) => `${prefix}${typedRedaction("authorization-bearer")}`],
    ["redis-cli-password", /(\bredis-cli\b[^\n\r]*?\s(?:-a|--pass)\s+)([^\s]+)/gi, (m, prefix) => `${prefix}${typedRedaction("redis-cli-password")}`],
    ["redis-cli-password", /(\bredis-server\b[^\n\r]*?\s--requirepass\s+)([^\s]+)/gi, (m, prefix) => `${prefix}${typedRedaction("redis-cli-password")}`],
    ["openai-api-key", /\b(?:sk-ant-[A-Za-z0-9_-]+|sk-(?:proj|live|test)-[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{20,})\b/gi],
    ["aws-access-key", /\b(?:AKIA|ASIA|A3T)[A-Z0-9]{16}\b/g],
    ["github-token", /\b(?:ghp_[0-9A-Za-z]{36}|gho_[0-9A-Za-z]{36}|(?:ghu|ghs)_[0-9A-Za-z]{36}|ghr_[0-9A-Za-z]{76}|github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}|gh[ps]_[A-Za-z0-9_]{20,})\b/g],
    ["gitlab-token", /\bglpat-[0-9A-Za-z_-]{20,}\b/g],
    ["huggingface-token", /\bhf_[0-9A-Za-z]{20,}\b/g],
    ["google-api-key", /\bAIza[0-9A-Za-z_-]{35}\b/g],
    ["slack-token", /\bxox[baprs]-[0-9A-Za-z-]{10,}\b/g],
    ["stripe-secret-key", /\bsk_(?:live|test)_[0-9A-Za-z]{16,}\b/g],
    ["pypi-token", /\bpypi-[0-9A-Za-z_-]{20,}\b/g],
    ["linear-token", /\blin_api_[0-9A-Za-z]{20,}\b/g],
    ["age-secret-key", /\bAGE-SECRET-KEY-1[0-9A-Za-z]+/g],
    ["jieli-api-key", /\bjieli_[A-Za-z0-9_-]{20,}\b/g],
    ["slack-webhook", /https:\/\/hooks\.slack\.com\/(?:services|triggers|workflows)\/[A-Za-z0-9/_-]+/g],
    ["jwt-token", /\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/g],
    ["sensitive-assignment", /\b([A-Z0-9_.-]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|REDISCLI_AUTH)[A-Z0-9_.-]*\s*(?:=|:=|:)\s*)(["']?)(?!\[REDACTED:)([^\s,"']+)(["']?)/gi, (m, prefix, quote, _value, endQuote) => `${prefix}${quote}${typedRedaction(sensitiveKeyId(prefix))}${endQuote}`],
    ["json-yaml-sensitive-key", /(["']?[A-Za-z0-9_.-]*(?:api[_-]?key|api[_-]?token|access[_-]?token|auth[_-]?token|ws[_-]?token|rvt[_-]?token|token|secret|client[_-]?secret|password|passwd|pwd|jwt|sessionid|session|sid)[A-Za-z0-9_.-]*["']?\s*(?:=|:=|:)\s*)(["']?)(?!\[REDACTED:)([^\s,\n\r"'}\]]+)(["']?)/gi, (m, prefix, quote, _value, endQuote) => `${prefix}${quote}${typedRedaction(sensitiveKeyId(prefix))}${endQuote}`],
  ];
  for (const [id, pattern, replacement] of rules) {
    text = text.replace(pattern, replacement || typedRedaction(id));
  }
  return text;
}

function typedRedaction(id) {
  return `[REDACTED:${id}]`;
}

function sensitiveKeyId(rawKey, prefix = "") {
  const key = String(rawKey || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "value";
  return prefix ? `${prefix}-${key}` : key;
}

function redactUrls(text) {
  return text.replace(/\b(?:https?|wss?|ftp|file|redis|rediss|mongodb(?:\+srv)?|postgres(?:ql)?|mysql|mariadb):\/\/[^\s<>")']+/gi, redactUrl);
}

function redactUrl(raw) {
  const match = /^([A-Za-z][A-Za-z0-9+.-]*:\/\/)([^/?#]*)(.*)$/.exec(raw);
  if (!match) return raw;
  const [, scheme, authority, rest] = match;
  const atIndex = authority.lastIndexOf("@");
  const redactedAuthority =
    atIndex >= 0 ? `${typedRedaction("url-userinfo")}@${authority.slice(atIndex + 1)}` : authority;
  return `${scheme}${redactedAuthority}${redactUrlRest(rest)}`;
}

function redactUrlRest(rest) {
  const withoutFragment = rest.split("#", 1)[0];
  const queryIndex = withoutFragment.indexOf("?");
  if (queryIndex < 0) return withoutFragment;
  return `${withoutFragment.slice(0, queryIndex + 1)}${redactQuery(withoutFragment.slice(queryIndex + 1))}`;
}

function redactQuery(query) {
  return query
    .split(/([&;])/)
    .map((part) => {
      if (part === "&" || part === ";") return part;
      const separatorIndex = part.indexOf("=");
      if (separatorIndex < 0) return part;
      const key = part.slice(0, separatorIndex);
      if (!isSensitiveQueryKey(key)) return part;
      return `${key}=${typedRedaction(sensitiveKeyId(key, "url-query"))}`;
    })
    .join("");
}

function isSensitiveQueryKey(key) {
  const lowered = key.toLowerCase();
  const compact = lowered.replace(/[^a-z0-9]/g, "");
  return ["token", "key", "apikey", "access_token", "secret", "password", "auth", "authorization", "bearer", "jwt", "session", "sessionid", "sid"].some(
    (term) => lowered.includes(term) || compact.includes(term.replace(/_/g, "")),
  );
}

function redactJson(value) {
  if (typeof value === "string") return redactText(value);
  if (Array.isArray(value)) return value.map(redactJson);
  if (!value || typeof value !== "object") return value;
  if (["base64", "image"].includes(value.type) && Object.hasOwn(value, "data")) return value;
  const out = {};
  for (const [key, item] of Object.entries(value)) {
    if (isSensitiveJsonKey(key)) out[key] = typedRedaction(sensitiveKeyId(key));
    else if (key === "content" && value.isImage === true && typeof item === "string") out[key] = item;
    else out[key] = redactJson(item);
  }
  return out;
}

function isSensitiveJsonKey(key) {
  const raw = String(key || "");
  const compact = raw.toLowerCase().replace(/[^a-z0-9]/g, "");
  return (
    /(?:^|[_\-.])(?:api[_\-.]?key|api[_\-.]?token|access[_\-.]?token|auth[_\-.]?token|token|secret|client[_\-.]?secret|password|passwd|pwd|jwt|sessionid|session|sid|authorization|bearer|private[_\-.]?key|(?:redis|postgres|postgresql|mysql|mongodb|database|db)[_\-.]?url)(?:$|[_\-.])/i.test(raw) ||
    /(apikey|apitoken|accesstoken|authtoken|wstoken|rvttoken|secret|clientsecret|password|passwd|pwd|jwt|sessionid|authorization|bearer|privatekey|redisurl|postgresurl|postgresqlurl|mysqlurl|mongodburl|databaseurl|dburl)/.test(compact)
  );
}

function isSecretFilePath(path) {
  const name = basename(String(path || "")).toLowerCase();
  if (!name) return false;
  if ([".env", ".envrc"].includes(name)) return true;
  if (/^\.env\.(?!example$|sample$)[a-z0-9_.-]+$/.test(name)) return true;
  if (/^env\.(?!example$|sample$)[a-z0-9_.-]+$/.test(name)) return true;
  return /\.(?:secret|secrets|credential|credentials)$/i.test(name);
}

function loadHookStdin() {
  const raw = readStdin();
  if (!raw.trim()) return {};
  const value = JSON.parse(raw);
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

async function syncMain(args) {
  const opts = parseArgs(args, { boolean: new Set(["jieli-hook"]) });
  try {
    const missing = missingConfigVars();
    if (missing.length) {
      const response = buildMissingConfigHookResponse(opts.trigger || "", missing);
      if (Object.keys(response).length) console.log(JSON.stringify(response));
      throw new Error(missing.join(", "));
    }
    const hookData = loadHookStdin();
    const sessionId = hookData.session_id || "";
    const lock = acquireSyncLock(sessionId);
    if (!lock.acquired) return 0;
    try {
      const transcriptPath = typeof hookData.transcript_path === "string" ? hookData.transcript_path : "";
      if (transcriptPath && !existsSync(transcriptPath)) return 0;
      if (TRANSCRIPT_FLUSH_TRIGGERS.has(String(opts.trigger || "").toLowerCase()) && transcriptPath) {
        await waitForTranscriptFlush(transcriptPath);
      }
      const baseUrl = (optionalEnv("JIELI_BASE_URL", "CLAUDE_PLUGIN_OPTION_BASE_URL") || DEFAULT_BASE_URL).replace(/\/+$/, "");
      const apiKey = requiredEnv("JIELI_API_KEY", "CLAUDE_PLUGIN_OPTION_API_KEY");
      const payload = await buildPayloadFromHook(hookData, baseUrl, (path) => uploadAttachmentCached(path, baseUrl, apiKey));
      await uploadPayload(payload, baseUrl, apiKey);
      const providerThreadId = payload.thread.id;
      writeSessionMapping(claudeSessionId(providerThreadId), baseUrl, providerThreadId);
    } finally {
      releaseSyncLock(lock);
    }
  } catch (error) {
    logHookError(`sync ${opts.trigger || ""}: ${formatError(error)}`);
  }
  return 0;
}

async function buildPayloadFromHook(hookData, baseUrl = null, imageUploader = null) {
  const transcriptPath = hookData.transcript_path;
  if (!transcriptPath) throw new Error("transcript_path is required");
  const transcript = await parseTranscript(transcriptPath, hookData.session_id || "", imageUploader);
  const cwd = transcript.cwd || hookData.cwd || process.cwd();
  const branch = transcript.branch || gitBranch(cwd);
  const sessionId = hookData.session_id || transcript.id;
  if (!sessionId) throw new Error("session_id is required");
  const providerThreadId = jieliThreadId(sessionId);
  const base = (baseUrl || optionalEnv("JIELI_BASE_URL", "CLAUDE_PLUGIN_OPTION_BASE_URL") || DEFAULT_BASE_URL).replace(/\/+$/, "");
  const messages = transcript.messages;
  const title = transcript.title || titleFromMessages(messages);
  const resolvedModel = transcript.model || "";
  const displayModel = displayModelName(resolvedModel);
  const thread = {
    id: providerThreadId,
    title,
    model: displayModel,
    cwd,
    created_ms: transcript.created_ms || 0,
    updated_ms: transcript.updated_ms || 0,
    messages,
  };
  if (resolvedModel && resolvedModel !== displayModel) thread.resolved_model = resolvedModel;
  return {
    provider: PROVIDER,
    repo: "",
    repo_url: repoUrlFromCwd(cwd),
    branch,
    source_url: base ? `${base}/threads/${providerThreadId}` : "",
    labels: [],
    thread,
  };
}

async function parseTranscript(path, fallbackSessionId = "", imageUploader = null) {
  const messages = [];
  const mergeSources = [];
  let sessionId = fallbackSessionId || "";
  let cwd = "";
  let branch = "";
  let model = "";
  let createdMs = 0;
  let updatedMs = 0;
  let pendingBashIndex = null;
  let pendingBashCommand = "";
  const lines = readFileSync(path, "utf8").split(/\r?\n/);
  for (const line of lines) {
    if (!line.trim()) continue;
    let entry;
    try {
      entry = JSON.parse(line);
    } catch {
      continue;
    }
    if (!["user", "assistant"].includes(entry.type)) continue;
    const message = entry.message;
    if (!message || typeof message !== "object") continue;

    let content;
    let role;
    if (entry.isCompactSummary) {
      content = COMPACTION_PLACEHOLDER;
      role = "user";
    } else {
      content = await normalizeContent(message.content, imageUploader);
      if (content == null) continue;
      role = normalizedRole(message.role || entry.type, content);
      content = normalizeLocalCommandMessage(role, content);
      if (content == null || isLoadedSkillBodyMessage(role, content)) continue;
    }

    const bash = role === "user" && typeof content === "string" ? parseBashBlock(content) : null;
    if (bash && bash.kind === "output" && pendingBashIndex !== null && pendingBashIndex === messages.length - 1) {
      messages[pendingBashIndex].content = renderBashTerminal(pendingBashCommand, bash.stdout, bash.stderr);
      pendingBashIndex = null;
    } else {
      if (bash) {
        if (bash.kind === "input") content = renderBashTerminal(bash.command, "", "", false);
        else if (bash.kind === "input_output") content = renderBashTerminal(bash.command, bash.stdout, bash.stderr);
        else content = renderBashTerminal(null, bash.stdout, bash.stderr);
      }
      const sourceMessageId = message.id || "";
      const item = {
        role,
        content,
        message_id: entry.uuid || sourceMessageId || message.message_id || "",
      };
      if (message.usage && typeof message.usage === "object") item.usage = redactJson(message.usage);
      const protocolId = message.protocolMessageID || message.protocol_message_id;
      if (protocolId) item.protocol_message_id = protocolId;
      if (isDuplicateUnavailableImageMessage(messages, item)) continue;
      appendTranscriptMessage(messages, mergeSources, item, sourceMessageId);
      if (bash && bash.kind === "input") {
        pendingBashIndex = messages.length - 1;
        pendingBashCommand = bash.command;
      } else {
        pendingBashIndex = null;
      }
    }

    sessionId = sessionId || entry.sessionId || entry.session_id || "";
    cwd = cwd || entry.cwd || "";
    branch = branch || entry.gitBranch || entry.git_branch || "";
    if (!model && role === "assistant") model = message.model || "";
    const stampMs = timestampMs(entry.timestamp);
    if (stampMs) {
      if (!createdMs) createdMs = stampMs;
      updatedMs = stampMs;
    }
  }
  return { id: sessionId, cwd, branch, model, created_ms: createdMs, updated_ms: updatedMs || createdMs, messages };
}

async function normalizeContent(content, imageUploader = null) {
  if (typeof content === "string") return await normalizeTextWithImages(content, imageUploader);
  if (Array.isArray(content)) {
    const blocks = [];
    for (const block of content) {
      if (!block || typeof block !== "object") continue;
      if (block.type === "tool_result") {
        blocks.push(normalizeToolResultBlock(block));
      } else if (block.type === "text") {
        if (block.text) appendBlocks(blocks, await normalizeTextBlocks(String(block.text), imageUploader));
      } else if (block.type === "thinking") {
        if (block.thinking) blocks.push({ type: "thinking", thinking: redactText(String(block.thinking)) });
      } else if (block.type === "image") {
        const image = await imageBlockFromPath(imagePathFromBlock(block), imageUploader);
        if (image) blocks.push(image);
        else if (!hasExistingImageLabel(blocks)) blocks.push({ type: "text", text: "[Image unavailable]" });
      } else {
        blocks.push(redactJson(block));
      }
    }
    return collapseTextOnlyBlocks(blocks);
  }
  if (content == null) return null;
  return redactJson(content);
}

function normalizeToolResultBlock(block) {
  const redacted = redactJson(block);
  const toolUseId = redacted.tool_use_id || redacted.toolUseID;
  const content = redacted.content ?? "";
  const run = redacted.run && typeof redacted.run === "object" ? redacted.run : {};
  const result = run.result && typeof run.result === "object" ? run.result : run.result;
  const output = toolResultOutput(result, content);
  const exitCode = toolResultExitCode(result);
  const out = { type: "tool_result", content };
  if (typeof toolUseId === "string" && toolUseId) out.tool_use_id = toolUseId;
  out.run = {
    status: normalizeToolStatus(run.status, redacted.is_error),
    result: { output },
  };
  if (exitCode !== null) out.run.result.exitCode = exitCode;
  return out;
}

function toolResultOutput(result, content) {
  if (result && typeof result === "object") {
    if (typeof result.output === "string") return result.output;
    if (result.content != null) return toolResultOutput(null, result.content);
  }
  if (typeof result === "string") return result;
  if (typeof content === "string") return content;
  if (content == null) return "";
  return JSON.stringify(content);
}

function toolResultExitCode(result) {
  if (!result || typeof result !== "object") return null;
  const value = result.exitCode ?? result.exit_code;
  return Number.isInteger(value) ? value : null;
}

function normalizeToolStatus(status, isError) {
  if (isError === true) return "error";
  if (typeof status === "string") {
    const value = status.trim().toLowerCase();
    if (["done", "success", "succeeded", "completed", "ok"].includes(value)) return "completed";
    if (["error", "failed", "failure", "errored"].includes(value)) return "error";
    if (["cancelled", "canceled"].includes(value)) return "cancelled";
  }
  return "completed";
}

async function normalizeTextWithImages(text, imageUploader = null) {
  return collapseTextOnlyBlocks(await normalizeTextBlocks(text, imageUploader));
}

async function normalizeTextBlocks(text, imageUploader = null) {
  const blocks = [];
  const regex = /\[Image:\s*source:\s*([^\]]+)\]/g;
  let position = 0;
  for (const match of text.matchAll(regex)) {
    appendTextBlock(blocks, text.slice(position, match.index));
    const image = await imageBlockFromPath(match[1], imageUploader);
    if (image) blocks.push(image);
    else if (!hasExistingImageLabel(blocks)) blocks.push({ type: "text", text: "[Image unavailable]" });
    position = match.index + match[0].length;
  }
  appendTextBlock(blocks, text.slice(position));
  return blocks;
}

function appendTextBlock(blocks, text) {
  const value = redactText(text).trim();
  if (value) blocks.push({ type: "text", text: value });
}

function appendBlocks(blocks, nextBlocks) {
  blocks.push(...nextBlocks);
}

function collapseTextOnlyBlocks(blocks) {
  if (!blocks.length) return null;
  if (blocks.every((block) => block && block.type === "text")) {
    return blocks.map((block) => block.text || "").filter(Boolean).join("\n\n");
  }
  return blocks;
}

function imagePathFromBlock(block) {
  if (typeof block.source === "string") return block.source;
  if (block.source && typeof block.source === "object") {
    for (const key of ["path", "file_path", "sourcePath", "url"]) {
      if (typeof block.source[key] === "string" && block.source[key].startsWith("/")) return block.source[key];
    }
  }
  for (const key of ["sourcePath", "path", "file_path"]) {
    if (typeof block[key] === "string") return block[key];
  }
  return "";
}

function mediaTypeForImage(path) {
  return SUPPORTED_IMAGE_MEDIA_TYPES.get(extname(path).toLowerCase()) || "";
}

async function imageBlockFromPath(rawPath, imageUploader = null) {
  if (!rawPath || !imageUploader) return null;
  const path = rawPath.trim();
  const mediaType = mediaTypeForImage(path);
  if (!mediaType) return null;
  try {
    const url = await imageUploader(path);
    return url ? { type: "image", source: { url, type: mediaType } } : null;
  } catch {
    return null;
  }
}

function hasExistingImageLabel(blocks) {
  for (let i = blocks.length - 1; i >= 0; i -= 1) {
    const block = blocks[i];
    if (!block || block.type !== "text") continue;
    if (/\[Image\s+#\d+\]/.test(block.text || "")) return true;
  }
  return false;
}

function normalizedContentText(content) {
  if (typeof content === "string") return content.trim();
  if (Array.isArray(content)) {
    return content.map((block) => (block && typeof block.text === "string" ? block.text.trim() : "")).filter(Boolean).join("\n\n").trim();
  }
  return "";
}

function isDuplicateUnavailableImageMessage(messages, item) {
  if (item.role !== "user" || normalizedContentText(item.content) !== "[Image unavailable]") return false;
  const previous = messages[messages.length - 1];
  if (!previous || previous.role !== "user") return false;
  const text = normalizedContentText(previous.content);
  return text.includes("[Image unavailable]") || /\[Image\s+#\d+\]/.test(text);
}

function normalizedRole(role, content) {
  const blocks = Array.isArray(content) ? content : [];
  if (blocks.length && blocks.every((block) => block && block.type === "tool_result")) return "tool";
  return String(role || "");
}

function normalizeLocalCommandMessage(role, content) {
  if (role !== "user") return content;
  const text = textFromNormalizedContent(content).trim();
  if (text.startsWith("<command-message>")) return tagText(text, "command-name") || null;
  if (["<local-command-caveat>", "<command-name>", "<local-command-stdout>", "<local-command-stderr>"].some((prefix) => text.startsWith(prefix))) return null;
  return content;
}

function isLoadedSkillBodyMessage(role, content) {
  return role === "user" && textFromNormalizedContent(content).trimStart().startsWith("Base directory for this skill:");
}

function textFromNormalizedContent(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((block) => (block && typeof block.text === "string" ? block.text : "")).filter(Boolean).join("\n\n");
  return "";
}

function parseBashBlock(text) {
  const stripped = text.trim();
  const startsInput = stripped.startsWith("<bash-input>");
  const startsOutput = stripped.startsWith("<bash-stdout>") || stripped.startsWith("<bash-stderr>");
  if (!startsInput && !startsOutput) return null;
  const hasOutput = stripped.includes("<bash-stdout>") || stripped.includes("<bash-stderr>");
  if (startsInput) {
    const command = firstTagCapture(stripped, "bash-input");
    if (hasOutput) {
      return { kind: "input_output", command, stdout: firstTagCapture(stripped, "bash-stdout"), stderr: firstTagCapture(stripped, "bash-stderr") };
    }
    return { kind: "input", command };
  }
  return { kind: "output", stdout: firstTagCapture(stripped, "bash-stdout"), stderr: firstTagCapture(stripped, "bash-stderr") };
}

function firstTagCapture(text, tagName) {
  const match = new RegExp(`<${tagName}>([\\s\\S]*?)</${tagName}>`).exec(text);
  return match ? match[1] : "";
}

function renderBashTerminal(command, stdout = "", stderr = "", noteEmpty = true) {
  let out = stdout.trim();
  let err = stderr.trim();
  if (BASH_NO_OUTPUT_MARKERS.has(out)) out = "";
  if (BASH_NO_OUTPUT_MARKERS.has(err)) err = "";
  const lines = [];
  if (command !== null) lines.push(`$ ${String(command).trim()}`);
  if (out) lines.push(out);
  if (err) lines.push("# [stderr]", err);
  if (!out && !err && command !== null && noteEmpty) lines.push("# (no output)");
  return `\`\`\`console\n${lines.join("\n")}\n\`\`\``;
}

function tagText(text, tagName) {
  const match = new RegExp(`<${tagName}>\\s*([\\s\\S]*?)\\s*</${tagName}>`).exec(text);
  return match ? match[1].trim() : "";
}

function appendTranscriptMessage(messages, mergeSources, item, sourceMessageId) {
  if (
    sourceMessageId &&
    messages.length &&
    mergeSources[mergeSources.length - 1] === sourceMessageId &&
    messages[messages.length - 1].role === "assistant" &&
    item.role === "assistant"
  ) {
    const previous = messages[messages.length - 1];
    previous.content = contentBlocks(previous.content).concat(contentBlocks(item.content));
    if (item.usage) previous.usage = item.usage;
    if (item.protocol_message_id) previous.protocol_message_id = item.protocol_message_id;
    return;
  }
  messages.push(item);
  mergeSources.push(sourceMessageId);
}

function contentBlocks(content) {
  if (Array.isArray(content)) return content;
  if (typeof content === "string") return content ? [{ type: "text", text: content }] : [];
  if (content == null) return [];
  return [content];
}

function displayModelName(resolvedModel) {
  if (!resolvedModel) return "";
  for (const [rawModel, displayName] of configuredModelAliases()) {
    if (modelMatchesAlias(resolvedModel, rawModel)) return displayName;
  }
  return resolvedModel;
}

function configuredModelAliases() {
  const aliases = [];
  for (const name of MODEL_ALIAS_ENV_NAMES) {
    const model = (process.env[name] || "").trim();
    if (model) aliases.push([model, model]);
  }
  const customModel = (process.env.ANTHROPIC_CUSTOM_MODEL_OPTION || "").trim();
  if (customModel) aliases.push([customModel, (process.env.ANTHROPIC_CUSTOM_MODEL_OPTION_NAME || "").trim() || customModel]);
  return aliases;
}

function modelMatchesAlias(resolvedModel, alias) {
  return resolvedModel === alias || new RegExp(`^${escapeRegExp(alias)}-\\d{4}-\\d{2}-\\d{2}$`).test(resolvedModel);
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function jieliThreadId(sessionId) {
  const value = String(sessionId || "").trim();
  return value && !value.startsWith("T-") ? `T-${value}` : value;
}

function claudeSessionId(providerThreadId) {
  const value = String(providerThreadId || "").trim();
  return value.startsWith("T-") ? value.slice(2) : value;
}

function titleFromMessages(messages) {
  for (const message of messages) {
    if (message.role !== "user") continue;
    if (typeof message.content === "string" && message.content.trim() && message.content !== COMPACTION_PLACEHOLDER) {
      return message.content.trim().slice(0, 80);
    }
  }
  return "Claude Code session";
}

function timestampMs(value) {
  if (typeof value !== "string" || !value) return 0;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? 0 : ms;
}

function gitOutput(cwd, args) {
  const result = spawnSync("git", args, { cwd: cwd || undefined, encoding: "utf8", timeout: 2000 });
  return result.status === 0 ? result.stdout.trim() : "";
}

function repoUrlFromCwd(cwd) {
  return gitOutput(cwd, ["config", "--get", "remote.origin.url"]);
}

function gitBranch(cwd) {
  return gitOutput(cwd, ["rev-parse", "--abbrev-ref", "HEAD"]);
}

function acquireSyncLock(sessionId) {
  const safe = String(sessionId || "").replace(/[^A-Za-z0-9_-]/g, "");
  const path = join(homeDir(), ".jieli", safe ? `sync-${safe}.lock` : "sync.lock");
  mkdirSync(dirname(path), { recursive: true });
  try {
    if (existsSync(path) && Date.now() / 1000 - statSync(path).mtimeMs / 1000 > LOCK_TTL_SECONDS) unlinkSync(path);
  } catch {}
  try {
    const fd = openSync(path, "wx", 0o600);
    writeFileSync(fd, JSON.stringify({ pid: process.pid, timestamp: Date.now() / 1000 }));
    return { acquired: true, path };
  } catch {
    return { acquired: false, path };
  }
}

function releaseSyncLock(lock) {
  if (!lock.acquired) return;
  try {
    unlinkSync(lock.path);
  } catch {}
}

async function waitForTranscriptFlush(path) {
  let previous = transcriptSignature(path);
  if (!previous) return;
  const deadline = Date.now() + TRANSCRIPT_FLUSH_TIMEOUT_MS;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, TRANSCRIPT_QUIET_MS));
    const current = transcriptSignature(path);
    if (!current || current === previous) return;
    previous = current;
  }
}

function transcriptSignature(path) {
  try {
    const stat = statSync(path);
    return `${stat.size}:${stat.mtimeMs}`;
  } catch {
    return "";
  }
}

async function uploadPayload(payload, baseUrl, apiKey) {
  const response = await fetch(`${baseUrl.replace(/\/+$/, "")}/plugin/threads/upload`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(20000),
  });
  if (!response.ok) throw new Error(await formatHttpError(response));
  return response.json();
}

async function uploadAttachment(path, baseUrl, apiKey) {
  const mediaType = mediaTypeForImage(path);
  if (!mediaType) throw new Error("unsupported image media type");
  return await uploadAttachmentData(readFileSync(path), mediaType, baseUrl, apiKey);
}

async function uploadAttachmentData(data, mediaType, baseUrl, apiKey) {
  const response = await fetch(`${baseUrl.replace(/\/+$/, "")}/plugin/attachments`, {
    method: "POST",
    headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
    body: JSON.stringify({ data: Buffer.from(data).toString("base64"), mediaType }),
    signal: AbortSignal.timeout(20000),
  });
  if (!response.ok) throw new Error(await formatHttpError(response));
  const body = await response.json();
  if (!body || typeof body.url !== "string" || !body.url) throw new Error("attachment upload response missing url");
  return body.url;
}

async function uploadAttachmentCached(path, baseUrl, apiKey, uploadFn = uploadAttachment) {
  const mediaType = mediaTypeForImage(path);
  if (!mediaType) throw new Error("unsupported image media type");
  const digest = createHash("sha256").update(readFileSync(path)).digest("hex");
  const cacheKey = [baseUrl.replace(/\/+$/, ""), mediaType, digest].join("|");
  const cachePath = join(homeDir(), ".jieli", ATTACHMENT_CACHE_FILE);
  const cache = readJson(cachePath, {});
  if (typeof cache[cacheKey] === "string" && cache[cacheKey]) return cache[cacheKey];
  const url = await uploadFn(path, baseUrl, apiKey);
  cache[cacheKey] = url;
  writeJsonAtomic(cachePath, cache);
  return url;
}

async function formatHttpError(response) {
  let body = "";
  try {
    body = await response.text();
  } catch {}
  const suffix = body ? `: ${redactText(body).slice(0, 1000)}` : "";
  return `HTTP ${response.status}${suffix}`;
}

function writeSessionMapping(sessionId, baseUrl, providerThreadId = "") {
  const path = join(homeDir(), ".jieli", "claude-sessions.json");
  const mapping = readJson(path, {});
  mapping[sessionId] = {
    provider_thread_id: providerThreadId || jieliThreadId(sessionId),
    base_url: baseUrl.replace(/\/+$/, ""),
  };
  writeJsonAtomic(path, mapping);
}

function logHookError(message) {
  const path = join(homeDir(), ".jieli", "hooks.log");
  mkdirSync(dirname(path), { recursive: true });
  const stamp = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  writeFileSync(path, `[${stamp}] ${message}\n`, { flag: "a", encoding: "utf8" });
}

function commitTrailerMain(args) {
  parseArgs(args, { boolean: new Set(["jieli-hook"]) });
  let response = {};
  try {
    response = buildHookResponse(JSON.parse(readStdin() || "{}"));
  } catch {
    response = {};
  }
  if (Object.keys(response).length) console.log(JSON.stringify(response));
  return 0;
}

function buildHookResponse(hookData) {
  if (hookData.tool_name !== "Bash") return {};
  const command = hookData.tool_input?.command;
  if (typeof command !== "string" || !command) return {};
  let updated = updatedHandoffCommand(command, hookData);
  if (!updated) updated = updatedCommitCommand(command, hookData.session_id || "");
  if (!updated) return {};
  return {
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "allow",
      updatedInput: { command: updated },
    },
  };
}

function updatedHandoffCommand(command, hookData) {
  const helper = resolvedHandoffHelperCommand(command);
  if (!helper) return "";
  const context = {
    session_id: String(hookData.session_id || ""),
    transcript_path: String(hookData.transcript_path || hookData.session_path || ""),
    cwd: String(hookData.cwd || ""),
  };
  const encoded = Buffer.from(JSON.stringify(context), "utf8").toString("base64");
  return `${HANDOFF_CONTEXT_ENV}=${quoteShell(encoded)} ${helper}`;
}

function resolvedHandoffHelperCommand(command) {
  if (command.includes(HANDOFF_CONTEXT_ENV) || AMBIGUOUS_TOKENS.some((token) => command.includes(token))) return "";
  const parts = shellSplit(command);
  if (!parts) return "";
  if (parts.length === 1 && basename(parts[0]) === HANDOFF_HELPER_COMMAND) {
    return `node ${quoteShell(join(pluginRoot, "scripts", "jieli_node.mjs"))} handoff-info`;
  }
  return "";
}

function updatedCommitCommand(command, sessionId) {
  if (!command || !sessionId || AMBIGUOUS_TOKENS.some((token) => command.includes(token))) return "";
  const mapping = readJson(join(homeDir(), ".jieli", "claude-sessions.json"), {});
  const session = mapping[sessionId];
  if (!session || typeof session !== "object") return "";
  const baseUrl = String(session.base_url || "").replace(/\/+$/, "");
  const providerThreadId = normalizeThreadId(String(session.provider_thread_id || sessionId));
  if (!baseUrl || !providerThreadId) return "";
  return injectTrailer(command, `${TRAILER_KEY}: ${baseUrl}/threads/${providerThreadId}`);
}

function injectTrailer(command, trailer) {
  const parts = splitTopLevelAndChain(command);
  if (!parts.length) return "";
  const updatedParts = [];
  let count = 0;
  for (const part of parts) {
    if (part === "&&") {
      updatedParts.push(part);
      continue;
    }
    const updated = appendTrailerToCommitSegment(part, trailer);
    if (updated) {
      count += 1;
      if (count > 1) return "";
      updatedParts.push(updated);
    } else {
      updatedParts.push(part);
    }
  }
  return count === 1 ? updatedParts.join("") : "";
}

function splitTopLevelAndChain(command) {
  const parts = [];
  let start = 0;
  let quote = "";
  let escaped = false;
  for (let i = 0; i < command.length; i += 1) {
    const char = command[i];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (char === "\\") {
      escaped = true;
      continue;
    }
    if (quote) {
      if (char === quote) quote = "";
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
      continue;
    }
    if (command.startsWith("&&", i)) {
      parts.push(command.slice(start, i), "&&");
      i += 1;
      start = i + 1;
      continue;
    }
    if (char === "&") return [];
  }
  if (quote || escaped) return [];
  parts.push(command.slice(start));
  return parts;
}

function appendTrailerToCommitSegment(segment, trailer) {
  const parts = shellSplit(segment);
  if (!parts || parts.length < 2 || parts[0] !== "git" || parts[1] !== "commit") return "";
  if (parts.some((part) => part.includes(TRAILER_KEY))) return "";
  const pathspecIndex = findStandaloneDoubleDash(segment);
  if (pathspecIndex >= 0) {
    return `${segment.slice(0, pathspecIndex).trimEnd()} --trailer "${trailer}" ${segment.slice(pathspecIndex).trimStart()}`;
  }
  return `${segment} --trailer "${trailer}"`;
}

function findStandaloneDoubleDash(command) {
  let quote = "";
  let escaped = false;
  for (let i = 0; i < command.length; i += 1) {
    const char = command[i];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (char === "\\") {
      escaped = true;
      continue;
    }
    if (quote) {
      if (char === quote) quote = "";
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
      continue;
    }
    if (command.startsWith("--", i)) {
      const before = i === 0 || /\s/.test(command[i - 1]);
      const afterIndex = i + 2;
      const after = afterIndex === command.length || /\s/.test(command[afterIndex]);
      if (before && after) return i;
      i += 1;
    }
  }
  return -1;
}

function normalizeThreadId(threadId) {
  const value = String(threadId || "").trim();
  return value && !value.startsWith("T-") ? `T-${value}` : value;
}

function shellSplit(command) {
  const parts = [];
  let current = "";
  let quote = "";
  let escaped = false;
  for (const char of command.trim()) {
    if (escaped) {
      current += char;
      escaped = false;
      continue;
    }
    if (char === "\\") {
      escaped = true;
      continue;
    }
    if (quote) {
      if (char === quote) quote = "";
      else current += char;
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
      continue;
    }
    if (/\s/.test(char)) {
      if (current) {
        parts.push(current);
        current = "";
      }
      continue;
    }
    current += char;
  }
  if (quote || escaped) return null;
  if (current) parts.push(current);
  return parts;
}

function quoteShell(value) {
  if (/^[A-Za-z0-9_/:=.,@%+-]+$/.test(value)) return value;
  return `'${String(value).replace(/'/g, "'\\''")}'`;
}

async function readThreadMain(args) {
  if (args.includes("--help") || args.includes("-h")) {
    console.log("Read a Jieli thread export.");
    return 0;
  }
  const opts = parseArgs(args, { boolean: new Set(["truncate-tool-results"]) });
  const threadId = opts._[0] || "";
  try {
    const baseUrl = optionalEnv("JIELI_BASE_URL", "CLAUDE_PLUGIN_OPTION_BASE_URL") || DEFAULT_BASE_URL;
    const apiKey = requiredEnv("JIELI_API_KEY", "CLAUDE_PLUGIN_OPTION_API_KEY");
    const content = await fetchThreadExport(threadId, baseUrl, apiKey, opts.format || "md", Boolean(opts.truncateToolResults));
    process.stdout.write(limitOutput(content, intOpt(opts.startLine), intOpt(opts.endLine), intOpt(opts.maxChars, 12000)));
    return 0;
  } catch (error) {
    console.error(`read_thread failed: ${formatError(error)}`);
    return 1;
  }
}

function validateThreadId(threadId) {
  const value = String(threadId || "").trim();
  if (!value) throw new Error("thread_id is required");
  if (value.includes("://") || value.includes("/") || value.includes("\\")) throw new Error("pass only the provider thread id, not a /threads/... URL");
  if (value.endsWith(".md") || value.endsWith(".json")) throw new Error("pass the provider thread id without .md or .json");
  if (/\s/.test(value)) throw new Error("thread_id must not contain whitespace");
  return value;
}

async function fetchThreadExport(threadId, baseUrl, apiKey, format = "md", truncateToolResults = false) {
  const cleanId = validateThreadId(threadId);
  if (!["md", "json"].includes(format)) throw new Error("export_format must be md or json");
  let url = `${baseUrl.replace(/\/+$/, "")}/threads/${encodeURIComponent(cleanId)}.${format}`;
  if (format === "md" && truncateToolResults) url += "?truncate_tool_results=1";
  const response = await fetch(url, {
    headers: { Authorization: `Bearer ${apiKey}` },
    signal: AbortSignal.timeout(20000),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.text();
}

function limitOutput(content, startLine = null, endLine = null, maxChars = 12000) {
  if (startLine !== null && startLine < 1) throw new Error("--start-line must be >= 1");
  if (endLine !== null && endLine < 1) throw new Error("--end-line must be >= 1");
  if (startLine !== null && endLine !== null && endLine < startLine) throw new Error("--end-line must be >= --start-line");
  if (maxChars !== null && maxChars < 0) throw new Error("--max-chars must be >= 0");
  let selected = content;
  if (startLine !== null || endLine !== null) {
    const lines = content.match(/[^\n]*\n|[^\n]+/g) || [];
    selected = lines.slice((startLine || 1) - 1, endLine ?? lines.length).join("");
  }
  if (maxChars && selected.length > maxChars) {
    selected = `${selected.slice(0, maxChars)}\n\n[Content truncated at ${maxChars} chars; rerun with --start-line/--end-line or increase --max-chars.]`;
  }
  return selected;
}

async function findThreadsMain(args) {
  if (args.includes("--help") || args.includes("-h")) {
    console.log("Find Jieli threads.");
    return 0;
  }
  const opts = parseArgs(args);
  try {
    const baseUrl = optionalEnv("JIELI_BASE_URL", "CLAUDE_PLUGIN_OPTION_BASE_URL") || DEFAULT_BASE_URL;
    const apiKey = requiredEnv("JIELI_API_KEY", "CLAUDE_PLUGIN_OPTION_API_KEY");
    const payload = await fetchThreads(opts._[0] || "", baseUrl, apiKey, opts);
    if ((opts.format || "markdown") === "json") console.log(JSON.stringify(payload, null, 2));
    else process.stdout.write(formatThreadsMarkdown(payload, baseUrl));
    return 0;
  } catch (error) {
    console.error(`find_threads failed: ${formatError(error)}`);
    return 1;
  }
}

async function fetchThreads(query, baseUrl, apiKey, opts) {
  const params = new URLSearchParams({ search: query, page_size: String(opts.pageSize || 10), sort: opts.sort || "updated" });
  if (opts.provider) params.set("provider", opts.provider);
  if (opts.repo) params.set("repo", opts.repo);
  if (opts.label) params.set("label", opts.label);
  if (opts.page) params.set("page", String(opts.page));
  const response = await fetch(`${baseUrl.replace(/\/+$/, "")}/plugin/threads?${params}`, {
    headers: { Authorization: `Bearer ${apiKey}` },
    signal: AbortSignal.timeout(20000),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function threadList(payload) {
  if (payload?.data && Array.isArray(payload.data.threads)) return payload.data.threads.filter((item) => item && typeof item === "object");
  if (Array.isArray(payload?.threads)) return payload.threads.filter((item) => item && typeof item === "object");
  return [];
}

function firstText(thread, ...keys) {
  for (const key of keys) {
    const value = thread[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function formatThreadsMarkdown(payload, baseUrl) {
  const threads = threadList(payload);
  if (!threads.length) return "No matching Jieli threads found.\n";
  const lines = [];
  threads.forEach((thread, index) => {
    const threadId = firstText(thread, "provider_thread_id", "id", "thread_id");
    const title = firstText(thread, "title") || "Untitled thread";
    const provider = firstText(thread, "provider") || "unknown";
    const repo = firstText(thread, "repo");
    const branch = firstText(thread, "branch");
    const updated = firstText(thread, "updated_at", "updated", "updatedAt");
    const preview = firstText(thread, "preview", "summary", "snippet");
    const messageCount = thread.message_count ?? thread.messages_count ?? thread.messageCount ?? "";
    const repoBranch = branch ? (repo ? `${repo}@${branch}` : branch) : repo;
    lines.push(`${index + 1}. ${title}`, `   provider: ${provider}`);
    if (threadId) lines.push(`   thread_id: ${threadId}`);
    if (repoBranch) lines.push(`   repo: ${repoBranch}`);
    if (updated) lines.push(`   updated: ${updated}`);
    if (messageCount !== "") lines.push(`   messages: ${messageCount}`);
    if (preview) lines.push(`   preview: ${preview}`);
    if (threadId) lines.push(`   read_url: ${baseUrl.replace(/\/+$/, "")}/threads/${encodeURIComponent(threadId)}`);
  });
  return `${lines.join("\n")}\n`;
}

function handoffInfoMain(args = []) {
  if (args.includes("--help") || args.includes("-h")) {
    console.log("usage: jieli-handoff-info");
    return 0;
  }
  console.log(JSON.stringify(buildHandoffInfo(), Object.keys(buildHandoffInfo()).sort()));
  return 0;
}

function buildHandoffInfo(env = process.env) {
  const context = decodeContext(env[HANDOFF_CONTEXT_ENV] || "");
  if (!context) return missingInfo("missing hook context");
  const sessionId = String(context.session_id || "").trim();
  if (!sessionId) return missingInfo("missing session_id in hook context");
  const cwd = String(context.cwd || "").trim();
  const transcriptPath = String(context.transcript_path || "").trim();
  const baseUrl = (optionalEnv("JIELI_BASE_URL", "CLAUDE_PLUGIN_OPTION_BASE_URL") || DEFAULT_BASE_URL).replace(/\/+$/, "");
  const threadId = jieliThreadId(sessionId);
  return {
    confidence: "high",
    provider: PROVIDER,
    session_id: sessionId,
    thread_id: threadId,
    url: baseUrl && threadId ? `${baseUrl}/threads/${threadId}` : "",
    base_url: baseUrl,
    cwd,
    transcript_path: transcriptPath,
    repo_url: repoUrlFromCwd(cwd),
    branch: gitBranch(cwd),
    worktree_status: worktreeStatus(cwd),
    reason: "hook context injected by PreToolUse",
  };
}

function decodeContext(encoded) {
  if (!String(encoded || "").trim()) return null;
  try {
    const value = JSON.parse(Buffer.from(encoded, "base64").toString("utf8"));
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

function missingInfo(reason) {
  return {
    confidence: "missing",
    provider: PROVIDER,
    session_id: "",
    thread_id: "",
    url: "",
    base_url: "",
    cwd: "",
    transcript_path: "",
    repo_url: "",
    branch: "",
    worktree_status: "unknown",
    reason,
  };
}

function worktreeStatus(cwd) {
  if (!cwd) return "unknown";
  const result = spawnSync("git", ["status", "--porcelain"], { cwd, encoding: "utf8", timeout: 2000 });
  if (result.status !== 0) return "unknown";
  return result.stdout.trim() ? "dirty" : "clean";
}

function intOpt(value, fallback = null) {
  if (value == null || value === "") return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function formatError(error) {
  if (!error) return "";
  return error.message || String(error);
}

export {
  COMPACTION_PLACEHOLDER,
  acquireSyncLock,
  buildHandoffInfo,
  buildHookResponse,
  buildMissingConfigHookResponse,
  buildPayloadFromHook,
  fetchThreadExport,
  fetchThreads,
  formatError,
  formatThreadsMarkdown,
  isSecretFilePath,
  limitOutput,
  missingConfigVars,
  optionalEnv,
  parseTranscript,
  readJson,
  redactJson,
  redactText,
  releaseSyncLock,
  requiredEnv,
  uploadAttachmentCached,
  uploadAttachmentData,
  uploadPayload,
  validateThreadId,
  waitForTranscriptFlush,
  writeSessionMapping,
};

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exit(await main());
}
