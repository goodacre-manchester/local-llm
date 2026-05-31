#!/usr/bin/env python3
"""
Code-RAG benchmark runner.

Reads a prompts file (e.g. linux-core-prompts.json) and runs each query
against the live rag-server. Records:

  - Retrieval results from POST /query (fast, no LLM): which file_paths
    appear in the top-K chunks, and at what rank the FIRST expected
    file shows up.
  - Optionally: chat results from POST /v1/chat/completions (slow, runs
    the full LLM). Captures the response text + the cited filenames.

Writes a JSON report to scripts/code-bench/results/<run-id>/<prompt>.json
and prints a compact table to stdout.

Usage:
    python run.py <prompts-file> <run-id> [--chat]
    python run.py linux-core-prompts.json baseline-2026-05-31

If --chat is given, also runs each query through /v1/chat/completions
using the prompts file's `collection` field as the model. Takes much
longer; use for the final eval pass once retrieval is happy.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path


SERVER = os.environ.get("RAG_SERVER", "http://127.0.0.1:3000")
TIMEOUT = int(os.environ.get("BENCH_TIMEOUT", "300"))


def post(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def score_retrieval(results: list[dict], expected: list[str]) -> dict:
    """For each retrieved chunk, check if any expected file path appears
    in its metadata. The path can live in:
      - metadata.section (e.g. "init/main.c :: chunk-3") — primary for code RAG
      - metadata.fileName (the encoded form, e.g. "init__main.c") — backup match
        with / mapped to __
    Substring match either way, since `expected` entries may be partial
    paths like 'kernel/sched/' or full paths like 'kernel/fork.c'.
    """
    matched: set[str] = set()
    hit_rank = None
    for i, chunk in enumerate(results, 1):
        meta = chunk.get("metadata") or {}
        section = (meta.get("section") or "").lower()
        # fileName has / -> __ encoding; translate back for the substring check.
        filename_decoded = (meta.get("fileName") or "").replace("__", "/").lower()
        # Belt-and-braces: also expose file_path if a future extractor lifts it.
        fp = (meta.get("file_path") or "").lower()
        haystack = f"{section} {filename_decoded} {fp}"
        for exp in expected:
            if exp.lower() in haystack:
                matched.add(exp)
                if hit_rank is None:
                    hit_rank = i
    return {
        "hit": bool(matched),
        "hit_rank": hit_rank,
        "matched": sorted(matched),
        "expected": expected,
        "topk_files": [
            ((c.get("metadata") or {}).get("section")
             or (c.get("metadata") or {}).get("fileName", "").replace("__", "/")
             or "")
            for c in results
        ],
    }


def run_retrieval(prompt: dict, collection: str, top_k: int) -> dict:
    t0 = time.time()
    resp = post(
        f"{SERVER}/query",
        {"query": prompt["query"], "collection": collection, "topK": top_k},
    )
    dt = time.time() - t0
    results = resp.get("matches") or []
    score = score_retrieval(results, prompt.get("must_include_files") or [])
    score["latency_s"] = round(dt, 2)
    score["n_results"] = len(results)
    return score


def run_chat(prompt: dict, model: str) -> dict:
    t0 = time.time()
    resp = post(
        f"{SERVER}/v1/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt["query"]}],
            "stream": False,
        },
    )
    dt = time.time() - t0
    # Citations come back as a top-level `citations` array per our server.js.
    citations = resp.get("citations") or []
    answer = (resp.get("choices", [{}])[0].get("message") or {}).get("content") or ""
    expected = prompt.get("must_include_files") or []
    # fileName uses / -> __ encoding for code RAG sidecars; translate back.
    cited_files = [
        (c.get("fileName") or "").replace("__", "/")
        for c in citations
    ]
    matched = sorted({
        exp for exp in expected
        for cf in cited_files if exp.lower() in cf.lower()
    })
    return {
        "hit": bool(matched),
        "matched": matched,
        "expected": expected,
        "cited_files": cited_files,
        "answer_chars": len(answer),
        "answer_head": answer[:240],
        "latency_s": round(dt, 1),
    }


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    do_chat = "--chat" in argv
    if len(args) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    prompts_path = Path(args[0]).resolve()
    run_id = args[1]
    if not prompts_path.is_file():
        # Allow naked file name relative to this script's dir.
        prompts_path = (Path(__file__).parent / args[0]).resolve()
    if not prompts_path.is_file():
        print(f"prompts file not found: {args[0]}", file=sys.stderr)
        return 2

    cfg = json.loads(prompts_path.read_text("utf-8"))
    collection = cfg["collection"]
    top_k = cfg.get("topK", 10)
    prompts = cfg["prompts"]

    out_dir = (Path(__file__).parent / "results" / run_id).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nCode-RAG bench run: {run_id}")
    print(f"  collection: {collection}, top-K: {top_k}, prompts: {len(prompts)}")
    print(f"  chat mode: {'yes' if do_chat else 'no (retrieval only)'}\n")

    summary = []
    for p in prompts:
        rid = p["id"]
        print(f"  [{rid}] {p['query'][:80]}")
        rec = {"prompt": p}
        rec["retrieval"] = run_retrieval(p, collection, top_k)
        r = rec["retrieval"]
        verdict = ("✓ rank " + str(r["hit_rank"])) if r["hit"] else "✗ miss"
        print(f"      retrieval: {verdict}  matched={r['matched']}  ({r['latency_s']}s)")
        if do_chat:
            rec["chat"] = run_chat(p, collection)
            c = rec["chat"]
            cv = "✓" if c["hit"] else "✗"
            print(f"      chat:      {cv}  cited={c['cited_files']}  ({c['latency_s']}s, {c['answer_chars']} chars)")
        (out_dir / f"{rid}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), "utf-8")
        summary.append(rec)

    # ── Summary ────────────────────────────────────────────────────────────
    n = len(summary)
    retr_hits = sum(1 for r in summary if r["retrieval"]["hit"])
    retr_top1 = sum(1 for r in summary if r["retrieval"]["hit_rank"] == 1)
    retr_top3 = sum(1 for r in summary if (r["retrieval"]["hit_rank"] or 99) <= 3)
    print()
    print(f"Summary [{run_id}]")
    print(f"  retrieval hit@{top_k}: {retr_hits}/{n} ({100*retr_hits//n}%)")
    print(f"  retrieval top-1:     {retr_top1}/{n}")
    print(f"  retrieval top-3:     {retr_top3}/{n}")
    print(f"  mean latency:        {sum(r['retrieval']['latency_s'] for r in summary)/n:.2f}s")
    if do_chat:
        chat_hits = sum(1 for r in summary if r["chat"]["hit"])
        print(f"  chat citation hit:  {chat_hits}/{n}")
        print(f"  mean chat latency:  {sum(r['chat']['latency_s'] for r in summary)/n:.1f}s")

    # Write a compact summary row that's easy to diff between runs.
    summary_path = out_dir / "_summary.json"
    summary_path.write_text(json.dumps({
        "run_id": run_id,
        "collection": collection,
        "topK": top_k,
        "n_prompts": n,
        "retrieval_hit": retr_hits,
        "retrieval_top1": retr_top1,
        "retrieval_top3": retr_top3,
        "mean_latency_s": round(sum(r["retrieval"]["latency_s"] for r in summary)/n, 2),
        "chat_mode": do_chat,
        "chat_hit": (sum(1 for r in summary if r["chat"]["hit"]) if do_chat else None),
    }, indent=2), "utf-8")
    print(f"  report dir: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
