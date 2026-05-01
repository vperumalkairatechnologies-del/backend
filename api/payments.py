"""
api/payments.py — PhonePe payment integration
Plans: pro (₹299/mo), advanced (₹799/mo)
"""

import hashlib
import base64
import json
import logging
import os
import uuid

import requests
from flask import Blueprint, request
from config.db import get_db
from utils import json_resp, json_error, require_auth

logger = logging.getLogger(__name__)
payments_bp = Blueprint("payments", __name__)

PLANS = {
    "pro":      {"amount": 29900,  "label": "Pro Plan",      "role": "pro"},
    "advanced": {"amount": 79900,  "label": "Advanced Plan",  "role": "advanced"},
}

PHONEPE_HOST        = os.getenv("PHONEPE_HOST", "https://api.phonepe.com/apis/hermes")
MERCHANT_ID         = os.getenv("PHONEPE_MERCHANT_ID", "")
MERCHANT_KEY        = os.getenv("PHONEPE_API_KEY", "")
MERCHANT_KEY_INDEX  = int(os.getenv("PHONEPE_KEY_INDEX", "1"))
REDIRECT_URL        = os.getenv("PHONEPE_REDIRECT_URL", "")   # e.g. https://yourapp.com/payment/success
CALLBACK_URL        = os.getenv("PHONEPE_CALLBACK_URL", "")   # e.g. https://yourbackend.com/api/pay/callback


def _sha256_checksum(payload_b64: str, endpoint: str) -> str:
    raw = payload_b64 + endpoint + MERCHANT_KEY
    return hashlib.sha256(raw.encode()).hexdigest() + "###" + str(MERCHANT_KEY_INDEX)


# POST /api/pay/initiate
@payments_bp.route("/initiate", methods=["POST"])
@require_auth
def initiate_payment(identity):
    user_id = int(identity["user_id"])
    body    = request.get_json(silent=True) or {}
    plan    = body.get("plan", "").lower()

    if plan not in PLANS:
        return json_error(400, "Invalid plan. Choose 'pro' or 'advanced'.")

    if not MERCHANT_ID or not MERCHANT_KEY:
        return json_error(500, "Payment gateway not configured.")

    plan_info  = PLANS[plan]
    order_id   = f"SC_{user_id}_{uuid.uuid4().hex[:12].upper()}"

    # Fetch user email for PhonePe
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT email, name FROM users WHERE id = %s", (user_id,))
            user = cur.fetchone()
        if not user:
            return json_error(404, "User not found.")

        # Save pending payment record
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO payments (user_id, plan, amount, phonepe_order_id, status) VALUES (%s,%s,%s,%s,'pending')",
                (user_id, plan, plan_info["amount"], order_id),
            )
    except Exception:
        logger.exception("initiate_payment DB error user_id=%s", user_id)
        return json_error(500, "Failed to create payment record.")
    finally:
        db.close()

    # Build PhonePe payload
    payload = {
        "merchantId":          MERCHANT_ID,
        "merchantTransactionId": order_id,
        "merchantUserId":      f"USER_{user_id}",
        "amount":              plan_info["amount"],
        "redirectUrl":         f"{REDIRECT_URL}?order_id={order_id}",
        "redirectMode":        "REDIRECT",
        "callbackUrl":         CALLBACK_URL,
        "mobileNumber":        "",
        "paymentInstrument":   {"type": "PAY_PAGE"},
    }

    payload_b64  = base64.b64encode(json.dumps(payload).encode()).decode()
    checksum     = _sha256_checksum(payload_b64, "/pg/v1/pay")
    headers      = {
        "Content-Type":  "application/json",
        "X-VERIFY":      checksum,
    }

    try:
        resp = requests.post(
            f"{PHONEPE_HOST}/pg/v1/pay",
            json={"request": payload_b64},
            headers=headers,
            timeout=15,
        )
        data = resp.json()
        if data.get("success") and data.get("data", {}).get("instrumentResponse", {}).get("redirectInfo", {}).get("url"):
            redirect_url = data["data"]["instrumentResponse"]["redirectInfo"]["url"]
            return json_resp(200, {"redirect_url": redirect_url, "order_id": order_id})
        else:
            logger.error("PhonePe initiate failed: %s", data)
            return json_error(502, data.get("message", "Payment gateway error."))
    except Exception:
        logger.exception("PhonePe API call failed")
        return json_error(502, "Could not reach payment gateway.")


