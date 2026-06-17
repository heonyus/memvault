#!/usr/bin/env python3
"""Index local AI agent conversations (Codex, Claude Code, Gemini) into the wiki.

This reads the user's local agent session logs and emits:

- a redacted JSONL manifest at ``raw/manifests/agent-session-index.jsonl``
- a generated overview page at ``wiki/system/agent-session-index.md``

Only visible chat turns are read. Tool outputs, images, attachments, and
credential-looking strings are skipped or scrubbed, and sensitive-looking
sessions are reduced to counts only. Full transcripts are never copied into the
wiki; the manifest stores redacted snippets, topic keywords, and metadata that
point back to the original ``.jsonl`` file.

The scan is incremental: a session file whose size and mtime match the previous
manifest row is reused without re-reading, so the hourly refresh stays cheap
even though the full corpus is several GB.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator


TOOL_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import okfio as build_inventory  # type: ignore[import-not-found]  # noqa: E402
import query_wiki  # type: ignore[import-not-found]  # noqa: E402


DEFAULT_HOME = Path("~")
DEFAULT_WIKI = DEFAULT_HOME / "llm-wiki"
MANIFEST_REL = "raw/manifests/agent-session-index.jsonl"
PAGE_REL = "wiki/system/agent-session-index.md"

# Skip parsing absurdly long single lines (almost always base64 images or large
# tool payloads, never user prose). Keep the byte guard generous but bounded.
MAX_LINE_BYTES = 2_000_000
# How many user turns to scan per session for snippet/keyword extraction.
TOPIC_SCAN_TURNS = 60

SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{16,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),  # email
    re.compile(r"\b[0-9a-fA-F]{40,}\b"),  # long hex (tokens, hashes)
]

STOPWORDS = {
    # english
    "the", "and", "for", "you", "your", "are", "was", "this", "that", "with",
    "have", "has", "not", "but", "can", "will", "from", "out", "get", "got",
    "into", "than", "then", "them", "they", "what", "when", "where", "which",
    "who", "how", "why", "all", "any", "our", "use", "used", "using", "let",
    "make", "made", "now", "one", "two", "see", "like", "just", "want", "need",
    "should", "would", "could", "about", "there", "here", "also", "more", "most",
    "some", "such", "only", "very", "much", "many", "each", "both", "able",
    "please", "thanks", "okay", "yeah", "yes", "no", "ok", "지금", "그리고",
    "그래서", "근데", "그냥", "이거", "그거", "저거", "이게", "그게", "해줘",
    "해서", "하는", "하고", "에서", "이제", "너가", "내가", "우리", "정말",
    "이해", "하나", "라고", "에게", "으로", "처럼", "한번", "한 번",
}


def now_stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def md_date() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def scrub(text: str) -> str:
    """Mask credential-looking substrings."""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def clip(text: str, width: int = 200) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + "\u2026"


def is_sensitive(text: str) -> bool:
    return bool(text) and query_wiki.is_sensitive_text(text)


def md_cell(text: str) -> str:
    """Escape a value for a Markdown table cell."""
    return (text or "").replace("\\", "\u29f5").replace("|", "\u2502").replace("\n", " ")


def parse_ts(value) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value


def short_date(ts: str | None) -> str:
    return ts[:10] if ts else "unknown"


# --------------------------------------------------------------------------- #
# Per-source streaming parsers. Each yields normalized dicts:
#   {"role": "user"|"assistant", "text": str, "ts": str|None}
# plus a meta dict captured during the stream.
# --------------------------------------------------------------------------- #


def _iter_json_lines(path: Path) -> Iterator[dict]:
    try:
        handle = path.open(encoding="utf-8", errors="ignore")
    except OSError:
        return
    with handle:
        for line in handle:
            if len(line) > MAX_LINE_BYTES:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def _claude_text(content) -> str:
    """Extract user/assistant prose from a Claude message.content value."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts)
    return ""


