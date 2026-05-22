# Local LLM + PDF RAG Runbook (Implemented Baseline)

This document is the definitive runbook for the implemented setup in this repository.
It records what is running now and the exact procedure to reproduce it on another machine.
It is kept in sync with `docker-compose.yml`, `app/server.js`, and the `scripts/`.

## 1. Scope and Architecture

This implementation uses a hybrid Windows + WSL model:

- Windows 11 is the operator host (VS Code, terminals, client apps).
- WSL2 Ubuntu 24.04 runs Ollama and Docker workloads.
- Docker in WSL runs:
  - `chroma` vector database on port `8000` (published `8000:8000`).
  - `rag-server` (Node.js) on port `3000` (`network_mode: host`).
  - `reranker` (Python) on port `8008` (`network_mode: host`, CPU-only).
  - `open-webui` browser chat interface on port `8080` (`network_mode: host`).
  - `sd-webui` Automatic1111 Stable Diffusion WebUI on port `7860` (port-mapped
    `7860:7860`, GPU reservation) — image-generation backend that Open WebUI
    dispatches to from the per-message **image** button. Runtime spec: §4
    item 5; provisioning: §6.5 + §7.2; ops + VRAM coexistence: §12.1;
    new-machine quick-start: §13.1.
- Ollama runs in WSL on port `11434`.

Data flow:

1. Each immediate subfolder of `./data/` (e.g. `data/ieee`, `data/amd`) is a
   **collection**. `./data` is mounted read-only into the RAG container as `/data`.
2. PDF text is extracted, chunked, and embedded via Ollama `nomic-embed-text`.
3. Embeddings are persisted in Chroma, one Chroma collection per folder
   (`rag_<folder>`).
4. Query requests embed the question and return the top-k matched chunks; the
   OpenAI-compatible endpoint additionally feeds them to `CHAT_MODEL` for a
   synthesised answer.
5. **Image-generation (orthogonal to RAG):** an assistant reply in Open WebUI
   may be turned into a text→image dispatch by the user clicking the per-message
   **image** button. Open WebUI POSTs the reply text as a prompt to
   `sd-webui`'s `/sdapi/v1/txt2img`; the rendered PNG is inlined into the
   conversation. The LLM emits ordinary text — there is no "create image"
   protocol on the model side and no change to RAG/grounding logic.

## 2. Verified Host Baseline (This Machine)

Verified on this machine 2026-05-18:

- OS: Windows 11 host + WSL2 Ubuntu 24.04.4 LTS (Noble)
- WSL kernel: `6.6.87.2-microsoft-standard-WSL2`
- GPU in WSL: `NVIDIA GeForce RTX 5070 Ti`, `16303 MiB`, driver `591.86`
- Ollama: `0.24.0`
- Docker Engine (Server): `29.5.0`
- Docker Compose plugin: `v5.1.3`

Re-capture these on a new machine with:

```powershell
wsl -e bash -lc "uname -r && . /etc/os-release && echo \$PRETTY_NAME"
wsl -e bash -lc "ollama --version"
wsl -e bash -lc "sudo docker version --format '{{.Server.Version}}'"
wsl -e bash -lc "sudo docker compose version"
wsl -e bash -lc "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader"
```

## 3. Repository Layout

Implemented files and folders:

- `docker-compose.yml`
- `app/package.json`, `app/server.js` — RAG server (structure-aware ingest,
  hybrid retrieval, grounded citations)
- `scripts/extract/extract.py`, `scripts/extract/requirements.txt` — PDF →
  page-tagged JSON sidecar extractor (Docling → PyMuPDF4LLM → pypdf)
- `scripts/extract-pdfs.ps1` — Windows entry point for the extractor
- `scripts/reranker/server.py`, `scripts/reranker/requirements.txt` — cross-
  encoder reranker sidecar
- `scripts/rag-mcp/package.json`, `scripts/rag-mcp/index.js` — MCP server
- `.vscode/mcp.json` — registers the MCP server for the editor
- `.env.example` — documents the variables `server.js` reads
- `scripts/bootstrap-models.sh`
- `scripts/download-sd-models.sh`, `scripts/download-sd-models.ps1` — fetches
  the default SDXL checkpoint (Juggernaut XL v9) into the `sd-webui` models
  directory. Idempotent (size-checked).
- `scripts/sd-webui-entrypoint.sh` — entrypoint wrapper for the `sd-webui`
  container. Pip-upgrades the bundled torch to a Blackwell-capable
  `cu128` wheel before A1111 starts (idempotent — skips if already
  current); then `exec`s ai-dock's normal `init.sh`. Full rationale in
  §6.5.2.
- `scripts/ensure-services.sh` (WSL start + health-wait logic)
- `scripts/start-local-llm.ps1` (Windows start entry point)
- `scripts/stop-local-llm.ps1`
- `scripts/restart-local-llm.ps1`
- `scripts/wsl-run.ps1`
- `scripts/install-startup-launcher.ps1` (installs logon autostart)
- `scripts/register-autostart-task.ps1` (Task Scheduler method, requires admin)
- `scripts/test-rag.ps1`
- `README.md`
- `data/<collection>/` (input PDFs, one folder per collection)
- `storage/chroma/` (persistent vector store)
- `storage/open-webui/` (persistent Open WebUI data)
- `storage/reranker/` (persisted reranker venv + HF model cache)
- `storage/sd-webui/` (ai-dock workspace; persists A1111 checkpoints, LoRAs,
  VAEs, outputs across container recreate. Standard layout:
  `storage/sd-webui/storage/stable_diffusion/models/{ckpt,lora,vae}/` +
  `.../output/`. Heavy: a single SDXL checkpoint is ~6.6 GB.) Also contains
  `storage/sd-webui/pip-cache/` — host-mounted pip wheel cache so the
  cu128 torch reinstall on container recreate stays fast (~2.5 GB after
  first boot; see §6.5.2).

## 4. Runtime Services

`docker-compose.yml` defines:

1. `chroma`
   - Image: `chromadb/chroma:0.5.5` (pinned — see note below)
   - Port: `8000:8000`
   - Persistence: `./storage/chroma:/chroma/chroma`
   - Healthcheck: HTTP `GET /api/v1/heartbeat`

2. `rag-server`
   - Image: `node:20-slim`
   - Network: `host`
   - Working dir: `/app`
   - Mounts: `./app:/app`, `./data:/data:ro`
   - Startup command: `npm install && node server.js`
   - Depends on: `chroma` healthy
   - Healthcheck: HTTP `GET /health`

3. `reranker`
   - Image: `python:3.12-slim`
   - Network: `host`, port `8008`
   - Mounts: `./scripts/reranker:/app`, `./storage/reranker:/cache`
     (persisted venv + HF model cache → torch/model download once)
   - Startup: venv + `pip install` + `python server.py`
   - Healthcheck: HTTP `GET /health` (long `start_period`: first boot
     installs torch and downloads the reranker model)
   - CPU-only by design (no GPU container plumbing). No `depends_on` from
     `rag-server` — retrieval degrades to fused order if it is unavailable.

