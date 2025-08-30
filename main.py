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

def strip_query(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))

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
    return {
        "pot_id": doc_id,
        "status": data.get("status", "open"),
        "name": data.get("name") or data.get("tournament_name") or data.get("event_name"),
        "event": data.get("event"),
        "location": data.get("location") or data.get("city"),
        "buyin_member": data.get("buyin_member") or data.get("member_buy_in"),
        "buyin_guest": data.get("buyin_guest") or data.get("guest_buy_in"),
        "createdAt": data.get("createdAt").isoformat() if hasattr(data.get("createdAt"), "isoformat") else str(data.get("createdAt")),
    }

def _matches_query(p: dict, q: str) -> bool:
    if not q: return True
    ql = q.lower()
    for k in ("name","event","location","pot_id"):
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
    """Public endpoint: list open pots for browsing/joining. Anyone can call this."""
    try:
        pots = []
        query = db.collection("pots").where("status", "==", "open")
        try:
            stream = query.order_by("createdAt", direction=firestore.Query.DESCENDING).limit(limit*2).stream()
        except Exception:
            stream = query.limit(limit*2).stream()
        for d in stream:
            data = d.to_dict() or {}
            public = _public_pot_dict(d.id, data)
            if _matches_query(public, q):
                pots.append(public)
            if len(pots) >= limit:
                break
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

    # stash the draft
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

# ------------------ Owner auth / rotate / resolve ------------------
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

@app.get("/resolve-owner-code")
def resolve_owner_code(token: Optional[str] = None, code: Optional[str] = None):
    if token:
        try:
            p_b64, mac_b64 = token.split(".")
            payload = b64url_decode(p_b64).decode()
            pot_id, _ = payload.split(".")
            if verify_owner_token(pot_id, token):
                return {"pot_id": pot_id}
        except Exception:
            pass
        raise HTTPException(400, "Invalid token")
    if code:
        # brute-light: search latest 100 pots
        for d in db.collection("pots").order_by("createdAt", direction=firestore.Query.DESCENDING).limit(100).stream():
            data = d.to_dict() or {}
            if hash_code(code) == data.get("owner_code_hash"):
                return {"pot_id": d.id}
        raise HTTPException(404, "Code not found")
    raise HTTPException(400, "Provide token or code")

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
        log.error("webhook_verify_failed", extra={"error": str(e)})
        raise HTTPException(400, "Invalid signature")

    et = event["type"]
    data = event["data"]["object"]
    log.info(f"webhook {et} id={data.get('id')}")

    # Create flow
    if et == "checkout.session.completed" and data.get("metadata", {}).get("flow") == "create":
        session_id = data.get("id")
        cs_ref = db.collection("create_sessions").document(session_id)
        cs_snap = cs_ref.get()
        if not cs_snap.exists:
            log.warning("no_create_session_map", extra={"session": session_id})
            return JSONResponse({"ok": True})
        map_data = cs_snap.to_dict() or {}
        draft_id = map_data.get("draft_id")
        count = int(map_data.get("count") or 1)
        draft = (db.collection("pot_drafts").document(draft_id).get().to_dict() if draft_id else {}) or {}

        pots_created = []
        for i in range(count):
            # create pot doc
            pot_ref = db.collection("pots").document()  # auto id
            pot_id = pot_ref.id

            # owner secrets
            owner_code = random_owner_code()
            owner_hash = hash_code(owner_code)
            owner_salt = b64url_encode(secrets.token_bytes(12))
            tok = make_owner_token(pot_id)
            manage_url = f"{FRONTEND_BASE_URL}/manage?pot={pot_id}&key={tok}"

            pot_doc = {
                "name": draft.get("name") or draft.get("tournament_name") or "Tournament",
                "organizer": draft.get("organizer") or "Pickleball Compete",
                "event": draft.get("event") or draft.get("event_name"),
                "skill": draft.get("skill") or "Any",
                "buyin_member": draft.get("buyin_member") or draft.get("member_buy_in") or 0,
                "buyin_guest": draft.get("buyin_guest") or draft.get("guest_buy_in") or 0,
                "location": draft.get("location") or draft.get("city") or "",
                "date": draft.get("date") or "",
                "time": draft.get("time") or "",
                "status": "open",                    # IMPORTANT for frontend Active list
                "public": True,
                "createdAt": utcnow(),
                "stripe_session_id": session_id,
                "payment_methods": draft.get("payment_methods") or {
                    "stripe": True, "zelle": False, "cashapp": False, "onsite": False
                },
                "owner_code_hash": owner_hash,
                "owner_token_salt": owner_salt,
            }
            pot_ref.set(pot_doc, merge=True)
            # pre-create subcollection maybe later

            pots_created.append({
                "pot_id": pot_id,
                "manage_url": manage_url,
                "owner_code": owner_code,
            })

        # cleanup draft + flip ready
        if draft_id:
            db.collection("pot_drafts").document(draft_id).delete()
        cs_ref.set({"ready": True, "pots": pots_created, "count": len(pots_created)}, merge=True)

        # ephemeral exposure of plaintext code
        exp = int(time.time()) + OWNER_CODE_TTL_SECONDS
        db.collection("create_status").document(session_id).set({
            "ready": True,
            "pots": pots_created,
            "expireAt": exp,
        }, merge=True)

        return JSONResponse({"ok": True})

    return JSONResponse({"ok": True})

# ------------------ Create Status (polled by success.html) ------------------
@app.get("/create-status")
def create_status(session_id: str):
    # First, try the ephemeral cache
    doc = db.collection("create_status").document(session_id).get()
    if doc.exists:
        data = doc.to_dict() or {}
        expire = int(data.get("expireAt") or 0)
        if expire > int(time.time()):
            return data

    # Fallback to session map (no plaintext owner code if too old)
    cs = db.collection("create_sessions").document(session_id).get()
    if not cs.exists:
        raise HTTPException(404, "Not found")
    m = cs.to_dict() or {}
    if not m.get("ready"):
        raise HTTPException(404, "Not ready")
    pots = m.get("pots") or []
    # strip owner_code for stale reads
    safe = []
    for p in pots:
        safe.append({"pot_id": p.get("pot_id"), "manage_url": p.get("manage_url")})
    return {"ready": True, "pots": safe, "count": len(safe)}
