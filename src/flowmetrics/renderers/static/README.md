# Vendored JS for interactive HTML charts

The HTML report inlines these three bundles so the chart renders with
no network connection (`flow ... --format html` is intended to be a
fully self-contained, archival artifact).

| File                | Version  | License   | Source                                              |
| ------------------- | -------- | --------- | --------------------------------------------------- |
| `vega.min.js`       | 5.x      | BSD-3     | https://cdn.jsdelivr.net/npm/vega@5/build/vega.min.js |
| `vega-lite.min.js`  | 5.x      | BSD-3     | https://cdn.jsdelivr.net/npm/vega-lite@5/build/vega-lite.min.js |
| `vega-embed.min.js` | 6.x      | BSD-3     | https://cdn.jsdelivr.net/npm/vega-embed@6/build/vega-embed.min.js |

Refresh procedure (when bumping versions):

```sh
cd src/flowmetrics/renderers/static
curl -sSL -o vega.min.js       https://cdn.jsdelivr.net/npm/vega@5/build/vega.min.js
curl -sSL -o vega-lite.min.js  https://cdn.jsdelivr.net/npm/vega-lite@5/build/vega-lite.min.js
curl -sSL -o vega-embed.min.js https://cdn.jsdelivr.net/npm/vega-embed@6/build/vega-embed.min.js
```

Total inlined weight: ~830KB. A rendered HTML report carries this
once per file. If size becomes a concern, a future `--cdn` flag could
replace the inlined scripts with `<script src="...jsdelivr...">` tags.
