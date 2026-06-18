import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

const EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904";
const GIT_TIMEOUT_MS = 2000;
const MAX_REPOS = 16;
const MAX_SCAN_DEPTH = 2;
const SKIP_DIRS = new Set([".git", "node_modules", ".venv", "vendor", ".cache", "Library", "dist", "build"]);

function jieliHome(home = homedir()) {
  return join(home, ".jieli");
}

function statePath(provider, home = homedir()) {
  return join(jieliHome(home), `${provider}-summary-stats.json`);
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
    chmodSync(path, mode);
  } catch {
    // chmod is best effort on Windows.
  }
}

function logSummaryStatsError(home, message) {
  try {
    const path = join(jieliHome(home), "hooks.log");
    mkdirSync(dirname(path), { recursive: true });
    const stamp = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
    writeFileSync(path, `[${stamp}] summary-stats: ${message}\n`, { flag: "a", encoding: "utf8" });
  } catch {
    // Logging must never block transcript upload.
  }
}

function git(args, cwd, options = {}) {
  return spawnSync("git", args, {
    cwd: cwd || undefined,
    env: options.env ? { ...process.env, ...options.env } : process.env,
    encoding: "utf8",
    timeout: options.timeout || GIT_TIMEOUT_MS,
    maxBuffer: 10 * 1024 * 1024,
  });
}

function gitOutput(cwd, args, options = {}) {
  const result = git(args, cwd, options);
  if (result.status !== 0 || result.error) return "";
  return String(result.stdout || "").trim();
}

function gitRoot(cwd) {
  if (!cwd || !existsSync(cwd)) return "";
  return gitOutput(cwd, ["rev-parse", "--show-toplevel"]);
}

function hasHead(repoRoot) {
  return Boolean(gitOutput(repoRoot, ["rev-parse", "--verify", "HEAD"]));
}

function headTree(repoRoot) {
  return gitOutput(repoRoot, ["rev-parse", "HEAD^{tree}"]) || EMPTY_TREE;
}

function worktreeClean(repoRoot) {
  const result = git(["status", "--porcelain"], repoRoot);
  return result.status === 0 && !result.error && String(result.stdout || "").trim() === "";
}

