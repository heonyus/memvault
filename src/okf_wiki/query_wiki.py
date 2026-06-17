#!/usr/bin/env python3
"""Search the local LLM Wiki and safe metadata manifests."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


SENSITIVE_TERMS = {
    "주민",
    "주민등록",
    "등본",
    "세금",
    "소득",
    "수입",
    "지출",
    "여권",
    "계약",
    "영수증",
    "은행",
    "신분증",
    "개인정보",
}

ENGLISH_SENSITIVE_TERMS = {
    "access_token",
    "auth_token",
    "bank_account",
    "bank_statement",
    "banking",
    "bearer",
    "credential",
    "credentials",
    "authorized_keys",
    "income",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "invoice",
    "known_hosts",
    "passport",
    "password",
    "passwd",
    "private_key",
    "receipt",
    "secret",
    "secret_key",
    "secrets",
    "sensitive-ssh-material",
    "ssh-material",
    "tax",
}


@dataclass(frozen=True)
class Match:
    score: int
    source: str
    location: str
    text: str


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def query_terms(query: str) -> list[str]:
    terms = [t for t in re.split(r"\s+", normalize(query).strip()) if t]
    return terms or [normalize(query)]


def is_sensitive_text(text: str) -> bool:
    lowered = normalize(text)
    if any(term in lowered for term in SENSITIVE_TERMS):
        return True
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", lowered)
        for term in ENGLISH_SENSITIVE_TERMS
    )


def score_text(text: str, terms: list[str]) -> int:
    lowered = normalize(text)
    score = 0
    for term in terms:
        if not term:
            continue
        score += lowered.count(term) * 10
    if all(term in lowered for term in terms):
        score += 25
    return score


def clean_snippet(text: str, width: int = 220) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + "..."


def iter_markdown(root: Path) -> Iterable[Path]:
    for subdir in ("wiki", "audit"):
        base = root / subdir
        if base.exists():
            yield from sorted(base.rglob("*.md"))


def markdown_matches(root: Path, terms: list[str], include_sensitive: bool) -> list[Match]:
    matches: list[Match] = []
    for page in iter_markdown(root):
        try:
            lines = page.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        rel = page.relative_to(root).as_posix()
        path_score = score_text(rel, terms)
        line_hit = False
        for index, line in enumerate(lines, start=1):
            line_score = score_text(line, terms)
            if line_score <= 0:
                continue
            text = line if line.strip() else rel
            if not include_sensitive and is_sensitive_text(text):
                continue
            line_hit = True
            matches.append(
                Match(
                    score=line_score + path_score,
                    source="wiki",
                    location=f"{rel}:{index}",
                    text=clean_snippet(text),
                )
            )
        if path_score > 0 and not line_hit:
            heading = next((line.strip() for line in lines if line.startswith("#")), rel)
            if not include_sensitive and is_sensitive_text(rel):
                continue
            matches.append(
                Match(
                    score=path_score,
                    source="wiki",
                    location=rel,
                    text=clean_snippet(heading),
                )
            )
    return matches


def manifest_row_text(row: dict) -> str:
    pieces: list[str] = []
    for key in (
        "path",
        "title",
        "id",
        "version",
        "question",
        "method",
        "parent",
        "item_type",
        "status",
        "risk",
        "reason",
        "doc_kind",
        "kind",
        "ext",
    ):
        value = row.get(key)
        if isinstance(value, str):
            pieces.append(value)
    markers = row.get("markers")
    if isinstance(markers, list):
        pieces.extend(str(item) for item in markers)
    return " | ".join(pieces)


def manifest_matches(root: Path, terms: list[str], include_sensitive: bool) -> list[Match]:
    matches: list[Match] = []
    manifests = sorted((root / "raw" / "manifests").glob("*.jsonl"))
    for manifest in manifests:
        rel = manifest.relative_to(root).as_posix()
        try:
            handle = manifest.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = manifest_row_text(row)
                if not text:
                    continue
                if not include_sensitive and (
                    row.get("sensitive_name") is True or is_sensitive_text(text)
                ):
                    continue
                score = score_text(text, terms) + score_text(rel, terms)
                if score <= 0:
                    continue
                matches.append(
                    Match(
                        score=score,
                        source="manifest",
                        location=f"{rel}:{line_number}",
                        text=clean_snippet(text),
                    )
                )
    return matches


def print_matches(title: str, matches: list[Match], limit: int) -> None:
    print(f"## {title}")
    if not matches:
        print("No matches.")
        return
    for match in sorted(matches, key=lambda m: (-m.score, m.location))[:limit]:
        print(f"- {match.location} [{match.source}, score={match.score}]")
        print(f"  {match.text}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="+", help="Query terms")
    parser.add_argument("--wiki", default="~/llm-wiki", help="LLM Wiki root")
    parser.add_argument("--limit", type=int, default=15, help="Matches per section")
    parser.add_argument(
        "--include-sensitive",
        action="store_true",
        help="Allow sensitive-looking manifest paths in output",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = parser.parse_args()

    root = Path(args.wiki).expanduser().resolve()
    query = " ".join(args.query)
    terms = query_terms(query)

    wiki = markdown_matches(root, terms, args.include_sensitive)
    manifests = manifest_matches(root, terms, args.include_sensitive)

    if args.json:
        payload = {
            "query": query,
            "terms": terms,
            "wiki": [match.__dict__ for match in sorted(wiki, key=lambda m: (-m.score, m.location))[: args.limit]],
            "manifests": [
                match.__dict__
                for match in sorted(manifests, key=lambda m: (-m.score, m.location))[: args.limit]
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"# Local LLM Wiki Query: {query}")
    print_matches("Wiki Matches", wiki, args.limit)
    print_matches("Manifest Matches", manifests, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
