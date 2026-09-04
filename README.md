# Frappe Helpdesk (SIS support ticketing)

Frappe Helpdesk running as its own stack, integrated with the SIS over HTTP.
Separate service, separate MariaDB — the SIS never touches these tables.

- `build-image.sh` — builds the custom image (Helpdesk ships in no official one)
- `docker-compose.helpdesk.yml` — the stack
- `apps.json` — which Frappe apps go into the image
- `scripts/deploy.sh` — one-shot deployment: build, start, wait, verify
- `scripts/provision.py` — idempotent setup of custom fields and webhooks
- `docs/helpdesk-setup.md` — **start here**

The SIS side lives in `SIS_BACKEND/support/`.

## Quick start

```bash
cp .env.example .env          # then fill it in
./scripts/deploy.sh           # builds if needed, starts, waits, verifies
python scripts/provision.py
```

`deploy.sh` is safe to re-run — it is the update path as well as the install
path. On a server where the compose file and environment live elsewhere:

```bash
COMPOSE_FILE=/root/helpdesk/docker/docker-compose.helpdesk.yml ENV_FILE=/root/helpdesk/config/.env.dev /root/helpdesk/scripts/deploy.sh
```

Full walkthrough, including how to point the two systems at each other and how
to verify the integration end to end: `docs/helpdesk-setup.md`.
