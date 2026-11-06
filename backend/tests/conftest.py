"""Test setup. Runs before any app module is imported.

Values set here win over backend/.env (python-dotenv never overrides existing variables), so the
tests never touch real Firebase, PayPal, Claude or Channel3 even on a machine with real keys.
"""
import os

os.environ.update({
    "STORE_BACKEND": "memory",
    "PAYPAL_MODE": "mock",
    "ANTHROPIC_API_KEY": "",
    "CHANNEL3_API_KEY": "",
    "FIREBASE_CREDENTIALS_PATH": "",
    "FIREBASE_CREDENTIALS_JSON": "",
    "GOOGLE_APPLICATION_CREDENTIALS": "",
    "FIREBASE_STORAGE_BUCKET": "",
    "PUBLIC_BASE_URL": "http://testserver",
    "SELLER_AGENT_URLS": "",
})

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def empty_seller_schedules():
    """Every test starts with idle sellers, so bookings from one test never delay quotes in another."""
    from app import schedule
    from app.store import store
    for sid in schedule.SLOTS:
        store.set_doc(schedule.COLLECTION, sid, {})
    yield


@pytest.fixture(autouse=True)
def no_saved_paypal():
    """Every test starts with the owner's PayPal not connected."""
    from app import wallet
    from app.store import store
    store.set_doc(*wallet.DOC, {})
    yield