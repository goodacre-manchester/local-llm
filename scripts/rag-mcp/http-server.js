/**
 * RAG MCP Server (Streamable HTTP transport).
 *
 * Companion to index.js (stdio). Exposes a SINGLE retrieval-only tool —
 * `rag_search` — over HTTP so external agents (e.g. a Claude Code
 * instance on another LAN host, the Anthropic SDK, Claude Desktop with
 * `mcpServers` HTTP config) can ground on local RAG collections without
 * spending an LLM call on synthesis.
 *
 * Transport endpoint: POST <host>:<port>/mcp (and GET /mcp for SSE).
 * Stateless mode (no session IDs) — every request is independent and
 * idempotent, which matches retrieval semantics.
 *
 * Environment:
 *   RAG_BASE_URL       upstream rag-server URL (default http://127.0.0.1:3000)
 *   RAG_API_KEY        optional bearer token forwarded to rag-server
 *   MCP_HTTP_PORT      listen port (default 3001)
 *   MCP_HTTP_HOST      bind address (default 0.0.0.0 — LAN-accessible)
 *   MCP_AUTH_TOKEN     optional bearer token clients must present
 */

import http from "node:http";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const RAG_BASE = process.env.RAG_BASE_URL || "http://127.0.0.1:3000";
const RAG_API_KEY = process.env.RAG_API_KEY || "";
const PORT = Number(process.env.MCP_HTTP_PORT || 3001);
const HOST = process.env.MCP_HTTP_HOST || "0.0.0.0";
const AUTH_TOKEN = process.env.MCP_AUTH_TOKEN || "";

/** Headers forwarded to the rag-server. */
function ragHeaders(extra = {}) {
  const h = { ...extra };
  if (RAG_API_KEY) h.Authorization = `Bearer ${RAG_API_KEY}`;
  return h;
}

// ─── Cached collection list (refreshes lazily) ───────────────────────────────

let _collections = null;
let _collectionsFetchedAt = 0;
const COLLECTIONS_TTL_MS = 30_000;

async function listCollections() {
  if (_collections && Date.now() - _collectionsFetchedAt < COLLECTIONS_TTL_MS) {
    return _collections;
  }
  const r = await fetch(`${RAG_BASE}/v1/models`, { headers: ragHeaders() });
  if (!r.ok) throw new Error(`rag-server /v1/models returned ${r.status}`);
  const data = await r.json();
  // Strip the "!deep" suffix variants and the synthetic "rag-active" handle —
  // those map to underlying collections that are already enumerated separately.
  const set = new Set(
    (data.data || [])
      .map((m) => m.id)
      .filter((id) => !id.endsWith("!deep") && id !== "rag-active")
  );
  _collections = [...set].sort();
  _collectionsFetchedAt = Date.now();
  return _collections;
}

// ─── MCP server factory ──────────────────────────────────────────────────────
//
// In stateless Streamable HTTP mode the recommended pattern is one
// (Server, Transport) pair PER request, so cross-request state can't leak
// (e.g. the transport closing one response shouldn't tear down the next).
// We share only the collection cache and the upstream fetch helpers above.