4. `open-webui`
   - Image: `ghcr.io/open-webui/open-webui:main`
   - Network: `host`
   - Port: `8080`
   - Ollama upstream: `http://127.0.0.1:11434`
   - Persistence: `./storage/open-webui:/app/backend/data`
   - Depends on: `rag-server` healthy (does **not** wait on `sd-webui` — its
     first boot is 10+ minutes and would needlessly delay the chat UI).
   - Image-generation env (pre-pointed at `sd-webui` so no first-run admin
     clicks are required):
     - `ENABLE_IMAGE_GENERATION=true`
     - `IMAGE_GENERATION_ENGINE=automatic1111`
     - `AUTOMATIC1111_BASE_URL=http://127.0.0.1:7860`
     - `IMAGE_GENERATION_MODEL=Juggernaut-XL_v9_RunDiffusionPhoto_v2`
     - `IMAGE_SIZE=1024x1024`
     - `IMAGE_STEPS=30`
   - No healthcheck is defined in compose; readiness is verified by
     `scripts/ensure-services.sh` polling `http://127.0.0.1:8080/health`.

5. `sd-webui`
   - Image: `ghcr.io/ai-dock/stable-diffusion-webui:latest-cuda` (chosen for
     active maintenance + explicit `WEBUI_ARGS` env contract). Pinning to a
     dated tag is recommended for reproducibility but not done by default to
     keep the entry minimal.
   - **Entrypoint override:** `/usr/local/bin/sd-webui-entrypoint.sh` (host
     file [scripts/sd-webui-entrypoint.sh](scripts/sd-webui-entrypoint.sh),
     bind-mounted in). Pip-upgrades the A1111 venv's torch to a Blackwell-
     capable cu128 wheel before A1111 starts; see §6.5.2 for the why.
     Idempotent; skips when already current.
   - Network: **port-mapped** `7860:7860` (not `network_mode: host`) because
     the ai-dock entrypoint expects to manage its own bind interface; the
     port map plus its internal `--listen` gives the same external surface.
     Open WebUI (host network) reaches it at `127.0.0.1:7860`.
   - Launch args via env: `WEBUI_ARGS=--api --listen --cors-allow-origins=* --no-half-vae`
     - `--api` exposes `/sdapi/v1/*` (the surface Open WebUI calls).
     - `--listen` binds `0.0.0.0` inside the container so the port map works.
     - `--cors-allow-origins=*` lets the Open WebUI browser tab POST cross-
       origin to `:7860`.
     - `--no-half-vae` avoids the occasional black-image SDXL+VAE precision
       bug; harmless on a 16 GB card.
   - `WEB_ENABLE_AUTH=false` — disable the ai-dock built-in basic auth; the
     stack is local-only and Open WebUI gates LAN access on `:8080`.
   - `AUTO_UPDATE=false` — do not silently `git pull` A1111 on every boot;
     keeps the runtime reproducible.
   - Optional env passthrough: `HF_TOKEN`, `CIVITAI_TOKEN` (blank = anonymous
     downloads only; needed only for gated models like FLUX-dev).
   - Volume: `./storage/sd-webui:/workspace` — ai-dock's workspace
     convention. Checkpoints live at
     `storage/sd-webui/storage/stable_diffusion/models/ckpt/` on the host →
     `/workspace/storage/stable_diffusion/models/ckpt/` in the container.
     A1111 picks them up on boot; **Settings → Reload UI** (or `docker
     compose restart sd-webui`) forces a re-scan without full restart.
   - GPU: `deploy.resources.reservations.devices` with `driver: nvidia,
     count: all, capabilities: [gpu]`. This is the **first** GPU-using
     container in the stack — the other services (Chroma, rag-server, Open
     WebUI) are CPU-only; reranker is CPU-only by design. Requires NVIDIA
     Container Toolkit in the Docker daemon (§6.5).
   - Healthcheck: `GET /sdapi/v1/options` (cheapest live endpoint), interval
     `30s`, **`start_period: 900s`** (15 min) because first boot clones the
     A1111 source, installs PyTorch + xformers, and fetches dependencies —
     easily 10 minutes on a fresh host. Subsequent boots are ~30 s.
   - No `depends_on` to/from any other service (orthogonal to RAG).

All containers use `restart: unless-stopped` so Docker restarts them after a
crash or daemon restart. Startup ordering is enforced through
`condition: service_healthy` on each `depends_on` reference.

> **Chroma version coupling:** `server.js` uses the `/api/v1` Chroma API,
> which only exists on Chroma `< 0.6`. The image is intentionally pinned to
> `chromadb/chroma:0.5.5`. Upgrading the image to `>= 0.6` removes `/api/v1`
> and breaks every Chroma call — migrate to `/api/v2` first if upgrading.

Environment defaults used by `server.js` (see `.env.example` for the full list):

- `PORT=3000`
- `OLLAMA_HOST=http://127.0.0.1:11434`
- `CHROMA_URL=http://127.0.0.1:8000`
- `DATA_DIR=/data`
- `EMBEDDING_MODEL=nomic-embed-text`
- `CHAT_MODEL=qwen2.5-coder:32b-instruct-q4_K_M` (see §5.1 for the
  speed/accuracy trade vs `deepseek-r1:14b` / `llama3.1:8b`)
- `CHAT_NUM_CTX=12288` (Ollama context window; its ~2-4k default silently
  truncates retrieved sources on long questions)
- `CHUNK_SIZE=1000`
- `CHUNK_OVERLAP=200` (clamped to `< CHUNK_SIZE`)
- `EMBED_MAX_CHARS=1600` (hard embed-input ceiling; oversized tables are
  row-split with the header repeated)
- `TOP_K_RESULTS=8`
- `RAG_GROUNDING=augmented` (`strict` = sources only; `augmented` =
  sources primary + tagged general expertise for gaps, NotebookLM-like)
- `AUTO_INGEST=true` (set `false` during large manual ingests to stop a
  container recreate from auto-flat-ingesting un-extracted PDFs)
- `DEFAULT_COLLECTION=` (empty → first folder alphabetically)
- `RAG_API_KEY=` (empty → unauthenticated; localhost-only use)

## 5. RAG Server API (Implemented)

### 5.1 Retrieval pipeline

1. **Extraction** (`scripts/extract/extract.py`, run via
   `scripts/extract-pdfs.ps1`): each PDF → `data/<col>/.rag-cache/<pdf>.json`
   with page/section-tagged blocks; tables exported as Markdown and kept whole.
   Backend chosen by availability: Docling > PyMuPDF4LLM > pypdf.
   **Clause-path sectioning**: the PDF bookmark/outline tree (PyMuPDF
   `get_toc`) is the authoritative section structure for standards/datasheets
   — every block's `section` is overwritten with the deepest active bookmark
   title (e.g. `12.29.1 The Gate Parameter Table`), falling back to the
   backend's heuristic heading only where the outline is silent (front
   matter / un-bookmarked PDFs). This is the key disambiguator for mechanisms
   that collide in prose but live in separate outline branches — IEEE 802.1Q
   **TAS** = 12.29 / 8.6.9, **PSFP** = 12.31, **ATS** = 8.6.5.6 / 47 — and it
   makes citations clause-exact. The full breadcrumb is also kept as
   `section_path`.
