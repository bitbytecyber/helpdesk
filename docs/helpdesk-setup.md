# Frappe Helpdesk — deployment and setup

Support ticketing for the SIS, running as its own stack with its own MariaDB.
The SIS never touches these tables; the two systems meet over HTTP only.

The SIS side is already built — the `support` app in `SIS_BACKEND`. This
document covers the other half: standing Frappe up and configuring it so the two
can talk.

> **Helpdesk is the source of truth for tickets.** The SIS stores no ticket rows
> at all: it creates tickets here and reads them back live, scoped by
> `sis_user_id` and `sis_phone_key`. There is no mirror table and no
> reconciliation poll. Exactly one thing flows the other way: a webhook when a
> ticket is resolved or closed, so the SIS can raise an in-app notification for
> the requester. It stores nothing either. If you are reading an older copy of
> this doc describing a mirror or a poll, it predates that change.

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
| `queue-short` / `queue-long` | RQ workers — email sending, SLA jobs, **webhook delivery** |
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

Put it in `WEBHOOK_SECRET`, and set `SIS_WEBHOOK_URL` to the SIS's
`/api/v1/support/webhooks/frappe/` endpoint **as reachable from inside the
Frappe containers**. `WEBHOOK_SECRET` must equal `HELPDESK_WEBHOOK_SECRET` in
the SIS environment exactly, or every notification delivery is rejected with a
401.

Both are optional: leave them blank and `provision.py` creates the custom fields
and skips the webhook. You lose the in-app "your ticket was resolved"
notification and nothing else.

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

## 4. Provision the custom fields

```bash
pip install requests
python scripts/provision.py --dry-run   # see what it would do
python scripts/provision.py
```

This creates, idempotently, **six Custom Fields on `HD Ticket`** (plus a
section break): `sis_phone`, `sis_phone_key`, `sis_email`, `sis_admission_no`,
`sis_campus_id` and `sis_user_id`.

These fieldnames are hardcoded in `support/services/frappe_client.py`; Frappe
drops unknown keys silently on insert, so a mismatch shows up as tickets that
simply have no phone number on them.

### Two fields decide who may read a ticket

Ownership is not one field but two, and they are not equally strong.

| Field | Written by | Strength |
|---|---|---|
| `sis_user_id` | the SIS, from `request.user` | **Proof.** The requester cannot influence it. |
| `sis_phone_key` | the SIS, from what the requester typed | **Claim.** Anyone can type any number. |

`GET /api/v1/support/my-tickets/` returns the union: tickets stamped with the
caller's `sis_user_id`, **plus** tickets whose `sis_phone_key` matches a mobile
number on that caller's own `Person` record. The thread endpoint applies the
same two checks before returning a single message, so anything the list shows
can actually be opened.

Frappe's REST filters are ANDed — there is no OR across fields — so the SIS runs
one query per signal and merges them, de-duplicated by ticket name.

**Why the phone half exists.** A ticket raised from the login page is raised by
someone who is not signed in, so it carries no user id. Without a second signal
it would be invisible in that person's portal forever, even after they sign in.
Matching on their number is what makes it recoverable.

**What the phone half costs, stated plainly.** `sis_phone_key` is derived from
free text typed into a public, unauthenticated form. Anyone who types *your*
number into it will have their ticket appear in *your* portal, thread included.
That is inherent to treating a phone number as an ownership signal and cannot be
designed away — only reduced. It is a deliberate, accepted trade.

Three constraints keep it as narrow as it can be, and none should be relaxed
without understanding what it buys:

- **Numbers come from the SIS contact book, never from the request.** A caller
  cannot supply a number to match on; they can only match numbers already
  recorded against their `Person`. This is the whole safety margin — without it,
  "my tickets" would return whatever number the caller cared to type.
- **`MOBILE` contacts only.** A `HOME` or `WORK` line is routinely shared — one
  school office number could cover every employee — and an `EMERGENCY` contact
  is usually somebody else's number entirely. Matching on any of those would put
  one person's tickets in another person's portal.
