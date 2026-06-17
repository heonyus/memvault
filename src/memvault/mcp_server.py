#!/usr/bin/env python3
"""Stdio MCP server exposing the local LLM Wiki (OKF bundle) to any MCP harness.

This is the single portable capability engine: Claude Code, Codex CLI, OpenCode,
gajae code, Gemini, and any other MCP-speaking harness register this one server
and get wiki-first retrieval. The retrieval logic is reused from the existing
tools (no re-implementation, no denylist drift):

Tools
- wiki_answer_context : compact wiki-first orientation for a local-context query
- wiki_search         : keyword search over wiki markdown + metadata manifests
                        (includes the agent session index)
- wiki_semantic_search: hybrid dense + keyword retrieval (falls back to keyword
                        if the embedding index is absent)

Resources
- memvault://wiki/<concept-id> : read any wiki page; index + system pages are listed

Transport: newline-delimited JSON-RPC 2.0 over stdio (the MCP stdio framing).
Pure standard library, so it runs under the same interpreter as the rest of the
tools with no pip install.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import query_wiki  # type: ignore[import-not-found]  # noqa: E402

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "memvault", "version": "0.1.0"}
DEFAULT_WIKI = Path("~/llm-wiki")
DEFAULT_ROOT = Path("~")

WIKI = DEFAULT_WIKI
ROOT = DEFAULT_ROOT


# --------------------------------------------------------------------------- #
# Tool implementations (reuse existing tool functions)
# --------------------------------------------------------------------------- #


def _fmt_matches(matches: list, limit: int) -> str:
    if not matches:
        return "No matches."
    lines = []
    for m in matches[:limit]:
        lines.append(f"- {m.location} [{m.source}, score={m.score}]\n  {m.text}")
    return "\n".join(lines)


def tool_wiki_search(args: dict) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("query is required")
    limit = int(args.get("limit", 12))
    include_sensitive = bool(args.get("include_sensitive", False))
    terms = query_wiki.query_terms(query)
    wiki = sorted(
        query_wiki.markdown_matches(WIKI, terms, include_sensitive),
        key=lambda m: (-m.score, m.location),
    )
    manifests = sorted(
        query_wiki.manifest_matches(WIKI, terms, include_sensitive),
        key=lambda m: (-m.score, m.location),
    )
    return (
        f"# Wiki keyword search: {query}\n\n"
        f"## Wiki Matches\n{_fmt_matches(wiki, limit)}\n\n"
        f"## Metadata / Session Matches\n{_fmt_matches(manifests, limit)}"
    )


def tool_wiki_answer_context(args: dict) -> str:
    import wiki_semantic_query as wsq  # type: ignore[import-not-found]

    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("query is required")
    limit = int(args.get("limit", 5))

    terms = query_wiki.query_terms(query)
    kw = sorted(
        query_wiki.markdown_matches(WIKI, terms, False) + query_wiki.manifest_matches(WIKI, terms, False),
        key=lambda m: (-m.score, m.location),
    )
    try:
        sem = wsq.semantic_ranking(WIKI, query, "auto", max(limit, 20), False)
    except SystemExit:
        sem = []

    # Read-first = unique pages from the strongest matches (semantic first, then keyword).
    pages: list[str] = ["wiki/index.md"]
    for r in sem:
        loc = str(r.get("location", ""))
        page = loc.split(".md:", 1)[0] + ".md" if ".md:" in loc else loc
        if page.endswith(".md") and page not in pages:
            pages.append(page)
    for m in kw:
        page = m.location.split(".md:", 1)[0] + ".md" if ".md:" in m.location else m.location
        if page.endswith(".md") and page not in pages:
            pages.append(page)

    out = [f"# Wiki answer context: {query}", "", "## Read First"]
    for page in pages[: limit + 1]:
        out.append(f"- {page}")
    out.append("\n## Top Matches")
    for m in kw[:limit]:
        out.append(f"- {m.location} [{m.source}, score={m.score}]\n  {m.text}")
    out.append("\n## Answer Contract")
    for item in (
        "Answer from wiki-backed matches first.",
        "Use metadata/session matches as inventory pointers, not full content evidence.",
        "Inspect original files only when wiki coverage is missing or file-level work is asked.",
        "Label local claims as wiki-backed, file-backed, or inferred.",
    ):
        out.append(f"- {item}")
    return "\n".join(out)


def tool_wiki_semantic_search(args: dict) -> str:
    import wiki_semantic_query as wsq  # type: ignore[import-not-found]

    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("query is required")
    limit = int(args.get("limit", 10))
    mode = str(args.get("mode", "hybrid"))
    include_sensitive = bool(args.get("include_sensitive", False))
    note = ""
    try:
        semantic = (
            wsq.semantic_ranking(WIKI, query, "auto", max(limit, 40), include_sensitive)
            if mode in {"hybrid", "semantic"}
            else []
        )
    except SystemExit as exc:  # index missing -> degrade to keyword
        semantic = []
        note = f"(semantic index unavailable: {exc}; keyword-only)\n\n"
        mode = "keyword"
    keyword = (
        wsq.keyword_ranking(WIKI, query, max(limit, 40), include_sensitive)
        if mode in {"hybrid", "keyword"}
        else []
    )
    if mode == "semantic":
        results = [{**r, "rrf": None, "keyword": None} for r in semantic][:limit]
    elif mode == "keyword":
        results = [{**r, "rrf": None, "cosine": None} for r in keyword][:limit]
    else:
        results = wsq.fuse(semantic, keyword, limit)

    lines = [f"# Wiki hybrid retrieval ({mode}): {query}", ""]
    if not results:
        lines.append("No matches.")
    for i, r in enumerate(results, 1):
        bits = []
        for k in ("rrf", "cosine", "keyword"):
            if r.get(k) is not None:
                bits.append(f"{k}={r[k]}")
        lines.append(f"{i}. {r.get('location')} [{r.get('kind')}] {' '.join(bits)}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
    return note + "\n".join(lines)


TOOLS = [
    {
        "name": "wiki_answer_context",
        "description": (
            "Wiki-first orientation for any question about the user's local files, "
            "projects, research, prior decisions, or agent sessions. Call this FIRST "
            "for local/personal/project-memory questions; returns read-first pages, "
            "wiki-backed matches, and metadata signals."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms / question"},
                "limit": {"type": "integer", "description": "Matches per section (default 5)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "wiki_search",
        "description": "Keyword search over wiki markdown pages and metadata manifests (includes the agent session index of Codex/Claude/Gemini conversations).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "default 12"},
                "include_sensitive": {"type": "boolean", "description": "default false"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "wiki_semantic_search",
        "description": "Hybrid dense + keyword retrieval over the wiki and agent sessions (reciprocal-rank fusion). Falls back to keyword search if the embedding index is absent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "default 10"},
                "mode": {"type": "string", "enum": ["hybrid", "semantic", "keyword"], "description": "default hybrid"},
            },
            "required": ["query"],
        },
    },
]

TOOL_IMPL = {
    "wiki_answer_context": tool_wiki_answer_context,
    "wiki_search": tool_wiki_search,
    "wiki_semantic_search": tool_wiki_semantic_search,
}


# --------------------------------------------------------------------------- #
# Resources (wiki pages)
# --------------------------------------------------------------------------- #


def list_resources() -> list[dict]:
    out: list[dict] = []
    index = WIKI / "wiki" / "index.md"
    if index.exists():
        out.append({"uri": "memvault://wiki/index", "name": "Wiki Index", "mimeType": "text/markdown"})
    sysdir = WIKI / "wiki" / "system"
    if sysdir.exists():
        for page in sorted(sysdir.glob("*.md")):
            cid = page.relative_to(WIKI / "wiki").with_suffix("").as_posix()
            out.append({"uri": f"memvault://wiki/{cid}", "name": cid, "mimeType": "text/markdown"})
    return out


def read_resource(uri: str) -> dict:
    if not uri.startswith("memvault://wiki/"):
        raise ValueError(f"unsupported uri scheme: {uri}")
    rel = uri[len("memvault://wiki/"):]
    candidate = (WIKI / "wiki" / rel)
    for path in (candidate, candidate.with_suffix(".md"), Path(str(candidate) + ".md")):
        try:
            resolved = path.resolve()
            resolved.relative_to((WIKI / "wiki").resolve())  # path-traversal guard
        except (ValueError, OSError):
            continue
        if resolved.is_file():
            return {"uri": uri, "mimeType": "text/markdown", "text": resolved.read_text(encoding="utf-8")}
    raise FileNotFoundError(f"no wiki page for {uri}")


# --------------------------------------------------------------------------- #
# JSON-RPC dispatch
# --------------------------------------------------------------------------- #


def _result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def dispatch(msg: dict) -> dict | None:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    # Notifications (no id) -> no response
    if req_id is None and method != "initialize":
        return None

    if method == "initialize":
        return _result(req_id, {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": SERVER_INFO,
        })
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        impl = TOOL_IMPL.get(name)
        if impl is None:
            return _error(req_id, -32602, f"unknown tool: {name}")
        try:
            text = impl(args)
            return _result(req_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:  # surface tool errors as MCP tool errors, not crashes
            return _result(req_id, {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True})
    if method == "resources/list":
        return _result(req_id, {"resources": list_resources()})
    if method == "resources/read":
        try:
            return _result(req_id, {"contents": [read_resource(str(params.get("uri", "")))]})
        except Exception as exc:
            return _error(req_id, -32602, str(exc))
    if method == "prompts/list":
        return _result(req_id, {"prompts": []})
    if method == "resources/templates/list":
        return _result(req_id, {"resourceTemplates": [
            {"uriTemplate": "memvault://wiki/{path}", "name": "Wiki page", "mimeType": "text/markdown"}
        ]})

    if req_id is not None:
        return _error(req_id, -32601, f"method not found: {method}")
    return None


def serve() -> int:
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            response = dispatch(msg)
        except Exception:  # never die on a single bad message
            response = _error(msg.get("id"), -32603, "internal error\n" + traceback.format_exc())
        if response is not None:
            out.write(json.dumps(response, ensure_ascii=False) + "\n")
            out.flush()
    return 0


def selftest() -> int:
    samples = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "wiki_search", "arguments": {"query": "memory as llm passage replacement", "limit": 3}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "wiki_semantic_search", "arguments": {"query": "tikfinity backend worker", "limit": 3}}},
        {"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
        {"jsonrpc": "2.0", "id": 6, "method": "resources/read", "params": {"uri": "memvault://wiki/index"}},
    ]
    for s in samples:
        r = dispatch(s)
        if r is None:
            print(f"-> notification {s.get('method')}: (no response)")
            continue
        if "error" in r:
            print(f"-> {s.get('method')}: ERROR {r['error']}")
            continue
        res = r["result"]
        if s.get("method") == "tools/list":
            print(f"-> tools/list: {[t['name'] for t in res['tools']]}")
        elif s.get("method") == "tools/call":
            c = res["content"][0]["text"]
            print(f"-> tools/call {s['params']['name']} (isError={res.get('isError')}): {c[:160].replace(chr(10),' ')}...")
        elif s.get("method") == "resources/list":
            print(f"-> resources/list: {len(res['resources'])} resources (e.g. {res['resources'][0]['uri'] if res['resources'] else 'none'})")
        elif s.get("method") == "resources/read":
            print(f"-> resources/read: {len(res['contents'][0]['text'])} chars")
        else:
            print(f"-> {s.get('method')}: ok {list(res)}")
    return 0


def main() -> int:
    global WIKI, ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki", default=str(DEFAULT_WIKI))
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--selftest", action="store_true", help="Run an in-process protocol smoke test")
    args = parser.parse_args()
    WIKI = Path(args.wiki).expanduser().resolve()
    ROOT = Path(args.root).expanduser().resolve()
    return selftest() if args.selftest else serve()


if __name__ == "__main__":
    raise SystemExit(main())
