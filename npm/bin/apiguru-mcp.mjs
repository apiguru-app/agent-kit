#!/usr/bin/env node
/**
 * apiguru-mcp: the Apiguru Amazon Data MCP server from npx, with nothing but
 * Node installed.
 *
 *   npx apiguru-mcp                  # stdio <-> https://mcp.apiguru.app/mcp
 *   npx apiguru-mcp --url <mcp url>  # bridge to another streamable-HTTP server
 *   npx apiguru-mcp --local          # run the Python server via uvx (needs uv)
 *
 * The default mode is a transparent stdio-to-streamable-HTTP bridge. Every
 * JSON-RPC message from the local client is POSTed to the hosted server and
 * every reply is written back, so the client sees exactly the 11 tools that
 * `uvx apiguru-mcp` would serve locally. The Python package is a thin HTTP
 * client over the same gateway anyway, so nothing is lost by running it on
 * our side instead of yours.
 *
 * Free probes are counted per caller, because the connection to the gateway
 * originates from THIS machine, not from a shared vendor address.
 *
 * Environment:
 *   APIGURU_API_KEY   optional; sent as X-API-KEY so calls bill that account
 *                     instead of using the free-probe-then-402 path
 *   APIGURU_MCP_URL   optional; same as --url
 *   APIGURU_MCP_DEBUG set to anything to log transport noise to stderr
 */
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const { version } = createRequire(import.meta.url)("../package.json");
const DEFAULT_URL = "https://mcp.apiguru.app/mcp";
const ACCOUNT_URL = "https://mcp.apiguru.app/account";

const argv = process.argv.slice(2);
const has = (flag) => argv.includes(flag);
const valueOf = (flag) => {
  const i = argv.indexOf(flag);
  if (i === -1) return undefined;
  const v = argv[i + 1];
  if (v === undefined || v.startsWith("--")) fail(`${flag} needs a value`);
  return v;
};

function fail(message, code = 2) {
  process.stderr.write(`apiguru-mcp: ${message}\n`);
  process.exit(code);
}

if (has("--help") || has("-h")) {
  process.stdout.write(
    [
      `apiguru-mcp ${version}`,
      "",
      "Usage:",
      `  apiguru-mcp                 bridge stdio to ${DEFAULT_URL}`,
      "  apiguru-mcp --url <url>     bridge stdio to another MCP streamable-HTTP URL",
      "  apiguru-mcp --local [...]   run the Python server with uvx instead (needs uv;",
      "                              extra arguments are passed through to it)",
      "",
      "Environment:",
      "  APIGURU_API_KEY   bill an Apiguru account (sent as X-API-KEY); omit to stay keyless",
      "  APIGURU_MCP_URL   default for --url",
      "",
      "Docs: https://github.com/apiguru-app/agent-kit",
      "",
    ].join("\n"),
  );
  process.exit(0);
}
if (has("--version") || has("-V")) {
  process.stdout.write(`${version}\n`);
  process.exit(0);
}

if (has("--local")) {
  runLocal(argv.filter((a) => a !== "--local"));
} else {
  await runBridge();
}

// ---------------------------------------------------------------------------

function runLocal(args) {
  const child = spawn("uvx", ["apiguru-mcp", ...args], { stdio: "inherit" });
  child.on("error", (err) => {
    if (err.code === "ENOENT") {
      fail(
        "`uvx` was not found. Install uv (https://docs.astral.sh/uv/) or drop " +
          "--local to use the hosted server, which needs nothing else.",
        127,
      );
    }
    fail(err.message, 1);
  });
  child.on("exit", (code, signal) => process.exit(code ?? (signal ? 1 : 0)));
  for (const sig of ["SIGINT", "SIGTERM"]) {
    process.on(sig, () => child.kill(sig));
  }
}

async function runBridge() {
  let url;
  try {
    url = new URL(valueOf("--url") || process.env.APIGURU_MCP_URL || DEFAULT_URL);
  } catch {
    fail("--url must be an absolute http(s) URL");
  }

  const headers = {
    "User-Agent": `apiguru-mcp-npm/${version} node/${process.versions.node}`,
  };
  const apiKey = (process.env.APIGURU_API_KEY || "").trim();
  if (apiKey) headers["X-API-KEY"] = apiKey;

  const remote = new StreamableHTTPClientTransport(url, { requestInit: { headers } });
  const local = new StdioServerTransport();

  let closing = false;
  const shutdown = async (code = 0) => {
    if (closing) return;
    closing = true;
    await Promise.allSettled([remote.close(), local.close()]);
    process.exit(code);
  };

  // Server -> client: responses and server notifications pass straight through.
  remote.onmessage = (message) => {
    local.send(message).catch((err) => {
      process.stderr.write(`apiguru-mcp: cannot write to client: ${err.message}\n`);
      shutdown(1);
    });
  };
  remote.onerror = (err) => {
    // Transport noise that is not tied to a request, e.g. the optional GET
    // stream that a stateless server answers with 405. Failing requests are
    // answered individually below, so this is only logged on demand.
    if (process.env.APIGURU_MCP_DEBUG) {
      process.stderr.write(`apiguru-mcp: remote: ${err.message}\n`);
    }
  };
  remote.onclose = () => shutdown(0);

  // Client -> server: a request that cannot reach the server gets a JSON-RPC
  // error back instead of leaving the client waiting forever.
  local.onmessage = async (message) => {
    try {
      await remote.send(message);
    } catch (err) {
      const isRequest =
        message && typeof message === "object" && "method" in message && "id" in message;
      const detail = describe(err, url);
      if (isRequest) {
        await local
          .send({ jsonrpc: "2.0", id: message.id, error: { code: -32000, message: detail } })
          .catch(() => {});
      } else {
        process.stderr.write(`apiguru-mcp: ${detail}\n`);
      }
    }
  };
  local.onerror = (err) => process.stderr.write(`apiguru-mcp: stdio: ${err.message}\n`);
  local.onclose = () => shutdown(0);

  for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => shutdown(0));

  await remote.start();
  await local.start();
}

function describe(err, url) {
  const msg = err && err.message ? err.message : String(err);
  if (err && err.name === "UnauthorizedError") {
    return (
      `${url.host} requires a sign-in this bridge cannot perform. Use the keyless ` +
      `${DEFAULT_URL}, or add ${ACCOUNT_URL} as a connector in a client that supports OAuth.`
    );
  }
  if (/ENOTFOUND|ECONNREFUSED|EAI_AGAIN|fetch failed/i.test(msg)) {
    return `cannot reach ${url.host}: ${msg}. Check the network, or run with --local to use the Python server via uvx.`;
  }
  return `bridge to ${url.host} failed: ${msg}`;
}
