#!/usr/bin/env python3
"""One-time (and safely repeatable) configuration of a Frappe Helpdesk site.

Creates the pieces the SIS integration depends on:

  1. Six Custom Fields on HD Ticket — the identifiers a ticket is later found
     by, plus what the requester claimed at intake.
  2. One Webhook, firing when a ticket is resolved or closed, so the SIS can
     raise an in-app notification for the requester. This is the only thing
     Frappe pushes back: there is no ticket mirror to keep up to date.

It also *disables* the two other webhook rows earlier versions of this script
created (ticket created, reply added). Those fed the mirror and now point at
nothing; left enabled, Frappe retries them against a 404 forever and fills
Webhook Request Log with noise.

Everything is idempotent — it checks before it creates, and updates in place
when a value has drifted — so running it after a Frappe upgrade or against a
fresh site is always safe.

Deliberately driven through Frappe's REST API rather than a custom Frappe app:
no image rebuild, no bench access needed, and it exercises the exact API path
the SIS itself uses, so a credential or Host-header problem surfaces here rather
than in production.

Usage:
    pip install requests
    python scripts/provision.py                # apply
    python scripts/provision.py --dry-run      # show what would change

Reads configuration from ../.env (or the environment).
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from typing import Any

try:
    import requests
except ImportError:
    sys.exit("This script needs `requests`:  pip install requests")

HERE = pathlib.Path(__file__).resolve().parent
ENV_FILE = HERE.parent / ".env"

TICKET_DOCTYPE = "HD Ticket"

# These fieldnames are hardcoded in the SIS at
# support/services/frappe_client.py (TicketPayload.to_doc). They must match
# exactly — Frappe silently drops unknown keys on insert, so a typo here shows
# up as tickets that simply have no phone number on them.
#
# `sis_user_id` additionally carries the whole ownership model: the SIS queries
# HD Ticket by it to build "My Tickets", so a missing field here does not fail
# loudly — it just makes every portal ticket list permanently empty.
CUSTOM_FIELDS: list[dict[str, Any]] = [
    {
        "fieldname": "sis_section_break",
        "label": "SIS",
        "fieldtype": "Section Break",
        "insert_after": "description",
    },
    {
        "fieldname": "sis_phone",
        "label": "Phone (claimed)",
        "fieldtype": "Data",
        "options": "Phone",
        "insert_after": "sis_section_break",
        "in_standard_filter": 1,
        "description": "As the requester typed it. What to dial — not proof of identity.",
    },
    {
        "fieldname": "sis_phone_key",
        "label": "Phone (match key)",
        "fieldtype": "Data",
        "insert_after": "sis_phone",
        "read_only": 1,
        "in_standard_filter": 1,
        "description": (
            "The phone number's last 10 digits, written by the SIS at intake. "
            "This — never sis_phone — is what the anonymous ticket lookup "
            "matches on: '+923001234567' and '0300-1234567' are one number and "
            "two strings, and Frappe cannot normalise inside a filter."
        ),
    },
    {
        "fieldname": "sis_email",
        "label": "Email (claimed)",
        "fieldtype": "Data",
        "options": "Email",
        "insert_after": "sis_phone_key",
    },
    {
        "fieldname": "sis_admission_no",
        "label": "Admission / roll number",
        "fieldtype": "Data",
        "insert_after": "sis_email",
        "description": "Optional. Breaks the tie when one phone number covers several siblings.",
    },
    {
        "fieldname": "sis_campus_id",
        "label": "SIS campus id",
        "fieldtype": "Data",
        "insert_after": "sis_admission_no",
        "read_only": 1,
    },
    {
        "fieldname": "sis_user_id",
        "label": "SIS user id",
        "fieldtype": "Data",
        "insert_after": "sis_campus_id",
        "read_only": 1,
        "in_standard_filter": 1,
        "description": (
            "The SIS user who raised this ticket while signed in. Written by "
            "the SIS and by nothing else, which is what makes it safe to serve "
            "a portal ticket list from. Blank on anonymously-raised tickets — "
            "and a blank value must never match a caller."
        ),
    },
]

def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    env.update({k: v for k, v in os.environ.items() if k in {
        "FRAPPE_URL", "SITE_NAME", "ADMIN_API_KEY", "ADMIN_API_SECRET",
        "SIS_WEBHOOK_URL", "WEBHOOK_SECRET",
    }})
    return env


class Frappe:
    def __init__(self, base_url: str, key: str, secret: str, site_host: str = ""):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {key}:{secret}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        # Frappe picks the site from the Host header. Harmless when reaching the
        # site by its real hostname; essential when reaching it any other way.
        if site_host:
            self.session.headers["Host"] = site_host

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def get(self, doctype: str, name: str) -> dict | None:
        r = self.session.get(self._url(f"/api/resource/{doctype}/{name}"), timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("data")

    def find(self, doctype: str, filters: list, fields: list[str] | None = None) -> dict | None:
        """One matching row.

        ``fields`` is not optional in spirit: a Frappe list query with no field
        list returns **only** ``name``. Ask for a field you did not request and
        you get ``None`` back, silently — which reads as "this value is unset"
        rather than "you did not fetch it". That mistake is what let
        ``disable_obsolete_webhooks`` report "already disabled" for rows that
        were in fact enabled.
        """
        import json as _json

        params = {"filters": _json.dumps(filters), "limit_page_length": 1}
        if fields:
            params["fields"] = _json.dumps(fields)
        r = self.session.get(
            self._url(f"/api/resource/{doctype}"), params=params, timeout=30
        )
        r.raise_for_status()
        rows = r.json().get("data") or []
        return rows[0] if rows else None

    def insert(self, doc: dict) -> dict:
        r = self.session.post(
            self._url(f"/api/resource/{doc['doctype']}"), json=doc, timeout=30
        )
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} creating {doc['doctype']}: {r.text[:800]}")
        return r.json().get("data") or {}

    def update(self, doctype: str, name: str, values: dict) -> dict:
        r = self.session.put(
            self._url(f"/api/resource/{doctype}/{name}"), json=values, timeout=30
        )
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} updating {doctype} {name}: {r.text[:800]}")
        return r.json().get("data") or {}

    def whoami(self) -> str:
        r = self.session.get(self._url("/api/method/frappe.auth.get_logged_user"), timeout=30)
        r.raise_for_status()
        return r.json().get("message", "")


def ensure_custom_fields(api: Frappe, dry_run: bool) -> None:
    print(f"\n== Custom Fields on {TICKET_DOCTYPE} ==")
    for spec in CUSTOM_FIELDS:
        name = f"{TICKET_DOCTYPE}-{spec['fieldname']}"
        existing = api.get("Custom Field", name)
        if existing:
            print(f"   ok       {spec['fieldname']}")
            continue
        if dry_run:
            print(f"   +create  {spec['fieldname']} ({spec['fieldtype']})")
            continue
        api.insert({"doctype": "Custom Field", "dt": TICKET_DOCTYPE, **spec})
        print(f"   created  {spec['fieldname']}")


# Payload for the one webhook that still exists. Frappe sends ONLY the fields
# listed in a webhook's Webhook Data table — never the whole document — so an
# omission here is silent: the SIS receives a payload it cannot act on.
#
#   name           which ticket. Without it there is nothing to identify.
#   status         the SIS notifies only on Resolved/Closed, and this is how it
#                  tells. Without it every event looks like "not resolved".
#   sis_user_id    who to notify, when the ticket was raised from a session.
#   sis_phone_key  who to notify otherwise — the SIS resolves the person whose
#                  own mobile number this is, the same signal the portal ticket
#                  list uses. Omit it and every anonymously-raised ticket goes
#                  quiet on resolution while still being visible in that
#                  person's portal.
#   subject        display text in the notification.
#   modified       not used for correctness (the SIS keys idempotency off
#                  name+status), but invaluable when reading Webhook Request Log.
TICKET_EVENT_FIELDS = [
    "name",
    "status",
    "subject",
    "sis_user_id",
    "sis_phone_key",
    "modified",
]

# `on_update` fires on every save. The condition keeps the noise off the wire;
# the SIS re-checks the status anyway, because a condition is configuration and
# configuration drifts.
TICKET_EVENT_CONDITION = 'doc.status in ("Resolved", "Closed")'

WEBHOOK_NAME = "SIS - HD Ticket - Resolved"


def ticket_event_webhook(url: str, secret: str) -> dict:
    """The Webhook row that tells the SIS a ticket closed.

    Two settings here are not cosmetic and both fail *silently* if undone —
    each was found by tracing a real end-to-end failure during first setup:

    `enable_security` gates the signature entirely.
    ``Webhook.get_webhook_headers()`` only computes and sends
    ``X-Frappe-Webhook-Signature`` when it is set; ``webhook_secret`` sits inert
    otherwise, and is hidden in the Desk UI unless the box is ticked. Leave it
    off and Frappe ships unsigned requests, which the SIS correctly rejects as
    unauthenticated — but the error is indistinguishable from a wrong secret.

    `request_structure` must NOT be "JSON". ``Webhook.validate_request_body()``
    unconditionally runs ``self.webhook_data = []`` on every save while that
    field equals "JSON" — the field table and the Jinja-templated
    ``webhook_json`` field are mutually exclusive, and "JSON" is Frappe's way of
    saying "use the template, not the table". Set both and ``webhook_data`` is
    wiped on save; the webhook then ships "{}" as its body — correctly signed,
    since signing happens over whatever ``data`` ends up being — so the SIS sees
    a *validly signed empty payload* and rejects it for a missing name, not for
    a bad signature.

    Webhook's autoname is "prompt", so a name must be supplied or ``insert()``
    417s with "Please set the document name".
    """
    return {
        "doctype": "Webhook",
        "name": WEBHOOK_NAME,
        "webhook_doctype": TICKET_DOCTYPE,
        "webhook_docevent": "on_update",
        "request_url": url,
        "request_method": "POST",
        "enable_security": 1,
        "webhook_secret": secret,
        "condition": TICKET_EVENT_CONDITION,
        "enabled": 1,
        "webhook_data": [{"fieldname": f, "key": f} for f in TICKET_EVENT_FIELDS],
    }


def ensure_ticket_webhook(api: Frappe, url: str, secret: str, dry_run: bool) -> None:
    """Create or refresh the resolved/closed notification webhook."""
    print("\n== Webhook -> SIS (ticket resolved) ==")
    spec = ticket_event_webhook(url, secret)
    existing = api.find(
        "Webhook",
        [
            ["webhook_doctype", "=", TICKET_DOCTYPE],
            ["webhook_docevent", "=", "on_update"],
            ["request_url", "=", url],
        ],
    )
    if existing:
        if dry_run:
            print("   ok       resolved/closed (would refresh secret and field list)")
            return
        # Refreshed in place: a rotated secret or an added field has to reach a
        # row that already exists. `request_structure` is explicitly reset to ""
        # rather than omitted — a row created before this was understood has it
        # stuck at "JSON", and merely leaving the key out of an update keeps the
        # stale value, so the row would go on silently losing its field list on
        # every future save.
        api.update(
            "Webhook",
            existing["name"],
            {
                "enable_security": 1,
                "webhook_secret": secret,
                "enabled": 1,
                "request_structure": "",
                "condition": TICKET_EVENT_CONDITION,
                "webhook_data": spec["webhook_data"],
            },
        )
        print(f"   updated  resolved/closed  [{existing['name']}]")
        return
    if dry_run:
        print("   +create  resolved/closed: HD Ticket / on_update")
        return
    created = api.insert(spec)
    print(f"   created  resolved/closed  [{created.get('name')}]")


def disable_obsolete_webhooks(api: Frappe, dry_run: bool) -> None:
    """Turn off the two webhooks that no longer have a receiver.

    Earlier versions of this script created three rows, all feeding a SIS-side
    ticket mirror that no longer exists. Only the ticket ``on_update`` row still
    has a purpose (notifying a requester their ticket closed) and is handled by
    ``ensure_ticket_webhook``; these two do not.

    A row left enabled does not fail safely — Frappe's RQ worker retries into a
    404 on every ticket creation and every reply, and Webhook Request Log fills
    with errors that read like an integration fault rather than a decommissioned
    one. Disabled rather than deleted, so a rollback is a checkbox and the
    request history stays readable.
    """
    print("\n== Obsolete webhooks ==")
    obsolete = [
        ("ticket created", TICKET_DOCTYPE, "after_insert"),
        ("reply added", "Communication", "after_insert"),
    ]
    found = False
    for label, doctype, event in obsolete:
        row = api.find(
            "Webhook",
            [
                ["webhook_doctype", "=", doctype],
                ["webhook_docevent", "=", event],
                ["request_url", "like", "%/support/webhooks/frappe/%"],
            ],
            fields=["name", "enabled"],
        )
        if not row:
            continue
        found = True
        if not row.get("enabled"):
            print(f"   ok       {label} (already disabled)")
            continue
        if dry_run:
            print(f"   -disable {label}  [{row['name']}]")
            continue
        api.update("Webhook", row["name"], {"enabled": 0})
        print(f"   disabled {label}  [{row['name']}]")
    if not found:
        print("   ok       none present")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="show changes without applying")
    args = parser.parse_args()

    env = load_env()
    missing = [k for k in ("FRAPPE_URL", "ADMIN_API_KEY", "ADMIN_API_SECRET")
               if not env.get(k)]
    if missing:
        print("Missing required settings in .env: " + ", ".join(missing), file=sys.stderr)
        print("See .env.example and docs/helpdesk-setup.md.", file=sys.stderr)
        return 2

    webhook_url = env.get("SIS_WEBHOOK_URL", "").strip()
    webhook_secret = env.get("WEBHOOK_SECRET", "").strip()

    api = Frappe(
        env["FRAPPE_URL"], env["ADMIN_API_KEY"], env["ADMIN_API_SECRET"],
        site_host=env.get("SITE_NAME", ""),
    )

    try:
        user = api.whoami()
    except Exception as exc:
        print(f"Could not reach Frappe at {env['FRAPPE_URL']}: {exc}", file=sys.stderr)
        return 1
    print(f"Connected to {env['FRAPPE_URL']} as {user}"
          + (" (DRY RUN — nothing will be written)" if args.dry_run else ""))

    try:
        ensure_custom_fields(api, args.dry_run)
        disable_obsolete_webhooks(api, args.dry_run)
        # Optional: the custom fields are the hard requirement, the notification
        # webhook is not. Skipping rather than failing keeps this script usable
        # for a fields-only run against a site whose SIS URL is not known yet.
        if webhook_url and webhook_secret:
            ensure_ticket_webhook(api, webhook_url, webhook_secret, args.dry_run)
        else:
            print("\n== Webhook -> SIS (ticket resolved) ==")
            print("   skipped  SIS_WEBHOOK_URL / WEBHOOK_SECRET not set")
    except Exception as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        return 1

    print("\nDone." if not args.dry_run else "\nDry run complete.")
    print("Reminder: sis_user_id must exist before the SIS is switched over, or")
    print("every portal ticket list comes back empty with no error anywhere.")
    if webhook_url and webhook_secret:
        print("WEBHOOK_SECRET here must equal HELPDESK_WEBHOOK_SECRET in the SIS,")
        print("or every resolved-ticket notification is rejected with a 401.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
