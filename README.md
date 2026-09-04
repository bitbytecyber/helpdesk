# Frappe Helpdesk (SIS support ticketing)

Frappe Helpdesk running as its own stack, integrated with the SIS over HTTP.
Separate service, separate MariaDB — the SIS never touches these tables.

- `build-image.sh` — builds the custom image (Helpdesk ships in no official one)
- `docker-compose.helpdesk.yml` — the stack
- `apps.json` — which Frappe apps go into the image
- `scripts/provision.py` — idempotent setup of custom fields and webhooks
- `docs/helpdesk-setup.md` — **start here**

The SIS side lives in `SIS_BACKEND/support/`.

## Quick start

```bash
./build-image.sh
cp .env.example .env          # then fill it in
docker compose -f docker-compose.helpdesk.yml up -d
python scripts/provision.py
```

Full walkthrough, including how to point the two systems at each other and how
to verify the integration end to end: `docs/helpdesk-setup.md`.