def parse_codex(path: Path) -> tuple[list[dict], dict]:
    turns: list[dict] = []
    meta: dict = {"source": "codex"}
    for obj in _iter_json_lines(path):
        typ = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if typ == "session_meta":
            meta["session_id"] = payload.get("id")
            meta["cwd"] = payload.get("cwd")
            meta["originator"] = payload.get("originator")
            meta["model"] = payload.get("model_provider") or payload.get("model")
            continue
        if typ == "turn_context" and not meta.get("model"):
            model = payload.get("model")
            if isinstance(model, str):
                meta["model"] = model
            continue
        if typ == "event_msg":
            ptype = payload.get("type")
            if ptype in {"user_message", "agent_message"}:
                message = payload.get("message")
                if isinstance(message, str) and message.strip():
                    turns.append(
                        {
                            "role": "user" if ptype == "user_message" else "assistant",
                            "text": message,
                            "ts": parse_ts(obj.get("timestamp")),
                        }
                    )
    return turns, meta


def parse_claude(path: Path) -> tuple[list[dict], dict]:
    turns: list[dict] = []
    meta: dict = {"source": "claude", "session_id": path.stem}
    for obj in _iter_json_lines(path):
        typ = obj.get("type")
        if typ not in {"user", "assistant"}:
            continue
        if obj.get("isSidechain"):
            continue
        if not meta.get("cwd") and isinstance(obj.get("cwd"), str):
            meta["cwd"] = obj["cwd"]
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        if typ == "assistant":
            model = message.get("model")
            if isinstance(model, str) and model and model != "<synthetic>":
                meta["model"] = model
        text = _claude_text(message.get("content"))
        if text and text.strip():
            turns.append(
                {"role": typ, "text": text, "ts": parse_ts(obj.get("timestamp"))}
            )
    if not meta.get("model"):
        meta["model"] = "claude-code"
    return turns, meta


def parse_gemini(path: Path) -> tuple[list[dict], dict]:
    turns: list[dict] = []
    # session id = the brain/<uuid> directory
    session_id = path.parent.parent.parent.name if len(path.parents) >= 3 else path.stem
    meta: dict = {"source": "gemini", "session_id": session_id, "model": "gemini-antigravity"}
    for obj in _iter_json_lines(path):
        typ = obj.get("type")
        if typ == "USER_INPUT":
            role = "user"
        elif typ == "PLANNER_RESPONSE":
            role = "assistant"
        else:
            continue
        content = obj.get("content")
        if isinstance(content, dict):
            content = content.get("text") or content.get("message") or json.dumps(content, ensure_ascii=False)
        if isinstance(content, str):
            text = re.sub(r"</?USER_REQUEST>", " ", content).strip()
            if text:
                turns.append({"role": role, "text": text, "ts": parse_ts(obj.get("created_at"))})
    return turns, meta


PARSERS = {"codex": parse_codex, "claude": parse_claude, "gemini": parse_gemini}


def discover(home: Path, sources: list[str]) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    if "codex" in sources:
        base = home / ".codex" / "sessions"
        if base.exists():
            found += [("codex", p) for p in base.rglob("rollout-*.jsonl")]
    if "claude" in sources:
        base = home / ".claude" / "projects"
        if base.exists():
            for p in base.rglob("*.jsonl"):
                rel = p.as_posix()
                if "/subagents/" in rel or p.name == "journal.jsonl":
                    continue
                found.append(("claude", p))
    if "gemini" in sources:
        base = home / ".gemini"
        if base.exists():
            found += [("gemini", p) for p in base.rglob("transcript.jsonl")]
    return found


def topic_keywords(texts: Iterable[str], limit: int = 8) -> list[str]:
    counts: Counter = Counter()
    for text in texts:
        for token in re.findall(r"[0-9a-zA-Z][0-9a-zA-Z_-]+|[\uac00-\ud7a3]{2,}", query_wiki.normalize(text)):
            if len(token) < 2 or token in STOPWORDS or token.isdigit():
                continue
            if re.fullmatch(r"[0-9a-f]{16,}", token) or any(p.search(token) for p in SECRET_PATTERNS):
                continue
            counts[token] += 1
    keywords = [word for word, _ in counts.most_common(limit * 3)]
    keywords = [w for w in keywords if not is_sensitive(w)][:limit]
    return keywords


