"""Environment configuration. Every secret comes from env vars (Heroku config vars / local .env)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# --- AI ---
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", "claude-sonnet-5-5")
# Only needed for organization-level keys that aren't scoped to a workspace.
ANTHROPIC_WORKSPACE_ID = _env("ANTHROPIC_WORKSPACE_ID")
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
# Outside seller agents (comma-separated agent URLs, e.g. https://northbox.example.com/agents/northbox).
# The buying agent deals with these as well as the three seller agents hosted by this app.
SELLER_AGENT_URLS = [u.strip().rstrip("/") for u in _env("SELLER_AGENT_URLS").split(",") if u.strip()]
# For testing an outside seller: deal ONLY with SELLER_AGENT_URLS, not the three built-in sellers.
SELLER_AGENTS_ONLY = _env("SELLER_AGENTS_ONLY").lower() in ("1", "true", "yes")
CORS_ORIGINS = [o.strip() for o in _env("CORS_ORIGINS", FRONTEND_URL).split(",") if o.strip()]

# --- Firebase / Firestore ---
# Service-account credentials, first one set wins:
#   FIREBASE_CREDENTIALS_JSON       the key file's JSON, pasted as the value (one line, or in quotes)
#   FIREBASE_CREDENTIALS_PATH       path to the downloaded key file
#   GOOGLE_APPLICATION_CREDENTIALS  standard Google path variable
# With none of them, data is kept in memory and lost on restart.
FIREBASE_CREDENTIALS_PATH = _env("FIREBASE_CREDENTIALS_PATH")
FIREBASE_CREDENTIALS_JSON = _env("FIREBASE_CREDENTIALS_JSON")
GOOGLE_APPLICATION_CREDENTIALS = _env("GOOGLE_APPLICATION_CREDENTIALS")
# Optional Cloud Storage bucket for full-size damage photos (needs the Blaze plan),
# e.g. my-project.firebasestorage.app. Without it, a compressed copy goes in Firestore.
FIREBASE_STORAGE_BUCKET = _env("FIREBASE_STORAGE_BUCKET")
# "memory" forces the in-memory store (tests); anything else uses Firestore when credentials exist.
STORE_BACKEND = _env("STORE_BACKEND")

# --- Security ---
# Seed used to derive every signing key (AP2 ES256 keys; Ed25519 for orders made before AP2 v0.2). Keep it secret and stable.
SIGNING_SEED = _env("SIGNING_SEED", "dev-only-insecure-seed")
CRON_SECRET = _env("CRON_SECRET", "dev-cron-secret")

DEFAULT_CURRENCY = _env("DEFAULT_CURRENCY", "USD")