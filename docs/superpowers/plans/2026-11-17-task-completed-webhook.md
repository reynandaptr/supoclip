# Task-completed Webhook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a task becomes `completed`, SupoClip POSTs one signed `task.completed` webhook (every clip + a non-expiring API download link) to the user's configured URL, durably, with retries.

**Architecture:** Per-user config in `user_webhooks`; an outbox table `webhook_deliveries` filled at completion (fast path in `process_task`) and by a reconciler; a cron on the cron worker claims due rows, signs them with HMAC and POSTs them with backoff. Settings live behind session-auth `/webhooks/*` routes and a card on `/settings/api-keys`.

**Tech Stack:** FastAPI, raw SQL via SQLAlchemy `text()`, arq cron, httpx, PostgreSQL 15; Next.js 15 + next-intl + ShadCN; pytest, vitest.

**Spec:** `docs/superpowers/specs/2026-11-17-task-completed-webhook-design.md`

## Global Constraints

- Fork-only: no feature flag, no rollout gate, no feature tour, no iOS work.
- Event names: `task.completed`, `webhook.test`. Delivery statuses: `pending | delivered | failed | cancelled`.
- Headers: `Content-Type: application/json`, `User-Agent: SupoClip-Webhooks/1`, `X-SupoClip-Event`, `X-SupoClip-Delivery`, `X-SupoClip-Timestamp` (unix seconds), `X-SupoClip-Signature: v1=<hex HMAC-SHA256(secret, "<timestamp>.<body>")>`.
- Secret format: `whsec_` + `secrets.token_urlsafe(32)`; stored encrypted with `runtime_settings.encrypt_setting_value`; signed with the **current** secret at send time.
- Retry delays after failed attempt n (n=1..7): 60, 300, 900, 3600, 10800, 21600, 43200 s; the 8th failure → `failed`.
- HTTP: 10 s timeout, `follow_redirects=False`, any 2xx = delivered, everything else = retry.
- `download_url` = `{PUBLIC_API_URL}/tasks/{task_id}/clips/{clip_id}/file?download=true`; `PUBLIC_API_URL` default `http://localhost:8000`, trailing `/` stripped.
- `WEBHOOK_ALLOW_PRIVATE_TARGETS` default **true**. True: `http`/`https`, any resolved address except unspecified/multicast. False: `https` only; reject loopback, private, link-local, multicast, reserved, unspecified (v4 + v6). URL max 2048 chars.
- Reconciler window: tasks `completed` with `completed_at` in the last 24 h and `>= enabled_since`.
- Prune `delivered`/`failed`/`cancelled` rows older than 30 days.
- `/webhooks/*` resolve the user only from signed session headers (`get_authenticated_user_id`), never an API key.
- A webhook problem never fails or delays a task: the fast-path enqueue is wrapped and only logs.
- Card strings: English, copied verbatim into every locale under `frontend/messages/*/settings.json`.

## Review Focus

- Receiver answers a 3xx redirect → not followed, counted as a failed attempt (Task 4 test `test_redirect_is_not_followed`).
- Receiver hangs past 10 s → attempt fails and is rescheduled; the cron run still returns (Task 4 test `test_timeout_reschedules`).
- With `WEBHOOK_ALLOW_PRIVATE_TARGETS=false`, a saved host that later resolves to `10.x`/`127.x`/`::1` → nothing is sent, attempt fails (Task 4 test `test_blocked_resolution_is_never_sent`).
- Task completes while the webhook is disabled, then the user re-enables it → that task is never announced (Task 1 test `test_reconciler_skips_completions_before_enabled_since`).
- Task resumed and completed again (new `completed_at`) → a second delivery; same `completed_at` seen twice → one row (Task 1 test `test_insert_delivery_is_idempotent_per_completion`).

---

### Task 1: Config, schema and repository

**Files:**
- Modify: `backend/src/config.py` (near the cancellation-review settings, ~line 246)
- Create: `backend/src/migrations/sql/20261117_0001_user_webhooks.sql`
- Modify: `init.sql` (append the same tables/indexes after `api_keys`)
- Create: `backend/src/repositories/webhook_repository.py`
- Test: `backend/tests/integration/test_webhook_repository.py`

