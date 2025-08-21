# app.py - Flask backend for Organizer Subscription (Stripe + Firestore)
import os
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
import stripe

# --- Firestore (Firebase Admin) ---
import firebase_admin
from firebase_admin import credentials, firestore

# ---------- Config from environment ----------
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")

# Legacy monthly/yearly (kept for backward-compat)
STRIPE_PRICE_ID_MONTHLY = (
    os.environ.get("STRIPE_PRICE_ID")
    or os.environ.get("STRIPE_ORG_PRICE_ID", "")
)
STRIPE_PRICE_ID_YEARLY = os.environ.get("STRIPE_PRICE_ID_YEARLY", "")

# --- Stripe prices for organizers (Phase A) ---

IND_M = os.environ.get('STRIPE_PRICE_ID_INDIVIDUAL_MONTHLY', 'price_1Rwq6nFFPAbZxH9HkmDxBJ73')
IND_Y = os.environ.get('STRIPE_PRICE_ID_INDIVIDUAL_YEARLY',  'price_1RwptxFFPAbZxH9HdPLdYIZR')
CLB_M = os.environ.get('STRIPE_PRICE_ID_CLUB_MONTHLY',       'price_1Rwq1JFFPAbZxH9HmpYCSJYv')
CLB_Y = os.environ.get('STRIPE_PRICE_ID_CLUB_YEARLY',        'price_1RwpyUFFPAbZxH9H2N1Ykd4U')

PLAN_CONFIG = {
    IND_M: {'plan': 'individual', 'pots_per_month': 2,  'max_users_per_event': 12},
    IND_Y: {'plan': 'individual', 'pots_per_month': 2,  'max_users_per_event': 12},
    CLB_M: {'plan': 'club',       'pots_per_month': 10, 'max_users_per_event': 64},
    CLB_Y: {'plan': 'club',       'pots_per_month': 10, 'max_users_per_event': 64},
}
ALLOWED_PRICE_IDS = list(PLAN_CONFIG.keys())
# Defaults provided; can be overridden by environment on Render.
STRIPE_PRICE_ID_INDIVIDUAL = os.environ.get("STRIPE_PRICE_ID_INDIVIDUAL", "price_1Rwq6nFFPAbZxH9HkmDxBJ73")
STRIPE_PRICE_ID_CLUB       = os.environ.get("STRIPE_PRICE_ID_CLUB",       "price_1Rwq1JFFPAbZxH9HmpYCSJYv")

# (removed duplicate 2-price PLAN_CONFIG)

# Webhook secret
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# Accept JSON string or file path under common env names
FIREBASE_SERVICE_ACCOUNT_JSON = (
    os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    or ""
)

# Optional CORS allowlist (comma-separated origins). If blank, allow all.
CORS_ALLOW = os.environ.get("CORS_ALLOW") or os.environ.get("CORS_ORIGINS") or "*"

# Whitelist allowed Stripe Prices (prevents tampering from the client)
_FOUR_PRICES = [IND_M, IND_Y, CLB_M, CLB_Y]
_LEGACY = [STRIPE_PRICE_ID_MONTHLY, STRIPE_PRICE_ID_YEARLY, STRIPE_PRICE_ID_INDIVIDUAL if 'STRIPE_PRICE_ID_INDIVIDUAL' in globals() else '', STRIPE_PRICE_ID_CLUB if 'STRIPE_PRICE_ID_CLUB' in globals() else '']
_ALLOWED_SET = set([p for p in (_FOUR_PRICES + _LEGACY) if p])
ALLOWED_PRICE_IDS = [p for p in _ALLOWED_SET if p]
print('[startup] Allowed price IDs at startup:', ALLOWED_PRICE_IDS)

stripe.api_key = STRIPE_SECRET_KEY

# ---------- Initialize Flask ----------
app = Flask(__name__)
if CORS_ALLOW == "*" or not CORS_ALLOW:
    CORS(app)
else:
    origins = [o.strip() for o in CORS_ALLOW.split(",") if o.strip()]
    CORS(
        app,
        resources={r"/*": {"origins": origins}},
        supports_credentials=False,
        allow_headers=["Content-Type", "Authorization"],
        methods=["GET", "POST", "OPTIONS"],
    )

@app.after_request
def add_cors_headers(resp):
    # Ensure these are always present (helps some proxies)
    resp.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, Authorization")
    resp.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    return resp

# ---------- Initialize Firestore ----------
if not firebase_admin._apps:
    if FIREBASE_SERVICE_ACCOUNT_JSON.strip().startswith("{"):
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT_JSON))
    elif os.path.isfile(FIREBASE_SERVICE_ACCOUNT_JSON):
        cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_JSON)
    else:
        raise RuntimeError(
            "Set FIREBASE_SERVICE_ACCOUNT_JSON to the JSON string or file path for your service account."
        )
    firebase_admin.initialize_app(cred)
db = firestore.client()

# ---------- Helpers ----------
def get_or_create_customer_for_uid(uid: str):
    """Look up a Stripe customer mapped to this uid in Firestore; create if missing."""
    doc_ref = db.collection("stripe_customers").document(uid)
    doc = doc_ref.get()
    if doc.exists:
        cid = (doc.to_dict() or {}).get("customer_id")
        if cid:
            try:
                stripe.Customer.retrieve(cid)
                return cid
            except Exception:
                pass  # fall through to re-create if invalid

    # Create new customer tagged with uid
    customer = stripe.Customer.create(metadata={"uid": uid})
    doc_ref.set({"customer_id": customer["id"]}, merge=True)
    return customer["id"]