def extract_row(source: str, path: Path, home: Path, stat) -> dict:
    turns, meta = PARSERS[source](path)
    user_turns = [t for t in turns if t["role"] == "user"]
    asst_turns = [t for t in turns if t["role"] == "assistant"]
    timestamps = [t["ts"] for t in turns if t["ts"]]
    started = min(timestamps) if timestamps else None
    ended = max(timestamps) if timestamps else None

    cwd = meta.get("cwd")
    project = Path(cwd).name if cwd else (meta.get("source") or source)
    scan_texts = [t["text"] for t in user_turns[:TOPIC_SCAN_TURNS]]
    keywords = topic_keywords(scan_texts)

    first_snip = ""
    for t in user_turns:
        candidate = scrub(clip(t["text"], 200))
        if candidate:
            first_snip = candidate
            break

    # Sensitivity gate: if the project path or first snippet looks sensitive,
    # keep only non-identifying counts.
    sensitive = bool(cwd and is_sensitive(cwd)) or is_sensitive(first_snip)
    if sensitive:
        first_snip = "[sensitive-redacted]"
        keywords = []
        project_field = "[sensitive-redacted]"
        cwd_field = None
    else:
        project_field = project
        cwd_field = cwd

    rel = build_inventory.rel_to(path, home)
    date = short_date(started)
    title = f"{source} · {project_field} · {date}"

    return {
        "path": rel,
        "title": title,
        "id": str(meta.get("session_id") or path.stem),
        "kind": source,
        "status": "indexed",
        "project": project_field,
        "cwd": cwd_field,
        "model": meta.get("model"),
        "originator": meta.get("originator"),
        "started": started,
        "ended": ended,
        "date": date,
        "user_messages": len(user_turns),
        "assistant_messages": len(asst_turns),
        "question": first_snip,
        "markers": keywords,
        "sensitive_name": sensitive,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        "mtime_ns": stat.st_mtime_ns,
    }


def load_cache(manifest: Path) -> dict[str, dict]:
    cache: dict[str, dict] = {}
    if not manifest.exists():
        return cache
    with manifest.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            path = row.get("path")
            if isinstance(path, str):
                cache[path] = row
    return cache


def build_rows(home: Path, wiki: Path, sources: list[str], rebuild: bool) -> tuple[list[dict], dict]:
    manifest = wiki / MANIFEST_REL
    cache = {} if rebuild else load_cache(manifest)
    files = discover(home, sources)
    rows: list[dict] = []
    stats = Counter({"total": 0, "reused": 0, "parsed": 0, "skipped_empty": 0, "errors": 0})

    for source, path in files:
        stats["total"] += 1
        try:
            stat = path.stat()
        except OSError:
            stats["errors"] += 1
            continue
        rel = build_inventory.rel_to(path, home)
        cached = cache.get(rel)
        if (
            cached
            and cached.get("mtime_ns") == stat.st_mtime_ns
            and cached.get("size") == stat.st_size
            and cached.get("kind") == source
        ):
            rows.append(cached)
            stats["reused"] += 1
            continue
        try:
            row = extract_row(source, path, home, stat)
        except Exception:  # one bad file must not abort the whole index
            stats["errors"] += 1
            continue
        if row["user_messages"] == 0 and row["assistant_messages"] == 0:
            stats["skipped_empty"] += 1
            continue
        rows.append(row)
        stats["parsed"] += 1

    rows.sort(key=lambda r: (r.get("kind") or "", r.get("started") or "", r.get("id") or ""))
    return rows, stats


