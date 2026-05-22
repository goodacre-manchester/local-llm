# Local LLM Stack (WSL + Docker)

This project runs a local LLM and PDF RAG stack on WSL with Docker.

## Services and URLs

After startup, the following endpoints are available from Windows:

- Open WebUI chat interface: http://localhost:8080
- Ollama API: http://localhost:11434
- RAG server health: http://localhost:3000/health
- Chroma heartbeat: http://localhost:8000/api/v1/heartbeat
- Stable Diffusion WebUI (Automatic1111): http://localhost:7860

## What each service does

- Ollama (`11434`): model runtime and embedding API.
- Open WebUI (`8080`): browser chat interface connected to Ollama.
- RAG server (`3000`): PDF ingestion and retrieval endpoints.
- Chroma (`8000`): vector database for document embeddings.
- sd-webui (`7860`): Automatic1111 Stable Diffusion WebUI; Open WebUI's per-message "image" button POSTs prompts here (see [Image generation (Automatic1111)](#image-generation-automatic1111)).

## Prerequisites

- WSL2 Ubuntu with GPU passthrough working (Ollama uses the GPU natively).
- Docker Engine and Compose plugin installed in WSL.
- **NVIDIA Container Toolkit** installed in Docker — required by the
  `sd-webui` container's GPU reservation. Bundled with Docker Desktop;
  on hand-installed `docker-ce` in WSL it must be added once (see
  [design.md §6.5](design.md)). Verify with:
  ```powershell
  wsl -e bash -lc "sudo docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi"
  ```
- Ollama installed in WSL.

Reference runbook: see [design.md](design.md) for full machine provisioning,
architecture decisions, and the new-machine quick-start sequence in §13.1.

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

## Image generation (Automatic1111)

Open WebUI has native image-generation support. With the `sd-webui` service in
[docker-compose.yml](docker-compose.yml) wired in, any assistant reply in the
chat gets a small **image** (picture) button — click it and the assistant's
text is sent as a prompt to the local Stable Diffusion WebUI, which renders an
image and inlines it into the conversation. No LLM-side "create image"
protocol is needed; the LLM just produces a normal text reply and Open WebUI
treats the click as a separate text→image dispatch.

### The five things that must be in place

If anything below is missing or skipped, image generation either doesn't work
or fails silently. Each step links to its detailed subsection below.

