import os, json, logging, base64, hashlib, hmac, time, secrets
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import stripe
import firebase_admin
from firebase_admin import credentials, firestore

# =========================
# Logging
# =========================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("picklepot-fastapi")

# =========================
# Environment
# =========================
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
OWNER_TOKEN_SECRET = os.getenv("OWNER_TOKEN_SECRET", "CHANGE-ME")  # set strong value
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://picklepotters.netlify.app")
POT_CREATE_PRICE_CENT = int(os.getenv("POT_CREATE_PRICE_CENT", "1000"))
CORS_ALLOW = os.getenv("CORS_ALLOW") or os.getenv("CORS_ORIGINS") or "*"

# Optional subscription price IDs
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

# Firebase
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
# Helpers
# =========================
def utcnow():
    return datetime.now(timezone.utc)

def server_base(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers.get('host')}"

def b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")

def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def random_owner_code(length_bytes: int = 5) -> str:
    code = base64.b32encode(secrets.token_bytes(length_bytes)).decode().rstrip("=")
    return code.replace("O","8").replace("I","9")

def hash_code(code: str) -> str:
    return hashlib.sha256(("pp_salt_"+code).encode()).hexdigest()

def _pot_token_salt(pot_id: str) -> str:
    snap = db.collection("pots").document(pot_id).get()
    data = snap.to_dict() if snap.exists else {}
    return (data or {}).get("owner_token_salt", "")

def make_owner_token(pot_id: str) -> str:
    ts = int(time.time())
    payload = f"{pot_id}.{ts}"
    key = (OWNER_TOKEN_SECRET + "|" + _pot_token_salt(pot_id)).encode()
    mac = hmac.new(key, payload.encode(), hashlib.sha256).digest()[:16]
    return f"{b64url_encode(payload.encode())}.{b64url_encode(mac)}"

def verify_owner_token(pot_id: str, token: str) -> bool:
    try:
        p_b64, mac_b64 = token.split(".")
        payload = b64url_decode(p_b64).decode()
        pot, ts_s = payload.split(".")
        if pot != pot_id:
            return False
        mac = b64url_decode(mac_b64)
        key = (OWNER_TOKEN_SECRET + "|" + _pot_token_salt(pot_id)).encode()
        exp = hmac.new(key, payload.encode(), hashlib.sha256).digest()[:16]
        return hmac.compare_digest(mac, exp)
    except Exception:
        return False

# =========================
# FastAPI
# =========================
app = FastAPI(title="PicklePot Backend — Multi-Pot + Owner Links (Rotate/Revoke)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if (CORS_ALLOW == "*" or not CORS_ALLOW) else [o.strip() for o in CORS_ALLOW.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Health ----------
@app.get("/", include_in_schema=False)
def root():
    return {"ok": True, "service": "picklepot-stripe", "try": ["/health", "/create-pot-session", "/create-checkout-session", "/webhook"]}

@app.get("/health")
def health():
    log.info("health_check", extra={"price_cents": POT_CREATE_PRICE_CENT})
    return {"ok": True, "price_cents": POT_CREATE_PRICE_CENT}

# ---------- Create-a-Pot (multi) ----------
class CreatePotPayload(BaseModel):
    draft: Dict[str, Any] | None = None
    success_url: str
    cancel_url: str
    amount_cents: Optional[int] = None
    count: Optional[int] = 1

@app.post("/create-pot-session")
async def create_pot_session(payload: CreatePotPayload, request: Request):
    draft = payload.draft or {}
    success_url = payload.success_url
    cancel_url  = payload.cancel_url
    amount_cents = int(payload.amount_cents or POT_CREATE_PRICE_CENT)
    count = max(1, int(payload.count or 1))

    log.info("create_pot_session_request", extra={"amount_cents": amount_cents, "count": count})
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
            "quantity": count,
        }],
        success_url=f"{success_url}?flow=create&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{server_base(request)}/cancel-create?session_id={{CHECKOUT_SESSION_ID}}&next={quote(cancel_url)}",
        metadata={"draft_id": draft_ref.id, "flow": "create", "count": str(count)},
    )

    db.collection("create_sessions").document(session["id"]).set({
        "draft_id": draft_ref.id,
        "count": count,
        "createdAt": utcnow(),
    })

    return {"draft_id": draft_ref.id, "url": session.url, "count": count}