2. **Ingest**: `server.js` prefers the sidecar (structure-aware chunking —
   tables/headings emitted whole, text packed to `CHUNK_SIZE`, page/section/
   blockType stored in Chroma metadata). **Clause-bounded chunking**: text
   packing never crosses a bookmark boundary (`section_path` truncated to
   `CHUNK_CLAUSE_DEPTH` levels) so every chunk is clause-pure — a paragraph
   from `8.6.8.5 ATS …` cannot share a chunk with `8.6.9 Scheduled traffic …`,
   which removes the cross-clause bleed that fed the TAS↔ATS/PSFP conflation. Falls back to flat `pdf-parse` text
   if no usable sidecar exists — that fallback now logs loudly
   (`[sidecar]`/`[ingest] FLAT`) so a silent structured→flat degradation
   can't recur (it once produced 21k page-less IEEE chunks). Embedding is
   **batched** (`embedTexts`, 64 chunks per `/api/embed` call): fewer HTTP
   round-trips and cleaner/more robust code, but measured wall-clock gain was
   marginal (~22 vs ~21 ms/chunk) — ingest is bound by nomic-embed-text GPU
   throughput + Chroma upsert, not request overhead. Real speedups would need
   a faster/smaller embed model, parallel embed requests, or not sharing the
   GPU with a resident chat model during ingest.
3. **Hybrid retrieval**: dense Chroma vector search + an in-process BM25
   lexical index (rebuilt lazily from Chroma after restart, invalidated on
   ingest), fused with Reciprocal Rank Fusion (k=60). Fixes exact-identifier
   recall on datasheets. Over-fetches `max(topK*6, RERANK_CANDIDATES*2, 40)`.
3a. **Dedupe + canonical preference**: this corpus has the consolidated
   standard, the amendments it incorporates, and an ISO reprint, so identical
   text recurs across files. Near-duplicate chunks (normalised-signature
   collision) are collapsed, keeping the copy from the earliest
   `CANONICAL_PREFERENCE` source so citations point at the current
   consolidated standard, not a superseded amendment.
3b. **Cross-encoder rerank** (`reranker` sidecar, best-effort): the deduped
   top `RERANK_CANDIDATES` are rescored by a `bge-reranker` cross-encoder so
   the right clause outranks vocabulary-colliding distractors — the concrete
   failure this fixes is IEEE 802.1Q **TAS transmission gate** (Clause 12.29)
   retrieving **PSFP stream gate** (Clause 12.31) chunks because the two
   mechanisms share nearly all terminology. Down/disabled → fused order
   (graceful, like the BM25 channel). The `!deep` profile retrieves
   `TOP_K_DEEP` (more context for the stronger model).
4. **Grounding**: faithfulness system prompt (answer only from numbered
   sources, inline `[n]` citations, fixed abstention sentence when
   unsupported). Zero-retrieval queries short-circuit to the abstention
   response. Replies get an appended **Sources** list; the non-stream JSON
   response also carries a `citations[]` array.

### 5.2 Endpoints

Service in `app/server.js` exposes:

1. `GET /health` — Chroma heartbeat + active-collection summary. Always
   exempt from `RAG_API_KEY` auth so the Docker healthcheck works.
2. `GET /sse` — SSE stream of ingest progress events.
3. `GET /collections` — list collection folders and their Chroma/ingest status.
4. `POST /collections/:name/ingest` — ingest one collection. Body
   `{ "force": false }`. Files whose mtime is unchanged are skipped.
5. `GET /active-collection` / `PUT /active-collection` — read / switch the
   active collection (`{ "name": "..." }`); switching does not re-index.
6. `POST /query` — body `{ "query": "...", "collection"?: "...", "topK"?: 5 }`;
   returns top-k matched chunks with metadata and distance.
7. `GET /v1/models` — OpenAI-compatible model list (`rag-active` + one entry
   per collection).
8. `POST /v1/chat/completions` — OpenAI-compatible RAG chat. `model` selects
   the collection (`rag-active` = current active). Honours an optional `topK`
   in the request body and `stream: true`.

Security notes:

- CORS is restricted to `localhost` / `127.0.0.1` browser origins. Non-browser
  clients (curl, the Open WebUI backend, the MCP server) are unaffected.
- If `RAG_API_KEY` is set, all endpoints except `/health` require
  `Authorization: Bearer <key>` or an `x-api-key` header. The Open WebUI
  OpenAI-connection API key and the MCP server's `RAG_API_KEY` must match.
- This is a REST + SSE + OpenAI-compatible service, plus a separate stdio MCP
  server (`scripts/rag-mcp`). It is not a single MCP protocol server.

### 5.3 Generation model & grounding (benchmarked on a deep Vitis-HLS question)

The retrieval/citation layer is model-independent and grounds correctly
(right UG1399 pages every time). The generator is a speed/accuracy trade:

| Model | Latency | Accuracy on deep specifics | Notes |
|---|---|---|---|
| `qwen2.5-coder:32b-instruct-q4_K_M` | ~5 min | Best — only local model right on BOTH exact pragma and the ram_s2p subtlety | >16GB VRAM → CPU offload. **`!deep` profile.** |
| `gemma4:e4b` | ~50 s | Shallower (honestly punts); imprecise pragma — but **lowest hallucination risk**, flags doc gaps instead of confabulating | ~10GB, **100% GPU**. **Fast default.** |
| `gemma4:26b` | ~3 min | Confident but **wrong** (mis-"corrected" the pragma; ram_s2p inversion; DATAFLOW mismatch) | MoE-A4B ~17GB, CPU offload. Not recommended. |
| `deepseek-r1:14b` | ~30 s | Structured but confidently wrong on ram_s2p | rare CJK token leak; fits 16GB |
| `llama3.1:8b-instruct-q8_0` | ~13 s | Weakest; hedges; one wrong B answer | fits 16GB, fast |

**Decision: a switchable two-profile setup** (the fast/honest vs slow/accurate
split is complementary, not a single winner):

- `CHAT_MODEL` (fast default) = `gemma4:e4b` — GPU-resident, ~50s, honest.
- `CHAT_MODEL_DEEP` = `qwen2.5-coder:32b` — max accuracy when it matters.
- Select per request via the OpenAI `model` suffix: `amd` (fast) vs
  `amd!deep` (deep). `resolveModel()` parses `<collection>[!profile]`;
  `!<ollama:tag>` also works as a literal override. `/v1/models`
  advertises both variants per collection (Open WebUI shows them); the MCP
  `query_pdfs` tool exposes a `deep` boolean. `CHAT_NUM_CTX_DEEP` lets the
  deep profile use a different context size.

#### Gemma 4 config notes (critically adapted, not copied)

Third-party guidance (leetllm.com) recommends `temperature=1.0, top_p=0.95,
top_k=64` and `num_ctx=32768` for Gemma 4, plus a `<|think|>` system-prompt
prefix for thinking mode. Applied judgement for *this* project:

- **Reject `temperature=1.0` for the RAG path.** That is Google's general/
  thinking default; this server does grounded, citation-faithful extraction,
  which needs *low* temperature (~0.2). `server.js` currently sends no
  sampling options (Ollama/model defaults); if we add them they must be
  RAG-tuned, not the blog's creative-mode values.
- **`num_ctx`:** the blog's 32768 is reasonable for `gemma4:e4b` (fits 16GB
  with headroom); keep it model-specific via `CHAT_NUM_CTX` — do NOT pair a
  large ctx with a CPU-offloaded model (qwen32b/gemma4:26b) or latency
  balloons. Default stays 12288.
