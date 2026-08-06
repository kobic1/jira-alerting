#!/usr/bin/env python3
"""Send an email through Kobi's Power Automate HTTP flow ("manual" trigger -> Send an email V2).

Durable, dependency-free (Python standard library only). This is the authoritative copy —
it lives in a normal home-directory folder so it survives across sessions. Any CoWork task,
scheduled routine, or skill that needs to send mail should call THIS path:

    /Users/Kobi.Cohen/cowork-email/send_email.py

The flow accepts this JSON body:
    { "to": str, "cc": str, "subject": str, "body": str (HTML), "importance": str }

Only `to`, `subject`, and `body` are required by the mailer. `cc` and `importance`
are optional. `importance` must be one of Low / Normal / High (defaults to Normal).

The trigger URL contains a `sig=` signature that acts as a secret key — anyone who can run
this can send mail through the flow. To override the baked-in URL (e.g. after regenerating
the signature), set the POWER_AUTOMATE_EMAIL_URL environment variable.

Examples:
    python3 send_email.py --to a@x.com --subject "Hi" --body "<p>Hello</p>"
    python3 send_email.py --to a@x.com --cc b@x.com --subject "Report" \
        --body-file /path/to/body.html --importance High
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error

DEFAULT_URL = None  # intentionally absent: this copy lives in a PUBLIC repo.
# The flow trigger URL carries its own sig= signature and is a credential, so it is
# supplied only via the POWER_AUTOMATE_EMAIL_URL env var (a GitHub Actions secret).

VALID_IMPORTANCE = {"Low", "Normal", "High"}


def main() -> int:
    p = argparse.ArgumentParser(description="Send an email via the Power Automate flow.")
    p.add_argument("--to", required=True, help="Recipient(s). Semicolon-separated for multiple.")
    p.add_argument("--subject", required=True, help="Email subject.")
    body = p.add_mutually_exclusive_group(required=True)
    body.add_argument("--body", help="Email body as an HTML string.")
    body.add_argument("--body-file", help="Path to a file containing the HTML body.")
    p.add_argument("--cc", default="", help="CC recipient(s). Semicolon-separated.")
    p.add_argument(
        "--importance",
        default="Normal",
        choices=sorted(VALID_IMPORTANCE),
        help="Message importance (default: Normal).",
    )
    p.add_argument("--url", default=os.environ.get("POWER_AUTOMATE_EMAIL_URL", DEFAULT_URL),
                   help="The flow trigger URL. Normally set via POWER_AUTOMATE_EMAIL_URL.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the payload that would be sent and exit without sending.")
    args = p.parse_args()

    if args.body_file:
        try:
            with open(args.body_file, "r", encoding="utf-8") as f:
                body_html = f.read()
        except OSError as e:
            print(f"ERROR: could not read --body-file: {e}", file=sys.stderr)
            return 2
    else:
        body_html = args.body

    payload = {
        "to": args.to,
        "cc": args.cc,
        "subject": args.subject,
        "body": body_html,
        "importance": args.importance,
    }

    if args.dry_run:
        print("DRY RUN — payload that would be POSTed:")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if not args.url:
        print("ERROR: no flow URL. Set POWER_AUTOMATE_EMAIL_URL (a GitHub Actions secret) "
              "or pass --url. This copy carries no default because the repo is public.",
              file=sys.stderr)
        return 2

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        args.url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.getcode()
            resp_body = resp.read().decode("utf-8", errors="replace").strip()
            print(f"OK: HTTP {status} — email accepted by the flow.")
            if resp_body:
                print(f"Response body: {resp_body}")
            return 0
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace").strip()
        print(f"ERROR: HTTP {e.code} {e.reason}", file=sys.stderr)
        if detail:
            print(f"Response body: {detail}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"ERROR: could not reach the flow endpoint: {e.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
