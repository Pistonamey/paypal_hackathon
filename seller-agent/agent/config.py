"""Settings for this seller agent, from environment variables (or a local .env file).

Nothing here is shared with Procuro: this agent has its own signing seed, its own PayPal sandbox
business account and (optionally) its own storage. The only thing it knows about Procuro is the
address of the buying agent's A2A card, which it uses to learn the public keys it should trust.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:  # python-dotenv is optional
    pass


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


AGENT_ID = _env("AGENT_ID", "northbox")
AGENT_NAME = _env("AGENT_NAME", "NorthBox Packaging")
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", "http://localhost:8100").rstrip("/")

# The buying agent this seller deals with. Its A2A card publishes the keys of the buying agent and of
# the owner's trusted surface; mandates signed by anyone else are refused.
TRUSTED_BUYER_URL = _env("TRUSTED_BUYER_URL", "http://localhost:8000/agents/buyer").rstrip("/")

SIGNING_SEED = _env("SIGNING_SEED", "northbox-dev-only-change-me")

PAYPAL_MODE = _env("PAYPAL_MODE", "mock").lower()  # mock | sandbox
PAYPAL_BASE = _env("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")
PAYPAL_CLIENT_ID = _env("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = _env("PAYPAL_CLIENT_SECRET")
# A saved PayPal (Vault token) only works for the PayPal app that saved it. Turn this on only if this
# agent runs on the same PayPal app as the buyer's platform.
ACCEPT_SAVED_PAYPAL = _env("ACCEPT_SAVED_PAYPAL", "false").lower() in ("1", "true", "yes")

FIREBASE_CREDENTIALS_JSON = _env("FIREBASE_CREDENTIALS_JSON")
FIREBASE_CREDENTIALS_PATH = _env("FIREBASE_CREDENTIALS_PATH")  # or a key file on disk (handy locally)
if not FIREBASE_CREDENTIALS_JSON and FIREBASE_CREDENTIALS_PATH:
    _path = Path(FIREBASE_CREDENTIALS_PATH)
    if not _path.is_absolute():
        _path = Path(__file__).resolve().parents[1] / _path
    FIREBASE_CREDENTIALS_JSON = _path.read_text(encoding="utf-8")
STORE_COLLECTION = _env("STORE_COLLECTION", f"{AGENT_ID}_agent")

DEFAULT_CURRENCY = "USD"

# How this seller does business. Run the same code as several sellers by giving each its own values.
# Prices are multiples of the item's cost (about 70% of the lowest retail price Channel3 finds).
MARKUP = float(_env("MARKUP", "1.30"))        # first quote = cost x MARKUP
FLOOR = float(_env("FLOOR", "1.10"))          # never sells below cost x FLOOR
LEAD_DAYS = int(_env("LEAD_DAYS", "4"))       # days to make and ship
MAX_QTY = int(_env("MAX_QTY", "3000"))        # bigger orders are declined
TAGLINE = _env("TAGLINE", "Independent box maker on recycled board. Runs on its own server, outside Procuro.")
GREETING = _env("GREETING", "Hi from NorthBox! We make every box here on recycled board.")


def seller_paypal_credentials(_seller_id: str = "") -> tuple[str, str]:
    return PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET