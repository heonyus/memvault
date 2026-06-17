# Contributing

Thanks for your interest in memvault.

## Dev setup

```bash
pip install -e ".[dev,yaml]"
pytest -q
```

## Guidelines

- The engine core stays dependency-light: stdlib + `numpy`. Heavier backends
  (neural embeddings, YAML) are optional extras with graceful fallbacks.
- Keep retrieval/redaction logic in one place — no per-harness copies.
- New harness integrations go through `memvault/install_harness.py` and must be
  idempotent, backed up, and reversible (`--uninstall`).
- Run `pytest -q` and the CLI smoke commands in `.github/workflows/ci.yml`
  before opening a PR.

## Scope

memvault is the *engine* (capture / retrieve / serve / visualize / export).
Personal knowledge-base content and bespoke home-audit pipelines belong in your
own private bundle, not here.
