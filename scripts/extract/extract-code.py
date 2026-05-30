#!/usr/bin/env python3
"""
Source-tree extractor for code collections.

Walks a source repository and emits one JSON sidecar per source file at
data/<collection>/.rag-cache/<encoded-path>.json. Sidecar shape matches
extract.py / extract-nemo.py:

    {
      "doc": "<repo-relative path>",
      "source_mtime": "<iso>",
      "backend": "code-tree-sitter",
      "blocks": [
        {
          "text": "<the code, original whitespace preserved>",
          "type": "code",
          "section": "<file>::<function-or-chunk-id>",
          "section_path": ["<top>", "<sub>", ..., "<filename>"],
          "file_path": "<repo-relative>",
          "line_start": <int>,
          "line_end": <int>,
          "language": "<language tag if known>",
          "github_url": "<deep-link if link-mode + github>"
        },
        ...
      ]
    }

Two modes — chosen by folder contents:

* LINK MODE — data/<collection>/.git-source.yaml exists. We clone (or
  fetch + reset) into storage/code-cache/<collection>/ at the configured
  ref + sparse paths, then walk that.

* IN-PLACE MODE — no yaml; walk data/<collection>/ directly (skipping
  hidden dirs, .rag-cache/, .rag-images/, .git/).

Usage:
    python extract-code.py <data_dir> [collection] [--force]

mtime-skip is per file (compares the recorded source_mtime in the
existing sidecar). --force re-extracts everything.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml  # PyYAML


# ─── Constants ────────────────────────────────────────────────────────────

DEFAULT_INCLUDE = [
    "*.c", "*.h", "*.cpp", "*.hpp", "*.cc", "*.hh",
    "*.py", "*.go", "*.rs",
    "*.js", "*.jsx", "*.ts", "*.tsx",
    "*.java", "*.kt", "*.swift", "*.rb",
    "*.md", "*.txt", "*.rst",
    "*.yaml", "*.yml", "*.toml", "*.ini", "*.json", "*.cfg", "*.conf",
    "Makefile", "Dockerfile", "CMakeLists.txt", "*.cmake",
    "*.sh", "*.bash",
]

DEFAULT_EXCLUDE = [
    ".git/*", ".git/**/*",
    "node_modules/*", "node_modules/**/*",
    "vendor/*", "vendor/**/*",
    "__pycache__/*", "**/__pycache__/*", "**/__pycache__/**/*",
    "build/*", "build/**/*",
    "dist/*", "dist/**/*",
    "*.min.js", "*.lock", "*.map",
    "*.pb.go", "*.pb.cc", "*.pb.h",
    "testdata/*", "**/testdata/*", "**/testdata/**/*",
]

# Tunables — environment-overridable.
CHUNK_MAX_LINES = int(os.environ.get("EXTRACT_CODE_CHUNK_LINES", "50"))
CHUNK_OVERLAP_LINES = int(os.environ.get("EXTRACT_CODE_OVERLAP_LINES", "10"))
# Hard char cap. Keep under server.js CHUNK_SIZE (1000) so chunkText()'s
# whitespace-collapse never fires and destroys code indentation.
CHUNK_MAX_CHARS = int(os.environ.get("EXTRACT_CODE_CHUNK_CHARS", "800"))
# Skip individual files larger than this (likely auto-generated / minified).
MAX_FILE_BYTES = int(os.environ.get("EXTRACT_CODE_MAX_FILE_KB", "1024")) * 1024

LANG_BY_EXT = {
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp", ".hh": "cpp",
    ".py": "python",
    ".go": "go",
    ".rs": "rust",
    ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".java": "java",
    ".rb": "ruby",
}

# Per-language: node types that should each become their own chunk.
TS_CHUNKABLE = {
    "c": {
        "function_definition", "preproc_function_def",
        "struct_specifier", "union_specifier", "enum_specifier", "type_definition",
    },
    "cpp": {
        "function_definition", "class_specifier", "struct_specifier",
        "namespace_definition", "enum_specifier", "template_declaration",
    },
    "python": {
        "function_definition", "async_function_definition",
        "class_definition", "decorated_definition",
    },
    "go": {
        "function_declaration", "method_declaration", "type_declaration",
    },
    "rust": {
        "function_item", "struct_item", "enum_item", "impl_item",
        "trait_item", "mod_item",
    },
    "javascript": {
        "function_declaration", "function_expression", "arrow_function",
        "class_declaration", "method_definition",
    },
    "typescript": {
        "function_declaration", "function_expression", "arrow_function",
        "class_declaration", "method_definition", "interface_declaration",
        "type_alias_declaration",
    },
    "java": {
        "method_declaration", "class_declaration", "interface_declaration",
        "constructor_declaration", "enum_declaration",
    },
    "ruby": {"method", "class", "module", "singleton_method"},
}


# ─── Helpers ──────────────────────────────────────────────────────────────

def _iso_mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()


def _encode_path(rel: str) -> str:
    # filenames can't contain '/', replace with '__'. preserve extension.
    return rel.replace("/", "__").replace("\\", "__")


def _git(args: list[str], cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["git"] + args, cwd=str(cwd) if cwd else None,
        check=check, capture_output=True, text=True,
    )
    return proc.stdout.strip()


def _clone_or_pull(url: str, dest: Path, ref: str | None,
                   sparse_paths: list[str] | None) -> None:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        clone_args = ["clone", "--depth", "1", "--filter=blob:none"]
        if ref:
            clone_args += ["--branch", ref]
        if sparse_paths:
            clone_args += ["--sparse"]
        clone_args += [url, str(dest)]
        print(f"  git clone {url} (ref={ref or 'default HEAD'})", flush=True)
        _git(clone_args)
        if sparse_paths:
            _git(["sparse-checkout", "set", "--cone"] + sparse_paths, cwd=dest)
    else:
        print("  git fetch + reset (existing clone)", flush=True)
        fetch_ref = ref if ref else "HEAD"
        try:
            _git(["fetch", "--depth", "1", "origin", fetch_ref], cwd=dest)
            _git(["reset", "--hard", "FETCH_HEAD"], cwd=dest)
        except subprocess.CalledProcessError:
            # Some refs (commit SHAs) need a different incantation; fall
            # back to a fetch-all + reset.
            _git(["fetch", "--depth", "1", "origin"], cwd=dest)
            _git(["reset", "--hard", "origin/" + (ref or "HEAD")], cwd=dest)


def _resolved_sha(dest: Path) -> str:
    try:
        return _git(["rev-parse", "HEAD"], cwd=dest)
    except Exception:
        return ""


def _looks_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except Exception:
        return True


def _matches_any(rel: str, name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p)
               for p in patterns)


def _walk_source(root: Path, include: list[str], exclude: list[str]):
    """Yield (abs_path, rel_posix) for each source file under root.

    Skips symlinks (avoids cycles), oversized files, and binary files.
    """
    seen_real: set[str] = set()
    for p in root.rglob("*"):
        if p.is_symlink():
            continue
        if not p.is_file():
            continue
        try:
            real = str(p.resolve())
        except Exception:
            continue
        if real in seen_real:
            continue
        seen_real.add(real)
        rel = p.relative_to(root).as_posix()
        if _matches_any(rel, p.name, exclude):
            continue
        if not _matches_any(rel, p.name, include):
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
        except Exception:
            continue
        if _looks_binary(p):
            continue
        yield p, rel


# ─── Tree-sitter chunking (lazy import) ───────────────────────────────────

_TS_PARSER_CACHE: dict[str, object] = {}


def _get_ts_parser(language: str):
    if language in _TS_PARSER_CACHE:
        return _TS_PARSER_CACHE[language]
    try:
        from tree_sitter_language_pack import get_parser
        parser = get_parser(language)
    except Exception:
        parser = None
    _TS_PARSER_CACHE[language] = parser
    return parser


def _ts_node_name(node, source_bytes: bytes) -> str | None:
    """Best-effort: find an identifier inside the node to label the chunk."""
    for child in node.children:
        if child.type == "identifier":
            return source_bytes[child.start_byte:child.end_byte].decode(
                "utf-8", errors="replace"
            )
        # Look one level deeper for declarator wrappers (C/C++).
        for grand in child.children:
            if grand.type == "identifier":
                return source_bytes[grand.start_byte:grand.end_byte].decode(
                    "utf-8", errors="replace"
                )
    return None


def _ts_chunks(text: str, language: str):
    """Return [(line_start_1based, line_end_1based, name, chunk_text), ...] or None."""
    parser = _get_ts_parser(language)
    if parser is None:
        return None
    types = TS_CHUNKABLE.get(language, set())
    if not types:
        return None
    source_bytes = text.encode("utf-8")
    try:
        tree = parser.parse(source_bytes)
    except Exception:
        return None
    lines = text.splitlines(keepends=True)

    chunks: list[tuple[int, int, str, str]] = []
    for child in tree.root_node.children:
        if child.type not in types:
            continue
        s = child.start_point[0]  # 0-indexed
        e = child.end_point[0]
        chunk_text = "".join(lines[s:e + 1])
        name = _ts_node_name(child, source_bytes) or f"chunk-{s + 1}"
        # Big node? sub-split via line windows but keep the function name as prefix.
        if len(chunk_text) > CHUNK_MAX_CHARS * 2:
            for (sub_s, sub_e, sub_id, sub_text) in _line_window_chunks(chunk_text):
                chunks.append((s + sub_s, s + sub_e, f"{name}:{sub_id}", sub_text))
        else:
            chunks.append((s + 1, e + 1, name, chunk_text))
    return chunks or None


def _line_window_chunks(text: str):
    """Fallback chunker bounded by BOTH line count AND char count.

    Char cap is the important one — we never want a chunk to exceed
    CHUNK_MAX_CHARS, because rag-server's chunkText() will whitespace-
    collapse it and destroy code formatting.
    """
    lines = text.splitlines(keepends=True)
    n = len(lines)
    chunks: list[tuple[int, int, str, str]] = []
    i = 0
    idx = 0
    while i < n:
        end = i
        char_count = 0
        while (end < n
               and (end - i) < CHUNK_MAX_LINES
               and (char_count + len(lines[end])) <= CHUNK_MAX_CHARS):
            char_count += len(lines[end])
            end += 1
        # Pathological: a single line longer than CHUNK_MAX_CHARS.
        # Emit it as its own chunk anyway — better than infinite loop.
        if end == i:
            end = i + 1
        chunk_text = "".join(lines[i:end])
        idx += 1
        chunks.append((i + 1, end, f"chunk-{idx}", chunk_text))
        if end == n:
            break
        step = max(1, (end - i) - CHUNK_OVERLAP_LINES)
        i += step
    return chunks


# ─── Sidecar build + URL helpers ──────────────────────────────────────────

def _github_blob_base(url: str, sha_or_ref: str) -> str | None:
    """Convert https://github.com/foo/bar(.git) -> https://github.com/foo/bar/blob/<sha>"""
    m = re.match(r"^https?://github\.com/([^/]+)/([^/.]+)(?:\.git)?/?$", url)
    if not m:
        return None
    return f"https://github.com/{m.group(1)}/{m.group(2)}/blob/{sha_or_ref}"


