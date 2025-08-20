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
STRIPE_PRICE_ID   = os.environ.get("STRIPE_PRICE_ID", "")  # e.g. price_123 for the Organizer monthly plan
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# Service account JSON for Firebase Admin SDK (store the full JSON in this env var)
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")

# Optional CORS allowlist (comma-separated origins). If blank, allow all.
CORS_ALLOW = os.environ.get("CORS_ALLOW", "*")

stripe.api_key = STRIPE_SECRET_KEY

# ---------- Initialize Flask ----------
app = Flask(__name__)
if CORS_ALLOW == "*" or not CORS_ALLOW:
    CORS(app)
else:
    CORS(app, origins=[o.strip() for o in CORS_ALLOW.split(",")])

# ---------- Initialize Firestore ----------
if not firebase_admin._apps:
    if FIREBASE_SERVICE_ACCOUNT_JSON.strip().startswith("{"):
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT_JSON))
    elif os.path.isfile(FIREBASE_SERVICE_ACCOUNT_JSON):
        cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_JSON)
    else:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON is not set to a JSON string or filepath")
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

def write_subscription_status(uid: str, customer_id: str, subscription_obj: dict):
    """Write subscription fields to Firestore: organizer_subs/{uid}"""
    status = subscription_obj.get("status")
    current_period_end = subscription_obj.get("current_period_end")  # unix ts (sec)
    current_period_end_ms = int(current_period_end) * 1000 if current_period_end else None
    doc_ref = db.collection("organizer_subs").document(uid)
    payload = {
        "status": status,
        "current_period_end": firestore.SERVER_TIMESTAMP if current_period_end_ms is None else current_period_end_ms,
        "stripe_customer_id": customer_id,
        "stripe_subscription_id": subscription_obj.get("id"),
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    doc_ref.set(payload, merge=True)

# ---------- Routes ----------
@app.route("/healthz")
def healthz():
    return "ok", 200

@app.route("/create-organizer-subscription", methods=["POST"])
def create_subscription():
    """Create a Stripe Checkout Session (mode=subscription) for the organizer plan."""
    data = request.get_json(force=True, silent=True) or {}
    uid = data.get("uid")
    success_url = data.get("success_url")
    cancel_url = data.get("cancel_url")

    if not (uid and success_url and cancel_url and STRIPE_PRICE_ID):
        return jsonify({"error": "Missing uid/success_url/cancel_url or STRIPE_PRICE_ID"}), 400

    try:
        customer_id = get_or_create_customer_for_uid(uid)
        session = stripe.checkout.Session.create(
            mode="subscription",
            payment_method_types=["card"],
            customer=customer_id,
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
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
            # Session completed for a subscription
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
        # Log and acknowledge to prevent retries
        print("Webhook handling error:", e)
        return "ok", 200

    return "ok", 200

if __name__ == "__main__":
    # For local testing
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
