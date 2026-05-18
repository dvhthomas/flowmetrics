# Sample reports

_Generated 2026-05-18 14:08:40 UTC_

Open the `.html` files directly in a browser — no server needed; Vega-Lite loads from CDN via plain `<script>` tags.

Each report comes in three formats: **html** (interactive chart), **txt** (terminal output), and **json** (agent-readable envelope). Reports marked _n/a_ are skipped for sources whose data shape doesn't support the report (e.g. CFD on a GitHub repo without intermediate workflow states).

## astral-sh/uv

_Async OSS PR workflow — review cycles span days, not hours. Uses `--gap-hours=24` because the inter-event gap distribution (P85=10h, P90=19h) shows cross-day pickup as a single session. Demonstrates per-repo clustering tuning._

| Report | Formats |
| --- | --- |
| Efficiency | [html](astral-sh_uv/efficiency.html) · [txt](astral-sh_uv/efficiency.txt) · [json](astral-sh_uv/efficiency.json) |
| WWIBD: Date | [html](astral-sh_uv/forecast-when-done.html) · [txt](astral-sh_uv/forecast-when-done.txt) · [json](astral-sh_uv/forecast-when-done.json) |
| WWIBD: How Many | [html](astral-sh_uv/forecast-how-many.html) · [txt](astral-sh_uv/forecast-how-many.txt) · [json](astral-sh_uv/forecast-how-many.json) |
| Cycle-time scatterplot | [html](astral-sh_uv/scatterplot.html) · [txt](astral-sh_uv/scatterplot.txt) · [json](astral-sh_uv/scatterplot.json) |
| CFD | [html](astral-sh_uv/cfd.html) · [txt](astral-sh_uv/cfd.txt) · [json](astral-sh_uv/cfd.json) |
| Aging WIP | [html](astral-sh_uv/aging.html) · [txt](astral-sh_uv/aging.txt) · [json](astral-sh_uv/aging.json) |

## pytest-dev/pytest