function buildServer() {
const server = new Server(
  { name: "local-rag-http", version: "1.0.0" },
  {
    capabilities: { tools: {} },
    instructions:
      "Local RAG retrieval over HTTP. Call rag_search(collection, query) " +
      "to retrieve top-K chunks (text + section + page + score + optional " +
      "github_url for source-tree collections) from a named collection. " +
      "The host application synthesises the final answer.\n\n" +
      "Query patterns:\n" +
      "- Two-pass: broad first to map the territory, then narrow with " +
      "section_filter (a glob over the chunk's section header, e.g. " +
      "\"8.6.9.4.*\" or \"*Hot-Plug*\") to grab verbatim subclause chunks.\n" +
      "- Drill in on named artifacts: if a chunk names a state machine, " +
      "parameter, subclause, or data set you don't already recognize, run " +
      "a follow-up rag_search with that exact name before concluding the " +
      "standard doesn't define a related concept.\n" +
      "- Specs have edition lineage: the IEEE collection includes both " +
      "base specs and amendments (e.g. 802.1AS-2025 absorbs 802.1ASdm-2024); " +
      "a feature added by an amendment is normative in the rolled-in " +
      "edition. Search both lineages.\n\n" +
      "Epistemic stance: positive claims should cite a retrieved chunk " +
      "verbatim. Treat \"the standard does not define X\" as a strong " +
      "claim that requires an exhausted, name-specific search — and " +
      "prefer phrasing it as \"did not retrieve\" rather than \"does not " +
      "exist.\"\n\n" +
      "Collections cover: AMD/Xilinx UG/PG docs (amd); IEEE 802.x + " +
      "selected RFCs (ieee); Linux kernel subsystems (linux-bt/core/cxl/" +
      "fs/mm/net/pci/usb); nginx source; PCIe/CXL/USB/HID/BT base specs " +
      "(seccom). A question may benefit from multiple collections — e.g. " +
      "a Linux PCI question often pairs linux-pci (kernel) with seccom " +
      "(spec).",
  }
);

server.setRequestHandler(ListToolsRequestSchema, async () => {
  let cols;
  try {
    cols = await listCollections();
  } catch (_) {
    cols = [];
  }
  const enumClause =
    cols.length > 0 ? ` Available: ${cols.join(", ")}.` : "";
  return {
    tools: [
      {
        name: "rag_search",
        description:
          "Retrieve the top-K most-relevant chunks for a query from a " +
          "named local RAG collection. Returns text + source metadata " +
          "(file, section, page, score, github_url where applicable) so " +
          "the caller can synthesise its own grounded answer." +
          enumClause,
        inputSchema: {
          type: "object",
          properties: {
            collection: {
              type: "string",
              description:
                "Name of the RAG collection to search. Discoverable via " +
                "GET /v1/models on the rag-server." +
                (cols.length ? ` One of: ${cols.join(", ")}.` : ""),
              ...(cols.length ? { enum: cols } : {}),
            },
            query: {
              type: "string",
              description: "Natural-language query string.",
            },
            top_k: {
              type: "number",
              description:
                "Number of chunks to return (default 12, max 40).",
              minimum: 1,
              maximum: 40,
              default: 12,
            },
            section_filter: {
              type: "string",
              description:
                "Optional glob over the chunk's `section` metadata, applied " +
                "after retrieval and before rerank. Use when you already know " +
                "the structural location to focus on, e.g. \"8.6.9.4.*\" for " +
                "an IEEE 802.1Q subclause family, \"*Hot-Plug*\" for any " +
                "section whose header contains that token, or " +
                "\"drivers/pci/hotplug/*\" for a source-tree subtree. " +
                "Wildcards: `*` matches any run of characters, `?` matches " +
                "one. Match is start-anchored and case-insensitive.",
            },
          },
          required: ["collection", "query"],
        },
      },
    ],
  };
});

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args } = req.params;
  if (name !== "rag_search") {
    return {
      content: [{ type: "text", text: `Unknown tool: ${name}` }],
      isError: true,
    };
  }

  const collection = args?.collection;
  const query = args?.query;
  const topK = Math.max(1, Math.min(Number(args?.top_k ?? 12), 40));
  const sectionFilter = args?.section_filter
    ? String(args.section_filter)
    : null;
  if (!collection || !query) {
    return {
      content: [
        {
          type: "text",
          text: 'rag_search requires both "collection" and "query".',
        },
      ],
      isError: true,
    };
  }

  // Validate collection. If unknown, refresh the cache and try once more.
  let cols = await listCollections();
  if (!cols.includes(collection)) {
    _collectionsFetchedAt = 0;
    cols = await listCollections();
    if (!cols.includes(collection)) {
      return {
        content: [
          {
            type: "text",
            text: `Unknown collection "${collection}". Available: ${cols.join(", ")}`,
          },
        ],
        isError: true,
      };
    }
  }

  const r = await fetch(`${RAG_BASE}/query`, {
    method: "POST",
    headers: ragHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      query,
      collection,
      topK,
      ...(sectionFilter ? { sectionFilter } : {}),
    }),
  });
  if (!r.ok) {
    const err = await r.text();
    return {
      content: [
        {
          type: "text",
          text: `rag-server /query returned ${r.status}: ${err}`,
        },
      ],
      isError: true,
    };
  }
  const data = await r.json();
  const matches = (data.matches || []).map((m) => {
    const meta = m.metadata || {};
    return {
      text: m.document ?? m.text,
      score: m.score ?? m.distance,
      fileName: meta.fileName,
      section: meta.section,
      page: meta.page,
      file_path: meta.file_path,
      github_url: meta.github_url,
    };
  });
  const payload = {
    collection,
    query,
    top_k: topK,
    ...(sectionFilter ? { section_filter: sectionFilter } : {}),
    matches,
  };
  return {
    content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
    structuredContent: payload,
  };
});

return server;
}

// ─── HTTP plumbing ───────────────────────────────────────────────────────────

function checkAuth(req) {
  if (!AUTH_TOKEN) return true;
  const hdr = req.headers["authorization"] || "";
  return hdr === `Bearer ${AUTH_TOKEN}`;
}

function readJSONBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      if (!raw) return resolve(undefined);
      try {
        resolve(JSON.parse(raw));
      } catch (e) {
        reject(e);
      }
    });
    req.on("error", reject);
  });
}

const httpServer = http.createServer(async (req, res) => {
  // Health probe — no auth, no MCP framing.
  if (req.url === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: true, rag: RAG_BASE }));
    return;
  }
  if (!req.url?.startsWith("/mcp")) {
    res.writeHead(404, { "Content-Type": "text/plain" });
    res.end("Not found. MCP endpoint is at /mcp; health at /health.\n");
    return;
  }
  if (!checkAuth(req)) {
    res.writeHead(401, {
      "Content-Type": "application/json",
      "WWW-Authenticate": 'Bearer realm="mcp"',
    });
    res.end(JSON.stringify({ error: "unauthorized" }));
    return;
  }
  try {
    const body = req.method === "POST" ? await readJSONBody(req) : undefined;
    // Per-request transport + server pair (stateless mode best-practice).
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: undefined,
    });
    res.on("close", () => transport.close());
    const server = buildServer();
    await server.connect(transport);
    await transport.handleRequest(req, res, body);
  } catch (e) {
    if (!res.headersSent) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: String(e?.message || e) }));
    } else {
      res.end();
    }
  }
});

httpServer.listen(PORT, HOST, () => {
  console.log(
    `[rag-mcp http] listening on http://${HOST}:${PORT}/mcp ` +
      `(rag=${RAG_BASE}${AUTH_TOKEN ? ", auth=bearer" : ", auth=none"})`
  );
});
