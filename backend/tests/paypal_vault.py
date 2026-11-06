"""Check why PayPal refuses to save a PayPal account (Vault v3). Run from backend/:

    python scripts/check_paypal_vault.py

It uses the keys in backend/.env, prints nothing secret, and changes nothing you'd notice: it only asks
PayPal for an access token and tries to create one setup token (which simply expires unused).
"""
import json
import os
import sys
import uuid

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config  # noqa: E402  (loads backend/.env)

base = config.PAYPAL_BASE.rstrip("/")
cid, secret = config.PAYPAL_CLIENT_ID, config.PAYPAL_CLIENT_SECRET
print(f"PayPal API:      {base}")
print(f"PAYPAL_MODE:     {config.PAYPAL_MODE}")
print(f"Client ID:       {cid[:6]}...{cid[-4:]}  (check this matches the NEW US app)" if cid else "Client ID:       MISSING")
if not (cid and secret):
    sys.exit("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set in backend/.env")

with httpx.Client(timeout=30) as http:
    r = http.post(f"{base}/v1/oauth2/token", data={"grant_type": "client_credentials"}, auth=(cid, secret))
    if r.status_code >= 400:
        sys.exit(f"Login to PayPal failed ({r.status_code}): {r.text}")
    tok = r.json()
    hours = int(tok.get("expires_in", 0)) / 3600
    print(f"Token valid for: {hours:.1f} h  (a brand-new token is ~9 h; less means PayPal handed back an older one)")
    # PayPal can keep returning a token issued before you ticked new features. Ask for a fresh one and compare.
    r2 = http.post(f"{base}/v1/oauth2/token", data={"grant_type": "client_credentials", "ignoreCache": "true"},
                   auth=(cid, secret))
    if r2.status_code < 400:
        fresh = r2.json()
        same = fresh.get("access_token") == tok.get("access_token")
        print(f"Fresh token:     {'same token as before' if same else 'NEW token'}, "
              f"valid {int(fresh.get('expires_in', 0)) / 3600:.1f} h")
        if not same:
            tok = fresh  # use the fresh one for the checks below
    scopes = tok.get("scope", "").split()
    vault = [s for s in scopes if "vault" in s]
    print(f"App ID:          {tok.get('app_id')}")
    print(f"Vault scopes:    {vault or 'NONE  <-- PayPal has not given this app Vault permission'}")
    print(f"Invoicing scope: {'yes' if any('invoicing' in s for s in scopes) else 'NO'}")

    body = {"payment_source": {"paypal": {
        "usage_type": "MERCHANT", "customer_type": "CONSUMER",
        "experience_context": {"return_url": "https://example.com/return", "cancel_url": "https://example.com/cancel"},
    }}}
    r = http.post(f"{base}/v3/vault/setup-tokens", json=body,
                  headers={"Authorization": f"Bearer {tok['access_token']}", "Content-Type": "application/json",
                           "PayPal-Request-Id": f"check-{uuid.uuid4().hex}"})
    print(f"\nTest setup token: HTTP {r.status_code}")
    print(json.dumps(r.json(), indent=2) if r.content else "(empty)")
    print("\nOK: Vault works for this app." if r.status_code < 300 else
          "\nRefused. Send the lines above (they include PayPal's debug_id) to keep digging.")