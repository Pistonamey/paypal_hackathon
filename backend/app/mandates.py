"""AP2 v0.2 (Agent Payments Protocol) mandates, built with Google's AP2 Python SDK.

The SDK is vendored in backend/vendor/ap2 (Apache-2.0, see VENDORED.md there). This module maps
Procuro's flow onto AP2's roles:

  Trusted Surface      Procuro's own screen where the owner confirms her rules or taps Approve. Its key
                       (the "agent provider" key in AP2's Trusted Agent Provider model) signs for her.
  Shopping Agent       the buying agent. Its key is bound into the open mandates (`cnf`), so only it can
                       act on them.
  Merchant             each seller agent. It signs the Checkout JWT (ES256) and verifies mandates.
  Merchant Payment     PayPal moves the money. The seller agent verifies the payment mandate and signs
  Processor            the Payment Receipt on PayPal's behalf (AP2 lets a role delegate its checks).

The documents, in order:

  1. open checkout mandate  (mandate.checkout.open.1)  what may be bought: the exact item and quantity,
                                                       from these sellers. Signed by the Trusted Surface.
  2. open payment mandate   (mandate.payment.open.1)   what the agent may pay on its own: up to the
                                                       owner's "ask me above" amount, to these sellers,
                                                       with PayPal, only for checkout #1.
  3. checkout JWT           (ES256, by the seller)     the seller's exact, signed offer.
  4. checkout mandate       (mandate.checkout.1)       approval of that exact checkout (its hash).
  5. payment mandate        (mandate.payment.1)        approval to pay that checkout that amount.
  6. checkout receipt / payment receipt                the seller's signed answer, pointing back to 4 and 5.

Under the owner's limit with her PayPal saved, nobody needs to be there: the buying agent signs 4 and
5 itself, on top of 1 and 2 (AP2's "human not present" flow; SD-JWT delegation). Above the limit the
owner approves, and the Trusted Surface signs 4 and 5 directly ("human present").

AP2 says nothing about capturing later, partial payment or moving a delivery date. Those are signed
Procuro extensions (ES256 JWTs) and are labelled that way everywhere.
"""
import hashlib
import json
import re
import sys
import time
import uuid
from pathlib import Path

_VENDOR = Path(__file__).resolve().parents[1] / "vendor" / "ap2"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from ap2.sdk.checkout_mandate_chain import CheckoutMandateChain  # noqa: E402
from ap2.sdk.constraints import check_checkout_constraints  # noqa: E402
from ap2.sdk.generated.checkout_mandate import CheckoutMandate  # noqa: E402
from ap2.sdk.generated.checkout_receipt import CheckoutReceipt  # noqa: E402
from ap2.sdk.generated.open_checkout_mandate import (  # noqa: E402
    AllowedMerchants, Item as WantedItem, LineItemRequirements, LineItems, OpenCheckoutMandate,
)
from ap2.sdk.generated.open_payment_mandate import (  # noqa: E402
    AllowedPayees, AllowedPaymentInstruments, AmountRange, OpenPaymentMandate, PaymentReference,
)
from ap2.sdk.generated.payment_mandate import PaymentMandate  # noqa: E402
from ap2.sdk.generated.payment_receipt import PaymentReceipt  # noqa: E402
from ap2.sdk.generated.types.amount import Amount  # noqa: E402
from ap2.sdk.generated.types.checkout import Checkout, Status  # noqa: E402
from ap2.sdk.generated.types.item import Item  # noqa: E402
from ap2.sdk.generated.types.line_item import LineItem  # noqa: E402
from ap2.sdk.generated.types.link import Link  # noqa: E402
from ap2.sdk.generated.types.merchant import Merchant  # noqa: E402
from ap2.sdk.generated.types.payment_instrument import PaymentInstrument  # noqa: E402
from ap2.sdk.generated.types.total import Total  # noqa: E402
from ap2.sdk.jwt_helper import create_jwt, verify_jwt  # noqa: E402
from ap2.sdk.mandate import MandateClient  # noqa: E402
from ap2.sdk.receipt_wrapper import ReceiptClient  # noqa: E402
from ap2.sdk.sdjwt.common import compute_sd_hash, parse_token  # noqa: E402
from ap2.sdk.utils import b64url_decode, compute_sha256_b64url  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from jwcrypto.jwk import JWK  # noqa: E402