- **A key under 10 digits is discarded.** Short keys are buckets that unrelated
  numbers fall into, and here they would be compared against every ticket in the
  school.

Two more consequences worth internalising:

- **A missing custom field does not fail loudly.** Frappe drops unknown keys on
  insert, the query then matches nothing, and every portal ticket list is
  permanently empty with no error in any log. Provision before switching the SIS
  over.
- **Blank must never match blank.** An anonymous ticket has an empty
  `sis_user_id`; a caller with no mobile on file has no key. The SIS refuses
  both explicitly — do not "helpfully" default either field to anything.

**If someone says an old ticket is not showing**, check their `Person` record
for a `MOBILE` contact first. No mobile on file means no phone query runs at
all, and they will see only tickets they raised while signed in.

### Why the phone is stored twice

The pair matters:

- **`sis_phone`** — as the requester typed it. This is what an agent dials.
- **`sis_phone_key`** — its last 10 digits, via
  `people.services.contact_channels.phone_match_key`, written at intake.

Every match — the anonymous lookup and the signed-in phone signal above — uses
the **key**, never `sis_phone`. The same number reaches Helpdesk in several shapes depending on where it was typed —
`+923001234567` from the frontend's phone field, `0300-1234567` from a raw API
call, `03001234567` from someone typing it plainly. An exact match on the typed
string finds one of those and misses the rest, and Frappe cannot normalise
inside a filter, so the key is computed once at intake and compared as-is.

No canonical E.164 form is produced anywhere, because the project has no
phone-parsing library and adding one bought only a nicer display string.

### One webhook, not three

Earlier versions of this script created three `Webhook` rows to feed the SIS
ticket mirror. Two of them — *ticket created* and *reply added* — have no
receiver any more, and `provision.py` **disables** them if it finds them. Left
enabled they do not fail safely: Frappe's RQ worker retries into a 404 on every
ticket creation and every reply, and Webhook Request Log fills with errors that
read like an integration fault rather than a decommissioned one. Disabled rather
than deleted, so a rollback is a checkbox and the history stays readable.

The third survives, with a narrower job:

| Webhook | DocType | Event | Condition |
|---|---|---|---|
| ticket resolved | `HD Ticket` | `on_update` | `doc.status in ("Resolved", "Closed")` |

It carries six fields — `name`, `status`, `subject`, `sis_user_id`,
`sis_phone_key`, `modified`. A Frappe webhook sends **only** what its Webhook
Data table lists, never the whole document, so an omission here is silent:

- drop `status` and every event looks "not resolved";
- drop `sis_user_id` and tickets raised while signed in stop notifying;
- drop `sis_phone_key` and tickets raised *anonymously* stop notifying — while
  still appearing in that person's portal, which is the confusing half.

`on_update` fires on **every** save, which is why the condition exists — and why
the SIS re-checks the status itself when the payload arrives. A condition is
configuration, and configuration drifts.

### Two Webhook settings that must never be touched

`provision.py` sets `enable_security: 1` and leaves `request_structure`
**unset**. Both were found by tracing a real failure end-to-end during first
setup, and both fail silently if undone — no error, just an unsigned or empty
request that looks fine in every log except the one that matters.

- **`enable_security`** gates the signature entirely.
  `Webhook.get_webhook_headers()` only computes and sends
  `X-Frappe-Webhook-Signature` when it is set; `webhook_secret` sits inert
  otherwise, and is hidden in the Desk UI unless the box is ticked. Leave it off
  and Frappe ships unsigned requests, which the SIS correctly rejects as
  unauthenticated — but the error looks identical to a wrong secret.
- **`request_structure` must not be `"JSON"`.**
  `Webhook.validate_request_body()` unconditionally runs `self.webhook_data = []`
  on every save while that field equals `"JSON"` — the field table and the
  Jinja-templated `webhook_json` field are mutually exclusive, and `"JSON"` is
  Frappe's way of saying "use the template, not the table". Set both and
  `webhook_data` is wiped on save; the webhook then ships `"{}"` as its body —
  still correctly signed, since signing happens over whatever `data` ends up
  being — so the SIS sees a *validly signed empty payload* and rejects it for a
  missing name, not for a bad signature.
