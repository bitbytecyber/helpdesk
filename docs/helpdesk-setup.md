# Frappe Helpdesk — deployment and setup

Support ticketing for the SIS, running as its own stack with its own MariaDB.
The SIS never touches these tables; the two systems meet over HTTP only.

The SIS side is already built — the `support` app in `SIS_BACKEND`. This
document covers the other half: standing Frappe up and configuring it so the two
can talk.

---

## What runs

Helpdesk is not one container. The stack is:

| Service | Role |
|---|---|
| `db` | MariaDB 10.6 — Helpdesk's own database, unrelated to the SIS's Postgres |
| `redis-cache` | Frappe's cache |
| `redis-queue` | Frappe's job queue — a separate instance from the cache |
| `backend` | gunicorn, the Python application |
| `websocket` | Node socket.io, for live updates in the agent UI |
| `queue-short` / `queue-long` | RQ workers. **Webhook delivery runs here** |
| `scheduler` | SLA escalations, inbound email polling |
| `frontend` | nginx, serving built assets and proxying |

Plus `configurator` and `create-site`, which run once and exit. Seeing them
`Exited` in `docker compose ps` is correct, not a crash.

Budget about 2 GB of RAM to boot and 4 GB to be comfortable.

---

## 1. Build the image

Helpdesk ships in no official image, so there is nothing to pull. The image is
built with the app baked in, using frappe_docker's own custom-apps build:

```bash
cd /c/Development/Helpdesk
./build-image.sh
```

This produces `sis/frappe-helpdesk:v15`. Everything is pinned —
`frappe_docker` ref, Frappe branch, Python and Node versions — because an
unpinned build is how an API method quietly gets renamed underneath a working
integration. Override with `IMAGE=`, `TAG=`, `FRAPPE_BRANCH=` if you need to.

The app list lives in `apps.json`. `frappe/helpdesk` declares `frappe/telephony` as a required app (`required_apps` in its `hooks.py`), so it must be listed *before* helpdesk or `bench new-site --install-app helpdesk` fails partway through with `ModuleNotFoundError: No module named 'telephony'`. To add another Frappe app later, add it here and rebuild.

## 2. Configure and start

```bash
cp .env.example .env
```

Fill in `SITE_NAME`, `ADMIN_PASSWORD` and `DB_ROOT_PASSWORD`, then generate the
shared webhook secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put that value in `WEBHOOK_SECRET`. It has to match `HELPDESK_WEBHOOK_SECRET` on
the SIS side exactly, or every delivery is rejected with a 401.

> **`SITE_NAME` is the Host header Frappe routes on.** Use the hostname you will
> actually serve from, even locally. If the SIS later reaches this stack by
> container name, the request must still carry this value or it resolves to no
> site at all.

```bash
docker compose -f docker-compose.helpdesk.yml up -d
docker compose -f docker-compose.helpdesk.yml logs -f create-site
```

Wait for `Created <site> with Helpdesk installed.` Then the agent UI is at
`http://localhost:8093/helpdesk`, and the Desk at `/app`. Log in as
`Administrator` with `ADMIN_PASSWORD`.

Re-running `up -d` is safe: `create-site` skips a site that already exists.

## 3. Create the SIS integration user

In the Desk (`/app`):

1. **User list → New.** Email `sis-integration@yourschool.com`, first name
   `SIS Integration`, User Type `System User`.
2. Give it the roles it needs to create and read tickets — the Helpdesk agent
   role plus `System Manager` is the quick path; tighten afterwards.
3. Open the user → **API Access → Generate Keys.** Copy the secret immediately;
   it is shown once.

Those two values become `HELPDESK_FRAPPE_API_KEY` and
`HELPDESK_FRAPPE_API_SECRET` in the SIS environment.

**Do not use an Administrator key for the SIS.** Generate a separate
Administrator key for step 4 only, and treat it as a setup credential.

## 4. Provision fields and webhooks

```bash
pip install requests
python scripts/provision.py --dry-run   # see what it would do
python scripts/provision.py
```

This creates, idempotently:

- **Five Custom Fields on `HD Ticket`** — `sis_phone`, `sis_email`,
  `sis_admission_no`, `sis_campus_id`, `sis_link_status` (plus a section break).
  These fieldnames are hardcoded in `support/services/frappe_client.py`; Frappe
  drops unknown keys silently on insert, so a mismatch shows up as tickets that
  simply have no phone number on them.

  The phone is stored **as the requester typed it** — dialable as-is. The SIS
  keeps a separate 10-digit comparison key for matching; no canonical E.164 form
  is produced anywhere, because the project has no phone-parsing library and
  adding one bought only a nicer display string.
