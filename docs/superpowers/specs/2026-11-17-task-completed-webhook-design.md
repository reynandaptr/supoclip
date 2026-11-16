# Task-completed webhook — design

Status: approved in conversation 2026-11-17 (fork `reynandaptr/supoclip`, branch `feat/task-completed-webhook`).

**Scope: the user's fork only**, self-hosted by the user. Not intended for upstream or the hosted
service, so there is no rollout gate or feature flag, and defaults suit a single self-hosted deployment.

## Intent

The user creates tasks by hand in the SupoClip web app and does **not** want SupoClip's built-in social
publishing. A separate service (designed next, outside this repo) will upload clips to YouTube and TikTok,
possibly much later. SupoClip's job is to tell that service, once, that a task's clips are all done and
where to fetch each one.

Success: the moment a task becomes `completed`, a signed HTTP POST reaches the user's configured URL with
every clip and a download link that still works hours or days later.

## Goals

- One webhook per user (URL + signing secret), applied to every task the user creates, however it was
  created (web, iOS, API key, MCP).
- Fire exactly when a task transitions to `completed` (= every clip rendered and saved).
- Durable delivery: survives worker restarts and receiver downtime; retried with backoff for ~24 h.
- Payload carries stable download links (no 15-minute expiry).
- Receiver can verify the request came from SupoClip.

## Non-goals

- Failure / cancellation events. A task that ends `error` or `cancelled` sends nothing.
- Per-task webhook URLs, multiple webhooks per user, event filters.
- Webhooks for clip-studio edits, exports, splits, merges, trims, or regenerations.
- A delivery-log UI. (Delivery state lives in the DB and logs; a "send test" button is in scope.)
- Feature flag / rollout gate, iOS app support, feature tour, real translations (fork-only).
- Uploading anywhere. That is the separate service.

## Anti-goals

- Must not change the highlights invariant (prompts, cache keys, job args, render path) or slow down the
  render job. The completion path only inserts a row.
- Must not let a slow or dead receiver block or fail a task.
- Must not become an SSRF probe of internal addresses when private targets are turned off.
- Must not leak the SupoClip API key or long-lived signed bucket URLs in the payload.

## Constraints from the codebase

- **Single completion point:** `TaskService.process_task` sets `completed` at
  `backend/src/services/task_service.py:~947` (`update_task_status(..., "completed", expected_statuses=["processing"])`),
  then calls `_send_completion_notification_if_needed`. No other code path sets a task `completed`
  (checked: timeouts, cancel, resume and requeue only set `error`/`cancelled`/`queued`). Resume re-runs
  `process_task`, so a resumed task that completes passes through the same point.
- **No shared disk** between API and workers: all webhook state goes in PostgreSQL.
- **Migrations:** plain SQL files in `backend/src/migrations/sql/`, applied in order at API startup
  (`database.py`). Also mirror new tables in `init.sql`.
- **Crons:** `backend/src/workers/cron_worker.py`, `scheduled(fn, ...)` with stable job ids.
- **Outbox precedent:** `media_cleanup.py` claims rows with `FOR UPDATE SKIP LOCKED` and `next_attempt_at`.
- **Signing precedent:** `cancellation_reviews/delivery.py` signs `timestamp + "." + body` with HMAC-SHA256.
- **Secret at rest precedent:** `runtime_settings.encrypt_setting_value` / decrypt.
- **Auth for settings routes:** `/api-keys/*` resolve the user from signed session headers only
  (`get_authenticated_user_id`), never from an API key. Webhook settings follow the same rule.
- **Frontend proxy pattern:** `frontend/src/app/api/api-keys/route.ts` → `fetchBackend(...)`.
- Backend has no public-API-URL setting today (`app_base_url` is the web app).

## Approaches considered

1. **Separate bridge service polling `GET /tasks/`** — no SupoClip change. Rejected by the user in favour of
   SupoClip pushing the event itself from their fork.
2. **Fire the HTTP request inline at completion** — simplest, but a slow receiver delays the job and a
   down receiver loses the event. Rejected.
3. **Outbox row at completion + cron sender (chosen)** — durable, retries, no impact on render time,
   matches the existing outbox pattern.

Download links: signed bucket URLs (15 min, max 1 h) were rejected because the receiver uploads later.
The payload uses the API's own clip URL, which the receiver opens with the user's API key; the API then
redirects to a fresh signed URL.

## Design

### Data

