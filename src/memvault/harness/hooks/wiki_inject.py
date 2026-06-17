#!/usr/bin/env python3
"""Wiki-first context injector for agent harness hooks (Codex / Claude Code).

Both Codex (`~/.codex/hooks.json` user_prompt_submit / session_start) and Claude
Code (UserPromptSubmit / SessionStart) feed whatever this script prints to stdout
into the model's context. This is the deterministic "consult the wiki first"
enforcement layer.

- SessionStart (no prompt): inject a short orientation + the memvault MCP tools.
- UserPromptSubmit: for local/personal/project/harness-engineering prompts, run
  `memvault search` and inject the top results (keyword gate avoids spending
  tokens on unrelated prompts).
- Never blocks, never errors out the harness loop.

Paths resolve from $MEMVAULT_WIKI / $MEMVAULT_HOME, defaulting to ~/llm-wiki and ~.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

WIKI = Path(os.environ.get("MEMVAULT_WIKI") or (Path.home() / "llm-wiki")).expanduser()

TRIGGERS = (
    "wiki", "local", "로컬", "내 ", "내가", "우리", "이전", "지난", "예전", "결정",
    "memory as llm", "memory-rag", "harness", "하네스", "codex", "claude", "opencode",
    "agent", "에이전트", "session", "세션", "research", "연구", "experiment", "실험",
    "project", "프로젝트", "decision", "okf",
)


def read_payload() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def extract_prompt(payload: dict) -> str:
    for key in ("prompt", "user_prompt", "userPrompt", "message", "input"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v
    for key in ("data", "params", "event"):
        sub = payload.get(key)
        if isinstance(sub, dict):
            inner = extract_prompt(sub)
            if inner:
                return inner
    return ""


def event_kind(payload: dict) -> str:
    for key in ("hook_event_name", "event", "type", "hook"):
        v = payload.get(key)
        if isinstance(v, str):
            return v.lower()
    return ""


def orientation() -> str:
    return (
        "[memvault] The local OKF wiki is the durable knowledge base for this "
        "machine (files, projects, research, and Codex/Claude/Gemini session "
        "history). For anything about local files, prior decisions, research, or "
        "harness engineering, consult it first via the `memvault` MCP tools "
        "(`wiki_answer_context`, `wiki_search`, `wiki_semantic_search`) or "
        "`memvault search \"<query>\"`."
    )


def deep_context(prompt: str) -> str:
    try:
        out = subprocess.run(
            [sys.executable, "-m", "memvault", "search", "--wiki", str(WIKI),
             "--mode", "hybrid", "--limit", "5", prompt],
            capture_output=True, text=True, timeout=30,
        )
        body = (out.stdout or "").strip()
    except Exception:
        body = ""
    if not body:
        return orientation()
    return "[memvault] Wiki-first context (consult before answering):\n" + body


def main() -> int:
    payload = read_payload()
    kind = event_kind(payload)
    prompt = extract_prompt(payload)
    try:
        if "session" in kind or not prompt:
            print(orientation())
        elif any(t in prompt.lower() for t in TRIGGERS):
            print(deep_context(prompt))
        else:
            print(orientation())
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