- **Three Webhook rows** pointing at the SIS, all carrying the shared secret.

| Webhook | DocType | Event |
|---|---|---|
| ticket created | `HD Ticket` | `after_insert` — catches tickets raised by email |
| ticket changed | `HD Ticket` | `on_update` — status, priority, assignee, resolution |
| reply added | `Communication` | `after_insert`, scoped to `HD Ticket` references |

Re-run it any time — after a Frappe upgrade, or after rotating the secret. It
updates existing rows in place rather than duplicating them.

### Why the field list on each webhook matters

A Frappe webhook sends **only** the fields listed in its Webhook Data table. It
is not the whole document. `provision.py` always includes `name` and `modified`:

- without `name`, the SIS cannot tell which ticket the payload is about
- without `modified`, it cannot tell a duplicate delivery from a real update

If you add a custom field later, re-run `provision.py` so the webhooks carry it.

### Two Webhook settings that must never be touched

`provision.py` deliberately sets `enable_security: 1` and leaves
`request_structure` **unset**. Both were found by tracing a real failure
end-to-end during first setup, and both fail silently if undone — no error,
just an empty or unsigned request that looks fine in every log except the
one that matters.

- **`enable_security`** gates the signature entirely.
  `Webhook.get_webhook_headers()` only computes and sends
  `X-Frappe-Webhook-Signature` when this is set; `webhook_secret` sits inert
  otherwise (and is hidden in the Desk UI unless the box is checked). Leave
  it off and Frappe ships unsigned requests, which the SIS correctly rejects
  as unauthenticated — but the error looks identical to a wrong secret.
- **`request_structure` must not be `"JSON"`.** `Webhook.validate_request_body()`
  unconditionally runs `self.webhook_data = []` on every save when this field
  equals `"JSON"` — the field-selection table and the alternative
  Jinja-templated `webhook_json` field are mutually exclusive, and setting
  `request_structure` to `"JSON"` is Frappe's way of saying "use
  `webhook_json`, not the table." Set both (as an earlier version of this
  script did) and `webhook_data` is silently wiped on save; the webhook then
  ships `"{}"` as its body — still correctly signed, since signing happens
  over whatever `data` ends up being — so the SIS sees a *validly signed
  empty payload* and rejects it for missing `name`, not for a bad signature.
  Confirmed by reading `webhook.py`'s `validate_request_body()` and
  `get_webhook_data()` directly, and reproduced with the REST layer removed
  entirely (`doc.update(...); doc.save()` via `bench console`) to rule out
  a client-side cause. `enqueue_webhook()` always serialises the body with
  `frappe.as_json(data)` regardless of `request_structure`, so leaving it
  unset changes nothing about what bytes are sent — only which field list
  survives a save.
- If you ever hand-edit a webhook row that `provision.py` created, updating
  only some fields (the raw REST `PUT`, or a partial `doc.update()`) leaves
  every field you didn't mention exactly as it was. A row saved once with
  `request_structure: "JSON"` stays stuck losing its field list on every
  future save until something explicitly resets it back to `""` — merely
  omitting the key from a later update is not enough.

## 5. Point the two systems at each other

In the SIS environment (`SIS_BACKEND`):

```
HELPDESK_FRAPPE_URL=http://localhost:8093
HELPDESK_FRAPPE_SITE_HOST=helpdesk.localhost
HELPDESK_FRAPPE_API_KEY=<from step 3>
HELPDESK_FRAPPE_API_SECRET=<from step 3>
HELPDESK_FRAPPE_TIMEZONE=Asia/Karachi
HELPDESK_WEBHOOK_SECRET=<same value as WEBHOOK_SECRET here>
```

Pass them through to the `backend`, `celery` and `celery-beat` services in the
SIS compose files — the poll and the webhook handler both need them.

Then create the waffle switch that turns the module on. In Django admin →
**Switches → Add**, name `support`, active. Until it exists the intake endpoint
returns 404: the flag fails closed, so an unconfigured deployment cannot
accidentally expose an unauthenticated write endpoint.

### Reaching each other over the internal network

Simplest is the public URL above. To keep traffic on the host instead:

```bash
docker network create sis-shared
```

Uncomment the `networks:` block at the bottom of
`docker-compose.helpdesk.yml`, add the same block to the SIS compose files, and
set `HELPDESK_FRAPPE_URL=http://frontend:8080` with
`HELPDESK_FRAPPE_SITE_HOST` still set to the site name. The SIS client sends
that as the `Host` header for exactly this reason.