- If you hand-edit a row `provision.py` created, a partial update leaves every
  field you did not mention exactly as it was. A row saved once with
  `request_structure: "JSON"` stays stuck losing its field list on every future
  save until something explicitly resets it to `""` — omitting the key is not
  enough. `provision.py` resets it on every run for this reason.

Re-run `provision.py` any time — after a Frappe upgrade, or against a fresh
site. It checks before it creates and updates in place rather than duplicating.

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
SIS compose files. `backend` serves the support routes and receives the webhook;
`celery` runs the notification the webhook enqueues, so it genuinely needs
`HELPDESK_WEBHOOK_SECRET` too.

`SIS_WEBHOOK_URL` must be reachable **from inside** the Frappe containers, and
its hostname must be in the SIS's `ALLOWED_HOSTS`.

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

Going the other way, `SIS_WEBHOOK_URL` must be reachable from inside the Frappe
containers — a container name if both stacks share a network, a public URL
otherwise — and that hostname must be in the SIS's `ALLOWED_HOSTS`. This is the
only outbound call Frappe makes to the SIS.

## 6. Email

Set up an `Email Account` in the Desk on a support mailbox, with **matching
inbound and outbound settings**. Asymmetric configuration is the usual cause of
replies that open new tickets instead of threading onto the existing one.

Frappe owns the email thread; the SIS raises in-app notifications only. Do not
configure both to email the requester or every reply arrives twice.

---

## Verifying it end to end

**1. Anonymous intake.**

```bash
curl -X POST http://localhost:8092/api/v1/support/tickets/ -H "Content-Type: application/json" -d '{"subject":"Test from SIS","description":"Checking the integration.","phone":"0300-1234567"}'
```

Expect `201` with a reference. The ticket should appear in the agent UI with
`0300-1234567` in the Phone field — proof the custom fields are wired. Its
**SIS user id must be empty**: this path never asserts identity.

**2. Anonymous lookup — and type the number differently on purpose.**

```bash
curl -X POST http://localhost:8092/api/v1/support/tickets/lookup/ -H "Content-Type: application/json" -d '{"phone":"+92 300 1234567"}'
```

The ticket from step 1 — raised as `0300-1234567` — must still come back.
That is the `sis_phone_key` normalisation working; an exact-string match would
return nothing here. Subject and status only, no description, no messages.

If it comes back empty, check the ticket in the agent UI: a blank
**Phone (match key)** means `sis_phone_key` did not exist when it was raised.

**3. Authenticated create, then list.** Signed in as a portal user, raise a
ticket from the Helpdesk page, then reload the list. It must appear immediately.
If it does not, open the ticket in the agent UI and look at **SIS user id**:

- **blank** → the `sis_user_id` custom field does not exist, or was created
  after the ticket. Re-run `provision.py`.
- **populated but the list is empty** → the SIS is querying a different id.
  Check that the portal user's `pk` matches the value on the ticket.

**4. The anonymous-to-signed-in handover.** Raise an anonymous ticket using a
phone number that is on a portal user's `Person` record as a `MOBILE` contact:

```bash
curl -X POST http://localhost:8092/api/v1/support/tickets/ -H "Content-Type: application/json" -d '{"subject":"Raised before signing in","description":"x","phone":"<that user's mobile>"}'
```

Now sign in as that user and open the Helpdesk page. The ticket must appear in
the list **and** its thread must open. This is the whole reason `sis_phone_key`
is an ownership signal and not just a lookup key.

If it does not appear, check in order: the user's `Person` has a `MOBILE`
contact (not `HOME`, `WORK` or `EMERGENCY` — those are deliberately ignored);
the number has at least 10 digits; the ticket's **Phone (match key)** in the
agent UI matches that number's last 10 digits.

**5. Ownership isolation. Do not skip this one.** Take the ticket id from step
3 and request its thread while signed in as a *different* user:

```bash
curl -H "Cookie: sessionid=<other user's session>" http://localhost:8092/api/v1/support/my-tickets/<ticket-name>/thread/
```

