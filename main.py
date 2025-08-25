import os, json, logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.middleware.cors import CORSMiddleware

import stripe
import firebase_admin
from firebase_admin import credentials, firestore

# =========================
# Logging
# =========================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("picklepot-fastapi")

# =========================
# Environment Configuration
# =========================
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]

IND_M = os.getenv("STRIPE_PRICE_ID_INDIVIDUAL_MONTHLY", "")
IND_Y = os.getenv("STRIPE_PRICE_ID_INDIVIDUAL_YEARLY", "")
CLB_M = os.getenv("STRIPE_PRICE_ID_CLUB_MONTHLY", "")
CLB_Y = os.getenv("STRIPE_PRICE_ID_CLUB_YEARLY", "")
PLAN_CONFIG: Dict[str, Dict[str, Any]] = {
    IND_M: {"plan":"individual","interval":"month","pots_per_month":2,"max_users_per_event":12},
    IND_Y: {"plan":"individual","interval":"year","pots_per_month":2,"max_users_per_event":12},
    CLB_M: {"plan":"club","interval":"month","pots_per_month":10,"max_users_per_event":64},
    CLB_Y: {"plan":"club","interval":"year","pots_per_month":10,"max_users_per_event":64},
}
ALLOWED_PRICE_IDS = [p for p in {IND_M, IND_Y, CLB_M, CLB_Y} if p]

POT_CREATE_PRICE_CENT = int(os.getenv("POT_CREATE_PRICE_CENT", "1000"))
CORS_ALLOW = os.getenv("CORS_ALLOW") or os.getenv("CORS_ORIGINS") or "*"

# Firebase Admin init
cred_json = os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"]
fb_project = os.getenv("FIRESTORE_PROJECT_ID")
cred = credentials.Certificate(json.loads(cred_json))
if not firebase_admin._apps:
    if fb_project:
        firebase_admin.initialize_app(cred, {"projectId": fb_project})
    else:
        firebase_admin.initialize_app(cred)
db = firestore.client()

# =========================
# FastAPI App
# =========================
app = FastAPI(title="PicklePot Stripe Backend (FastAPI)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if (CORS_ALLOW == "*" or not CORS_ALLOW) else [o.strip() for o in CORS_ALLOW.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def utcnow():
    return datetime.now(timezone.utc)

def server_base(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers.get('host')}"

# -------------------------
# Health / Utility
# -------------------------
@app.get("/", include_in_schema=False)
def root():
    return {"ok": True, "service": "picklepot-stripe", "try": ["/health", "/create-pot-session", "/create-checkout-session", "/webhook"]}

@app.head("/", include_in_schema=False)
def head_root(): return Response(status_code=200)

@app.get("/favicon.ico", include_in_schema=False)
def favicon(): return Response(status_code=204)

@app.get("/health")
def health(): 
    log.info("health_check", extra={"price_cents": POT_CREATE_PRICE_CENT})
    return {"ok": True, "price_cents": POT_CREATE_PRICE_CENT}

# -------------------------
# Create a Pot (one-time purchase)
# -------------------------
@app.post("/create-pot-session")
async def create_pot_session(payload: dict, request: Request):
    draft = payload.get("draft") or {}
    success_url = payload.get("success_url")
    cancel_url  = payload.get("cancel_url")
    amount_cents = int(payload.get("amount_cents") or POT_CREATE_PRICE_CENT)

    log.info("create_pot_session_request", extra={"amount_cents": amount_cents, "success_url": success_url, "cancel_url": cancel_url})
    if not success_url or not cancel_url:
        raise HTTPException(400, "Missing success/cancel URLs")
    if amount_cents < 50:
        raise HTTPException(400, "Minimum amount is 50 cents")

    draft_ref = db.collection("pot_drafts").document()
    draft_ref.set({**draft, "status": "draft", "createdAt": utcnow()}, merge=True)

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": f"Create Pot — {draft.get('name') or 'Tournament'}"},
                "unit_amount": amount_cents,
            },
            "quantity": 1,
        }],
        success_url=f"{success_url}?flow=create&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{server_base(request)}/cancel-create?session_id={{CHECKOUT_SESSION_ID}}&next={quote(cancel_url)}",
        metadata={"draft_id": draft_ref.id, "flow": "create"},
    )

    db.collection("create_sessions").document(session["id"]).set({
        "draft_id": draft_ref.id,
        "createdAt": utcnow(),
    })

    log.info("create_pot_session_created", extra={"session_id": session["id"], "draft_id": draft_ref.id})
    return {"draft_id": draft_ref.id, "url": session.url}

