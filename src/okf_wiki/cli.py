#!/usr/bin/env python3
"""okf-wiki unified CLI.

Subcommands dispatch to the engine modules. Each module also remains runnable on
its own (``python -m okf_wiki.<module>``); this wrapper gives one clean surface:

    okf-wiki serve     # run the MCP server (stdio) for any harness
    okf-wiki search    # hybrid semantic + keyword retrieval
    okf-wiki keyword   # keyword-only search
    okf-wiki index     # (re)build the semantic embedding index
    okf-wiki ingest    # capture Codex / Claude / Gemini sessions
    okf-wiki viz       # render the interactive knowledge graph (viz.html)
    okf-wiki export    # export the wiki to a portable OKF bundle
    okf-wiki install   # wire okf-wiki into detected agent harnesses
"""

from __future__ import annotations

import importlib
import sys

SUBCOMMANDS = {
    "serve": "okf_wiki.okf_wiki_mcp",
    "search": "okf_wiki.wiki_semantic_query",
    "keyword": "okf_wiki.query_wiki",
    "index": "okf_wiki.wiki_semantic_index",
    "ingest": "okf_wiki.sessions",
    "viz": "okf_wiki.wiki_viz",
    "export": "okf_wiki.export_okf",
    "install": "okf_wiki.install_harness",
}


def _usage() -> str:
    lines = ["okf-wiki <command> [options]", "", "commands:"]
    width = max(len(c) for c in SUBCOMMANDS)
    helps = {
        "serve": "run the MCP server (stdio) for any harness",
        "search": "hybrid semantic + keyword retrieval",
        "keyword": "keyword-only search",
        "index": "(re)build the semantic embedding index",
        "ingest": "capture Codex / Claude / Gemini sessions",
        "viz": "render the interactive knowledge graph (viz.html)",
        "export": "export the wiki to a portable OKF bundle",
        "install": "wire okf-wiki into detected agent harnesses",
    }
    for cmd in SUBCOMMANDS:
        lines.append(f"  {cmd.ljust(width)}  {helps.get(cmd, '')}")
    lines.append("")
    lines.append("Run `okf-wiki <command> --help` for command options.")
    lines.append("Paths resolve from --wiki/--home, then $OKF_WIKI/$OKF_HOME, then ~/llm-wiki and ~.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_usage())
        return 0
    if argv[0] in ("-V", "--version"):
        from okf_wiki import __version__
        print(__version__)
        return 0
    cmd, rest = argv[0], argv[1:]
    module_name = SUBCOMMANDS.get(cmd)
    if not module_name:
        print(f"unknown command: {cmd}\n", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2
    module = importlib.import_module(module_name)
    sys.argv = [f"okf-wiki {cmd}", *rest]
    return int(module.main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