def _build_blocks(rel: str, text: str, language: str | None,
                  github_base: str | None) -> list[dict]:
    chunks = None
    if language:
        chunks = _ts_chunks(text, language)
    if chunks is None:
        chunks = _line_window_chunks(text)

    section_path = rel.split("/")
    blocks = []
    for (l_start, l_end, name, ctext) in chunks:
        block = {
            "text": ctext,
            "type": "code",
            "section": f"{rel} :: {name}",
            "section_path": section_path,
            "file_path": rel,
            "line_start": l_start,
            "line_end": l_end,
        }
        if language:
            block["language"] = language
        if github_base:
            block["github_url"] = f"{github_base}/{rel}#L{l_start}-L{l_end}"
        blocks.append(block)
    return blocks


# ─── Per-collection driver ────────────────────────────────────────────────

def process_collection(data_dir: Path, collection: str, force: bool) -> None:
    folder = data_dir / collection
    if not folder.is_dir():
        print(f"[{collection}] folder not found: {folder}")
        return
    cfg_path = folder / ".git-source.yaml"

    include = list(DEFAULT_INCLUDE)
    exclude = list(DEFAULT_EXCLUDE)
    github_base: str | None = None

    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text("utf-8")) or {}
        url = cfg.get("url")
        if not url:
            print(f"[{collection}] .git-source.yaml missing 'url' field; skipping")
            return
        ref = cfg.get("ref")
        sparse = cfg.get("sparse_paths") or None
        include = cfg.get("include_globs") or DEFAULT_INCLUDE
        exclude = (cfg.get("exclude_globs") or []) + DEFAULT_EXCLUDE

        cache_root = (data_dir.parent / "storage" / "code-cache" / collection).resolve()
        print(f"[{collection}] LINK mode: {url} (ref={ref or 'default'})", flush=True)
        _clone_or_pull(url, cache_root, ref, sparse)

        # Build the canonical github blob URL with the actual resolved SHA
        # so citations stay stable across re-extractions.
        sha = _resolved_sha(cache_root) or ref or "HEAD"
        explicit = (cfg.get("github_blob_base_url") or "").strip()
        github_base = explicit or _github_blob_base(url, sha)
        source_root = cache_root
    else:
        # In-place mode — walk data/<collection>/ but skip our own cache dirs.
        print(f"[{collection}] IN-PLACE mode: {folder}", flush=True)
        exclude = exclude + [
            ".rag-cache/*", ".rag-cache/**/*",
            ".rag-images/*", ".rag-images/**/*",
            ".rag-md/*", ".rag-md/**/*",
        ]
        source_root = folder

    sidecar_dir = folder / ".rag-cache"
    sidecar_dir.mkdir(exist_ok=True)

    n_total = n_extracted = n_skipped = n_failed = 0
    for src, rel in _walk_source(source_root, include, exclude):
        n_total += 1
        out = sidecar_dir / f"{_encode_path(rel)}.json"
        try:
            mtime = _iso_mtime(src)
        except Exception:
            n_failed += 1
            continue

        if not force and out.exists():
            try:
                if json.loads(out.read_text("utf-8")).get("source_mtime") == mtime:
                    n_skipped += 1
                    continue
            except Exception:
                pass

        try:
            text = src.read_text("utf-8", errors="replace")
        except Exception as exc:
            print(f"[{collection}]   read failed: {rel}: {exc}", file=sys.stderr)
            n_failed += 1
            continue
        if not text.strip():
            continue

        ext = src.suffix.lower()
        language = LANG_BY_EXT.get(ext)
        blocks = _build_blocks(rel, text, language, github_base)
        if not blocks:
            continue

        out.write_text(json.dumps({
            "doc": rel,
            "source_mtime": mtime,
            "backend": "code-tree-sitter",
            "blocks": blocks,
        }, ensure_ascii=False), "utf-8")
        n_extracted += 1
        if n_extracted % 200 == 0:
            print(f"[{collection}]   {n_extracted} files extracted...", flush=True)

    print(f"[{collection}] DONE: {n_extracted} extracted, "
          f"{n_skipped} unchanged-skip, {n_failed} failed, "
          f"{n_total} files considered", flush=True)


# ─── Entry ────────────────────────────────────────────────────────────────

def _is_code_collection(folder: Path) -> bool:
    """Detect whether a folder is a code collection.

    Heuristic: presence of .git-source.yaml, or at least one source file
    matching the language extensions we know how to chunk.
    """
    if (folder / ".git-source.yaml").exists():
        return True
    for ext in (".c", ".py", ".go", ".js", ".ts", ".rs", ".java"):
        if any(folder.glob(f"**/*{ext}")):
            return True
    return False


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    force = "--force" in argv

    if not args:
        print(__doc__, file=sys.stderr)
        return 2
    data_dir = Path(args[0]).resolve()
    if not data_dir.is_dir():
        print(f"data dir not found: {data_dir}", file=sys.stderr)
        return 2

    if len(args) > 1:
        collections = [args[1]]
    else:
        collections = [
            entry.name for entry in sorted(data_dir.iterdir())
            if entry.is_dir() and not entry.name.startswith(".")
            and _is_code_collection(entry)
        ]
        if not collections:
            print("[extract-code] no code collections detected under "
                  f"{data_dir} (add a .git-source.yaml or drop source files in)")
            return 0

    for c in collections:
        process_collection(data_dir, c, force)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
