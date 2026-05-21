# Local LLM Stack (WSL + Docker)

This project runs a local LLM and PDF RAG stack on WSL with Docker.

## Services and URLs

After startup, the following endpoints are available from Windows:

- Open WebUI chat interface: http://localhost:8080
- Ollama API: http://localhost:11434
- RAG server health: http://localhost:3000/health
- Chroma heartbeat: http://localhost:8000/api/v1/heartbeat

## What each service does

- Ollama (`11434`): model runtime and embedding API.
- Open WebUI (`8080`): browser chat interface connected to Ollama.
- RAG server (`3000`): PDF ingestion and retrieval endpoints.
- Chroma (`8000`): vector database for document embeddings.

## Prerequisites

- WSL2 Ubuntu with GPU passthrough working.
- Docker Engine and Compose plugin installed in WSL.
- Ollama installed in WSL.

Reference runbook: see design.md for full machine provisioning and architecture decisions.

## Start / Stop / Restart

All scripts live in `scripts/` and work from any PowerShell terminal.

**Start (or recover after a reboot):**

```powershell
.\scripts\start-local-llm.ps1
```

This brings up Ollama, Docker, and all containers, then waits for every health endpoint before returning.

**Stop everything:**

```powershell
.\scripts\stop-local-llm.ps1
```

**Restart all services:**

```powershell
.\scripts\restart-local-llm.ps1
```

**Restart a single service** (e.g. open-webui):

```powershell
.\scripts\restart-local-llm.ps1 open-webui
```

Check running containers and health:

```powershell
wsl -e bash -lc "sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"
```

## Pull required models

```powershell
.\scripts\wsl-run.ps1 "chmod +x scripts/bootstrap-models.sh && ./scripts/bootstrap-models.sh"
```

Models pulled by default:

- llama3.1:8b-instruct-q8_0
- qwen2.5-coder:32b-instruct-q4_K_M
- deepseek-r1:14b
- gemma4:e4b  (Google Gemma 4 edge, ~9.6GB — fits 16GB VRAM fully)
- gemma4:26b  (Gemma 4 MoE, ~18GB — 4B active params/token; CPU-offloads on 16GB)
- nomic-embed-text

### Generation model & answer tuning

**Two model profiles, switch per request — no restart:**

- **fast** (`CHAT_MODEL`, default `gemma4:e4b`) — GPU-resident, ~50 s,
  honest about doc gaps. Used when the model field has no suffix.
- **deep** (`CHAT_MODEL_DEEP`, default `qwen2.5-coder:32b`) — best accuracy
  on hard technical questions, ~5 min (CPU-offloaded). Request it by
  appending `!deep` to the collection: `amd!deep`, `ieee!deep`,
  `rag-active!deep`.

In Open WebUI the model picker lists both per collection (e.g. `amd` and
`amd!deep`). Via the API set `"model": "amd!deep"`. Via the MCP
`query_pdfs` tool pass `deep: true`. `<collection>!<ollama:tag>` also works
as a literal one-off model override.

Changing the *defaults* (`CHAT_MODEL` / `CHAT_MODEL_DEEP` in
`docker-compose.yml`) needs a container recreate:
`wsl -e bash -lc "cd <repo> && sudo docker compose up -d rag-server"`
(a plain `restart` does **not** pick up env changes).

Other answer knobs (see `.env.example` / design.md §5.1, §5.3):

- `RAG_GROUNDING=augmented` — sources are primary and cited, but the model
  may add general expertise for gaps, tagged and never falsely cited
  (NotebookLM-like). `strict` = sources only, maximum provenance.
- `CHAT_NUM_CTX=12288` — context window; too small silently truncates the
  retrieved sources and causes weak/abstaining answers.
- `TOP_K_RESULTS=8` — retrieved chunks fed to the model.
- `QUERY_EXPANSION=true` / `MAX_SUBQUERIES=5` — fast model decomposes the
  question into focused sub-queries for multi-query retrieval.

### Cloud generation hybrid (Gemini `!deep`) — planned, not implemented