@app.get("/cancel-create")
def cancel_create(session_id: str, next: str = "/"):
    map_ref = db.collection("create_sessions").document(session_id)
    snap = map_ref.get()
    draft_id = (snap.to_dict() or {}).get("draft_id") if snap.exists else None

    log.info("cancel_create", extra={"session_id": session_id, "draft_id": draft_id})

    if draft_id:
        db.collection("pot_drafts").document(draft_id).delete()
        try:
            for pot_doc in db.collection("pots").where("draft_id", "==", draft_id).stream():
                pot_doc.reference.delete()
        except Exception as e:
            log.warning("cancel_create_cleanup_error", extra={"error": str(e)})

    try:
        for pot_doc in db.collection("pots").where("stripe_session_id", "==", session_id).stream():
            pot_doc.reference.delete()
    except Exception as e:
        log.warning("cancel_create_session_cleanup_error", extra={"error": str(e)})

    map_ref.delete()
    return RedirectResponse(next, status_code=302)

# -------------------------
# Join a Pot (player payment)
# -------------------------
@app.post("/create-checkout-session")
async def create_checkout_session(payload: dict, request: Request):
    pot_id = payload.get("pot_id")
    entry_id = payload.get("entry_id")
    amount_cents = int(payload.get("amount_cents") or 0)
    success_url = payload.get("success_url")
    cancel_url = payload.get("cancel_url")
    player_name = payload.get("player_name") or "Player"
    player_email = payload.get("player_email")

    log.info("create_checkout_session_request", extra={"pot_id": pot_id, "entry_id": entry_id, "amount_cents": amount_cents})

    if not pot_id or not entry_id:
        raise HTTPException(400, "Missing pot_id or entry_id")
    if amount_cents < 50:
        raise HTTPException(400, "Minimum amount is 50 cents")
    if not success_url or not cancel_url:
        raise HTTPException(400, "Missing success/cancel URLs")

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": f"Join Pot — {player_name}"},
                "unit_amount": amount_cents,
            },
            "quantity": 1,
        }],
        customer_email=player_email,
        success_url=f"{success_url}?flow=join&session_id={{CHECKOUT_SESSION_ID}}&pot_id={pot_id}&entry_id={entry_id}",
        cancel_url=f"{server_base(request)}/cancel-join?session_id={{CHECKOUT_SESSION_ID}}&pot_id={pot_id}&entry_id={entry_id}&next={quote(cancel_url)}",
        metadata={"flow": "join", "pot_id": pot_id, "entry_id": entry_id, "player_email": player_email or "", "player_name": player_name or ""},
    )

    db.collection("join_sessions").document(session["id"]).set({
        "pot_id": pot_id,
        "entry_id": entry_id,
        "createdAt": utcnow(),
    })

    log.info("create_checkout_session_created", extra={"session_id": session["id"]})
    return {"url": session.url, "session_id": session["id"]}

@app.get("/cancel-join")
def cancel_join(session_id: str, pot_id: Optional[str] = None, entry_id: Optional[str] = None, next: str = "/"):
    map_ref = db.collection("join_sessions").document(session_id)
    snap = map_ref.get()
    if snap.exists:
        m = snap.to_dict() or {}
        pot_id = pot_id or m.get("pot_id")
        entry_id = entry_id or m.get("entry_id")
        map_ref.delete()

    log.info("cancel_join", extra={"session_id": session_id, "pot_id": pot_id, "entry_id": entry_id})

    if pot_id and entry_id:
        entry_ref = db.collection("pots").document(pot_id).collection("entries").document(entry_id)
        es = entry_ref.get()
        if es.exists:
            entry = es.to_dict() or {}
            if not entry.get("paid"):
                entry_ref.delete()

    return RedirectResponse(next, status_code=302)

# -------------------------
# Organizer Subscriptions (optional)
# -------------------------
@app.post("/create-organizer-subscription")
async def create_organizer_subscription(payload: dict):
    price_id = (payload.get("price_id") or "").strip()
    success_url = payload.get("success_url")
    cancel_url = payload.get("cancel_url")
    email = (payload.get("email") or "").strip() or None

    log.info("create_organizer_subscription_request", extra={"price_id": price_id, "email": email})

    if not (price_id and success_url and cancel_url):
        raise HTTPException(400, "Missing price_id/success_url/cancel_url")
    if price_id not in ALLOWED_PRICE_IDS:
        raise HTTPException(400, "Invalid price_id")

    session = stripe.checkout.Session.create(
        mode="subscription",
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        customer_email=email,
        allow_promotion_codes=True,
    )
    log.info("create_organizer_subscription_created", extra={"session_id": session["id"]})
    return {"url": session.url}

