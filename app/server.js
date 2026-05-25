import fs from "fs/promises";
import path from "path";
import express from "express";
import cors from "cors";
import morgan from "morgan";
import dotenv from "dotenv";
import pdfParse from "pdf-parse";
import { Agent, setGlobalDispatcher } from "undici";

dotenv.config();

// Bump undici's default 5-minute bodyTimeout / headersTimeout. Long-form
// generations from verbose / partial-GPU-spilled models (Qwen 3.6, 30B+
// dense on a 16 GB card, future vLLM-served Nemotron Parse/Embed/Rerank)
// routinely exceed 5 min with stream:false. Default 30 min gives headroom.
// Overridable via env so tests / smaller hardware can tighten if desired.
const FETCH_BODY_TIMEOUT_MS    = Number(process.env.FETCH_BODY_TIMEOUT_MS    || 30 * 60 * 1000);
const FETCH_HEADERS_TIMEOUT_MS = Number(process.env.FETCH_HEADERS_TIMEOUT_MS || 30 * 60 * 1000);
setGlobalDispatcher(new Agent({
  bodyTimeout:    FETCH_BODY_TIMEOUT_MS,
  headersTimeout: FETCH_HEADERS_TIMEOUT_MS,
}));

const PORT          = Number(process.env.PORT || 3000);
const OLLAMA_HOST   = process.env.OLLAMA_HOST   || "http://127.0.0.1:11434";
const CHROMA_URL    = process.env.CHROMA_URL    || "http://127.0.0.1:8000";
const DATA_DIR      = process.env.DATA_DIR      || "/data";
const EMBEDDING_MODEL = process.env.EMBEDDING_MODEL || "nomic-embed-text";
// Two generation profiles, selectable per request via the OpenAI `model`
// field suffix `!deep` (e.g. "amd!deep"):
//   CHAT_MODEL      – fast default (GPU-resident, snappy, honest).
//   CHAT_MODEL_DEEP – slower/higher-accuracy (may CPU-offload).
const CHAT_MODEL      = process.env.CHAT_MODEL      || "llama3.1:8b-instruct-q8_0";
const CHAT_MODEL_DEEP = process.env.CHAT_MODEL_DEEP || CHAT_MODEL;
// Retrieved chunks for the deep profile (more context for the stronger model).
const TOP_K_DEEP    = Number(process.env.TOP_K_DEEP || 15);
// Multi-query expansion: decompose a question into focused sub-queries so
// first-stage retrieval recalls every concept (fixes contrastive questions
// where one mechanism's vocabulary dominates and starves the others).
const QUERY_EXPANSION = String(process.env.QUERY_EXPANSION ?? "true").toLowerCase() === "true";
const QUERY_EXPANSION_MODEL = process.env.QUERY_EXPANSION_MODEL || process.env.CHAT_MODEL || "llama3.1:8b-instruct-q8_0";
const MAX_SUBQUERIES = Number(process.env.MAX_SUBQUERIES || 5);
// Optional cross-encoder reranker sidecar. Empty = disabled (RRF order kept).
const RERANKER_URL  = process.env.RERANKER_URL || "";
// How many fused/deduped candidates to send to the reranker.
const RERANK_CANDIDATES = Number(process.env.RERANK_CANDIDATES || 30);
// Per-collection Nemotron routing (Phase 4 of the RAG eval). When NEMO_RAG_URL
// is set, two independent comma-separated allowlists decide which collections
// route which feature to the nemo-rag sidecar (scripts/nemo-rag/server.py):
//   NEMO_EMBED_COLLECTIONS   — embed (ingest + query) via Nemotron instead of Ollama nomic
//   NEMO_RERANK_COLLECTIONS  — rerank via Nemotron instead of bge-reranker
// They're split so a collection can opt in to JUST rerank (nomic-embed kept,
// 768-dim vectors) — the original full-Nemotron Phase 4 regressed first-stage
// recall (Nemotron embed-vl is multimodal-tuned, weaker on long technical
// text), but the rerank scored cleanly in isolation. Collections NOT listed
// keep the default Ollama-embed + bge-rerank path. NEMO_RAG_COLLECTIONS
// (legacy) still works as a shorthand for "this collection in both lists".
const NEMO_RAG_URL = process.env.NEMO_RAG_URL || "";
const _parseList = (v) => new Set(String(v || "").split(",").map((s) => s.trim()).filter(Boolean));
const _legacyBoth = _parseList(process.env.NEMO_RAG_COLLECTIONS);
const NEMO_EMBED_COLLECTIONS  = new Set([..._parseList(process.env.NEMO_EMBED_COLLECTIONS), ..._legacyBoth]);
const NEMO_RERANK_COLLECTIONS = new Set([..._parseList(process.env.NEMO_RERANK_COLLECTIONS), ..._legacyBoth]);
function useNemoEmbed(collectionName) {
  return Boolean(NEMO_RAG_URL) && NEMO_EMBED_COLLECTIONS.has(String(collectionName || ""));
}
function useNemoRerank(collectionName) {
  return Boolean(NEMO_RAG_URL) && NEMO_RERANK_COLLECTIONS.has(String(collectionName || ""));
}
// Near-duplicate handling: this corpus has the consolidated standard plus the
// amendments. When near-identical chunks collide, keep the one from the
// earliest-listed preferred file so citations point at the current
// consolidated standard.
// 2026-05-25: canonical for IEEE 802.1Q switched from `8021Q-2022` (IEEE
// 802.1Q-2022, third edition) to `8802-1Q-2024` (ISO/IEC/IEEE 8802-1Q:2024,
// the international reprint incorporating 802.1Qcw-2023 / Qdx-2024 plus the
// 2021 maintenance amendments). The ISO 2024 edition supersedes the IEEE
// 2022 edition and is the current spec for implementation compliance work.
const CANONICAL_PREFERENCE = (process.env.CANONICAL_PREFERENCE ||
  "8802-1Q-2024,ug1399-vitis-hls-en-us-2025.2").split(",").map((s) => s.trim()).filter(Boolean);
