#!/usr/bin/env python3
"""memvault unified CLI.

Subcommands dispatch to the engine modules. Each module also remains runnable on
its own (``python -m memvault.<module>``); this wrapper gives one clean surface:

    memvault serve     # run the MCP server (stdio) for any harness
    memvault search    # hybrid semantic + keyword retrieval
    memvault keyword   # keyword-only search
    memvault index     # (re)build the semantic embedding index
    memvault ingest    # capture Codex / Claude / Gemini sessions
    memvault viz       # render the interactive knowledge graph (viz.html)
    memvault export    # export the wiki to a portable OKF bundle
    memvault install   # wire memvault into detected agent harnesses
"""

from __future__ import annotations

import importlib
import sys

SUBCOMMANDS = {
    "serve": "memvault.mcp_server",
    "search": "memvault.wiki_semantic_query",
    "keyword": "memvault.query_wiki",
    "index": "memvault.wiki_semantic_index",
    "ingest": "memvault.sessions",
    "viz": "memvault.wiki_viz",
    "export": "memvault.export_okf",
    "install": "memvault.install_harness",
}


def _usage() -> str:
    lines = ["memvault <command> [options]", "", "commands:"]
    width = max(len(c) for c in SUBCOMMANDS)
    helps = {
        "serve": "run the MCP server (stdio) for any harness",
        "search": "hybrid semantic + keyword retrieval",
        "keyword": "keyword-only search",
        "index": "(re)build the semantic embedding index",
        "ingest": "capture Codex / Claude / Gemini sessions",
        "viz": "render the interactive knowledge graph (viz.html)",
        "export": "export the wiki to a portable OKF bundle",
        "install": "wire memvault into detected agent harnesses",
    }
    for cmd in SUBCOMMANDS:
        lines.append(f"  {cmd.ljust(width)}  {helps.get(cmd, '')}")
    lines.append("")
    lines.append("Run `memvault <command> --help` for command options.")
    lines.append("Paths resolve from --wiki/--home, then $MEMVAULT_WIKI/$MEMVAULT_HOME, then ~/llm-wiki and ~.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_usage())
        return 0
    if argv[0] in ("-V", "--version"):
        from memvault import __version__
        print(__version__)
        return 0
    cmd, rest = argv[0], argv[1:]
    module_name = SUBCOMMANDS.get(cmd)
    if not module_name:
        print(f"unknown command: {cmd}\n", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2
    module = importlib.import_module(module_name)
    sys.argv = [f"memvault {cmd}", *rest]
    return int(module.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