function snapshotWorkingTree(repoRoot) {
  const tempDir = mkdtempSync(join(tmpdir(), "jieli-summary-index-"));
  const indexPath = join(tempDir, "index");
  const env = { GIT_INDEX_FILE: indexPath };
  try {
    if (hasHead(repoRoot)) {
      const readTree = git(["read-tree", "HEAD"], repoRoot, { env });
      if (readTree.status !== 0 || readTree.error) throw new Error(`git read-tree failed: ${readTree.stderr || readTree.error?.message || ""}`);
    }
    const add = git(["add", "-A", "--", "."], repoRoot, { env });
    if (add.status !== 0 || add.error) throw new Error(`git add failed: ${add.stderr || add.error?.message || ""}`);
    const tree = gitOutput(repoRoot, ["write-tree"], { env });
    if (!/^[0-9a-f]{40}$/.test(tree)) throw new Error("git write-tree did not return a tree hash");
    return tree;
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
}

function diffTrees(repoRoot, baseTree, currentTree) {
  const result = git(["diff", "--numstat", "--find-renames", baseTree, currentTree], repoRoot);
  if (result.status !== 0 || result.error) throw new Error(`git diff failed: ${result.stderr || result.error?.message || ""}`);
  let added = 0;
  let deleted = 0;
  let filesChanged = 0;
  for (const line of String(result.stdout || "").split(/\r?\n/)) {
    if (!line.trim()) continue;
    const [rawAdded, rawDeleted] = line.split("\t");
    added += rawAdded === "-" ? 0 : Number(rawAdded) || 0;
    deleted += rawDeleted === "-" ? 0 : Number(rawDeleted) || 0;
    filesChanged += 1;
  }
  return { diffStats: { added, deleted, changed: 0 }, filesChanged };
}

function discoverRepoRoots({ cwd }) {
  const roots = [];
  const skipped = { count: 0, incomplete: false };
  const addRoot = (root) => {
    if (!root || roots.includes(root)) return;
    if (roots.length >= MAX_REPOS) {
      skipped.count += 1;
      skipped.incomplete = true;
      return;
    }
    roots.push(root);
  };

  const root = gitRoot(cwd);
  if (root) return { roots: [root], skippedRepoCount: 0, complete: true };
  if (!cwd || !existsSync(cwd)) return { roots: [], skippedRepoCount: 0, complete: true };

  const start = Date.now();
  const walk = (dir, depth) => {
    if (Date.now() - start > 1000) {
      skipped.incomplete = true;
      return;
    }
    if (depth > MAX_SCAN_DEPTH) return;
    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    const hasDotGit = entries.some((entry) => entry.name === ".git");
    if (hasDotGit) {
      addRoot(gitRoot(dir));
      return;
    }
    for (const entry of entries) {
      if (!entry.isDirectory() || SKIP_DIRS.has(entry.name)) continue;
      walk(join(dir, entry.name), depth + 1);
    }
  };
  walk(resolve(cwd), 0);
  roots.sort();
  return { roots, skippedRepoCount: skipped.count, complete: !skipped.incomplete };
}

function readState(provider, home = homedir()) {
  const state = readJson(statePath(provider, home), { entries: {} });
  if (!state.entries || typeof state.entries !== "object" || Array.isArray(state.entries)) state.entries = {};
  return state;
}

function writeState(provider, state, home = homedir()) {
  writeJsonAtomic(statePath(provider, home), state, 0o600);
}

function entryKey(threadId, repoRoot) {
  return `${threadId}|${repoRoot}`;
}

function captureSummaryStatsBaseline({ provider, threadId, sessionId = "", cwd, trigger = "unknown", home = homedir() }) {
  try {
    if (!provider || !threadId) return { captured: 0 };
    const discovery = discoverRepoRoots({ cwd });
    if (!discovery.roots.length) return { captured: 0 };
    const state = readState(provider, home);
    let captured = 0;
    for (const repoRoot of discovery.roots) {
      const key = entryKey(threadId, repoRoot);
      if (state.entries[key]?.baseTree) continue;
      state.entries[key] = {
        provider,
        threadId,
        sessionId,
        repoRoot,
        baseTree: snapshotWorkingTree(repoRoot),
        baseHead: gitOutput(repoRoot, ["rev-parse", "--verify", "HEAD"]),
        baseHeadTree: headTree(repoRoot),
        baseCapturedAt: new Date().toISOString(),
        baseTrigger: String(trigger || "unknown").toLowerCase(),
        complete: true,
      };
      captured += 1;
    }
    writeState(provider, state, home);
    return { captured };
  } catch (error) {
    logSummaryStatsError(home, error.message || String(error));
    return { captured: 0, error };
  }
}

function ensureBaselineEntry(state, { provider, threadId, sessionId, repoRoot, trigger }) {
  const key = entryKey(threadId, repoRoot);
  if (state.entries[key]?.baseTree) return state.entries[key];
  const entry = {
    provider,
    threadId,
    sessionId,
    repoRoot,
    baseTree: snapshotWorkingTree(repoRoot),
    baseHead: gitOutput(repoRoot, ["rev-parse", "--verify", "HEAD"]),
    baseHeadTree: headTree(repoRoot),
    baseCapturedAt: new Date().toISOString(),
    baseTrigger: String(trigger || "upload").toLowerCase(),
    complete: false,
  };
  state.entries[key] = entry;
  return entry;
}

function digestTrees(items, field) {
  const hash = createHash("sha256");
  for (const item of items) hash.update(`${item[field]}\n`);
  return `sha256:${hash.digest("hex")}`;
}

function aggregateRepoStats(repoStats, { messageCount, discovery }) {
  if (!repoStats.length) return null;
  const totals = { added: 0, deleted: 0, changed: 0 };
  let filesChanged = 0;
  let completeRepoCount = 0;
  for (const item of repoStats) {
    totals.added += item.stats.diffStats.added;
    totals.deleted += item.stats.diffStats.deleted;
    filesChanged += item.stats.filesChanged;
    if (item.complete) completeRepoCount += 1;
  }
  const complete = discovery.complete && completeRepoCount === repoStats.length;
  const base = {
    diffStats: totals,
    filesChanged,
    messageCount,
    source: "git_tree_diff",
    complete,
    repoCount: repoStats.length,
  };
  if (repoStats.length === 1) {
    base.baseTree = repoStats[0].baseTree;
    base.currentTree = repoStats[0].currentTree;
  } else {
    base.completeRepoCount = completeRepoCount;
    base.baseTreeDigest = digestTrees(repoStats, "baseTree");
    base.currentTreeDigest = digestTrees(repoStats, "currentTree");
  }
  if (discovery.skippedRepoCount) base.skippedRepoCount = discovery.skippedRepoCount;
  return base;
}

function summaryStatsForUpload({ provider, threadId, sessionId = "", cwd, messageCount = 0, trigger = "upload", home = homedir() }) {
  try {
    if (!provider || !threadId) return null;
    const discovery = discoverRepoRoots({ cwd });
    if (!discovery.roots.length) return null;
    const state = readState(provider, home);
    const repoStats = [];
    for (const repoRoot of discovery.roots) {
      const entry = ensureBaselineEntry(state, { provider, threadId, sessionId, repoRoot, trigger });
      let stats = entry.lastStats;
      let currentTree = "";
      if (stats && entry.lastCurrentTree && worktreeClean(repoRoot)) {
        const currentHeadTree = headTree(repoRoot);
        if (currentHeadTree === entry.lastCurrentTree) {
          currentTree = entry.lastCurrentTree;
        }
      }
      if (!currentTree) currentTree = snapshotWorkingTree(repoRoot);
      if (entry.lastCurrentTree !== currentTree || !stats) {
        stats = diffTrees(repoRoot, entry.baseTree, currentTree);
        entry.lastCurrentTree = currentTree;
        entry.lastStats = stats;
        entry.lastComputedAt = new Date().toISOString();
      }
      repoStats.push({ baseTree: entry.baseTree, currentTree, stats, complete: entry.complete !== false });
    }
    writeState(provider, state, home);
    return aggregateRepoStats(repoStats, { messageCount, discovery });
  } catch (error) {
    logSummaryStatsError(home, error.message || String(error));
    return null;
  }
}

export { captureSummaryStatsBaseline, summaryStatsForUpload };