const CHUNK_SIZE    = Number(process.env.CHUNK_SIZE   || 1000);
// Clause-bounded chunking: text packing never crosses a bookmark/clause
// boundary (truncated to this outline depth). Keeps every chunk clause-pure
// — e.g. an 8.6.8.5 ATS paragraph can't bleed into an 8.6.9 TAS chunk — so
// embeddings/rerank don't conflate vocabulary-colliding mechanisms. 0 = off.
const CHUNK_CLAUSE_DEPTH = Number(process.env.CHUNK_CLAUSE_DEPTH || 3);
const TOP_K_RESULTS = Number(process.env.TOP_K_RESULTS || 8);
const AUTO_INGEST   = String(process.env.AUTO_INGEST || "true").toLowerCase() === "true";
const DEFAULT_COLLECTION = process.env.DEFAULT_COLLECTION || "";
// Optional shared secret. When set, all endpoints require a matching
// Authorization: Bearer <key> or x-api-key header. Unset = open (localhost only).
const RAG_API_KEY   = process.env.RAG_API_KEY || "";
// Ollama context window for the chat model. Must hold TOP_K source chunks +
// a long question + the answer; default well above Ollama's ~2-4k default.
const CHAT_NUM_CTX  = Number(process.env.CHAT_NUM_CTX || 12288);
// Deep profile may want a different ctx (e.g. smaller for a CPU-offloaded
// model so latency doesn't balloon). Defaults to CHAT_NUM_CTX.
const CHAT_NUM_CTX_DEEP = Number(process.env.CHAT_NUM_CTX_DEEP || CHAT_NUM_CTX);

/**
 * Resolve an OpenAI `model` field into { collection, llmModel, numCtx }.
 * Syntax: "<collection>" or "<collection>!<profile>".
 *   collection: a folder name, "rag-active", or "" → active collection.
 *   profile:    "deep" → CHAT_MODEL_DEEP; "fast"/absent → CHAT_MODEL;
 *               anything containing ':' → used as a literal Ollama model.
 */
function resolveModel(modelField) {
  const raw = String(modelField || "").trim();
  const bang = raw.indexOf("!");
  const base = (bang >= 0 ? raw.slice(0, bang) : raw).trim();
  const profile = (bang >= 0 ? raw.slice(bang + 1) : "").trim().toLowerCase();

  let llmModel = CHAT_MODEL, numCtx = CHAT_NUM_CTX, topK = TOP_K_RESULTS;
  if (profile === "deep") {
    llmModel = CHAT_MODEL_DEEP; numCtx = CHAT_NUM_CTX_DEEP; topK = TOP_K_DEEP;
  } else if (profile && profile !== "fast") {
    llmModel = profile; // explicit "<collection>!ollama:tag" override
  }
  // Per-model num_ctx bump. Qwen 3.6 ships with hybrid-thinking ON; the
  // reasoning tokens it emits before the final answer combined with our
  // TOP_K source chunks overflow the default 12288 ctx on long answers
  // (HTTP 500 from Ollama). Bumping to 24576 gives reasoning headroom.
  // Only kicks in when a qwen3.6 model is explicitly requested via the
  // literal-tag override (existing collections / profiles unaffected).
  if (/^qwen3\.6:/i.test(llmModel)) {
    numCtx = Math.max(numCtx, 24576);
  }
  return { base, llmModel, numCtx, topK };
}
// Grounding mode:
//  "strict"    – answer ONLY from retrieved sources (max provenance, no
//                hallucination; will be vague where docs don't cover it).
//  "augmented" – primary = sources (cited); may add clearly-labelled general
//                expertise for gaps the sources don't cover (NotebookLM-like
//                depth, looser provenance). Never fabricates citations.
const RAG_GROUNDING = (process.env.RAG_GROUNDING || "strict").toLowerCase();

// Overlap must be strictly smaller than the chunk size, otherwise chunkText()
// never advances and loops forever. Clamp defensively.
const CHUNK_OVERLAP = Math.min(
  Number(process.env.CHUNK_OVERLAP || 200),
  Math.max(0, CHUNK_SIZE - 1)
);

// Hard ceiling on characters sent to the embedding model. nomic-embed-text
// has a ~2048-token window; token-dense datasheet tables can hit it well
// before 8k chars, so keep this conservative. Any chunk (including whole
// tables/headings) is split to stay under this before embedding.
const EMBED_MAX_CHARS = Number(process.env.EMBED_MAX_CHARS || 1600);

// ─── In-memory state (survives across requests, resets on container restart) ──
const collectionCache = new Map(); // folderName → Chroma collection ID
let activeName = DEFAULT_COLLECTION || null;

const sseClients = new Set();

// ─── Utilities ────────────────────────────────────────────────────────────────

/** Chroma collection name for a given folder name. */
function chromaCollName(folderName) {
  return `rag_${folderName}`;
}

function pushEvent(event, payload) {
  const line = `event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`;
  for (const res of sseClients) res.write(line);
}

function chunkText(text, chunkSize, overlap) {
  const chunks = [];
  const clean = text.replace(/\s+/g, " ").trim();
  if (!clean) return chunks;
  const size = Math.max(1, chunkSize);
  const step = Math.max(1, size - overlap); // guarantees forward progress
  let start = 0;
  while (start < clean.length) {
    const end = Math.min(start + size, clean.length);
    chunks.push(clean.slice(start, end));
    if (end >= clean.length) break;
    start += step;
  }
  return chunks;
}

/**
 * Embed a batch of strings in ONE call. Routes to the Nemotron sidecar if the
 * collection is opted in via NEMO_RAG_COLLECTIONS; otherwise hits Ollama's
 * /api/embed (nomic). Batching cuts ingest round-trips by ~an order of
 * magnitude vs one request per chunk. truncate:true degrades an over-long
 * input to a truncated embedding instead of failing the whole batch.
 *
 * `kind` is only consulted by the Nemotron path (bi-encoder needs separate
 * query/document encoders). Ollama doesn't care.
 */
async function embedTexts(inputs, kind = "document", collectionName = "") {
  if (inputs.length === 0) return [];

  if (useNemoEmbed(collectionName)) {
    const response = await fetch(`${NEMO_RAG_URL}/embed`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ inputs, kind }),
    });
    if (!response.ok) {
      const txt = await response.text();
      throw new Error(`nemo-rag embed failed: ${response.status} ${txt}`);
    }
    const json = await response.json();
    if (Array.isArray(json.embeddings) && json.embeddings.length === inputs.length) {
      return json.embeddings;
    }
    throw new Error(
      `nemo-rag embed: expected ${inputs.length} embeddings, got ${json.embeddings?.length}`
    );
  }

  const response = await fetch(`${OLLAMA_HOST}/api/embed`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: EMBEDDING_MODEL, input: inputs, truncate: true })
  });

  if (!response.ok) {
    const txt = await response.text();
    throw new Error(`Ollama embed failed: ${response.status} ${txt}`);
  }

  const json = await response.json();
  if (Array.isArray(json.embeddings) && json.embeddings.length === inputs.length) {
    return json.embeddings;
  }
  throw new Error(
    `Ollama embed: expected ${inputs.length} embeddings, got ${json.embeddings?.length}`
  );
}

/** Single-string convenience (query path). */
async function embedText(input, collectionName = "") {
  return (await embedTexts([input], "query", collectionName))[0];
}

