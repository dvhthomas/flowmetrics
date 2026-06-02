---
title: Deploy with Docker
---

# Deploy with Docker

> **Diátaxis: How-to.** Containerised dashboard + materialize.

```bash
docker compose up serve
# → http://localhost:8000

docker compose --profile ingest run --rm materialize
```

`compose.yml` bind-mounts `./contracts` and `./data` so edits and
deletes round-trip to the host. Both services use the same image
built from `Dockerfile`.

## CI-hosted ingest (no host to operate)

See `.github/workflows/materialize.yml` — runs `flow materialize --all`
on a cron schedule and uploads `data/` as a build artifact.

## Next

- [Schedule data fetches](schedule-fetches.md) — alternative if you
  prefer host-side scheduling.
- [Troubleshooting § Address already in use from Docker](troubleshooting.md#address-already-in-use-from-docker)
