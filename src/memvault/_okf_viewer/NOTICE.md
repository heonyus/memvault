# Vendored OKF viewer assets

`viz.html`, `viz.css`, and `viz.js` in this directory are copied verbatim from
the Open Knowledge Format (OKF) reference viewer:

- Source: https://github.com/GoogleCloudPlatform/knowledge-catalog
  (`okf/src/enrichment_agent/viewer/`)
- License: Apache License 2.0 (see `okf/LICENSE.md` in that repository)
- Copyright: Google LLC

They are loaded unmodified by `tools/wiki_viz.py`, which substitutes the same
template placeholders the upstream generator uses (`/*__VIZ_CSS__*/`,
`/*__VIZ_JS__*/`, `__BUNDLE_NAME__`, `__BUNDLE_DATA__`). The graph viewer
renders with Cytoscape.js and marked loaded from a CDN at view time.
