#!/usr/bin/env python3
"""Pluggable text embedders for the local LLM Wiki semantic retriever.

Two backends, selected automatically:

- ``HashingEmbedder`` (default): a pure-numpy, dependency-free lexical vector
  encoder. It hashes word tokens and character n-grams (works for Korean and
  English) into a fixed-width signed vector, TF-weighted and L2-normalized. It
  needs no model download, is deterministic across processes (uses BLAKE2b, not
  Python's salted ``hash``), and runs offline today.

- ``SentenceTransformerEmbedder`` (optional): used only when
  ``sentence_transformers`` is importable. Produces real neural multilingual
  embeddings with the same interface and output contract (L2-normalized float32
  rows), so the rest of the pipeline is backend-agnostic.

All embedders return ``np.ndarray`` of shape ``(n, dim)``, dtype float32, with
unit-norm rows (zero vectors stay zero), so cosine similarity is a plain dot
product.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import sys
from pathlib import Path

import numpy as np

TOOL_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import query_wiki  # type: ignore[import-not-found]  # noqa: E402


_TOKEN_RE = re.compile(r"[0-9a-zA-Z]+|[\uac00-\ud7a3]+")
_MAX_CHARS = 6000  # cap per-document feature extraction for speed


def _features(text: str) -> list[str]:
    """Word tokens plus character trigrams from normalized text."""
    norm = query_wiki.normalize(text)[:_MAX_CHARS]
    feats: list[str] = []
    for token in _TOKEN_RE.findall(norm):
        if len(token) >= 2:
            feats.append("w:" + token)
        if len(token) >= 3:
            for i in range(len(token) - 2):
                feats.append("c:" + token[i : i + 3])
        elif token:
            feats.append("c:" + token)
    return feats


def _hash(feature: str) -> int:
    return int.from_bytes(hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(), "big")


class HashingEmbedder:
    name = "hashing-v1"

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            counts: dict[int, float] = {}
            signs: dict[int, int] = {}
            tally: dict[str, int] = {}
            for feat in _features(text or ""):
                tally[feat] = tally.get(feat, 0) + 1
            for feat, count in tally.items():
                h = _hash(feat)
                idx = h % self.dim
                sign = 1 if (h >> 63) & 1 else -1
                counts[idx] = counts.get(idx, 0.0) + sign * (1.0 + math.log(count))
                signs[idx] = sign
            if counts:
                vec = out[row]
                for idx, value in counts.items():
                    vec[idx] = value
                norm = float(np.linalg.norm(vec))
                if norm > 0:
                    vec /= norm
        return out


class SentenceTransformerEmbedder:
    def __init__(self, model: str = "paraphrase-multilingual-MiniLM-L12-v2") -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore

        self.model_name = model
        self.name = f"st:{model}"
        self._model = SentenceTransformer(model)
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def embed(self, texts: list[str]) -> np.ndarray:
        vecs = self._model.encode(
            [t or "" for t in texts],
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vecs.astype(np.float32)


def available_st() -> bool:
    import importlib.util

    return importlib.util.find_spec("sentence_transformers") is not None


def get_embedder(prefer: str = "auto", *, dim: int = 512, st_model: str | None = None):
    """Return an embedder.

    prefer: "auto" (neural if available, else hashing), "hashing", or "st".
    """
    if prefer in {"auto", "st"} and available_st():
        try:
            return SentenceTransformerEmbedder(st_model or "paraphrase-multilingual-MiniLM-L12-v2")
        except Exception as exc:  # model download/load failure -> graceful fallback
            if prefer == "st":
                raise
            print(f"[wiki_embed] sentence-transformers unavailable ({exc}); using hashing", file=sys.stderr)
    if prefer == "st":
        raise SystemExit("sentence-transformers backend requested but not importable")
    return HashingEmbedder(dim=dim)


def main() -> int:
    parser = argparse.ArgumentParser(description="Embedder self-test / similarity probe")
    parser.add_argument("texts", nargs="+", help="First text is the query; rest are candidates")
    parser.add_argument("--backend", default="auto", choices=["auto", "hashing", "st"])
    parser.add_argument("--dim", type=int, default=512)
    args = parser.parse_args()

    embedder = get_embedder(args.backend, dim=args.dim)
    vecs = embedder.embed(args.texts)
    query, cands = vecs[0], vecs[1:]
    sims = cands @ query
    print(f"backend: {embedder.name} dim={embedder.dim}")
    order = np.argsort(-sims)
    for rank, i in enumerate(order, 1):
        print(f"{rank}. cos={sims[i]:.4f}  {args.texts[i + 1][:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
