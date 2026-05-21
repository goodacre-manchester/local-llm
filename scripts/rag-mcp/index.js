/**
 * RAG MCP Server — exposes local-llm RAG collections as Copilot tools.
 *
 * Tools:
 *   query_pdfs          – ask a question; returns a full RAG+LLM answer
 *   list_collections    – list available collections and which is active
 *   set_active_collection – switch the active collection (no re-indexing)
 *   ingest_collection   – trigger indexing for a collection after adding PDFs
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const RAG_BASE = process.env.RAG_BASE_URL || "http://127.0.0.1:3000";
const RAG_API_KEY = process.env.RAG_API_KEY || "";

/** Headers for RAG server requests, including auth when RAG_API_KEY is set. */
function ragHeaders(extra = {}) {
  const h = { ...extra };
  if (RAG_API_KEY) h.Authorization = `Bearer ${RAG_API_KEY}`;
  return h;
}

// ─── Server definition ────────────────────────────────────────────────────────

const server = new Server(
  { name: "local-rag", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

// ─── Tool list ────────────────────────────────────────────────────────────────

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "query_pdfs",
      description:
        "Ask a question about documents in a PDF collection. " +
        "The server retrieves the most relevant chunks and returns an LLM-generated answer with source references. " +
        "Use this whenever the user asks something that could be answered from their local PDF documents.",
      inputSchema: {
        type: "object",
        properties: {
          query: {
            type: "string",
            description: "The question to answer from the PDF documents.",
          },
          collection: {
            type: "string",
            description:
              'Collection name to query (e.g. "ieee", "amd"). ' +
              "Omit to use the currently active collection.",
          },
          topK: {
            type: "number",
            description:
              "Number of document chunks to retrieve for context (default 5).",
          },
          deep: {
            type: "boolean",
            description:
              "Use the slower, higher-accuracy 'deep' model instead of the " +
              "fast default. Set true for hard/technical questions where " +
              "correctness matters more than latency (default false).",
          },
        },
        required: ["query"],
      },
    },
    {
      name: "list_collections",
      description:
        "List all available PDF collections, their Chroma collection name, " +
        "whether they have been ingested, and which is currently active.",
      inputSchema: {
        type: "object",
        properties: {},
      },
    },
    {
      name: "set_active_collection",
      description:
        "Switch the active PDF collection. Switching is instant — documents are not re-indexed. " +
        "Subsequent query_pdfs calls without an explicit collection will use this one.",
      inputSchema: {
        type: "object",
        properties: {
          name: {
            type: "string",
            description: "Name of the collection to activate.",
          },
        },
        required: ["name"],
      },
    },
    {
      name: "ingest_collection",
      description:
        "Trigger PDF ingestion for a collection. Only new or changed files are processed. " +
        "Run this after dropping new PDFs into a collection folder.",
      inputSchema: {
        type: "object",
        properties: {
          name: {
            type: "string",
            description: "Collection name to ingest (e.g. \"ieee\").",
          },
          force: {
            type: "boolean",
            description: "Re-index all files even if unchanged (default false).",
          },
        },
        required: ["name"],
      },
    },
  ],
}));

// ─── Tool handlers ────────────────────────────────────────────────────────────

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args } = req.params;

  try {
    if (name === "query_pdfs") {
      const body = { query: args.query };
      if (args.collection) body.collection = args.collection;
      if (args.topK) body.topK = args.topK;

      // Use the OpenAI-compatible endpoint so we get a full LLM-synthesised answer.
      const base = args.collection || "rag-active";
      const payload = {
        model: args.deep ? `${base}!deep` : base,
        messages: [{ role: "user", content: args.query }],
        stream: false,
      };
      if (args.topK) payload.topK = args.topK;

      const res = await fetch(`${RAG_BASE}/v1/chat/completions`, {
        method: "POST",
        headers: ragHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        const err = await res.text();
        return { content: [{ type: "text", text: `RAG server error (${res.status}): ${err}` }], isError: true };
      }

      const data = await res.json();
      const answer = data.choices?.[0]?.message?.content ?? JSON.stringify(data);
      const collection = data.model ?? (args.collection || "active");
      return {
        content: [
          {
            type: "text",
            text: `**Collection: ${collection}**\n\n${answer}`,
          },
        ],
      };
    }

    if (name === "list_collections") {
      const res = await fetch(`${RAG_BASE}/collections`, { headers: ragHeaders() });
      if (!res.ok) {
        return { content: [{ type: "text", text: `RAG server error (${res.status})` }], isError: true };
      }
      const data = await res.json();
      const lines = [
        `Active collection: **${data.active ?? "none"}**`,
        "",
        "| Name | Chroma collection | Ingested |",
        "|------|------------------|----------|",
        ...(data.collections ?? []).map(
          (c) =>
            `| ${c.name}${c.active ? " ✓" : ""} | ${c.chromaCollection} | ${c.ingested ? "yes" : "no"} |`
        ),
      ];
      return { content: [{ type: "text", text: lines.join("\n") }] };
    }

    if (name === "set_active_collection") {
      const res = await fetch(`${RAG_BASE}/active-collection`, {
        method: "PUT",
        headers: ragHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ name: args.name }),
      });
      if (!res.ok) {
        const err = await res.text();
        return { content: [{ type: "text", text: `Error: ${err}` }], isError: true };
      }
      const data = await res.json();
      return {
        content: [
          {
            type: "text",
            text: data.ok
              ? `Active collection set to **${args.name}**.`
              : `Failed: ${JSON.stringify(data)}`,
          },
        ],
      };
    }

    if (name === "ingest_collection") {
      const url = `${RAG_BASE}/collections/${encodeURIComponent(args.name)}/ingest`;
      const res = await fetch(url, {
        method: "POST",
        headers: ragHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ force: args.force ?? false }),
      });
      if (!res.ok) {
        const err = await res.text();
        return { content: [{ type: "text", text: `Ingest error (${res.status}): ${err}` }], isError: true };
      }
      const data = await res.json();
      // Server returns { files, totalChunks, results: [{ fileName, chunkCount, skipped? }] }
      const results = Array.isArray(data.results) ? data.results : [];
      const skipped = results.filter((r) => r.skipped).length;
      const indexed = results.length - skipped;
      return {
        content: [
          {
            type: "text",
            text: `Ingest complete for **${args.name}**: ${data.totalChunks ?? 0} chunks across ${indexed} file(s) indexed, ${skipped} file(s) skipped (unchanged).`,
          },
        ],
      };
    }

    return { content: [{ type: "text", text: `Unknown tool: ${name}` }], isError: true };
  } catch (err) {
    return {
      content: [
        {
          type: "text",
          text: `Tool error: ${err.message}\n\nMake sure the RAG server is running at ${RAG_BASE}`,
        },
      ],
      isError: true,
    };
  }
});

// ─── Start ────────────────────────────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