_Established OSS team with conventional review cadence. Default `--gap-hours=4` (Vacanti's corporate-synchronous setting) is appropriate here — contrast with uv's async rhythm above._

| Report | Formats |
| --- | --- |
| Efficiency | [html](pytest-dev_pytest/efficiency.html) · [txt](pytest-dev_pytest/efficiency.txt) · [json](pytest-dev_pytest/efficiency.json) |
| WWIBD: Date | [html](pytest-dev_pytest/forecast-when-done.html) · [txt](pytest-dev_pytest/forecast-when-done.txt) · [json](pytest-dev_pytest/forecast-when-done.json) |
| WWIBD: How Many | [html](pytest-dev_pytest/forecast-how-many.html) · [txt](pytest-dev_pytest/forecast-how-many.txt) · [json](pytest-dev_pytest/forecast-how-many.json) |
| Cycle-time scatterplot | [html](pytest-dev_pytest/scatterplot.html) · [txt](pytest-dev_pytest/scatterplot.txt) · [json](pytest-dev_pytest/scatterplot.json) |
| CFD | [html](pytest-dev_pytest/cfd.html) · [txt](pytest-dev_pytest/cfd.txt) · [json](pytest-dev_pytest/cfd.json) |
| Aging WIP | [html](pytest-dev_pytest/aging.html) · [txt](pytest-dev_pytest/aging.txt) · [json](pytest-dev_pytest/aging.json) |

## huggingface/transformers

_Massive scale with large external-contribution backlog. Uses `--exclude-stale-days=14` so headline metrics reflect engaged work, not zombie PRs sitting in queue indefinitely. Demonstrates signal-vs-noise filtering at OSS scale._

| Report | Formats |
| --- | --- |
| Efficiency | [html](huggingface_transformers/efficiency.html) · [txt](huggingface_transformers/efficiency.txt) · [json](huggingface_transformers/efficiency.json) |
| WWIBD: Date | [html](huggingface_transformers/forecast-when-done.html) · [txt](huggingface_transformers/forecast-when-done.txt) · [json](huggingface_transformers/forecast-when-done.json) |
| WWIBD: How Many | [html](huggingface_transformers/forecast-how-many.html) · [txt](huggingface_transformers/forecast-how-many.txt) · [json](huggingface_transformers/forecast-how-many.json) |
| Cycle-time scatterplot | [html](huggingface_transformers/scatterplot.html) · [txt](huggingface_transformers/scatterplot.txt) · [json](huggingface_transformers/scatterplot.json) |
| CFD | [html](huggingface_transformers/cfd.html) · [txt](huggingface_transformers/cfd.txt) · [json](huggingface_transformers/cfd.json) |
| Aging WIP | [html](huggingface_transformers/aging.html) · [txt](huggingface_transformers/aging.txt) · [json](huggingface_transformers/aging.json) |

## pre-commit/pre-commit

_Small-team OSS baseline — minimal label vocabulary, default settings work. The 'no tuning required' anchor for the demo set._

| Report | Formats |
| --- | --- |
| Efficiency | [html](pre-commit_pre-commit/efficiency.html) · [txt](pre-commit_pre-commit/efficiency.txt) · [json](pre-commit_pre-commit/efficiency.json) |
| WWIBD: Date | [html](pre-commit_pre-commit/forecast-when-done.html) · [txt](pre-commit_pre-commit/forecast-when-done.txt) · [json](pre-commit_pre-commit/forecast-when-done.json) |
| WWIBD: How Many | [html](pre-commit_pre-commit/forecast-how-many.html) · [txt](pre-commit_pre-commit/forecast-how-many.txt) · [json](pre-commit_pre-commit/forecast-how-many.json) |
| Cycle-time scatterplot | [html](pre-commit_pre-commit/scatterplot.html) · [txt](pre-commit_pre-commit/scatterplot.txt) · [json](pre-commit_pre-commit/scatterplot.json) |
| CFD | [html](pre-commit_pre-commit/cfd.html) · [txt](pre-commit_pre-commit/cfd.txt) · [json](pre-commit_pre-commit/cfd.json) |
| Aging WIP | [html](pre-commit_pre-commit/aging.html) · [txt](pre-commit_pre-commit/aging.txt) · [json](pre-commit_pre-commit/aging.json) |

## rust-lang/rust

_Rust compiler — large, label-driven OSS workflow with rich S-* state labels (`S-waiting-on-author`, `S-waiting-on-review`, etc.). The Aging chart is the standout: columns map directly to the team's WIP states, so each in-flight PR shows up under the state it's actually blocked on. Demonstrates label-driven WIP workflow._

| Report | Formats |
| --- | --- |
| Efficiency | _n/a_ |
| WWIBD: Date | _n/a_ |
| WWIBD: How Many | _n/a_ |
| Cycle-time scatterplot | [html](rust-lang_rust/scatterplot.html) · [txt](rust-lang_rust/scatterplot.txt) · [json](rust-lang_rust/scatterplot.json) |
| CFD | _n/a_ |
| Aging WIP | [html](rust-lang_rust/aging.html) · [txt](rust-lang_rust/aging.txt) · [json](rust-lang_rust/aging.json) |

## CalcMark/go-calcmark

_Solo developer using Issue+PR linking. Issues capture the work request, PRs the implementation; `--include-issues` folds both into the same canonical pipeline and stitched cycle times use the closing PR's mergedAt. Demonstrates Issue+PR stitching._

| Report | Formats |
| --- | --- |
| Efficiency | [html](CalcMark_go-calcmark/efficiency.html) · [txt](CalcMark_go-calcmark/efficiency.txt) · [json](CalcMark_go-calcmark/efficiency.json) |
| WWIBD: Date | [html](CalcMark_go-calcmark/forecast-when-done.html) · [txt](CalcMark_go-calcmark/forecast-when-done.txt) · [json](CalcMark_go-calcmark/forecast-when-done.json) |
| WWIBD: How Many | [html](CalcMark_go-calcmark/forecast-how-many.html) · [txt](CalcMark_go-calcmark/forecast-how-many.txt) · [json](CalcMark_go-calcmark/forecast-how-many.json) |
| Cycle-time scatterplot | [html](CalcMark_go-calcmark/scatterplot.html) · [txt](CalcMark_go-calcmark/scatterplot.txt) · [json](CalcMark_go-calcmark/scatterplot.json) |
| CFD | [html](CalcMark_go-calcmark/cfd.html) · [txt](CalcMark_go-calcmark/cfd.txt) · [json](CalcMark_go-calcmark/cfd.json) |
| Aging WIP | [html](CalcMark_go-calcmark/aging.html) · [txt](CalcMark_go-calcmark/aging.txt) · [json](CalcMark_go-calcmark/aging.json) |

## ASF/CASSANDRA

_Apache Cassandra — rich Jira workflow with 5+ explicit statuses. `status_intervals` come directly from the changelog so efficiency is computed via status-duration (no event clustering). Demonstrates Jira-direct workflow versus GitHub-cluster heuristic._

| Report | Formats |
| --- | --- |
| Efficiency | [html](ASF_CASSANDRA/efficiency.html) · [txt](ASF_CASSANDRA/efficiency.txt) · [json](ASF_CASSANDRA/efficiency.json) |
| WWIBD: Date | [html](ASF_CASSANDRA/forecast-when-done.html) · [txt](ASF_CASSANDRA/forecast-when-done.txt) · [json](ASF_CASSANDRA/forecast-when-done.json) |
| WWIBD: How Many | [html](ASF_CASSANDRA/forecast-how-many.html) · [txt](ASF_CASSANDRA/forecast-how-many.txt) · [json](ASF_CASSANDRA/forecast-how-many.json) |
| Cycle-time scatterplot | [html](ASF_CASSANDRA/scatterplot.html) · [txt](ASF_CASSANDRA/scatterplot.txt) · [json](ASF_CASSANDRA/scatterplot.json) |
| CFD | [html](ASF_CASSANDRA/cfd.html) · [txt](ASF_CASSANDRA/cfd.txt) · [json](ASF_CASSANDRA/cfd.json) |
| Aging WIP | [html](ASF_CASSANDRA/aging.html) · [txt](ASF_CASSANDRA/aging.txt) · [json](ASF_CASSANDRA/aging.json) |

## ASF/BIGTOP

_Apache Bigtop — smaller-team Jira project. Same canonical pipeline as Cassandra; scale variation shows how the same workflow definitions handle very different team sizes._

| Report | Formats |
| --- | --- |
| Efficiency | [html](ASF_BIGTOP/efficiency.html) · [txt](ASF_BIGTOP/efficiency.txt) · [json](ASF_BIGTOP/efficiency.json) |
| WWIBD: Date | [html](ASF_BIGTOP/forecast-when-done.html) · [txt](ASF_BIGTOP/forecast-when-done.txt) · [json](ASF_BIGTOP/forecast-when-done.json) |
| WWIBD: How Many | [html](ASF_BIGTOP/forecast-how-many.html) · [txt](ASF_BIGTOP/forecast-how-many.txt) · [json](ASF_BIGTOP/forecast-how-many.json) |
| Cycle-time scatterplot | [html](ASF_BIGTOP/scatterplot.html) · [txt](ASF_BIGTOP/scatterplot.txt) · [json](ASF_BIGTOP/scatterplot.json) |
| CFD | [html](ASF_BIGTOP/cfd.html) · [txt](ASF_BIGTOP/cfd.txt) · [json](ASF_BIGTOP/cfd.json) |
| Aging WIP | [html](ASF_BIGTOP/aging.html) · [txt](ASF_BIGTOP/aging.txt) · [json](ASF_BIGTOP/aging.json) |

---

## Reference

- **[README](https://github.com/dvhthomas/flowmetrics/blob/main/README.md)** — What flowmetrics is and how to run it.
- **[Metrics](https://github.com/dvhthomas/flowmetrics/blob/main/docs/METRICS.md)** — How cycle / active / wait / flow efficiency are computed.
- **[Tuning](https://github.com/dvhthomas/flowmetrics/blob/main/docs/TUNING.md)** — Per-repo --gap-hours / --exclude-stale-days / --include-issues.
- **[Forecasting](https://github.com/dvhthomas/flowmetrics/blob/main/docs/FORECAST.md)** — Monte Carlo when-done and how-many.
- **[Decisions](https://github.com/dvhthomas/flowmetrics/blob/main/docs/DECISIONS.md)** — Architectural trade-offs and known constraints.
- **[Glossary](https://github.com/dvhthomas/flowmetrics/blob/main/docs/GLOSSARY.md)** — Vacanti terms and our usage.
