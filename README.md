# PicklePot Backend — Unified (Render‑friendly)

- Multi‑pot creation via Stripe (`count`)
- Owner codes + magic links
- Rotate owner code, rotate link (salt), revoke‑all
- Join‑a‑Pot checkout marks entry paid
- Optional organizer subscriptions
- Lighter requirements for Render builds

## Deploy
- Build Command:
  ```
  pip install --upgrade pip setuptools wheel
  pip install -r requirements.txt
  ```
- Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- runtime.txt pins Python 3.11.9
