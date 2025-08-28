import os, json, logging, base64, hashlib, hmac, time, secrets
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import stripe
import firebase_admin
from firebase_admin import credentials, firestore

# ------------------- Logging -------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("picklepot-fastapi")

# ------------------- Stripe / App Config -------------------
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
OWNER_TOKEN_SECRET = os.getenv("OWNER_TOKEN_SECRET", "CHANGE-ME")

# Your site root; keep this as your Netlify origin
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://picklepotters.netlify.app")

# IMPORTANT: point to the actual deployed paths (you can override via Render env)
PUBLIC_SUCCESS_URL = os.getenv(
    "PUBLIC_SUCCESS_URL",
    f"{FRONTEND_BASE_URL}/picklepotters/success.html",  # your deploy shows files under /picklepotters
)
PUBLIC_CANCEL_URL = os.getenv(
    "PUBLIC_CANCEL_URL",
    f"{FRONTEND_BASE_URL}/picklepotters/cancel.html",
)

# Where the organizer manage page lives (extensionless in your deploy)
FRONTEND_MANAGE_PATH = os.getenv("FRONTEND_MANAGE_PATH", "/picklepotters/manage")

POT_CREATE_PRICE_CENT = int(os.getenv("POT_CREATE_PRICE_CENT", "1000"))

# CORS
CORS_ALLOW_STR = os.getenv("CORS_ALLOW") or os.getenv("CORS_ORIGINS") or ""
DEFAULT_ORIGINS = ["https://picklepotters.netlify.app","http://localhost:8080"]
if not CORS_ALLOW_STR.strip():
    ALLOWED_ORIGINS = DEFAULT_ORIGINS
elif CORS_ALLOW_STR.strip() == "*":
    ALLOWED_ORIGINS = DEFAULT_ORIGINS
else:
    ALLOWED_ORIGINS = [o.strip() for o in CORS_ALLOW_STR.split(",") if o.strip()]

# Optional subscription price ids
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

# ------------------- Firebase -------------------
cred_json = os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"]
fb_project = os.getenv("FIRESTORE_PROJECT_ID")
cred = credentials.Certificate(json.loads(cred_json))
if not firebase_admin._apps:
    if fb_project:
        firebase_admin.initialize_app(cred, {"projectId": fb_project})
    else:
        firebase_admin.initialize_app(cred)
db = firestore.client()

# ------------------- Helpers -------------------
def utcnow(): return datetime.now(timezone.utc)
def server_base(request: Request) -> str: return f"{request.url.scheme}://{request.headers.get('host')}"
def b64url_encode(b: bytes) -> str: return base64.urlsafe_b64encode(b).decode().rstrip("=")
def b64url_decode(s: str) -> bytes: return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

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
    import hashlib as _hashlib, hmac as _hmac, time as _time
    ts = int(_time.time())
    payload = f"{pot_id}.{ts}"
    key = (OWNER_TOKEN_SECRET + "|" + _pot_token_salt(pot_id)).encode()
    mac = _hmac.new(key, payload.encode(), _hashlib.sha256).digest()[:16]
    return f"{b64url_encode(payload.encode())}.{b64url_encode(mac)}"

# ------------------- Optional SMTP Email -------------------
import smtplib, ssl
from email.message import EmailMessage

SMTP_HOST = os.getenv("SMTP_HOST")         # e.g. smtp.gmail.com / smtp.office365.com
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "")
FROM_NAME  = os.getenv("FROM_NAME", "Pickle Pot")

def send_email_smtp(to_email: str, subject: str, html: str):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and SMTP_FROM and to_email):
        log.info("SMTP not configured or recipient missing; skipping email.")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{FROM_NAME} <{SMTP_FROM}>"
    msg["To"] = to_email
    msg.set_content("Your email client does not support HTML.")
    msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=ctx)
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)

