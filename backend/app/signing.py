"""AP2-style signed mandates.

Every agent has an Ed25519 key derived from SIGNING_SEED + agent id, so keys
survive dyno restarts. Public keys are published in each agent's A2A agent card.

Mandates (following the roles in Google's Agent Payments Protocol):
  intent_mandate   the user's rules (budget, approval limit, deadline), signed by the buyer agent
                   on the user's behalf once the user confirms them
  cart_mandate     the seller's final, binding offer, signed by the seller agent
  payment_mandate  the user's approval of that exact cart, signed by the buyer agent
  release          instruction to capture a specific amount after delivery, signed by the buyer agent
"""
import base64
import hashlib
import json
import time
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from . import config


def _private_key(agent_id: str) -> Ed25519PrivateKey:
    seed = hashlib.sha256(f"{config.SIGNING_SEED}:{agent_id}".encode()).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key_b64(agent_id: str) -> str:
    raw = _private_key(agent_id).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def sign(agent_id: str, mandate_type: str, payload: dict) -> dict:
    body = {
        "type": mandate_type,
        "id": f"{mandate_type}_{uuid.uuid4().hex[:12]}",
        "issued_at": int(time.time()),
        "signer": agent_id,
        "payload": payload,
    }
    sig = _private_key(agent_id).sign(canonical(body))
    return {**body, "signature": base64.b64encode(sig).decode(), "alg": "Ed25519"}


def verify(mandate: dict, public_key: str | None = None) -> bool:
    try:
        body = {k: mandate[k] for k in ("type", "id", "issued_at", "signer", "payload")}
        key_b64 = public_key or public_key_b64(mandate["signer"])
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(key_b64))
        key.verify(base64.b64decode(mandate["signature"]), canonical(body))
        return True
    except (KeyError, ValueError, InvalidSignature):
        return False
