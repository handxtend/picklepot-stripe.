import os, json
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

import stripe
import firebase_admin
from firebase_admin import credentials, firestore

# ---------- Environment ----------
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
POT_CREATE_PRICE_CENT = int(os.getenv("POT_CREATE_PRICE_CENT", "1000"))  # 500 => $5

# Firebase Admin
cred = credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"]))
firebase_admin.initialize_app(cred, {
    "projectId": os.environ["FIRESTORE_PROJECT_ID"]
})
db = firestore.client()

# ---------- App ----------
app = FastAPI(title="PicklePot Stripe Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten to your frontend origins if you want
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"ok": True, "price_cents": POT_CREATE_PRICE_CENT}

# ---------- Helpers ----------
def utcnow():
    return datetime.now(timezone.utc)

# ---------- 1) Create Checkout Session ----------
@app.post("/create-pot-session")
async def create_pot_session(payload: dict):
    """
    Body:
      { draft: {...}, success_url: "...", cancel_url: "..." }
    Returns:
      { draft_id, url }
    """
    draft = payload.get("draft")
    success_url = payload.get("success_url")
    cancel_url = payload.get("cancel_url")

    if not draft:
        raise HTTPException(status_code=400, detail="Missing draft")
    if not success_url or not cancel_url:
        raise HTTPException(status_code=400, detail="Missing success/cancel URLs")

    # 1) Save draft in Firestore
    draft_ref = db.collection("pot_drafts").document()
    draft_to_store = {
        **draft,
        "status": "draft",
        "createdAt": utcnow(),
    }
    draft_ref.set(draft_to_store)

    # 2) Stripe Checkout Session
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Create a Pot"},
                "unit_amount": POT_CREATE_PRICE_CENT,  # e.g. 500 = $5
            },
            "quantity": 1
        }],
        success_url=f"{success_url}?flow=create&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{cancel_url}?flow=create&session_id={{CHECKOUT_SESSION_ID}}",
        metadata={"draft_id": draft_ref.id, "flow": "create"},
    )

    return {"draft_id": draft_ref.id, "url": session.url}

# ---------- 2) Cancel: delete the draft ----------

# ---------- JOIN: Create Checkout Session for "Join a Pot" ----------
@app.post("/create-checkout-session")
async def create_checkout_session(payload: dict):
    """
    Body: { pot_id, entry_id, amount_cents, player_name?, player_email?, success_url, cancel_url }
    Returns: { url }
    """
    pot_id = payload.get("pot_id")
    entry_id = payload.get("entry_id")
    amount_cents = int(payload.get("amount_cents") or 0)
    success_url = payload.get("success_url")
    cancel_url = payload.get("cancel_url")
    player_email = payload.get("player_email")

    if amount_cents < 50:
        raise HTTPException(status_code=400, detail="Amount must be at least 50 cents.")
    if not success_url or not cancel_url:
        raise HTTPException(status_code=400, detail="Missing success/cancel URLs")

    # Create Stripe Checkout Session for this entry
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": f"Join Pot {pot_id or ''}"},
                "unit_amount": amount_cents,
            },
            "quantity": 1
        }],
        success_url=f"{success_url}?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{cancel_url}?session_id={{CHECKOUT_SESSION_ID}}",
        customer_email=player_email or None,
        metadata={
            "flow": "join",
            "pot_id": pot_id or "",
            "entry_id": entry_id or ""
        },
    )
    return {"url": session.url}

@app.post("/cancel-pot-session")
async def cancel_pot_session(payload: dict):
    """
    Body:
      { draft_id, session_id? }
    Deletes the draft if it exists.
    """
    draft_id = payload.get("draft_id")
    if not draft_id:
        raise HTTPException(status_code=400, detail="Missing draft_id")

    # delete (or you could mark status: "canceled")
    db.collection("pot_drafts").document(draft_id).delete()
    return {"ok": True}

# ---------- 3) Stripe Webhook ----------
@app.post("/webhook")
async def stripe_webhook(request: Request):
    # Stripe needs the RAW body (do not parse as JSON before verification)
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        draft_id = (session.get("metadata") or {}).get("draft_id")
        if draft_id:
            # Idempotency: use session.id as the Pot document id
            pot_doc = db.collection("pots").document(session["id"])

            if not pot_doc.get().exists:
                # Get and remove the draft
                draft_ref = db.collection("pot_drafts").document(draft_id)
                draft_snap = draft_ref.get()
                draft = draft_snap.to_dict() if draft_snap.exists else {}

                # Create the real Pot (you can map/transform fields as you like)
                pot_doc.set({
                    **(draft or {}),
                    "status": "active",
                    "createdAt": utcnow(),
                    "source": "checkout",
                    "draft_id": draft_id,
                    "stripe_session_id": session["id"],
                    "amount_total": session.get("amount_total"),
                    "currency": session.get("currency", "usd"),
                })

                # Remove the draft (or mark status)
                draft_ref.delete()

        else:
            # Handle Join-a-Pot payments
            md = session.get("metadata") or {}
            pot_id = md.get("pot_id")
            entry_id = md.get("entry_id")
            if pot_id and entry_id:
                entry_ref = db.collection("pots").document(pot_id).collection("entries").document(entry_id)
                entry_ref.set({
                    "paid": True,
                    "paid_amount": session.get("amount_total"),
                    "paid_at": utcnow(),
                    "payment_method": "stripe",
                    "stripe_session_id": session["id"],
                }, merge=True)

    return JSONResponse({"received": True})
