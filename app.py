# app.py
import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import stripe

# --- Config from environment ---
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
PRICE_ID = os.environ["STRIPE_ORG_PRICE_ID"]

# Comma-separated origins, no trailing slashes
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "").split(",")
    if o.strip()
]

app = Flask(__name__)
CORS(
    app,
    origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS else "*",
    methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

@app.get("/")
def health():
    return "OK", 200

# Helper to honor preflight quickly
def _maybe_preflight():
    if request.method == "OPTIONS":
        # Flask-CORS will add ACA* headers; 204 means “preflight OK”
        return ("", 204)

# --- Primary route used by the frontend ---
@app.route("/create-organizer-subscription", methods=["POST", "OPTIONS"])
def create_organizer_subscription():
    pf = _maybe_preflight()
    if pf:
        return pf

    data = request.get_json(silent=True) or {}
    # default return url to same origin if not provided
    return_url = data.get("returnUrl") or "https://pickle-pot.web.app?sub=success"

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": PRICE_ID, "quantity": 1}],
            success_url=return_url,
            cancel_url=return_url.replace("success", "cancel"),
        )
        return jsonify({"url": session.url}), 200
    except Exception as e:
        app.logger.exception("Stripe session error")
        return jsonify({"error": str(e)}), 500

# --- Back-compat alias (if the frontend ever calls the older name) ---
@app.route("/create-checkout-session", methods=["POST", "OPTIONS"])
def create_checkout_session_alias():
    return create_organizer_subscription()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
