# Task-completed webhook

SupoClip can tell another service, once, that a task's clips are all done and where to fetch each
one. Each user has one webhook (a URL plus a signing secret). It fires for every task the user
creates, however it was created (web, iOS, API key, MCP).

Events: `task.completed` (a task became `completed`) and `webhook.test` (the "Send test" button).
A task that ends `error` or `cancelled` sends nothing, and neither do clip-studio edits or exports.

## Set up

1. Create an API key at `/settings/api-keys`. The receiver uses it to download clips.
2. In the **Webhook** card on the same page, enter the receiver URL, keep **Enabled** on and save.
3. Copy the signing secret (`whsec_...`) now: it is shown once, after creating or rotating it.
   Later the card shows only the last four characters.
4. Press **Send test** to queue a `webhook.test` delivery. The card shows the last delivery's status.

**Rotate secret** replaces the secret. Retries of an unsent delivery are signed with the secret
that is current when they are sent, not the one at enqueue time. **Remove** deletes the webhook.

## Request

`POST <your URL>` with a JSON body.

| Header | Value |
|---|---|
| `Content-Type` | `application/json` |
| `User-Agent` | `SupoClip-Webhooks/1` |
| `X-SupoClip-Event` | `task.completed` or `webhook.test` |
| `X-SupoClip-Delivery` | Delivery id (UUID), also `delivery_id` in the body |
| `X-SupoClip-Timestamp` | Unix seconds when this attempt was sent |
| `X-SupoClip-Signature` | `v1=` + hex HMAC-SHA256 of `"<timestamp>.<raw body>"` keyed with the secret |

### `task.completed` body

```json
{
  "event": "task.completed",
  "delivery_id": "0b0e3d52-6f0c-4c43-9f0b-1c1a5a1d9e10",
  "created_at": "2026-11-17T10:00:00Z",
  "task": {
    "id": "5a1f0c7e-0d2b-4f55-8f4e-3a2b9c6d7e11",
    "status": "completed",
    "source_title": "My long video",
    "source_url": "https://youtube.com/watch?v=...",
    "source_type": "youtube",
    "workflow": "highlights",
    "completed_at": "2026-11-17T09:59:58Z",
    "clips_count": 1,
    "url": "https://app.example.com/tasks/5a1f0c7e-0d2b-4f55-8f4e-3a2b9c6d7e11"
  },
  "clips": [
    {
      "id": "c3d9a1b2-7e4f-4a60-b1c2-9d8e7f6a5b43",
      "order": 1,
      "hook_title": "The mistake everyone makes",
      "text": "transcript of the clip",
      "start_time": "01:23",
      "end_time": "02:05",
      "duration": 42.0,
      "virality_score": 87,
      "workflow_role": null,
      "part_number": null,
      "download_url": "https://api.example.com/tasks/5a1f0c7e-0d2b-4f55-8f4e-3a2b9c6d7e11/clips/c3d9a1b2-7e4f-4a60-b1c2-9d8e7f6a5b43/file?download=true"
    }
  ]
}
```

Clips are ordered by `order`. `task.url` points at the web app,
`download_url` at the API (`PUBLIC_API_URL`). The `webhook.test` body is
`{"event": "webhook.test", "delivery_id": "...", "created_at": "...", "message": "SupoClip webhook test"}`.

## Verify the signature

Sign the **raw** request body, not a re-serialised copy. Compare in constant time and reject a
timestamp more than 5 minutes from your clock.

Python:

```python
import hashlib, hmac, time

def verify(secret: str, timestamp: str, signature: str, raw_body: bytes) -> bool:
    if abs(time.time() - int(timestamp)) > 300:
        return False
    expected = "v1=" + hmac.new(
        secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
```

Node:

```js
const crypto = require("crypto");

function verify(secret, timestamp, signature, rawBody) {
  if (Math.abs(Date.now() / 1000 - Number(timestamp)) > 300) return false;
  const expected =
    "v1=" +
    crypto.createHmac("sha256", secret).update(`${timestamp}.`).update(rawBody).digest("hex");
  const a = Buffer.from(expected);
  const b = Buffer.from(signature);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
```

`rawBody` is a `Buffer` (or string) of the exact bytes received. Headers are
`X-SupoClip-Timestamp` and `X-SupoClip-Signature`.

## Delivery, retries and de-duplication

- Any `2xx` response means delivered. Anything else (other status, timeout, connection or URL
  error) is retried. The request times out after 10 seconds and redirects are not followed, so
  answer `2xx` quickly and do the work afterwards.
- Retry delays after failed attempt 1 to 7: 1 min, 5 min, 15 min, 1 h, 3 h, 6 h, 12 h. The 8th
  failure marks the delivery `failed` (about 23 hours in total).
- Delivery is at-least-once. De-duplicate by `delivery_id`. A task that is resumed and completes
  again sends a new delivery (new `delivery_id`, same `task.id`).
- A delivery is queued when the task completes; a cron (`deliver_webhooks`, every 15 seconds) sends
  due deliveries. The same cron queues completions from the last 24 hours that were missed, for
  example after a worker crash.
- Turning the webhook off or removing it cancels pending deliveries. Turning it back on does not
  send tasks that completed while it was off. A POST already in flight when you disable may still
  arrive once.
- Delivered, failed and cancelled rows are pruned after 30 days.
- A webhook problem never fails or delays a task.

## Download a clip

`download_url` is stable (it does not expire). Fetch it with the API key from step 1:

```bash
curl -L -H "Authorization: Bearer sk_..." -o clip.mp4 "<download_url>"
```

With S3 storage the API answers `307` to a short-lived signed bucket URL, so follow redirects
(`-L`). With local storage it streams the file directly. The payload never contains your API key
or a signed bucket URL.

## Configuration

Set on the backend (API and worker):

| Variable | Default | Purpose |
|---|---|---|
| `PUBLIC_API_URL` | `http://localhost:8000` | Public base URL of the API, used to build `download_url` (trailing `/` stripped). Set it to the address the receiver can reach |
| `WEBHOOK_ALLOW_PRIVATE_TARGETS` | `true` | `true`: `http` and `https` URLs, any address except unspecified or multicast (the receiver may be on the same host or Docker network). `false`: `https` only, and loopback, private, link-local, multicast, reserved and unspecified addresses (IPv4 and IPv6) are rejected |

The URL is checked when saved and again before every send, and the request connects to the address
that was checked. URLs are limited to 2048 characters.

## API

Session-authenticated only (an API key is rejected), like `/api-keys`:
`GET /webhooks/`, `PUT /webhooks/` (`{url, enabled}`), `POST /webhooks/rotate-secret`,
`DELETE /webhooks/`, `POST /webhooks/test`.
