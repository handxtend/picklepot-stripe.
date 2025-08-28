import os
import hmac, hashlib, base64, time
from typing import Optional, List, Dict, Any

import stripe
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

# ---- Environment ----
STRIPE_SECRET = os.getenv("STRIPE_SECRET", "")
OWNER_TOKEN_SECRET = os.getenv("OWNER_TOKEN_SECRET", "change-me")
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "https://picklepotters.netlify.app")
ALLOWED_ORIGINS = [
    FRONTEND_BASE_URL,
    "https://picklepotters.netlify.app",
    "http://localhost:5173",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
]

# Stripe client (safe even if key is empty; endpoints that need it will check)
if STRIPE_SECRET:
    stripe.api_key = STRIPE_SECRET

# ---- Firestore ----
# Uses GOOGLE_APPLICATION_CREDENTIALS in environment (Render/Google best practice).
try:
    from google.cloud import firestore  # type: ignore
    db = firestore.Client()
except Exception:
    db = None  # Service can still boot; /health will show no DB.

def utcnow() -> int:
    return int(time.time())

def b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

def hash_code(code: str) -> str:
    mac = hmac.new(OWNER_TOKEN_SECRET.encode(), code.encode(), hashlib.sha256).digest()
    return b64url_encode(mac)

def make_owner_token(pot_id: str) -> str:
    mac = hmac.new(OWNER_TOKEN_SECRET.encode(), pot_id.encode(), hashlib.sha256).digest()
    return f"{pot_id}.{b64url_encode(mac)}"

app = FastAPI(title="PicklePot Backend — drop‑in helpers")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET","POST","OPTIONS"],
    allow_headers=["*"],
)

@app.get("/health", include_in_schema=False)
def health():
    return {
        "ok": True,
        "db": bool(db),
        "stripe": bool(STRIPE_SECRET),
        "service": "picklepot helpers"
    }

# ------------------------------------------------------------------------------------
# 1) Alias used by the success page to poll for results written by your Stripe webhook
#    (your webhook should write documents into collection `create_results/{session_id}`
#     with shape: { results: [{pot_id, manage_url, owner_code}], ... })
# ------------------------------------------------------------------------------------
@app.get("/create-status")
def create_status(session_id: str):
    if not db:
        raise HTTPException(503, "database unavailable")
    doc = db.collection("create_results").document(session_id).get()
    if not doc.exists:
        # still waiting on the webhook to populate
        return {"ready": False}
    data = doc.to_dict() or {}
    return {"ready": True, "results": data.get("results", []), "raw": data}

# ------------------------------------------------------------------------------------
# 2) Resolve an owner code OR an owner manage link token to a pot_id
#    – enables auto-filling the Pot ID on /manage.html
# ------------------------------------------------------------------------------------
@app.get("/resolve-owner-code")
def resolve_owner_code(code: Optional[str] = None, token: Optional[str] = None):
    if not db:
        raise HTTPException(503, "database unavailable")
    # Prefer token (key=... from manage link). Token is "<pot>.<sig>"
    if token:
        try:
            pot_id, sig = token.split(".", 1)
        except ValueError:
            raise HTTPException(400, "bad token")
        # Best-effort verify
        expected = make_owner_token(pot_id).split(".", 1)[1]
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(400, "invalid token")
        return {"pot_id": pot_id, "via": "token"}

    # Otherwise try owner code lookup (DB stores only a hash)
    if not code:
        raise HTTPException(400, "missing code or token")
    hashed = hash_code(code)
    # You likely store the hash either on the pot document as `owner_code_hash` or
    # in a side collection. We check both approaches.
    # A) direct collection "pots" with field owner_code_hash
    q = (
        db.collection("pots")
          .where("owner_code_hash", "==", hashed)
          .limit(1)
          .stream()
    )
    for snap in q:
        d = snap.to_dict() or {}
        pot_id = d.get("id") or snap.id
        return {"pot_id": pot_id, "via": "hash"}

    # B) side collection "owner_codes/{codeHash} -> {pot_id}"
    snap = db.collection("owner_codes").document(hashed).get()
    if snap.exists:
        d = snap.to_dict() or {}
        pot_id = d.get("pot_id")
        if pot_id:
            return {"pot_id": pot_id, "via": "map"}

    raise HTTPException(404, "not found")

# ------------------------------------------------------------------------------------
# (Optional convenience) simple redirect that turns ?pot=...&key=... into manage page
# ------------------------------------------------------------------------------------
@app.get("/go/manage", include_in_schema=False)
def go_manage(pot: str, key: str):
    url = f"{FRONTEND_BASE_URL}/manage.html?pot={pot}&key={key}"
    return RedirectResponse(url, status_code=302)