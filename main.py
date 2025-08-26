# FastAPI backend for PicklePot (consolidated & patched)
# - CORS from CORS_ALLOW env (comma-separated origins)
# - Firestore via FIREBASE_SERVICE_ACCOUNT_JSON (JSON string or path) or ADC
# - Stripe endpoints for creating pot sessions (organizer) and join sessions (players)
# - Webhook processes checkout.session.completed
# - Success helpers: /created-pots & /finalize-create
# - Owner utilities: list entries, add manual entry, mark paid, undo paid
# - Owner token uses HMAC(pot_id, OWNER_TOKEN_SECRET) with base64url (no padding)
#
# Start command on Render: uvicorn main:app --host 0.0.0.0 --port $PORT

import os, json, base64, hmac, hashlib, secrets, datetime
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---- Stripe (optional for local dev) ----
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
try:
    import stripe  # type: ignore
    if STRIPE_SECRET_KEY:
        stripe.api_key = STRIPE_SECRET_KEY
except Exception:
    stripe = None  # allow server to run without stripe for non-checkout routes

# ---- Firestore setup ----
import firebase_admin  # type: ignore
from firebase_admin import credentials, firestore  # type: ignore

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

db = get_db()

def utcnow():
    return datetime.datetime.utcnow().isoformat() + "Z"

# ---- Owner token helpers (patched) ----
OWNER_TOKEN_SECRET = os.getenv("OWNER_TOKEN_SECRET", "").strip()

def _hmac_key(pot_id: str, secret: str) -> str:
    dig = hmac.new(secret.encode("utf-8"), pot_id.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(dig).rstrip(b"=").decode("utf-8")

def generate_owner_key(pot_id: str) -> str:
    if not OWNER_TOKEN_SECRET:
        raise RuntimeError("OWNER_TOKEN_SECRET not set")
    return _hmac_key(pot_id, OWNER_TOKEN_SECRET)

def verify_owner_token(pot_id: str, key: str) -> bool:
    """Accept key if it matches the current HMAC or a stored owner_key on the pot document."""
    if OWNER_TOKEN_SECRET:
        expected = _hmac_key(pot_id, OWNER_TOKEN_SECRET)
        if secrets.compare_digest(expected, key):
            return True
    pot_ref = db.collection("pots").document(pot_id)
    snap = pot_ref.get()
    if snap.exists:
        pot = snap.to_dict() or {}
        stored = pot.get("owner_key") or pot.get("ownerKey")
        if stored and secrets.compare_digest(stored, key):
            return True
    return False

# ---- App + CORS ----
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "").strip() or "http://localhost:5500"
CORS_ALLOW = [o.strip() for o in os.getenv("CORS_ALLOW", FRONTEND_BASE_URL).split(",") if o.strip()]

app = FastAPI(title="picklepot-fastapi")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Health ----
@app.get("/")
def root():
    return {"ok": True, "service": "picklepot-fastapi", "try": ["/health","/create-pot-session","/create-checkout-session","/webhook"]}

@app.get("/health")
def health_check():
    return {"ok": True, "timestamp": utcnow()}

# ---- Models ----
class CreatePotRequest(BaseModel):
    pot_qty: int = 1
    organizer_email: Optional[str] = ""
    settings_json: Optional[str] = ""  # arbitrary JSON string from your form (optional)

class JoinPotRequest(BaseModel):
    pot_id: str
    player_name: Optional[str] = ""
    player_email: Optional[str] = ""

class ManualEntryIn(BaseModel):
    name: Optional[str] = ""
    email: Optional[str] = ""

class ManualPaymentIn(BaseModel):
    amount_cents: int = 0
    method: str = "cash"
    note: Optional[str] = ""

# ---- Stripe: create session for organizer (create pots) ----
@app.post("/create-pot-session")
def create_pot_session(body: CreatePotRequest):
    if not stripe or not STRIPE_SECRET_KEY:
        raise HTTPException(500, "Stripe not configured")
    pot_qty = max(1, min(20, int(body.pot_qty or 1)))
    price_cents = int(os.getenv("POT_CREATE_PRICE_CENT", "1500") or "1500")
    currency = "usd"

    # We create a one-time PaymentLink/CheckoutSession
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": currency,
                    "product_data": {"name": f"Create Pots (x{pot_qty})"},
                    "unit_amount": price_cents,
                },
                "quantity": pot_qty,
            }],
            success_url=f"{FRONTEND_BASE_URL}/success.html?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{FRONTEND_BASE_URL}/cancel.html",
            metadata={
                "type": "create",
                "pot_qty": str(pot_qty),
                "organizer_email": body.organizer_email or "",
                "settings_json": body.settings_json or "",
            },
        )
        return {"id": session.get("id")}
    except Exception as e:
        raise HTTPException(500, f"stripe-error: {e}")

