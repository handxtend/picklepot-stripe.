
# app.py — PiCo Pickle Pot backend (subscriptions + joins)
import os, json
from typing import Dict, Any
from flask import Flask, request, jsonify
from flask_cors import CORS
import stripe
import firebase_admin
from firebase_admin import credentials, firestore

# ----- Config from environment -----
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# 4 live prices (can be overridden by env)
IND_M = os.environ.get("STRIPE_PRICE_ID_INDIVIDUAL_MONTHLY", "price_1Rwq6nFFPAbZxH9HkmDxBJ73")
IND_Y = os.environ.get("STRIPE_PRICE_ID_INDIVIDUAL_YEARLY",  "price_1RwptxFFPAbZxH9HdPLdYIZR")
CLB_M = os.environ.get("STRIPE_PRICE_ID_CLUB_MONTHLY",       "price_1Rwq1JFFPAbZxH9HmpYCSJYv")
CLB_Y = os.environ.get("STRIPE_PRICE_ID_CLUB_YEARLY",        "price_1RwpyUFFPAbZxH9H2N1Ykd4U")

PLAN_CONFIG: Dict[str, Dict[str, Any]] = {
    IND_M: {"plan": "individual", "interval": "month", "pots_per_month": 2,  "max_users_per_event": 12},
    IND_Y: {"plan": "individual", "interval": "year",  "pots_per_month": 2,  "max_users_per_event": 12},
    CLB_M: {"plan": "club",       "interval": "month", "pots_per_month": 10, "max_users_per_event": 64},
    CLB_Y: {"plan": "club",       "interval": "year",  "pots_per_month": 10, "max_users_per_event": 64},
}
ALLOWED_PRICE_IDS = [p for p in {IND_M, IND_Y, CLB_M, CLB_Y} if p]

# Firestore service account
FIREBASE_SERVICE_ACCOUNT_JSON = (
    os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    or ""
)

# CORS
CORS_ALLOW = os.environ.get("CORS_ALLOW") or os.environ.get("CORS_ORIGINS") or "*"

# Stripe init
stripe.api_key = STRIPE_SECRET_KEY

# Flask
app = Flask(__name__)
if CORS_ALLOW == "*" or not CORS_ALLOW:
    CORS(app)
else:
    CORS(app, resources={r"/*": {"origins": [o.strip() for o in CORS_ALLOW.split(",") if o.strip()]}})

@app.after_request
def add_cors_headers(resp):
    resp.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, Authorization")
    resp.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    return resp

# Firestore init
if not firebase_admin._apps:
    if FIREBASE_SERVICE_ACCOUNT_JSON.strip().startswith("{"):
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT_JSON))
    elif os.path.isfile(FIREBASE_SERVICE_ACCOUNT_JSON):
        cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_JSON)
    else:
        raise RuntimeError("Set FIREBASE_SERVICE_ACCOUNT_JSON to the service-account JSON string or file path.")
    firebase_admin.initialize_app(cred)
db = firestore.client()

# ---------- Helpers ----------
ACTIVE = {"active", "trialing", "past_due"}

def _extract_sub_bits(sub: Dict[str, Any]) -> Dict[str, Any]:
    items = (sub.get("items", {}) or {}).get("data") or []
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

def _write_email_row(email_lc: str, customer_id: str, sub: Dict[str, Any]):
    doc = {
        "email": email_lc,
        "status": sub.get("status"),
        "current_period_end": sub.get("current_period_end"),
        "stripe_customer_id": sub.get("customer"),
        "stripe_subscription_id": sub.get("id"),
        "updated_at": firestore.SERVER_TIMESTAMP,
        **_extract_sub_bits(sub),
    }
    db.collection("organizer_subs_emails").document(email_lc).set(doc, merge=True)

def _write_uid_row(uid: str, info: Dict[str, Any]):
    # info is a payload from email row or direct extract
    payload = {**info, "uid": uid, "updated_at": firestore.SERVER_TIMESTAMP}
    db.collection("organizer_subs").document(uid).set(payload, merge=True)