from . import config  # noqa: E402
import ap2.sdk.mandate as _sdk_mandate  # noqa: E402
import os  # noqa: E402

# The SDK appends a debug line to a log file inside its own folder on every check; send it nowhere.
_sdk_mandate.LOG_FILE_PATH = os.devnull

# How AP2 data travels inside A2A messages (as in Google's AP2 samples).
EXTENSION_URI = "https://github.com/google-agentic-commerce/ap2/v1"
CHECKOUT_MANDATE_KEY = "ap2.mandates.CheckoutMandateSdJwt"
PAYMENT_MANDATE_KEY = "ap2.mandates.PaymentMandateSdJwt"
CHECKOUT_KEY = "ap2.checkout"
CHECKOUT_RECEIPT_KEY = "ap2.CheckoutReceipt"
PAYMENT_RECEIPT_KEY = "ap2.PaymentReceipt"
PRESENTATION_KEY = "ap2.presentation"  # which flow, plus the nonce and audience each closed mandate is bound to

HUMAN_PRESENT, HUMAN_NOT_PRESENT = "human_present", "human_not_present"

PAYPAL_SAVED = PaymentInstrument(id="paypal-vault", type="paypal",
                                 description="The owner's saved PayPal (PayPal Vault payment token)")
PAYPAL_CHECKOUT = PaymentInstrument(id="paypal-checkout", type="paypal",
                                    description="PayPal checkout, approved on PayPal's page")

SURFACE = "trusted-surface"
BUYER = "buyer-agent"

# ---------------------------------------------------------------- keys

_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_keys: dict[str, JWK] = {}


def key(name: str) -> JWK:
    """An ES256 (P-256) key per role, derived from SIGNING_SEED so it survives restarts.

    name: "trusted-surface", "buyer-agent" or "seller:<id>".
    """
    if name not in _keys:
        digest = hashlib.sha256(f"{config.SIGNING_SEED}:ap2:{name}".encode()).digest()
        secret = int.from_bytes(digest, "big") % (_P256_ORDER - 1) + 1
        d = json.loads(JWK.from_pyca(ec.derive_private_key(secret, ec.SECP256R1())).export())
        d["kid"] = f"procuro-{name.replace(':', '-')}"
        _keys[name] = JWK(**d)
    return _keys[name]


def public_jwk(name: str) -> dict:
    return json.loads(key(name).export_public())


def jwk_from(data: dict | None) -> JWK | None:
    return JWK(**data) if data else None


# ---------------------------------------------------------------- small helpers

def cents(amount: float) -> int:
    return int(round(float(amount) * 100))


def item_id(item: str, product_id: str | None) -> str:
    """The line item every seller must quote: the exact Channel3 product, or the item named in the request."""
    if product_id:
        return str(product_id)
    return "item:" + re.sub(r"[^a-z0-9]+", "-", item.lower()).strip("-")[:60]


def merchant(seller_id: str, name: str, url: str) -> Merchant:
    return Merchant(id=seller_id, name=name, website=url)


def sha256_b64url(token: str) -> str:
    return compute_sha256_b64url(token)


def leaf(chain: str) -> str:
    """The closed mandate at the end of an SD-JWT chain. sha256(leaf) is what a receipt points to."""
    return MandateClient().get_closed_mandate_jwt(chain)


def sd_hash(sd_jwt: str) -> str:
    return compute_sd_hash(parse_token(sd_jwt if sd_jwt.endswith("~") else sd_jwt + "~"))