def email_organizer_links(organizer_email: str, pots: list[dict]):
    if not organizer_email or not pots:
        return
    items = "".join(
        f'<li><a href="{p["manage_url"]}">Manage Link</a> — Owner code: <code>{p["owner_code"]}</code></li>'
        for p in pots
    )
    html = f"""
    <p>Hi! Your tournament has been created.</p>
    <p>Use the links/codes below to manage your pot(s):</p>
    <ul>{items}</ul>
    <p>Organizer page (bookmark): <a href="{FRONTEND_BASE_URL}{FRONTEND_MANAGE_PATH}">Manage Pot</a></p>
    """
    try:
        send_email_smtp(organizer_email, "Your Pickle Pot — Organizer Links", html)
    except Exception as e:
        log.exception("Email send failed: %s", e)

# ------------------- FastAPI App -------------------
app = FastAPI(title="PicklePot Backend — Multi-Pot + Owner Links")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST","GET","OPTIONS"],
    allow_headers=["*"],
)

@app.get("/", include_in_schema=False)
def root():
    return {"ok": True, "service": "picklepot-stripe"}

@app.get("/health")
def health():
    return {"ok": True, "price_cents": POT_CREATE_PRICE_CENT, "cors": ALLOWED_ORIGINS}

# ------------------- Create Pot Session -------------------
class CreatePotPayload(BaseModel):
    draft: Dict[str, Any] | None = None
    success_url: Optional[str] = None   # kept for compatibility, but we override
    cancel_url: Optional[str] = None    # kept for compatibility, but we override
    amount_cents: Optional[int] = None
    count: Optional[int] = 1

@app.post("/create-pot-session")
async def create_pot_session(payload: CreatePotPayload, request: Request):
    draft = payload.draft or {}
    amount_cents = int(payload.amount_cents or POT_CREATE_PRICE_CENT)
    count = max(1, int(payload.count or 1))
    if amount_cents < 50:
        raise HTTPException(400, "Minimum amount is 50 cents")

    # Always use the server-configured success/cancel URLs so they match your deployed paths
    success_url = PUBLIC_SUCCESS_URL
    cancel_url = PUBLIC_CANCEL_URL

    # Organizer email (various field names supported)
    org_email = (draft.get("organizer_email") or draft.get("organizerEmail") or draft.get("org_email") or "").strip()

    log.info("[CREATE] Using success_url=%s  cancel_url=%s  count=%s", success_url, cancel_url, count)

    draft_ref = db.collection("pot_drafts").document()
    draft_ref.set({**draft, "status":"draft", "createdAt": utcnow()}, merge=True)

    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data": {
                "currency":"usd",
                "product_data":{"name": f"Create Pot — {draft.get('name') or 'Tournament'}"},
                "unit_amount": amount_cents
            },
            "quantity": count,
        }],
        # IMPORTANT: include session id so the success page can fetch results
        success_url=f"{success_url}?flow=create&session_id={{CHECKOUT_SESSION_ID}}",
        # cancel: we can still hop back to your frontend if you like
        cancel_url=f"{server_base(request)}/cancel-create?session_id={{CHECKOUT_SESSION_ID}}&next={quote(cancel_url)}",
        metadata={
            "draft_id": draft_ref.id,
            "flow":"create",
            "count": str(count),
            "organizer_email": org_email,
        },
    )

    db.collection("create_sessions").document(session["id"]).set({
        "draft_id": draft_ref.id,
        "count": count,
        "organizer_email": org_email,
        "createdAt": utcnow()
    })
    return {"draft_id": draft_ref.id, "url": session.url, "count": count}

@app.get("/cancel-create")
def cancel_create(session_id: str, next: str = "/"):
    db.collection("create_sessions").document(session_id).delete()
    return RedirectResponse(next, status_code=302)