async function readPdfText(filePath) {
  const fileBuffer = await fs.readFile(filePath);
  const parsed = await pdfParse(fileBuffer);
  return parsed.text || "";
}

// ─── Structure-aware extraction (page-tagged sidecars) ───────────────────────

/**
 * Load the page-tagged JSON sidecar produced by scripts/extract/extract.py:
 *   data/<collection>/.rag-cache/<file>.json
 * Returns { backend, blocks:[{id,page,section,type,text}] } or null if absent.
 */
async function loadSidecar(folderPath, fileName) {
  const p = path.join(folderPath, ".rag-cache", `${fileName}.json`);
  try {
    const parsed = JSON.parse(await fs.readFile(p, "utf-8"));
    if (Array.isArray(parsed?.blocks)) return parsed;
    console.warn(`[sidecar] ${p} has no blocks[] — ignoring`);
    return null;
  } catch (err) {
    // ENOENT = genuinely not extracted yet (expected). Anything else (parse
    // error, transient mount/read failure) is logged: a silent flat-text
    // fallback on an EXISTING sidecar otherwise hides a real degradation.
    if (err.code !== "ENOENT") {
      console.warn(`[sidecar] failed to read ${p}: ${err.message} — flat fallback`);
    }
    return null;
  }
}

/**
 * Turn extracted blocks into retrieval chunks that preserve citations.
 * - headings/tables are kept as their own chunks, but a table or block bigger
 *   than the embed budget is split — tables by rows with the header repeated
 *   on each fragment so every piece stays a self-describing table,
 * - consecutive text blocks on the same page are packed,
 * - EVERY emitted chunk's embedInput is kept under EMBED_MAX_CHARS so the
 *   embedding call cannot 400 on context overflow,
 * - every chunk keeps {page, section, blockType} for citation + reranking.
 */
function chunkBlocks(blocks) {
  const chunks = [];
  let buf = null; // packed text accumulator: { text, page, section }

  const sectionCtx = (b) => {
    const sec = (b.section || "").slice(0, 160);
    return [sec ? `Section: ${sec}` : "", b.type === "table" ? "Table:" : ""]
      .filter(Boolean).join(" ");
  };
  // chars available for the chunk body after the context prefix
  const budgetFor = (b) => Math.max(256, EMBED_MAX_CHARS - sectionCtx(b).length - 2);

  const emit = (text, b, type) => {
    const t = String(text).trim();
    if (!t) return;
    chunks.push({
      text: t,
      page: b.page ?? null,
      section: b.section || "",
      blockType: type,
      embedInput: `${sectionCtx({ ...b, type })}\n${t}`.trim(),
    });
  };

  // Clause identity for chunk-boundary purposes: the section breadcrumb
  // truncated to CHUNK_CLAUSE_DEPTH outline levels (falls back to the leaf
  // section, then "" for un-bookmarked content).
  const clauseKey = (b) => {
    if (!CHUNK_CLAUSE_DEPTH) return "";
    const sp = b.section_path || b.section || "";
    const parts = String(sp).split(">").map((s) => s.trim()).filter(Boolean);
    return parts.slice(0, CHUNK_CLAUSE_DEPTH).join(" > ") || (b.section || "");
  };

  const flush = () => {
    if (buf && buf.text.trim()) {
      emit(buf.text, { page: buf.page, section: buf.section }, "text");
    }
    buf = null;
  };

  // Split an oversized table by rows, repeating the header on each fragment.
  const splitTable = (text, b) => {
    const lines = text.split("\n").filter((l) => l.trim());
    const lim = budgetFor(b);
    const header = lines.slice(0, 2).join("\n"); // header row + separator
    if (!text.includes("|") || header.length >= lim) {
      for (const p of chunkText(text, lim, Math.min(CHUNK_OVERLAP, lim - 1))) emit(p, b, "table");
      return;
    }
    let group = [];
    let len = header.length;
    const flushGroup = () => {
      if (group.length) emit(`${header}\n${group.join("\n")}`, b, "table");
      group = []; len = header.length;
    };
    for (let i = 2; i < lines.length; i++) {
      if (len + lines[i].length + 1 > lim && group.length) flushGroup();
      group.push(lines[i]);
      len += lines[i].length + 1;
    }
    flushGroup();
  };

  for (const b of blocks) {
    const text = String(b.text || "").trim();
    if (!text) continue;
    const lim = budgetFor(b);

    if (b.type === "table") {
      flush();
      if (text.length <= lim) emit(text, b, "table");
      else splitTable(text, b);
      continue;
    }

    if (b.type === "heading") {
      // Don't index headings as their own chunks — single-word/"Notes:" blocks
      // are retrieval noise. The heading text is already carried as the
      // `section` field (and embed-prefixed) on the text/table chunks beneath
      // it, so nothing searchable is lost.
      flush();
      continue;
    }

    // text block
    const textLim = Math.min(CHUNK_SIZE, lim);
    if (text.length > textLim) {
      flush();
      for (const p of chunkText(text, textLim, Math.min(CHUNK_OVERLAP, textLim - 1))) {
        emit(p, b, "text");
      }
      continue;
    }
    // Pack consecutive text within the SAME clause up to the size budget;
    // a clause-key change forces a flush so chunks stay clause-pure.
    const key = clauseKey(b);
    if (buf && buf.key === key && (buf.text.length + text.length + 1) <= textLim) {
      buf.text += " " + text;
    } else {
      flush();
      buf = { text, page: b.page ?? null, section: b.section || "", key };
    }
  }
  flush();

  // Last-resort hard cap (e.g. token-dense content where chars underestimate
  // tokens); embedText() also sends truncate:true as the final backstop.
  for (const c of chunks) {
    if (c.embedInput.length > EMBED_MAX_CHARS) {
      c.embedInput = c.embedInput.slice(0, EMBED_MAX_CHARS);
    }
  }
  return chunks;
}

// ─── BM25 lexical index (in-process, rebuilt lazily from Chroma) ──────────────
// Dense embeddings are weak on the exact alphanumeric tokens that fill
// datasheets (register names, 0x04, AXI_INTC). A lightweight BM25 index gives
// us a lexical channel; results are fused with the vector channel via RRF.

const bm25Cache = new Map(); // collectionName → built index

const tokenize = (s) => String(s).toLowerCase().match(/[a-z0-9_]+/g) || [];

function buildBm25(docs) {
  // docs: [{ id, document, metadata }]
  const N = docs.length;
  const df = new Map();
  const postings = [];
  let totalLen = 0;
  for (const d of docs) {
    const tf = new Map();
    const toks = tokenize(d.document);
    for (const t of toks) tf.set(t, (tf.get(t) || 0) + 1);
    for (const t of tf.keys()) df.set(t, (df.get(t) || 0) + 1);
    totalLen += toks.length;
    postings.push({ id: d.id, document: d.document, metadata: d.metadata, tf, len: toks.length });
  }
  const avgdl = N ? totalLen / N : 0;
  const idf = new Map();
  for (const [t, n] of df) idf.set(t, Math.log(1 + (N - n + 0.5) / (n + 0.5)));
  return { postings, idf, avgdl, k1: 1.5, b: 0.75 };
}

