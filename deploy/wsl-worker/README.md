# SupoClip video worker on WSL (GPU)

The only video worker of the Dokploy deployment (`deploy/dokploy/`). It downloads sources,
transcribes with Whisper on the NVIDIA GPU and renders clips, then publishes the results to the R2
bucket and PostgreSQL. It reaches the VPS's PostgreSQL and Redis over Tailscale
(`100.98.156.81:15432` / `:16379`, exposed with `tailscale serve --tcp`, see
`deploy/dokploy/README.md`). It needs nothing else from the VPS (`docs/render-worker.md`).

When the PC is off, generations, studio preparations and exports wait in the queue and start once
the worker is back. Browsing and playback keep working.

## Requirements

- Docker with NVIDIA GPU support (Docker Desktop with WSL 2 here).
- Tailscale on the PC, logged in to the same tailnet as the VPS.
- Run from a normal checkout of this repo on the Linux filesystem (not under `/mnt/c`), on the same
  commit the VPS runs (`git pull` on `main`).

## Setup

1. Log in to GHCR with a token that has `read:packages` (the image is private):

   ```sh
   docker login ghcr.io -u reynandaptr
   ```

2. Create the two env files from `.env.example`, outside Git, mode 600:

   ```sh
   install -d -m 700 ~/.config/supoclip
   install -m 600 /dev/null ~/.config/supoclip/worker-compose.env   # WORKER_DATABASE_URL, ...
   install -m 600 /dev/null ~/.config/supoclip/worker.env           # credentials, same as the VPS
   ```

3. Start the worker (the first pull of the CUDA image is several GB):

   ```sh
   docker compose --env-file ~/.config/supoclip/worker-compose.env \
     -f deploy/wsl-worker/compose.yml up -d
   docker compose --env-file ~/.config/supoclip/worker-compose.env \
     -f deploy/wsl-worker/compose.yml logs --tail 50 worker
   ```

   Do not run `docker compose config` with the real env files: it prints credentials.

4. Keep it on the release the VPS runs. A systemd user timer pulls `latest` every 5 minutes and
   recreates the worker only when the image changed:

   ```sh
   systemctl --user link "$PWD/deploy/wsl-worker/supoclip-worker-update.service" \
                         "$PWD/deploy/wsl-worker/supoclip-worker-update.timer"
   systemctl --user enable --now supoclip-worker-update.timer
   systemctl --user list-timers supoclip-worker-update.timer
   ```

   The service assumes the checkout is at `~/Development/sourcecodes/personal/supoclip`; edit
   `SUPOCLIP_DIR` in the unit if not. The VPS applies database migrations when its API starts; they
   are additive, so a worker one release behind keeps working until the timer updates it.

## Checks

- `https://supoclip-api.reynandaptr.dev/health/worker` reports a live worker.
- `docker compose ... exec worker nvidia-smi` shows the GPU.
- A generation's progress in the worker log shows Whisper on `cuda`.

## Stopping

`docker compose ... stop worker`. Running jobs get up to 3 hours to finish; a job cut off is retried
by the queue when a worker is back. Disable the timer first (`systemctl --user disable --now
supoclip-worker-update.timer`), or it starts the worker again.
