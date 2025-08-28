python
import os
import json
import hmac
import hashlib
import base64
import secrets
import string
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

# ---------- Optional: Firestore storage (fallbacks to in-memory) ----------
class MemoryStore:
   def __init__(self):
       self.sessions: Dict[str, Dict[str, Any]] = {}
       self.pots: Dict[str, Dict[str, Any]] = {}

   def save_session(self, sid: str, data: Dict[str, Any]):
       self.sessions[sid] = data

   def get_session(self, sid: str) -> Optional[Dict[str, Any]]:
       return self.sessions.get(sid)

   def save_pot(self, pot_id: str, data: Dict[str, Any]):
       self.pots[pot_id] = data

   def get_pot(self, pot_id: str) -> Optional[Dict[str, Any]]:
       return self.pots.get(pot_id)


STORE = MemoryStore()

# ---------- Email (optional) ----------
import smtplib
from email.message import EmailMessage

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "noreply@example.com")

def maybe_send_email(to_email: Optional[str], subject: str, html: str):
   """Send an email if SMTP env vars are configured; otherwise skip."""
   if not to_email:
       return
   if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
       # No email configuration; silently skip.
       return
   msg = EmailMessage()
   msg["From"] = SMTP_FROM
   msg["To"] = to_email
   msg["Subject"] = subject
   msg.set_content("HTML email required")
   msg.add_alternative(html, subtype="html")
   with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
       s.starttls()
       s.login(SMTP_USER, SMTP_PASS)
       s.send_message(msg)

# ---------- Stripe ----------
import stripe
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
if STRIPE_SECRET_KEY:
   stripe.api_key = STRIPE_SECRET_KEY

STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

# ---------- App ----------
app = FastAPI(title="PicklePot Backend")

frontend_origin = os.getenv("FRONTEND_ORIGIN", "https://picklepotters.netlify.app")
allowed = [frontend_origin, "http://localhost", "http://localhost:5173", "http://127.0.0.1:5173", "*"]

app.add_middleware(
   CORSMiddleware,
   allow_origins=allowed,
   allow_credentials=True,
   allow_methods=["*"],
   allow_headers=["*"],
)

@app.get("/health")
def health():
   return {"ok": True}

# ---------- Models ----------
class CreatePotPayload(BaseModel):
   success_url: str
   cancel_url: str
   amount_cents: int = Field(..., ge=50)
   count: int = Field(1, ge=1, le=16)
   organizer_name: Optional[str] = None
   organizer_email: Optional[str] = None
   tournament_name: Optional[str] = None
   skill_level: Optional[str] = None
   location: Optional[str] = None
   date: Optional[str] = None
   time: Optional[str] = None

class JoinPayload(BaseModel):
   pot_id: str
   entry_id: str
   amount_cents: int
   success_url: str
   cancel_url: str
   player_name: Optional[str] = None
   player_email: Optional[str] = None

def _rand_code(n=5) -> str:
   return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(n))

# ---------- Endpoints ----------
@app.post("/create-pot-session")
def create_pot_session(payload: CreatePotPayload, request: Request):
   if not STRIPE_SECRET_KEY:
       raise HTTPException(status_code=500, detail="Stripe not configured")

   # Create a draft record tied to this checkout session that we can fulfill in webhook
   draft_id = "draft_" + _rand_code(8)

   # A basic product/price on the fly
   price_data = {
       "currency": "usd",
       "unit_amount": payload.amount_cents,
       "product_data": {
           "name": payload.tournament_name or "PicklePot — Create Tournament",
           "description": f"Create tournament ({payload.count} pot{'s' if payload.count>1 else ''})",
       },
   }
   metadata = {
       "type": "create_pot",
       "draft_id": draft_id,
       "count": str(payload.count),
       "organizer_name": payload.organizer_name or "",
       "organizer_email": payload.organizer_email or "",
       "tournament_name": payload.tournament_name or "",
       "skill_level": payload.skill_level or "",
       "location": payload.location or "",
       "date": payload.date or "",
       "time": payload.time or "",
   }
   session = stripe.checkout.Session.create(
       mode="payment",
       line_items=[{"price_data": price_data, "quantity": 1}],
       success_url=payload.success_url + "?flow=create&session_id={CHECKOUT_SESSION_ID}",
       cancel_url=payload.cancel_url,
       metadata=metadata,
   )

   # Store the draft so /create-status can poll even before webhook arrives
   STORE.save_session(session.id, {
       "status": "pending",
       "draft_id": draft_id,
       "metadata": metadata,
       "organizer_email": payload.organizer_email or "",
   })
   return {"sessionId": session.id}