**Interfaces:**
- Produces: `Config.public_api_url: str`, `Config.webhook_allow_private_targets: bool`.
- Produces `class WebhookRepository` (all `@staticmethod async`, first arg `db: AsyncSession`, no commits inside — callers commit):
  - `get_config(db, user_id) -> dict | None` keys: `user_id, url, secret_enc, enabled, enabled_since, created_at, updated_at`
  - `upsert_config(db, user_id, *, url: str, enabled: bool, secret_enc: str | None) -> dict` — insert (secret_enc required) or update url/enabled (`secret_enc=None` keeps the secret). `enabled_since = now()` on insert and on false→true. On true→false, calls `cancel_pending`.
  - `set_secret(db, user_id, secret_enc: str) -> bool`
  - `delete_config(db, user_id) -> bool` — also `cancel_pending`.
  - `cancel_pending(db, user_id) -> int`
  - `insert_delivery(db, *, delivery_id: str, user_id: str, task_id: str | None, event: str, url: str, payload: dict, task_completed_at: datetime | None) -> bool` — `ON CONFLICT DO NOTHING`; True if inserted.
  - `get_task_for_payload(db, task_id) -> dict | None` keys: `id, user_id, status, workflow, completed_at, source_title, source_url, source_type` (join `sources` on `tasks.source_id`).
  - `get_clips_for_payload(db, task_id) -> list[dict]` ordered by `clip_order`; keys: `id, clip_order, hook_title, text, start_time, end_time, duration, virality_score, workflow_role, part_number`.
  - `find_unannounced_completions(db, *, window: timedelta = timedelta(hours=24), limit: int = 50) -> list[dict]` keys `task_id, user_id, completed_at`.
  - `claim_due(db, *, limit: int = 20, lease: timedelta = timedelta(minutes=2)) -> list[dict]` — `FOR UPDATE SKIP LOCKED`, bumps `next_attempt_at` by `lease`, returns `id, user_id, event, url, payload, attempts`.
  - `mark_delivered(db, delivery_id, *, status_code: int) -> None`
  - `mark_attempt_failed(db, delivery_id, *, attempts: int, status_code: int | None, error: str, retry_at: datetime | None) -> None` — `retry_at=None` → status `failed`; `last_error` truncated to 500 chars.
  - `prune(db, *, older_than: timedelta = timedelta(days=30)) -> int`
  - `mark_cancelled(db, delivery_id) -> None`
  - `last_delivery(db, user_id) -> dict | None` keys `status, attempts, last_status_code, created_at` — newest row of any event.

- [ ] **Step 1: Add config.** `PUBLIC_API_URL` via `_get_optional_env` (default `http://localhost:8000`, `rstrip("/")`); `WEBHOOK_ALLOW_PRIVATE_TARGETS` via `_get_bool_env(..., True)`.
- [ ] **Step 2: Write the migration** exactly as the spec's Data section (`user_webhooks` with `enabled_since`; `webhook_deliveries` with `task_completed_at`, status comment incl. `cancelled`; the due index; the unique partial index `uq_webhook_deliveries_completion`). Use `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`. Mirror into `init.sql`.
- [ ] **Step 3: Write failing integration tests** in `tests/integration/test_webhook_repository.py` using `db_session` and `tests.fixtures.factories.create_user/create_source/create_task/create_clip`:
  - `test_upsert_then_disable_cancels_pending` — insert config, insert a pending delivery, `upsert_config(enabled=False)` → delivery status `cancelled`.
  - `test_reenable_moves_enabled_since` — disable then enable → `enabled_since` later than before.
  - `test_delete_cancels_pending` — same as above via `delete_config`; `get_config` → None.
  - `test_insert_delivery_is_idempotent_per_completion` — same `(task_id, "task.completed", completed_at)` twice → second returns False; a different `completed_at` → True.
  - `test_reconciler_skips_completions_before_enabled_since` — task completed 1 h ago, config `enabled_since` = now → `find_unannounced_completions` returns nothing; task completed after `enabled_since` → returned; after `insert_delivery` for it → not returned.
  - `test_reconciler_ignores_disabled_and_old` — disabled config or `completed_at` 25 h ago → not returned.
  - `test_claim_due_leases_rows` — two claims back-to-back: second returns nothing for the same row.
  - `test_mark_attempt_failed_terminal` — `retry_at=None` → `failed`, `last_error` length ≤ 500.
  - `test_payload_queries` — `get_task_for_payload` returns source title/url/type; clips come back in `clip_order`.