Expect `404` — not `403`, and not `200`. A `403` would confirm the id refers to
someone's ticket; a `200` means the ownership check is not running and every
ticket in the system is readable by every signed-in user.

Then repeat with all three of these, each of which must also be `404`:

- the ticket from step 4, requested by a user whose mobile is **not** that
  number — a phone match must be specific, not a skeleton key;
- the anonymous ticket from step 1, requested by a user with **no** `MOBILE`
  contact at all — blank must never match blank;
- any ticket id at all, requested with no session — `401`/`403` is fine here,
  the point is that it is never `200`.

**6. The resolved-ticket notification.** Resolve the ticket from step 3 in the
agent UI, then check the requester's notification bell in the portal.

Diagnose in this order — each stage has its own evidence:

| Symptom | Look at | Likely cause |
|---|---|---|
| No request left Frappe | **Webhook Request Log** in the Desk | webhook disabled, or its condition never matched |
| `401` in the log | the two secrets | `WEBHOOK_SECRET` ≠ `HELPDESK_WEBHOOK_SECRET`, or `enable_security` unticked |
| `400` in the log | the payload in the log | `webhook_data` was wiped — see `request_structure` above |
| `202` but no notification | SIS `celery` logs | see below |
| `404` in the log | the `support` waffle Switch | module is off |

A `202` means the SIS accepted and enqueued it; everything after that is the
worker's. The failure to expect there is:

```
Received unregistered task of type 'support.process_ticket_event'
```

Celery workers do **not** hot-reload. A worker that was running before this
integration was deployed has never heard of the task and will reject every one.
Restart `celery` after any deploy that adds or renames a task.

## Operating notes

- **Back up the `sites` volume, not just the database.** Attachments live on
  disk; a database-only backup loses every file.
- **Pin versions.** Both `apps.json` and `build-image.sh` are pinned. Keep them
  that way — a Helpdesk minor bump can rename API methods.
- **Helpdesk being down is now user-visible.** With no mirror, every portal
  read is a live call: a Frappe outage turns the ticket list and thread into a
  `503 support_unavailable` rather than serving stale-but-present rows. That is
  the trade the no-mirror design makes.
- **Watch `Webhook Request Log`** when a notification did not arrive. It records
  every attempt and Frappe's view of the response, and it is the first place to
  look — it tells you immediately whether the problem is on Frappe's side of the
  wire or the SIS's.
- **Restart `celery` after deploying a new task.** Workers register tasks at
  import time and never reload; a stale worker rejects `support.process_ticket_event`
  with `KeyError` while the webhook itself keeps returning a perfectly healthy
  `202`.
- **Anonymous requesters get no in-app notification** — there is no account to
  notify. Frappe's own email to the address on the ticket is the only channel
  that reaches them, which is why an outgoing Email Account is not optional.
- **Timezones.** Frappe stores naive datetimes in the site timezone; Django is
  UTC-aware. `HELPDESK_FRAPPE_TIMEZONE` drives the conversion, which happens in
  `support/services/frappe_time.py` and nowhere else.
- **A phone number is an ownership signal, not just a lookup key.** Two
  consequences, both accepted deliberately. First, `POST
  /api/v1/support/tickets/lookup/` is unauthenticated: it returns subjects and
  statuses to anyone who knows a number (message bodies stay behind the
  authenticated thread endpoint). Second, and sharper: a ticket raised from the
  public form claiming someone's number will appear in *that* person's portal,
  thread included, once they sign in. Both follow from letting an anonymous
  ticket be recoverable at all. The single fix for both is an SMS one-time code
  in front of the phone signal — which is also the point at which a real E.164
  parser starts to earn its keep.
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
- **Backfill `sis_phone_key` on historical tickets.** Everything raised before
  the switchover has a blank key, so it is invisible to both the anonymous
  lookup and the signed-in phone match. This backfill is safe: it is pure
  normalisation of `sis_phone`, data already on the ticket, adding no new claim.
  `sis_user_id` is the opposite — inferring it from a phone match would be
  manufacturing proof out of a claim, so leave it blank.
