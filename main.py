import os, json, logging, base64, hashlib, hmac, time, secrets
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from urllib.parse import quote

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import stripe
import firebase_admin
from firebase_admin import credentials, firestore

# ------------------ Logging ------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("picklepot-fastapi")

# ------------------ Env ------------------
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://picklepotters.netlify.app")
OWNER_TOKEN_SECRET = os.getenv("OWNER_TOKEN_SECRET", "CHANGE-ME")  # set strong value
POT_CREATE_PRICE_CENT = int(os.getenv("POT_CREATE_PRICE_CENT", "1000"))
CORS_ALLOW = os.getenv("CORS_ALLOW") or os.getenv("CORS_ORIGINS") or "*"

# Owner code TTL (plaintext exposure on /create-status)
OWNER_CODE_TTL_SECONDS = int(os.getenv("OWNER_CODE_TTL", "600"))  # 10 minutes default

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

# ------------------ Helpers ------------------
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
        if pot != pot_id: return False
        mac = b64url_decode(mac_b64)
        key = (OWNER_TOKEN_SECRET + "|" + _pot_token_salt(pot_id)).encode()
        exp = hmac.new(key, payload.encode(), hashlib.sha256).digest()[:16]
        return hmac.compare_digest(mac, exp)
    except Exception:
        return False

def _public_pot_dict(doc_id: str, data: dict) -> dict:
    # expose only fields useful for listing & joining
    return {
        "pot_id": doc_id,
        "status": data.get("status", "active"),
        "name": data.get("name") or data.get("tournament_name") or data.get("event_name"),
        "event_name": data.get("event_name"),
        "tournament_name": data.get("tournament_name"),
        "location": data.get("location") or data.get("city"),
        "member_buy_in": data.get("member_buy_in") or data.get("buy_in"),
        "createdAt": data.get("createdAt").isoformat() if hasattr(data.get("createdAt"), "isoformat") else str(data.get("createdAt")),
    }

def _matches_query(p: dict, q: str) -> bool:
    if not q: return True
    ql = q.lower()
    for k in ("name","event_name","tournament_name","location","pot_id"):
        v = p.get(k)
        if v and ql in str(v).lower():
            return True
    return False

# ------------------ FastAPI ------------------
app = FastAPI(title="PicklePot Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if (CORS_ALLOW == "*" or not CORS_ALLOW) else [o.strip() for o in CORS_ALLOW.split(",") if o.strip()],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

@app.get("/", include_in_schema=False)
def root():
    return {"ok": True}

@app.get("/health")
def health():
    return {"ok": True, "now": utcnow().isoformat()}

# ------------------ Public list/search of Active Tournaments ------------------
@app.get("/pots")
def list_pots(q: Optional[str] = Query(None, description="search text"),
              limit: int = Query(50, ge=1, le=200)):
    """Public endpoint: list active/open pots for browsing/joining. Anyone can call this."""
    try:
        pots: List[dict] = []
        try:
            stream = (db.collection("pots")
                        .where("status", "in", ["active", "open"])
                        .order_by("createdAt", direction=firestore.Query.DESCENDING)
                        .limit(limit * 2)
                        .stream())
            for d in stream:
                data = d.to_dict() or {}
                public = _public_pot_dict(d.id, data)
                if _matches_query(public, q):
                    pots.append(public)
                if len(pots) >= limit: break
        except Exception as e:
            seen = {}
            for status in ("active","open"):
                try:
                    qref = db.collection("pots").where("status","==",status).limit(limit * 2)
                    for d in qref.stream():
                        data = d.to_dict() or {}
                        public = _public_pot_dict(d.id, data)
                        if _matches_query(public, q):
                            seen[d.id] = public
                except Exception:
                    pass
            pots = list(seen.values())
            pots.sort(key=lambda x: x.get("createdAt",""), reverse=True)
            pots = pots[:limit]

        return {"ok": True, "pots": pots, "count": len(pots)}
    except Exception as e:
        log.error("list_pots_error", extra={"error": str(e)})
        raise HTTPException(500, "Failed to list active tournaments")

# ------------------ Create-a-Pot ------------------
class CreatePotPayload(BaseModel):
    draft: Dict[str, Any] | None = None
    success_url: str
    cancel_url: str
    amount_cents: Optional[int] = None
    count: Optional[int] = 1

@app.post("/create-pot-session")
async def create_pot_session(payload: CreatePotPayload, request: Request):
    draft = payload.draft or {}
    amount_cents = int(payload.amount_cents or POT_CREATE_PRICE_CENT)
    count = max(1, int(payload.count or 1))

    if not payload.success_url or not payload.cancel_url:
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
                "product_data": {"name": f"Create Pot — {draft.get('name') or draft.get('tournament_name') or 'Tournament'}"},
                "unit_amount": amount_cents,
            },
            "quantity": count,
        }],
        success_url=f"{payload.success_url}?flow=create&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{server_base(request)}/cancel-create?session_id={{CHECKOUT_SESSION_ID}}&next={quote(payload.cancel_url)}",
        metadata={"draft_id": draft_ref.id, "flow": "create", "count": str(count)},
    )

    db.collection("create_sessions").document(session["id"]).set({
        "draft_id": draft_ref.id,
        "count": count,
        "createdAt": utcnow(),
        "ready": False,
    }, merge=True)

    return {"draft_id": draft_ref.id, "url": session.url, "count": count}