@app.get("/cancel-create")
def cancel_create(session_id: str, next: str = "/"):
    map_ref = db.collection("create_sessions").document(session_id)
    snap = map_ref.get()
    draft_id = (snap.to_dict() or {}).get("draft_id") if snap.exists else None

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

# ---------- Join-a-Pot ----------
class JoinPayload(BaseModel):
    pot_id: str
    entry_id: str
    amount_cents: int
    success_url: str
    cancel_url: str
    player_name: Optional[str] = "Player"
    player_email: Optional[str] = None

@app.post("/create-checkout-session")
async def create_checkout_session(payload: JoinPayload, request: Request):
    pot_id = payload.pot_id
    entry_id = payload.entry_id
    amount_cents = int(payload.amount_cents or 0)
    success_url = payload.success_url
    cancel_url = payload.cancel_url
    player_name = payload.player_name or "Player"
    player_email = payload.player_email

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

    db.collection("join_sessions").document(session["id"]).set({"pot_id": pot_id,"entry_id": entry_id,"createdAt": utcnow()})
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

    if pot_id and entry_id:
        entry_ref = db.collection("pots").document(pot_id).collection("entries").document(entry_id)
        es = entry_ref.get()
        if es.exists:
            entry = es.to_dict() or {}
            if not entry.get("paid"):
                entry_ref.delete()
    return RedirectResponse(next, status_code=302)

# ---------- Subscriptions (optional) ----------
ACTIVE = {"active","trialing","past_due"}

class SubCreate(BaseModel):
    price_id: str
    success_url: str
    cancel_url: str
    email: Optional[str] = None

@app.post("/create-organizer-subscription")
async def create_organizer_subscription(payload: SubCreate):
    price_id = (payload.price_id or "").strip()
    success_url = payload.success_url
    cancel_url = payload.cancel_url
    email = (payload.email or "").strip() or None

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
    return {"url": session.url}

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

# ---------- Owner auth + rotate/revoke ----------
class OwnerAuth(BaseModel):
    key: Optional[str] = None
    code: Optional[str] = None

def _require_owner(pot_id: str, auth: OwnerAuth):
    if auth.key and verify_owner_token(pot_id, auth.key):
        return True
    if auth.code:
        snap = db.collection("pots").document(pot_id).get()
        if not snap.exists: 
            raise HTTPException(404, "Pot not found")
        if hash_code(auth.code) == (snap.to_dict() or {}).get("owner_code_hash"):
            return True
    raise HTTPException(401, "Invalid owner credentials")

@app.post("/pots/{pot_id}/owner/auth")
def owner_auth(pot_id: str, body: OwnerAuth):
    _require_owner(pot_id, body)
    return {"ok": True, "owner": True}

@app.post("/pots/{pot_id}/owner/rotate-code")
def owner_rotate_code(pot_id: str, body: OwnerAuth):
    _require_owner(pot_id, body)
    code = random_owner_code()
    db.collection("pots").document(pot_id).set({
        "owner_code_hash": hash_code(code),
        "owner_code_rotated_at": firestore.SERVER_TIMESTAMP,
    }, merge=True)
    return {"ok": True, "new_code": code}

@app.post("/pots/{pot_id}/owner/rotate-link")
def owner_rotate_link(pot_id: str, body: OwnerAuth):
    _require_owner(pot_id, body)
    new_salt = b64url_encode(secrets.token_bytes(12))
    db.collection("pots").document(pot_id).set({
        "owner_token_salt": new_salt,
        "owner_token_rotated_at": firestore.SERVER_TIMESTAMP,
    }, merge=True)
    token = make_owner_token(pot_id)
    manage_url = f"{FRONTEND_BASE_URL}/manage?pot={pot_id}&key={token}"
    db.collection("owner_links").document(pot_id).set({
        "manage_url": manage_url,
        "rotatedAt": firestore.SERVER_TIMESTAMP,
    }, merge=True)
    return {"ok": True, "manage_url": manage_url}

