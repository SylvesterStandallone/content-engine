# Content Pipeline — Refined Implementation Plan

## Context

The user wants a local, mostly-unattended pipeline: a pending row in Google Sheets triggers AI generation of an image + copy, a human approves/rejects the pair from a dashboard reachable over wifi (and via an ngrok tunnel), and approval publishes to TikTok/X/Facebook/Instagram via Upload-Post while rejection produces a new variant without touching the Sheet row or publishing anything. The project now lives in `C:\Users\Ivan\Coding\content-engine`. This is a greenfield build — Fase 0 must first create and `git init` the new folder. The user asked for an "ultra"-refined plan (deeper than the pasted spec) before any code is written, since several parts of the original spec were underspecified: how Postgres history reconciles with the dashboard's live queue, how n8n workflows avoid double-processing, and how the ngrok-exposed services stay reasonably secure.

Decisions confirmed with the user in this session:
- **Auth**: ngrok Google OAuth on the tunnel **+** app-level Basic Auth on the Flask dashboard **+** shared-token Header Auth on n8n webhooks (defense in depth, two independent layers).
- **Ollama fallback**: runs natively on Windows host, not in Docker; n8n reaches it via `host.docker.internal:11434`.
- **Image handling**: n8n downloads Pollinations/HF image bytes immediately after generation and caches them on a volume the dashboard serves at a stable URL; Upload-Post fetches from that stable (ngrok-exposed) URL, not the original Pollinations link.
- **Partial publish**: a distinct `partial` status is tracked per item when some but not all of the 4 networks succeed, so the dashboard can flag exactly which network(s) need a manual retry.

## Data model — Postgres as single source of truth

No sqlite, no in-memory queue. The Flask dashboard is a thin read/write view over Postgres; all state-changing writes happen through n8n webhooks (not directly from Flask), so there's one place to guard against double-submits.

```sql
-- one row per Sheet row being processed
CREATE TABLE items (
  id                SERIAL PRIMARY KEY,
  sheet_row_id      TEXT NOT NULL UNIQUE,
  topic             TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'pending',
                    -- pending | generating | awaiting_review | approved | publishing | published | partial | failed
  active_variant_id INTEGER REFERENCES variants(id),
  created_at        TIMESTAMPTZ DEFAULT now(),
  updated_at        TIMESTAMPTZ DEFAULT now()
);

-- one row per generation attempt (approved AND rejected variants both kept)
CREATE TABLE variants (
  id            SERIAL PRIMARY KEY,
  item_id       INTEGER NOT NULL REFERENCES items(id),
  attempt_no    INTEGER NOT NULL,
  copy_text     TEXT,
  image_path    TEXT,                       -- local cached path served by dashboard, e.g. /images/{variant_id}.png
  image_source  TEXT,                       -- 'pollinations' | 'huggingface'
  text_source   TEXT,                       -- 'groq' | 'ollama'
  decision      TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected
  decided_at    TIMESTAMPTZ,
  created_at    TIMESTAMPTZ DEFAULT now(),
  UNIQUE(item_id, attempt_no)
);

-- one row per network publish attempt, only for the approved variant
CREATE TABLE publications (
  id            SERIAL PRIMARY KEY,
  variant_id    INTEGER NOT NULL REFERENCES variants(id),
  network       TEXT NOT NULL,              -- tiktok | x | facebook | instagram
  status        TEXT NOT NULL DEFAULT 'pending', -- pending | success | failed
  response_meta JSONB,
  error_message TEXT,
  created_at    TIMESTAMPTZ DEFAULT now(),
  updated_at    TIMESTAMPTZ DEFAULT now()
);
```

**Concurrency guards:**
- The Google Sheet's status column is the upstream lock: cron poll and the manual button both re-check "still literally pending" before writing `generating` to the cell, so a duplicate trigger finds nothing left to claim.
- Within Postgres, approve/reject webhook handlers use `UPDATE ... WHERE decision='pending'` and check rows-affected; 0 rows → return 409 (already decided), preventing double-approve/double-reject races.
- Flask never runs state-changing SQL directly — it only reads, and relays button clicks to n8n webhooks server-side (so the shared token never reaches client JS).