- **Thinking mode** adds latency/verbosity; not desirable for grounded
  factual extraction — leave off.
- Gemma 4 supports the `system` role (older Gemma didn't), so the existing
  faithfulness system prompt works as-is.

No local model fully matches NotebookLM (Gemini) on questions the manuals
only partially cover — that gap is what `RAG_GROUNDING=augmented` narrows by
letting the model add clearly-tagged general expertise on top of cited
sources. `strict` keeps maximum provenance at the cost of depth.

`CHAT_NUM_CTX` must stay large enough for `TOP_K_RESULTS` chunks + a long
question + the answer; the original abstention/quality failures traced to
Ollama's small default context silently truncating the retrieved sources.

### 5.4 Planned: Gemini `!deep` cloud hybrid (NOT YET IMPLEMENTED)

The local stack has now been exhaustively tuned (every retrieval lever in
§5.1 plus multi-query expansion in `expandQueries`). One failure class
remains, evidence-isolated: **contrastive standards questions** where one
mechanism's vocabulary dominates the question (e.g. *"is TAS the same as a
PSFP stream gate, does it have an IPV?"*). For these, first-stage retrieval
never recalls the TAS-defining `§12.29 Gate Parameter Table` / `§8.6.9`
chunks because **resolving the disambiguation requires knowing
TAS ⇒ scheduled-traffic / 802.1Qbv / Clause 12.29** — domain knowledge the
local generation model **and** the local query-expansion model don't have,
so neither generation nor expansion can bridge it. The same wrong-cite
pattern reproduced across `gemma4:e4b`, `qwen2.5-coder:32b`, with and
without clause-bounded chunking, with and without query expansion (see
[NEXT-STEPS.md](NEXT-STEPS.md) for the benchmark trail).

NotebookLM/Gemini gets this class right because Gemini has the standard's
domain knowledge plus very large context. Hence the planned hybrid:

| Profile | Backend | Privacy | Cost |
|---|---|---|---|
| `<col>` (fast, default) | local `gemma4:e4b` | fully private | free |
| `<col>!deep` | **Gemini paid key** (e.g. 2.x Pro) | no-training data terms | per-token |
| `<col>!gflash` (optional) | Gemini free-tier (Flash) | **used by Google for improvement — non-private** | free, rate-limited |

Implementation is contained — the existing `resolveModel` already supports
arbitrary profiles; only the LLM generation call needs a Gemini provider
keyed off the profile (embeddings/retrieval/grounding/citations are
unchanged and stay local). New env: `GEMINI_API_KEY`,
`GEMINI_API_KEY_DEEP`, `GEMINI_MODEL`, `GEMINI_MODEL_DEEP`.

Status: **deliberately not implemented yet.** It crosses the local/private
line the project was built around. Enable only as a conscious decision.

## 6. One-Time Machine Provisioning

Run from Windows PowerShell.

### 6.1 WSL and GPU prerequisites

```powershell
wsl -l -v
wsl -e bash -lc "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader"
```

### 6.2 Install WSL packages

Install `zstd` (required by the Ollama installer):

```powershell
wsl -e bash -lc "sudo apt-get update && sudo apt-get install -y zstd"
```

Install Ollama:

```powershell
wsl -e bash -lc "curl -fsSL https://ollama.com/install.sh | sh"
```

Install Docker Engine + Compose plugin in Ubuntu 24.04:

```powershell
wsl -e bash -lc "sudo apt-get update && sudo apt-get install -y ca-certificates curl gnupg"
wsl -e bash -lc "sudo install -m 0755 -d /etc/apt/keyrings"
wsl -e bash -lc "[ -f /etc/apt/keyrings/docker.gpg ] && sudo rm -f /etc/apt/keyrings/docker.gpg || true"
wsl -e bash -lc "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor --batch --yes -o /etc/apt/keyrings/docker.gpg"
wsl -e bash -lc "sudo chmod a+r /etc/apt/keyrings/docker.gpg"
wsl -e bash -lc "ARCH=$(dpkg --print-architecture); CODENAME=$(. /etc/os-release && echo $VERSION_CODENAME); echo \"deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${CODENAME} stable\" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null"
wsl -e bash -lc "sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
```

Start Docker in WSL:

```powershell
wsl -e bash -lc "sudo service docker start"
```

### 6.3 Passwordless sudo (required for autostart)

The startup, stop, and restart scripts run inside a **non-interactive**
`wsl -e bash -lc` session, so any `sudo` that prompts for a password will hang.
`scripts/ensure-services.sh`, `stop-local-llm.ps1`, and `restart-local-llm.ps1`
fall back to non-`sudo` `docker` when passwordless sudo is unavailable, but
unattended logon autostart and `sudo service docker start` require it. Either:

- Add the user to the `docker` group (avoids `sudo` for docker entirely):

  ```powershell
  wsl -e bash -lc "sudo usermod -aG docker $USER"
  ```

  (Applies on the next WSL login.)

- Or grant passwordless sudo for the relevant commands via `visudo`.

### 6.4 PDF extraction prerequisite

The extractor (`scripts/extract-pdfs.ps1`) builds a Python venv. Install the
venv package once in WSL:

```powershell
wsl -e bash -lc "sudo apt-get install -y python3-venv"
```

The lightweight backend (`pymupdf4llm`, `pypdf`) installs into that venv
automatically on first run. Optionally `pip install docling` into
`scripts/extract/.venv` for best-quality table/layout extraction.

### 6.5 GPU plumbing for the sd-webui container

#### 6.5.1 NVIDIA Container Toolkit (GPU into Docker)

The `sd-webui` container requests a GPU via
`deploy.resources.reservations.devices: nvidia`. For this to resolve, the
Docker daemon needs the **NVIDIA Container Toolkit** runtime registered. On
Docker Desktop (Windows + WSL2) this is bundled and on by default for recent
versions; on a hand-installed `docker-ce` in WSL it must be added once.

Verify GPU is visible inside a container:

```powershell
wsl -e bash -lc "sudo docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi"
```

Expected: an `nvidia-smi` table identical to the host one. If you get
`could not select device driver "" with capabilities: [[gpu]]`, install the
toolkit in WSL Ubuntu:

```powershell
wsl -e bash -lc "curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg"
wsl -e bash -lc "curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list"
wsl -e bash -lc "sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit"
wsl -e bash -lc "sudo nvidia-ctk runtime configure --runtime=docker && sudo service docker restart"
```

Re-run the verification command. If still failing, also confirm the WSL
distro has GPU passthrough working (§6.1) and that
`/usr/lib/wsl/lib/libcuda.so.1` exists (it should be auto-mounted by WSL).

#### 6.5.2 PyTorch ↔ GPU compute-capability matrix (Blackwell workaround)

A working NVIDIA runtime in Docker is necessary but not sufficient: the
image's PyTorch must have prebuilt CUDA kernels for your GPU's compute
capability (`sm_xy`). The ai-dock `:latest-cuda` tag ships
**torch 2.4.0+cu121** whose kernels stop at `sm_90` — fine for Ampere
(`sm_86`) and Hopper (`sm_90`), but **breaks at generate time on Blackwell**
(`sm_100`/`sm_120`, RTX 50-series) with:

```
RuntimeError: CUDA error: no kernel image is available for execution on the device
```

Evidence captured on RTX 5070 Ti (2026-05-21):

| Wheel | Includes sm_120? | Verdict |
|---|---|---|
| `torch 2.4.0+cu121` (ai-dock default) | no — archs `[sm_50..sm_90]` | fails |
| `torch 2.7.1+cu126` | no — same arch list | fails |
| `torch 2.11.0+cu128` | **yes** — `[sm_75..sm_90, sm_100, sm_120]` | works |

(PyTorch shipped Blackwell kernels in the **cu128** wheels specifically, not
cu126.) The workaround lives in
[scripts/sd-webui-entrypoint.sh](scripts/sd-webui-entrypoint.sh) — a thin
entrypoint wrapper that pip-upgrades to `torch 2.11.0+cu128` into the A1111
venv on every container CREATION, then hands off to ai-dock's normal
`init.sh`. The wrapper is mounted read-only at
`/usr/local/bin/sd-webui-entrypoint.sh` and wired in via `entrypoint:` in
docker-compose.yml. It is idempotent (skips when already on cu128), and a
host-mounted pip cache
(`./storage/sd-webui/pip-cache:/root/.cache/pip`) makes repeat installs
near-instant.

xformers ships compiled against the original `torch 2.4.0+cu121` and is ABI-
incompatible with the upgraded wheel; we disable it implicitly by adding
`--opt-sdp-attention` to `WEBUI_ARGS`, which uses PyTorch's native
scaled-dot-product attention (comparable speed on modern PyTorch).

**Removal criterion:** when ai-dock publishes a `:latest-cuda-12.8+` (or
later) tag whose bundled PyTorch already includes Blackwell kernels, switch
the image tag in docker-compose.yml and delete this wrapper. The wrapper is
a stopgap, not a permanent design.

### 6.6 Install MCP server dependencies

The editor MCP integration (`.vscode/mcp.json`) runs
`node scripts/rag-mcp/index.js`, which needs its npm dependencies installed:

```powershell
.\scripts\wsl-run.ps1 "cd scripts/rag-mcp && npm install"
```

(or run `npm install` in `scripts/rag-mcp` from any shell). The RAG server's
own dependencies install automatically — the `rag-server` container runs
`npm install` on start.

## 7. Model Provisioning

### 7.1 Ollama models (LLM + embeddings)

From repository root:

```powershell
.\scripts\wsl-run.ps1 "chmod +x scripts/bootstrap-models.sh && ./scripts/bootstrap-models.sh"
```

This script pulls:

- `llama3.1:8b-instruct-q8_0`
- `qwen2.5-coder:32b-instruct-q4_K_M`
- `deepseek-r1:14b`
- `gemma4:e4b`, `gemma4:26b`
- `nomic-embed-text`

### 7.2 Stable Diffusion checkpoints (image generation)

The `sd-webui` container starts empty — A1111 will boot and show **no models
available** until at least one `.safetensors` is dropped into
`storage/sd-webui/storage/stable_diffusion/models/ckpt/`. The provisioning
script downloads the project default:

```powershell
.\scripts\download-sd-models.ps1
```

What it does:

- Downloads `Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors` (~6.6 GB)
  from `https://huggingface.co/RunDiffusion/Juggernaut-XL-v9` (public,
  no token required) into the mounted host path above.
- Idempotent: skips if the file is already present at ≥6 GB; restarts the
  download if a previous attempt left a smaller partial file.
- Files placed under `storage/sd-webui/storage/stable_diffusion/models/`
  appear inside the container without restart on the next A1111 model
  rescan (**Settings → Reload UI** in the WebUI, or
  `docker compose restart sd-webui`).

Add more checkpoints by dropping any SDXL/SD1.5 `.safetensors` into the same
folder. The choice of Juggernaut XL v9 as the default — over plain SDXL Base
1.0, DreamShaper XL, or the Lightning variants — was a deliberate trade:
best general-purpose quality at acceptable speed and VRAM cost on a 16 GB
card co-resident with Ollama. Pivot to a **Lightning** variant if VRAM
contention with `!deep` becomes a recurring problem (§12.1).

## 8. Autostart After Machine Reboot

A Windows Startup folder launcher is installed by:

```powershell
.\scripts\install-startup-launcher.ps1
```

This places a `.cmd` file in:

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\start-local-llm.cmd
```

On every Windows logon, that launcher calls `scripts\start-local-llm.ps1` which
runs `scripts/ensure-services.sh` in WSL. That script:

1. Starts the Docker daemon in WSL if not already running.
2. Starts the Ollama service in WSL if not already running.
3. Runs `docker compose up -d` **with no service names** so every service in
   `docker-compose.yml` is brought up (currently: `chroma`, `rag-server`,
   `reranker`, `open-webui`, `sd-webui`). New services added to compose are
   auto-included on the next boot without script edits.
4. Waits for the **critical user path** health endpoints only:
   `chroma /api/v1/heartbeat`, `rag-server /health`, `open-webui /health`.
   The slow first-boot services start **in the background**, intentionally
   un-waited:
   - `reranker` — first boot installs torch + downloads `bge-reranker-base`
     (~5 min). `rag-server` already falls back to fused order when it's not
     ready, so blocking the user's chat access on it is unjustified.
   - `sd-webui` — first boot clones A1111 + installs PyTorch/xformers
     (~10–15 min on a fresh host). Blocking the chat UI on this would defeat
     the autostart UX; the image-gen button in chat will surface a transient
     error until first boot finishes, then work for all subsequent boots
     (~30 s warm restart).
   After the critical waits, the script does a **non-blocking probe** of
   `:7860/sdapi/v1/options` and `:8008/health` and logs `ready` /
   `still booting`, so the autostart log makes the image-gen state obvious.
5. Holds the WSL session open (`exec sleep infinity`) so WSL — and therefore
   Docker — stays alive and reachable from Windows. The launcher process
   therefore stays resident by design; this also means running
   `start-local-llm.ps1` from an interactive terminal will not return.

After first-boot completion, `restart: unless-stopped` on every container
means subsequent WSL/Windows reboots resume all services (including
`sd-webui`) automatically — the explicit `up -d` in step 3 is what guarantees
they exist on the very first run after a fresh clone.

An alternative Task Scheduler method is available via
`scripts\register-autostart-task.ps1` but requires admin elevation, and the
created task runs with limited rights — passwordless sudo (§6.3) is required
for it to start Docker unattended.

## 9. Stack Startup and Validation

```powershell
.\scripts\start-local-llm.ps1            # start + wait for health (then blocks)
.\scripts\stop-local-llm.ps1             # stop all containers
.\scripts\restart-local-llm.ps1          # restart all
.\scripts\restart-local-llm.ps1 open-webui   # restart a single service
```

Validate endpoints directly:

```powershell
wsl -e bash -lc "curl -fsS http://127.0.0.1:8000/api/v1/heartbeat"
wsl -e bash -lc "curl -fsS http://127.0.0.1:3000/health"
wsl -e bash -lc "curl -I http://127.0.0.1:8080"
wsl -e bash -lc "curl -fsS http://127.0.0.1:7860/sdapi/v1/options >/dev/null && echo 'sd-webui: ok'"
wsl -e bash -lc "sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"
```

Expected result:

- Chroma heartbeat returns JSON.
- RAG `/health` returns `{ "ok": true, ... }`.
- Open WebUI responds on `http://localhost:8080`.
- `sd-webui` returns `ok` once first boot is complete (10+ min on a fresh
  install; tail with `docker compose logs -f sd-webui` if still booting).
- All five containers (`chroma`, `rag-server`, `reranker`, `open-webui`,
  `sd-webui`) are `Up`.

## 10. Open WebUI First-Run Access

1. Open `http://localhost:8080` in a browser on Windows.
2. On first launch, create the admin account.
3. In the model selector, choose a pulled Ollama model, or a RAG collection
   (`ieee`, `amd`, `rag-active`) after adding the OpenAI connection — see
   README "PDF RAG — Query from the browser".

## 11. Data Operations

Create one subfolder per collection under `data/` and drop PDFs into it
(e.g. `data/ieee/`, `data/amd/`). Avoid storing the same document under two
filenames in one collection — dedup is keyed on file name, so duplicates are
indexed twice.

Extract PDFs into page-tagged sidecars (recommended — enables table-aware
chunks and page-level citations), then ingest:

```powershell
.\scripts\extract-pdfs.ps1 amd
wsl -e bash -lc "curl -fsS -X POST http://127.0.0.1:3000/collections/amd/ingest"
```

Without the extract step, ingest still works via flat `pdf-parse` text but
loses page citations and table structure. Re-run `extract-pdfs.ps1` after
adding/changing PDFs (unchanged files skip; `-Force` overrides). Ingest only
processes new/changed files.

Query example:

```powershell
wsl -e bash -lc "curl -fsS -H 'Content-Type: application/json' -d '{\"query\":\"summarize the architecture\",\"collection\":\"amd\",\"topK\":5}' http://127.0.0.1:3000/query"
```

If `RAG_API_KEY` is set, add `-H "Authorization: Bearer <key>"` to every call
except `/health`.

## 12. Operational Notes

- Autostart on logon is handled by the Startup folder launcher (§8).
- Container-level auto-recovery is handled by `restart: unless-stopped`.
- Startup ordering between containers is enforced by `condition: service_healthy`.
- First-run `AUTO_INGEST` embeds every PDF in every collection sequentially and
  can take a while for large document sets; the `rag-server` healthcheck has a
  generous `start_period`/retry budget to tolerate this.
- If `rag-server` fails at startup, inspect logs:

  ```powershell
  .\scripts\wsl-run.ps1 "sudo docker compose logs --tail=200 rag-server"
  ```

- If Ollama embedding calls fail, verify Ollama:

  ```powershell
  wsl -e bash -lc "ollama --version && curl -fsS http://127.0.0.1:11434/api/tags"
  ```

### 12.1 Image generation (sd-webui) — full enable runbook

This section is the end-to-end "make image generation actually work"
reference. README's "Image generation (Automatic1111)" is the user-facing
summary; this is the implementation-level detail.

#### 12.1.1 The five things that must be in place

If any of these is missing, image generation either fails or silently
no-ops. They are ordered by where they live, not by user effort.

| # | Layer | What | Configured by | Done once per |
|---|---|---|---|---|
| 1 | Host / Docker | NVIDIA Container Toolkit registered in the Docker daemon (so `deploy.resources.reservations.devices: nvidia` resolves) | §6.5.1 apt install + `nvidia-ctk runtime configure --runtime=docker` + `service docker restart` | Machine |
| 2 | Host disk | At least one `.safetensors` checkpoint under `storage/sd-webui/storage/stable_diffusion/models/ckpt/` | `scripts/download-sd-models.ps1` (idempotent) | Once + per added model |
| 3 | sd-webui container | A1111 running with the `--api` surface and a Blackwell-capable PyTorch in its venv | docker-compose.yml service + `scripts/sd-webui-entrypoint.sh` wrapper | Auto on every container creation |
| 4 | Open WebUI backend | OW configured to talk to sd-webui (engine, base URL, default model, size, steps) | Pre-set via env vars on the `open-webui` service in docker-compose.yml; verifiable in Admin Panel → Settings → Images | Once (env-driven, survives recreate) |
| 5 | Open WebUI chat trigger | An explicit per-chat trigger turned on (Integrations → Images, or per-message button, or LLM tool calling) | Per chat or one-time per-model admin config — see §12.1.5 | Per chat / per click |

**The single most common "image gen isn't working" failure is missing #5.**
The LLM emitting JSON or claiming to generate an image triggers nothing —
Open WebUI does not parse reply content for image intent. The dispatch is
strictly user-triggered or tool-call-triggered.

#### 12.1.2 First-boot behaviour

The ai-dock A1111 image is heavy on first start of a fresh container:

- Initialises supervisord-managed services (caddy, jupyter, syncthing,
  quicktunnel) — these are ai-dock baggage we don't actively use but they
  start anyway. None contend for the GPU.
- Clones the upstream AUTOMATIC1111/stable-diffusion-webui repo into
  `/opt/stable-diffusion-webui` (cached in the container's writable layer).
- Downloads a default SD 1.5 checkpoint
  (`v1-5-pruned-emaonly.safetensors`, ~4 GB) into
  `/opt/stable-diffusion-webui/models/Stable-diffusion/` — this happens
  even though we've bind-mounted our own model dir on top, because
  ai-dock's provisioning runs before our mounts apply during early init.
  Persist it to the host with
  `docker cp local-llm-sd-webui:/opt/stable-diffusion-webui/models/Stable-diffusion/v1-5-pruned-emaonly.safetensors storage/sd-webui/storage/stable_diffusion/models/ckpt/`
  if you want it to survive recreates.
- Runs `scripts/sd-webui-entrypoint.sh` (our wrapper) **before** A1111
  starts, which pip-installs `torch 2.11.0+cu128` + `torchvision` into
  `/opt/environments/python/webui/` for Blackwell support (~2.5 GB
  download, cached in host volume `./storage/sd-webui/pip-cache` so repeat
  recreates are near-instant). See §6.5.2 for why.
- Starts A1111 with `--api --listen --cors-allow-origins=* --no-half-vae
  --opt-sdp-attention` (see service spec in §4 item 5).

Healthcheck `start_period: 900s` (15 min) covers the worst case. Subsequent
boots of the same container are ~30 s.

> **What survives `docker compose down` + `up -d`?**
> - Host-mounted dirs survive unconditionally: checkpoints, LoRAs, VAEs,
>   outputs, pip cache, `/workspace`.
> - The container's writable layer (which holds the A1111 source clone and
>   the ai-dock-auto-downloaded v1-5) is **destroyed by `down`** and
>   recreated on the next `up`. First-boot install runs again — but the
>   pip cache mount makes the torch reinstall fast.
> - The entrypoint wrapper's torch upgrade also re-runs (idempotent; skips
>   when already on a `cu128` build).
>
> So `down/up` costs roughly ~5 min on a warm host (A1111 git clone +
> default v1-5 download), not the full 15 min of true first boot.