Migration `backend/src/migrations/sql/20261117_0001_user_webhooks.sql` (+ `init.sql`):

```sql
CREATE TABLE user_webhooks (
    user_id        VARCHAR(36) PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    url            TEXT NOT NULL,
    secret_enc     TEXT NOT NULL,              -- encrypted with encrypt_setting_value
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    enabled_since  TIMESTAMPTZ NOT NULL DEFAULT now(),  -- reset on create and on disabled→enabled
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE webhook_deliveries (
    id               VARCHAR(36) PRIMARY KEY,  -- also sent as X-SupoClip-Delivery
    user_id          VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_id          VARCHAR(36) REFERENCES tasks(id) ON DELETE SET NULL,
    event            VARCHAR(40) NOT NULL,     -- 'task.completed' | 'webhook.test'
    url              TEXT NOT NULL,            -- snapshot at enqueue
    payload          JSONB NOT NULL,           -- snapshot at enqueue
    task_completed_at TIMESTAMPTZ,             -- tasks.completed_at this delivery announces (NULL for tests)
    status           VARCHAR(16) NOT NULL DEFAULT 'pending',  -- pending | delivered | failed | cancelled
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_status_code INTEGER,
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at     TIMESTAMPTZ
);
CREATE INDEX idx_webhook_deliveries_due ON webhook_deliveries (next_attempt_at) WHERE status = 'pending';
-- One delivery per completion, whichever path (fast enqueue or reconciler) gets there first.
CREATE UNIQUE INDEX uq_webhook_deliveries_completion
    ON webhook_deliveries (task_id, event, task_completed_at) WHERE task_completed_at IS NOT NULL;
```

**Delete / disable (builder-check call):** `DELETE /webhooks/` and setting `enabled=false` mark that user's
`pending` deliveries `cancelled` in the same transaction. Re-enabling does not revive them and does not
backfill tasks completed while disabled (`enabled_since` moves forward). Unsigned sends never happen.

The secret is generated server-side (`whsec_` + 32 random bytes, urlsafe). Rotating it replaces it.
The secret is signed with whatever is current at send time (not snapshotted), so a rotation applies to
pending retries. Retries also use the user's current URL (the row's `url` is kept as history), not the
one snapshotted at enqueue.

### Enqueue (worker)

Right after the `completed` status update and runtime-metadata write in `process_task`, next to the
completion email, call `WebhookService.enqueue_task_completed(task_id)`:

- Look up the task's user's `user_webhooks` row; if missing or disabled, do nothing.
- Build the payload (below) from the task and its clips, insert one `webhook_deliveries` row.
- Wrapped so any exception is logged and swallowed: a webhook problem never fails the task.

Every completion enqueues a new row with a new delivery id, so a task resumed and completed again sends
again. The receiver de-duplicates by `delivery_id` (and may by `task.id`).

The insert is `ON CONFLICT DO NOTHING` on `(task_id, event, task_completed_at)`.

**Reconciler (builder-check call, at-least-once):** the status update commits on its own before the
enqueue, so a worker crash in between would lose the event. The `deliver_webhooks` cron therefore also
enqueues, with the same idempotent insert, any task that is `completed`, has `completed_at` within the last
24 h and `>= user_webhooks.enabled_since` (webhook enabled), and has no delivery for that `completed_at`.
`render_task` and `process_task` are otherwise untouched. Accepted gap: a crash in the milliseconds between
the status commit and the `completed_at` write leaves `completed_at` NULL, and that task is not announced.

### Payload (`task.completed`)

```json
{
  "event": "task.completed",
  "delivery_id": "uuid",
  "created_at": "2026-11-17T10:00:00Z",
  "task": {
    "id": "uuid",
    "status": "completed",
    "source_title": "…",
    "source_url": "https://youtube.com/…",
    "source_type": "youtube",
    "workflow": "highlights",
    "completed_at": "…",
    "clips_count": 6,
    "url": "https://app.example.com/tasks/<id>"
  },
  "clips": [
    {
      "id": "uuid",
      "order": 1,
      "hook_title": "…",
      "text": "transcript of the clip",
      "start_time": "01:23",
      "end_time": "02:05",
      "duration": 42.0,
      "virality_score": 87,
      "workflow_role": null,
      "part_number": null,
      "download_url": "https://api.example.com/tasks/<task>/clips/<clip>/file?download=true"
    }
  ]
}
```

