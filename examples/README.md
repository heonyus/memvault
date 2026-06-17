# Demo bundle

`demo/` is a small, synthetic, public OKF-shaped knowledge bundle (a fictional
"Acme Platform" knowledge base) used by the docs, tests, and the README
screenshot. It contains no personal data.

Render it:

```bash
okf-wiki viz --wiki examples/demo --out demo.html && open demo.html
```

Export it to a conformant OKF v0.1 bundle:

```bash
okf-wiki export --wiki examples/demo --out /tmp/acme-okf
```