function bm25Search(idx, query, limit) {
  const qToks = [...new Set(tokenize(query))];
  const scored = [];
  for (const p of idx.postings) {
    let score = 0;
    for (const t of qToks) {
      const f = p.tf.get(t);
      if (!f) continue;
      const denom = f + idx.k1 * (1 - idx.b + idx.b * (p.len / (idx.avgdl || 1)));
      score += (idx.idf.get(t) || 0) * ((f * (idx.k1 + 1)) / denom);
    }
    if (score > 0) scored.push({ id: p.id, document: p.document, metadata: p.metadata, score });
  }
  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, limit);
}

/** Fetch every chunk for a collection from Chroma (paged) and build BM25. */
async function getBm25Index(name, colId) {
  if (bm25Cache.has(name)) return bm25Cache.get(name);
  const docs = [];
  const pageSize = 1000;
  let offset = 0;
  for (;;) {
    const batch = await chromaRequest(`/api/v1/collections/${colId}/get`, {
      method: "POST",
      body: JSON.stringify({ limit: pageSize, offset, include: ["documents", "metadatas"] }),
    });
    const ids = batch?.ids || [];
    if (ids.length === 0) break;
    for (let i = 0; i < ids.length; i++) {
      docs.push({ id: ids[i], document: batch.documents?.[i] || "", metadata: batch.metadatas?.[i] || {} });
    }
    if (ids.length < pageSize) break;
    offset += ids.length;
  }
  const idx = buildBm25(docs);
  bm25Cache.set(name, idx);
  return idx;
}