`download_url` needs the public API base: new env `PUBLIC_API_URL` (e.g. `https://api.supoclip.com`,
default `http://localhost:8000`). The receiver fetches it with `Authorization: Bearer sk_…` and follows
the 307 redirect. Clips are ordered by `clip_order`. Exact source-title/URL fields come from whatever the
task row already exposes (builder maps them; omit a field rather than invent one).

### Delivery (cron)

New cron `deliver_webhooks` on the cron worker, every 15 s (`second={5,20,35,50}`, timeout 120 s):

- Claim due rows (`status='pending' AND next_attempt_at <= now()`) with `FOR UPDATE SKIP LOCKED`, up to
  ~20 per run, sending concurrently with a small limit.
- `POST` JSON body, 10 s timeout, **no redirects followed**, `User-Agent: SupoClip-Webhooks/1`.
- Headers: `Content-Type: application/json`, `X-SupoClip-Event`, `X-SupoClip-Delivery`,
  `X-SupoClip-Timestamp` (unix seconds), `X-SupoClip-Signature: v1=<hex HMAC-SHA256(secret, "<ts>.<body>")>`.
- Any 2xx → `delivered`. Anything else (non-2xx, timeout, connection or validation error) → reschedule.
- Backoff after attempt n: 1 m, 5 m, 15 m, 1 h, 3 h, 6 h, 12 h (≈ 23 h total), then `failed`. Store
  `last_status_code` / `last_error` (truncated, no response body beyond ~500 chars).
- Prune `delivered`/`failed` rows older than 30 days in the same job.

### URL safety

Validated on save **and** before every send:

- `WEBHOOK_ALLOW_PRIVATE_TARGETS` defaults to **true** (fork-only, self-hosted: the receiver may run on the
  same machine or Docker network). With it true, `http` and `https` are both allowed and any address is
  accepted except unspecified/multicast.
- Set to `false`: `https` required; resolve the host and reject loopback, private, link-local, multicast,
  reserved and unspecified addresses (IPv4 and IPv6).
- Connect to the address that was validated (pin the resolved IP) to avoid DNS rebinding.
- Max URL length 2048.

### API (session auth only, like `/api-keys`)

- `GET /webhooks/` → `{ url, enabled, secret_hint (last 4), created_at, updated_at, last_delivery: {status, attempts, last_status_code, created_at} | null }` or `{ configured: false }`.
- `PUT /webhooks/` `{ url, enabled }` → creates (generating a secret, returned **once** in this
  response) or updates.
- `POST /webhooks/rotate-secret` → new secret, returned once.
- `DELETE /webhooks/` → removes the config (pending deliveries are left to finish/fail).
- `POST /webhooks/test` → enqueues a `webhook.test` delivery with a small sample payload; returns its id.

Router registered in `main.py`; Next.js proxies under `frontend/src/app/api/webhooks/…` using `fetchBackend`.

### Frontend

A "Webhook" card on `/settings/api-keys` (below the keys list): URL input, enabled switch, save, the
secret shown once after create/rotate with a copy button, "Rotate secret", "Send test", "Remove", and the
last delivery's status line. The page already uses i18n messages: add the card's strings in English and copy
the English text into the other locales (no translation work). No feature tour (fork-only). A screenshot in
the PR.

### Docs

- `docs/webhooks.md`: setup, payload, headers, signature verification snippet (Python + Node), retries,
  dedupe, fetching a clip with the API key.
- CLAUDE.md: endpoints list, env vars (`PUBLIC_API_URL`, `WEBHOOK_ALLOW_PRIVATE_TARGETS`), cron list.

### Tests

- Unit: payload builder; signature; backoff schedule; URL validator (public ok; private/loopback/
  link-local/IPv6 rejected; allow flag); enqueue no-op without config; enqueue failure does not raise.
- Unit: cron sender with a fake HTTP transport: 2xx → delivered; 500/timeout → rescheduled; final attempt
  → failed; redirect not followed.
- Integration: `process_task` happy path enqueues exactly one delivery; settings routes reject API-key auth.
- Frontend: vitest for the settings card (create shows secret once, test button).

## Left to the builder

- Module layout (suggested: `backend/src/webhooks/` with `service.py`, `delivery.py`, `url_safety.py`,
  plus `repositories/webhook_repository.py` and `api/routes/webhooks.py`).
- Exact HTTP client (httpx is already a dependency) and how IP pinning is done with it.
- Exact field mapping for `task.source_*`.
- UI layout details within the existing settings page style.