# POST /api/pay/callback  (PhonePe webhook)
@payments_bp.route("/callback", methods=["POST"])
def payment_callback():
    body         = request.get_json(silent=True) or {}
    x_verify     = request.headers.get("X-VERIFY", "")
    response_b64 = body.get("response", "")

    if not response_b64:
        return json_error(400, "Missing response.")

    # Verify checksum
    expected = hashlib.sha256((response_b64 + MERCHANT_KEY).encode()).hexdigest() + "###" + str(MERCHANT_KEY_INDEX)
    if x_verify != expected:
        logger.warning("PhonePe callback checksum mismatch")
        return json_error(403, "Checksum mismatch.")

    try:
        decoded = json.loads(base64.b64decode(response_b64).decode())
    except Exception:
        return json_error(400, "Invalid response payload.")

    txn_id   = decoded.get("data", {}).get("merchantTransactionId", "")
    phonepe_txn = decoded.get("data", {}).get("transactionId", "")
    success  = decoded.get("success", False)
    code     = decoded.get("code", "")

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM payments WHERE phonepe_order_id = %s", (txn_id,))
            payment = cur.fetchone()

        if not payment:
            return json_error(404, "Payment record not found.")

        if success and code == "PAYMENT_SUCCESS":
            # Update payment record
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE payments SET status='success', phonepe_txn_id=%s WHERE phonepe_order_id=%s",
                    (phonepe_txn, txn_id),
                )
            # Upgrade user plan
            plan      = payment["plan"]
            role      = PLANS[plan]["role"]
            user_id   = payment["user_id"]
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE users SET role=%s, plan_status='active', plan_expires_at=DATE_ADD(NOW(), INTERVAL 30 DAY) WHERE id=%s",
                    (role, user_id),
                )
            # Create notification
            try:
                from config.admin import create_notification
                create_notification(
                    user_id, "payment_success",
                    "Payment Successful!",
                    f"Your {plan.capitalize()} plan is now active. Enjoy your features!",
                )
            except Exception:
                pass
            logger.info("Payment success user_id=%s plan=%s order=%s", user_id, plan, txn_id)
        else:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE payments SET status='failed', phonepe_txn_id=%s WHERE phonepe_order_id=%s",
                    (phonepe_txn, txn_id),
                )
            logger.warning("Payment failed order=%s code=%s", txn_id, code)

        return json_resp(200, {"message": "Callback processed."})
    except Exception:
        logger.exception("payment_callback failed txn=%s", txn_id)
        return json_error(500, "Callback processing failed.")
    finally:
        db.close()


# GET /api/pay/status?order_id=xxx  (frontend polls after redirect)
@payments_bp.route("/status", methods=["GET"])
@require_auth
def payment_status(identity):
    user_id  = int(identity["user_id"])
    order_id = request.args.get("order_id", "").strip()
    if not order_id:
        return json_error(400, "order_id required.")

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT status, plan, amount, created_at FROM payments WHERE phonepe_order_id=%s AND user_id=%s",
                (order_id, user_id),
            )
            payment = cur.fetchone()
        if not payment:
            return json_error(404, "Payment not found.")
        return json_resp(200, {"payment": payment})
    except Exception:
        logger.exception("payment_status failed order=%s", order_id)
        return json_error(500, "Failed to fetch payment status.")
    finally:
        db.close()


# GET /api/pay/history
@payments_bp.route("/history", methods=["GET"])
@require_auth
def payment_history(identity):
    user_id = int(identity["user_id"])
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT plan, amount, status, phonepe_order_id, created_at FROM payments WHERE user_id=%s ORDER BY created_at DESC LIMIT 20",
                (user_id,),
            )
            payments = cur.fetchall()
        return json_resp(200, {"payments": payments})
    except Exception:
        logger.exception("payment_history failed user_id=%s", user_id)
        return json_error(500, "Failed to fetch payment history.")
    finally:
        db.close()
