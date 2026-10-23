"""Environment configuration. Every secret comes from env vars (Heroku config vars / local .env)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# --- AI ---
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", "claude-sonnet-5-5")
# Without a key the agents fall back to a simple rule-based negotiator so the
# app still runs locally. The hosted demo always runs with the real model.
AI_ENABLED = bool(ANTHROPIC_API_KEY)

# --- Channel3 (product data + market prices) ---
CHANNEL3_API_KEY = _env("CHANNEL3_API_KEY")
CHANNEL3_BASE = _env("CHANNEL3_BASE", "https://api.trychannel3.com")

# --- PayPal ---
# "sandbox" calls the real PayPal sandbox. "mock" simulates PayPal in memory
# (only for local development and automated tests).
PAYPAL_MODE = _env("PAYPAL_MODE", "sandbox")
PAYPAL_BASE = _env("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")
PAYPAL_CLIENT_ID = _env("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = _env("PAYPAL_CLIENT_SECRET")
PAYPAL_WEBHOOK_ID = _env("PAYPAL_WEBHOOK_ID")


def seller_paypal_credentials(seller_id: str) -> tuple[str, str]:
    """Each seller agent can own its own PayPal sandbox business account.

    Set SELLER_<ID>_PAYPAL_CLIENT_ID / _SECRET (e.g. SELLER_QUICKPRINT_...).
    Falls back to the shared PAYPAL_CLIENT_ID / SECRET.
    """
    key = seller_id.upper()
    cid = _env(f"SELLER_{key}_PAYPAL_CLIENT_ID") or PAYPAL_CLIENT_ID
    secret = _env(f"SELLER_{key}_PAYPAL_CLIENT_SECRET") or PAYPAL_CLIENT_SECRET
    return cid, secret


# --- URLs ---
FRONTEND_URL = _env("FRONTEND_URL", "http://localhost:3000").rstrip("/")
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
# Comma-separated agent base URLs. Defaults to the seller agents hosted by this app.
SELLER_AGENT_URLS = [u.strip().rstrip("/") for u in _env("SELLER_AGENT_URLS").split(",") if u.strip()]
CORS_ORIGINS = [o.strip() for o in _env("CORS_ORIGINS", FRONTEND_URL).split(",") if o.strip()]

# --- Firebase / Firestore ---
# Either the full service-account JSON as a string (best for Heroku config vars)
# or a path in GOOGLE_APPLICATION_CREDENTIALS. Without either, an in-memory store is used.
FIREBASE_CREDENTIALS_JSON = _env("FIREBASE_CREDENTIALS_JSON")
GOOGLE_APPLICATION_CREDENTIALS = _env("GOOGLE_APPLICATION_CREDENTIALS")

# --- Security ---
# Seed used to derive each agent's Ed25519 signing key. Keep it secret and stable.
SIGNING_SEED = _env("SIGNING_SEED", "dev-only-insecure-seed")
CRON_SECRET = _env("CRON_SECRET", "dev-cron-secret")

DEFAULT_CURRENCY = _env("DEFAULT_CURRENCY", "USD")
