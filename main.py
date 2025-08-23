
import os, json
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.middleware.cors import CORSMiddleware

import stripe
import firebase_admin
from firebase_admin import credentials, firestore

# ====== ENV ======
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
POT_CREATE_PRICE_CENT = int(os.getenv("POT_CREATE_PRICE_CENT", "500"))  # default $5

cred = credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"]))
firebase_admin.initialize_app(cred, {
    "projectId": os.environ["FIRESTORE_PROJECT_ID"]
})
db = firestore.client()

# ====== APP ======
app = FastAPI(title="PicklePot Stripe Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # restrict to your domains if desired
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def utcnow(): return datetime.now(timezone.utc)
def server_base(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers.get('host')}"

@app.get("/", include_in_schema=False)
def root():
    return {"ok": True, "service": "picklepot-stripe", "try": ["/health", "/create-pot-session", "/create-checkout-session", "/webhook"]}

@app.head("/", include_in_schema=False)
def head_root(): return Response(status_code=200)

@app.get("/favicon.ico", include_in_schema=False)
def favicon(): return Response(status_code=204)

@app.get("/health")
def health(): return {"ok": True, "price_cents": POT_CREATE_PRICE_CENT}

# ====== CREATE A POT ======
@app.post("/create-pot-session")
async def create_pot_session(payload: dict, request: Request):
    """Create Stripe checkout for organizer 'Create a Pot'. Save draft. Delete draft on cancel."""
    draft = payload.get("draft") or {}
    success_url = payload.get("success_url")
    cancel_url  = payload.get("cancel_url")
    if not draft:
        raise HTTPException(400, "Missing draft")
    if not success_url or not cancel_url:
        raise HTTPException(400, "Missing success/cancel URLs")

    # Save draft
    draft_ref = db.collection("pot_drafts").document()
    draft_ref.set({**draft, "status": "draft", "createdAt": utcnow()})

    # Stripe session; route cancel via backend
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Create a Pot"},
                "unit_amount": POT_CREATE_PRICE_CENT,
            },
            "quantity": 1,
        }],
        success_url=f"{success_url}?flow=create&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{server_base(request)}/cancel-create?session_id={{CHECKOUT_SESSION_ID}}&next={quote(cancel_url)}",
        metadata={"draft_id": draft_ref.id, "flow": "create"},
    )

    # Map session -> draft for cancel cleanup
    db.collection("create_sessions").document(session["id"]).set({
        "draft_id": draft_ref.id,
        "createdAt": utcnow(),
    })

    return {"draft_id": draft_ref.id, "url": session.url}

@app.get("/cancel-create")
def cancel_create(session_id: str, next: str = "/"):
    """Stripe Cancel redirect (Create-a-Pot): delete draft then send user back."""
    map_ref = db.collection("create_sessions").document(session_id)
    snap = map_ref.get()
    if snap.exists:
        draft_id = (snap.to_dict() or {}).get("draft_id")
        if draft_id:
            db.collection("pot_drafts").document(draft_id).delete()
        map_ref.delete()
    return RedirectResponse(next, status_code=302)

# ====== JOIN A POT ======
@app.post("/create-checkout-session")
async def create_checkout_session(payload: dict, request: Request):
    """Create Stripe checkout for player 'Join a Pot'. Delete pending entry on cancel."""
    pot_id = payload.get("pot_id")
    entry_id = payload.get("entry_id")
    amount_cents = int(payload.get("amount_cents") or 0)
    success_url = payload.get("success_url")
    cancel_url = payload.get("cancel_url")

    if not pot_id or not entry_id:
        raise HTTPException(400, "Missing pot_id or entry_id")
    if amount_cents <= 0:
        raise HTTPException(400, "Invalid amount")
    if not success_url or not cancel_url:
        raise HTTPException(400, "Missing success/cancel URLs")

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": f"Join Pot {pot_id}"},
                "unit_amount": amount_cents,
            },
            "quantity": 1,
        }],
        success_url=f"{success_url}?flow=join&session_id={{CHECKOUT_SESSION_ID}}&pot_id={pot_id}&entry_id={entry_id}",
        cancel_url=f"{server_base(request)}/cancel-join?session_id={{CHECKOUT_SESSION_ID}}&pot_id={pot_id}&entry_id={entry_id}&next={quote(cancel_url)}",
        metadata={"flow": "join", "pot_id": pot_id, "entry_id": entry_id},
    )

    # Map session -> {pot_id, entry_id} for cancel cleanup
    db.collection("join_sessions").document(session["id"]).set({
        "pot_id": pot_id,
        "entry_id": entry_id,
        "createdAt": utcnow(),
    })

    return {"url": session.url, "session_id": session["id"]}

@app.get("/cancel-join")
def cancel_join(session_id: str, pot_id: str = None, entry_id: str = None, next: str = "/"):
    """Stripe Cancel redirect (Join-a-Pot): delete unpaid entry then send user back."""
    # Find mapping if not provided
    map_ref = db.collection("join_sessions").document(session_id)
    snap = map_ref.get()
    if snap.exists:
        m = snap.to_dict() or {}
        pot_id = pot_id or m.get("pot_id")
        entry_id = entry_id or m.get("entry_id")
        map_ref.delete()

    if pot_id and entry_id:
        entry_ref = db.collection("pots").document(pot_id).collection("entries").document(entry_id)
        es = entry_ref.get()
        if es.exists:
            entry = es.to_dict() or {}
            if not entry.get("paid"):
                entry_ref.delete()

    return RedirectResponse(next, status_code=302)

# ====== WEBHOOK ======
@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(400, str(e))

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        flow = (session.get("metadata") or {}).get("flow")

        if flow == "create":
            draft_id = (session.get("metadata") or {}).get("draft_id")
            if draft_id:
                pot_doc = db.collection("pots").document(session["id"])
                if not pot_doc.get().exists:
                    draft_ref = db.collection("pot_drafts").document(draft_id)
                    draft_snap = draft_ref.get()
                    draft = draft_snap.to_dict() if draft_snap.exists else {}
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
                    draft_ref.delete()
                # clean mapping
                db.collection("create_sessions").document(session["id"]).delete()

        elif flow == "join":
            pot_id = (session.get("metadata") or {}).get("pot_id")
            entry_id = (session.get("metadata") or {}).get("entry_id")
            if pot_id and entry_id:
                entry_ref = db.collection("pots").document(pot_id).collection("entries").document(entry_id)
                entry_ref.set({
                    "paid": True,
                    "paid_amount": session.get("amount_total"),
                    "paid_at": utcnow(),
                    "payment_method": "stripe",
                    "stripe_session_id": session["id"],
                }, merge=True)
                db.collection("join_sessions").document(session["id"]).delete()

    return JSONResponse({"received": True})
