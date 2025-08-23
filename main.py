
import os, json
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.middleware.cors import CORSMiddleware

import stripe
import firebase_admin
from firebase_admin import credentials, firestore

# ---------- ENV ----------
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
POT_CREATE_PRICE_CENT = int(os.getenv("POT_CREATE_PRICE_CENT", "1000"))  # 500 => $5

REQUIRE_ADMIN_TOGGLE = os.getenv("REQUIRE_ADMIN_TOGGLE", "false").lower() in ("1","true","yes")
ADMIN_TOGGLE_KEY = os.getenv("ADMIN_TOGGLE_KEY", "")  # optional shared secret header 'X-Admin-Key'

# Firebase Admin
cred = credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"]))
firebase_admin.initialize_app(cred, {
    "projectId": os.environ["FIRESTORE_PROJECT_ID"]
})
db = firestore.client()

# ---------- APP ----------
app = FastAPI(title="PicklePot Stripe Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],    # tighten to your domains if desired
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def utcnow(): return datetime.now(timezone.utc)

@app.get("/", include_in_schema=False)
def root():
    return {"ok": True, "service": "picklepot-stripe", "try": ["/health", "/create-pot-session", "/create-checkout-session", "/webhook"]}

@app.head("/", include_in_schema=False)
def head_root(): return Response(status_code=200)

@app.get("/favicon.ico", include_in_schema=False)
def favicon(): return Response(status_code=204)

@app.get("/health")
def health(): return {"ok": True, "price_cents": POT_CREATE_PRICE_CENT}

# ---------- Helpers ----------
def server_base(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers.get('host')}"

def is_admin_request(request: Request) -> bool:
    if not REQUIRE_ADMIN_TOGGLE:
        return True  # compatibility mode: do not enforce unless asked
    return request.headers.get("x-admin-key", "") == ADMIN_TOGGLE_KEY

# ---------- CREATE POT (Organizer) ----------
@app.post("/create-pot-session")
async def create_pot_session(payload: dict, request: Request):
    """
    Body: { draft: {...}, success_url, cancel_url }
    Returns: { draft_id, url }
    """
    draft = payload.get("draft")
    success_url = payload.get("success_url")
    cancel_url  = payload.get("cancel_url")
    if not draft:        raise HTTPException(400, "Missing draft")
    if not success_url or not cancel_url:
        raise HTTPException(400, "Missing success/cancel URLs")

    # Enforce admin-only toggle for Stripe payments if required
    pm = draft.get("payment_methods", {}) if isinstance(draft.get("payment_methods"), dict) else {}
    allow_stripe_client = bool(pm.get("stripe"))
    if REQUIRE_ADMIN_TOGGLE and allow_stripe_client and not is_admin_request(request):
        # force off if caller isn't admin
        pm["stripe"] = False
        draft["payment_methods"] = pm

    # 1) Save draft
    ref = db.collection("pot_drafts").document()
    ref.set({**draft, "status": "draft", "createdAt": utcnow()})

    # 2) Stripe Checkout
    sess = stripe.checkout.Session.create(
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
        # IMPORTANT: route cancel through backend to delete draft automatically, then redirect to front-end
        cancel_url=f"{server_base(request)}/cancel-create?session_id={{CHECKOUT_SESSION_ID}}&next={quote(cancel_url)}",
        metadata={"draft_id": ref.id, "flow": "create"},
    )

    # Map session -> draft so we can delete on cancel without relying on the front-end
    db.collection("create_sessions").document(sess["id"]).set({
        "draft_id": ref.id,
        "createdAt": utcnow(),
    })

    return {"draft_id": ref.id, "url": sess.url}

@app.get("/cancel-create")
def cancel_create(session_id: str, next: str = "/"):
    """Server-handled cancel for 'create pot': delete the draft (if any) and redirect to the front-end cancel page."""
    doc = db.collection("create_sessions").document(session_id).get()
    if doc.exists:
        draft_id = doc.to_dict().get("draft_id")
        if draft_id:
            db.collection("pot_drafts").document(draft_id).delete()
        db.collection("create_sessions").document(session_id).delete()
    return RedirectResponse(next, status_code=302)

@app.post("/cancel-pot-session")
async def cancel_pot_session(payload: dict):
    """Legacy cancel handler used by older front-ends; still supported."""
    draft_id = payload.get("draft_id")
    if not draft_id: raise HTTPException(400, "Missing draft_id")
    db.collection("pot_drafts").document(draft_id).delete()
    return {"ok": True}

# ---------- JOIN POT (Player) ----------
@app.post("/create-checkout-session")
async def create_checkout_session(payload: dict, request: Request):
    """
    Body: { pot_id, entry_id, amount_cents, success_url, cancel_url }
    Returns: { url, session_id }
    """
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

    sess = stripe.checkout.Session.create(
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
        # Route cancel through backend to remove the pending entry automatically, then redirect
        cancel_url=f"{server_base(request)}/cancel-join?session_id={{CHECKOUT_SESSION_ID}}&pot_id={pot_id}&entry_id={entry_id}&next={quote(cancel_url)}",
        metadata={"flow": "join", "pot_id": pot_id, "entry_id": entry_id},
    )

    # Map session -> {pot_id, entry_id} for cancel path
    db.collection("join_sessions").document(sess["id"]).set({
        "pot_id": pot_id, "entry_id": entry_id, "createdAt": utcnow()
    })

    return {"url": sess.url, "session_id": sess["id"]}

@app.get("/cancel-join")
def cancel_join(session_id: str, pot_id: str = None, entry_id: str = None, next: str = "/"):
    """Server-handled cancel for 'join pot': delete the pending entry (if any) and redirect to front-end cancel page."""
    meta = db.collection("join_sessions").document(session_id).get()
    if meta.exists:
        data = meta.to_dict()
        pot_id = pot_id or data.get("pot_id")
        entry_id = entry_id or data.get("entry_id")
        db.collection("join_sessions").document(session_id).delete()

    if pot_id and entry_id:
        # Remove the entry if it still exists and isn't marked paid
        entry_ref = db.collection("pots").document(pot_id).collection("entries").document(entry_id)
        snap = entry_ref.get()
        if snap.exists:
            entry = snap.to_dict() or {}
            if not entry.get("paid"):
                entry_ref.delete()

    return RedirectResponse(next, status_code=302)

# ---------- STRIPE WEBHOOK ----------
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
                pots_ref = db.collection("pots").document(session["id"])
                if not pots_ref.get().exists:
                    draft_ref = db.collection("pot_drafts").document(draft_id)
                    draft_snap = draft_ref.get()
                    draft = draft_snap.to_dict() if draft_snap.exists else {}
                    pots_ref.set({
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
                # clean mapping
                db.collection("join_sessions").document(session["id"]).delete()

    return JSONResponse({"received": True})
