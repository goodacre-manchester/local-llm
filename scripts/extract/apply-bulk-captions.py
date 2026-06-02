#!/usr/bin/env python3
"""
Bulk-caption picture blocks in a Phase-H sidecar via perceptual-hash
clustering. Skips the VLM for repeated decorative imagery (logos,
section dividers) that recur thousands of times across a corpus.

Workflow:
  1. Walk every picture block in <data_dir>/<collection>/.rag-cache/*.json
  2. Re-hash the PNG referenced by `image_path` with dHash-16
  3. Union-find cluster all hashes at a Hamming threshold (default 12)
  4. Match each cluster's representative against the LABEL_MAP below
  5. If the cluster has a canned label, fill `vlm_description` and append
     to `text` (same shape as caption-images.py). Otherwise leave the
     block alone for caption-images.py to handle.

Designed to be RE-RUN-SAFE: if a block already has a non-empty
`vlm_description` from a prior run or from the VLM captioner, we skip
it unless --force is passed.

Usage:
    python apply-bulk-captions.py <data_dir> <collection>
    python apply-bulk-captions.py /mnt/d/Projects/local-llm/data seccom
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image


# ---- LABEL MAP -----------------------------------------------------------
# For each labelled cluster, we store ONE representative image filename
# (relative to the collection's .rag-images/<pdf_stem>/ folder). At
# apply-time, we compute that representative's dHash and bulk-label every
# picture block whose dHash falls within HAMMING_THRESHOLD of it.
#
# Clusters tagged `None` are intentionally NOT bulk-labelled — they're
# content figures whose specific text content differs per occurrence and
# must be VLM-captioned individually. Those blocks are left untouched so
# caption-images.py picks them up.

LABEL_MAP: dict[str, tuple[str, str]] = {
    # cluster_id: (representative_path, canned_caption)
    "bt_logo_blue_crop":  ("BT-Core_v6.0/p1-pic2.png",      "Bluetooth logo (decorative header/footer)."),
    "bt_logo_bluet_crop": ("BT-Core_v6.0/p1064-pic1.png",   "Bluetooth logo (decorative header/footer)."),
    "bt_logo_bluet_v2":   ("BT-Core_v6.0/p1005-pic1.png",   "Bluetooth logo (decorative header/footer)."),
    "bt_logo_blue_v2":    ("BT-Core_v6.0/p1077-pic1.png",   "Bluetooth logo (decorative header/footer)."),
    "bt_logo_blue_v3":    ("BT-Core_v6.0/p1181-pic2.png",   "Bluetooth logo (decorative header/footer)."),
    "bt_logo_bluet_v3":   ("BT-Core_v6.0/p1347-pic2.png",   "Bluetooth logo (decorative header/footer)."),
    "bt_logo_wordmark":   ("BT-Core_v6.0/p1-pic1.png",      "Bluetooth wordmark logo (decorative)."),
    "bt_logo_bluet_v4":   ("BT-Core_v6.0/p1435-pic1.png",   "Bluetooth logo (decorative header/footer)."),
    "bt_logo_bluet_v5":   ("BT-Core_v6.0/p1058-pic3.png",   "Bluetooth logo (decorative header/footer)."),
    "bt_logo_bluet_v6":   ("BT-Core_v6.0/p1916-pic1.png",   "Bluetooth logo (decorative header/footer)."),
    "bt_logo_bluet_v7":   ("BT-Core_v6.0/p1594-pic1.png",   "Bluetooth logo (decorative header/footer)."),
    "bt_divider_stars_1": ("BT-Core_v6.0/p3261-pic1.png",   "Section divider (decorative row of asterisks)."),
    "bt_divider_stars_2": ("BT-Core_v6.0/p3272-pic1.png",   "Section divider (decorative row of asterisks)."),
    "bt_divider_stars_3": ("BT-Core_v6.0/p3261-pic2.png",   "Section divider (decorative row of asterisks)."),
    "bt_faint_line":      ("BT-Core_v6.0/p3278-pic2.png",   "Decorative horizontal line (low-contrast separator)."),
}


HAMMING_THRESHOLD = 12  # dHash-16 bits; chosen via threshold-sweep + visual inspection.


def _dhash(p: Path) -> np.ndarray:
    with Image.open(p) as im:
        return np.array(imagehash.dhash(im, hash_size=16).hash, dtype=bool).flatten()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir")
    ap.add_argument("collection")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite vlm_description even if already set.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change without writing.")
    args = ap.parse_args(argv)

    coll_dir = Path(args.data_dir) / args.collection
    cache_dir = coll_dir / ".rag-cache"
    images_dir = coll_dir / ".rag-images"

    if not cache_dir.is_dir():
        print(f"No .rag-cache at {cache_dir}", file=sys.stderr)
        return 2

    # PASS 1: Hash every picture block's PNG so we can cluster them.
    #
    # We use union-find over (block_index, hash) pairs at the same
    # HAMMING_THRESHOLD that the offline analysis used, so a "cluster"
    # here means exactly the same transitive group the analysis found —
    # not a point-to-point match to a single representative. That's
    # important because the representative may be far from many cluster
    # members under threshold-12 even though they're connected via
    # chains of near-neighbours.
    print(f"Pass 1: hash every picture block + its location...", flush=True)
    block_info: list[tuple[Path, dict]] = []   # (sidecar_path, block_dict)
    hashes: list[np.ndarray] = []
    skipped_existing = skipped_nohash = 0

    for sidecar in sorted(cache_dir.glob("*.json")):
        with sidecar.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
        for b in doc.get("blocks", []):
            if b.get("type") != "picture":
                continue
            if not args.force and b.get("vlm_description"):
                skipped_existing += 1
                continue
            img_rel = b.get("image_path") or ""
            img_path = coll_dir / img_rel
            if not img_path.exists():
                skipped_nohash += 1
                continue
            try:
                hashes.append(_dhash(img_path))
            except Exception:
                skipped_nohash += 1
                continue
            block_info.append((sidecar, b))
        # We need the loaded doc later for write-back; cache it on the sidecar path.
        # Cheaper: just defer rewriting until pass 3 by re-reading.
    n = len(hashes)
    print(f"  hashed {n} picture blocks ({skipped_existing} already captioned, "
          f"{skipped_nohash} unhashable).", flush=True)
    if n == 0:
        print("Nothing to do.")
        return 0
    H = np.array(hashes)

    # PASS 2: union-find at HAMMING_THRESHOLD to recover clusters.
    print(f"Pass 2: cluster at dHash threshold {HAMMING_THRESHOLD}...", flush=True)
    parent = list(range(n))
    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    for i in range(n):
        dists = np.sum(H[i] ^ H, axis=1)
        nbrs = np.where(dists <= HAMMING_THRESHOLD)[0]
        for j in nbrs:
            if j != i:
                union(i, j)
        if i and i % 1000 == 0:
            print(f"  {i}/{n}", flush=True)

    # PASS 3: resolve LABEL_MAP representatives to cluster roots.
    print("Pass 3: resolve labelled cluster roots...", flush=True)
    # Build an image_path -> index map from block_info so we can find the
    # representative in the same picture-block universe.
    path_to_idx: dict[str, int] = {}
    for idx, (_sc, b) in enumerate(block_info):
        path_to_idx[b.get("image_path") or ""] = idx
    root_to_caption: dict[int, str] = {}
    cluster_label: dict[str, int] = {}  # cid -> root index
    for cid, (rep_rel, caption) in LABEL_MAP.items():
        # Find the representative's block index. The path in LABEL_MAP
        # is relative to images_dir (e.g. "BT-Core_v6.0/p1-pic2.png"),
        # but image_path on a block is collection-relative
        # (".rag-images/BT-Core_v6.0/p1-pic2.png"). Try both shapes.
        key_options = [
            f".rag-images/{rep_rel}",
            rep_rel,
        ]
        idx = None
        for k in key_options:
            if k in path_to_idx:
                idx = path_to_idx[k]
                break
        if idx is None:
            print(f"  WARN: rep {rep_rel} not found in block universe, "
                  f"skipping cluster {cid}")
            continue
        root = find(idx)
        if root in root_to_caption and root_to_caption[root] != caption:
            # Two label-map entries collapsed into the same cluster.
            # That's fine if their captions agree; otherwise surface it.
            print(f"  NOTE: cluster {cid} merged into the same root as a "
                  f"prior label; using first caption")
            continue
        root_to_caption[root] = caption
        cluster_label[cid] = root

    print(f"  resolved {len(root_to_caption)} labelled cluster roots.")

    # PASS 4: walk block_info and apply captions. Group writes by sidecar.
    print("Pass 4: apply captions...", flush=True)
    counts: dict[str, int] = defaultdict(int)
    by_sidecar: dict[Path, list[tuple[dict, str]]] = defaultdict(list)
    for idx, (sidecar, b) in enumerate(block_info):
        root = find(idx)
        caption = root_to_caption.get(root)
        if caption is None:
            continue
        # Reverse-lookup cluster id for reporting.
        for cid, croot in cluster_label.items():
            if croot == root:
                counts[cid] += 1
                break
        by_sidecar[sidecar].append((b, caption))

    if args.dry_run:
        print()
        print("Per-cluster apply counts (dry-run):")
        for cid, (_, cap) in LABEL_MAP.items():
            print(f"  {cid:25} {counts.get(cid, 0):>5}  ({cap[:60]})")
        print()
        print(f"Total blocks labelled:     {sum(counts.values())}")
        print(f"Skipped (already had):     {skipped_existing}")
        print(f"Skipped (no hashable PNG): {skipped_nohash}")
        print(f"Sidecars that would change: {len(by_sidecar)}")
        print("(dry-run — no files written)")
        return 0

    # Apply + write each modified sidecar exactly once.
    touched_files = 0
    for sidecar, edits in by_sidecar.items():
        # Re-read to ensure we don't lose pass-1 state (block_info holds
        # dicts from a prior load that may not be the canonical write
        # target). Match by image_path which is unique within a sidecar.
        with sidecar.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
        path_to_block = {(b.get("image_path") or "") : b
                         for b in doc.get("blocks", [])
                         if b.get("type") == "picture"}
        for old_b, caption in edits:
            target = path_to_block.get(old_b.get("image_path") or "")
            if target is None:
                continue
            target["vlm_description"] = caption
            existing_text = (target.get("text") or "").strip()
            sep = "\n\n" if existing_text else ""
            target["text"] = f"{existing_text}{sep}{caption}"
        with sidecar.open("w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)
        touched_files += 1

    print()
    print("Per-cluster apply counts:")
    for cid, (_, cap) in LABEL_MAP.items():
        print(f"  {cid:25} {counts.get(cid, 0):>5}  ({cap[:60]})")
    print()
    print(f"Total blocks labelled:     {sum(counts.values())}")
    print(f"Skipped (already had):     {skipped_existing}")
    print(f"Skipped (no hashable PNG): {skipped_nohash}")
    print(f"Sidecars modified:         {touched_files}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
