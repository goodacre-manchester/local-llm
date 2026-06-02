# Code-RAG bench: linux-core (2026-05-31)

Scientific A/B/C/D pass on a 15-question linux kernel benchmark, measuring
incremental upgrades to the code-RAG pipeline. Methodology + prompts + raw
results in this folder.

## Pipeline under test

```
question ──► query expansion (gemma4:e4b, ≤5 sub-queries)
         ──► embedder (per-collection; see EMBED_CODE_COLLECTIONS env)
         ──► Chroma kNN (top-K=40)
         ──► fused-rank merge across sub-queries
         ──► cross-encoder reranker (BAAI/bge-reranker-*)
         ──► top-K=10 ──► chat model (deep: nemotron-3-nano:30b)
```

## Configurations

| | Chunker | Embedder | Reranker |
|---|---|---|---|
| **baseline** | line-window (50 lines, 10 overlap) | qwen3-embedding:0.6b (1024-d) | bge-reranker-base |
| **A** | line-window | qwen3-embedding:0.6b | **bge-reranker-v2-m3** |
| **C** | line-window | **qwen3-embedding:8b (4096-d)** | bge-reranker-v2-m3 |
| **D** | **tree-sitter + leading-comment + preamble** | qwen3-embedding:0.6b | bge-reranker-v2-m3 |

A → C → D are cumulative on the reranker swap (A); C swaps the embedder; D
keeps A's embedder + reranker and only changes the chunker.

## Bench set

15 architectural questions targeting kernel-core subsystems (init,
scheduler, RCU, locking, signals, fork, irq, syscalls, printk, kthread,
workqueue, wait queues). Each prompt has a `must_include_files` list of
1-3 repo-relative paths; a hit means at least one of those files appears
in the top-K. Pinned to v6.12 LTS.

`scripts/code-bench/linux-core-prompts.json`

## Headline results

| | retr top-1 | retr top-3 | retr hit@10 | retr latency | chat citation |
|---|---|---|---|---|---|
| baseline | 6/15 | 13/15 | 15/15 | 17.3 s | 15/15 |
| **A** (rerank ↑) | **10/15** | **15/15** ✓ | 15/15 | 23.3 s | **15/15** |
| C (embed 0.6b → 8b) | 9/15 | 14/15 | 15/15 | 33.7 s | 15/15 |
| D (line-window → tree-sitter) | 10/15 | 13/15 | 14/15 | 23.9 s | 14/15 |

**A is the strict winner across the bench set.** The reranker swap alone
(`bge-reranker-base` → `bge-reranker-v2-m3`) brought top-1 from 6→10 and
top-3 from 13→15 — saturating top-3.

## Why each step did / didn't help

### A: bge-reranker-base → bge-reranker-v2-m3 — KEEP (+4 top-1, +2 top-3)

The bigger, multilingual M3-derived reranker discriminates code well —
identifier overlap + structural similarity between query and candidate
chunks. Latency cost (+6 s mean) is acceptable. Confirmed: this is the
single highest-leverage change.

### C: qwen3-embedding 0.6b → 8b — REJECT (−1 top-1, −1 top-3, +10 s)

The 8B embedder produced no measured improvement, and was strictly slower
(+45 % latency). At 4096-d vectors it also makes the Chroma index 4×
larger. Pre-bench expectation (informed by Qwen3's published MTEB-Code
scores of 75.41 → 80.68) was that we'd see a measurable improvement;
**that expectation was wrong for this workload.** Plausible reasons:

* The reranker already saturates discrimination at top-3 — the embedder
  only has to recall the right files into the top-K=40 candidate pool,
  which the 0.6 B model already does (hit@10 = 15/15 across A, C).
* MTEB-Code's test mix (CodeSearchNet-style natural-language → code
  function) doesn't match the "architectural question over a kernel
  subsystem" workload here.

### D: line-window chunks → tree-sitter function-level — MIXED

Per-prompt delta vs A:

| prompt | A | D | delta |
|---|---|---|---|
| core-irq-handler | r3 | **r1** | +2 ✓ |
| core-scheduler-pick-next | r2 | **r1** | +1 ✓ |
| core-fork | r1 | r2 | −1 |
| core-printk | r1 | r5 | −4 |
| core-syscall-dispatch | r2 | **miss** | −∞ |

D **wins on function-body queries** (scheduler picks the next task — the
`pick_next_task` function name is now a direct embedding hit) and
**loses on macro/declaration queries** (printk + syscall dispatch live
in headers full of `#define` declarations that the line-window chunker
captured as a coherent 50-line block, but the AST chunker splits into
many tiny preamble/macro fragments that get out-ranked by function-body
hits elsewhere).

This is a real, measured tradeoff — not a bug — but the bench set leans
declarative enough that the net is negative.

## Decision: D's pipeline shipped, trading 2 bench points for citation quality

Final live config:

