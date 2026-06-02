# Multi-stage build: a slim Python runtime carrying just the venv +
# package. One image runs either `flow materialise(-all)` or
# `flow serve` based on the entrypoint args.
#
# Build:   docker build -t flowmetrics .
# Run:     docker run --rm flowmetrics flow --help
# Ingest:  docker run --rm -v $PWD/contracts:/app/contracts \
#             -v $PWD/data:/app/data flowmetrics \
#             flow materialise-all --workflows-dir /app/contracts --data-dir /app/data
# Serve:   docker run --rm -p 8000:8000 -v $PWD/contracts:/app/contracts \
#             -v $PWD/data:/app/data flowmetrics \
#             flow serve --host 0.0.0.0 --workflows-dir /app/contracts --data-dir /app/data --password "$FLOW_PASSWORD"

# ---- Stage 1: builder. Install uv + sync deps into /opt/venv. ----
FROM python:3.13-slim AS builder

# Pinned uv version keeps the build deterministic. Must match (or
# exceed) the version that wrote `uv.lock` — older uv chokes on
# newer lockfile schema (e.g. editable packages without a version
# field). Bump in lockstep when the local toolchain advances.
ENV UV_VERSION=0.8.22
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /build

# Only the manifest first so dep installs cache; source comes second.
COPY pyproject.toml uv.lock README.md ./

# Build a venv at /opt/venv that the runtime stage can copy.
# `--frozen` enforces the lockfile so the image matches local.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN uv sync --frozen --no-install-project

# Install the project itself NON-editable so the runtime stage,
# which doesn't carry /build/src, still finds the package on import.
# A plain `uv sync` would write an editable `.pth` pointing at
# /build/src and break once the builder layer is dropped.
COPY src/ ./src/
RUN uv pip install --no-deps --python /opt/venv/bin/python .

# ---- Stage 2: runtime. Carry only the venv + package src. ----
FROM python:3.13-slim AS runtime

# Non-root user for the runtime — defence in depth.
RUN groupadd --system flow && useradd --system --gid flow --home-dir /app flow

# Copy the populated venv. The package's CLI entry point
# (`flow = "flowmetrics:main"`) lives at /opt/venv/bin/flow.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Working dir is also where we expect bind-mounted contracts/ and data/.
WORKDIR /app
RUN mkdir -p /app/contracts /app/data && chown -R flow:flow /app

USER flow

# Default port for `flow serve`.
EXPOSE 8000

# `docker run flowmetrics` with no args prints help so a misconfigured
# CMD surfaces immediately.
ENTRYPOINT []
CMD ["flow", "--help"]

LABEL org.opencontainers.image.title="flowmetrics"
LABEL org.opencontainers.image.description="Vacanti-style flow metrics + Monte Carlo forecasting"
LABEL org.opencontainers.image.source="https://github.com/dvhthomas/flowmetrics"
LABEL org.opencontainers.image.licenses="MIT"