def jwt_payload(token: str) -> dict:
    """The (unverified) payload of a compact JWT, for display. Verify first when it matters."""
    return json.loads(b64url_decode(token.split(".")[1]))


# ---------------------------------------------------------------- 1 + 2: the owner's rules

def open_mandates(*, item: str, product_id: str | None, quantity: int, merchants: list[Merchant],
                  approval_limit: float, currency: str, expires_at: int) -> dict:
    """The owner confirmed her rules on Procuro's screen (the Trusted Surface): sign the open mandates."""
    agent = {"jwk": public_jwk(BUYER)}
    now = int(time.time())
    client = MandateClient()
    open_checkout = client.create(payloads=[OpenCheckoutMandate(
        constraints=[
            LineItems(items=[LineItemRequirements(
                id="line_1", acceptable_items=[WantedItem(id=item_id(item, product_id), title=item[:200])],
                quantity=quantity)]),
            AllowedMerchants(allowed=merchants),
        ],
        cnf=agent, iat=now, exp=expires_at,
    )], issuer_key=key(SURFACE))
    open_checkout_hash = sd_hash(open_checkout)
    open_payment = client.create(payloads=[OpenPaymentMandate(
        constraints=[
            # What the agent may pay without asking: the owner's "ask me above" amount.
            AmountRange(currency=currency, min=0, max=cents(approval_limit)),
            AllowedPayees(allowed=merchants),
            AllowedPaymentInstruments(allowed=[PAYPAL_SAVED]),
            PaymentReference(conditional_transaction_id=open_checkout_hash),
        ],
        cnf=agent, iat=now, exp=expires_at,
    )], issuer_key=key(SURFACE))
    return {"open_checkout": open_checkout, "open_payment": open_payment, "open_checkout_hash": open_checkout_hash}


def verify_open_checkout(open_checkout: str, surface_jwk: JWK | None = None) -> OpenCheckoutMandate:
    """A seller checks the open checkout mandate came from the owner's Trusted Surface before quoting a checkout."""
    m = MandateClient().verify(token=open_checkout, key_or_provider=surface_jwk or key(SURFACE),
                               payload_type=OpenCheckoutMandate)
    return m.mandate_payload


# ---------------------------------------------------------------- 3: the seller's checkout

def checkout_jwt(*, seller_id: str, seller: Merchant, order_id: str, item: str, product_id: str | None,
                 quantity: int, unit_price: float, currency: str, extra: dict | None = None) -> tuple[str, str, dict]:
    """The seller's exact offer, as a merchant-signed Checkout JWT (ES256, as AP2 requires)."""
    unit, total = cents(unit_price), cents(unit_price) * quantity
    checkout = Checkout(
        id=f"chk_{order_id}_{seller_id}",
        merchant=seller,
        line_items=[LineItem(id="li_1", item=Item(id=item_id(item, product_id), title=item[:200], price=unit),
                             quantity=quantity,
                             totals=[Total(type="subtotal", amount=total), Total(type="total", amount=total)])],
        status=Status.ready_for_complete,
        currency=currency,
        totals=[Total(type="subtotal", amount=total), Total(type="total", amount=total)],
        links=[Link(type="terms_of_service", url=seller.website or "https://example.com", title="Seller agent")],
    )
    payload = checkout.model_dump(mode="json", exclude_none=True)
    payload.update({"iat": int(time.time()), **(extra or {})})
    header = {"alg": "ES256", "typ": "JWT", "kid": key(f"seller:{seller_id}").get("kid")}
    token = create_jwt(header, payload, key(f"seller:{seller_id}"))
    return token, sha256_b64url(token), payload


def checkout_violations(open_checkout: OpenCheckoutMandate, checkout_payload: dict) -> list[str]:
    return check_checkout_constraints(open_checkout, Checkout.model_validate(checkout_payload))


# ---------------------------------------------------------------- 4 + 5: approving that checkout