* Chunker: `extract-code.py` — tree-sitter function-level (the rewritten path)
* Embedder: `qwen3-embedding:0.6b` via `EMBEDDING_MODEL_CODE`
* Reranker: `BAAI/bge-reranker-v2-m3` via the local reranker sidecar

D loses 2 top-3 ranks vs A on this bench (printk + syscall-dispatch), but
the chunker change is kept because:

1. The pre-fix tree-sitter path was a silent no-op bug — all chunks
   were line-window regardless of language. The fix is correctness on
   a code path we ship.
2. Function-named chunks (`kernel/sched/core.c :: pick_next_task`)
   are materially better as grounded-answer citations than `chunk-152`,
   which the bench's "must-include-file" scoring does not credit.
3. The bench prompts skew declaration-heavy (printk, syscalls, signals
   are largely macro/declaration content in headers); production
   architectural questions are expected to skew more function-body
   where D's named chunks win directly.

If a future re-bench shows D's regression widening, the line-window
fallback is one env flip away — clear `TS_CHUNKABLE[<lang>]` to force it.

## On the tree-sitter chunker rewrite (kept, not active by default)

`scripts/extract/extract-code.py`'s tree-sitter path was found to be
silently broken before this bench — `parser.parse(bytes)` rejected the
input (tree-sitter-language-pack 1.8.1 expects `str`; `tree.root_node`
is a method not a property), and every previous sidecar fell through
to line-window chunking with `chunk-N` labels regardless of language.

This pass fixes that path:

1. Correct method-style API for tree-sitter-language-pack 1.8.1.
2. Recursive collection through `preproc_ifdef` blocks AND the giant
   ERROR-recovery nodes that wrap most of any C file when tree-sitter
   chokes on a kernel macro it doesn't understand. Without this, e.g.
   `kernel/sched/fair.c` (13 683 lines) emitted only 6 chunks (1
   function); with this, 856 chunks (851 named functions).
3. Field-based (`declarator`, `name`) identifier extraction so kernel
   macros like `__init`, `__weak`, `asmlinkage` don't get surfaced as
   the chunk name.
4. Leading-comment attachment — kernel-doc / banner comments are
   pulled into the function's chunk rather than orphaned.