#### 12.1.3 Open WebUI backend wiring

docker-compose.yml sets these env vars on `open-webui` so first run is
keystroke-free:

```yaml
- ENABLE_IMAGE_GENERATION=true
- IMAGE_GENERATION_ENGINE=automatic1111
- AUTOMATIC1111_BASE_URL=http://127.0.0.1:7860
- IMAGE_GENERATION_MODEL=Juggernaut-XL_v9_RunDiffusionPhoto_v2
- IMAGE_SIZE=1024x1024
- IMAGE_STEPS=30
```

Verify in browser at Admin Panel → **Settings → Images** — the values above
should be reflected. The **Model** dropdown is populated by a live
`GET /sdapi/v1/sd-models` against sd-webui; an empty dropdown means
sd-webui is unreachable, still booting, or has no `.safetensors` in the
mounted ckpt dir.

> **Env vs config precedence.** OW stores effective config in
> `storage/open-webui/webui.db` (SQLite, `config` table). On startup, env
> vars seed values that aren't already in the DB. Once a user explicitly
> sets a value in the Admin panel, the DB wins and subsequent env changes
> are ignored until either the DB is reset or the value is changed in the
> UI. This means **changing the env vars after first launch may have no
> visible effect** — go to the UI and confirm/correct directly.

#### 12.1.4 Stray OpenAI connection cleanup (SSL handshake errors)