# ---- Stripe: create session for a player to join a pot ----
@app.post("/create-checkout-session")
def create_checkout_session(body: JoinPotRequest):
    if not stripe or not STRIPE_SECRET_KEY:
        raise HTTPException(500, "Stripe not configured")
    pot_id = (body.pot_id or "").strip()
    if not pot_id:
        raise HTTPException(400, "Missing pot_id")

    # Find the pot (must exist)
    pot_ref = db.collection("pots").document(pot_id)
    if not pot_ref.get().exists:
        raise HTTPException(404, "pot-not-found")

    # Price for player buy-in (you can store this on the pot if it varies)
    player_price_cents = int(os.getenv("PLAYER_BUYIN_CENT", "1500") or "1500")
    currency = "usd"

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": currency,
                    "product_data": {"name": f"Join Pot {pot_id}"},
                    "unit_amount": player_price_cents,
                },
                "quantity": 1,
            }],
            success_url=f"{FRONTEND_BASE_URL}/success.html?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{FRONTEND_BASE_URL}/cancel.html",
            metadata={
                "type": "join",
                "pot_id": pot_id,
                "player_name": body.player_name or "",
                "player_email": body.player_email or "",
            },
        )
        return {"id": session.get("id")}
    except Exception as e:
        raise HTTPException(500, f"stripe-error: {e}")

# ---- Webhook ----
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

@app.post("/webhook")
def webhook(payload: Dict[str, Any] = Body(...), stripe_signature: Optional[str] = Query(None)):
    """Stripe webhook for checkout.session.completed"""
    if not stripe:
        # Allow local testing without Stripe
        return {"ok": True}

    # Render forwards JSON → we already have dict payload
    event = payload
    try:
        # If you want signature verification, uncomment and adapt:
        # sig = request.headers.get("Stripe-Signature")
        # event = stripe.Webhook.construct_event(request_body, sig, STRIPE_WEBHOOK_SECRET)
        pass
    except Exception as e:
        raise HTTPException(400, f"Webhook error: {e}")

    if event.get("type") == "checkout.session.completed":
        data = event.get("data", {}).get("object", {})
        metadata = data.get("metadata", {}) or {}
        sess_id = data.get("id")
        if metadata.get("type") == "create":
            # Create pots
            qty = int(str(metadata.get("pot_qty", "1") or "1"))
            settings_json = metadata.get("settings_json") or ""
            organizer_email = metadata.get("organizer_email") or ""
            _create_pots_for_session(sess_id, qty, organizer_email, settings_json)
        elif metadata.get("type") == "join":
            pot_id = metadata.get("pot_id") or ""
            player_name = metadata.get("player_name") or ""
            player_email = metadata.get("player_email") or ""
            _record_join_payment(pot_id, player_name, player_email, source="stripe", session_id=sess_id)

    return {"ok": True}

def _create_pots_for_session(session_id: str, qty: int, organizer_email: str, settings_json: str):
    qty = max(1, min(20, int(qty or 1)))
    pots: List[str] = []
    for _ in range(qty):
        doc = db.collection("pots").document()
        pot_id = doc.id
        owner_key = generate_owner_key(pot_id)
        doc.set({
            "createdAt": utcnow(),
            "createdSessionId": session_id,
            "owner_key": owner_key,
            "organizer_email": organizer_email,
            "settings_json": settings_json,
        }, merge=True)
        pots.append(pot_id)
    # store a small "session record" to speed up lookup
    db.collection("sessions").document(session_id).set({
        "pots": pots,
        "createdAt": utcnow()
    }, merge=True)

def _record_join_payment(pot_id: str, player_name: str, player_email: str, source: str, session_id: Optional[str] = None):
    if not pot_id:
        return
    entry_ref = db.collection("pots").document(pot_id).collection("entries").document()
    entry_ref.set({
        "name": (player_name or "").strip(),
        "email": (player_email or "").strip().lower(),
        "createdAt": utcnow(),
        "paid": True,
        "paid_method": source,
        "paid_at": utcnow(),
        "payments": [{
            "type": "stripe",
            "amount_cents": int(os.getenv("PLAYER_BUYIN_CENT", "1500") or "1500"),
            "at": utcnow(),
            "session_id": session_id or ""
        }]
    }, merge=True)

# ---- Success helpers ----
@app.get("/created-pots")
def created_pots(session_id: str = Query(...)):
    # First, check the small session record
    sess_doc = db.collection("sessions").document(session_id).get()
    if sess_doc.exists:
        pots = (sess_doc.to_dict() or {}).get("pots") or []
        return {"pots": [{"id": p, "owner_key": _try_owner_key(p)} for p in pots]}

    # Fallback: scan for createdSessionId (small dataset expected)
    pots = db.collection("pots").where("createdSessionId", "==", session_id).stream()
    out = []
    for d in pots:
        pot_id = d.id
        out.append({"id": pot_id, "owner_key": _try_owner_key(pot_id)})
    return {"pots": out}