# ---------- Routes ----------
@app.get("/")
def root():
    return "ok", 200

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.post("/create-organizer-subscription")
def create_organizer_subscription():
    data = request.get_json(force=True, silent=True) or {}
    price_id = (data.get("price_id") or "").strip()
    success_url = data.get("success_url")
    cancel_url = data.get("cancel_url")
    email = (data.get("email") or "").strip() or None
    if not (price_id and success_url and cancel_url):
        return jsonify({"error": "Missing price_id/success_url/cancel_url"}), 400
    if price_id not in ALLOWED_PRICE_IDS:
        return jsonify({"error": "Invalid price_id"}), 400
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            customer_email=email,   # pre-fill + create Customer for email
            allow_promotion_codes=True,
        )
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.post("/create-checkout-session")
def create_checkout_session():
    data = request.get_json(force=True, silent=True) or {}
    pot_id = data.get("pot_id")
    entry_id = data.get("entry_id")
    amount_cents = int(data.get("amount_cents") or 0)
    player_name = data.get("player_name") or "Player"
    player_email = data.get("player_email") or None
    success_url = data.get("success_url")
    cancel_url = data.get("cancel_url")
    if not (pot_id and entry_id and success_url and cancel_url):
        return jsonify({"error":"Missing pot_id/entry_id/success_url/cancel_url"}), 400
    if amount_cents < 50:
        return jsonify({"error":"Minimum amount is 50 cents"}), 400
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            customer_email=player_email,
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": amount_cents,
                    "product_data": {"name": f"Pot Join — {player_name}"}
                },
                "quantity": 1
            }],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "pot_id": pot_id, "entry_id": entry_id,
                "player_email": player_email or "", "player_name": player_name or ""
            }
        )
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.post("/activate-subscription-for-uid")
def activate_subscription_for_uid():
    data = request.get_json(force=True, silent=True) or {}
    uid = data.get("uid"); email = (data.get("email") or "").strip().lower()
    if not (uid and email): return jsonify({"error":"Missing uid/email"}), 400
    snap = db.collection("organizer_subs_emails").document(email).get()
    if not snap.exists: return jsonify({"error":"No subscription found for that email"}), 404
    info = snap.to_dict() or {}
    if (info.get("status") or "") not in ACTIVE:
        return jsonify({"error": f"Subscription not active (status={info.get('status')})"}), 400
    _write_uid_row(uid, info)
    return jsonify({"ok": True, "attached_to_uid": uid})

@app.post("/stripe-webhook")
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature","")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        return "Invalid signature", 400
    except Exception:
        return "Invalid payload", 400

    etype = event.get("type")
    obj = event.get("data",{}).get("object",{})
    try:
        if etype == "checkout.session.completed":
            if (obj.get("mode") == "subscription"):
                # Use the email on the session; Stripe will also create the Customer
                email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email")
                email_lc = (email or "").lower()
                sub_id = obj.get("subscription")
                if email_lc and sub_id:
                    sub = stripe.Subscription.retrieve(sub_id)
                    _write_email_row(email_lc, obj.get("customer"), sub)
            elif (obj.get("mode") == "payment"):
                # Pot join: mark entry as paid
                pot_id = (obj.get("metadata") or {}).get("pot_id")
                entry_id = (obj.get("metadata") or {}).get("entry_id")
                amount_total = obj.get("amount_total")
                payment_intent = obj.get("payment_intent")
                if pot_id and entry_id:
                    db.collection("pots").document(pot_id).collection("entries").document(entry_id).set({
                        "paid": True,
                        "paid_amount": amount_total,
                        "stripe_session_id": obj.get("id"),
                        "stripe_payment_intent_id": payment_intent,
                        "paid_at": firestore.SERVER_TIMESTAMP,
                    }, merge=True)

        elif etype in ("invoice.payment_succeeded","customer.subscription.updated",
                       "customer.subscription.deleted","customer.subscription.paused"):
            if etype.startswith("customer.subscription."):
                sub = obj
                customer_id = sub.get("customer")
            else:
                sub_id = obj.get("subscription")
                customer_id = obj.get("customer")
                sub = stripe.Subscription.retrieve(sub_id) if sub_id else None
            if sub:
                try:
                    cust = stripe.Customer.retrieve(customer_id) if customer_id else None
                    email = (cust.get("email") if cust else None) or ""
                    email_lc = email.lower() if email else None
                except Exception:
                    email_lc = None
                if email_lc:
                    _write_email_row(email_lc, customer_id, sub)
    except Exception as e:
        print("webhook handler error:", e)

    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