After exhaustive local tuning (structure-aware chunking, hybrid retrieval,
dedupe + canonical preference, cross-encoder reranker, bookmark clause-path
metadata, clause-bounded chunking, deep 32B model, multi-query expansion —
all live), one failure class remains: **contrastive standards questions**
where vocabulary collisions starve first-stage retrieval. Concretely, IEEE
802.1Q TAS vs PSFP vs ATS: a question phrased around "PSFP stream gate / IPV"
never recalls the TAS-defining `§12.29 Gate Parameter Table` / `§8.6.9
Scheduled traffic` chunks, because resolving the disambiguation requires
*knowing* that TAS ⇒ scheduled-traffic / Qbv / Clause 12.29 — domain
knowledge the local generation **and** query-expansion models don't reliably
have. NotebookLM/Gemini gets this class right because Gemini has that
knowledge plus very large context. See [NEXT-STEPS.md](NEXT-STEPS.md) and
design.md §5.4 for the full evidence trail and the planned implementation.

**Planned design** (uses the existing switchable `!profile` architecture):

| Profile | Backend | Privacy | Cost | Use |
|---|---|---|---|---|
| `ieee` (fast, default) | **local `gemma4:e4b`** | fully private | free | everyday, lookups |
| `ieee!deep` | **Gemini paid key** (e.g. 2.x Pro) | no-training data terms | per-token | hard contrastive questions |
| `ieee!gflash` (optional) | **Gemini free-tier key** (Flash) | **used by Google for improvement — not private** | free (rate-limited) | zero-cost trialing only |

Local retrieval / embeddings / Chroma / grounding / citations stay on-box;
only the chosen Gemini profile's generation call egresses. Implementation is
contained: a Gemini provider in `server.js` keyed off the resolved profile,
plus `GEMINI_API_KEY` / `GEMINI_MODEL` env (per profile). This is a deliberate
local-→-cloud line-cross — left as a conscious decision for later rather
than enabled by default.

## Auto-start after reboot

The startup launcher is installed in the Windows user Startup folder and runs automatically at logon:

```
C:\Users\<username>\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\start-local-llm.cmd
```

To re-install it on a new machine:

```powershell
.\scripts\install-startup-launcher.ps1
```

## Use the chat interface

1. Open http://localhost:8080
2. Create your first admin account (first run only).
3. In model selection, choose one of your pulled Ollama models.
4. Start chatting.

If no models appear, verify Ollama is running and models are present:

```powershell
wsl -e bash -lc "ollama list"
```

## Access Open WebUI from other devices on your LAN

By default Open WebUI is only reachable as `http://localhost:8080` from the
Windows host. To reach it from a phone or another PC on the same network
(`http://<windows-lan-ip>:8080`), **two firewall layers** must allow it —
this is specific to WSL2 mirrored networking.

**Prerequisite:** WSL must be in mirrored networking mode. Check
`%USERPROFILE%\.wslconfig` contains:

```ini
[wsl2]
networkingMode=mirrored
```

(If you change this, run `wsl --shutdown` then `.\scripts\start-local-llm.ps1`.)

**1. Find the Windows LAN IP:**

```powershell
Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.*' } |
  Select-Object IPAddress, InterfaceAlias
```

Use the Wi-Fi / Ethernet address (e.g. `192.168.0.83`), not the
`vEthernet (WSL ...)` one.

**2. Open both firewalls (PowerShell as Administrator):**

Inbound LAN traffic hits the **Windows Defender Firewall** on the physical
adapter first, then is mirrored into WSL through the **Hyper-V firewall**.
Both block inbound by default, so you need a rule in each:

```powershell
# Layer 1 — Windows Defender Firewall (physical adapter)
New-NetFirewallRule -DisplayName "Open WebUI 8080" -Direction Inbound `
  -Action Allow -Protocol TCP -LocalPort 8080 -Profile Any

# Layer 2 — Hyper-V firewall (WSL vNIC; {40E0AC32-...} is WSL's fixed ID)
New-NetFirewallHyperVRule -Name "OpenWebUI-8080" -DisplayName "Open WebUI 8080" `
  -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' `
  -Protocol TCP -LocalPorts 8080 -Action Allow
```

No reboot needed; the rules apply immediately.

**3. Test from another device — not the Windows host.** Browse to
`http://<windows-lan-ip>:8080` from a phone or second PC on the same network.

> Testing from the Windows machine itself using its own LAN IP **will fail
> even when LAN access is working correctly** — in mirrored mode the host
> connecting to its own mirrored IP does not loop into WSL; only
> `localhost`/`127.0.0.1` has the special relay. Always validate from a
> separate device.