Going the other way, `SIS_WEBHOOK_URL` must be reachable *from inside* the
Frappe containers, and that hostname must be in the SIS's `ALLOWED_HOSTS`.

### Seed the reconciliation poll on local/dev

One more SIS-side step, and it is easy to miss because nothing errors when
it's skipped — the poll just silently never runs.

`sis_backend/settings/dev.py` deliberately filters `CELERY_BEAT_SCHEDULE` down
to keys starting with `finance-` (comment: *"Dev deliberately runs no periodic
business jobs"*), so `support-reconcile-tickets` — defined in `base.py` — never
reaches `django_celery_beat`'s scheduler on local or dev, even though
`celery-beat` is running and the task is registered. Production is fine:
`prod.py` does a full merge, not a filter.

The project's own established fix for exactly this is a seed command that
writes the `PeriodicTask` row directly, bypassing the settings-dict path
entirely — the same pattern `attendance` uses for
`seed_attendance_beat`. Run it once per environment:

```bash
docker compose -f docker-compose.local.yml exec backend python manage.py seed_support_beat
```

Confirm it took:

```bash
docker compose -f docker-compose.local.yml exec backend python manage.py shell -c "
from django_celery_beat.models import PeriodicTask
print(PeriodicTask.objects.filter(task='support.reconcile_tickets').exists())
"
```

## 6. Email

Set up an `Email Account` in the Desk on a support mailbox, with **matching
inbound and outbound settings**. Asymmetric configuration is the usual cause of
replies that open new tickets instead of threading onto the existing one.

Frappe owns the email thread; the SIS raises in-app notifications only. Do not
configure both to email the requester or every reply arrives twice.

---

## Verifying it end to end

**1. Outbound — SIS to Helpdesk.**

```bash
curl -X POST http://localhost:8092/api/v1/support/tickets/ -H "Content-Type: application/json" -d '{"subject":"Test from SIS","description":"Checking the integration.","phone":"0300-1234567"}'
```

Expect `201` with a reference. The ticket should appear in the agent UI with
`0300-1234567` in the Phone field — proof the custom fields are wired.

**2. Inbound — webhook.** Resolve that ticket in the agent UI, then check
**Webhook Request Log** in the Desk. A `200`/`202` means the SIS accepted it.
A `401` means the secrets do not match.

**3. Inbound — the poll. Do not skip this one.** Webhooks are best-effort:
Frappe dispatches them from a worker with limited retries, so a restart, a
flushed Redis or a brief SIS outage loses that event permanently and silently.

```bash
docker compose -f docker-compose.local.yml stop backend celery
```

Change a ticket's priority in the agent UI, bring the SIS back, and wait up to
three minutes for `support.reconcile_tickets`. If the mirror does not catch up,
the integration is not finished — a webhook-only setup passes every other test
and drifts in production.

---

## Operating notes

- **Back up the `sites` volume, not just the database.** Attachments live on
  disk; a database-only backup loses every file.
- **Pin versions.** Both `apps.json` and `build-image.sh` are pinned. Keep them
  that way — a Helpdesk minor bump can rename API methods.
- **Watch `Webhook Request Log`** when something did not arrive. It records
  every attempt and response, and it is the first place to look.
- **Timezones.** Frappe stores naive datetimes in the site timezone; Django is
  UTC-aware. `HELPDESK_FRAPPE_TIMEZONE` drives the conversion, which happens in
  `support/services/sync.py` and nowhere else.
- **Phone matching** is last-10-digits, via
  `people.services.contact_channels.phone_match_key` — the one helper both
  `onboarding` and `support` use. If you later add SMS one-time codes, that is
  the point at which a real E.164 parser starts to earn its keep.
- **Upgrades** run here on their own schedule, independent of SIS deploys.
  Rebuild the image, `up -d`, then `bench --site <site> migrate` in the
  `backend` container.

## Later, not now

- **SSO for agents.** The SIS already runs `allauth.idp.oidc`, so it is an
  OpenID Connect provider today. Register Helpdesk as a client and add a
  *Social Login Key* so staff sign in with their SIS account — one password, one
  offboarding action. Parents and students get no Frappe account at all.
- **Disable Frappe's customer portal** so there is no second, unmanaged front
  door into support data.
- **Move the provisioning into a custom Frappe app** (`sis_bridge`) with the
  custom fields as fixtures, once the field set has settled. `provision.py` is
  the right tool while it is still moving.
