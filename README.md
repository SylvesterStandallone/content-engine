# Content Pipeline

A local, mostly-unattended content pipeline: a pending row in a Google Sheet triggers AI generation of an image + copy via n8n, a human approves/rejects the pair from a Flask dashboard, and approval publishes to TikTok/X/Facebook/Instagram via Upload-Post. Rejection generates a new variant for the same item without touching the Sheet row or publishing anything.

See `PLAN.md` for the full design rationale. See `CLAUDE.md` for architecture notes aimed at an AI coding assistant working in this repo — it also documents where the running system diverges from `PLAN.md` and why.

## Topology note (read this first)

n8n in this deployment is **not** the `n8n` service in `docker-compose.yml` — it's a separate, standalone `n8n` Docker container (`docker run`, not compose) that the owner already uses for other projects, exposed publicly via an existing ngrok tunnel. It was manually joined to this project's Docker network (`docker network connect content-engine_pipeline_net n8n`) so it can reach `postgres` and `dashboard` by hostname. Only `postgres` and `dashboard` run via this repo's `docker-compose.yml`.

Practical implications:
- The n8n container has no volume for cached images, and its environment variables are not the ones in `.env` (no `CRON_ENABLED`, no API keys as container env). Two consequences documented below: images are stored via the dashboard's HTTP API instead of a shared volume, and the cron toggle is a node enable/disable in the n8n UI instead of an env var.
- If you later migrate to running n8n via `docker compose up -d n8n` instead, the compose file already wires `CRON_ENABLED` etc. as real env vars — the workflows would need their Google Sheets/env-dependent parts re-pointed accordingly. Not done as part of this build.

## Running the stack

```
docker compose up -d postgres dashboard   # this project's services
docker compose logs -f dashboard          # tail one service
docker compose restart dashboard          # after editing dashboard/app.py (no hot reload)
```

- Dashboard: `http://localhost:5000` (HTTP Basic Auth — `DASHBOARD_USER` / `DASHBOARD_PASS` in `.env`)
- n8n UI: `http://localhost:5678` (pre-existing instance, not started by this repo)
- Postgres has no published port — reachable only from containers on `pipeline_net`.
- Copy `.env.example` to `.env` and fill in real values before first run. Never commit `.env`.

## Environment variables (`.env`)

| Var | Used by | Notes |
|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | postgres, dashboard | Password is only applied at first `initdb`; changing it later requires `ALTER ROLE` or a volume reset. |
| `DASHBOARD_USER` / `DASHBOARD_PASS` | dashboard | HTTP Basic Auth for the review UI. |
| `PIPELINE_SHARED_TOKEN` | dashboard, n8n | Sent as `X-Pipeline-Token`. Authenticates: dashboard → n8n webhooks (approve/reject/generate-next), n8n → dashboard's `/internal/store-image` and `/images/<file>`. Must match the `Pipeline Shared Token` credential in n8n. |
| `GROQ_API_KEY` | n8n (Groq credential, set manually in n8n UI) | Primary copy generator. Falls back to Ollama on failure. |
| `HUGGINGFACE_API_KEY` | n8n (HuggingFace credential) | Fallback image generator, used only if Pollinations fails. |
| `UPLOAD_POST_API_KEY` | n8n (Upload-Post credential) | Required for `approve-and-publish` to actually post. |
| `CRON_ENABLED`, `N8N_*` | only relevant if n8n is later run via this repo's compose | See topology note above — currently unused by the live n8n instance. |

## n8n workflows

Exported to `n8n/workflows/*.json` and already created live in the running n8n instance:

| Workflow | Trigger | Status |
|---|---|---|
| `generate-variant-core` | sub-workflow only (Execute Workflow) | **Active, tested end-to-end** with real Ollama + Pollinations calls. |
| `reject` | `POST /webhook/reject` (Header Auth) | **Active, tested end-to-end.** |
| `generate` | Schedule Trigger (disabled by default) + `POST /webhook/generate-next` (Header Auth) | Built, but cannot activate until the Google Sheet is connected (see below). |
| `approve-and-publish` | `POST /webhook/approve` (Header Auth) | Built, but cannot activate until the Google Sheet is connected. |

