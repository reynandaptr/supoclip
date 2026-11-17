-- Task-completed webhooks: one endpoint per user, plus a durable delivery queue.
CREATE TABLE IF NOT EXISTS user_webhooks (
    user_id        VARCHAR(36) PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    url            TEXT NOT NULL,
    secret_enc     TEXT NOT NULL,              -- encrypted with encrypt_setting_value
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    enabled_since  TIMESTAMPTZ NOT NULL DEFAULT now(),  -- reset on create and on disabled->enabled
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
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
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_due ON webhook_deliveries (next_attempt_at) WHERE status = 'pending';
-- One delivery per completion, whichever path (fast enqueue or reconciler) gets there first.
CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_deliveries_completion
    ON webhook_deliveries (task_id, event, task_completed_at) WHERE task_completed_at IS NOT NULL;