- [ ] **Step 4: Run** `cd backend && .venv/bin/pytest --no-cov tests/integration/test_webhook_repository.py -v` → FAIL (module missing).
- [ ] **Step 5: Implement** `WebhookRepository`. `find_unannounced_completions` is one query: `tasks t JOIN user_webhooks w ON w.user_id = t.user_id AND w.enabled WHERE t.status='completed' AND t.completed_at IS NOT NULL AND t.completed_at > now() - :window AND t.completed_at >= w.enabled_since AND NOT EXISTS (SELECT 1 FROM webhook_deliveries d WHERE d.task_id = t.id AND d.event='task.completed' AND d.task_completed_at = t.completed_at)`.
- [ ] **Step 6: Run** the same command → PASS.
- [ ] **Step 7: Commit** `feat(webhooks): schema, config and repository`.

### Task 2: URL safety, signing, payload

**Files:**
- Create: `backend/src/webhooks/__init__.py` (empty), `backend/src/webhooks/url_safety.py`, `backend/src/webhooks/signing.py`, `backend/src/webhooks/payload.py`
- Test: `backend/tests/unit/test_webhook_units.py`

**Interfaces:**
- Produces (`url_safety.py`): `class WebhookUrlError(ValueError)`; `@dataclass(frozen=True) class ResolvedTarget: url: str; host: str; ip: str`; `def validate_webhook_url(url: str, *, allow_private: bool) -> str` (returns stripped URL; syntax/scheme/length only); `async def resolve_target(url: str, *, allow_private: bool) -> ResolvedTarget` (resolves with `asyncio.get_running_loop().getaddrinfo`, checks **every** address, returns the first; raises `WebhookUrlError`).
- Produces (`signing.py`): `def generate_secret() -> str`; `def sign(secret: str, timestamp: int, body: bytes) -> str` (returns `"v1=" + hex`); `def delivery_headers(*, secret: str, event: str, delivery_id: str, timestamp: int, body: bytes) -> dict[str, str]` (all Global-Constraints headers).
- Produces (`payload.py`): `def build_task_completed_payload(*, delivery_id: str, created_at: datetime, task: Mapping, clips: Sequence[Mapping], public_api_url: str, app_base_url: str) -> dict` (shape exactly as the spec's Payload section; `clips_count = len(clips)`; `order` = `clip_order`; ISO-8601 UTC `Z` timestamps); `def build_test_payload(*, delivery_id: str, created_at: datetime) -> dict` (`event: "webhook.test"`, `delivery_id`, `created_at`, `message: "SupoClip webhook test"`).

- [ ] **Step 1: Write failing tests** in `tests/unit/test_webhook_units.py`:
  - `test_sign_matches_reference` — `sign("whsec_x", 1700000000, b'{"a":1}') == "v1=" + hmac.new(b"whsec_x", b'1700000000.{"a":1}', sha256).hexdigest()`.
  - `test_headers_complete` — keys equal the 6 Global-Constraints headers; `X-SupoClip-Timestamp == "1700000000"`.
  - `test_generate_secret_prefix_and_entropy` — starts with `whsec_`, length ≥ 46, two calls differ.
  - `test_validate_rejects_bad_syntax` — `ftp://x`, `https://`, 2049-char URL, `"not a url"` raise; with `allow_private=False`, `http://example.com` raises; with `True` it passes.
  - `test_resolve_blocks_private_when_disallowed` (parametrize `127.0.0.1`, `10.1.2.3`, `169.254.1.1`, `::1`, `fd00::1`, `0.0.0.0`; monkeypatch `getaddrinfo`) → raises with `allow_private=False`.
  - `test_resolve_allows_private_when_allowed` — `10.1.2.3` ok, `0.0.0.0` and `224.0.0.1` still raise.
  - `test_resolve_checks_every_address` — getaddrinfo returns `[93.184.216.34, 10.0.0.1]` with `allow_private=False` → raises.
  - `test_payload_shape` — two clips (one with `hook_title=None`, `workflow_role=None`) → `download_url == "https://api.x/tasks/T/clips/C1/file?download=true"`, `task.url == "https://app.x/tasks/T"`, `clips_count == 2`, order preserved, `None` passed through.
- [ ] **Step 2: Run** `.venv/bin/pytest --no-cov tests/unit/test_webhook_units.py -v` → FAIL.
- [ ] **Step 3: Implement** the three modules (`ipaddress` for the checks: `is_loopback/is_private/is_link_local/is_multicast/is_reserved/is_unspecified`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(webhooks): url safety, signing and payload`.

### Task 3: WebhookService

**Files:**
- Create: `backend/src/webhooks/service.py`
- Test: `backend/tests/integration/test_webhook_service.py`

**Interfaces:**
- Consumes: Task 1 repository, Task 2 modules, `runtime_settings.encrypt_setting_value/decrypt_setting_value`.
- Produces `class WebhookService(db: AsyncSession, config: Config | None = None)` (commits its own writes):
  - `async get_settings(user_id) -> dict` → `{"configured": False}` or `{"configured": True, "url", "enabled", "secret_hint" (last 4 chars), "created_at", "updated_at", "last_delivery": dict | None}`
  - `async save(user_id, *, url: str, enabled: bool) -> dict` → `get_settings` result plus `"secret"` only when newly created; validates with `validate_webhook_url` (raises `WebhookUrlError`).
  - `async rotate_secret(user_id) -> str | None` (None when not configured)
  - `async delete(user_id) -> bool`
  - `async send_test(user_id) -> str | None` (delivery id; None when not configured or disabled)
  - `async enqueue_task_completed(task_id: str) -> str | None` — no-op (None) when the task isn't `completed`, has no `completed_at`, the user has no enabled config, or `completed_at < enabled_since`; else builds the payload and `insert_delivery`; returns the id if inserted.
  - `async reconcile() -> int` — `find_unannounced_completions` → `enqueue_task_completed` each; returns count inserted.

- [ ] **Step 1: Write failing tests:** `test_save_creates_secret_once` (first save has `secret`, second doesn't, hint = last 4 of first); `test_save_rejects_invalid_url` (raises `WebhookUrlError`, nothing stored); `test_secret_is_encrypted_at_rest` (raw `secret_enc` ≠ plaintext, decrypts to it); `test_rotate_changes_secret`; `test_enqueue_noop_without_config`; `test_enqueue_builds_payload_from_db` (task + 2 clips → one row, payload `clips` length 2, `url` snapshot = config URL); `test_enqueue_twice_inserts_once`; `test_reconcile_picks_up_missed_completion`; `test_send_test_inserts_webhook_test_event`.
- [ ] **Step 2: Run** `.venv/bin/pytest --no-cov tests/integration/test_webhook_service.py -v` → FAIL.
- [ ] **Step 3: Implement** `WebhookService`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(webhooks): settings and enqueue service`.

### Task 4: Delivery cron

**Files:**
- Create: `backend/src/webhooks/delivery.py`
- Modify: `backend/src/workers/cron_worker.py` (import + `scheduled(deliver_webhooks, second={5, 20, 35, 50}, run_at_startup=True, timeout=120)`)
- Modify: `backend/tests/unit/test_cron_worker.py` (count 8 → 9, add `"cron:deliver_webhooks"`)
- Test: `backend/tests/unit/test_webhook_delivery.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: `BACKOFF_SECONDS = (60, 300, 900, 3600, 10800, 21600, 43200)`; `def next_retry_delay(attempts: int) -> int | None` (`attempts` = attempts made including this one; 1→60 … 7→43200, ≥8→None); `@dataclass class DeliveryResult: ok: bool; status_code: int | None; error: str | None`; `async def send_delivery(row: Mapping, *, secret: str, client: httpx.AsyncClient, allow_private: bool) -> DeliveryResult`; `async def deliver_webhooks(ctx: dict) -> dict` (keys `reconciled, sent, delivered, failed, pruned`).
- `send_delivery`: `resolve_target` → request to the pinned IP: replace the URL host with `ip` (bracketed for IPv6), header `Host: <original host[:port]>`, `extensions={"sni_hostname": host}` for https. Body is `json.dumps(payload, separators=(",", ":"), default=str).encode()`; headers from `delivery_headers`. `WebhookUrlError`, `httpx.HTTPError` → `DeliveryResult(False, None, str(exc))`.
- `deliver_webhooks`: open `AsyncSessionLocal()`; `WebhookService.reconcile()`; `claim_due` + commit; send concurrently (`asyncio.Semaphore(5)`, one shared `httpx.AsyncClient(timeout=10, follow_redirects=False)`); decrypt the user's **current** secret per row (config missing or disabled → `mark_cancelled`, never sent unsigned); record with `mark_delivered` / `mark_attempt_failed(retry_at=now()+delay or None)`; `prune`; commit.

- [ ] **Step 1: Write failing tests** (unit, `httpx.MockTransport`, monkeypatched `getaddrinfo` → `93.184.216.34`): `test_backoff_schedule` (exact table, 8 → None); `test_2xx_is_ok` (also asserts the request carried a valid signature over the exact body, and `Host` = original host); `test_500_is_failure`; `test_redirect_is_not_followed` (302 → `ok False`, `status_code 302`, only one request made); `test_timeout_reschedules` (transport raises `httpx.ReadTimeout` → `ok False`, error mentions timeout); `test_blocked_resolution_is_never_sent` (`allow_private=False`, resolution `10.0.0.1` → transport never called).
- [ ] **Step 2: Run** `.venv/bin/pytest --no-cov tests/unit/test_webhook_delivery.py tests/unit/test_cron_worker.py -v` → FAIL.
- [ ] **Step 3: Implement** `delivery.py`, register the cron, update the cron test.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Add integration test** `tests/integration/test_webhook_delivery_cron.py::test_deliver_webhooks_end_to_end` — config + pending row, monkeypatch `httpx.AsyncClient` transport to a `MockTransport` returning 500 then 204 across two `deliver_webhooks({})` calls (force `next_attempt_at` to now between calls) → row ends `delivered`, `attempts == 2`. Run it → PASS.
- [ ] **Step 6: Commit** `feat(webhooks): delivery cron with retries`.

### Task 5: Fast-path enqueue at completion

**Files:**
- Modify: `backend/src/services/task_service.py` — in `process_task`, after `update_task_runtime_metadata(... completed_at=...)` and **before** `_send_completion_notification_if_needed`.
- Test: `backend/tests/integration/test_webhook_task_completion.py`

**Interfaces:**
- Consumes: `WebhookService(self.db, self.config).enqueue_task_completed(task_id)`.

- [ ] **Step 1: Write failing tests**, modelled on the `process_task` setup in `tests/integration/test_retry_checkpoints.py`: `test_completion_enqueues_one_delivery` (user with enabled webhook; task processed to `completed` → exactly one `task.completed` row whose payload lists the task's clips); `test_enqueue_error_does_not_fail_task` (monkeypatch `WebhookService.enqueue_task_completed` to raise → task still `completed`).
- [ ] **Step 2: Run** `.venv/bin/pytest --no-cov tests/integration/test_webhook_task_completion.py -v` → FAIL.
- [ ] **Step 3: Implement**: `try: await WebhookService(self.db, self.config).enqueue_task_completed(task_id) except Exception: logger.exception("Webhook enqueue failed for task %s", task_id)`.
- [ ] **Step 4: Run** it plus `tests/integration/test_retry_checkpoints.py tests/integration/test_processing_cancellation.py` → PASS.
- [ ] **Step 5: Commit** `feat(webhooks): enqueue when a task completes`.

### Task 6: `/webhooks` API

**Files:**
- Create: `backend/src/api/routes/webhooks.py`
- Modify: `backend/src/main.py` (import and `app.include_router(webhooks_router)` next to `api_keys_router`)
- Test: `backend/tests/integration/test_webhook_routes.py`

**Interfaces:**
- Consumes: `WebhookService`; `get_authenticated_user_id(request, get_config())` as in `api/routes/api_keys.py`.
- Produces routes (prefix `/webhooks`): `GET /` → `get_settings`; `PUT /` body `{url: str, enabled: bool = true}` → `save` (`WebhookUrlError` → 400 `{"detail": str(e)}`); `POST /rotate-secret` → `{"secret": ...}` or 404; `DELETE /` → `{"deleted": bool}`; `POST /test` → `{"delivery_id": ...}` or 409 `{"detail": "Webhook is not configured or disabled"}`.

- [ ] **Step 1: Write failing tests** with `client` + `auth_headers` (user `user-1`, create it with `create_user(user_id="user-1")`): `test_full_lifecycle` (GET unconfigured → PUT returns `secret` → GET shows hint → rotate → test → DELETE → GET unconfigured); `test_invalid_url_400`; `test_api_key_auth_rejected` (`Authorization: Bearer sk_anything`, no session headers → 401).
- [ ] **Step 2: Run** `.venv/bin/pytest --no-cov tests/integration/test_webhook_routes.py -v` → FAIL.
- [ ] **Step 3: Implement** routes and register the router.
- [ ] **Step 4: Run** → PASS; then the full backend suite `.venv/bin/pytest` → PASS (coverage gate included).
- [ ] **Step 5: Commit** `feat(webhooks): settings API`.

### Task 7: Frontend proxies and settings card

**Files:**
- Create: `frontend/src/app/api/webhooks/route.ts` (GET, PUT, DELETE), `frontend/src/app/api/webhooks/rotate-secret/route.ts` (POST), `frontend/src/app/api/webhooks/test/route.ts` (POST) — same shape as `src/app/api/api-keys/route.ts`.
- Create: `frontend/src/components/webhook-settings-card.tsx`
- Modify: `frontend/src/app/settings/api-keys/page.tsx` (render `<WebhookSettingsCard />` below the keys list)
- Modify: `frontend/messages/{en,de,es,id,pt,zh}/settings.json` (new `webhook` object, identical English text)
- Test: `frontend/src/components/webhook-settings-card.test.tsx`

**Interfaces:**
- Consumes: the Task 6 JSON shapes via `/api/webhooks…`.
- Produces: `export function WebhookSettingsCard(): JSX.Element` using `useTranslations("settings")` keys under `webhook.*`.
- English copy: title "Webhook"; description "SupoClip sends a signed POST to this URL when every clip of a task is ready."; URL label "Webhook URL"; "Enabled"; "Save"; "Send test"; "Rotate secret"; "Remove"; secret notice "Copy this signing secret now. It won't be shown again."; last delivery "Last delivery: {status} ({code})"; never "No deliveries yet".

- [ ] **Step 1: Write failing vitest tests** (mock `fetch` like `page.test.tsx`): `shows the empty form when unconfigured`; `saving shows the secret once and hides it after reload`; `send test posts to /api/webhooks/test`; `shows the server error for an invalid URL`.
- [ ] **Step 2: Run** `cd frontend && pnpm exec vitest run src/components/webhook-settings-card.test.tsx` → FAIL.
- [ ] **Step 3: Implement** proxies, card, messages; add the card to the page.
- [ ] **Step 4: Run** the test file → PASS; then `pnpm run test && pnpm run lint && pnpm exec tsc --noEmit` → PASS (existing `page.test.tsx` included).
- [ ] **Step 5: Commit** `feat(webhooks): settings card`.

### Task 8: Docs

**Files:**
- Create: `docs/webhooks.md`
- Modify: `CLAUDE.md` (endpoint list: `/webhooks/*`; env block: `PUBLIC_API_URL`, `WEBHOOK_ALLOW_PRIVATE_TARGETS`; cron list: `deliver_webhooks`; Key Backend Files: `src/webhooks/`)

- [ ] **Step 1: Write `docs/webhooks.md`**: setup on `/settings/api-keys`; full payload example from the spec; headers; signature verification snippets in Python and Node (`timestamp + "." + rawBody`, constant-time compare, reject timestamps older than 5 min); retry schedule; dedupe by `delivery_id`; fetching a clip: `curl -L -H "Authorization: Bearer sk_..." "<download_url>"`; the two env vars.
- [ ] **Step 2: Verify** the Python snippet against `signing.sign` with a one-off run of `tests/unit/test_webhook_units.py` values (same output).
- [ ] **Step 3: Commit** `docs(webhooks): receiver guide`.