**Troubleshooting:** if a second device still cannot connect:

- Confirm both devices are on the **same network** (not a guest SSID). Some
  routers (e.g. BT Hub) apply client/AP isolation on guest networks.
- Confirm the active network profile is **Private**, not Public
  (`Get-NetConnectionProfile`).
- The LAN IP is a DHCP lease and can change on reconnect — set a DHCP
  reservation on your router for a stable address.

**Security:**

- Open WebUI requires a login (`WEBUI_AUTH=true`), so LAN exposure is gated.
- Do **not** expose the RAG server port (`3000`) the same way unless you set
  `RAG_API_KEY` first — it is unauthenticated by default. Exposing another
  port uses the identical two-rule procedure with that port number.

## PDF RAG — Setup

The RAG server organises documents into **collections**. Each subfolder under `data/` is one collection. The server detects folders automatically on startup.

### 1. Add a collection folder and drop in PDFs

Create a subfolder for your topic and copy PDFs into it:

```
data\
  ieee\       ← put IEEE papers here
  amd\        ← put AMD documentation here
```

Folders with only a `.gitkeep` placeholder are valid but empty — no indexing occurs until PDFs are present.

### 2. Extract PDFs (recommended — enables tables + page citations)

Convert PDFs into page-tagged JSON sidecars (`data/<collection>/.rag-cache/`).
This preserves tables, sections, and page numbers so answers can cite
"file.pdf — p.12, §3.2". Without this step the server falls back to flat text
extraction (no page citations, tables flattened).

Each chunk's section is taken from the PDF's **bookmark/outline tree** (the
authoritative clause structure, e.g. `12.29.1 The Gate Parameter Table`),
not heuristic heading detection — this both makes citations clause-exact and
lets retrieval separate mechanisms that share vocabulary but live in
different clauses. Re-run `extract-pdfs.ps1 -Force` after changing
`extract.py` (PDFs unchanged but logic changed); plain re-run skips unchanged
files by source mtime.

```powershell
.\scripts\extract-pdfs.ps1            # all collections
.\scripts\extract-pdfs.ps1 amd        # one collection
.\scripts\extract-pdfs.ps1 amd -Force # re-extract even if unchanged
```

First run creates a venv under `scripts/extract/.venv` and installs the
lightweight backend (`pymupdf4llm`). This needs the WSL `python3-venv`
package once:

```powershell
wsl -e bash -lc "sudo apt-get install -y python3-venv"
```

For best quality on dense datasheets, also `pip install docling` into that
venv — `extract.py` picks it up automatically. Re-run after adding or
changing PDFs (unchanged files skip).

### 3. Ingest a collection

Ingest must be triggered once after extracting (or after adding/replacing PDFs). Unchanged files are skipped automatically on subsequent calls.

```powershell
# Ingest the ieee collection
wsl -e bash -lc "curl -fsS -X POST http://127.0.0.1:3000/collections/ieee/ingest"

# Ingest the amd collection
wsl -e bash -lc "curl -fsS -X POST http://127.0.0.1:3000/collections/amd/ingest"
```

Ingest reads the sidecar if present (structure-aware chunks: tables kept
whole, page/section metadata). Retrieval is **hybrid** — dense vector search
fused with BM25 keyword search via Reciprocal Rank Fusion — so exact
identifiers (register names, `0x04`, `AXI_INTC`) are found, not just
semantically-similar prose. Cross-document duplicates (consolidated standard
vs. its amendments vs. ISO reprint) are collapsed with a preference for the
canonical source, then a **cross-encoder reranker** (the `reranker` container)
reorders candidates so the right clause beats vocabulary-colliding
distractors — e.g. IEEE 802.1Q *TAS transmission gate* (Clause 12.29) no
longer pulls *PSFP stream gate* (Clause 12.31) chunks. The reranker is
best-effort: if its container is down or still loading (first boot installs
torch + downloads the model, several minutes), retrieval transparently falls
back to fused order. Answers are grounded: the model is instructed to
answer only from retrieved sources, cite them inline with `[n]`, and abstain
when the sources don't cover the question. Every reply ends with a **Sources**
list (file, page, section).

> Citation rendering in Open WebUI: because the RAG server is an *external*
> OpenAI connection, citations appear as the inline `[n]` markers + the
> Sources list in the message body (Open WebUI's native citation chips are
> tied to its own built-in RAG, not external models). The MCP `query_pdfs`
> tool returns the same cited answer text.

