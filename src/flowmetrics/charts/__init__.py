"""Layer 2 — the chart model.

Stateless, pure Python: raw warehouse rows + a view window in, a
fully-resolved chart model out. Every chart decision — percentiles,
slider bounds, tick density, window clamping, empty-states,
headline text — is made here, where it can be tested without a
DuckDB warehouse or a Vega spec. The web view layer
(`flowmetrics.web.components`) only translates a model into a spec.
"""
