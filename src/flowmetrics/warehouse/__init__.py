"""Layer 1 — data access over the DuckDB warehouse.

Pure SQL: a connection in, raw typed rows out. No windowing, no
percentiles, no chart decisions. The chart-model layer
(`flowmetrics.charts`) consumes these rows and does the deciding.
"""