A stray `https://127.0.0.1:3000/v1` entry in
`config.openai.api_base_urls` causes Open WebUI to attempt an SSL handshake
against the plain-HTTP rag-server on every model-list refresh, logging:

```
ERROR | open_webui.routers.openai:send_get_request:121 - Connection error:
Cannot connect to host 127.0.0.1:3000 ssl:default [[SSL: WRONG_VERSION_NUMBER]
```

It's noisy but also masks legitimate connection failures. Fix in the UI
(Settings → Connections, remove or correct the https entry) or directly in
the DB:

```bash
sudo docker exec local-llm-open-webui python3 -c "
import sqlite3, json
db = sqlite3.connect('/app/backend/data/webui.db')
row = db.execute('SELECT data FROM config WHERE id=1').fetchone()
cfg = json.loads(row[0])
old_urls = cfg.get('openai', {}).get('api_base_urls', [])
old_keys = cfg.get('openai', {}).get('api_keys', [])
# api_base_urls and api_keys are positionally aligned — drop the matching key too.
keep = [i for i, u in enumerate(old_urls) if not u.startswith('https://127.0.0.1:3000')]
cfg['openai']['api_base_urls'] = [old_urls[i] for i in keep]
if len(old_keys) == len(old_urls):
    cfg['openai']['api_keys'] = [old_keys[i] for i in keep]
db.execute('UPDATE config SET data=? WHERE id=1', (json.dumps(cfg),))
db.commit()
print('cleaned:', cfg['openai']['api_base_urls'])
" && sudo docker restart local-llm-open-webui
```

