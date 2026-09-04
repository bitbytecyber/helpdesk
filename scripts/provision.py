#!/usr/bin/env python3
"""One-time (and safely repeatable) configuration of a Frappe Helpdesk site.

Creates the pieces the SIS integration depends on:""

  1. Five Custom Fields on HD Ticket, carrying what a requester claimed at intake.
  2. Three Webhook rows pointing back at the SIS, all signed with a shared secret.

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
        "fieldname": "sis_email",
        "label": "Email (claimed)",
        "fieldtype": "Data",
        "options": "Email",
        "insert_after": "sis_phone",
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
        "fieldname": "sis_link_status",
        "label": "SIS link status",
        "fieldtype": "Data",
        "insert_after": "sis_campus_id",
        "read_only": 1,
        "description": (
            "How far the ticket->family link is trusted. Anything other than "
            "verified_* came from an unproven phone number: treat it as a "
            "suggestion, and confirm identity before disclosing anything."
        ),
    },
]

# Payloads carry ONLY the fields listed here. Omitting `name` or `modified` is
# the single most common webhook misconfiguration: the SIS then cannot identify
# the ticket, or cannot tell a duplicate delivery from a real update.
TICKET_WEBHOOK_FIELDS = [
    "name",
    "subject",
    "status",
    "priority",
    "modified",
    "opening_date",
    "first_responded_on",
    "resolution_date",
    "sis_phone",
    "sis_email",
    "sis_admission_no",
    "sis_campus_id",
    "sis_link_status",
]

# A Communication row does not know it is "a ticket reply" — it points at its
# parent through reference_doctype/reference_name. Without those two the SIS
# receives a message body with nothing to attach it to.
COMMUNICATION_WEBHOOK_FIELDS = [
    "name",
    "reference_doctype",
    "reference_name",
    "communication_type",
    "sent_or_received",
    "modified",
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

    def find(self, doctype: str, filters: list) -> dict | None:
        import json as _json

        r = self.session.get(
            self._url(f"/api/resource/{doctype}"),
            params={"filters": _json.dumps(filters), "limit_page_length": 1},
            timeout=30,
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


def webhook_spec(name: str, doctype: str, event: str, fields: list[str], url: str, secret: str,
                 condition: str = "") -> dict:
    # Webhook's autoname is "prompt" (naming_rule: "Set by user") — Frappe
    # will not assign a name on its own, and insert() 417s with "Please set
    # the document name" if one isn't supplied.
    #
    # enable_security is not cosmetic: frappe/integrations/doctype/webhook/
    # webhook.py only computes and sends X-Frappe-Webhook-Signature when this
    # is set — webhook_secret sits inert (and is even hidden in the Desk UI,
    # via depends_on: eval:doc.enable_security==1) otherwise. Omitting it
    # doesn't error; Frappe just silently ships unsigned requests, which the
    # SIS then correctly rejects as unauthenticated. Confirmed by reading the
    # signing code directly: it reuses the same frappe.as_json(data) for both
    # the signature and the request body, so there's no other place this can
    # drift once security is actually turned on.
    # request_structure is deliberately left unset (NOT "JSON"). Reading
    # Webhook.validate_request_body() directly: when request_structure ==
    # "JSON" it unconditionally does `self.webhook_data = []` on every save —
    # the field list is then expected to come from a separate Jinja-templated
    # `webhook_json` field instead. get_webhook_data() only falls back to
    # webhook_json when webhook_data is empty, so setting both (as an earlier
    # version of this script did) silently produces an EMPTY payload: Frappe
    # sends "{}" as the body, `enable_security` was still added as its digest,
    # so the request even carries a *valid* signature over nothing — the SIS
    # then rejects it for real, but for "no name in payload", not "bad sig".
    # This costs nothing: enqueue_webhook always sends `frappe.as_json(data)`
    # regardless of request_structure, so leaving it unset changes nothing
    # about how the bytes are transmitted — only how the field list survives.
    return {
        "doctype": "Webhook",
        "name": name,
        "webhook_doctype": doctype,
        "webhook_docevent": event,
        "request_url": url,
        "request_method": "POST",
        "enable_security": 1,
        "webhook_secret": secret,
        "condition": condition,
        "enabled": 1,
        "webhook_data": [{"fieldname": f, "key": f} for f in fields],
    }


def ensure_webhooks(api: Frappe, url: str, secret: str, dry_run: bool) -> None:
    print("\n== Webhooks -> SIS ==")
    wanted = [
        ("ticket created", webhook_spec(
            "SIS - HD Ticket - After Insert",
            TICKET_DOCTYPE, "after_insert", TICKET_WEBHOOK_FIELDS, url, secret)),
        ("ticket changed", webhook_spec(
            "SIS - HD Ticket - On Update",
            TICKET_DOCTYPE, "on_update", TICKET_WEBHOOK_FIELDS, url, secret)),
        # Scoped, or the SIS receives every email in the system.
        ("reply added", webhook_spec(
            "SIS - Communication - After Insert",
            "Communication", "after_insert", COMMUNICATION_WEBHOOK_FIELDS, url, secret,
            condition='doc.reference_doctype == "HD Ticket"')),
    ]

    for label, spec in wanted:
        existing = api.find("Webhook", [
            ["webhook_doctype", "=", spec["webhook_doctype"]],
            ["webhook_docevent", "=", spec["webhook_docevent"]],
            ["request_url", "=", url],
        ])
        if existing:
            if dry_run:
                print(f"   ok       {label} (would refresh secret and field list)")
            else:
                # Refresh in place: a rotated secret or an added custom field
                # has to reach an already-created webhook.
                #
                # request_structure is explicitly reset to "" here, not
                # merely omitted: a row created before this fix has it stuck
                # at "JSON", and Webhook.validate_request_body() wipes
                # webhook_data on every future save as long as that value
                # remains — omitting the key from this payload would leave
                # the stale value in place and the row would keep silently
                # losing its field list each time provision.py runs.
                api.update("Webhook", existing["name"], {
                    "enable_security": 1,
                    "webhook_secret": secret,
                    "enabled": 1,
                    "request_structure": "",
                    "webhook_data": spec["webhook_data"],
                    "condition": spec["condition"],
                })
                print(f"   updated  {label}  [{existing['name']}]")
            continue
        if dry_run:
            print(f"   +create  {label}: {spec['webhook_doctype']} / {spec['webhook_docevent']}")
            continue
        created = api.insert(spec)
        print(f"   created  {label}  [{created.get('name')}]")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="show changes without applying")
    args = parser.parse_args()

    env = load_env()
    missing = [k for k in ("FRAPPE_URL", "ADMIN_API_KEY", "ADMIN_API_SECRET",
                           "SIS_WEBHOOK_URL", "WEBHOOK_SECRET") if not env.get(k)]
    if missing:
        print("Missing required settings in .env: " + ", ".join(missing), file=sys.stderr)
        print("See .env.example and docs/helpdesk-setup.md.", file=sys.stderr)
        return 2

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
        ensure_webhooks(api, env["SIS_WEBHOOK_URL"], env["WEBHOOK_SECRET"], args.dry_run)
    except Exception as exc:
        print(f"\nFailed: {exc}", file=sys.stderr)
        return 1

    print("\nDone." if not args.dry_run else "\nDry run complete.")
    print("Reminder: WEBHOOK_SECRET here must equal HELPDESK_WEBHOOK_SECRET in the SIS,")
    print("or every delivery is rejected with a 401.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
