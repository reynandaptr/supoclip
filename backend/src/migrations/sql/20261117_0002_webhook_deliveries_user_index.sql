CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_user ON webhook_deliveries (user_id, created_at DESC);
