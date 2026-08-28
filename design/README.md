# Design working files

Source for the design canvas at
https://claude.ai/code/artifact/526d60cf-d77c-44b1-8fe3-9d15051286d7

Four directions for the watch grid, same data in each:

| File | Direction |
|---|---|
| `Main.dc.html` | A · Terminal — dense table, monospace, ops register |
| `Editorial.dc.html` | B · Editorial — serif display, generous space |
| `Brutalist.dc.html` | C · Brutalist — hard borders, inverted state blocks |
| `SoftSaaS.dc.html` | D · Soft SaaS — muted, rounded, four columns |

`canvas.json` positions them. The seeded `.html` is not kept — it is 2.5 MB of
editor payload and is regenerated from these files.

To change a direction, edit its `.dc.html` and re-seed:

```bash
BASE=<design skill base dir>
node "$BASE/seed-canvas.mjs" --template "$BASE/payload.template.html" \
  --out restock-dashboard.html --title "Restock Dashboard" \
  --artboard Main.dc.html --artboard Editorial.dc.html \
  --artboard Brutalist.dc.html --artboard SoftSaaS.dc.html \
  --canvas canvas.json
```

then republish to the same artifact URL.

## Sizing

Frame heights in `canvas.json` must match each root's `min-height`, and every
file sets `box-sizing: border-box`. Without it the root's padding is added to
`min-height` and the artboard overflows its frame by exactly that amount —
which is how all four shipped clipped on the first pass.

## The state vocabulary these encode

`in_stock` · `sold_out` · `armed` · `failing` · `no_baseline`, each rendered as
an icon **and** a word so it survives being read without color. `failing` must
never read as `sold_out`: a wall of calm sold-out cards is exactly what a dead
monitor looks like, which is the failure the whole app is built to avoid.