## n8n workflows

Five workflows total:

1. **`generate`** — Cron trigger (toggleable, gated by an `IF` on env `CRON_ENABLED`) **or** manual webhook `POST /webhook/generate-next`. Reads Sheet for first `pending` row, claims it by writing `generating` to its status cell, upserts into `items`, calls sub-workflow `generate-variant-core`, then sets Sheet + `items.status = 'awaiting_review'`.
2. **`generate-variant-core`** (sub-workflow, called via Execute Workflow node) — Groq call (fallback to host-native Ollama on error/timeout) → copy; Pollinations call (fallback to HF Inference API) → image; download image bytes to the shared volume; insert `variants` row (`attempt_no` = max+1); update `items.active_variant_id`.
3. **`reject`** — Webhook `POST /webhook/reject` `{item_id, variant_id}`, Header Auth token required. Atomically flips `variants.decision='rejected'` (guard: `WHERE decision='pending'`), sets `items.status='generating'`, calls `generate-variant-core` again for the same `item_id`. Never touches the Sheet row beyond its generating/awaiting_review cycle — no new row.
4. **`approve-and-publish`** — Webhook `POST /webhook/approve` `{item_id, variant_id}`, Header Auth required. Flips `variants.decision='approved'` (same guard), `items.status='publishing'`, then 4 parallel Upload-Post HTTP branches (`Continue on Fail` each) fetching the image from the dashboard's stable cached URL; inserts one `publications` row per network. Afterward: all succeed → `published`; none succeed → `failed`; some succeed → `partial`. Sheet status cell updated to match.
5. (Optional, deferred to a later pass) **`retry-network`** — per-network manual republish for `partial` items, out of scope for v1 per the plan below unless the user wants it now.

## Ngrok / auth setup

- ngrok tunnel(s) configured with Google OAuth (`ngrok http --oauth=google ...`) covering both the n8n and dashboard hosts.
- Flask dashboard additionally requires Basic Auth (`flask-httpauth`, credentials from `DASHBOARD_USER`/`DASHBOARD_PASS` env vars) as a second layer independent of the tunnel.
- n8n webhooks (`reject`, `approve`, `generate-next`) use n8n's built-in Header Auth credential type, header `X-Pipeline-Token` checked against `PIPELINE_SHARED_TOKEN` env var — native n8n feature, no custom code.
- README documents this is "layered but not enterprise-grade," appropriate for a personal single-user tool.

## Docker Compose topology

- **postgres** (`postgres:16-alpine`) — internal-only (no published port), volume `postgres_data`, schema bootstrapped via `postgres/init.sql` mounted to `docker-entrypoint-initdb.d/`.
- **n8n** (`n8nio/n8n`) — volume `n8n_data` for workflows/credentials, port `5678` published (LAN + ngrok target), env `N8N_HOST`, `WEBHOOK_URL`, `N8N_ENCRYPTION_KEY`; reaches Ollama via `host.docker.internal:11434`.
- **dashboard** (built from `dashboard/Dockerfile`) — port `5000` published (LAN + ngrok target), env `DATABASE_URL` (→ `postgres` service), `N8N_WEBHOOK_BASE_URL` (→ `http://n8n:5678`), `PIPELINE_SHARED_TOKEN`, `DASHBOARD_USER/PASS`; serves cached images from a shared volume `image_cache`.
- All three on a single bridge network `pipeline_net` for internal DNS resolution.
- Ollama is **not** a compose service — runs natively on the Windows host per the user's decision.

## Build sequence

**Fase 0 — Infra**
- Create the project folder `C:\Users\Ivan\Coding\content-engine` and `git init` it.
- Create `docker-compose.yml`, `.env.example`, `postgres/init.sql` (DDL from above).
- `docker compose up -d postgres n8n` → n8n UI reachable at `localhost:5678`; verify Postgres via an n8n Postgres node `SELECT 1`.