def close_mandates(*, mode: str, open_checkout: str, open_payment: str, checkout_token: str, checkout_hash: str,
                   payee: Merchant, amount: float, currency: str, saved_paypal: bool, audience: str) -> dict:
    """Approve one exact checkout and its payment.

    human_not_present: the buying agent signs on top of the owner's open mandates (it can only do that
    inside their limits: the seller checks every constraint).
    human_present: the owner approved on Procuro's screen, so the Trusted Surface signs directly.
    """
    now = int(time.time())
    checkout = CheckoutMandate(checkout_jwt=checkout_token, checkout_hash=checkout_hash, iat=now, exp=now + 3600)
    payment = PaymentMandate(
        transaction_id=checkout_hash, payee=payee,
        payment_amount=Amount(amount=cents(amount), currency=currency),
        payment_instrument=PAYPAL_SAVED if saved_paypal else PAYPAL_CHECKOUT,
        iat=now, exp=now + 3600,
    )
    client = MandateClient()
    checkout_nonce, payment_nonce = uuid.uuid4().hex, uuid.uuid4().hex
    if mode == HUMAN_NOT_PRESENT:
        checkout_chain = client.present(holder_key=key(BUYER), mandate_token=open_checkout, payloads=[checkout],
                                        nonce=checkout_nonce, aud=audience)
        payment_chain = client.present(holder_key=key(BUYER), mandate_token=open_payment, payloads=[payment],
                                       nonce=payment_nonce, aud=audience)
    else:
        checkout_chain = client.create(payloads=[checkout], issuer_key=key(SURFACE))
        payment_chain = client.create(payloads=[payment], issuer_key=key(SURFACE))
    return {"mode": mode, "checkout_mandate": checkout_chain, "payment_mandate": payment_chain,
            "presentation": {"mode": mode, "audience": audience, "checkout_nonce": checkout_nonce,
                             "payment_nonce": payment_nonce}}


class MandateRejected(Exception):
    def __init__(self, code: str, description: str):
        super().__init__(description)
        self.code, self.description = code, description


def verify_checkout_mandate(chain: str, presentation: dict, *, expected_checkout_hash: str,
                            surface_jwk: JWK | None = None) -> CheckoutMandate:
    """Merchant checks (AP2 §Verification: Merchant), done in code, never by the model."""
    surface = surface_jwk or key(SURFACE)
    client = MandateClient()
    try:
        if presentation.get("mode") == HUMAN_NOT_PRESENT:
            payloads = client.verify(token=chain, key_or_provider=lambda _t: surface,
                                     expected_aud=presentation.get("audience"),
                                     expected_nonce=presentation.get("checkout_nonce"))
            parsed = CheckoutMandateChain.parse(payloads)
            closed = parsed.closed_mandate
            problems = parsed.verify(expected_checkout_hash=expected_checkout_hash, checkout_jwt=closed.checkout_jwt)
        else:
            closed = client.verify(token=chain, key_or_provider=surface, payload_type=CheckoutMandate).mandate_payload
            problems = [] if closed.checkout_hash == expected_checkout_hash else ["checkout_hash does not match"]
    except Exception as exc:  # noqa: BLE001 - any failure to verify is a rejection
        raise MandateRejected("invalid_checkout_mandate", f"checkout mandate doesn't verify: {exc}") from None
    if sha256_b64url(closed.checkout_jwt) != closed.checkout_hash:
        problems.append("checkout_hash isn't the hash of the checkout it carries")
    if problems:
        raise MandateRejected("checkout_mismatch", "; ".join(problems))
    return closed


