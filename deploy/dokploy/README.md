# SupoClip on Dokploy (application host)

`compose.yml` runs the web app, API, AI worker, scheduler, PostgreSQL and Redis on the Dokploy VPS.
The only video worker runs on another machine with a GPU (`deploy/wsl-worker/`), so:

- media lives in a private Cloudflare R2 bucket (`MEDIA_STORAGE=s3`, see `docs/media-storage.md`);
- `MANUAL_CLIP_EDITS_ENABLED=false`: trim, split, merge, caption edit, regenerate, the synchronous
  preset export and applying task settings to existing clips answer `403`, because they would encode
  video on this host (saving task settings for new clips still works);
- PostgreSQL and Redis are published on `127.0.0.1:15432` / `127.0.0.1:16379` only and reach the
  worker through Tailscale.

Images come from CircleCI (`.circleci/config.yml`): `ghcr.io/reynandaptr/supoclip-backend` (CPU build)
and `ghcr.io/reynandaptr/supoclip-frontend`, built for `https://supoclip.reynandaptr.dev` and
`https://supoclip-api.reynandaptr.dev`. Every service uses `pull_policy: always`, so a deploy runs the
newest `latest`.

## One-time setup

### Cloudflare R2

1. Create a private bucket (for example `supoclip-media`).
2. Create an R2 API token with **Object Read & Write** on that bucket only. Its access key id and
   secret go in `MEDIA_S3_ACCESS_KEY_ID` / `MEDIA_S3_SECRET_ACCESS_KEY`; the account's S3 endpoint
   (`https://<account-id>.r2.cloudflarestorage.com`) goes in `MEDIA_S3_ENDPOINT`.
3. CORS policy on the bucket (browsers play clips and upload parts directly):

   ```json
   [
     {
       "AllowedOrigins": ["https://supoclip.reynandaptr.dev"],
       "AllowedMethods": ["GET", "HEAD", "PUT"],
       "AllowedHeaders": ["*"],
       "ExposeHeaders": ["ETag", "Content-Length", "Content-Range"],
       "MaxAgeSeconds": 3600
     }
   ]
   ```

4. Lifecycle rule: abort incomplete multipart uploads after 1 day.

### DNS

`supoclip.reynandaptr.dev` and `supoclip-api.reynandaptr.dev` point at the VPS (unless a wildcard
record already does).

### Dokploy

- GHCR access: a registry entry for `ghcr.io` (or `docker login ghcr.io` on the VPS) with a token that
  can read the `supoclip-*` packages.
- Project `supoclip`, service type **Compose**:
  - Provider: GitHub `reynandaptr`, repository `supoclip`, branch `main`.
  - Compose path: `./deploy/dokploy/compose.yml`.
  - Isolated deployment: **off** (the network must keep the name `supoclip-network`).
  - Auto deploy: **off**. CircleCI deploys after the images are pushed.
- Environment: every variable in `.env.example`. Generate secrets with `openssl rand -hex 32`.
  `DISABLE_SIGN_UP=false` only for the first deploy (below).
- Domains (HTTPS, Let's Encrypt):

  | Host | Service | Port |
  |---|---|---|
  | `supoclip.reynandaptr.dev` | `frontend` | 3107 |
  | `supoclip-api.reynandaptr.dev` | `backend` | 8000 |

### Tailscale on the VPS

The WSL worker reaches PostgreSQL and Redis over the tailnet only:

```sh
sudo tailscale serve --bg --tcp 15432 tcp://127.0.0.1:15432
sudo tailscale serve --bg --tcp 16379 tcp://127.0.0.1:16379
tailscale serve status
```

`--bg` keeps the configuration across reboots. From outside the tailnet, the VPS public IP must not
answer on 15432 or 16379. (Other Dokploy apps on this host may publish 5432/6379 themselves; SupoClip
publishes nothing on 0.0.0.0.)

### First account (sign-up stays off afterwards)

1. Deploy once with `DISABLE_SIGN_UP=false` and create the owner account on
   `https://supoclip.reynandaptr.dev` straight away.
2. Set `DISABLE_SIGN_UP=true` (or remove it; `true` is the compose default) and redeploy.
3. Check from a machine on the tailnet, with the local `postgres:15-alpine` image:

   ```sh
   docker run --rm -it postgres:15-alpine psql \
     "postgresql://supoclip@100.98.156.81:15432/supoclip" \
     -c 'SELECT count(*) FROM users;'
   ```

   It must be `1`. To use the `/admin` panel (feature flags such as `workflows`), mark the account
   as admin in the same session: `UPDATE users SET is_admin = true WHERE id = '<your user id>';`.

## Deploy order

SupoClip first: its compose creates `supoclip-network`, which clipper-uploader joins as an external
network. Then start the WSL worker (`deploy/wsl-worker/README.md`).