#### 12.1.5 Chat-side triggers (the three ways to actually get an image)

Open WebUI does **not** parse the LLM's reply for image-generation intent.
The dispatch must be triggered explicitly. Pick whichever matches user
expectations.

| Trigger | Mechanism | UX | When to pick |
|---|---|---|---|
| **Integrations → Images** | In the chat input, click the **Integrations** icon (puzzle-piece / **+** on older OW) → toggle **Images** on. Every assistant reply in that chat is then ALSO sent to sd-webui (auto-image-per-reply). | "Ask normally, get an image inline with each answer." | Closest match to "ask, get image" intent. Per-chat scope (re-toggle in new chats). Recommended default. |
| **Per-message picture button** | Each assistant reply has a picture icon in its action row — clicking it sends just that reply's text to sd-webui. | One-off, on-demand. | When you only occasionally want an image and don't want auto-gen overhead. |
| **Tool calling (Native FC)** | Admin Panel → Settings → Models → (gear at top right) → **Function Calling = Native**; then per-model toggle **Capabilities → Image Generation** on. LLM emits a `generate_image` tool call which OW catches and dispatches. | "Tell the model to generate an image, it decides when to call the tool." | Most "agentic". Reliability depends on the model's native function-calling quality (Gemma 4 and Llama 3.1 8B work; smaller models are inconsistent). Setup is per-model. |

Each is a real, supported trigger; none of them route based on the LLM
saying "I'll generate an image" or emitting DALL·E-shaped JSON.

#### 12.1.6 End-to-end smoke test (bypasses Open WebUI)

Useful when isolating "is the backend healthy?" from "is OW configured
correctly?":

```bash
# Load default model + generate a 1024x1024 PNG to d:\tmp\sd-smoke-test.png
wsl -e bash -lc "curl -fsS -X POST http://127.0.0.1:7860/sdapi/v1/options \
  -H 'Content-Type: application/json' \
  -d '{\"sd_model_checkpoint\":\"Juggernaut-XL_v9_RunDiffusionPhoto_v2\"}' --max-time 180 && \
  curl -fsS -X POST http://127.0.0.1:7860/sdapi/v1/txt2img \
  -H 'Content-Type: application/json' \
  -d '{\"prompt\":\"a single ripe red apple on a wooden table, soft window light, photorealistic\",\"steps\":20,\"width\":1024,\"height\":1024,\"sampler_name\":\"Euler a\",\"cfg_scale\":6}' \
  --max-time 300 -o /tmp/sd-smoke.json && \
  python3 -c 'import json,base64; png=base64.b64decode(json.load(open(\"/tmp/sd-smoke.json\"))[\"images\"][0]); open(\"/mnt/d/tmp/sd-smoke-test.png\",\"wb\").write(png); print(\"OK:\",len(png),\"bytes\")'"
```

Returns `OK: <bytes>` and writes a PNG on success. On RTX 5070 Ti expect
~6–7 s for the generation step itself; first-call model load adds ~30–60 s.

#### 12.1.7 GPU coexistence with Ollama (16 GB card)

The default config keeps both runtimes resident; CUDA juggles VRAM:

| Loaded together | Approx VRAM | Status |
|---|---|---|
| `gemma4:e4b` (fast) + SDXL Juggernaut XL | ~10 GB / 16 | comfortable headroom |
| `gemma4:e4b` + SDXL during a generation | ~13 GB / 16 | tight on activations, fine |
| `qwen2.5-coder:32b` (`!deep`) + SDXL | exceeds 16 GB | OOM if concurrent |

`!deep` already CPU-offloads on 16 GB; running an SDXL generation
mid-`!deep` answer steals more VRAM and slows both. Practical pattern:
image generation on top of fast-model replies; `!deep` for hard contrastive
questions only; no concurrent use. Pivot to a **Lightning** variant of the
checkpoint (4-step generation, ~3–5 s/image, ~6 GB VRAM peak) if
contention becomes a recurring problem.

#### 12.1.8 Operational commands

```powershell
# Tail sd-webui logs (incl. the [sd-webui-entrypoint] wrapper output)
.\scripts\wsl-run.ps1 "sudo docker compose logs --tail=200 sd-webui"

# List models currently visible to A1111
wsl -e bash -lc "curl -fsS http://127.0.0.1:7860/sdapi/v1/sd-models | python3 -c 'import sys,json; [print(m[\"model_name\"]) for m in json.load(sys.stdin)]'"

# Force A1111 to rescan models/ckpt/ after dropping a new .safetensors in
wsl -e bash -lc "curl -fsS -X POST http://127.0.0.1:7860/sdapi/v1/refresh-checkpoints"

# Inspect currently-active checkpoint
wsl -e bash -lc "curl -fsS http://127.0.0.1:7860/sdapi/v1/options | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"sd_model_checkpoint\"])'"
```

