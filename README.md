# PicklePot Backend — Unified (Multi-Pot + Owner Links + Rotate/Revoke)

**Features**
- Multiple tournament creations in one Stripe Checkout (`count` on `/create-pot-session`)
- Owner access without login via **magic link** and **8-char owner code**
- **Rotate** owner code, **rotate** link (per-pot salt), or **revoke all** in one call
- Join-a-Pot Checkout + webhook marks entries as `paid:true`
- Optional organizer subscriptions (+ lifecycle webhooks)
- Logging + CORS

**Env Vars**
- `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`
- `FIREBASE_SERVICE_ACCOUNT_JSON`, `FIRESTORE_PROJECT_ID` (optional)
- `POT_CREATE_PRICE_CENT` (default 1000 == $10)
- `CORS_ALLOW` (comma-separated origins or `*`)
- `LOG_LEVEL` (INFO/DEBUG)
- `OWNER_TOKEN_SECRET` **(required)** — random long string
- `FRONTEND_BASE_URL` — used to build manage links
- (Optional) Stripe price IDs for subscriptions

**Endpoints (new/important)**
- `POST /create-pot-session` `{ draft, amount_cents, count, success_url, cancel_url }`
- `POST /create-checkout-session` `{ pot_id, entry_id, amount_cents, ... }`
- `POST /pots/{pot_id}/owner/auth` `{ key }` or `{ code }`
- `POST /pots/{pot_id}/owner/rotate-code` → `{ new_code }`
- `POST /pots/{pot_id}/owner/rotate-link` → `{ manage_url }`
- `POST /pots/{pot_id}/owner/revoke-all` → `{ new_code, manage_url }`
- `POST /webhook` — Stripe events

**Data writes**
- `pots/{potId}`: `owner_code_hash`, `owner_token_salt`, status/amounts
- `owner_links/{potId}`: `manage_url`
- `pots/{potId}/entries/{entryId}`: `paid:true` on successful join checkout

**Deploy**
- Use `Procfile` with `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Set env vars on Render and point Stripe webhook to `/webhook`
