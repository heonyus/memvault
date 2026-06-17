#!/usr/bin/env python3
"""Install the memvault integration into every detected agent harness.

This is the oh-my-* style distributor: it wires the one portable MCP engine
(``tools/mcp_server.py``) plus wiki-first routing/enforcement into each harness
using that harness's own extension points, idempotently and with backups.

Per harness:
- OpenCode  : drop ``plugin/llm-wiki.js`` (registers the memvault MCP; coexists
              with omo). Reversible by deleting the file.
- Codex     : append ``[mcp_servers.memvault]`` to ``~/.codex/config.toml``.
- Claude    : add ``memvault`` to ``~/.claude.json`` mcpServers and wire
              SessionStart + UserPromptSubmit hooks in ``~/.claude/settings.json``
              to ``harness/hooks/wiki_inject.py`` (deterministic wiki-first).
- All       : append the AGENTS.md routing block to ``~/AGENTS.md`` and
              ``~/CLAUDE.md`` (between markers, idempotent).

Modes: default applies changes (with .bak backups); ``--dry-run`` reports only;
``--check`` reports current wiring status; ``--uninstall`` reverses changes.
Codex *hooks* (trust-hash + omx co-managed) are intentionally left as an opt-in
documented step, not auto-applied.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import memvault.config as _config

HOME = Path.home()
WIKI = _config.wiki_root()
PKG = Path(__file__).resolve().parent          # source templates ship inside the package
HARNESS = PKG / "harness"
HOOK = HARNESS / "hooks" / "wiki_inject.py"
PY = sys.executable or "python3"
MCP_CMD = [PY, str(PKG / "mcp_server.py"), "--wiki", str(WIKI)]
MARK_BEGIN = "<!-- memvault:begin -->"
MARK_END = "<!-- memvault:end -->"


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def backup(path: Path) -> Path | None:
    if path.exists():
        b = path.with_name(path.name + f".bak-{stamp()}")
        shutil.copy2(path, b)
        return b
    return None


# --------------------------------------------------------------------------- #
# OpenCode
# --------------------------------------------------------------------------- #


def opencode_status() -> tuple[bool, str]:
    base = HOME / ".config" / "opencode"
    if not base.exists():
        return False, "opencode not detected"
    target = base / "plugin" / "llm-wiki.js"
    return target.exists(), f"plugin {'present' if target.exists() else 'absent'}: {target}"


def _opencode_plugin_js() -> str:
    cmd = ", ".join(json.dumps(a) for a in MCP_CMD)
    return (
        "// memvault — OpenCode plugin (generated). Registers the memvault MCP\n"
        "// server so every session gets wiki_answer_context / wiki_search /\n"
        "// wiki_semantic_search. Coexists with omo; delete this file to remove.\n"
        "export const OkfWiki = async () => ({\n"
        "  config: async (config) => {\n"
        "    try {\n"
        "      config.mcp = config.mcp || {};\n"
        f"      if (!config.mcp[\"memvault\"]) config.mcp[\"memvault\"] = {{ type: \"local\", command: [{cmd}], enabled: true }};\n"
        "    } catch (_e) {}\n"
        "  },\n"
        "});\n"
        "export default OkfWiki;\n"
    )


def opencode_install(dry: bool) -> str:
    base = HOME / ".config" / "opencode"
    if not base.exists():
        return "skip: opencode not detected"
    plugin_dir = base / "plugin"
    target = plugin_dir / "llm-wiki.js"
    if dry:
        return f"would write opencode plugin -> {target}"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(_opencode_plugin_js(), encoding="utf-8")
    return f"installed opencode plugin -> {target}"


def opencode_uninstall(dry: bool) -> str:
    target = HOME / ".config" / "opencode" / "plugin" / "llm-wiki.js"
    if not target.exists():
        return "opencode plugin not present"
    if dry:
        return f"would remove {target}"
    target.unlink()
    return f"removed {target}"


# --------------------------------------------------------------------------- #
# Codex (config.toml mcp_servers append)
# --------------------------------------------------------------------------- #


def codex_block() -> str:
    args = ", ".join(json.dumps(a) for a in MCP_CMD[1:])
    return (
        "\n[mcp_servers.memvault]\n"
        f"command = {json.dumps(MCP_CMD[0])}\n"
        f"args = [{args}]\n"
        "startup_timeout_sec = 30\n"
    )


def codex_status() -> tuple[bool, str]:
    cfg = HOME / ".codex" / "config.toml"
    if not cfg.exists():
        return False, "codex not detected"
    present = "[mcp_servers.memvault]" in cfg.read_text(encoding="utf-8")
    return present, f"mcp_servers.memvault {'present' if present else 'absent'}"


def codex_install(dry: bool) -> str:
    cfg = HOME / ".codex" / "config.toml"
    if not cfg.exists():
        return "skip: codex not detected"
    text = cfg.read_text(encoding="utf-8")
    if "[mcp_servers.memvault]" in text:
        return "codex mcp already registered"
    if dry:
        return f"would append [mcp_servers.memvault] to {cfg}"
    backup(cfg)
    cfg.write_text(text.rstrip() + "\n" + codex_block(), encoding="utf-8")
    return f"registered codex mcp_servers.memvault in {cfg}"


def codex_uninstall(dry: bool) -> str:
    cfg = HOME / ".codex" / "config.toml"
    if not cfg.exists() or "[mcp_servers.memvault]" not in cfg.read_text(encoding="utf-8"):
        return "codex mcp not present"
    if dry:
        return f"would remove [mcp_servers.memvault] from {cfg}"
    lines = cfg.read_text(encoding="utf-8").splitlines()
    out, skip = [], False
    for ln in lines:
        if ln.strip() == "[mcp_servers.memvault]":
            skip = True
            continue
        if skip and ln.startswith("[") and ln.strip() != "[mcp_servers.memvault]":
            skip = False
        if not skip:
            out.append(ln)
    backup(cfg)
    cfg.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return f"removed codex mcp_servers.memvault from {cfg}"


# --------------------------------------------------------------------------- #
# Claude (mcpServers in ~/.claude.json + hooks in settings.json)
# --------------------------------------------------------------------------- #


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def claude_status() -> tuple[bool, str]:
    cj = HOME / ".claude.json"
    settings = HOME / ".claude" / "settings.json"
    if not (HOME / ".claude").exists():
        return False, "claude not detected"
    mcp = "memvault" in (_load_json(cj).get("mcpServers") or {}) if cj.exists() else False
    hooks = _load_json(settings).get("hooks") or {}
    hooked = any("wiki_inject" in json.dumps(hooks.get(ev, [])) for ev in ("SessionStart", "UserPromptSubmit"))
    return (mcp and hooked), f"mcp={'yes' if mcp else 'no'} hooks={'yes' if hooked else 'no'}"


def _claude_hook_entry() -> dict:
    cmd = f'MEMVAULT_WIKI={json.dumps(str(WIKI))} {json.dumps(PY)[1:-1]} {HOOK}'
    return {"hooks": [{"type": "command", "command": cmd}]}


def claude_install(dry: bool) -> str:
    cdir = HOME / ".claude"
    if not cdir.exists():
        return "skip: claude not detected"
    msgs = []
    # MCP into ~/.claude.json
    cj = HOME / ".claude.json"
    if cj.exists():
        data = _load_json(cj)
        servers = data.setdefault("mcpServers", {}) if not dry else (data.get("mcpServers") or {})
        if "memvault" in (data.get("mcpServers") or {}):
            msgs.append("claude mcp already registered")
        elif dry:
            msgs.append("would add memvault to ~/.claude.json mcpServers")
        else:
            backup(cj)
            servers["memvault"] = {"command": MCP_CMD[0], "args": MCP_CMD[1:]}
            data["mcpServers"] = servers
            cj.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            msgs.append("registered claude mcp memvault")
    else:
        msgs.append("note: ~/.claude.json absent; run `claude mcp add -s user memvault -- " + " ".join(MCP_CMD) + "`")
    # Hooks into settings.json
    settings = cdir / "settings.json"
    data = _load_json(settings)
    hooks = data.get("hooks") or {}
    changed = False
    for ev in ("SessionStart", "UserPromptSubmit"):
        existing = hooks.get(ev) or []
        if any("wiki_inject" in json.dumps(e) for e in existing):
            continue
        existing.append(_claude_hook_entry())
        hooks[ev] = existing
        changed = True
    if not changed:
        msgs.append("claude hooks already wired")
    elif dry:
        msgs.append("would wire claude SessionStart + UserPromptSubmit hooks")
    else:
        backup(settings)
        data["hooks"] = hooks
        settings.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        msgs.append("wired claude hooks -> wiki_inject.py")
    return "; ".join(msgs)


def claude_uninstall(dry: bool) -> str:
    cdir = HOME / ".claude"
    if not cdir.exists():
        return "claude not detected"
    msgs = []
    cj = HOME / ".claude.json"
    if cj.exists():
        data = _load_json(cj)
        if "memvault" in (data.get("mcpServers") or {}):
            if dry:
                msgs.append("would remove claude mcp memvault")
            else:
                backup(cj)
                data["mcpServers"].pop("memvault", None)
                cj.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                msgs.append("removed claude mcp memvault")
    settings = cdir / "settings.json"
    data = _load_json(settings)
    hooks = data.get("hooks") or {}
    changed = False
    for ev in ("SessionStart", "UserPromptSubmit"):
        kept = [e for e in (hooks.get(ev) or []) if "wiki_inject" not in json.dumps(e)]
        if len(kept) != len(hooks.get(ev) or []):
            hooks[ev] = kept
            changed = True
    if changed and not dry:
        backup(settings)
        data["hooks"] = hooks
        settings.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        msgs.append("removed claude hooks")
    elif changed:
        msgs.append("would remove claude hooks")
    return "; ".join(msgs) or "claude integration not present"


# --------------------------------------------------------------------------- #
# AGENTS.md / CLAUDE.md routing snippet
# --------------------------------------------------------------------------- #


def routing_snippet() -> str:
    return (
        f"{MARK_BEGIN}\n"
        "## Local OKF wiki — required orientation (memvault)\n\n"
        "Before answering anything about the user's local files, projects, research, "
        "prior decisions, agent sessions, or harness engineering, consult the local "
        f"OKF wiki at `{WIKI}` **first**.\n\n"
        "- Prefer the `memvault` MCP tools: `wiki_answer_context` (call first), "
        "`wiki_search`, `wiki_semantic_search`.\n"
        f"- CLI fallback: `memvault search --wiki {WIKI} \"<query>\"`.\n"
        "- Label local claims as wiki-backed, file-backed, or inferred. If the wiki "
        "has no coverage, say so, then inspect the needed local files.\n"
        f"{MARK_END}\n"
    )


def routing_status(path: Path) -> bool:
    return path.exists() and MARK_BEGIN in path.read_text(encoding="utf-8")


def routing_install(path: Path, dry: bool) -> str:
    snippet = routing_snippet()
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if MARK_BEGIN in text:
            return f"{path.name}: routing already present"
        if dry:
            return f"would append routing block to {path}"
        backup(path)
        path.write_text(text.rstrip() + "\n\n" + snippet, encoding="utf-8")
    else:
        if dry:
            return f"would create {path} with routing block"
        path.write_text(snippet, encoding="utf-8")
    return f"{path.name}: routing block added"


def routing_uninstall(path: Path, dry: bool) -> str:
    if not routing_status(path):
        return f"{path.name}: routing not present"
    text = path.read_text(encoding="utf-8")
    pre = text.split(MARK_BEGIN, 1)[0].rstrip()
    post = text.split(MARK_END, 1)[1] if MARK_END in text else ""
    if dry:
        return f"would strip routing block from {path}"
    backup(path)
    path.write_text((pre + "\n" + post.lstrip()).rstrip() + "\n", encoding="utf-8")
    return f"{path.name}: routing block removed"


# --------------------------------------------------------------------------- #


def run(mode: str) -> int:
    dry = mode == "dry-run"
    actions = []
    if mode == "check":
        for name, fn in (("opencode", opencode_status), ("codex", codex_status), ("claude", claude_status)):
            ok, detail = fn()
            actions.append(f"[{'OK' if ok else '..'}] {name}: {detail}")
        for p in (HOME / "AGENTS.md", HOME / "CLAUDE.md"):
            actions.append(f"[{'OK' if routing_status(p) else '..'}] routing {p.name}")
        print("# memvault harness wiring")
        for a in actions:
            print(a)
        return 0

    if mode == "uninstall":
        actions.append(opencode_uninstall(False))
        actions.append(codex_uninstall(False))
        actions.append(claude_uninstall(False))
        actions.append(routing_uninstall(HOME / "AGENTS.md", False))
        actions.append(routing_uninstall(HOME / "CLAUDE.md", False))
    else:
        actions.append(opencode_install(dry))
        actions.append(codex_install(dry))
        actions.append(claude_install(dry))
        actions.append(routing_install(HOME / "AGENTS.md", dry))
        actions.append(routing_install(HOME / "CLAUDE.md", dry))

    header = {"dry-run": "# memvault install (DRY RUN)", "install": "# memvault install", "uninstall": "# memvault uninstall"}[mode]
    print(header)
    for a in actions:
        print(f"- {a}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="Report planned changes without applying")
    g.add_argument("--check", action="store_true", help="Report current wiring status")
    g.add_argument("--uninstall", action="store_true", help="Reverse the integration")
    args = parser.parse_args()
    mode = "dry-run" if args.dry_run else "check" if args.check else "uninstall" if args.uninstall else "install"
    return run(mode)


if __name__ == "__main__":
    raise SystemExit(main())
