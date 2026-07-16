#!/usr/bin/env python3
"""One-time interactive login to seed the snooze-flow bearer token.

The snooze flow's HTTP trigger is OAuth ("any user in my tenant"), so posting
the Adaptive Card requires an Azure AD bearer token. At runtime
``TeamsPowerAutomateSender._bearer_token()`` only refreshes *silently* from a
cached MSAL token at ``~/.jira_alerting_token.json`` — it can't do the initial
sign-in. Run this once (it needs a browser + you signing in) to create that
cache; afterwards the daily pipeline refreshes it on its own.

    python3 seed_snooze_token.py

Uses the same public client, scope, and cache path the sender reads, so no app
registration is needed.
"""
from __future__ import annotations

import os
import sys

import msal

CLIENT_ID = "1950a258-227b-4e31-a9cf-717495945fc2"  # Azure public client (matches teams.py)
SCOPES = ["https://service.flow.microsoft.com//.default"]
CACHE_PATH = os.path.expanduser("~/.jira_alerting_token.json")


def main() -> int:
    cache = msal.SerializableTokenCache()
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            cache.deserialize(f.read())

    app = msal.PublicClientApplication(
        CLIENT_ID,
        authority="https://login.microsoftonline.com/common",
        token_cache=cache,
    )

    # Reuse an existing account silently if the cache already has one.
    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if not result or "access_token" not in result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            print("Failed to start device flow:", flow.get("error_description", flow), file=sys.stderr)
            return 1
        print("\n" + flow["message"] + "\n")  # "go to https://microsoft.com/devicelogin and enter CODE"
        result = app.acquire_token_by_device_flow(flow)  # blocks until you finish signing in

    if not result or "access_token" not in result:
        print("Login failed:", (result or {}).get("error_description", "unknown error"), file=sys.stderr)
        return 1

    if cache.has_state_changed:
        with open(CACHE_PATH, "w") as f:
            f.write(cache.serialize())
        os.chmod(CACHE_PATH, 0o600)

    print(f"✅ Token cached at {CACHE_PATH} — the snooze flow can now be called.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