@app.post("/create-checkout-session")
def create_checkout_session(payload: JoinPayload):
   if not STRIPE_SECRET_KEY:
       raise HTTPException(status_code=500, detail="Stripe not configured")

   metadata = {
       "type": "join_pot",
       "pot_id": payload.pot_id,
       "entry_id": payload.entry_id,
       "player_name": payload.player_name or "",
       "player_email": payload.player_email or "",
   }
   price_data = {
       "currency": "usd",
       "unit_amount": payload.amount_cents,
       "product_data": {"name": f"Join Pot {payload.pot_id}"},
   }
   session = stripe.checkout.Session.create(
       mode="payment",
       line_items=[{"price_data": price_data, "quantity": 1}],
       success_url=payload.success_url + "?flow=join&session_id={CHECKOUT_SESSION_ID}",
       cancel_url=payload.cancel_url,
       metadata=metadata,
   )
   return {"sessionId": session.id}

@app.get("/create-status")
def create_status(session_id: str):
   rec = STORE.get_session(session_id)
   if not rec:
       # Could be after server restart; try Stripe lookup for status
       try:
           sess = stripe.checkout.Session.retrieve(session_id)
           if sess and sess.get("payment_status") == "paid":
               # Not found in memory; treat as pending fulfill
               return {"status": "processing"}
       except Exception:
           pass
       raise HTTPException(status_code=404, detail="Unknown session")

   out = {"status": rec["status"]}
   if rec["status"] == "ready":
       out.update({
           "pot_id": rec["pot_id"],
           "owner_code": rec["owner_code"],
           "manage_link": rec["manage_link"],
           "count": rec.get("count", 1),
       })
   return out

from fastapi import Header

@app.post("/webhook")
async def stripe_webhook(request: Request):
   payload = await request.body()
   sig = request.headers.get("stripe-signature")
   event = None
   try:
       if STRIPE_WEBHOOK_SECRET:
           event = stripe.Webhook.construct_event(
               payload=payload, sig_header=sig, secret=STRIPE_WEBHOOK_SECRET
           )
       else:
           event = json.loads(payload)
   except Exception as e:
       raise HTTPException(status_code=400, detail=str(e))

   if event["type"] == "checkout.session.completed":
       session = event["data"]["object"]
       sid = session["id"]
       meta = session.get("metadata") or {}
       if meta.get("type") == "create_pot":
           # Fulfill: make a pot id + owner code + manage link
           pot_id = "pot_" + _rand_code(6)
           owner_code = _rand_code(5)
           count = int(meta.get("count", "1") or "1")
           manage_link = f"{frontend_origin.rstrip('/')}/manage.html?pot={pot_id}&key={owner_code}"

           STORE.save_pot(pot_id, {
               "owner_code": owner_code,
               "count": count,
               "organizer_email": meta.get("organizer_email") or "",
               "tournament_name": meta.get("tournament_name") or "",
           })
           # mark session ready
           rec = STORE.get_session(sid) or {}
           rec.update({
               "status": "ready",
               "pot_id": pot_id,
               "owner_code": owner_code,
               "manage_link": manage_link,
               "count": count,
           })
           STORE.save_session(sid, rec)

           # Email the organizer if provided
           to_email = meta.get("organizer_email")
           if to_email:
               html = f"""
               <h2>Your PicklePot is ready</h2>
               <p><strong>Pot ID:</strong> {pot_id}</p>
               <p><strong>Owner Code:</strong> {owner_code}</p>
               <p><a href="{manage_link}">Open Organizer Manage Page</a></p>
               """
               maybe_send_email(to_email, "Your PicklePot — Organizer Access", html)

   return {"received": True}


if __name__ == "__main__":
   port = int(os.getenv("PORT", "10000"))
   uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)