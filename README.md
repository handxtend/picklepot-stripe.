# PicklePot Stripe Backend (FastAPI) — Logging Enabled

Logs key events to help you debug:
- Request creation for sessions
- Stripe webhook hits and flows
- Firestore updates (promote draft, mark entry paid)
- Cancel routes and cleanup
- Subscription lifecycle events

## Change log level
Set env var `LOG_LEVEL` to `DEBUG`, `INFO`, or `WARNING` on Render.

## Deploy
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Webhook: `POST /webhook`

Check Render logs for entries like `webhook_join_marked_paid` to confirm payments are updating Firestore.