// NOTE: all Chroma calls use the /api/v1 surface, which only exists on Chroma
// < 0.6. This is intentionally coupled to chromadb/chroma:0.5.5 pinned in
// docker-compose.yml. Bumping that image to >= 0.6 removes /api/v1 and breaks
// every call below — migrate to /api/v2 if the image is ever upgraded.
async function chromaRequest(endpoint, init = {}) {
  const response = await fetch(`${CHROMA_URL}${endpoint}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init.headers || {})
    }
  });

  if (!response.ok) {
    const txt = await response.text();
    throw new Error(`Chroma ${init.method || "GET"} ${endpoint} → ${response.status}: ${txt}`);
  }

  const text = await response.text();
  return text ? JSON.parse(text) : null;
}

// ─── Collection management ────────────────────────────────────────────────────

/** List all non-hidden subdirectories of DATA_DIR as available collections. */
async function listCollectionFolders() {
  try {
    const entries = await fs.readdir(DATA_DIR, { withFileTypes: true });
    return entries
      .filter((e) => e.isDirectory() && !e.name.startsWith("."))
      .map((e) => e.name)
      .sort();
  } catch {
    return [];
  }
}

/** Get or create the Chroma collection for a folder, return its ID. */
async function ensureCollection(name) {
  if (collectionCache.has(name)) return collectionCache.get(name);

  const chromaName = chromaCollName(name);
  const existing = await chromaRequest("/api/v1/collections");
  const match = Array.isArray(existing) ? existing.find((c) => c.name === chromaName) : undefined;

  let id;
  if (match?.id) {
    id = match.id;
  } else {
    const created = await chromaRequest("/api/v1/collections", {
      method: "POST",
      body: JSON.stringify({
        name: chromaName,
        metadata: { source: "local-llm", folder: name },
        get_or_create: true
      })
    });
    id = created.id;
  }

  collectionCache.set(name, id);
  return id;
}

/** Return the active collection name, defaulting to the first available folder. */
async function getActiveCollection() {
  if (activeName) return activeName;
  const folders = await listCollectionFolders();
  if (folders.length > 0) activeName = folders[0];
  return activeName;
}

/**
 * Check whether a specific PDF has already been ingested into a collection.
 * Uses the file's mtime stored in Chroma metadata to detect changes.
 */
async function isFileIngested(collectionId, fileName, mtime) {
  try {
    const result = await chromaRequest(`/api/v1/collections/${collectionId}/get`, {
      method: "POST",
      body: JSON.stringify({ where: { fileName }, limit: 1, include: ["metadatas"] })
    });
    if (!result?.ids?.length) return false;
    return result.metadatas?.[0]?.mtime === mtime;
  } catch {
    return false;
  }
}

// ─── Ingest ───────────────────────────────────────────────────────────────────

/**
 * Ingest all PDFs in DATA_DIR/<name>/.
 * Skips files whose mtime hasn't changed since the last ingest (fast switch-back).
 * Pass force:true to re-ingest everything regardless.
 */
async function ingestCollection(name, { force = false } = {}) {
  const folderPath = path.join(DATA_DIR, name);
  const entries = await fs.readdir(folderPath, { withFileTypes: true });
  const pdfs = entries
    .filter((e) => e.isFile() && e.name.toLowerCase().endsWith(".pdf"))
    .map((e) => e.name);

  if (pdfs.length === 0) {
    return { collection: name, files: 0, totalChunks: 0, results: [] };
  }

  const colId = await ensureCollection(name);
  const results = [];

  for (const fileName of pdfs) {
    const fullPath = path.join(folderPath, fileName);
    const stat = await fs.stat(fullPath);
    const mtime = stat.mtime.toISOString();

    if (!force && await isFileIngested(colId, fileName, mtime)) {
      pushEvent("ingest.progress", { state: "skipped", fileName, collection: name });
      results.push({ fileName, chunkCount: 0, skipped: true });
      continue;
    }

    pushEvent("ingest.progress", { state: "started", fileName, collection: name });

    try {
      // Remove stale chunks for this file before re-ingesting
      try {
        const old = await chromaRequest(`/api/v1/collections/${colId}/get`, {
          method: "POST",
          body: JSON.stringify({ where: { fileName }, include: [] })
        });
        if (old?.ids?.length) {
          await chromaRequest(`/api/v1/collections/${colId}/delete`, {
            method: "POST",
            body: JSON.stringify({ ids: old.ids })
          });
        }
      } catch { /* ignore — collection may be empty */ }

      // Prefer the structure-aware sidecar (tables/pages/sections preserved);
      // fall back to flat pdf-parse text if the PDF hasn't been extracted yet.
      const sidecar = await loadSidecar(folderPath, fileName);
      let chunks, source;
      if (sidecar) {
        chunks = chunkBlocks(sidecar.blocks);
        source = `sidecar:${sidecar.backend || "?"}`;
      } else {
        console.warn(`[ingest] ${name}/${fileName}: no usable sidecar — FLAT pdf-parse fallback (no pages/sections)`);
        const text = await readPdfText(fullPath);
        chunks = chunkText(text, CHUNK_SIZE, CHUNK_OVERLAP)
          .map((t) => ({ text: t, page: null, section: "", blockType: "text", embedInput: t }));
        source = "pdf-parse (no sidecar — run scripts/extract-pdfs.ps1 for citations)";
      }

      // Embed + upsert in batches: large datasheets produce tens of thousands
      // of chunks — a single upsert would be a >100 MB JSON body. Batching
      // bounds memory/request size and gives incremental progress.
      const BATCH = 64;
      let written = 0;
      for (let s = 0; s < chunks.length; s += BATCH) {
        const slice = chunks.slice(s, s + BATCH);
        // One embed call for the whole batch (vs one per chunk) — the ingest
        // hot path; ~10x fewer round-trips on large corpora.
        const embeddings = await embedTexts(slice.map((c) => c.embedInput || c.text), "document", name);
        const ids = [], documents = [], metadatas = [];
        for (let j = 0; j < slice.length; j++) {
          const c = slice[j];
          const i = s + j;
          ids.push(`${name}:${fileName}:${i}`);
          documents.push(c.text);
          metadatas.push({
            fileName, chunkIndex: i, collection: name, mtime,
            page: c.page ?? null, section: c.section || "", blockType: c.blockType || "text",
          });
        }
        if (ids.length) {
          await chromaRequest(`/api/v1/collections/${colId}/upsert`, {
            method: "POST",
            body: JSON.stringify({ ids, documents, metadatas, embeddings })
          });
          written += ids.length;
        }
        pushEvent("ingest.progress", {
          state: "embedding", fileName, collection: name, done: written, total: chunks.length,
        });
      }

      pushEvent("ingest.progress", { state: "completed", fileName, chunkCount: chunks.length, collection: name, source });
      results.push({ fileName, chunkCount: chunks.length });
    } catch (err) {
      // One bad file (e.g. a pathological table) must not abort the whole
      // collection — record it and move on.
      console.error(`[ingest] ${name}/${fileName} failed:`, err.message);
      pushEvent("ingest.progress", { state: "error", fileName, collection: name, error: String(err) });
      results.push({ fileName, chunkCount: 0, error: String(err), failed: true });
    }
  }

  // Prune chunks for files that no longer exist (deleted/renamed PDFs).
  // Per-file ingest only deletes-then-reinserts files that still exist, so a
  // removed PDF would otherwise leave stale, page-less chunks polluting
  // retrieval forever. $nin keeps only chunks whose fileName is a current PDF.
  try {
    await chromaRequest(`/api/v1/collections/${colId}/delete`, {
      method: "POST",
      body: JSON.stringify({ where: { fileName: { $nin: pdfs } } }),
    });
  } catch (e) {
    console.error(`[ingest] orphan prune failed for ${name}:`, e.message);
  }

  // (Re)ingest or prune invalidates the lexical index so it rebuilds clean.
  bm25Cache.delete(name);

  return {
    collection: name,
    files: pdfs.length,
    totalChunks: results.reduce((s, r) => s + r.chunkCount, 0),
    results
  };
}

// ─── Query ────────────────────────────────────────────────────────────────────

/**
 * Hybrid retrieval: dense (Chroma vector) + lexical (BM25), fused with
 * Reciprocal Rank Fusion. RRF needs no score calibration between the two
 * channels and is robust to their different scales.
 */
/** Normalised signature for near-duplicate detection (markdown/space-insensitive). */
function dedupeSig(text) {
  return String(text)
    .toLowerCase()
    .replace(/[*_`#|>-]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 300);
}

/** Lower = more preferred source when collapsing duplicate content. */
function canonRank(fileName) {
  const i = CANONICAL_PREFERENCE.findIndex((p) => String(fileName || "").includes(p));
  return i === -1 ? CANONICAL_PREFERENCE.length : i;
}

/**
 * Collapse near-identical chunks that recur across the consolidated standard,
 * its amendments, and the ISO reprint. Keeps the best copy: canonical source
 * first, then higher fused score. Distinct content has distinct signatures so
 * it is untouched — this only removes true cross-document duplication.
 */
function dedupePreferCanonical(items) {
  const best = new Map();
  for (const it of items) {
    const k = dedupeSig(it.document);
    const prev = best.get(k);
    if (!prev) { best.set(k, it); continue; }
    const a = canonRank(it.metadata?.fileName);
    const b = canonRank(prev.metadata?.fileName);
    if (a < b || (a === b && (it.rrf || 0) > (prev.rrf || 0))) best.set(k, it);
  }
  return [...best.values()].sort((x, y) => (y.rrf || 0) - (x.rrf || 0));
}

/**
 * Optional cross-encoder rerank via the reranker sidecar. Best-effort: any
 * failure degrades to the input (fused) order, same as the BM25 channel.
 *
 * Routes to the Nemotron sidecar's /rerank when the collection is opted in
 * via NEMO_RAG_COLLECTIONS; otherwise hits the bge-reranker at RERANKER_URL.
 * Both speak the same {query, documents[]} -> {scores[]} protocol so the
 * downstream sort is identical.
 */
async function rerankItems(query, items, collectionName = "") {
  if (items.length <= 1) return items;
  const url = useNemoRerank(collectionName) ? `${NEMO_RAG_URL}/rerank` : RERANKER_URL;
  if (!url) return items;
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, documents: items.map((i) => i.document) }),
    });
    if (!resp.ok) throw new Error(`reranker ${resp.status}`);
    const { scores } = await resp.json();
    if (!Array.isArray(scores) || scores.length !== items.length) {
      throw new Error("reranker score length mismatch");
    }
    return items
      .map((it, i) => ({ it, s: scores[i] }))
      .sort((a, b) => b.s - a.s)
      .map(({ it }) => it);
  } catch (err) {
    console.error("Reranker unavailable, keeping fused order:", err.message);
    return items;
  }
}

/** Hybrid retrieve + fuse + dedupe for ONE query. Returns up to
 *  RERANK_CANDIDATES items (pre-rerank), each carrying its Chroma id so the
 *  multi-query path can union across sub-queries. */
