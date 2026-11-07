# NorthBox Packaging: an outside seller agent

A seller agent that is **not part of Procuro**. It runs on its own server and is built on the official
[A2A Python SDK](https://github.com/a2aproject/a2a-python) (`a2a-sdk` 1.3, A2A v1.0 with v0.3 compatibility).
It speaks AP2 v0.2 through Google's AP2 SDK (vendored in `vendor/ap2`, Apache-2.0), signs with its own key,
and is paid into its own PayPal sandbox account. Procuro's buying agent finds it from its agent card and
negotiates with it exactly like it does with its built-in sellers.

What it shares with Procuro: nothing but the protocol.
- It learns which signatures to trust from the buying agent's public A2A card (`TRUSTED_BUYER_URL`).
- Its own `SIGNING_SEED` gives it its own ES256 key, published in its card.
- It has its own PayPal account, so Procuro can't see its money directly. For the receipt, Procuro asks
  it over A2A (`payment_status`) what PayPal shows.
- It has its own storage (memory, or Firestore under its own collection).

## Endpoints

| | |
|---|---|
| `GET /agents/northbox/.well-known/agent-card.json` | Its A2A agent card (also at `/.well-known/agent-card.json`) |
| `POST /agents/northbox` | A2A JSON-RPC: `SendMessage` (v1.0) and `message/send` (v0.3) |
| `GET /` | A page showing its recent conversations with buying agents |
| `GET /health` | Settings check |

Requests carry one data part with an `action`:
- `request_quote`, `counter_offer` and `close` for negotiating
- `finalize`, which returns an AP2 checkout as a task artifact
- `create_payment` and `authorize` for AP2 mandates and the PayPal hold
- `release`, `void` and `reauthorize` for the money afterwards
- `propose_remedy`, `reschedule` and `payment_status` after the sale

These are the same messages Procuro's own seller agents use.

What it checks, in code:
- It signs a checkout only if it fits the owner's AP2 open checkout mandate.
- It takes payment only on AP2 checkout and payment mandates that verify against the keys in the buyer's card, for its exact checkout, merchant id and total.
- It never captures more than PayPal holds, and only on the buying agent's signed release.

## Run it locally

```bash
cd seller-agent
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash; on macOS/Linux: .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env        # PAYPAL_MODE=mock needs no PayPal keys
uvicorn agent.main:app --port 8100
```

Then tell Procuro's backend about it (in `backend/.env`) and restart the backend:

```
SELLER_AGENT_URLS=http://localhost:8100/agents/northbox
```

## Deploy it on Heroku (a second app)

1. In the Heroku dashboard, create a new app (for example `northbox-agent`).
2. Go to **Settings → Buildpacks** and add `https://github.com/lstoll/heroku-buildpack-monorepo` first, then `heroku/python`.
3. Go to **Settings → Config Vars** and add:
   - `APP_BASE=seller-agent`
   - `PUBLIC_BASE_URL`: this app's own URL, from **Settings → Domains**
   - `TRUSTED_BUYER_URL`: `<Procuro backend URL>/agents/buyer`
   - `SIGNING_SEED`: a new random value
   - `PAYPAL_MODE=sandbox`, plus `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` from its own PayPal app (see below)
   - optionally `FIREBASE_CREDENTIALS_JSON`
4. Go to **Deploy**, connect the GitHub repo and deploy `main`.
5. On Procuro's backend app, set `SELLER_AGENT_URLS=<this app's URL>/agents/northbox`.

**Its own PayPal account:**
1. In the PayPal developer dashboard, go to **Testing Tools → Sandbox Accounts → Create account** and make a Business account.
2. Go to **Apps & Credentials → Create App**, and choose that new business account as the app's sandbox account.
3. Use that app's client ID and secret here.

Payments to NorthBox then land in that account, separate from Procuro's.

A PayPal saved through Procuro (Vault) only works for Procuro's PayPal app. So when Procuro's agent tries to auto-pay NorthBox, NorthBox refuses with a signed AP2 error receipt (`payment_instrument_unavailable`), and the owner approves on PayPal's page instead. That's the correct AP2 behaviour. If you want auto-pay to work with NorthBox in a demo, give it Procuro's PayPal app keys and set `ACCEPT_SAVED_PAYPAL=true`.

## Tests

The end-to-end tests run this agent next to Procuro's backend (two apps, talking over HTTP):

```bash
cd backend && python -m pip install -r requirements-dev.txt && python -m pytest tests/test_outside_seller.py
```
