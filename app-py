import os, json
from flask import Flask, request, jsonify, abort
from flask_cors import CORS
import stripe

# ---------- Stripe ----------
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")  # sk_test_... (use test key first)
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")  # whsec_...

# ---------- Firestore (Admin) ----------
from google.cloud import firestore
from google.oauth2 import service_account

project_id = os.environ.get("FIRESTORE_PROJECT_ID")

sa_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if not sa_json:
    raise RuntimeError("Missing GOOGLE_APPLICATION_CREDENTIALS_JSON env var")
creds = service_account.Credentials.from_service_account_info(json.loads(sa_json))
db = firestore.Client(project=project_id, credentials=creds)

app = Flask(__name__)
# Allow your static site to call us (you can restrict origins later)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.get("/")
def health():
    return "ok", 200

@app.post("/create-checkout-session")
def create_checkout_session():
    """
    Request JSON:
      {
        "pot_id": "abc123",
        "entry_id": "xyz789",
        "amount_cents": 1000,             # e.g., $10.00
        "player_name": "Jane Doe",
        "player_email": "jane@example.com",
        "success_url": "https://yourapp/success",
        "cancel_url": "https://yourapp/cancel"
      }
    """
    data = request.get_json(force=True, silent=True) or {}
    required = ["pot_id","entry_id","amount_cents","success_url","cancel_url"]
    missing = [k for k in required if k not in data]
    if missing:
        return jsonify({"error": f"Missing: {', '.join(missing)}"}), 400

    amount = int(data["amount_cents"])
    if amount < 50:
        return jsonify({"error":"Minimum is 50¢"}), 400

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card","link"],   # Apple/Google Pay supported automatically
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": f"Pickle Pot Entry ({data.get('player_name','Player')})",
                        "metadata": {
                            "pot_id": data["pot_id"],
                            "entry_id": data["entry_id"]
                        }
                    },
                    "unit_amount": amount
                },
                "quantity": 1
            }],
            customer_email=data.get("player_email"),
            success_url=data["success_url"] + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=data["cancel_url"],
            client_reference_id=f"{data['pot_id']}::{data['entry_id']}",
            metadata={ "pot_id": data["pot_id"], "entry_id": data["entry_id"] }
        )
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.post("/webhook")
def webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except Exception as e:
        return abort(400, str(e))

    etype = event.get("type", "")
    # Check both sync and async success
    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        session = event["data"]["object"]
        meta = session.get("metadata") or {}
        pot_id = meta.get("pot_id")
        entry_id = meta.get("entry_id")
        amount_total = session.get("amount_total")

        if pot_id and entry_id:
            try:
                db.collection("pots").document(pot_id)\
                  .collection("entries").document(entry_id)\
                  .set({
                      "paid": True,
                      "paid_amount": amount_total,
                      "paid_at": firestore.SERVER_TIMESTAMP
                  }, merge=True)
            except Exception as e:
                # Log to server output; Stripe will retry on 5xx only, so we return 200 here.
                print("Firestore update failed:", e)

    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