async function retrieveCandidates(name, query) {
  const colId = await ensureCollection(name);

  // Guard against querying an empty collection (Chroma errors if n_results > count)
  const countResult = await chromaRequest(`/api/v1/collections/${colId}/count`);
  const docCount = typeof countResult === "number" ? countResult : 0;
  if (docCount === 0) return [];

  const topK = TOP_K_RESULTS;

  // Over-fetch generously: dedupe collapses cross-document duplicates and the
  // reranker needs candidate headroom to pull the right clause up.
  const fetchN = Math.min(docCount, Math.max(topK * 6, RERANK_CANDIDATES * 2, 40));

  // Dense channel
  const queryEmbedding = await embedText(query, name);
  const dense = await chromaRequest(`/api/v1/collections/${colId}/query`, {
    method: "POST",
    body: JSON.stringify({
      query_embeddings: [queryEmbedding],
      n_results: fetchN,
      include: ["documents", "metadatas", "distances"],
    }),
  });
  const dIds = dense.ids?.[0] || [];
  const dDocs = dense.documents?.[0] || [];
  const dMeta = dense.metadatas?.[0] || [];
  const dDist = dense.distances?.[0] || [];

  const pool = new Map(); // id → { document, metadata, distance, rrf }
  const RRF_K = 60;
  dIds.forEach((id, rank) => {
    pool.set(id, {
      id, document: dDocs[rank], metadata: dMeta[rank], distance: dDist[rank],
      rrf: 1 / (RRF_K + rank),
    });
  });

  // Lexical channel (best-effort — degrade to dense-only if it fails)
  try {
    const idx = await getBm25Index(name, colId);
    bm25Search(idx, query, fetchN).forEach((hit, rank) => {
      const e = pool.get(hit.id);
      if (e) {
        e.rrf += 1 / (RRF_K + rank);
      } else {
        pool.set(hit.id, {
          id: hit.id, document: hit.document, metadata: hit.metadata,
          distance: null, rrf: 1 / (RRF_K + rank),
        });
      }
    });
  } catch (err) {
    console.error("BM25 channel unavailable, using dense only:", err.message);
  }

  // Fuse → collapse cross-document duplicates (canonical-preferred).
  const fused = [...pool.values()].sort((a, b) => b.rrf - a.rrf);
  return dedupePreferCanonical(fused).slice(0, RERANK_CANDIDATES);
}

/**
 * Decompose a question into focused retrieval sub-queries with the fast
 * model. A contrastive question ("is TAS the same as a PSFP stream gate,
 * does it have an IPV?") is lexically dominated by one mechanism, so
 * single-query retrieval never recalls the others' defining clauses. One
 * focused sub-query per concept fixes first-stage recall. Best-effort:
 * any failure falls back to just the original question.
 */
