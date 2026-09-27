// End-to-end smoke test against the hosted server: spawn the bridge the way a
// client would, list the tools, call the free tool. Needs network access.
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const bin = fileURLToPath(new URL("../bin/apiguru-mcp.mjs", import.meta.url));
const transport = new StdioClientTransport({
  command: process.execPath,
  args: [bin, ...process.argv.slice(2)],
  stderr: "pipe",
});
transport.stderr?.on("data", (d) => process.stderr.write(`[bridge] ${d}`));

const client = new Client({ name: "apiguru-mcp-smoke", version: "0" });
let failures = 0;
const check = (label, ok, extra = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${extra ? `  (${extra})` : ""}`);
  if (!ok) failures++;
};

try {
  await client.connect(transport);
  const info = client.getServerVersion();
  check("initialize handshake", !!info, `${info?.name} ${info?.version}`);

  const { tools } = await client.listTools();
  check("12 tools listed", tools.length === 12, String(tools.length));
  const names = new Set(tools.map((t) => t.name));
  check("list_capabilities present", names.has("list_capabilities"));
  check("product_details present", names.has("product_details"));

  const unannotated = tools.filter(
    // send_feedback writes to the public wall, so readOnlyHint is false on it;
    // what the directories require is that the hint is stated, not that it is true.
    (t) =>
      !(
        t.title &&
        t.annotations &&
        typeof t.annotations.readOnlyHint === "boolean" &&
        typeof t.annotations.destructiveHint === "boolean"
      ),
  );
  console.log(
    `INFO  tools without title+readOnlyHint: ${unannotated.length} (0 once the hosted server runs >= 1.1.1)`,
  );

  const result = await client.callTool({ name: "list_capabilities", arguments: {} });
  const text = result.content?.find((c) => c.type === "text")?.text || "";
  check("list_capabilities answers", text.includes("access_mode"), text.slice(0, 60).replace(/\s+/g, " "));
  check("access mode matches APIGURU_API_KEY", /keyless/.test(text) === !process.env.APIGURU_API_KEY);
} catch (err) {
  check("no exception", false, err.message);
} finally {
  await client.close().catch(() => {});
}
process.exit(failures ? 1 : 0);