# ------------------- Join Pot Session -------------------
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
    if amount_cents < 50: raise HTTPException(400, "Minimum amount is 50 cents")
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "price_data":{
                "currency":"usd",
                "product_data":{"name": f"Join Pot — {payload.player_name or 'Player'}"},
                "unit_amount": amount_cents
            },
            "quantity":1
        }],
        customer_email=payload.player_email,
        success_url=f"{payload.success_url}?flow=join&session_id={{CHECKOUT_SESSION_ID}}&pot_id={pot_id}&entry_id={entry_id}",
        cancel_url=f"{server_base(request)}/cancel-join?session_id={{CHECKOUT_SESSION_ID}}&pot_id={pot_id}&entry_id={entry_id}&next={quote(payload.cancel_url)}",
        metadata={
            "flow":"join",
            "pot_id": pot_id,
            "entry_id": entry_id,
            "player_email": payload.player_email or "",
            "player_name": payload.player_name or "",
        },
    )
    db.collection("join_sessions").document(session["id"]).set({"pot_id": pot_id,"entry_id": entry_id,"createdAt": utcnow()})
    return {"url": session.url, "session_id": session["id"]}

@app.get("/cancel-join")
def cancel_join(session_id: str, pot_id: Optional[str] = None, entry_id: Optional[str] = None, next: str = "/"):
    db.collection("join_sessions").document(session_id).delete()
    return RedirectResponse(next, status_code=302)

# ------------------- Webhook -------------------
@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(400, str(e))

    etype = event.get("type")
    obj = event.get("data",{}).get("object",{})
    if etype == "checkout.session.completed":
        session = obj
        flow = (session.get("metadata") or {}).get("flow")
        log.info("[WEBHOOK] checkout.session.completed flow=%s session_id=%s", flow, session.get("id"))

        if flow == "create":
            draft_id = (session.get("metadata") or {}).get("draft_id")
            count = int((session.get("metadata") or {}).get("count","1"))
            organizer_email = (session.get("metadata") or {}).get("organizer_email","")
            results = []
            if draft_id:
                draft = db.collection("pot_drafts").document(draft_id).get().to_dict() or {}
                for _ in range(max(1,count)):
                    pot_id = db.collection("pots").document().id
                    code = random_owner_code()
                    salt = b64url_encode(os.urandom(12))
                    db.collection("pots").document(pot_id).set({
                        **draft, "status":"active","createdAt": utcnow(),
                        "source":"checkout","draft_id": draft_id,
                        "stripe_session_id": session["id"],
                        "amount_total": session.get("amount_total"),
                        "currency": session.get("currency","usd"),
                        "owner_code_hash": hash_code(code),
                        "owner_token_salt": salt,
                    }, merge=True)
                    token = make_owner_token(pot_id)
                    manage_url = f"{FRONTEND_BASE_URL}{FRONTEND_MANAGE_PATH}?pot={pot_id}&key={token}"
                    db.collection("owner_links").document(pot_id).set(
                        {"manage_url": manage_url, "createdAt": firestore.SERVER_TIMESTAMP}, merge=True
                    )
                    results.append({"pot_id": pot_id, "manage_url": manage_url, "owner_code": code})

                db.collection("create_results").document(session["id"]).set(
                    {"pots": results, "createdAt": firestore.SERVER_TIMESTAMP}, merge=True
                )
                db.collection("pot_drafts").document(draft_id).delete()
                db.collection("create_sessions").document(session["id"]).delete()

                if organizer_email:
                    email_organizer_links(organizer_email, results)

        elif flow == "join":
            pot_id = (session.get("metadata") or {}).get("pot_id")
            entry_id = (session.get("metadata") or {}).get("entry_id")
            if pot_id and entry_id:
                ref = db.collection("pots").document(pot_id).collection("entries").document(entry_id)
                ref.set({
                    "paid": True,
                    "paid_amount": session.get("amount_total"),
                    "paid_at": utcnow(),
                    "payment_method":"stripe",
                    "stripe_session_id": session["id"]
                }, merge=True)
                db.collection("join_sessions").document(session["id"]).delete()
    return JSONResponse({"received": True})

# ------------------- Lookup endpoint for success page -------------------
@app.get("/create-result")
def get_create_result(session_id: str):
    snap = db.collection("create_results").document(session_id).get()
    if not snap.exists:
        raise HTTPException(404, "Result not ready")
    return snap.to_dict()