ACTIVE = {"active","trialing","past_due"}

def _extract_plan(sub: Dict[str,Any]) -> Dict[str,Any]:
    items = (sub.get("items",{}) or {}).get("data") or []
    first = items[0] if items else {}
    price = (first or {}).get("price") or {}
    recurring = price.get("recurring") or {}
    price_id = price.get("id")
    bits = PLAN_CONFIG.get(price_id, {})
    return {
        "price_id": price_id,
        "interval": recurring.get("interval") or bits.get("interval"),
        "amount_cents": price.get("unit_amount"),
        "currency": price.get("currency"),
        "plan": bits.get("plan"),
        "pots_per_month": bits.get("pots_per_month"),
        "max_users_per_event": bits.get("max_users_per_event"),
    }

def _write_email(email_lc: str, cust_id: str, sub: Dict[str,Any]):
    doc = {
        "email": email_lc,
        "status": sub.get("status"),
        "current_period_end": sub.get("current_period_end"),
        "stripe_customer_id": sub.get("customer") or cust_id,
        "stripe_subscription_id": sub.get("id"),
        "updated_at": firestore.SERVER_TIMESTAMP,
        **_extract_plan(sub),
    }
    db.collection("organizer_subs_emails").document(email_lc).set(doc, merge=True)

@app.post("/activate-subscription-for-uid")
async def activate_subscription_for_uid(payload: dict):
    uid = payload.get("uid")
    email = (payload.get("email") or "").strip().lower()
    if not (uid and email):
        raise HTTPException(400, "Missing uid/email")

    snap = db.collection("organizer_subs_emails").document(email).get()
    if not snap.exists:
        raise HTTPException(404, "No subscription found for that email")
    info = snap.to_dict() or {}
    if (info.get("status") or "") not in ACTIVE:
        raise HTTPException(400, f"Subscription not active (status={info.get('status')})")

    db.collection("organizer_subs").document(uid).set({**info, "uid": uid, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
    log.info("activate_subscription_for_uid_ok", extra={"uid": uid, "email": email})
    return {"ok": True, "attached_to_uid": uid}

# -------------------------
# Stripe Webhook
# -------------------------
@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except Exception as e:
        log.error("webhook_bad_signature", extra={"error": str(e)})
        raise HTTPException(400, str(e))

    etype = event.get("type")
    obj = event.get("data",{}).get("object",{})
    log.info("webhook_event_received", extra={"type": etype})

    if etype == "checkout.session.completed":
        session = obj
        flow = (session.get("metadata") or {}).get("flow")
        log.info("webhook_checkout_completed", extra={"session_id": session.get("id"), "flow": flow})

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
                db.collection("create_sessions").document(session["id"]).delete()
                log.info("webhook_create_promoted", extra={"session_id": session.get("id"), "draft_id": draft_id})

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
                log.info("webhook_join_marked_paid", extra={"pot_id": pot_id, "entry_id": entry_id})

        elif session.get("mode") == "subscription":
            email = (session.get("customer_details") or {}).get("email") or session.get("customer_email")
            email_lc = (email or "").lower()
            sub_id = session.get("subscription")
            if email_lc and sub_id:
                sub = stripe.Subscription.retrieve(sub_id)
                _write_email(email_lc, session.get("customer"), sub)
                log.info("webhook_subscription_checkout", extra={"email": email_lc, "subscription_id": sub_id})

    elif etype in ("invoice.payment_succeeded","customer.subscription.updated","customer.subscription.deleted","customer.subscription.paused"):
        if etype.startswith("customer.subscription."):
            sub = obj; cust_id = sub.get("customer")
        else:
            sub_id = obj.get("subscription"); cust_id = obj.get("customer")
            sub = stripe.Subscription.retrieve(sub_id) if sub_id else None
        if sub:
            try:
                cust = stripe.Customer.retrieve(cust_id) if cust_id else None
                email = (cust.get("email") if cust else None) or ""
                email_lc = email.lower() if email else None
            except Exception as e:
                log.warning("webhook_subscription_email_lookup_error", extra={"error": str(e)})
                email_lc = None
            if email_lc:
                _write_email(email_lc, cust_id, sub)
                log.info("webhook_subscription_lifecycle", extra={"type": etype, "email": email_lc})

    return JSONResponse({"received": True})