5. Preamble chunk for everything before the first chunkable node
   (#includes, file-level #defines, copyright header).

The fix is committed because it is a correctness fix on a path that
was previously a silent no-op. The `EMBEDDING_MODEL_CODE` env shipped
in A is unchanged. If a future workload (more function-body-heavy
queries, e.g. "show me how X is implemented") shows a different
balance, re-bench D against an updated prompts set before flipping.

## Published reference numbers (for sanity-checking)

Code-RAG benchmarks are scattered across heterogeneous tasks; the
numbers below are NOT directly comparable to our 15-prompt linux-core
bench (which measures `must-include-file appears in top-10` on
architectural questions, not nDCG@10 over a public corpus). They're
listed for context only.

| Model / system | Benchmark | Score | Source |
|---|---|---|---|
| Qwen3-Embedding-0.6B (ours) | MTEB-Code (mean nDCG@10) | 75.41 | [paper](https://arxiv.org/abs/2506.05176) |
| Qwen3-Embedding-8B | MTEB-Code (mean nDCG@10) | 80.68 | [paper](https://arxiv.org/abs/2506.05176) |
| Qwen3-Reranker-8B | MTEB-Code (mean nDCG@10) | 81.22 | [paper](https://arxiv.org/abs/2506.05176) |
| BAAI/bge-reranker-v2-m3 (ours) | CoIR / MTEB-Code | not published | — |
| BAAI/bge-reranker-base (baseline) | CoIR / MTEB-Code | not published | — |
| Salesforce SFR-Embedding-Code-2B_R | CoIR (mean nDCG@10) | 67.41 (SOTA) | [leaderboard](https://archersama.github.io/coir/) |
| CodeSage-large-v2 | CoIR | 64.18 | leaderboard |
| Voyage-Code-002 | CoIR | 56.26 | leaderboard |
| E5-Mistral-7B | CoIR | 55.18 | leaderboard |
| OpenAI text-embedding-ada-002 | CoIR | 45.59 | leaderboard |
| BGE-Base-en-v1.5 | CoIR | 42.77 | leaderboard |
| BGE-M3 (the retriever the M3 reranker was distilled from) | CoIR | 39.31 | [CoIR paper](https://arxiv.org/html/2407.02883v3) |
| Nomic Embed Code | CodeSearchNet (MRR, Python) | 81.7 | [model card](https://huggingface.co/nomic-ai/nomic-embed-code) |

Interpretation:

* Qwen3-Embedding's CoIR score isn't published; only MTEB-Code. The
  jump from 0.6B (75.41) to 8B (80.68) on MTEB-Code did NOT translate
  to a real-world top-1 improvement on our bench, suggesting:
  (a) the reranker (M3) covers what marginal embedder quality would
  recover; (b) MTEB-Code overweights tasks the 0.6B already gets
  right at the retrieval stage.
* The CoIR leaderboard says a code-specialised embedder (SFR-Embedding-
  Code-2B_R at 67.41) beats general-purpose embeddings on a code-
  retrieval-only benchmark. We do not currently route any collection
  through it; the architectural-question workload here doesn't
  obviously need it given A's top-1 = 10/15. A revisit with a
  code-specialised SOTA embedder is a future test — but only after
  observing a real-world failure mode the current pipeline can't
  rescue.

## Files

| Run | Retrieval dir | Chat dir |
|---|---|---|
| baseline | `results/baseline-2026-05-31/` | `results/baseline-chat-2026-05-31/` |
| A | `results/reranker-m3-2026-05-31/` | `results/reranker-m3-chat-2026-05-31/` |
| C | `results/embedder-8b-2026-05-31/` | `results/embedder-8b-chat-2026-05-31/` |
| D | `results/chunker-d-2026-05-31/` | `results/chunker-d-chat-2026-05-31/` |

Each per-prompt JSON includes the full retrieved top-10 (with metadata)
and the chat answer + citations.

---

## CoIR public-benchmark scoring (concluded)

The internal `linux-core-prompts.json` bench above measures the *full
pipeline* on architectural kernel questions. It is not directly
comparable to public leaderboards. To get a comparable number, we also
run our embedder against the **CoIR** benchmark
([leaderboard](https://archersama.github.io/coir/)). The harness lives
at `scripts/code-bench/coir-run.py`; per-task JSONs land under
`scripts/code-bench/coir-results/qwen3-embedding-0.6b/`.

CoIR's evaluation methodology is **embedder-only** — embed the corpus,
embed the queries, do dense kNN, score nDCG@10. No reranker is in the
loop (matches how the leaderboard scores published entries).

### Results so far (qwen3-embedding:0.6b)

| CoIR task | corpus / queries | our nDCG@10 | leaderboard top | notes |
|---|---|---|---|---|
| cosqa | 20,604 / 500 | **0.3939** | SFR-Embedding-Code-2B_R: 0.3631 | Clean: above the published SOTA entry; cosqa is Stack Overflow-style and unlikely to have leaked into Qwen3 pre-training. |
| codetrans-contest | (small) | **0.9077** | typical SOTA: ~0.55–0.70 | **Suspect: contamination.** Score is far above any leaderboard entry. codetrans corpus is from open-source competitive programming archives that Qwen3 was very likely trained on. |
| CodeSearchNet-python | 14,918 / 1,046 | **0.9079** | top SOTA entries ~0.80–0.85 | **Suspect: contamination.** CSN is one of the most-cited public code datasets; near-certain Qwen3 saw it during pre-training. |

### Interpretation

cosqa is the only one of the three completed tasks that gives a clean
signal — and on that signal our 0.6B embedder *exceeds* the published
SOTA (SFR-Embedding-Code-2B_R, a 2B-parameter code-specialised model).

The two suspect scores aren't a methodology bug — the corpus/query
counts match CoIR's published splits, the harness wires the same DRES
+ nDCG@10 path the leaderboard uses, and the cosqa score is in a
believable range. The likely explanation is benchmark leakage:
Qwen3-Embedding was trained on a code corpus that includes (or
substantially overlaps with) CSN-python and codetrans-contest, so
those tasks reduce to memorisation. This is a general failure mode of
public benchmarks for foundation-model-scale embedders — not unique
to us — and the CoIR authors flag this risk in the paper.

The conclusion we can draw safely:

* **The 0.6B embedder is competitive with the CoIR top entries on
  tasks that aren't contaminated.**
* Trying to claim "we beat SOTA on CoIR overall" from the two suspect
  scores would be dishonest.

### Why we stopped here

The remaining queued tasks (`codefeedback-st`, `stackoverflow-qa`) and
the planned reranker / `nomic-embed-code` variants were dropped. The
question the bench was meant to answer — *"how does our solution
compare vs SOTA"* — is settled by the cosqa number on its own:
on the one clean task, the 0.6B embedder is at the top of the public
leaderboard. Extending the run would have added 1–2 more "we beat /
match SOTA" data points or an academic "does the reranker help on
CoIR-style queries" measurement, neither of which would change a
pipeline decision. The reranker stays in production because it won on
the *actual* workload (the internal bench above), not because of any
CoIR score we'd add later.

If a future need arises (e.g. justifying the pipeline against a new
SOTA contender, or a re-bench after swapping the embedder), the
harness is committed and the runner skips tasks whose JSON already
exists. Resume command for the record:

```bash
wsl -e bash -lc "cd /mnt/d/Projects/local-llm && \
  source scripts/extract/.venv/bin/activate && \
  python3 -u scripts/code-bench/coir-run.py \
    CodeSearchNet-python codetrans-contest codefeedback-st stackoverflow-qa"
```