async function expandQueries(question) {
  if (!QUERY_EXPANSION) return [question];
  try {
    const sys =
      "Decompose the user's question into the distinct technical concepts/" +
      "mechanisms it involves. Output ONLY a JSON array of 2-5 short search " +
      "queries (≤12 words each), one self-contained concept per query, no prose.";
    const resp = await fetch(`${OLLAMA_HOST}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: QUERY_EXPANSION_MODEL, stream: false,
        options: { temperature: 0, num_ctx: 4096 },
        messages: [
          { role: "system", content: sys },
          { role: "user", content: question },
        ],
      }),
    });
    if (!resp.ok) throw new Error(`expand ${resp.status}`);
    const txt = (await resp.json()).message?.content || "";
    const m = txt.match(/\[[\s\S]*\]/);
    const arr = m ? JSON.parse(m[0]) : [];
    const subs = arr.filter((s) => typeof s === "string" && s.trim())
      .map((s) => s.trim()).slice(0, MAX_SUBQUERIES);
    // Always keep the original question so expansion can only add recall.
    return [question, ...subs.filter((s) => s.toLowerCase() !== question.toLowerCase())];
  } catch (err) {
    console.error("Query expansion failed, using original only:", err.message);
    return [question];
  }
}

/**
 * Multi-query retrieval: expand → retrieve candidates per sub-query → union
 * (keep best fused score per chunk) → ONE cross-encoder rerank against the
 * ORIGINAL question → top-K. Falls back to single-query behaviour when
 * expansion yields nothing extra.
 */
async function queryCollection(name, question, topK) {
  const queries = await expandQueries(question);
  const union = new Map(); // id → candidate (best rrf seen)
  for (const q of queries) {
    for (const c of await retrieveCandidates(name, q)) {
      const prev = union.get(c.id);
      if (!prev || (c.rrf || 0) > (prev.rrf || 0)) union.set(c.id, c);
    }
  }
  const merged = [...union.values()].sort((a, b) => (b.rrf || 0) - (a.rrf || 0));
  const reranked = await rerankItems(question, merged.slice(0, RERANK_CANDIDATES), name);
  return reranked
    .slice(0, topK)
    .map(({ document, metadata, distance }) => ({ document, metadata, distance }));
}

// ─── RAG + LLM pipeline (used by /v1/chat/completions) ───────────────────────

async function ragChat(collectionName, messages, { stream = false, res = null, topK = TOP_K_RESULTS, llmModel = CHAT_MODEL, numCtx = CHAT_NUM_CTX } = {}) {
  const userMessage = [...messages].reverse().find((m) => m.role === "user");
  if (!userMessage) throw new Error("No user message found in messages");

  const matches = await queryCollection(collectionName, userMessage.content, topK);

  const ABSTAIN = "I don't have enough information in the provided sources to answer that.";
  const id = `chatcmpl-${Date.now()}`;
  const created = Math.floor(Date.now() / 1000);

  // Build numbered, citable source entries.
  const srcLabel = (m) => {
    const f = m.metadata?.fileName || "unknown";
    const p = m.metadata?.page;
    const s = m.metadata?.section;
    return `${f}${p ? ` — p.${p}` : ""}${s ? `, §${s}` : ""}`;
  };
  const citations = matches.map((m, i) => ({
    n: i + 1,
    fileName: m.metadata?.fileName || "unknown",
    page: m.metadata?.page ?? null,
    section: m.metadata?.section || "",
    blockType: m.metadata?.blockType || "text",
    snippet: String(m.document || "").slice(0, 240),
  }));
  const sourcesBlock = matches.length
    ? "\n\n---\n**Sources**\n" + citations.map((c) =>
        `[${c.n}] ${c.fileName}${c.page ? ` — p.${c.page}` : ""}${c.section ? `, §${c.section}` : ""}`
      ).join("\n")
    : "";

  // No retrieved context → abstain without burning an LLM call.
  if (matches.length === 0) {
    if (stream) {
      res.setHeader("Content-Type", "text/event-stream");
      res.setHeader("Cache-Control", "no-cache");
      res.setHeader("Connection", "keep-alive");
      res.write(`data: ${JSON.stringify({ id, object: "chat.completion.chunk", created, model: collectionName, choices: [{ index: 0, delta: { role: "assistant", content: ABSTAIN }, finish_reason: "stop" }] })}\n\n`);
      res.write("data: [DONE]\n\n");
      res.end();
      return;
    }
    return {
      id, object: "chat.completion", created, model: collectionName,
      choices: [{ index: 0, message: { role: "assistant", content: ABSTAIN }, finish_reason: "stop" }],
      usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
      citations: [],
    };
  }

  const contextText = matches
    .map((m, i) => `[${i + 1}] ${srcLabel(m)}\n${m.document}`)
    .join("\n\n");

  const sourceRule = RAG_GROUNDING === "augmented"
    ? `1. The numbered SOURCES are your primary, authoritative basis — prefer ` +
      `and cite them. You MAY add well-established general expertise to cover ` +
      `aspects the SOURCES omit, but explicitly mark every such statement with ` +
      `"(general knowledge — not from the provided sources)" and NEVER attach a ` +
      `[n] citation to it. Keep sourced vs. general clearly separated.`
    : `1. Use ONLY information found in the SOURCES. Do not use prior knowledge. ` +
      `If the sources don't cover part of the question, say so explicitly ` +
      `rather than filling it in.`;

  const systemPrompt =
    `You are a precise technical assistant answering from the numbered SOURCES ` +
    `(a retrieval set from the "${collectionName}" collection).\n\n` +
    `Rules:\n` +
    `${sourceRule}\n` +
    `2. Be thorough and well-structured. Address EVERY distinct sub-question ` +
    `explicitly (use headings/numbered points mirroring the question). Where the ` +
    `sources give exact pragma/directive spellings, code, option names, numeric ` +
    `values, or trade-offs, reproduce them verbatim — do not paraphrase syntax.\n` +
    `3. Synthesise across sources; a specific, scoped answer beats a vague one. ` +
    `After each claim cite its source(s) with bracketed numbers, e.g. [1] or [2][3].\n` +
    `4. ONLY if none of the SOURCES are relevant at all, reply with exactly: ` +
    `"${ABSTAIN}". Never use this when the answer is merely incomplete — answer ` +
    `what you can and state what is missing.\n` +
    `5. Never invent citations or fabricate identifiers/values.\n\n` +
    `SOURCES:\n${contextText}`;

  const llmMessages = [
    { role: "system", content: systemPrompt },
    ...messages.filter((m) => m.role !== "system")
  ];

  // Without an explicit num_ctx Ollama caps context at the model default
  // (~2-4k tokens), silently truncating the system prompt — i.e. the
  // retrieved sources — on long questions, which causes weak/abstaining
  // answers. Size it for TOP_K chunks (~CHUNK_SIZE chars each) + a long
  // question + the answer. Override via CHAT_NUM_CTX.
  const ollamaResponse = await fetch(`${OLLAMA_HOST}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: llmModel,
      messages: llmMessages,
      stream,
      options: { num_ctx: numCtx },
    })
  });

  if (!ollamaResponse.ok) {
    const txt = await ollamaResponse.text();
    throw new Error(`Ollama chat failed: ${ollamaResponse.status} ${txt}`);
  }

  if (!stream) {
    const json = await ollamaResponse.json();
    const answer = json.message?.content || "";
    // Append the source list unless the model abstained.
    const content = answer.trim() === ABSTAIN ? answer : answer + sourcesBlock;
    return {
      id,
      object: "chat.completion",
      created,
      model: collectionName,
      choices: [{
        index: 0,
        message: { role: "assistant", content },
        finish_reason: "stop"
      }],
      usage: {
        prompt_tokens: json.prompt_eval_count || 0,
        completion_tokens: json.eval_count || 0,
        total_tokens: (json.prompt_eval_count || 0) + (json.eval_count || 0)
      },
      citations
    };
  }

  // Streaming: pipe Ollama NDJSON → OpenAI SSE format
  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");

  const reader = ollamaResponse.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const json = JSON.parse(line);
        const delta = json.message?.content || "";
        const chunk = {
          id,
          object: "chat.completion.chunk",
          created,
          model: collectionName,
          choices: [{
            index: 0,
            delta: delta ? { role: "assistant", content: delta } : {},
            finish_reason: json.done ? "stop" : null
          }]
        };
        res.write(`data: ${JSON.stringify(chunk)}\n\n`);
      } catch { /* skip malformed NDJSON lines */ }
    }
  }
  // Emit the source list as a final assistant delta so streamed answers are
  // cited too (the model was instructed to use [n] markers inline).
  if (sourcesBlock) {
    res.write(`data: ${JSON.stringify({
      id, object: "chat.completion.chunk", created, model: collectionName,
      choices: [{ index: 0, delta: { content: sourcesBlock }, finish_reason: null }],
    })}\n\n`);
  }
  res.write("data: [DONE]\n\n");
  res.end();
}

// ─── Express app ──────────────────────────────────────────────────────────────

const app = express();

// Only allow same-machine browser origins. The Open WebUI backend and curl
// scripts are not browsers and are unaffected; this blocks a random web page
// in the user's browser from POSTing to the (otherwise unauthenticated)
// ingest / active-collection endpoints.
app.use(cors({
  origin: (origin, cb) => {
    if (!origin) return cb(null, true); // non-browser clients (curl, server-side)
    cb(null, /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin));
  }
}));
app.use(express.json({ limit: "2mb" }));
app.use(morgan("combined"));

// Optional shared-secret auth. Disabled (open) unless RAG_API_KEY is set.
// /health is always exempt so the Docker healthcheck keeps working.
app.use((req, res, next) => {
  if (!RAG_API_KEY || req.path === "/health") return next();
  const auth = req.get("authorization") || "";
  const presented = auth.startsWith("Bearer ")
    ? auth.slice(7).trim()
    : (req.get("x-api-key") || "").trim();
  if (presented === RAG_API_KEY) return next();
  res.status(401).json({ ok: false, error: "unauthorized" });
});

app.get("/health", async (_req, res) => {
  try {
    await chromaRequest("/api/v1/heartbeat");
    const active = await getActiveCollection();
    res.json({
      ok: true,
      service: "local-llm-rag-server",
      ollamaHost: OLLAMA_HOST,
      chromaUrl: CHROMA_URL,
      dataDir: DATA_DIR,
      activeCollection: active
    });
  } catch (err) {
    res.status(500).json({ ok: false, error: String(err) });
  }
});

app.get("/sse", (req, res) => {
  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    Connection: "keep-alive"
  });
  sseClients.add(res);
  res.write(`event: ready\ndata: ${JSON.stringify({ ok: true })}\n\n`);
  req.on("close", () => sseClients.delete(res));
});

// List all available collections (subfolders) and their Chroma status
app.get("/collections", async (_req, res) => {
  try {
    const folders = await listCollectionFolders();
    const active = await getActiveCollection();
    const chromaList = await chromaRequest("/api/v1/collections");
    const chromaNames = new Set(
      Array.isArray(chromaList) ? chromaList.map((c) => c.name) : []
    );
    const collections = folders.map((name) => ({
      name,
      chromaCollection: chromaCollName(name),
      ingested: chromaNames.has(chromaCollName(name)),
      active: name === active
    }));
    res.json({ ok: true, active, collections });
  } catch (err) {
    res.status(500).json({ ok: false, error: String(err) });
  }
});

// Ingest (or re-ingest) all PDFs in a specific collection folder
app.post("/collections/:name/ingest", async (req, res) => {
  const { name } = req.params;
  const force = Boolean(req.body?.force);
  try {
    const folders = await listCollectionFolders();
    if (!folders.includes(name)) {
      res.status(404).json({ ok: false, error: `Folder '${name}' not found in ${DATA_DIR}` });
      return;
    }
    const result = await ingestCollection(name, { force });
    res.json({ ok: true, ...result });
  } catch (err) {
    res.status(500).json({ ok: false, error: String(err) });
  }
});

// Get the active collection
app.get("/active-collection", async (_req, res) => {
  try {
    const active = await getActiveCollection();
    res.json({ ok: true, active });
  } catch (err) {
    res.status(500).json({ ok: false, error: String(err) });
  }
});

// Switch active collection — no re-ingest, instant
app.put("/active-collection", async (req, res) => {
  const name = String(req.body?.name || "").trim();
  if (!name) {
    res.status(400).json({ ok: false, error: "name is required" });
    return;
  }
  try {
    const folders = await listCollectionFolders();
    if (!folders.includes(name)) {
      res.status(404).json({ ok: false, error: `Collection '${name}' not found` });
      return;
    }
    activeName = name;
    res.json({ ok: true, active: activeName });
  } catch (err) {
    res.status(500).json({ ok: false, error: String(err) });
  }
});

// Query the active collection (or a specific one)
app.post("/query", async (req, res) => {
  const query = String(req.body?.query || "").trim();
  const topK = Number(req.body?.topK || TOP_K_RESULTS);
  const requestedCollection = req.body?.collection ? String(req.body.collection).trim() : null;

  if (!query) {
    res.status(400).json({ ok: false, error: "query is required" });
    return;
  }

  try {
    const name = requestedCollection || await getActiveCollection();
    if (!name) {
      res.status(400).json({ ok: false, error: "No active collection. Run POST /collections/:name/ingest first." });
      return;
    }
    const matches = await queryCollection(name, query, topK);
    res.json({ ok: true, query, collection: name, matches, topK });
  } catch (err) {
    res.status(500).json({ ok: false, error: String(err) });
  }
});

// ─── OpenAI-compatible endpoints (connects to Open WebUI as a model source) ──
// Add http://127.0.0.1:3000/v1 as an OpenAI connection in Open WebUI settings.
// Each collection appears as a selectable model. "rag-active" uses the current
// active collection.

app.get("/v1/models", async (_req, res) => {
  try {
    const folders = await listCollectionFolders();
    const created = Math.floor(Date.now() / 1000);
    // Each collection is offered twice: bare (fast = CHAT_MODEL) and "!deep"
    // (CHAT_MODEL_DEEP). So Open WebUI's model picker shows e.g. amd / amd!deep.
    const bases = ["rag-active", ...folders];
    const data = bases.flatMap((name) => [
      { id: name, object: "model", created, owned_by: "local-llm",
        description: name === "rag-active"
          ? `Active collection, fast model (${CHAT_MODEL})`
          : `${name}, fast model (${CHAT_MODEL})` },
      { id: `${name}!deep`, object: "model", created, owned_by: "local-llm",
        description: `${name}, deep model (${CHAT_MODEL_DEEP})` },
    ]);
    res.json({ object: "list", data });
  } catch (err) {
    res.status(500).json({ error: { message: String(err), type: "server_error" } });
  }
});

app.post("/v1/chat/completions", async (req, res) => {
  const { model, messages, stream = false } = req.body;
  if (!Array.isArray(messages) || messages.length === 0) {
    res.status(400).json({ error: { message: "messages array is required", type: "invalid_request_error" } });
    return;
  }

  try {
    const { base, llmModel, numCtx, topK: profileTopK } = resolveModel(model);
    // Explicit body.topK wins; otherwise the profile default (deep = more).
    const topK = Number(req.body?.topK) > 0 ? Number(req.body.topK) : profileTopK;
    const collectionName = (!base || base === "rag-active")
      ? await getActiveCollection()
      : base;

    if (!collectionName) {
      res.status(400).json({
        error: { message: "No active collection. Use PUT /active-collection to set one.", type: "invalid_request_error" }
      });
      return;
    }

    if (stream) {
      await ragChat(collectionName, messages, { stream: true, res, topK, llmModel, numCtx });
    } else {
      const result = await ragChat(collectionName, messages, { stream: false, topK, llmModel, numCtx });
      res.json(result);
    }
  } catch (err) {
    if (!res.headersSent) {
      res.status(500).json({ error: { message: String(err), type: "server_error" } });
    }
  }
});

// ─── Start ────────────────────────────────────────────────────────────────────

app.listen(PORT, async () => {
  console.log(`RAG server listening on port ${PORT}`);
  console.log(`Data directory : ${DATA_DIR}`);
  console.log(`Ollama         : ${OLLAMA_HOST}`);
  console.log(`Chroma         : ${CHROMA_URL}`);
  console.log(`Embedding model: ${EMBEDDING_MODEL}`);
  if (NEMO_RAG_URL && (NEMO_EMBED_COLLECTIONS.size || NEMO_RERANK_COLLECTIONS.size)) {
    console.log(`Nemo-RAG       : ${NEMO_RAG_URL}`);
    if (NEMO_EMBED_COLLECTIONS.size)  console.log(`  embed for    : ${[...NEMO_EMBED_COLLECTIONS].join(", ")}`);
    if (NEMO_RERANK_COLLECTIONS.size) console.log(`  rerank for   : ${[...NEMO_RERANK_COLLECTIONS].join(", ")}`);
  }
  console.log(`Chat model     : ${CHAT_MODEL}`);

  const folders = await listCollectionFolders();
  console.log(`Collections    : ${folders.join(", ") || "(none yet)"}`);

  const initial = await getActiveCollection();
  if (initial) console.log(`Active         : ${initial}`);

  if (AUTO_INGEST && folders.length > 0) {
    console.log("AUTO_INGEST: ingesting new/changed files in all collections...");
    for (const name of folders) {
      try {
        const summary = await ingestCollection(name);
        console.log(`  ${name}: ${summary.files} PDFs, ${summary.totalChunks} new chunks`);
      } catch (err) {
        console.error(`  ${name}: ingest failed —`, err);
      }
    }
    console.log("AUTO_INGEST complete");
  }
});