### 12.2 LAN access to Open WebUI (WSL2 mirrored mode)

Open WebUI is localhost-only by default. Exposing it to other LAN devices
under `networkingMode=mirrored` requires opening **two independent firewall
layers** — inbound LAN traffic clears the Windows Defender Firewall on the
physical adapter, then the Hyper-V firewall on the WSL vNIC. Both default to
blocking inbound. Run as Administrator:

```powershell
New-NetFirewallRule -DisplayName "Open WebUI 8080" -Direction Inbound `
  -Action Allow -Protocol TCP -LocalPort 8080 -Profile Any
New-NetFirewallHyperVRule -Name "OpenWebUI-8080" -DisplayName "Open WebUI 8080" `
  -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' `
  -Protocol TCP -LocalPorts 8080 -Action Allow
```

Gotchas:

- **Validate from a separate device**, never the Windows host itself: in
  mirrored mode the host connecting to its own mirrored LAN IP does not loop
  into WSL (only `127.0.0.1` has the localhost relay), so a host-side test of
  `http://<lan-ip>:8080` fails even when LAN access is correct.
- The service binds `0.0.0.0:8080` inside WSL already; this is purely a
  firewall matter, not a binding/config one.
- DHCP lease IP changes on reconnect — use a router reservation for stability.
- Same two-rule pattern exposes any other port. Do not expose `3000`
  (RAG server) without first setting `RAG_API_KEY` — it is unauthenticated.

Full step-by-step (finding the LAN IP, profile/guest-SSID troubleshooting)
is in README → "Access Open WebUI from other devices on your LAN".

Note: `sd-webui` (port `7860`) is not LAN-exposed by default and shouldn't be
— it's reached from Open WebUI's backend over the host loopback. The chat UI
on `:8080` is the user-facing surface; the image-gen API on `:7860` stays
local to the host.

## 13. Configuration for Other Machines

Keep these invariant unless intentionally changed:

1. WSL Ubuntu release (`24.04`) and Docker/Ollama installation method.
2. Chroma image (`chromadb/chroma:0.5.5`) and `/api/v1` usage in `server.js`
   (coupled — see §4).
3. Collection folder convention (`data/<name>`, `data/<name>/.rag-cache/`
   for extraction sidecars, `storage/chroma`).
4. Default ports (`11434`, `8000`, `3000`, `7860`, `8008`, `8080`).
5. Open WebUI persistent storage path (`storage/open-webui`).
6. Run `scripts\install-startup-launcher.ps1` on each new machine for logon
   autostart, and ensure passwordless sudo or `docker` group membership (§6.3).
7. Run `npm install` in `scripts/rag-mcp` on each new machine for the MCP
   integration (§6.6).
8. Run `scripts\extract-pdfs.ps1` after adding/changing PDFs so ingest gets
   table-aware chunks and page citations (the `.venv` and `.rag-cache/` are
   machine-local and rebuildable — safe to gitignore).
9. **Image-gen prerequisites (new):** verify NVIDIA Container Toolkit is
   installed (§6.5.1) and run `.\scripts\download-sd-models.ps1` once to
   populate `storage/sd-webui/storage/stable_diffusion/models/ckpt/` with the
   default Juggernaut XL v9 checkpoint. Without the model file, `sd-webui`
   boots cleanly but Open WebUI's image button surfaces a `no model` error.
10. **sd-webui persistent storage path** (`storage/sd-webui`). Contains the
    checkpoints (`models/ckpt/` — ~6.6 GB+ per model), outputs, ai-dock
    workspace, and the pip cache (`pip-cache/` — ~2.5 GB of cu128 wheels
    after first boot). Heavy; exclude from any "small backup" set;
    rebuildable by re-downloading the image, the model, and letting the
    wrapper re-populate the pip cache.
11. **Entrypoint wrapper for Blackwell GPUs** —
    `scripts/sd-webui-entrypoint.sh` is host-mounted to
    `/usr/local/bin/sd-webui-entrypoint.sh` and set as the container's
    entrypoint (see §6.5.2). On non-Blackwell GPUs the wrapper still runs
    safely (the cu128 wheel includes all earlier archs); the wrapper has no
    operational cost beyond the one-time pip cache population. To skip the
    wrapper on machines that already have a Blackwell-capable bundled image,
    delete the `entrypoint:` line + the wrapper mount from docker-compose.yml.
12. Pre-pointed Open WebUI image-gen env vars in `docker-compose.yml`
    (`ENABLE_IMAGE_GENERATION`, `IMAGE_GENERATION_ENGINE`,
    `AUTOMATIC1111_BASE_URL`, `IMAGE_GENERATION_MODEL`, `IMAGE_SIZE`,
    `IMAGE_STEPS`) — these are what wire the **backend** out-of-the-box.
    Changing them needs a container recreate (`docker compose up -d
    open-webui`), not just a restart, and they only apply on first launch
    when the OW config DB is empty — see §12.1.3.
13. **Per-chat image trigger is NOT covered by env or autostart.** Open
    WebUI does not auto-route LLM replies to image generation; the user
    must explicitly enable a trigger per chat (Integrations → Images, the
    recommended path) or accept the per-message picture-button workflow.
    Tool calling is the third option but requires per-model Admin Panel
    config (Function Calling = Native + Image Generation capability) — see
    §12.1.5. This is the single most common "image gen isn't working"
    failure mode for new installs.

**Relocation:** The project is relocatable. All PowerShell scripts derive the
repo path dynamically from `$PSScriptRoot` — no hardcoded paths. Clone the repo
to any drive or directory and the scripts work without modification.

### 13.1 New-machine quick-start (single sequence)

The canonical fresh-clone bring-up, in order, end-to-end:

```powershell
# 1. WSL + Docker + Ollama + NVIDIA Container Toolkit (§6.1–§6.5)
#    Verify with:
wsl -e bash -lc "sudo docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi"

# 2. Pull Ollama models (§7.1)
.\scripts\wsl-run.ps1 "chmod +x scripts/bootstrap-models.sh && ./scripts/bootstrap-models.sh"

# 3. Download default SDXL checkpoint (§7.2, ~6.6 GB one-off)
.\scripts\download-sd-models.ps1

# 4. Install MCP server deps (§6.6)
.\scripts\wsl-run.ps1 "cd scripts/rag-mcp && npm install"

# 5. Install autostart launcher (§8)
.\scripts\install-startup-launcher.ps1

# 6. First start — brings up every compose service (5+ containers).
#    chroma / rag-server / open-webui health-waited; reranker + sd-webui
#    boot in background (10-15 min first time, ~30 s subsequent).
.\scripts\start-local-llm.ps1

# 7. Optional: drop PDFs into data/<collection>/, then
.\scripts\extract-pdfs.ps1
wsl -e bash -lc "curl -fsS -X POST http://127.0.0.1:3000/collections/<name>/ingest"
```

After this sequence, **every subsequent boot of the Windows host fully
self-activates**: logon → Startup-folder launcher → ensure-services →
`docker compose up -d` resurrects all containers via `restart: unless-stopped`
→ chat + RAG + image generation all ready. No further intervention.