@app.get("/cancel-create")
def cancel_create(session_id: str, next: str = "/"):
    map_ref = db.collection("create_sessions").document(session_id)
    snap = map_ref.get()
    draft_id = (snap.to_dict() or {}).get("draft_id") if snap.exists else None
    if draft_id:
        db.collection("pot_drafts").document(draft_id).delete()
    try:
        for pot_doc in db.collection("pots").where("stripe_session_id", "==", session_id).stream():
            pot_doc.reference.delete()
    except Exception as e:
        log.warning("cancel_create_session_cleanup_error", extra={"error": str(e)})
    map_ref.delete()
    return RedirectResponse(next, status_code=302)

# ------------------ Join-a-Pot ------------------
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
    if not pot_id or not payload.entry_id:
        raise HTTPException(400, "Missing pot_id or entry_id")
    if payload.amount_cents < 50:
        raise HTTPException(400, "Minimum amount is 50 cents")

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": f"Join Pot — {payload.player_name or 'Player'}"},
                "unit_amount": int(payload.amount_cents),
            },
            "quantity": 1,
        }],
        customer_email=payload.player_email,
        success_url=f"{payload.success_url}?flow=join&session_id={{CHECKOUT_SESSION_ID}}&pot_id={pot_id}&entry_id={payload.entry_id}",
        cancel_url=f"{server_base(request)}/cancel-join?session_id={{CHECKOUT_SESSION_ID}}&pot_id={pot_id}&entry_id={payload.entry_id}&next={quote(payload.cancel_url)}",
        metadata={"flow": "join", "pot_id": pot_id, "entry_id": payload.entry_id, "player_email": payload.player_email or ""},
    )

    db.collection("join_sessions").document(session["id"]).set({"pot_id": pot_id,"entry_id": payload.entry_id,"createdAt": utcnow()})
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

# ------------------ Owner auth / rotate ------------------
class OwnerAuth(BaseModel):
    key: Optional[str] = None
    code: Optional[str] = None

def _require_owner(pot_id: str, auth: OwnerAuth):
    if auth.key and verify_owner_token(pot_id, auth.key):
        return True
    if auth.code:
        snap = db.collection("pots").document(pot_id).get()
        if not snap.exists: raise HTTPException(404, "Pot not found")
        if hash_code(auth.code) == (snap.to_dict() or {}).get("owner_code_hash"):
            return True
    raise HTTPException(401, "Invalid owner credentials")

@app.post("/pots/{pot_id}/owner/auth")
def owner_auth(pot_id: str, body: OwnerAuth):
    _require_owner(pot_id, body); return {"ok": True, "owner": True}

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

# ------------------ Webhook ------------------
@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except Exception as e:
        log.error("webhook_bad_signature", extra={"error": str(e)})
        raise HTTPException(400, "Bad signature")

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

                pots_payload = []
                for _ in range(max(1, count)):
                    pot_id = db.collection("pots").document().id
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

                    token = make_owner_token(pot_id)
                    manage_url = f"{FRONTEND_BASE_URL}/manage?pot={pot_id}&key={token}"
                    db.collection("owner_links").document(pot_id).set({
                        "manage_url": manage_url,
                        "createdAt": firestore.SERVER_TIMESTAMP,
                    }, merge=True)

                    now = int(time.time())
                    pots_payload.append({
                        "pot_id": pot_id,
                        "manage_url": manage_url,
                        "owner_code_plain": code,
                        "owner_code_plain_exp": now + OWNER_CODE_TTL_SECONDS,
                    })

                db.collection("create_sessions").document(session["id"]).set({
                    "ready": True,
                    "updated_at": firestore.SERVER_TIMESTAMP,
                    "pots": pots_payload,
                }, merge=True)

                draft_ref.delete()

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

# ------------------ Create Status ------------------
@app.get("/create-status")
def create_status(session_id: str = Query(..., description="Stripe checkout session id")):
    doc = db.collection("create_sessions").document(session_id).get()
    if not doc.exists:
        raise HTTPException(404, "not-ready")

    data = doc.to_dict() or {}
    pots = data.get("pots") or data.get("results") or []
    now = int(time.time())

    cleaned = []
    for p in pots:
        out = {"pot_id": p.get("pot_id"), "manage_url": p.get("manage_url")}
        exp = p.get("owner_code_plain_exp")
        if isinstance(exp, int) and exp > now and p.get("owner_code_plain"):
            out["owner_code"] = p["owner_code_plain"]
        cleaned.append(out)

    ready = bool(cleaned) and bool(data.get("ready"))
    return {"ready": ready, "pots": cleaned, "count": len(cleaned)}

@app.get("/create-status2")
def create_status2(session_id: str = Query(...)):
    return create_status(session_id=session_id)
