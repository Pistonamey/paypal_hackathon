"""Can Procuro be a PayPal platform for its sellers? A 30-minute sandbox check. Run from backend/.

Step 1: make a sign-up link (PayPal "partner referral"):

    python scripts/check_partner_signup.py link

  Open the printed link in an INCOGNITO window and log in as a sandbox BUSINESS account that is not your
  main one (NorthBox's is ideal). Approve. PayPal sends you to a page whose address contains
  merchantIdInPayPal=XXXXXXXX. Copy that value. (It's also that sandbox account's "Account ID" in the
  developer dashboard.)

Step 2: pay that seller through Procuro, with your saved PayPal and a platform fee:

    python scripts/check_partner_signup.py pay --merchant XXXXXXXX

  Using your PayPal app's keys (backend/.env) and the PayPal you saved in Procuro (Vault), it:
    1. creates a $1.00 order to that seller (intent AUTHORIZE, payee = the seller), held at once
    2. captures it with a $0.10 platform fee for Procuro
  and prints what PayPal said at each step. Afterwards, the seller's sandbox account shows $1.00 received
  minus the fee, and your main account shows the $0.10 fee.

Sandbox only: it refuses to run against live PayPal. Prints nothing secret.
"""
import argparse
import json
import os
import sys
import time
import uuid

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config  # noqa: E402  (loads backend/.env)

BASE = config.PAYPAL_BASE.rstrip("/")


def show(label: str, r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:500]}
    ok = r.status_code < 400
    print(f"\n{'OK ' if ok else 'NO '} {label}: HTTP {r.status_code}")
    if not ok:
        print("   ", json.dumps({k: body.get(k) for k in ("name", "message", "details", "debug_id") if k in body},
                                indent=2)[:1500])
    return body if ok else {}


def token(http: httpx.Client) -> str:
    r = http.post(f"{BASE}/v1/oauth2/token", data={"grant_type": "client_credentials", "ignoreCache": "true"},
                  auth=(config.PAYPAL_CLIENT_ID, config.PAYPAL_CLIENT_SECRET))
    if r.status_code >= 400:
        sys.exit(f"Login to PayPal failed ({r.status_code}): {r.text[:300]}")
    scopes = r.json().get("scope", "")
    print("Partner (platform) scopes on this app's token:")
    for s in ("https://uri.paypal.com/services/customer/partner-referrals",
              "https://uri.paypal.com/services/customer/merchant-integrations/readonly",
              "https://uri.paypal.com/services/vault/payment-tokens/read"):
        print(f"   {'yes' if s in scopes else 'NO '}  {s.rsplit('/services/', 1)[-1]}")
    return r.json()["access_token"]