@app.post("/pots/{pot_id}/owner/revoke-all")
def owner_revoke_all(pot_id: str, body: OwnerAuth):
    _require_owner(pot_id, body)
    code = random_owner_code()
    new_salt = b64url_encode(secrets.token_bytes(12))
    db.collection("pots").document(pot_id).set({
        "owner_code_hash": hash_code(code),
        "owner_code_rotated_at": firestore.SERVER_TIMESTAMP,
        "owner_token_salt": new_salt,
        "owner_token_rotated_at": firestore.SERVER_TIMESTAMP,
    }, merge=True)
    token = make_owner_token(pot_id)
    manage_url = f"{FRONTEND_BASE_URL}/manage?pot={pot_id}&key={token}"
    db.collection("owner_links").document(pot_id).set({
        "manage_url": manage_url,
        "rotatedAt": firestore.SERVER_TIMESTAMP,
    }, merge=True)
    return {"ok": True, "new_code": code, "manage_url": manage_url}

# ---------- Stripe webhook ----------
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

        if flow == "create":
            draft_id = (session.get("metadata") or {}).get("draft_id")
            count = int((session.get("metadata") or {}).get("count", "1"))
            if draft_id:
                draft_ref = db.collection("pot_drafts").document(draft_id)
                draft_snap = draft_ref.get()
                draft = draft_snap.to_dict() if draft_snap.exists else {}
                for _ in range(max(1, count)):
                    pot_id = db.collection("pots").document().id
                    # create initial salt for this pot so future tokens are bound to it
                    initial_salt = b64url_encode(secrets.token_bytes(12))
                    code = random_owner_code()
                    db.collection("pots").document(pot_id).set({
                        **(draft or {}),
                        "status": "active",
                        "createdAt": utcnow(),
                        "source": "checkout",
                        "draft_id": draft_id,
                        "stripe_session_id": session["id"],
                        "amount_total": session.get("amount_total"),
                        "currency": session.get("currency", "usd"),
                        "owner_code_hash": hash_code(code),
                        "owner_token_salt": initial_salt,
                    }, merge=True)
                    # issue magic link using this pot's salt
                    token = make_owner_token(pot_id)
                    manage_url = f"{FRONTEND_BASE_URL}/manage?pot={pot_id}&key={token}"
                    db.collection("owner_links").document(pot_id).set({
                        "manage_url": manage_url,
                        "createdAt": firestore.SERVER_TIMESTAMP,
                    })
                draft_ref.delete()
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

        elif session.get("mode") == "subscription":
            email = (session.get("customer_details") or {}).get("email") or session.get("customer_email")
            email_lc = (email or "").lower()
            sub_id = session.get("subscription")
            if email_lc and sub_id:
                sub = stripe.Subscription.retrieve(sub_id)
                _write_email(email_lc, session.get("customer"), sub)

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
            except Exception:
                email_lc = None
            if email_lc:
                _write_email(email_lc, cust_id, sub)

    return JSONResponse({"received": True})

# ----------------------
# Create Status Endpoints
# ----------------------
from fastapi import Query

def _collect_create_status(session_id: str):
    """
    Look up any newly created pots for a given Stripe Checkout session_id.
    Returns (ready: bool, results: list[dict]).
    """
    pots = []
    try:
        q = db.collection("pots").where("stripe_session_id", "==", session_id).stream()
        for doc in q:
            data = doc.to_dict() or {}
            pot_id = doc.id
            # Prefer cached manage_url if present; otherwise generate from pot's salt
            link_doc = db.collection("owner_links").document(pot_id).get()
            manage_url = None
            if link_doc.exists:
                manage_url = (link_doc.to_dict() or {}).get("manage_url")
            if not manage_url:
                try:
                    token = make_owner_token(pot_id)
                    manage_url = f"{FRONTEND_BASE_URL}/manage?pot={pot_id}&key={token}"
                except Exception:
                    manage_url = None
            pots.append({
                "pot_id": pot_id,
                "status": data.get("status", "active"),
                "manage_url": manage_url,
            })
    except Exception as e:
        log.error("status_lookup_failed", extra={"error": str(e)})
        pots = []

    return (len(pots) > 0, pots)


@app.get("/create-status")
def create_status(session_id: str = Query(..., description="Stripe checkout session id")):
    ready, pots = _collect_create_status(session_id)
    if not ready:
        # 404 signals the front-end to keep polling
        raise HTTPException(404, "not-ready")
    return {"ready": True, "pots": pots, "count": len(pots)}


@app.get("/create-status2")
def create_status2(session_id: str = Query(..., description="Stripe checkout session id")):
    # alias for older front-ends
    return create_status(session_id=session_id)
