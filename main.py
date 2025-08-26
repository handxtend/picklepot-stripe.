import os
import hmac
import json
import hashlib
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Request, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

import stripe
import firebase_admin
from firebase_admin import credentials, firestore
from pydantic import BaseModel

# ------------------
# ENV & configuration
# ------------------
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "http://localhost:5500")
OWNER_TOKEN_SECRET = os.getenv("OWNER_TOKEN_SECRET", "dev-owner-secret")
POT_CREATE_PRICE_CENT = int(os.getenv("POT_CREATE_PRICE_CENT", "1500"))

if not STRIPE_SECRET_KEY:
    raise RuntimeError("Missing STRIPE_SECRET_KEY")
stripe.api_key = STRIPE_SECRET_KEY

# Firestore init (supports FIREBASE_SERVICE_ACCOUNT_JSON string OR path, else ADC)
_db = None
def get_db():
    global _db
    if _db is not None:
        return _db
    cred_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    if cred_json:
        if cred_json.strip().startswith("{"):
            cred = credentials.Certificate(json.loads(cred_json))
        else:
            cred = credentials.Certificate(cred_json)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
    else:
        if not firebase_admin._apps:
            firebase_admin.initialize_app()
    _db = firestore.client()
    return _db

def db():
    return get_db()

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

def owner_token(pot_id: str) -> str:
    sig = hmac.new(OWNER_TOKEN_SECRET.encode(), pot_id.encode(), hashlib.sha256).hexdigest()
    return sig[:32]

def verify_owner_token(pot_id: str, key: str) -> bool:
    return hmac.compare_digest(owner_token(pot_id), (key or ""))

# --------------
# FastAPI & CORS
# --------------
app = FastAPI(title="picklepot-fastapi")

allow_origins = [o.strip() for o in os.getenv("CORS_ALLOW", "").split(",") if o.strip()]
if not allow_origins:
    allow_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------
# Health
# ----------
@app.get("/", include_in_schema=False)
def root():
    return {"ok": True, "service": "picklepot-fastapi", "try": ["/health", "/create-pot-session", "/create-checkout-session", "/webhook"]}

@app.get("/health")
def health_check():
    return {"ok": True, "at": utcnow()}

# ----------------
# Create pot flow
# ----------------

class CreatePotIn(BaseModel):
    count: int = 1            # number of pots to create
    title: str = "Pickle Pot"
    city: Optional[str] = ""
    location: Optional[str] = ""
    date: Optional[str] = ""
    time: Optional[str] = ""
    end_time: Optional[str] = ""
    host: Optional[str] = ""
    guest_buy_in: Optional[int] = None
    member_buy_in: Optional[int] = None
    percent: Optional[int] = None
    zelle: Optional[str] = ""
    cashapp: Optional[str] = ""
    onsite_payment: Optional[str] = "Allowed"
    pot_price_cents: Optional[int] = None   # override default

@app.post("/create-pot-session")
def create_pot_session(body: CreatePotIn):
    """Starts Stripe checkout for organizer to pay creation fee(s).
       We record a draft in Firestore then create a Stripe Checkout Session.
    """
    if body.count < 1 or body.count > 20:
        raise HTTPException(400, "count must be between 1 and 20")

    draft_ref = db().collection("pot_drafts").document()
    draft = body.dict()
    draft["createdAt"] = utcnow()
    draft_ref.set(draft)

    unit_amount = body.pot_price_cents or POT_CREATE_PRICE_CENT
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data":{
                "currency":"usd",
                "product_data":{"name":"Create Pot(s)"},
                "unit_amount": unit_amount,
            },
            "quantity": body.count,
        }],
        success_url= f"{FRONTEND_BASE_URL}/success.html?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url= f"{FRONTEND_BASE_URL}/cancel.html?session_id={{CHECKOUT_SESSION_ID}}",
        metadata={
            "flow": "create",
            "draft_id": draft_ref.id,
            "count": str(body.count),
        }
    )

    # Map session -> draft for later clean/fallback
    db().collection("create_sessions").document(session["id"]).set({
        "draft_id": draft_ref.id,
        "createdAt": utcnow(),
    })

    return {"url": session["url"]}