### 4. Set the active collection

The active collection is used by default when no collection is specified in a request. Switching is instant — no re-indexing.

```powershell
wsl -e bash -lc "curl -fsS -X PUT http://127.0.0.1:3000/active-collection -H 'Content-Type: application/json' -d '{\"name\":\"ieee\"}'"
```

Check which collection is currently active:

```powershell
wsl -e bash -lc "curl -fsS http://127.0.0.1:3000/active-collection"
```

List all collections and their ingest status:

```powershell
wsl -e bash -lc "curl -fsS http://127.0.0.1:3000/collections"
```

---

## PDF RAG — Query from the browser (Open WebUI)

The RAG server exposes an OpenAI-compatible endpoint. Open WebUI can connect to it as a second model source, making each collection appear as a selectable model.

**One-time setup:**

1. Open http://127.0.0.1:8080 and sign in.
2. Go to **Settings → Connections**.
3. Under **OpenAI API**, click **Add connection** and enter:
   - **URL:** `http://127.0.0.1:3000/v1`
   - **API key:** `local` (any non-empty value, unless `RAG_API_KEY` is set — see below)
4. Save. The model selector now shows `ieee`, `amd`, and `rag-active` alongside your Ollama models.

> **Authentication:** By default the RAG server is unauthenticated and CORS is
> restricted to localhost (single-user, local-only use). To require a key, set
> `RAG_API_KEY=<secret>` in the `rag-server` environment (`docker-compose.yml`
> or `.env`) and restart it. Then use that exact value as the Open WebUI
> connection API key, and set the same `RAG_API_KEY` for the MCP server.
> `/health` stays open so the Docker healthcheck keeps working.

**Chatting with a collection:**

- Select **`ieee`** to always query the IEEE collection regardless of active-collection state.
- Select **`amd`** to always query the AMD collection.
- Select **`rag-active`** to query whichever collection is currently set as active.

Type your question normally. The RAG server retrieves the most relevant document chunks and passes them to the LLM as context before answering.

---

## PDF RAG — Query from the API

**Query the active collection:**

```powershell
wsl -e bash -lc "curl -fsS -X POST http://127.0.0.1:3000/query -H 'Content-Type: application/json' -d '{\"query\":\"summarize the proposed architecture\",\"topK\":5}'"
```

**Query a specific collection by name:**

```powershell
wsl -e bash -lc "curl -fsS -X POST http://127.0.0.1:3000/query -H 'Content-Type: application/json' -d '{\"query\":\"what power states are supported?\",\"collection\":\"amd\",\"topK\":5}'"
```

**Full RAG chat via OpenAI-compatible endpoint** (e.g. for MCP clients or scripts):

```powershell
wsl -e bash -lc @'
curl -fsS -X POST http://127.0.0.1:3000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "ieee",
    "messages": [{"role": "user", "content": "What is the key contribution of the paper?"}]
  }'
'@
```

The `model` field selects the collection: `ieee`, `amd`, or `rag-active`.

**Streaming responses** are supported — add `"stream": true` to the request body.

The `topK` field is honoured here too — add `"topK": 8` to retrieve more context chunks.

## PDF RAG — Query from the editor (MCP)

`scripts/rag-mcp/` is a stdio MCP server exposing the RAG collections as editor
tools (`query_pdfs`, `list_collections`, `set_active_collection`,
`ingest_collection`). It is registered via [.vscode/mcp.json](.vscode/mcp.json).

**One-time setup** — install its dependencies (not auto-installed, unlike the
containerised RAG server):

```powershell
.\scripts\wsl-run.ps1 "cd scripts/rag-mcp && npm install"
```

If `RAG_API_KEY` is set on the RAG server, add a matching `RAG_API_KEY` to the
`env` block in `.vscode/mcp.json` so the MCP server can authenticate.

## Useful operational commands

Tail Open WebUI logs:

```powershell
.\scripts\wsl-run.ps1 "sudo docker compose logs --tail=150 open-webui"
```

Tail RAG server logs:

```powershell
.\scripts\wsl-run.ps1 "sudo docker compose logs --tail=150 rag-server"
```

Check health of all containers:

```powershell
wsl -e bash -lc "sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"
```
