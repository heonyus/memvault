#!/usr/bin/env python3
"""Render the local LLM Wiki as a self-contained interactive graph (OKF viewer).

This adapts the Open Knowledge Format reference viewer
(github.com/GoogleCloudPlatform/knowledge-catalog, Apache-2.0) to this wiki's
conventions. It walks ``wiki/`` (optionally ``audit/``), treats each markdown
page as a concept node, and draws an edge for every cross-link — both
OKF/markdown links (``[label](/path.md)``) and Obsidian wikilinks
(``[[path|label]]``). The output is one self-contained ``viz.html``
(Cytoscape.js + marked from CDN); no backend, no install on the viewing side.

Frontmatter parsing uses PyYAML when available and falls back to a small
stdlib parser so the tool keeps working in a zero-dependency environment.
The vendored viewer assets live in ``tools/_okf_viewer/`` (see its NOTICE.md).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

DEFAULT_WIKI = Path("~/llm-wiki")
ASSET_DIR = Path(__file__).resolve().parent / "_okf_viewer"

# Markdown links to a .md target: [label](/a/b.md) or [label](b.md#frag)
_MD_LINK_RE = re.compile(r"\]\(([^)\s]+\.md)(?:#[^)]*)?\)")
# Obsidian wikilinks: [[target]], [[target|label]], [[target#heading|label]]
_WIKILINK_RE = re.compile(r"!?\[\[([^\]]+)\]\]")

_TYPE_PALETTE = {
    "index": "#f59e0b",
    "system": "#8b5cf6",
    "project": "#3b82f6",
    "experiment": "#10b981",
    "paper": "#ec4899",
    "presentation": "#ef4444",
    "concept": "#14b8a6",
    "people": "#a3a3a3",
}
_DEFAULT_NODE_COLOR = "#94a3b8"


# --------------------------------------------------------------------------- #
# Frontmatter parsing (pyyaml if available, stdlib fallback otherwise)
# --------------------------------------------------------------------------- #

try:  # pragma: no cover - environment dependent
    import yaml  # type: ignore

    def _parse_yaml(text: str) -> dict:
        data = yaml.safe_load(text) or {}
        return data if isinstance(data, dict) else {}
except Exception:  # pragma: no cover
    def _parse_yaml(text: str) -> dict:
        return _stdlib_frontmatter(text)


def _stdlib_frontmatter(text: str) -> dict:
    out: dict[str, Any] = {}
    key: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if key and re.match(r"\s+-\s+", line):
            out.setdefault(key, [])
            if isinstance(out[key], list):
                out[key].append(line.strip()[2:].strip().strip('"').strip("'"))
            continue
        m = re.match(r"([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value == "":
            out[key] = []  # may be filled by following "- " items
        elif value.startswith("[") and value.endswith("]"):
            out[key] = [v.strip().strip('"').strip("'") for v in value[1:-1].split(",") if v.strip()]
        else:
            out[key] = value.strip().strip('"').strip("'")
            key = None
    return out


def split_frontmatter(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    fm = _parse_yaml("\n".join(lines[1:end]))
    body = "\n".join(lines[end + 1:])
    return fm, body.lstrip("\n")


# --------------------------------------------------------------------------- #
# Concept walk + link resolution
# --------------------------------------------------------------------------- #


def concept_index(roots: list[Path], wiki: Path) -> dict[str, str]:
    """Map full concept ids and unique basenames to full concept ids."""
    full: set[str] = set()
    basename_to_ids: dict[str, list[str]] = {}
    for base in roots:
        if not base.exists():
            continue
        for page in base.rglob("*.md"):
            cid = page.relative_to(wiki).with_suffix("").as_posix()
            full.add(cid)
            basename_to_ids.setdefault(page.stem, []).append(cid)
    resolver = {cid: cid for cid in full}
    for name, ids in basename_to_ids.items():
        if name not in resolver and len(ids) == 1:
            resolver[name] = ids[0]
    return resolver


def resolve_wikilink(target: str, resolver: dict[str, str]) -> str | None:
    target = target.split("|", 1)[0].split("#", 1)[0].strip()
    if not target:
        return None
    if target.endswith(".md"):
        target = target[:-3]
    target = target.lstrip("/")
    return resolver.get(target) or resolver.get(target.split("/")[-1])


def convert_wikilinks(body: str, resolver: dict[str, str]) -> str:
    """Rewrite [[target|label]] into [label](/concept.md) so marked renders it."""

    def repl(m: re.Match) -> str:
        inner = m.group(1)
        target_part, _, label = inner.partition("|")
        label = label.strip() or target_part.split("#", 1)[0].strip()
        cid = resolve_wikilink(target_part, resolver)
        if not cid:
            return label  # broken/unknown link -> plain text
        return f"[{label}](/{cid}.md)"

    return _WIKILINK_RE.sub(repl, body)


def extract_edges(body: str, resolver: dict[str, str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(cid: str | None) -> None:
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)

    for m in _WIKILINK_RE.finditer(body):
        add(resolve_wikilink(m.group(1), resolver))
    for m in _MD_LINK_RE.finditer(body):
        raw = m.group(1)
        if "://" in raw:
            continue
        add(resolve_wikilink(raw, resolver))
    return out


def color_for(ctype: str) -> str:
    return _TYPE_PALETTE.get(ctype, _DEFAULT_NODE_COLOR)


def build_graph(wiki: Path, include_audit: bool) -> dict[str, Any]:
    roots = [wiki / "wiki"] + ([wiki / "audit"] if include_audit else [])
    resolver = concept_index(roots, wiki)

    nodes: list[dict] = []
    bodies: dict[str, str] = {}
    edges: list[dict] = []
    seen_edges: set[tuple[str, str]] = set()
    ids: set[str] = set()
    types: set[str] = set()
    raw_concepts: list[tuple[str, str, dict]] = []

    for base in roots:
        if not base.exists():
            continue
        for page in sorted(base.rglob("*.md")):
            cid = page.relative_to(wiki).with_suffix("").as_posix()
            try:
                fm, body = split_frontmatter(page.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            ids.add(cid)
            raw_concepts.append((cid, body, fm))

    for cid, body, fm in raw_concepts:
        ctype = str(fm.get("type") or "concept")
        types.add(ctype)
        tags = fm.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        resource = fm.get("resource")
        if not resource:
            src = fm.get("sources")
            resource = (src[0] if isinstance(src, list) and src else src) or ""
        rendered_body = convert_wikilinks(body, resolver)
        nodes.append(
            {
                "data": {
                    "id": cid,
                    "label": str(fm.get("title") or cid.split("/")[-1]),
                    "type": ctype,
                    "description": str(fm.get("description") or ""),
                    "resource": str(resource or ""),
                    "tags": [str(t) for t in tags],
                    "color": color_for(ctype),
                    "size": 30 + min(60, len(body) // 200),
                }
            }
        )
        bodies[cid] = rendered_body
        for target in extract_edges(body, resolver):
            if target == cid or target not in ids:
                continue
            key = (cid, target)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append({"data": {"id": f"{cid}__{target}", "source": cid, "target": target}})

    palette = {t: color_for(t) for t in sorted(types)}
    return {"nodes": nodes, "edges": edges, "bodies": bodies, "types": sorted(types), "palette": palette}


def render_html(graph: dict[str, Any], name: str) -> str:
    template = (ASSET_DIR / "viz.html").read_text(encoding="utf-8")
    css = (ASSET_DIR / "viz.css").read_text(encoding="utf-8")
    js = (ASSET_DIR / "viz.js").read_text(encoding="utf-8")
    return (
        template.replace("/*__VIZ_CSS__*/", css)
        .replace("/*__VIZ_JS__*/", js)
        .replace("__BUNDLE_NAME__", json.dumps(name))
        .replace("__BUNDLE_DATA__", json.dumps(graph, ensure_ascii=False))
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki", default=str(DEFAULT_WIKI), help="LLM Wiki root")
    parser.add_argument("--out", default=None, help="Output HTML path (default: <wiki>/viz.html)")
    parser.add_argument("--name", default="Local LLM Wiki", help="Display name in the viewer header")
    parser.add_argument("--include-audit", action="store_true", help="Also include audit/ pages")
    parser.add_argument("--json", action="store_true", help="Emit graph stats as JSON")
    args = parser.parse_args()

    wiki = Path(args.wiki).expanduser().resolve()
    out = Path(args.out).expanduser().resolve() if args.out else wiki / "viz.html"
    if not ASSET_DIR.exists():
        print(f"viewer assets missing: {ASSET_DIR}", file=sys.stderr)
        return 2

    graph = build_graph(wiki, args.include_audit)
    html = render_html(graph, args.name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    stats = {
        "concepts": len(graph["nodes"]),
        "edges": len(graph["edges"]),
        "types": len(graph["types"]),
        "bytes": len(html.encode("utf-8")),
        "out": str(out),
    }
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        print(f"wrote {out}")
        print(f"concepts={stats['concepts']} edges={stats['edges']} types={stats['types']} bytes={stats['bytes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
