# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local, mostly-unattended content pipeline: a pending row in a Google Sheet triggers AI generation of an image + copy via n8n, a human approves/rejects the pair from a Flask dashboard (reachable over LAN and via an ngrok tunnel), and approval publishes to TikTok/X/Facebook/Instagram via Upload-Post. Rejection generates a new variant for the same item without touching the Sheet row or publishing anything.

See `PLAN.md` for the full design rationale, decisions, and phased build sequence — it is the authoritative spec for *why* things are structured this way. Re-read it when working on n8n workflows or the data model, since intent isn't always obvious from code alone.

## Running the stack

```
docker compose up -d postgres n8n        # infra only
docker compose up -d                     # full stack including dashboard
docker compose logs -f dashboard          # tail one service
docker compose restart dashboard          # after editing dashboard/app.py (no hot reload)
```

- n8n UI: `http://localhost:5678`
- Dashboard: `http://localhost:5000` (HTTP Basic Auth — see `DASHBOARD_USER`/`DASHBOARD_PASS`)
- Copy `.env.example` to `.env` and fill in real values before first run (never commit `.env`).
- Ollama is **not** a compose service — it must be running natively on the Windows host; n8n reaches it at `host.docker.internal:11434`.

There is no test suite, linter, or build step in this repo yet — verification happens via the manual per-phase checkpoints described in `PLAN.md`.

## Architecture

**Postgres is the single source of truth**, not the Google Sheet and not the dashboard. Three tables (`postgres/init.sql`):
- `items` — one row per Sheet row being processed; `status` drives the whole lifecycle (`pending → generating → awaiting_review → approved → publishing → published|partial|failed`); `active_variant_id` points at whichever variant is currently shown for review.
- `variants` — one row per generation attempt for an item (rejected attempts are kept, not deleted); `decision` is `pending|approved|rejected`.
- `publications` — one row per (variant, network) publish attempt, only ever created for the approved variant.

**Flask (`dashboard/app.py`) is read-only against Postgres.** It never runs state-changing SQL directly. Approve/Reject button clicks are relayed server-side as authenticated HTTP calls to n8n webhooks (`/webhook/approve`, `/webhook/reject`) — this keeps the shared token (`PIPELINE_SHARED_TOKEN`, sent as `X-Pipeline-Token`) out of client-side JS, and keeps n8n as the only writer of pipeline state.

**n8n owns all state transitions**, organized as workflows under `n8n/workflows/` (exported JSON, not yet populated as of this writing):
1. `generate` — cron (gated by `CRON_ENABLED`) or manual webhook; claims the first pending Sheet row, writes `generating` back to the Sheet cell (the Sheet's status column is the upstream lock against double-processing), upserts `items`, calls the sub-workflow below.
2. `generate-variant-core` — Groq for copy (falls back to host-native Ollama on error/timeout), Pollinations for image (falls back to HF Inference API), downloads image bytes into the shared `image_cache` volume, inserts a new `variants` row.
3. `reject` — flips the current variant to `decision='rejected'` (guarded by `WHERE decision='pending'` to avoid double-reject races) and re-invokes `generate-variant-core` for the same item; never creates a new Sheet row.
4. `approve-and-publish` — flips variant to `approved`, fires 4 parallel Upload-Post branches (`Continue on Fail`), records one `publications` row per network, and rolls the item up to `published`/`partial`/`failed` depending on how many networks succeeded.

**Concurrency guards worth knowing before touching this logic:** every decision webhook uses an `UPDATE ... WHERE decision='pending'` pattern and checks rows-affected (0 rows → 409, already decided) — this is the only thing preventing double-approve/double-reject races, so preserve it in any workflow changes.

**Images**: n8n downloads generated image bytes immediately (rather than linking to the original Pollinations/HF URL) into the `image_cache` volume, shared between the `n8n` and `dashboard` containers. The dashboard serves them at `/images/<path>`, and Upload-Post fetches from that stable dashboard-served URL rather than the original generation source.

**Auth is layered, not enterprise-grade** (documented as acceptable for a single-user personal tool): ngrok Google OAuth on the tunnel + Flask HTTP Basic Auth on the dashboard + n8n Header Auth (`X-Pipeline-Token`) on webhooks — three independent layers, each optional if you disable the outer ones.

**Docker topology**: `postgres` (internal only, no published port), `n8n` (port 5678), `dashboard` (port 5000, built from `dashboard/Dockerfile`), all on one bridge network `pipeline_net`. `postgres/init.sql` is mounted read-only into `docker-entrypoint-initdb.d/` and only runs on first container creation — schema changes need a manual migration or a volume reset, not just an edit to that file.

## Known drift from PLAN.md

`postgres/init.sql` creates `variants` before `items` (with the `variants → items` FK added later via a separate `ALTER TABLE`), the reverse order from the schema shown in `PLAN.md`. Functionally equivalent, but if you're diffing the two, that's why they don't match line-for-line.