| # | What | Where | Done once per |
|---|---|---|---|
| 1 | **NVIDIA Container Toolkit** in the Docker daemon | [design.md §6.5.1](design.md) | Machine |
| 2 | **SDXL checkpoint on disk** (`.safetensors` in the host model dir) | [§1 below](#1-download-a-base-model-66-gb) | Once |
| 3 | **sd-webui container running + first boot complete** (incl. Blackwell torch upgrade on RTX 50-series) | [§2 below](#2-start-the-service-fully-automatic) | Auto on every boot |
| 4 | **Open WebUI backend wired** to `http://127.0.0.1:7860` (pre-set via env; verify in Settings → Images) | [§3 below](#3-wire-open-webui-to-it) | Once (env-driven) |
| 5 | **A chat trigger turned on** (Integrations → Images, per-message button, or tool calling) — Open WebUI does NOT auto-route based on LLM reply content | [§4 below](#4-generate-images-from-chat) | Per chat / per click |

Step 5 is the one most users miss — the LLM emitting "I'll create an image…"
or DALL·E-shaped JSON does **nothing** on its own. The trigger has to be
explicit. See §4 below for the three trigger modes.

### 1. Download a base model (~6.6 GB)

The default checkpoint is **Juggernaut XL v9** (RunDiffusion's flagship SDXL
fine-tune — strong general-purpose, photoreal-leaning, commercial-friendly
licence). One-shot:

```powershell
.\scripts\download-sd-models.ps1
```

This downloads `Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors` from
HuggingFace into [storage/sd-webui/storage/stable_diffusion/models/ckpt/](storage/sd-webui/storage/stable_diffusion/models/ckpt/).
Idempotent — re-runs only fetch what's missing or partial. Add more models by
dropping any SDXL/SD1.5 `.safetensors` into the same folder and clicking
**Settings → Reload UI** at http://localhost:7860, or just
`docker compose restart sd-webui`.

### 2. Start the service (fully automatic)

`sd-webui` is part of the standard autostart chain — it's brought up by
[scripts/ensure-services.sh](scripts/ensure-services.sh) (called by
[start-local-llm.ps1](scripts/start-local-llm.ps1), itself called by the
Startup-folder launcher on every Windows logon). After the one-time first
boot, **you never need to touch it again** — `restart: unless-stopped` on
the container plus the autostart launcher means every WSL/Windows restart
re-activates image generation alongside chat and RAG.

**First boot is slow** — the ai-dock image clones A1111, installs A1111's
own dependencies, and downloads a default SD 1.5 checkpoint (10–15 minutes
on a fresh host). On RTX 50-series (Blackwell) cards, the
[scripts/sd-webui-entrypoint.sh](scripts/sd-webui-entrypoint.sh) wrapper
additionally pip-installs `torch 2.11.0+cu128` into the A1111 venv before
A1111 starts (adds ~3 min on first run; near-instant thereafter thanks to
the host-mounted pip cache). See [design.md §6.5](design.md) for why this
is needed — short version: ai-dock's image ships `torch 2.4.0+cu121` whose
CUDA kernels only go up to `sm_90`, and RTX 50-series cards are `sm_120`.

The autostart script does **not** block on first boot — chat is reachable
immediately; the image-generation button will surface a transient error
until first boot completes, then work on every subsequent reply. The
autostart log prints `sd-webui (image generation): ready` once it's up.
Subsequent boots take ~30 s.

Verify it's up:

```powershell
wsl -e bash -lc "curl -fsS http://127.0.0.1:7860/sdapi/v1/options >/dev/null && echo 'sd-webui: ok'"
```

Tail first-boot progress:

```powershell
.\scripts\wsl-run.ps1 "sudo docker compose logs -f sd-webui"
```

### 3. Wire Open WebUI to it

[docker-compose.yml](docker-compose.yml) pre-sets the engine env vars
(`IMAGE_GENERATION_ENGINE=automatic1111`, `AUTOMATIC1111_BASE_URL=http://127.0.0.1:7860`,
default model + size + steps), so for most setups **no admin-panel clicks are
needed** — `docker compose up -d open-webui` after the env change is enough.

To verify or override interactively in the browser:

1. http://localhost:8080 → **Admin Panel** (top-right user menu)
2. **Settings → Images**
3. **Image Generation Engine:** `Default (Automatic1111)`
4. **AUTOMATIC1111 Base URL:** `http://127.0.0.1:7860`
5. **Default Model:** `Juggernaut-XL_v9_RunDiffusionPhoto_v2`
   (drop-down populates from sd-webui's `/sdapi/v1/sd-models` — if it's empty,
   the service is still booting or the model file isn't in `models/ckpt/`)
6. **Image Size:** `1024x1024` (SDXL native — anything smaller degrades quality)
7. **Steps:** `30` (good speed/quality balance; 50 for max quality, 20 for fast)
8. **Save**

While you're in Admin Panel, also check **Settings → Connections** for any
stray `https://127.0.0.1:3000/v1` entry — it will spam SSL-handshake errors
in the open-webui log against the plain-HTTP rag-server. The correct entry
is `http://127.0.0.1:3000/v1`. Remove duplicates / fix the scheme and save.
(If editing in the UI is awkward, [design.md §12.1](design.md) documents the
direct SQLite cleanup recipe used during install.)

### Smoke-test the backend without Open WebUI

Useful when you want to know the bug is in the chat layer vs the image
backend. Loads the default checkpoint and generates a 1024×1024 PNG to
`d:\tmp\sd-smoke-test.png`:

```powershell
wsl -e bash -lc "curl -fsS -X POST http://127.0.0.1:7860/sdapi/v1/options \
  -H 'Content-Type: application/json' \
  -d '{\"sd_model_checkpoint\":\"Juggernaut-XL_v9_RunDiffusionPhoto_v2\"}' \
  --max-time 180 && \
  curl -fsS -X POST http://127.0.0.1:7860/sdapi/v1/txt2img \
  -H 'Content-Type: application/json' \
  -d '{\"prompt\":\"a single ripe red apple on a wooden table, soft window light, photorealistic\",\"steps\":20,\"width\":1024,\"height\":1024,\"sampler_name\":\"Euler a\",\"cfg_scale\":6}' \
  --max-time 300 -o /tmp/sd-smoke.json && \
  python3 -c 'import json,base64; png=base64.b64decode(json.load(open(\"/tmp/sd-smoke.json\"))[\"images\"][0]); open(\"/mnt/d/tmp/sd-smoke-test.png\",\"wb\").write(png); print(\"OK:\",len(png),\"bytes\")'"
```

If this returns `OK: <bytes>` and the PNG looks right, sd-webui is fully
healthy and anything still broken is in Open WebUI's chat-trigger config (§4
below), not the backend.

### 4. Generate images from chat

Open WebUI does **not** route image generation based on the LLM's response
text — even if the model emits DALL·E-shaped JSON, OW just renders it as
plain text. Image generation has to be triggered explicitly via one of:

- **Auto-image per reply (Integrations menu — recommended for "ask, get
  image"):** in the chat input area, click the **Integrations** icon (it's
  the `+` / puzzle-piece button left of the message box — labelled
  **Integrations** on newer OW versions, **More** / **+** on older ones) →
  toggle **Images** on. From then on, every assistant reply *in that chat*
  also runs through sd-webui and inlines a generated image below the text.
  This is the trigger that makes natural-language requests like "create an
  image of …" actually produce an image. Costs ~6–10 s and ~8 GB VRAM per
  reply on SDXL — toggle it off when you don't want images.
- **Per-message manual:** every assistant reply has an **image** icon in its
  message toolbar — click it; the reply's text becomes the prompt; the
  rendered image appears as a follow-up. Use when you only occasionally want
  an image and don't want auto-gen running on every reply.
- **Tool calling (advanced, optional):** Admin Panel → Settings → Models →
  set **Function Calling = Native** globally, then on each model toggle the
  **Image Generation** capability under **Capabilities** / **Default
  Features**. With this on, the LLM emits a `generate_image` tool call
  which OW catches and dispatches to A1111. Requires the model to do native
  function calling reliably; not all local models do.

### GPU coexistence with Ollama (16 GB card)

The current config keeps both runtimes resident — CUDA juggles the VRAM:

| Loaded together | Approx VRAM | Status |
|---|---|---|
| `gemma4:e4b` (fast) + SDXL (Juggernaut XL) | ~10 GB / 16 | comfortable headroom |
| `gemma4:e4b` + SDXL during a generation | ~13 GB / 16 | fine, tight on activations |
| `qwen2.5-coder:32b` (`!deep`) + SDXL | exceeds 16 GB | OOM if both active concurrently |

`!deep` already CPU-offloads on a 16 GB card, so running an SDXL generation
mid-`!deep`-answer just steals more VRAM and slows both. Practical pattern:
use `!deep` for hard contrastive lookups, fast model for everything else, and
image generation only on top of fast-model replies. If you hit OOM, either
generate images between `!deep` turns rather than during them, or switch the
checkpoint to a **Lightning** variant (4-step generation, ~3-5 s/image,
much shorter VRAM-occupancy window).

### Troubleshooting

- **"I asked the model to make an image and got JSON/text back":** Open
  WebUI does NOT route based on the LLM's reply content. The model emitting
  DALL·E-shaped JSON or text like "I'll generate an image…" does nothing on
  its own. You need an explicit trigger — easiest is **Integrations →
  Images** in the chat input (§4 above). Confirm: the input box should show
  a small **Images** chip/badge when the toggle is on.
- **Open WebUI says "Failed to generate image":** check sd-webui logs —
  `.\scripts\wsl-run.ps1 "sudo docker compose logs --tail=200 sd-webui"`.
  Most common causes: model file missing from `models/ckpt/`, first boot
  still installing, or VRAM OOM (see table above). Run the curl smoke test
  in §3 to isolate backend vs Open WebUI.
- **Model drop-down is empty in Admin → Images:** sd-webui is reachable
  (`curl http://127.0.0.1:7860/sdapi/v1/options` works) but no `.safetensors`
  is in `models/ckpt/`. Re-run `.\scripts\download-sd-models.ps1` and
  `docker compose restart sd-webui`.
- **Open WebUI log spams `[SSL: WRONG_VERSION_NUMBER]` against
  `127.0.0.1:3000`:** there's a stray `https://127.0.0.1:3000/v1` entry in
  the OpenAI connections list. Fix in Admin Panel → **Settings →
  Connections** by removing/correcting the https entry (the rag-server is
  plain HTTP). See [design.md §12.1](design.md) for the direct SQLite
  cleanup recipe if the UI fights you.
- **GPU not visible inside the container:** confirm NVIDIA Container Toolkit
  is installed in the Docker daemon (Docker Desktop bundles it; hand-installed
  `docker-ce` in WSL does NOT — see [design.md §6.5.1](design.md) for the
  install procedure). Test:
  `wsl -e bash -lc "sudo docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi"`.
- **"CUDA error: no kernel image is available for execution on the device"
  in sd-webui logs:** the container's PyTorch lacks kernels for your GPU's
  compute capability. The
  [scripts/sd-webui-entrypoint.sh](scripts/sd-webui-entrypoint.sh) wrapper
  upgrades to `torch 2.11.0+cu128` (covers `sm_100`/`sm_120` Blackwell) on
  every container creation; if you see this error, the wrapper either didn't
  run or its `pip install` failed. Check the boot log for the
  `[sd-webui-entrypoint]` lines and confirm the wrapper file is mounted at
  `/usr/local/bin/sd-webui-entrypoint.sh`. For non-Blackwell cards needing
  a different arch, edit `TARGET_TORCH` in the wrapper. Full background in
  [design.md §6.5.2](design.md).
- **xformers warning at sd-webui startup ("xformers can't load C++/CUDA
  extensions"):** expected and harmless. The image's bundled xformers is
  ABI-pinned to torch 2.4.0+cu121, so it can't load against the upgraded
  cu128 torch. We use `--opt-sdp-attention` in `WEBUI_ARGS` instead
  (PyTorch's native scaled-dot-product attention, comparable speed). No
  action needed.

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
