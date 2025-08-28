# FastAPI backend patch for PicklePot
# Provides:
#   - /create-status?session_id=...   -> returns organizer manage info when webhook has finished
#   - /resolve-owner-code?code=...    -> resolves owner short code to pot_id
#   - /resolve-owner-code?token=...   -> resolves long owner link token to pot_id
#   - Stripe webhook /webhook         -> writes create result on checkout.session.completed
#
# Firestore is optional: if unavailable, falls back to in-memory stores (good for dev).
# Env:
#   STRIPE_SECRET_KEY       (required)
#   STRIPE_WEBHOOK_SECRET   (recommended for webhook verification)
#   NETLIFY_SITE            (default: https://picklepotters.netlify.app)
#   MANAGE_BASE             (default: https://picklepotters.netlify.app/manage.html)

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import os, hmac, hashlib, json

# ---- Optional Firestore ----
db = None
try:
    from google.cloud import firestore
    db = firestore.Client()  # requires GOOGLE_APPLICATION_CREDENTIALS or ADC on Render
except Exception:
    db = None

# ---- Stripe ----
import stripe
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
NETLIFY_SITE = os.environ.get("NETLIFY_SITE", "https://picklepotters.netlify.app")
MANAGE_BASE = os.environ.get("MANAGE_BASE", f"{NETLIFY_SITE.rstrip('/')}/manage.html")

def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()

# In-memory fallback stores
_mem_create_results: Dict[str, Dict[str, Any]] = {}   # session_id -> result doc
_mem_owner_keys: Dict[str, str] = {}                  # code/token -> pot_id

def _fs_set(doc_path: str, data: Dict[str, Any]):
    """doc_path like 'create_results/{id}'"""
    if db:
        parts = doc_path.split('/')
        col = db.collection(parts[0])
        doc = col.document(parts[1])
        doc.set(data, merge=True)
    else:
        head, _, key = doc_path.partition('/')
        if head == 'create_results':
            _mem_create_results[key] = data
        elif head == 'owner_keys':
            _mem_owner_keys[key] = data.get('pot_id', '')

def _fs_get(doc_path: str) -> Optional[Dict[str, Any]]:
    if db:
        parts = doc_path.split('/')
        snap = db.collection(parts[0]).document(parts[1]).get()
        return snap.to_dict() if snap.exists else None
    else:
        head, _, key = doc_path.partition('/')
        if head == 'create_results':
            return _mem_create_results.get(key)
        elif head == 'owner_keys':
            pid = _mem_owner_keys.get(key)
            return {'pot_id': pid} if pid else None
        return None

app = FastAPI(title="PicklePot Backend Patch")

# CORS (allow Netlify app + localhost dev)
allowed_origins = [
    NETLIFY_SITE,
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class StatusResponse(BaseModel):
    ready: bool
    results: Optional[list] = None

@app.get("/create-status", response_model=StatusResponse)
async def create_status(session_id: str):
    doc = _fs_get(f"create_results/{session_id}")
    if not doc:
        # not ready yet
        return StatusResponse(ready=False)
    # Return compact payload the success page expects
    return StatusResponse(
        ready=True,
        results=[{
            "pot_id": doc.get("pot_id"),
            "owner_code": doc.get("owner_code"),
            "owner_token": doc.get("owner_token"),
            "manage_url": doc.get("manage_url"),
            "created_at": doc.get("created_at"),
        }]
    )

@app.get("/resolve-owner-code")
async def resolve_owner_code(code: Optional[str] = None, token: Optional[str] = None):
    # Either a short owner code OR a long token
    key = code or token
    if not key:
        raise HTTPException(status_code=400, detail="Provide code or token")
    kdoc = _fs_get(f"owner_keys/{key}")
    if not kdoc or not kdoc.get("pot_id"):
        raise HTTPException(status_code=404, detail="Unknown code/token")
    return {"pot_id": kdoc["pot_id"]}

@app.get("/health")
async def health():
    return {"ok": True, "time": utcnow_iso()}

def _make_owner_code(pot_id: str) -> str:
    # 5-char base36 code derived from pot_id
    h = hashlib.sha256(pot_id.encode()).hexdigest()
    num = int(h[:8], 16)
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = []
    for _ in range(5):
        out.append(alphabet[num % 36])
        num //= 36
    return "".join(out)

def _calc_token(pot_id: str, session_id: str) -> str:
    secret = (WEBHOOK_SECRET or os.environ.get("STRIPE_SECRET_KEY","")).encode()
    raw = f"{pot_id}:{session_id}".encode()
    return hashlib.sha256(secret + raw).hexdigest()

def _compose_manage_url(pot_id: str, owner_token: str) -> str:
    # /manage.html?pot=...&key=...
    return f"{MANAGE_BASE}?pot={pot_id}&key={owner_token}"

@app.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("Stripe-Signature")
    event = None
    if WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(
                payload=payload, sig_header=sig_header, secret=WEBHOOK_SECRET
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Webhook signature error: {e}")
    else:
        # No verification (dev)
        try:
            event = json.loads(payload.decode("utf-8"))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid payload: {e}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        session_id = session.get("id")
        # You can derive a pot_id deterministically or from metadata.
        # If your create flow passes metadata, prefer it. Fallback to deterministic id.
        pot_id = (session.get("metadata") or {}).get("pot_id")
        if not pot_id:
            # Deterministic pot id from session + email
            email = (session.get("customer_details") or {}).get("email", "")
            pot_id = ("pot_" + hashlib.sha1(f"{session_id}:{email}".encode()).hexdigest()[:8]).lower()

        owner_code = _make_owner_code(pot_id)
        owner_token = _calc_token(pot_id, session_id)
        manage_url = _compose_manage_url(pot_id, owner_token)

        # Persist for success page polling
        _fs_set(f"create_results/{session_id}", {
            "pot_id": pot_id,
            "owner_code": owner_code,
            "owner_token": owner_token,
            "manage_url": manage_url,
            "created_at": utcnow_iso(),
        })
        # Map code and token -> pot_id for manager resolution
        _fs_set(f"owner_keys/{owner_code}", {"pot_id": pot_id, "created_at": utcnow_iso()})
        _fs_set(f"owner_keys/{owner_token}", {"pot_id": pot_id, "created_at": utcnow_iso()})

    return {"received": True}
