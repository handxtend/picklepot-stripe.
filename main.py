import os
import uuid
import smtplib
from email.message import EmailMessage
from typing import Optional

import stripe
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---- Configuration ----
# Required
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
if not STRIPE_SECRET_KEY:
    raise RuntimeError("STRIPE_SECRET_KEY is not set")

stripe.api_key = STRIPE_SECRET_KEY

# Where your frontend is hosted:
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "https://picklepotters.netlify.app").rstrip("/")
PUBLIC_BASE_URL = FRONTEND_ORIGIN  # used to build links to manage.html

# Optional SMTP (no SendGrid required). Leave these blank to disable email.
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587").strip() or "587")
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "").strip()

# In-memory store so we can demonstrate end-to-end without a DB.
# Replace with Firestore in production.
POTS = {}          # pot_id -> dict
DRAFTS = {}        # draft_id -> dict  (payload snapshot for create flow)
SESSION_TO_POT = {}  # checkout session id -> pot_id

app = FastAPI(title="PicklePot Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN, "http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5500"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


class CreatePotPayload(BaseModel):
    # What your front-end collects. Add/rename safely.
    tournament_name: Optional[str] = None
    organizer: Optional[str] = None
    event: Optional[str] = None
    skill: Optional[str] = None
    member_buy_in: Optional[float] = None
    guest_buy_in: Optional[float] = None
    pot_percent: Optional[int] = 100
    date: Optional[str] = None
    time: Optional[str] = None
    location: Optional[str] = None
    onsite_payment: Optional[str] = None
    zelle_info: Optional[str] = None
    cashapp_info: Optional[str] = None

    # pricing
    amount_cents: int
    count: int = 1

    # checkout redirects
    success_url: str
    cancel_url: str

    # helpful to have
    organizer_email: Optional[str] = None


def _make_pot_id() -> str:
    return f"pot_{uuid.uuid4().hex[:10]}"

def _make_owner_code() -> str:
    return uuid.uuid4().hex[:5].upper()

def _make_manage_key() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex[:8]

def _send_email(to_addr: str, subject: str, body: str) -> None:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and SMTP_FROM and to_addr):
        return  # emailing disabled or no recipient
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/create-pot-session")
@app.post("/create-checkout-session")  # backwards compat
def create_pot_session(payload: CreatePotPayload):
    # Snapshot the draft so we can finish creation later.
    draft_id = uuid.uuid4().hex
    DRAFTS[draft_id] = payload.dict()

    # Compose success URL that your success.html understands.
    success_url = f"{payload.success_url.rstrip('?')}" \
                  f"?flow=create&session_id={{CHECKOUT_SESSION_ID}}"

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            success_url=success_url,
            cancel_url=payload.cancel_url,
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": payload.tournament_name or "PicklePot Entry"},
                    "unit_amount": payload.amount_cents,
                },
                "quantity": max(1, payload.count),
            }],
            metadata={"draft_id": draft_id},
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"url": session.url, "id": session.id}


@app.get("/create-status")
def create_status(session_id: str = Query(..., description="Stripe Checkout session id")):
    """
    Called by success.html; finishes the 'create a pot' flow:
    - checks Stripe session
    - creates the pot, owner code, and manage link
    - returns data to display + store client-side
    """
    # If we've already created a pot for this session, return it.
    pot_id = SESSION_TO_POT.get(session_id)
    if pot_id:
        pot = POTS[pot_id]
        return {"ready": True, **pot}

    # Fetch session from Stripe
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Stripe: {e}")

    # Not paid/complete yet?
    if session.get("payment_status") != "paid":
        return {"ready": False}

    # Pull the draft
    draft_id = (session.get("metadata") or {}).get("draft_id")
    if not draft_id or draft_id not in DRAFTS:
        # We allow idempotency: if pot already exists we’d have returned above.
        # If we get here, we don't have context to build a pot, treat as error.
        raise HTTPException(status_code=400, detail="Missing draft for this session")

    draft = DRAFTS.pop(draft_id)

    # Create the pot record
    pot_id = _make_pot_id()
    owner_code = _make_owner_code()
    manage_key = _make_manage_key()
    manage_link = f"{PUBLIC_BASE_URL}/manage.html?pot={pot_id}&key={manage_key}"

    pot_record = {
        "pot_id": pot_id,
        "owner_code": owner_code,
        "manage_key": manage_key,
        "manage_link": manage_link,
        "tournament_name": draft.get("tournament_name"),
        "organizer": draft.get("organizer"),
        "event": draft.get("event"),
        "skill": draft.get("skill"),
        "member_buy_in": draft.get("member_buy_in"),
        "guest_buy_in": draft.get("guest_buy_in"),
        "pot_percent": draft.get("pot_percent") or 100,
        "location": draft.get("location"),
        "date": draft.get("date"),
        "time": draft.get("time"),
        "onsite_payment": draft.get("onsite_payment"),
        "zelle_info": draft.get("zelle_info"),
        "cashapp_info": draft.get("cashapp_info"),
        "count": draft.get("count") or 1,
        "amount_cents": draft.get("amount_cents"),
        "organizer_email": draft.get("organizer_email"),
        "created_from_session": session_id,
    }

    # Persist it (in-memory demo; replace with Firestore writes)
    POTS[pot_id] = pot_record
    SESSION_TO_POT[session_id] = pot_id

    # Optional organizer email
    if draft.get("organizer_email"):
        body = (
            f"Your PicklePot was created!\n\n"
            f"Pot ID: {pot_id}\n"
            f"Owner Code: {owner_code}\n"
            f"Organizer Manage Link: {manage_link}\n\n"
            f"Keep this email for your records."
        )
        try:
            _send_email(
                to_addr=draft["organizer_email"],
                subject="Your PicklePot Link & Owner Code",
                body=body
            )
        except Exception:
            # Don't fail the request if email can't be sent
            pass

    return {"ready": True, **pot_record}