def verify_payment_mandate(chain: str, presentation: dict, *, expected_checkout_hash: str,
                           open_checkout_chain: str | None, surface_jwk: JWK | None = None) -> PaymentMandate:
    """Payment checks (AP2 §Verification: Credential Provider / Merchant Payment Processor)."""
    surface = surface_jwk or key(SURFACE)
    client = MandateClient()
    try:
        if presentation.get("mode") == HUMAN_NOT_PRESENT:
            from ap2.sdk.payment_mandate_chain import PaymentMandateChain
            payloads = client.verify(token=chain, key_or_provider=lambda _t: surface,
                                     expected_aud=presentation.get("audience"),
                                     expected_nonce=presentation.get("payment_nonce"))
            parsed = PaymentMandateChain.parse(payloads)
            open_hash = sd_hash(open_checkout_chain.split("~~")[0]) if open_checkout_chain else None
            problems = parsed.verify(expected_transaction_id=expected_checkout_hash,
                                     expected_open_checkout_hash=open_hash)
            closed = parsed.closed_mandate
        else:
            closed = client.verify(token=chain, key_or_provider=surface, payload_type=PaymentMandate).mandate_payload
            problems = [] if closed.transaction_id == expected_checkout_hash else ["transaction_id does not match"]
    except Exception as exc:  # noqa: BLE001
        raise MandateRejected("invalid_payment_mandate", f"payment mandate doesn't verify: {exc}") from None
    if problems:
        code = "unresolved_constraint" if any("exceeds" in p or "Amount" in p for p in problems) else "payment_mismatch"
        raise MandateRejected(code, "; ".join(problems))
    return closed


# ---------------------------------------------------------------- 6: receipts

def _sign(name: str, payload: dict, typ: str = "JWT") -> str:
    k = key(name)
    return create_jwt({"alg": "ES256", "typ": typ, "kid": k.get("kid")}, payload, k)


def checkout_receipt(seller_id: str, issuer: str, checkout_chain: str, order_ref: str) -> str:
    content = ReceiptClient().create_checkout_receipt(merchant=issuer, reference=sha256_b64url(leaf(checkout_chain)),
                                                      order_id=order_ref)
    return _sign(f"seller:{seller_id}", content.model_dump(mode="json", exclude_none=True))


def payment_receipt(seller_id: str, issuer: str, payment_chain: str, payment_id: str, psp_id: str,
                    network_id: str) -> str:
    content = {"status": "Success", "iss": issuer, "iat": int(time.time()),
               "reference": sha256_b64url(leaf(payment_chain)), "payment_id": payment_id,
               "psp_confirmation_id": psp_id, "network_confirmation_id": network_id}
    PaymentReceipt.model_validate(content)
    return _sign(f"seller:{seller_id}", content)


def error_receipt(seller_id: str, issuer: str, chain: str | None, code: str, description: str,
                  payment: bool) -> str:
    """A signed refusal: AP2 says a rejected mandate still gets a receipt, with the reason."""
    ref = sha256_b64url(leaf(chain)) if chain else ""
    content = {"status": "Error", "iss": issuer, "iat": int(time.time()), "reference": ref, "error": code,
               "error_description": description}
    if payment:
        content["payment_id"] = "none"
        PaymentReceipt.model_validate(content)
    else:
        CheckoutReceipt.model_validate(content)
    return _sign(f"seller:{seller_id}", content)


def verify_receipt(token: str, issuer_jwk: JWK) -> dict:
    return verify_jwt(token, issuer_jwk)


# ---------------------------------------------------------------- Procuro extensions (not part of AP2)

RELEASE_TYP = "procuro-release+jwt"
AMENDMENT_TYP = "procuro-checkout-amendment+jwt"


def release_instruction(payload: dict) -> str:
    """The buying agent's signed instruction to capture (part of) the hold, or to void it."""
    return _sign(BUYER, {"iat": int(time.time()), **payload}, RELEASE_TYP)


def amendment(seller_id: str, payload: dict) -> str:
    """A seller's signed change to an agreed checkout (a new delivery date at the same price)."""
    return _sign(f"seller:{seller_id}", {"iat": int(time.time()), **payload}, AMENDMENT_TYP)


def verify_extension(token: str, signer_jwk: JWK, typ: str) -> dict:
    header = json.loads(b64url_decode(token.split(".")[0]))
    if header.get("typ") != typ:
        raise ValueError(f"expected a {typ}")
    return verify_jwt(token, signer_jwk)