To toggle cron polling: open `generate` in the n8n UI and enable/disable the **Schedule Trigger (disabled by default)** node directly (this replaces the `CRON_ENABLED` env-var gate from `PLAN.md`, since the live n8n container doesn't receive that env var — see topology note).

## One-time manual setup still required

### 1. Google Sheet

Not created yet. Create a Sheet with a header row and these exact column names:

```
row_id | topic | status
```

Data starts at row 2. `row_id` must be a stable, unique value per row (e.g. a short manually-entered id) — it's how workflows match rows for updates. `status` starts as `pending` for each new row you want processed.

Then in the n8n UI:
1. Create a **Google Sheets OAuth2** credential (Settings → Credentials → New → Google Sheets OAuth2 API) and complete the OAuth consent flow — this needs a browser and can't be automated.
2. Open `generate` and `approve-and-publish`. On each Google Sheets node (`Read Pending Row`, `Claim Row (Sheet -> generating)`, `Mark Awaiting Review (Sheet)`, `Update Sheet Status`), set the real spreadsheet + sheet, and attach the credential you just created (currently a placeholder `REPLACE_ME` credential id).
3. Activate both workflows (they'll refuse to activate until the Sheets nodes are valid — n8n will tell you exactly which nodes still have issues).

### 2. API keys

Fill in real values and update the corresponding n8n credential (Settings → Credentials):

- `GROQ_API_KEY` → **Groq API (header auth)** credential, value `Bearer <key>`.
- `HUGGINGFACE_API_KEY` → **HuggingFace API (header auth)** credential, value `Bearer <key>`.
- `UPLOAD_POST_API_KEY` → **Upload-Post API (header auth)** credential, value `Apikey <key>` (Upload-Post uses `Apikey`, not `Bearer` — confirmed against their docs).

Without a real Groq key, copy generation always falls back to Ollama (this works — verified live — but Groq is meant to be primary per the design).

### 3. Upload-Post account

Upload-Post requires you to connect your TikTok/X/Facebook/Instagram accounts to a **profile** on their own dashboard first — the API only ever references that profile by username, it doesn't do platform auth itself. Once you have a profile:
- Open `approve-and-publish` in n8n, find the **Config** node, and replace `REPLACE_WITH_UPLOAD_POST_PROFILE` with your real profile username.

**Known open risk** (carried over from `PLAN.md`, still unconfirmed): Upload-Post's docs don't state whether TikTok supports photo-only posts via `/api/upload_photos` (their examples are video-first). If TikTok rejects a photo post, the existing per-network `Continue on Fail` + `partial` status handling will surface it cleanly rather than failing the whole publish — but it's worth testing a single approval early to find out.

## Architecture as actually built

- **Postgres is still the single source of truth** — unchanged from `PLAN.md`.
- **Images are stored via the dashboard's HTTP API, not a shared Docker volume.** `PLAN.md` assumed n8n and the dashboard would share an `image_cache` volume. That's not possible here (see topology note), and it turned out to be unnecessary: `generate-variant-core` POSTs the generated image bytes to a new dashboard endpoint (`POST /internal/store-image`, multipart, token-authed) right after generation; the dashboard writes them to its own normal compose-managed volume. At publish time, `approve-and-publish` fetches the bytes back via `GET /images/<file>` (now accepts the shared token as an alternative to Basic Auth) and re-uploads them to Upload-Post. This is arguably cleaner than the original plan — image storage belongs entirely to the one container we fully control via compose.
- **Upload-Post integration**: confirmed for real against their docs — `POST https://api.upload-post.com/api/upload_photos`, `Authorization: Apikey <key>`, `multipart/form-data` with `user`, `platform[]`, `file`, `caption`. Images are fetched as real bytes and uploaded as a file, not linked by URL (their API doesn't support URL-based photo posts, unlike what `PLAN.md` assumed).
- **Concurrency guards**: implemented exactly as specified — `UPDATE ... WHERE decision='pending'` with `alwaysOutputData: true` so a 0-row update can still be detected downstream and turned into a `409` response, verified against both a real double-reject-style race and a nonexistent variant id.
- **Publications rollup**: `approve-and-publish` fires all 4 networks in parallel (`Continue on Fail` per branch), records one `publications` row per network (verified each network gets its own correctly-parameterized insert via Postgres's `queryBatching: independently`), then computes `published` / `partial` / `failed` from the actual per-network results.

## Manual test script

1. **Postgres/network sanity**: `docker exec content-engine-postgres-1 psql -U pipeline -d content_engine -c "SELECT 1"` should succeed; `docker exec n8n wget -qO- http://dashboard:5000/` should return `401 Unauthorized` (proves n8n can reach the dashboard by hostname).
2. **Reject loop** (works today, no Sheet/API keys needed beyond what's already configured):
   ```sql
   INSERT INTO items (sheet_row_id, topic, status) VALUES ('manual-test', 'a cozy coffee shop', 'awaiting_review') RETURNING id;
   INSERT INTO variants (item_id, attempt_no, copy_text, image_path, image_source, text_source, decision)
     VALUES (<id>, 1, 'placeholder', 'placeholder.png', 'pollinations', 'groq', 'pending') RETURNING id;
   ```
   ```
   curl -X POST http://localhost:5678/webhook/reject -H "X-Pipeline-Token: $PIPELINE_SHARED_TOKEN" -H "Content-Type: application/json" -d '{"item_id": <id>, "variant_id": <variant id>}'
   ```
   Expect `{"status":"ok", ...}` and a second `variants` row with real generated `copy_text` and a real image file under the dashboard's `image_cache`.
3. **Dashboard**: open `http://localhost:5000`, log in with `DASHBOARD_USER`/`DASHBOARD_PASS`, confirm the queue renders and Approve/Reject buttons call through to n8n.
4. **Generate** (after Google Sheet setup): add a `pending` row to the Sheet, `POST /webhook/generate-next` with the shared token, confirm the row claims, generates, and lands as `awaiting_review` in both the Sheet and `items`.
5. **Approve-and-publish** (after Upload-Post setup): approve a variant from the dashboard, confirm 4 `publications` rows appear and `items.status` rolls up correctly.
6. **Double-decide race guard**: call `/webhook/reject` or `/webhook/approve` twice with the same `variant_id` — second call should return `409`.
