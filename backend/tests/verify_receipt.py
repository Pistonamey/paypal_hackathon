"""Check a Procuro receipt on your own, without Procuro's server.

    python scripts/verify_receipt.py receipt-<order>.json

Run it from the backend folder (it uses Google's AP2 SDK, vendored in backend/vendor/ap2, plus
`jwcrypto` and `sd-jwt` from requirements.txt). For every document in the receipt it:

  1. checks the signature with the public key in the receipt, the way AP2 says:
       open mandates and owner-approved mandates   SD-JWT signed by the trusted surface
       agent-approved mandates                     SD-JWT delegation chain, with its audience and nonce
       checkouts, receipts, Procuro extensions     ES256 JWT signed by that agent
  2. recomputes the document's id (the hash other documents point at) and its fingerprint
  3. checks every "points to" link lands on another document in the receipt
Then it recomputes the receipt's fingerprint (SHA-256 of the step fingerprints, one per line).

Compare the public keys in the receipt with the ones the agents publish in their A2A cards
(<agent url>/.well-known/agent-card.json, field signing.jwk) to know who really signed.
Receipts for orders made before AP2 v0.2 can't be checked by this version.
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor" / "ap2"))

from ap2.sdk.generated.checkout_mandate import CheckoutMandate  # noqa: E402
from ap2.sdk.generated.open_checkout_mandate import OpenCheckoutMandate  # noqa: E402
from ap2.sdk.generated.open_payment_mandate import OpenPaymentMandate  # noqa: E402
from ap2.sdk.generated.payment_mandate import PaymentMandate  # noqa: E402
from ap2.sdk.jwt_helper import verify_jwt  # noqa: E402
from ap2.sdk.mandate import MandateClient  # noqa: E402
from ap2.sdk.sdjwt.common import compute_sd_hash, parse_token  # noqa: E402
from ap2.sdk.utils import b64url_decode, compute_sha256_b64url  # noqa: E402
from jwcrypto.jwk import JWK  # noqa: E402

import ap2.sdk.mandate as _sdk_mandate  # noqa: E402
import os  # noqa: E402

_sdk_mandate.LOG_FILE_PATH = os.devnull  # the SDK's debug log file isn't needed here

MODELS = {m.__name__: m for m in (OpenCheckoutMandate, OpenPaymentMandate, CheckoutMandate, PaymentMandate)}


def check(step: dict, keys: dict) -> str:
    spec, token = step["verify"], step["token"]
    key = JWK(**keys[spec["key"]])
    client = MandateClient()
    if spec["how"] == "sd-jwt-chain":
        payloads = client.verify(token=token, key_or_provider=lambda _t: key,
                                 expected_aud=spec.get("aud"), expected_nonce=spec.get("nonce"))
        if len(payloads) != 2:
            raise ValueError("expected an open mandate plus the closed one")
    elif spec["how"] == "sd-jwt":
        client.verify(token=token, key_or_provider=key, payload_type=MODELS[spec["payload_type"]])
    else:
        if spec.get("typ"):
            header = json.loads(b64url_decode(token.split(".")[0]))
            if header.get("typ") != spec["typ"]:
                raise ValueError(f"expected typ {spec['typ']}")
        verify_jwt(token, key)
    return "signature OK"


def doc_id(step: dict) -> str:
    token = step["token"]
    if step["type"].startswith("mandate.") and ".open." in step["type"]:
        return compute_sd_hash(parse_token(token if token.endswith("~") else token + "~"))
    if step["type"] in ("mandate.checkout.1", "mandate.payment.1"):
        return compute_sha256_b64url(MandateClient().get_closed_mandate_jwt(token))
    return compute_sha256_b64url(token)


def main(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        r = json.load(f)
    if not any("token" in s for s in r.get("steps", [])):
        print("This receipt is from before AP2 v0.2; this version of the checker can't verify it.")
        return 2
    keys, ok_all, hashes, ids = r["public_keys"], True, [], set()
    for i, step in enumerate(r["steps"], 1):
        try:
            sig = check(step, keys)
        except Exception as exc:  # noqa: BLE001
            sig, ok_all = f"FAILS ({str(exc)[:80]})", False
        h = compute_sha256_b64url(step["token"])
        did = doc_id(step)
        same = h == step["hash"] and did == step["id"]
        ok_all &= same
        ids.add(did)
        hashes.append(h)
        # Links to things outside the receipt (the PayPal hold, a refused mandate) aren't checked here.
        broken = [x["id"] for x in step["refers_to"]
                  if x.get("label") not in ("the PayPal hold", "the refused payment mandate") and x["id"] not in ids]
        ok_all &= not broken
        print(f"{i:>2}. {step['type']:<22} {sig:<14} id {did[:14]}…{'' if same else '  ID/HASH MISMATCH'}"
              + (f"  BROKEN LINK to {', '.join(str(b)[:14] for b in broken)}" if broken else ""))
    fp = hashlib.sha256("\n".join(hashes).encode()).hexdigest()
    same = fp == r.get("fingerprint")
    print(f"\nReceipt fingerprint {fp}\n{'matches' if same else 'DOES NOT MATCH'} the receipt ({r.get('fingerprint')})")
    return 0 if ok_all and same else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))