**Fase 1 — Data source (Sheets)**
- Build `generate` workflow skeleton: cron (disabled by default) + manual webhook, both → Google Sheets node reading pending rows.
- Export to `n8n/workflows/generate.json` (verify export doesn't embed credential secrets before committing).
- Checkpoint: manual webhook trigger picks the row, writes `generating` back to its status cell.

**Fase 2 — Text gen (Groq)**
- Build `generate-variant-core` sub-workflow now (not later, to avoid rework): Groq HTTP node + Ollama fallback branch (IF on Groq HTTP error), writes `variants.copy_text`.
- Export `n8n/workflows/generate-variant-core.json`.
- Checkpoint: `variants` row has non-null `copy_text` and correct `text_source`.

**Fase 3 — Image gen (Pollinations)**
- Add Pollinations HTTP GET (URL-encoded prompt) + HF Inference API fallback; download bytes to `image_cache` volume; store `image_path`.
- Checkpoint: `variants` row has both `copy_text` and a locally cached image file that opens correctly.

**Fase 4 — Approval dashboard + webhooks**
- Create `dashboard/app.py`, `dashboard/templates/review.html`, `dashboard/static/`, `dashboard/requirements.txt` (Flask, psycopg2-binary, requests, flask-httpauth, python-dotenv), `dashboard/Dockerfile`.
- Flask reads Postgres (read-only) for the queue view, serves cached images, and relays Approve/Reject clicks server-side to n8n webhooks with the shared token.
- Build `reject` webhook workflow in n8n with Header Auth.
- Add `dashboard` service to compose.
- Checkpoint: from another device on wifi (or via the ngrok+OAuth URL), the pending pair appears within 30s; Reject produces a new pair without a new Sheet row; Approve flips `items.status` to `approved` (publish still stubbed).

**Fase 5 — Publishing (Upload-Post) + mark published**
- Build `approve-and-publish` workflow: 4 parallel Upload-Post HTTP branches with `Continue on Fail`, `publications` inserts, `published`/`partial`/`failed` status logic, Sheet status update.
- Export `n8n/workflows/approve-and-publish.json`.
- Checkpoint: approving a test item produces 4 `publications` rows; if one network fails, item shows `partial` with that network flagged in the dashboard.

**Fase 6 — Rejection loop verification**
- No new files. Test: reject twice on the same item → 3 `variants` rows (attempts 1–3, first two `rejected`, third `pending`), zero `publications` rows, exactly one Sheet row still present.

**README.md** — written last: env vars, compose commands, ngrok OAuth setup, shared-token generation, per-phase manual test script.

## Risks flagged (informational, not blocking)

- **Upload-Post/TikTok**: confirm Upload-Post's API actually supports image-only posts for TikTok before building Fase 5 — TikTok is primarily video-first and the aggregator's per-network field requirements may differ from a single unified payload.
- **10 posts/month free tier**: 4 networks per approval ≈ 2–3 approved items/month before hitting the cap — worth keeping in mind during testing cadence.
- **Sheet-as-lock race window**: a small window exists between reading and writing the Sheet status cell if two triggers fire within milliseconds; acceptable for a single-user tool, documented as a caveat rather than engineered away.
- **Per-network manual retry**: v1 has no "republish just the failed network" button for `partial` items — deferred; can be added as workflow 5 (`retry-network`) later if needed.
- **Credentials in n8n vs. exported JSON**: Groq/Upload-Post/Postgres credentials must live in n8n's encrypted credential store, not embedded in exported workflow JSON — verify before committing `n8n/workflows/*.json` to git.

## Verification

- Each phase's checkpoint above is the test for that phase.
- End-to-end: add a real Sheet row → confirm hands-off generation → dashboard shows pair within 30s from a phone on wifi and via the ngrok URL → approve → confirm posts appear (or fail clearly) on all 4 networks → reject on a fresh row twice → confirm 2 distinct variants, no publish, one Sheet row.