@app.get("/cancel-create")
def cancel_create(session_id: str):
    """If organizer aborts, clean the draft & any temp data and redirect to cancel page."""
    mref = db().collection("create_sessions").document(session_id)
    snap = mref.get()
    draft_id = (snap.to_dict() or {}).get("draft_id") if snap.exists else None

    # delete draft
    if draft_id:
        db().collection("pot_drafts").document(draft_id).delete()
        # defensive remove any pot linked with this draft or session
        try:
            for p in db().collection("pots").where("draft_id","==",draft_id).stream():
                p.reference.delete()
        except Exception:
            pass
    try:
        for p in db().collection("pots").where("stripe_session_id","==",session_id).stream():
            p.reference.delete()
    except Exception:
        pass
    mref.delete()

    return RedirectResponse(f"{FRONTEND_BASE_URL}/cancel.html")

# ----------------
# Join pot flow
# ----------------
class JoinIn(BaseModel):
    pot_id: str
    name: Optional[str] = ""
    email: Optional[str] = ""
    amount_cents: int = 2000

@app.post("/create-checkout-session")
def create_checkout_session(body: JoinIn):
    # Pre-create an entry
    entry_ref = db().collection("pots").document(body.pot_id).collection("entries").document()
    entry_ref.set({
        "name": (body.name or "").strip(),
        "email": (body.email or "").strip().lower(),
        "paid": False,
        "createdAt": utcnow(),
    }, merge=True)

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data":{
                "currency":"usd",
                "product_data":{"name":"Join Pot"},
                "unit_amount": int(body.amount_cents),
            },
            "quantity": 1,
        }],
        success_url= f"{FRONTEND_BASE_URL}/success.html?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url= f"{FRONTEND_BASE_URL}/cancel.html?session_id={{CHECKOUT_SESSION_ID}}",
        metadata={
            "flow": "join",
            "pot_id": body.pot_id,
            "entry_id": entry_ref.id,
        }
    )

    # Map session for cleanup if needed
    db().collection("join_sessions").document(session["id"]).set({
        "pot_id": body.pot_id,
        "entry_id": entry_ref.id,
        "createdAt": utcnow(),
    })

    return {"url": session["url"]}

@app.get("/cancel-join")
def cancel_join(session_id: str):
    """If player cancels at Checkout, remove the pending entry."""
    mref = db().collection("join_sessions").document(session_id).get()
    if mref.exists:
        data = mref.to_dict() or {}
        pot_id = data.get("pot_id")
        entry_id = data.get("entry_id")
        if pot_id and entry_id:
            db().collection("pots").document(pot_id).collection("entries").document(entry_id).delete()
        db().collection("join_sessions").document(session_id).delete()
    return RedirectResponse(f"{FRONTEND_BASE_URL}/cancel.html")