def headers(tok: str, request_id: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json", "Prefer": "return=representation"}
    if request_id:
        h["PayPal-Request-Id"] = request_id
    return h


def link(http: httpx.Client, tok: str) -> None:
    body = {
        "tracking_id": f"procuro-seller-{int(time.time())}",
        "operations": [{"operation": "API_INTEGRATION", "api_integration_preference": {"rest_api_integration": {
            "integration_method": "PAYPAL", "integration_type": "THIRD_PARTY",
            "third_party_details": {"features": ["PAYMENT", "REFUND", "PARTNER_FEE"]}}}}],
        "products": ["EXPRESS_CHECKOUT"],
        "legal_consents": [{"type": "SHARE_DATA_CONSENT", "granted": True}],
        "partner_config_override": {"return_url": f"{config.FRONTEND_URL}/?paypal_seller=connected"},
    }
    res = show("Create a seller sign-up link (partner referral)", http.post(
        f"{BASE}/v2/customer/partner-referrals", headers=headers(tok), json=body))
    url = next((l["href"] for l in res.get("links", []) if l.get("rel") == "action_url"), None)
    if url:
        print(f"\nOpen this in an INCOGNITO window and log in as the seller's sandbox BUSINESS account:\n\n{url}\n")
        print("After approving, copy merchantIdInPayPal=... from the address bar, then run:\n"
              "    python scripts/check_partner_signup.py pay --merchant <that value>")
    else:
        print("\nNo sign-up link. If the error says NOT_AUTHORIZED or PERMISSION_DENIED, this app isn't allowed to "
              "onboard sellers: in the developer dashboard create a new sandbox app of type 'Platform' and try with "
              "its keys.")


def pay(http: httpx.Client, tok: str, merchant: str, fee: float) -> None:
    from app import wallet  # reads your saved PayPal from Firestore
    vault_id = wallet.payment_token()
    if not vault_id:
        sys.exit("No saved PayPal found. Connect PayPal in Procuro first (the 'Let your agent pay' card).")
    order_body = {
        "intent": "AUTHORIZE",
        "purchase_units": [{
            "reference_id": "partner-check", "description": "Procuro platform check",
            "amount": {"currency_code": "USD", "value": "1.00"},
            "payee": {"merchant_id": merchant},
        }],
        "payment_source": {"paypal": {"vault_id": vault_id}},
    }
    order = show("1. Order to the seller, paid with your saved PayPal (intent AUTHORIZE)", http.post(
        f"{BASE}/v2/checkout/orders", headers=headers(tok, f"partner-check-{uuid.uuid4()}"), json=order_body))
    if not order:
        print("\n=> The saved PayPal can't pay this seller through the platform (auto-pay across sellers would need "
              "PayPal to enable shared vaulting).\n   Trying the same $1 order with a normal PayPal approval instead...")
        order_body.pop("payment_source")
        order_body["application_context"] = {"return_url": "https://example.com/approved",
                                             "cancel_url": "https://example.com/cancelled",
                                             "brand_name": "Procuro check", "user_action": "CONTINUE"}
        order = show("1b. Order to the seller, approved on PayPal's page", http.post(
            f"{BASE}/v2/checkout/orders", headers=headers(tok, f"partner-check-{uuid.uuid4()}"), json=order_body))
        if not order:
            print("\nPayPal wouldn't create any order paying this seller: check the merchant id (the seller's sandbox "
                  "Account ID) and that the seller approved the sign-up link.")
            return
        approve = next((l["href"] for l in order.get("links", []) if l.get("rel") in ("approve", "payer-action")), None)
        print(f"\nOpen this in an INCOGNITO window, log in as your PERSONAL sandbox buyer and approve:\n\n{approve}\n")
        input("(The page you land on afterwards won't load; that's fine.) Press Enter here once you've approved... ")
        auth_res = show("1c. Hold the money (authorize)", http.post(
            f"{BASE}/v2/checkout/orders/{order['id']}/authorize", headers=headers(tok, f"partner-check-auth-{order['id']}"),
            json={}))
        if not auth_res:
            return
        order = auth_res
    auths = [a for pu in order.get("purchase_units", []) for a in (pu.get("payments") or {}).get("authorizations", [])]
    if not auths:
        print(f"   Order {order.get('id')} status {order.get('status')}: no authorization came back.")
        return
    auth = auths[0]
    print(f"   Held ${auth['amount']['value']} for the seller (authorization {auth['id']}, status {auth['status']})")
    cap = show(f"2. Capture with a ${fee:.2f} platform fee for Procuro", http.post(
        f"{BASE}/v2/payments/authorizations/{auth['id']}/capture",
        headers=headers(tok, f"partner-check-cap-{uuid.uuid4()}"),
        json={"amount": {"currency_code": "USD", "value": "1.00"}, "final_capture": True,
              **({"payment_instruction": {"platform_fees": [{"amount": {"currency_code": "USD", "value": f"{fee:.2f}"}}]}}
                 if fee else {})}))
    if cap:
        breakdown = cap.get("seller_receivable_breakdown") or {}
        fees = breakdown.get("platform_fees") or []
        print(f"   Captured {cap.get('id')} status {cap.get('status')}")
        print(f"   Platform fee in PayPal's answer: {fees[0]['amount']['value'] if fees else 'none'}; "
              f"seller receives {(breakdown.get('net_amount') or {}).get('value', '?')}")
        print("\nIt works. Procuro can take payments for a seller through your saved PayPal and keep a fee.")
    else:
        print("\nThe hold worked but the capture with a fee didn't. If PayPal says the fee isn't allowed, run again "
              "with --fee 0 to check the rest works: the fee would then need PayPal to enable it.")
        if fee:
            print("   (The $1.00 hold will simply expire unused.)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["link", "pay"])
    ap.add_argument("--merchant", help="the seller's merchantIdInPayPal (step 'pay')")
    ap.add_argument("--fee", type=float, default=0.10, help="platform fee in USD (default 0.10; 0 = none)")
    args = ap.parse_args()
    if "sandbox" not in BASE:
        sys.exit(f"Refusing: PAYPAL_BASE is {BASE}, not the sandbox.")
    if not (config.PAYPAL_CLIENT_ID and config.PAYPAL_CLIENT_SECRET):
        sys.exit("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set in backend/.env")
    with httpx.Client(timeout=40) as http:
        tok = token(http)
        if args.step == "link":
            link(http, tok)
        else:
            if not args.merchant:
                sys.exit("Give the seller's id: --merchant <merchantIdInPayPal>")
            pay(http, tok, args.merchant, args.fee)
    return 0


if __name__ == "__main__":
    sys.exit(main())