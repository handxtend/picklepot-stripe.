# app.py — PiCo Pickle Pot backend (FIXED)
# Removes `customer_creation` from subscription Checkout Sessions.
import os, json
from typing import Optional, Dict, Any
from flask import Flask, request, jsonify
from flask_cors import CORS
import stripe
import firebase_admin
from firebase_admin import credentials, firestore

STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY','')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET','')

IND_M = os.environ.get('STRIPE_PRICE_ID_INDIVIDUAL_MONTHLY', 'price_1Rwq6nFFPAbZxH9HkmDxBJ73')
IND_Y = os.environ.get('STRIPE_PRICE_ID_INDIVIDUAL_YEARLY',  'price_1RwptxFFPAbZxH9HdPLdYIZR')
CLB_M = os.environ.get('STRIPE_PRICE_ID_CLUB_MONTHLY',       'price_1Rwq1JFFPAbZxH9HmpYCSJYv')
CLB_Y = os.environ.get('STRIPE_PRICE_ID_CLUB_YEARLY',        'price_1RwpyUFFPAbZxH9H2N1Ykd4U')
LEG_M = os.environ.get('STRIPE_PRICE_ID','')
LEG_Y = os.environ.get('STRIPE_PRICE_ID_YEARLY','')

PLAN_CONFIG: Dict[str, Dict[str, Any]] = {
    IND_M: {'plan':'individual','interval':'month','pots_per_month':2,'max_users_per_event':12},
    IND_Y: {'plan':'individual','interval':'year','pots_per_month':2,'max_users_per_event':12},
    CLB_M: {'plan':'club','interval':'month','pots_per_month':10,'max_users_per_event':64},
    CLB_Y: {'plan':'club','interval':'year','pots_per_month':10,'max_users_per_event':64},
}
ALLOWED_PRICE_IDS = [p for p in {IND_M,IND_Y,CLB_M,CLB_Y,LEG_M,LEG_Y} if p]

CORS_ALLOW = os.environ.get('CORS_ALLOW') or os.environ.get('CORS_ORIGINS') or '*'
FIREBASE_SERVICE_ACCOUNT_JSON = (os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON') 
    or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS_JSON') 
    or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS') or '')

stripe.api_key = STRIPE_SECRET_KEY

app = Flask(__name__)
if CORS_ALLOW == '*' or not CORS_ALLOW:
    CORS(app)
else:
    CORS(app, resources={r'/*': {'origins':[o.strip() for o in CORS_ALLOW.split(',') if o.strip()]}})

@app.after_request
def add_cors_headers(resp):
    resp.headers.setdefault('Access-Control-Allow-Headers','Content-Type, Authorization')
    resp.headers.setdefault('Access-Control-Allow-Methods','GET, POST, OPTIONS')
    return resp

if not firebase_admin._apps:
    if FIREBASE_SERVICE_ACCOUNT_JSON.strip().startswith('{'):
        cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT_JSON))
    elif os.path.isfile(FIREBASE_SERVICE_ACCOUNT_JSON):
        cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_JSON)
    else:
        raise RuntimeError('Missing FIREBASE_SERVICE_ACCOUNT_JSON')
    firebase_admin.initialize_app(cred)
db = firestore.client()

def _extract_subscription_bits(sub: Dict[str, Any]) -> Dict[str, Any]:
    items = (sub.get('items',{}) or {}).get('data') or []
    first = items[0] if items else {}
    price = (first or {}).get('price') or {}
    recurring = price.get('recurring') or {}
    price_id = price.get('id')
    amount_cents = price.get('unit_amount')
    currency = price.get('currency')
    interval = recurring.get('interval')
    plan_bits = PLAN_CONFIG.get(price_id, {})
    return {
        'price_id': price_id,
        'amount_cents': amount_cents,
        'currency': currency,
        'interval': interval or plan_bits.get('interval'),
        'plan': plan_bits.get('plan'),
        'pots_per_month': plan_bits.get('pots_per_month'),
        'max_users_per_event': plan_bits.get('max_users_per_event'),
    }

def _write_sub_status_by_email(email_lc: str, customer_id: str, sub: Dict[str, Any]):
    bits = _extract_subscription_bits(sub)
    status = sub.get('status')
    period_end = sub.get('current_period_end')
    period_end_ms = int(period_end) * 1000 if period_end else None
    doc = {
        'email': email_lc,
        'status': status,
        'stripe_customer_id': customer_id,
        'stripe_subscription_id': sub.get('id'),
        'current_period_end': period_end_ms,
        'updated_at': firestore.SERVER_TIMESTAMP,
        **bits,
    }
    db.collection('organizer_subs_emails').document(email_lc).set(doc, merge=True)

def _write_sub_status_by_uid(uid: str, customer_id: str, sub: Dict[str, Any]):
    bits = _extract_subscription_bits(sub)
    status = sub.get('status')
    period_end = sub.get('current_period_end')
    period_end_ms = int(period_end) * 1000 if period_end else None
    doc = {
        'uid': uid,
        'status': status,
        'stripe_customer_id': customer_id,
        'stripe_subscription_id': sub.get('id'),
        'current_period_end': period_end_ms,
        'updated_at': firestore.SERVER_TIMESTAMP,
        **bits,
    }
    db.collection('organizer_subs').document(uid).set(doc, merge=True)

@app.get('/healthz')
def healthz():
    return 'ok', 200