def _try_owner_key(pot_id: str) -> str:
    # Return stored key if present; otherwise compute current HMAC
    snap = db.collection("pots").document(pot_id).get()
    if snap.exists:
        pot = snap.to_dict() or {}
        if pot.get("owner_key"):
            return pot["owner_key"]
    if OWNER_TOKEN_SECRET:
        return generate_owner_key(pot_id)
    return ""

@app.post("/finalize-create")
def finalize_create(session_id: str = Query(...)):
    """If webhook didn't create pots, this will (idempotent)."""
    if not stripe or not STRIPE_SECRET_KEY:
        # Without stripe, we cannot inspect the session, so create one pot as fallback
        _create_pots_for_session(session_id, 1, "", "")
        return {"ok": True}

    try:
        sess = stripe.checkout.Session.retrieve(session_id)
        meta = sess.get("metadata", {}) or {}
        qty = int(str(meta.get("pot_qty", "1") or "1"))
        organizer_email = meta.get("organizer_email") or ""
        settings_json = meta.get("settings_json") or ""
        # Only create if none exist yet
        existing = db.collection("pots").where("createdSessionId", "==", session_id).stream()
        if not any(True for _ in existing):
            _create_pots_for_session(session_id, qty, organizer_email, settings_json)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"finalize-error: {e}")

# ---- Owner utilities (entries) ----
@app.get("/pots/{pot_id}/entries")
def list_entries(pot_id: str, key: str = Query(...)):
    if not verify_owner_token(pot_id, key):
        raise HTTPException(status_code=401, detail="Invalid owner key")
    docs = db.collection("pots").document(pot_id).collection("entries").stream()
    entries = []
    for d in docs:
        data = d.to_dict() or {}
        entries.append({
            "id": d.id,
            "name": data.get("name") or data.get("player") or "",
            "email": data.get("email") or "",
            "paid": bool(data.get("paid")),
        })
    return {"entries": entries}

@app.post("/pots/{pot_id}/entries/add-manual")
def add_manual_entry(pot_id: str, key: str = Query(...), body: ManualEntryIn = Body(...)):
    if not verify_owner_token(pot_id, key):
        raise HTTPException(status_code=401, detail="Invalid owner key")
    entry_ref = db.collection("pots").document(pot_id).collection("entries").document()
    entry_ref.set({
        "name": (body.name or "").strip(),
        "email": (body.email or "").strip().lower(),
        "createdAt": utcnow(),
        "paid": False,
        "payments": [],
    }, merge=True)
    return {"ok": True, "id": entry_ref.id}

@app.post("/pots/{pot_id}/owner-add-entry")
def owner_add_entry(pot_id: str, key: str = Query(...), body: ManualEntryIn = Body(...)):
    # alias for compatibility
    return add_manual_entry(pot_id, key, body)

@app.post("/pots/{pot_id}/entries/{entry_id}/mark-paid-manual")
def mark_paid_manual(pot_id: str, entry_id: str, key: str = Query(...), body: ManualPaymentIn = Body(...)):
    if not verify_owner_token(pot_id, key):
        raise HTTPException(status_code=401, detail="Invalid owner key")
    entry_ref = db.collection("pots").document(pot_id).collection("entries").document(entry_id)
    if not entry_ref.get().exists:
        raise HTTPException(404, "entry-not-found")
    amount = int(body.amount_cents or 0)
    method = (body.method or "cash").lower()
    note = (body.note or "")[:120]
    entry_ref.set({
        "paid": True,
        "paid_at": utcnow(),
        "paid_method": method,
        "paid_amount_cents": amount,
        "payments": firestore.ArrayUnion([{
            "type": "manual",
            "method": method,
            "amount_cents": amount,
            "note": note,
            "at": utcnow()
        }])
    }, merge=True)
    return {"ok": True}

@app.post("/pots/{pot_id}/entries/{entry_id}/unmark-paid")
def unmark_paid(pot_id: str, entry_id: str, key: str = Query(...)):
    if not verify_owner_token(pot_id, key):
        raise HTTPException(status_code=401, detail="Invalid owner key")
    entry_ref = db.collection("pots").document(pot_id).collection("entries").document(entry_id)
    if not entry_ref.get().exists:
        raise HTTPException(404, "entry-not-found")
    entry_ref.set({
        "paid": False,
        "paid_at": None,
        "paid_method": None,
        "paid_amount_cents": None,
    }, merge=True)
    return {"ok": True}
