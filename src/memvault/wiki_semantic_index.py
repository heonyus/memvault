#!/usr/bin/env python3
"""Build a semantic embedding index over the wiki and the agent session index.

Sources embedded:
- Markdown chunks from ``wiki/`` and ``audit/`` (windowed by ~900 chars).
- Non-sensitive rows from ``raw/manifests/agent-session-index.jsonl``
  (title + first-user snippet + topic keywords).

Sensitive-looking text is filtered out before embedding so secrets/PII never
enter the vector store. Vectors are written to ``raw/embeddings/`` (git-ignored,
since they are derived binary artifacts). The build is incremental: a chunk whose
content hash is unchanged reuses its previous vector, so only new/edited chunks
are re-embedded — important when the optional neural backend is active.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

TOOL_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import query_wiki  # type: ignore[import-not-found]  # noqa: E402
import wiki_embed  # type: ignore[import-not-found]  # noqa: E402

DEFAULT_WIKI = Path("~/llm-wiki")
EMB_DIR_REL = "raw/embeddings"
VECTORS_NAME = "wiki-index.npz"
META_NAME = "wiki-index.jsonl"
HEADER_NAME = "wiki-index.meta.json"
SESSION_MANIFEST_REL = "raw/manifests/agent-session-index.jsonl"


def content_hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=12).hexdigest()


def chunk_markdown(text: str, max_chars: int = 900) -> list[tuple[int, str]]:
    chunks: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 1
    size = 0
    for i, line in enumerate(text.splitlines(), start=1):
        if not buf:
            start = i
        buf.append(line)
        size += len(line) + 1
        if size >= max_chars:
            joined = "\n".join(buf).strip()
            if joined:
                chunks.append((start, joined))
            buf, size = [], 0
    if buf:
        joined = "\n".join(buf).strip()
        if joined:
            chunks.append((start, joined))
    return chunks


def collect_documents(wiki: Path, include_sessions: bool) -> list[dict]:
    docs: list[dict] = []
    for subdir in ("wiki", "audit"):
        base = wiki / subdir
        if not base.exists():
            continue
        for page in sorted(base.rglob("*.md")):
            try:
                text = page.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            rel = page.relative_to(wiki).as_posix()
            for start, chunk in chunk_markdown(text):
                if query_wiki.is_sensitive_text(chunk):
                    continue
                docs.append(
                    {
                        "location": f"{rel}:{start}",
                        "kind": "wiki",
                        "text": chunk,
                        "snippet": " ".join(chunk.split())[:200],
                    }
                )

    if include_sessions:
        manifest = wiki / SESSION_MANIFEST_REL
        if manifest.exists():
            with manifest.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("sensitive_name"):
                        continue
                    title = str(row.get("title") or "")
                    question = str(row.get("question") or "")
                    markers = " ".join(row.get("markers") or [])
                    text = f"{title}\n{question}\n{markers}".strip()
                    if not text or query_wiki.is_sensitive_text(text):
                        continue
                    docs.append(
                        {
                            "location": str(row.get("path") or row.get("id") or ""),
                            "kind": "session",
                            "text": text,
                            "snippet": " ".join((title + " — " + question).split())[:200],
                        }
                    )
    return docs


def load_previous(emb_dir: Path, backend_name: str) -> dict[str, np.ndarray]:
    header = emb_dir / HEADER_NAME
    vectors = emb_dir / VECTORS_NAME
    meta = emb_dir / META_NAME
    if not (header.exists() and vectors.exists() and meta.exists()):
        return {}
    try:
        head = json.loads(header.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if head.get("backend") != backend_name:
        return {}  # backend changed -> full rebuild
    try:
        mat = np.load(vectors)["vectors"]
    except (OSError, KeyError, ValueError):
        return {}
    cache: dict[str, np.ndarray] = {}
    with meta.open(encoding="utf-8") as handle:
        for i, line in enumerate(handle):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            h = row.get("hash")
            if h is not None and i < len(mat):
                cache[h] = mat[i]
    return cache


def build(wiki: Path, backend: str, dim: int, include_sessions: bool, rebuild: bool) -> dict:
    embedder = wiki_embed.get_embedder(backend, dim=dim)
    emb_dir = wiki / EMB_DIR_REL
    emb_dir.mkdir(parents=True, exist_ok=True)

    docs = collect_documents(wiki, include_sessions)
    for doc in docs:
        doc["hash"] = content_hash(doc["text"])

    cache = {} if rebuild else load_previous(emb_dir, embedder.name)
    to_embed = [d for d in docs if d["hash"] not in cache]
    new_vecs = embedder.embed([d["text"] for d in to_embed]) if to_embed else np.zeros((0, embedder.dim), np.float32)

    new_map = {d["hash"]: new_vecs[i] for i, d in enumerate(to_embed)}
    matrix = np.zeros((len(docs), embedder.dim), dtype=np.float32)
    for i, doc in enumerate(docs):
        vec = cache.get(doc["hash"])
        if vec is None:
            vec = new_map[doc["hash"]]
        matrix[i] = vec

    # Atomic writes
    tmp_vec = emb_dir / (VECTORS_NAME + ".tmp")
    np.savez_compressed(tmp_vec, vectors=matrix)
    # np.savez appends .npz to the name; normalize back to the .tmp target
    produced = tmp_vec.with_suffix(tmp_vec.suffix + ".npz")
    produced.replace(emb_dir / VECTORS_NAME)

    tmp_meta = emb_dir / (META_NAME + ".tmp")
    with tmp_meta.open("w", encoding="utf-8") as handle:
        for doc in docs:
            handle.write(
                json.dumps(
                    {
                        "hash": doc["hash"],
                        "location": doc["location"],
                        "kind": doc["kind"],
                        "snippet": doc["snippet"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    tmp_meta.replace(emb_dir / META_NAME)

    header = {
        "backend": embedder.name,
        "dim": embedder.dim,
        "count": len(docs),
        "reused": len(docs) - len(to_embed),
        "embedded": len(to_embed),
        "sessions_included": include_sessions,
    }
    (emb_dir / HEADER_NAME).write_text(json.dumps(header, ensure_ascii=False, indent=2), encoding="utf-8")
    return header


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki", default=str(DEFAULT_WIKI), help="LLM Wiki root")
    parser.add_argument("--backend", default="auto", choices=["auto", "hashing", "st"])
    parser.add_argument("--dim", type=int, default=512, help="Hashing embedder dimension")
    parser.add_argument("--no-sessions", action="store_true", help="Skip agent session rows")
    parser.add_argument("--rebuild", action="store_true", help="Ignore the incremental vector cache")
    parser.add_argument("--json", action="store_true", help="Emit JSON header")
    args = parser.parse_args()

    wiki = Path(args.wiki).expanduser().resolve()
    header = build(wiki, args.backend, args.dim, not args.no_sessions, args.rebuild)
    if args.json:
        print(json.dumps(header, ensure_ascii=False, indent=2))
    else:
        print(
            f"semantic index: backend={header['backend']} dim={header['dim']} "
            f"chunks={header['count']} embedded={header['embedded']} reused={header['reused']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