# -----------------
# Stripe webhook
# -----------------
@app.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(400, f"invalid webhook: {e}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        flow = (session.get("metadata") or {}).get("flow")

        if flow == "create":
            # create N pots from draft
            draft_id = (session.get("metadata") or {}).get("draft_id")
            count = int((session.get("metadata") or {}).get("count") or "1")
            draft = {}
            if draft_id:
                dref = db().collection("pot_drafts").document(draft_id)
                dsnap = dref.get()
                draft = dsnap.to_dict() if dsnap.exists else {}
            for i in range(max(count,1)):
                pot_id = f"{session['id']}-{i+1}" if count>1 else session["id"]
                pot_ref = db().collection("pots").document(pot_id)
                if not pot_ref.get().exists:
                    pot_ref.set({
                        **(draft or {}),
                        "status": "active",
                        "createdAt": utcnow(),
                        "stripe_session_id": session["id"],
                        "source": "checkout",
                        "index": i+1,
                    }, merge=True)
            # cleanup maps/drafts
            db().collection("create_sessions").document(session["id"]).delete()
            if draft_id:
                db().collection("pot_drafts").document(draft_id).delete()

        elif flow == "join":
            pot_id = (session.get("metadata") or {}).get("pot_id")
            entry_id = (session.get("metadata") or {}).get("entry_id")
            if pot_id and entry_id:
                entry_ref = db().collection("pots").document(pot_id).collection("entries").document(entry_id)
                entry_ref.set({
                    "paid": True,
                    "paid_amount": session.get("amount_total"),
                    "paid_at": utcnow(),
                    "payment_method": "stripe",
                    "stripe_session_id": session["id"],
                }, merge=True)
            db().collection("join_sessions").document(session["id"]).delete()

    return JSONResponse({"received": True})

# ------------------------------------------
# Success page helpers: created + finalize
# ------------------------------------------

@app.get("/created-pots")
def get_created_pots(session_id: str):
    """Return pot ids created by a 'create' session + owner keys, or empty list."""
    pots = []
    for p in db().collection("pots").where("stripe_session_id","==",session_id).stream():
        pot_id = p.id
        pots.append({"pot_id": pot_id, "key": owner_token(pot_id)})
    return {"pots": pots}

@app.post("/finalize-create")
def finalize_create(session_id: str):
    """Fallback when webhook missed: create pots from stored draft."""
    # find mapping
    msnap = db().collection("create_sessions").document(session_id).get()
    if not msnap.exists:
        # nothing to do; respond empty
        return {"pots": []}

    draft_id = (msnap.to_dict() or {}).get("draft_id")
    dsnap = db().collection("pot_drafts").document(draft_id).get() if draft_id else None
    draft = dsnap.to_dict() if (dsnap and dsnap.exists) else {}
    count = int(str(draft.get("count") or "1")) if isinstance(draft, dict) else 1

    pots = []
    for i in range(max(count,1)):
        pot_id = f"{session_id}-{i+1}" if count>1 else session_id
        pot_ref = db().collection("pots").document(pot_id)
        if not pot_ref.get().exists:
            pot_ref.set({
                **(draft or {}),
                "status": "active",
                "createdAt": utcnow(),
                "stripe_session_id": session_id,
                "source": "finalize",
                "index": i+1,
            }, merge=True)
        pots.append({"pot_id": pot_id, "key": owner_token(pot_id)})

    # cleanup mapping + draft
    db().collection("create_sessions").document(session_id).delete()
    if draft_id:
        db().collection("pot_drafts").document(draft_id).delete()

    return {"pots": pots}

# --------------------------------------
# Owner utilities: list/mark/unmark/add
# --------------------------------------

@app.get("/pots/{pot_id}/entries")
def list_entries(pot_id: str, key: str = Query(...)):
    if not verify_owner_token(pot_id, key):
        raise HTTPException(401, "Invalid owner key")

    docs = db().collection("pots").document(pot_id).collection("entries").stream()
    out = []
    for d in docs:
        data = d.to_dict() or {}
        out.append({
            "id": d.id,
            "name": data.get("name") or data.get("player") or "",
            "email": data.get("email") or "",
            "paid": bool(data.get("paid")),
        })
    return {"entries": out}

class ManualEntryIn(BaseModel):
    name: Optional[str] = ""
    email: Optional[str] = ""

@app.post("/pots/{pot_id}/entries/add-manual")
def add_manual_entry(pot_id: str, key: str, body: ManualEntryIn):
    if not verify_owner_token(pot_id, key):
        raise HTTPException(401, "Invalid owner key")
    ref = db().collection("pots").document(pot_id).collection("entries").document()
    ref.set({
        "name": (body.name or "").strip(),
        "email": (body.email or "").strip().lower(),
        "paid": False,
        "createdAt": utcnow(),
        "payments": [],
    }, merge=True)
    return {"ok": True, "id": ref.id}

# alias for old client fallback
@app.post("/pots/{pot_id}/owner-add-entry")
def owner_add_entry(pot_id: str, key: str, body: ManualEntryIn):
    return add_manual_entry(pot_id, key, body)

class ManualPaymentIn(BaseModel):
    amount_cents: int = 0
    method: str = "cash"
    note: Optional[str] = ""

@app.post("/pots/{pot_id}/entries/{entry_id}/mark-paid-manual")
def mark_paid_manual(pot_id: str, entry_id: str, key: str, body: ManualPaymentIn):
    if not verify_owner_token(pot_id, key):
        raise HTTPException(401, "Invalid owner key")
    ref = db().collection("pots").document(pot_id).collection("entries").document(entry_id)
    if not ref.get().exists:
        raise HTTPException(404, "entry-not-found")
    ref.set({
        "paid": True,
        "paid_at": utcnow(),
        "paid_method": body.method,
        "paid_amount_cents": int(body.amount_cents or 0),
        "payments": firestore.ArrayUnion([{
            "type": "manual",
            "method": body.method,
            "amount_cents": int(body.amount_cents or 0),
            "note": (body.note or "")[:120],
            "at": utcnow(),
        }])
    }, merge=True)
    return {"ok": True}

@app.post("/pots/{pot_id}/entries/{entry_id}/unmark-paid")
def unmark_paid(pot_id: str, entry_id: str, key: str):
    if not verify_owner_token(pot_id, key):
        raise HTTPException(401, "Invalid owner key")
    ref = db().collection("pots").document(pot_id).collection("entries").document(entry_id)
    if not ref.get().exists:
        raise HTTPException(404, "entry-not-found")
    ref.set({
        "paid": False,
        "paid_at": None,
        "paid_method": None,
        "paid_amount_cents": None,
    }, merge=True)
    return {"ok": True}