@app.post('/create-organizer-subscription')
def create_organizer_subscription():
    data = request.get_json(force=True, silent=True) or {}
    price_id = (data.get('price_id') or '').strip()
    success_url = data.get('success_url')
    cancel_url = data.get('cancel_url')
    email = (data.get('email') or '').strip().lower() or None
    if not (price_id and success_url and cancel_url):
        return jsonify({'error':'Missing price_id/success_url/cancel_url'}), 400
    if price_id not in ALLOWED_PRICE_IDS:
        return jsonify({'error':'Invalid price_id'}), 400
    try:
        # NOTE: Do NOT pass customer_creation for subscriptions (causes the error you saw)
        session = stripe.checkout.Session.create(
            mode='subscription',
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity':1}],
            success_url=success_url,
            cancel_url=cancel_url,
            allow_promotion_codes=True,
            customer_email=email,           # prefill Checkout; Stripe will create a Customer
            metadata={'email_lc': email} if email else None,
        )
        return jsonify({'url': session.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.post('/create-checkout-session')
def create_checkout_session():
    data = request.get_json(force=True, silent=True) or {}
    pot_id = data.get('pot_id')
    entry_id = data.get('entry_id')
    amount_cents = int(data.get('amount_cents') or 0)
    player_name = data.get('player_name') or 'Player'
    player_email = data.get('player_email') or None
    success_url = data.get('success_url')
    cancel_url = data.get('cancel_url')
    if not (pot_id and entry_id and success_url and cancel_url):
        return jsonify({'error':'Missing pot_id/entry_id/success_url/cancel_url'}), 400
    if amount_cents < 50:
        return jsonify({'error':'Minimum amount is 50 cents'}), 400
    try:
        session = stripe.checkout.Session.create(
            mode='payment',
            payment_method_types=['card'],
            customer_email=player_email,
            line_items=[{
                'price_data': {
                    'currency':'usd',
                    'unit_amount': amount_cents,
                    'product_data': {'name': f'Pot Join — {player_name}'}
                },
                'quantity': 1
            }],
            success_url=success_url + ('?join=success' if '?' not in success_url else '&join=success'),
            cancel_url=cancel_url,
            metadata={'pot_id': pot_id, 'entry_id': entry_id, 'player_email': player_email or '', 'player_name': player_name or ''}
        )
        return jsonify({'url': session.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.post('/activate-subscription-for-uid')
def activate_subscription_for_uid():
    data = request.get_json(force=True, silent=True) or {}
    uid = data.get('uid'); email = data.get('email')
    if not (uid and email): return jsonify({'error':'Missing uid/email'}), 400
    email_lc = email.strip().lower()
    doc = db.collection('organizer_subs_emails').document(email_lc).get()
    if not doc.exists: return jsonify({'error':'No subscription found for that email'}), 404
    info = doc.to_dict() or {}
    status = info.get('status')
    if status not in ('active','trialing','past_due'):
        return jsonify({'error': f'Subscription not active (status={status})'}), 400
    payload = {
        'uid': uid, 'email': email_lc, 'status': status,
        'price_id': info.get('price_id'), 'interval': info.get('interval'),
        'amount_cents': info.get('amount_cents'), 'currency': info.get('currency'),
        'pots_per_month': info.get('pots_per_month'), 'max_users_per_event': info.get('max_users_per_event'),
        'stripe_customer_id': info.get('stripe_customer_id'), 'stripe_subscription_id': info.get('stripe_subscription_id'),
        'current_period_end': info.get('current_period_end'),
        'updated_at': firestore.SERVER_TIMESTAMP,
    }
    db.collection('organizer_subs').document(uid).set(payload, merge=True)
    return jsonify({'ok': True, 'attached_to_uid': uid})

@app.post('/stripe-webhook')
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature','')
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        return 'Invalid signature', 400
    except Exception:
        return 'Invalid payload', 400

    etype = event.get('type')
    obj = event.get('data',{}).get('object',{})
    try:
        if etype == 'checkout.session.completed':
            mode = obj.get('mode')
            if mode == 'subscription':
                customer_id = obj.get('customer')
                subscription_id = obj.get('subscription')
                email = (obj.get('customer_details') or {}).get('email') or obj.get('customer_email')
                email_lc = email.lower() if email else None
                if subscription_id:
                    sub = stripe.Subscription.retrieve(subscription_id)
                    if email_lc:
                        _write_sub_status_by_email(email_lc, customer_id, sub)
            elif mode == 'payment':
                pot_id = (obj.get('metadata') or {}).get('pot_id')
                entry_id = (obj.get('metadata') or {}).get('entry_id')
                amount_total = obj.get('amount_total')
                payment_intent = obj.get('payment_intent')
                if pot_id and entry_id:
                    db.collection('pots').document(pot_id).collection('entries').document(entry_id).set({
                        'paid': True,
                        'paid_amount': amount_total,
                        'stripe_session_id': obj.get('id'),
                        'stripe_payment_intent_id': payment_intent,
                        'paid_at': firestore.SERVER_TIMESTAMP,
                    }, merge=True)
        elif etype in ('invoice.payment_succeeded','customer.subscription.updated','customer.subscription.deleted','customer.subscription.paused'):
            sub = None
            if etype.startswith('customer.subscription.'):
                sub = obj; customer_id = sub.get('customer')
            else:
                subscription_id = obj.get('subscription'); customer_id = obj.get('customer')
                if subscription_id: sub = stripe.Subscription.retrieve(subscription_id)
            if sub:
                email_lc = None
                try:
                    cust = stripe.Customer.retrieve(customer_id) if customer_id else None
                    e = cust.get('email') if cust else None
                    if e: email_lc = e.lower()
                except Exception: pass
                if email_lc:
                    _write_sub_status_by_email(email_lc, customer_id, sub)
    except Exception as e:
        print('webhook handler error:', e)
    return 'ok', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT',5000)))
