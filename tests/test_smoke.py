"""Smoke tests for the memvault engine, run against the public demo bundle."""

from pathlib import Path

import numpy as np

import memvault.wiki_embed as emb
import memvault.export_okf as exp
import memvault.wiki_viz as viz
import memvault.mcp_server as mcp
import memvault.query_wiki as qw

DEMO = Path(__file__).resolve().parent.parent / "examples" / "demo"


def test_hashing_embedder_ranks_related_higher():
    e = emb.get_embedder("hashing")
    v = e.embed([
        "auth service token verification",
        "auth service issues and verifies tokens",  # related
        "billing invoice usage metering",           # unrelated
    ])
    sims = v[1:] @ v[0]
    assert sims[0] > sims[1]


def test_export_is_okf_conformant(tmp_path):
    report = exp.export(DEMO, tmp_path / "bundle")
    assert report["conformant"], report["failures"]
    assert report["concepts"] >= 8
    # required field present on every exported concept
    sample = next((tmp_path / "bundle").rglob("architecture.md"))
    assert sample.read_text(encoding="utf-8").startswith("---\ntype:")


def test_viz_builds_graph_with_edges():
    g = viz.build_graph(DEMO, include_audit=False)
    assert len(g["nodes"]) >= 8
    assert len(g["edges"]) >= 5
    assert "service" in g["types"]


def test_keyword_search_finds_concept():
    terms = qw.query_terms("billing service invoice")
    matches = qw.markdown_matches(DEMO, terms, include_sensitive=False)
    assert any("billing-service" in m.location for m in matches)


def test_mcp_protocol_handshake_and_tools():
    mcp.WIKI = DEMO
    init = mcp.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "memvault"
    tl = mcp.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in tl["result"]["tools"]}
    assert {"wiki_answer_context", "wiki_search", "wiki_semantic_search"} <= names
    call = mcp.dispatch({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "wiki_search", "arguments": {"query": "api gateway", "limit": 3}},
    })
    assert call["result"]["isError"] is False
    assert "api-gateway" in call["result"]["content"][0]["text"]
