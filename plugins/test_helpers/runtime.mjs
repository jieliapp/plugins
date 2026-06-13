import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export function makeTempDir(prefix = "jieli-node-test-") {
  return mkdtempSync(join(tmpdir(), prefix));
}

export function writeJsonl(path, entries) {
  writeFileSync(path, `${entries.map((entry) => JSON.stringify(entry)).join("\n")}\n`, "utf8");
}

export async function withEnv(values, fn) {
  const previous = new Map();
  for (const key of Object.keys(values)) previous.set(key, process.env[key]);
  try {
    for (const [key, value] of Object.entries(values)) {
      if (value === undefined || value === null) delete process.env[key];
      else process.env[key] = String(value);
    }
    return await fn();
  } finally {
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
}

export function createMockJieliServer(options = {}) {
  const state = {
    uploads: [],
    attachments: [],
    threadReads: [],
    searches: [],
  };
  const server = createServer((request, response) => {
    let body = "";
    request.setEncoding("utf8");
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      const url = new URL(request.url, "http://127.0.0.1");
      const headers = Object.fromEntries(Object.entries(request.headers));
      if (request.method === "POST" && url.pathname === "/plugin/threads/upload") {
        state.uploads.push({ body: body ? JSON.parse(body) : null, headers, path: url.pathname });
        const upload = options.uploadResponse || { success: true };
        response.writeHead(options.uploadStatus || 200, { "content-type": "application/json" });
        response.end(typeof upload === "string" ? upload : JSON.stringify(upload));
        return;
      }
      if (request.method === "POST" && url.pathname === "/plugin/attachments") {
        state.attachments.push({ body: body ? JSON.parse(body) : null, headers, path: url.pathname });
        const attachment = options.attachmentResponse || {
          url: `${state.baseUrl || "http://127.0.0.1"}/attachments/${state.attachments.length}.png`,
        };
        response.writeHead(options.attachmentStatus || 200, { "content-type": "application/json" });
        response.end(typeof attachment === "string" ? attachment : JSON.stringify(attachment));
        return;
      }
      if (request.method === "GET" && url.pathname.startsWith("/threads/")) {
        state.threadReads.push({ url, headers });
        response.writeHead(options.threadStatus || 200, { "content-type": "text/plain; charset=utf-8" });
        response.end(options.threadBody || "thread body");
        return;
      }
      if (request.method === "GET" && url.pathname === "/plugin/threads") {
        state.searches.push({ url, headers });
        response.writeHead(options.searchStatus || 200, { "content-type": "application/json" });
        response.end(JSON.stringify(options.searchResponse || { data: { threads: [] } }));
        return;
      }
      response.writeHead(404, { "content-type": "text/plain" });
      response.end("not found");
    });
  });
  return { server, state };
}

export function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      resolve(`http://127.0.0.1:${address.port}`);
    });
  });
}

export function close(server) {
  return new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

export function runNode(args, options = {}) {
  return new Promise((resolve) => {
    const child = spawn("node", args, {
      env: options.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("close", (status) => resolve({ status, stdout, stderr }));
    child.stdin.end(options.input || "");
  });
}

export function decodeHandoffContext(command) {
  const match = /JIELI_HANDOFF_CONTEXT_B64=(?:'([^']+)'|([^\s]+))/.exec(command);
  if (!match) throw new Error("handoff context not found");
  return JSON.parse(Buffer.from(match[1] || match[2], "base64").toString("utf8"));
}