def render_page(rows: list[dict], stats: Counter, manifest_rel: str, limit: int) -> str:
    by_source: Counter = Counter(r["kind"] for r in rows)
    by_project: Counter = Counter(r.get("project") or "?" for r in rows if not r.get("sensitive_name"))
    by_day: Counter = Counter(r.get("date") or "unknown" for r in rows)
    total_user = sum(int(r.get("user_messages") or 0) for r in rows)
    total_asst = sum(int(r.get("assistant_messages") or 0) for r in rows)
    sensitive = sum(1 for r in rows if r.get("sensitive_name"))

    def count_table(title: str, counter: Counter, top: int = 25) -> str:
        lines = [f"### {title}", "", "| Item | Count |", "| --- | ---: |"]
        for key, count in counter.most_common(top):
            lines.append(f"| `{md_cell(str(key))}` | {count} |")
        return "\n".join(lines) + "\n"

    recent = sorted(rows, key=lambda r: r.get("ended") or r.get("started") or "", reverse=True)[:limit]
    rec_lines = [
        "| Date | Source | Project | Model | Msgs | First user message |",
        "| --- | --- | --- | --- | ---: | --- |",
    ]
    for r in recent:
        msgs = f"{r.get('user_messages', 0)}/{r.get('assistant_messages', 0)}"
        snippet = md_cell(clip(str(r.get("question") or ""), 90))
        rec_lines.append(
            f"| {md_cell(r.get('date') or '?')} | {md_cell(r.get('kind') or '?')} | "
            f"`{md_cell(str(r.get('project') or '?'))}` | {md_cell(str(r.get('model') or '?'))} | "
            f"{msgs} | {snippet} |"
        )
    recent_table = "\n".join(rec_lines)

    source_line = ", ".join(f"{k}={v}" for k, v in sorted(by_source.items())) or "none"

    return f"""---
title: Agent Session Index
type: system
status: generated
updated: {md_date()}
sources:
  - ~/.codex/sessions
  - ~/.claude/projects
  - ~/.gemini
  - ~/llm-wiki/tools/build_agent_session_index.py
  - ~/llm-wiki/{manifest_rel}
---

# Agent Session Index

Generated: `{now_stamp()}`

Captured AI agent conversations from Codex, Claude Code, and Gemini. This is a
redacted metadata + topic index, not a transcript copy. Each row points back to
the original local `.jsonl` session file. Sensitive-looking sessions are reduced
to counts only.

## Summary

- Sessions indexed: {len(rows)}
- By source: {source_line}
- Total user messages: {total_user}
- Total assistant messages: {total_asst}
- Sensitive sessions (counts only): {sensitive}
- Scan: total={stats['total']} parsed={stats['parsed']} reused={stats['reused']} empty={stats['skipped_empty']} errors={stats['errors']}

Manifest: `{manifest_rel}`

## By Source

{count_table("Sessions Per Source", by_source, top=10)}
## Top Projects

{count_table("Sessions Per Project", by_project, top=30)}
## Activity By Day

{count_table("Sessions Per Day", by_day, top=40)}
## Recent Sessions

{recent_table}

## Notes

This page is generated by `tools/build_agent_session_index.py`. The manifest is
searchable through `tools/query_wiki.py` and the semantic retriever. Only visible
chat turns are read; tool outputs, attachments, and credential-looking strings
are skipped or scrubbed.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=str(DEFAULT_HOME), help="Home root")
    parser.add_argument("--wiki", default=str(DEFAULT_WIKI), help="LLM Wiki root")
    parser.add_argument(
        "--sources",
        default="codex,claude,gemini",
        help="Comma-separated subset of: codex, claude, gemini",
    )
    parser.add_argument("--limit", type=int, default=40, help="Recent sessions shown in the page")
    parser.add_argument("--rebuild", action="store_true", help="Ignore the incremental cache")
    parser.add_argument("--json", action="store_true", help="Emit summary JSON to stdout")
    args = parser.parse_args()

    home = Path(args.home).expanduser().resolve()
    wiki = Path(args.wiki).expanduser().resolve()
    sources = [s.strip() for s in args.sources.split(",") if s.strip() in PARSERS]
    if not sources:
        print("no valid sources selected", file=sys.stderr)
        return 2

    rows, stats = build_rows(home, wiki, sources, args.rebuild)

    manifest = wiki / MANIFEST_REL
    page = wiki / PAGE_REL
    written = build_inventory.write_jsonl_atomic(manifest, rows)
    build_inventory.write_text_atomic(page, render_page(rows, stats, MANIFEST_REL, args.limit))

    summary = {
        "sessions": len(rows),
        "manifest_rows": written,
        "manifest": str(manifest),
        "page": str(page),
        "scan": dict(stats),
        "by_source": dict(Counter(r["kind"] for r in rows)),
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"indexed {len(rows)} sessions -> {manifest}")
        print(f"scan: {dict(stats)}")
        print(f"by source: {summary['by_source']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
