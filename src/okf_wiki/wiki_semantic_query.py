#!/usr/bin/env python3
"""Hybrid retrieval over the local LLM Wiki: semantic vectors + keyword scoring.

Combines two rankings for a query:
1. Dense cosine similarity against the embedding index built by
   ``wiki_semantic_index.py`` (works for the numpy hashing backend or the
   optional neural backend).
2. The existing lexical scorer in ``query_wiki.py`` over wiki markdown and the
   metadata manifests (which now includes the agent session index).

The two ranked lists are merged with Reciprocal Rank Fusion (RRF), which is
scale-free and robust to the different score magnitudes of the two methods.
Sensitive matches are filtered exactly as in ``query_wiki`` unless
``--include-sensitive`` is given.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

TOOL_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import query_wiki  # type: ignore[import-not-found]  # noqa: E402
import wiki_embed  # type: ignore[import-not-found]  # noqa: E402
import wiki_semantic_index as wsi  # type: ignore[import-not-found]  # noqa: E402

DEFAULT_WIKI = Path("~/llm-wiki")
RRF_K = 60  # standard RRF damping constant


def load_index(wiki: Path) -> tuple[np.ndarray, list[dict], dict]:
    emb_dir = wiki / wsi.EMB_DIR_REL
    header_path = emb_dir / wsi.HEADER_NAME
    vectors_path = emb_dir / wsi.VECTORS_NAME
    meta_path = emb_dir / wsi.META_NAME
    if not (header_path.exists() and vectors_path.exists() and meta_path.exists()):
        raise SystemExit(
            f"semantic index missing under {emb_dir}; run tools/wiki_semantic_index.py first"
        )
    header = json.loads(header_path.read_text(encoding="utf-8"))
    matrix = np.load(vectors_path)["vectors"]
    meta: list[dict] = []
    with meta_path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                meta.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if len(meta) != len(matrix):
        raise SystemExit("semantic index corrupt: meta/vector count mismatch; rebuild it")
    return matrix, meta, header


def semantic_ranking(
    wiki: Path, query: str, backend: str, limit: int, include_sensitive: bool
) -> list[dict]:
    matrix, meta, header = load_index(wiki)
    embedder = wiki_embed.get_embedder(backend, dim=int(header.get("dim", 512)))
    if embedder.name != header.get("backend"):
        # query backend must match the indexed backend's vector space
        embedder = wiki_embed.get_embedder("hashing", dim=int(header.get("dim", 512)))
        if embedder.name != header.get("backend"):
            raise SystemExit(
                f"index backend {header.get('backend')} != available {embedder.name}; rebuild index"
            )
    qvec = embedder.embed([query])[0]
    sims = matrix @ qvec
    order = np.argsort(-sims)
    results: list[dict] = []
    for i in order:
        score = float(sims[i])
        if score <= 0:
            break
        row = meta[i]
        if not include_sensitive and query_wiki.is_sensitive_text(row.get("snippet", "")):
            continue
        results.append(
            {
                "location": row.get("location"),
                "kind": row.get("kind"),
                "snippet": row.get("snippet"),
                "cosine": round(score, 4),
            }
        )
        if len(results) >= limit:
            break
    return results


def keyword_ranking(wiki: Path, query: str, limit: int, include_sensitive: bool) -> list[dict]:
    terms = query_wiki.query_terms(query)
    matches = query_wiki.markdown_matches(wiki, terms, include_sensitive) + query_wiki.manifest_matches(
        wiki, terms, include_sensitive
    )
    matches.sort(key=lambda m: (-m.score, m.location))
    out: list[dict] = []
    for m in matches[:limit]:
        out.append(
            {
                "location": m.location,
                "kind": m.source,
                "snippet": m.text,
                "keyword": m.score,
            }
        )
    return out


def _page_key(location: str) -> str:
    """Normalize a chunk/line location to its page so the two lists align."""
    loc = location or ""
    if ".md:" in loc:
        return loc.split(".md:", 1)[0] + ".md"
    if ".jsonl:" in loc:
        return loc.split(".jsonl:", 1)[0] + ".jsonl"
    return loc


def fuse(semantic: list[dict], keyword: list[dict], limit: int) -> list[dict]:
    fused: dict[str, dict] = {}

    def add(rank: int, item: dict, field: str) -> None:
        key = _page_key(item["location"])
        entry = fused.setdefault(
            key,
            {
                "location": item["location"],
                "kind": item.get("kind"),
                "snippet": item.get("snippet"),
                "rrf": 0.0,
                "cosine": None,
                "keyword": None,
            },
        )
        entry["rrf"] += 1.0 / (RRF_K + rank)
        if field == "cosine":
            entry["cosine"] = item["cosine"]
            if not entry.get("snippet"):
                entry["snippet"] = item.get("snippet")
        else:
            entry["keyword"] = item["keyword"]

    for rank, item in enumerate(semantic, 1):
        add(rank, item, "cosine")
    for rank, item in enumerate(keyword, 1):
        add(rank, item, "keyword")

    ranked = sorted(fused.values(), key=lambda e: (-e["rrf"], e["location"]))
    for e in ranked:
        e["rrf"] = round(e["rrf"], 5)
    return ranked[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="+", help="Query text")
    parser.add_argument("--wiki", default=str(DEFAULT_WIKI), help="LLM Wiki root")
    parser.add_argument("--backend", default="auto", choices=["auto", "hashing", "st"])
    parser.add_argument("--limit", type=int, default=10, help="Final fused results")
    parser.add_argument("--pool", type=int, default=40, help="Candidates per ranker before fusion")
    parser.add_argument("--mode", default="hybrid", choices=["hybrid", "semantic", "keyword"])
    parser.add_argument("--include-sensitive", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    wiki = Path(args.wiki).expanduser().resolve()
    query = " ".join(args.query)

    semantic = (
        semantic_ranking(wiki, query, args.backend, args.pool, args.include_sensitive)
        if args.mode in {"hybrid", "semantic"}
        else []
    )
    keyword = (
        keyword_ranking(wiki, query, args.pool, args.include_sensitive)
        if args.mode in {"hybrid", "keyword"}
        else []
    )

    if args.mode == "semantic":
        results = [{**r, "rrf": None, "keyword": None} for r in semantic][: args.limit]
    elif args.mode == "keyword":
        results = [{**r, "rrf": None, "cosine": None} for r in keyword][: args.limit]
    else:
        results = fuse(semantic, keyword, args.limit)

    if args.json:
        print(json.dumps({"query": query, "mode": args.mode, "results": results}, ensure_ascii=False, indent=2))
        return 0

    print(f"# Hybrid Wiki Retrieval: {query}  (mode={args.mode})")
    if not results:
        print("No matches.")
        return 0
    for rank, r in enumerate(results, 1):
        bits = []
        if r.get("rrf") is not None:
            bits.append(f"rrf={r['rrf']}")
        if r.get("cosine") is not None:
            bits.append(f"cos={r['cosine']}")
        if r.get("keyword") is not None:
            bits.append(f"kw={r['keyword']}")
        print(f"{rank}. {r['location']} [{r.get('kind')}] {' '.join(bits)}")
        if r.get("snippet"):
            print(f"   {r['snippet']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