def _extract_price_info_from_subscription(subscription_obj: dict):
    """Return (price_id, interval, amount_cents, currency)."""
    items = (subscription_obj.get("items", {}) or {}).get("data") or []
    first = items[0] if items else {}
    price = (first or {}).get("price") or {}
    recurring = price.get("recurring") or {}
    return (
        price.get("id"),
        recurring.get("interval"),
        price.get("unit_amount"),
        price.get("currency"),
    )

def _plan_bits_from_price_id(price_id: str):
    """Look up plan metadata (plan name + limits) from a price_id."""
    info = PLAN_CONFIG.get(price_id) or {}
    return {
        "plan": info.get("plan"),
        "pots_per_month": info.get("pots_per_month"),
        "max_users_per_event": info.get("max_users_per_event"),
    }

def write_subscription_status(uid: str, customer_id: str, subscription_obj: dict):
    """
    Write subscription fields to Firestore: organizer_subs/{uid}
    Includes plan (price_id), interval, amount, currency, status, period end, and Phase A limits.
    """
    status = subscription_obj.get("status")
    current_period_end = subscription_obj.get("current_period_end")  # unix ts (sec)
    current_period_end_ms = int(current_period_end) * 1000 if current_period_end else None

    price_id, interval, amount_cents, currency = _extract_price_info_from_subscription(subscription_obj)
    plan_bits = _plan_bits_from_price_id(price_id or "")

    doc_ref = db.collection("organizer_subs").document(uid)
    payload = {
        "status": status,
        "current_period_end": firestore.SERVER_TIMESTAMP if current_period_end_ms is None else current_period_end_ms,
        "stripe_customer_id": customer_id,
        "stripe_subscription_id": subscription_obj.get("id"),
        # Plan details
        "price_id": price_id,
        "interval": interval,                # "month" | "year" (if configured in Stripe)
        "amount_cents": amount_cents,
        "currency": currency,
        # Phase A: limits
        **plan_bits,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    doc_ref.set(payload, merge=True)

# ---------- Routes ----------
@app.route("/healthz", methods=["GET"])
def healthz():
    return "ok", 200

@app.get("/")
def root():
    return "ok", 200

@app.route("/create-organizer-subscription", methods=["POST"])
def create_subscription():
    """Create a Stripe Checkout Session (mode=subscription) for the organizer plan."""
    data = request.get_json(force=True, silent=True) or {}
    uid = data.get("uid")
    success_url = data.get("success_url")
    cancel_url = data.get("cancel_url")

    # Accept explicit price_id (from UI plan selector). Fall back: individual -> monthly legacy.
    price_id = (data.get("price_id") or STRIPE_PRICE_ID_INDIVIDUAL or STRIPE_PRICE_ID_MONTHLY or "").strip()

    if not (uid and success_url and cancel_url and price_id):
        return jsonify({"error": "Missing uid/success_url/cancel_url or price_id"}), 400

    # Prevent tampering: only allow known price IDs
    if price_id not in ALLOWED_PRICE_IDS:
        return jsonify({"error": "Invalid price_id"}), 400

    try:
        customer_id = get_or_create_customer_for_uid(uid)
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            customer=customer_id,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=uid,
            metadata={"uid": uid},
            allow_promotion_codes=True,
        )
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    """Handle Stripe webhook events to activate/deactivate organizer subscription."""
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        return "Invalid signature", 400
    except Exception:
        return "Invalid payload", 400

    event_type = event["type"]
    data_obj = event["data"]["object"]

    try:
        if event_type == "checkout.session.completed":
            uid = data_obj.get("client_reference_id") or (data_obj.get("metadata") or {}).get("uid")
            subscription_id = data_obj.get("subscription")
            customer_id = data_obj.get("customer")
            if uid and subscription_id:
                sub = stripe.Subscription.retrieve(subscription_id)
                write_subscription_status(uid, customer_id, sub)

        elif event_type in ("invoice.payment_succeeded", "customer.subscription.updated"):
            subscription_id = data_obj.get("subscription") if event_type == "invoice.payment_succeeded" else data_obj.get("id")
            customer_id = data_obj.get("customer") if event_type == "invoice.payment_succeeded" else data_obj.get("customer")
            if subscription_id:
                sub = stripe.Subscription.retrieve(subscription_id)
                customer = stripe.Customer.retrieve(customer_id) if customer_id else None
                uid = customer["metadata"].get("uid") if customer and customer.get("metadata") else None
                if uid:
                    write_subscription_status(uid, customer_id, sub)

        elif event_type in ("customer.subscription.deleted", "customer.subscription.paused"):
            sub = data_obj
            customer_id = sub.get("customer")
            customer = stripe.Customer.retrieve(customer_id) if customer_id else None
            uid = customer["metadata"].get("uid") if customer and customer.get("metadata") else None
            if uid:
                write_subscription_status(uid, customer_id, sub)

    except Exception as e:
        # Log and acknowledge to prevent Stripe retries
        print("Webhook handling error:", e)
        return "ok", 200

    return "ok", 200

if __name__ == "__main__":
    # For local testing
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
