#!/usr/bin/env python3
"""Export this wiki as a conformant Open Knowledge Format (OKF) v0.1 bundle.

OKF (github.com/GoogleCloudPlatform/knowledge-catalog, Apache-2.0) represents
knowledge as a directory of markdown files with YAML frontmatter. This wiki is
already that shape; this exporter produces a portable OKF bundle from it:

- Concept pages: ``wiki/<path>.md`` -> ``<bundle>/<path>.md`` with frontmatter
  mapped to OKF fields (required ``type``; recommended ``title``,
  ``description``, ``resource``, ``tags``, ``timestamp``). Existing extra keys
  (``status``, ``sources``, ...) are preserved as allowed extensions.
- Wikilinks ``[[target|label]]`` are rewritten to OKF bundle-relative links
  ``[label](/path.md)`` so any OKF consumer can traverse the graph.
- ``index.md`` files are regenerated per directory for progressive disclosure
  (no frontmatter, per spec §6); the bundle-root ``index.md`` declares
  ``okf_version: "0.1"``.

The exporter then validates the bundle against the OKF v0.1 conformance rules
(§9): every non-reserved ``.md`` has parseable frontmatter with a non-empty
``type``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOOL_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import wiki_viz  # type: ignore[import-not-found]  # noqa: E402

DEFAULT_WIKI = Path("~/llm-wiki")
RESERVED = {"index.md", "log.md"}
OKF_PRIORITY_KEYS = ["type", "title", "description", "resource", "tags", "timestamp"]


def yaml_scalar(value: Any) -> str:
    s = str(value)
    if s == "" or any(c in s for c in ":#[]{}\",&*!|>%@`") or s.strip() != s:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def dump_frontmatter(fm: dict[str, Any]) -> str:
    lines = ["---"]
    keys = [k for k in OKF_PRIORITY_KEYS if k in fm] + [k for k in fm if k not in OKF_PRIORITY_KEYS]
    for key in keys:
        value = fm[key]
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                for item in value:
                    lines.append(f"  - {yaml_scalar(item)}")
        else:
            lines.append(f"{key}: {yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def to_okf_frontmatter(fm: dict[str, Any], stem: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["type"] = str(fm.get("type") or "concept")
    out["title"] = str(fm.get("title") or stem.replace("-", " ").title())
    if fm.get("description"):
        out["description"] = str(fm["description"])
    # resource: first source URI/path if present
    src = fm.get("sources")
    resource = fm.get("resource") or (src[0] if isinstance(src, list) and src else (src if isinstance(src, str) else None))
    if resource:
        out["resource"] = str(resource)
    tags = fm.get("tags")
    if isinstance(tags, list) and tags:
        out["tags"] = [str(t) for t in tags]
    # timestamp <- updated (ISO date acceptable)
    ts = fm.get("timestamp") or fm.get("updated")
    if ts:
        out["timestamp"] = str(ts)
    # preserve remaining producer keys as OKF extensions
    for key, value in fm.items():
        if key in {"type", "title", "description", "resource", "tags", "timestamp", "updated"}:
            continue
        out[key] = value
    return out


def directory_index(dir_path: Path, bundle_root: Path, concepts: dict[str, dict]) -> str:
    """Build a progressive-disclosure index.md body for one directory."""
    rel_dir = dir_path.relative_to(bundle_root)
    title = "Bundle Index" if rel_dir == Path(".") else f"{rel_dir.as_posix()} Index"
    subdirs: list[str] = []
    items: list[tuple[str, str, str]] = []
    for child in sorted(dir_path.iterdir()):
        if child.is_dir():
            subdirs.append(child.name)
        elif child.suffix == ".md" and child.name not in RESERVED:
            cid = child.relative_to(bundle_root).with_suffix("").as_posix()
            meta = concepts.get(cid, {})
            items.append((meta.get("title") or child.stem, child.name, meta.get("description") or ""))
    lines = [f"# {title}", ""]
    if items:
        lines.append("# Concepts")
        lines.append("")
        for disp, fname, desc in items:
            suffix = f" - {desc}" if desc else ""
            lines.append(f"* [{disp}]({fname}){suffix}")
        lines.append("")
    if subdirs:
        lines.append("# Subdirectories")
        lines.append("")
        for name in subdirs:
            lines.append(f"* [{name}/]({name}/)")
        lines.append("")
    return "\n".join(lines)


def first_sentence(body: str, width: int = 200) -> str:
    """Derive a one-line description from the first prose line of the body."""
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "|", "```", ">", "-", "*", "<")):
            continue
        line = re.sub(r"\[\[([^\]|#]+)(?:\|([^\]]+))?\]\]", lambda m: m.group(2) or m.group(1), line)
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) > width:
            line = line[: width - 1].rstrip() + "\u2026"
        return line
    return ""


def export(wiki: Path, out: Path) -> dict[str, Any]:
    src_root = wiki / "wiki"
    resolver = wiki_viz.concept_index([src_root], src_root)  # ids relative to wiki/wiki

    concepts: dict[str, dict] = {}
    written = 0
    out.mkdir(parents=True, exist_ok=True)

    for page in sorted(src_root.rglob("*.md")):
        if page.name in RESERVED:
            continue
        cid = page.relative_to(src_root).with_suffix("").as_posix()
        try:
            fm, body = wiki_viz.split_frontmatter(page.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        okf_fm = to_okf_frontmatter(fm, page.stem)
        if not okf_fm.get("description"):
            desc = first_sentence(body)
            if desc:
                okf_fm["description"] = desc
        if not okf_fm.get("timestamp"):
            try:
                mtime = datetime.fromtimestamp(page.stat().st_mtime, tz=timezone.utc)
                okf_fm["timestamp"] = mtime.isoformat(timespec="seconds")
            except OSError:
                pass
        # Reorder so OKF priority keys lead after late-added fields
        okf_fm = {**{k: okf_fm[k] for k in OKF_PRIORITY_KEYS if k in okf_fm},
                  **{k: v for k, v in okf_fm.items() if k not in OKF_PRIORITY_KEYS}}
        okf_body = wiki_viz.convert_wikilinks(body, resolver)
        dest = out / page.relative_to(src_root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = dump_frontmatter(okf_fm) + "\n\n" + (okf_body.rstrip() + "\n")
        dest.write_text(text, encoding="utf-8")
        concepts[cid] = {"title": okf_fm.get("title"), "description": okf_fm.get("description", "")}
        written += 1

    # Regenerate index.md per directory (progressive disclosure)
    index_count = 0
    all_dirs = {out} | {p for p in out.rglob("*") if p.is_dir()}
    for d in sorted(all_dirs):
        body = directory_index(d, out, concepts)
        if d == out:
            header = '---\nokf_version: "0.1"\n---\n\n'
            (d / "index.md").write_text(header + body + "\n", encoding="utf-8")
        else:
            (d / "index.md").write_text(body + "\n", encoding="utf-8")
        index_count += 1

    report = validate_bundle(out)
    report.update({"concepts": written, "index_files": index_count, "bundle": str(out)})
    return report


def validate_bundle(bundle: Path) -> dict[str, Any]:
    failures: list[str] = []
    checked = 0
    for page in sorted(bundle.rglob("*.md")):
        if page.name in RESERVED:
            continue
        checked += 1
        try:
            fm, _ = wiki_viz.split_frontmatter(page.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            failures.append(f"{page.relative_to(bundle)}: read error {exc}")
            continue
        if not fm:
            failures.append(f"{page.relative_to(bundle)}: missing frontmatter")
        elif not fm.get("type"):
            failures.append(f"{page.relative_to(bundle)}: empty/missing type")
    return {"conformant": not failures, "checked": checked, "failures": failures[:20], "failure_count": len(failures)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki", default=str(DEFAULT_WIKI), help="LLM Wiki root")
    parser.add_argument("--out", default=None, help="Bundle output dir (default: <wiki>/okf-bundle)")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    args = parser.parse_args()

    wiki = Path(args.wiki).expanduser().resolve()
    out = Path(args.out).expanduser().resolve() if args.out else wiki / "okf-bundle"
    report = export(wiki, out)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"OKF bundle -> {report['bundle']}")
        print(f"concepts={report['concepts']} index_files={report['index_files']} "
              f"checked={report['checked']} conformant={report['conformant']} failures={report['failure_count']}")
        for f in report["failures"]:
            print(f"- {f}")
    return 0 if report["conformant"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
