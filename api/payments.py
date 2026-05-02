"""
api/payments.py — Hybrid billing system
- PhonePe payment gateway
- Subscription lifecycle management
- Coupon/discount support
- Webhook-only plan activation (never trust frontend)
- Plan expiry auto-downgrade
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
from config.admin import create_notification
from utils import json_resp, json_error, require_auth, require_admin

logger = logging.getLogger(__name__)
payments_bp = Blueprint("payments", __name__)

PLANS = {
    "pro":      {"amount": 29900, "label": "Pro Plan",      "role": "pro",      "days": 30},
    "advanced": {"amount": 79900, "label": "Advanced Plan",  "role": "advanced", "days": 30},
}

PHONEPE_HOST       = os.getenv("PHONEPE_HOST", "https://api.phonepe.com/apis/hermes")
MERCHANT_ID        = os.getenv("PHONEPE_MERCHANT_ID", "")
MERCHANT_KEY       = os.getenv("PHONEPE_API_KEY", "")
MERCHANT_KEY_INDEX = int(os.getenv("PHONEPE_KEY_INDEX", "1"))
REDIRECT_URL       = os.getenv("PHONEPE_REDIRECT_URL", "")
CALLBACK_URL       = os.getenv("PHONEPE_CALLBACK_URL", "")


def _checksum(payload_b64: str, endpoint: str) -> str:
    raw = payload_b64 + endpoint + MERCHANT_KEY
    return hashlib.sha256(raw.encode()).hexdigest() + "###" + str(MERCHANT_KEY_INDEX)


def _activate_plan(db, user_id: int, plan: str, payment_id: int):
    """Activate plan, create subscription record, update user role."""
    plan_info = PLANS[plan]
    role = plan_info["role"]
    days = plan_info["days"]

    # Cancel any existing active subscription
    with db.cursor() as cur:
        cur.execute(
            "UPDATE subscriptions SET status='cancelled', cancelled_at=NOW() "
            "WHERE user_id=%s AND status='active'",
            (user_id,)
        )

    # Create new subscription
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO subscriptions (user_id, plan, status, payment_id, start_date, end_date) "
            "VALUES (%s, %s, 'active', %s, NOW(), DATE_ADD(NOW(), INTERVAL %s DAY))",
            (user_id, plan, payment_id, days)
        )
        sub_id = cur.lastrowid

    # Link subscription to payment
    with db.cursor() as cur:
        cur.execute("UPDATE payments SET subscription_id=%s WHERE id=%s", (sub_id, payment_id))

    # Upgrade user role
    with db.cursor() as cur:
        cur.execute(
            "UPDATE users SET role=%s, plan_status='active', "
            "plan_expires_at=DATE_ADD(NOW(), INTERVAL %s DAY) WHERE id=%s",
            (role, days, user_id)
        )

    try:
        create_notification(
            user_id, "payment_success",
            "Payment Successful!",
            f"Your {plan.capitalize()} plan is now active for {days} days. Enjoy your features!",
        )
    except Exception:
        pass

    logger.info("Plan activated user_id=%s plan=%s sub_id=%s", user_id, plan, sub_id)


# ── POST /api/pay/initiate ────────────────────────────────────────────────────
@payments_bp.route("/initiate", methods=["POST"])
@require_auth
def initiate_payment(identity):
    user_id = int(identity["user_id"])
    body    = request.get_json(silent=True) or {}
    plan    = body.get("plan", "").lower()
    coupon_code = body.get("coupon", "").strip().upper()
    use_dummy = body.get("dummy", False)  # dummy mode flag

    if plan not in PLANS:
        return json_error(400, "Invalid plan. Choose 'pro' or 'advanced'.")

    plan_info = PLANS[plan]
    amount    = plan_info["amount"]
    discount  = 0
    coupon_id = None

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT email, name FROM users WHERE id=%s", (user_id,))
            user = cur.fetchone()
        if not user:
            return json_error(404, "User not found.")

        # Validate coupon
        if coupon_code:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT * FROM coupons WHERE code=%s AND is_active=1 "
                    "AND (valid_until IS NULL OR valid_until > NOW()) "
                    "AND (max_uses IS NULL OR used_count < max_uses) "
                    "AND (applicable_plan='all' OR applicable_plan=%s)",
                    (coupon_code, plan)
                )
                coupon = cur.fetchone()
            if not coupon:
                return json_error(400, "Invalid or expired coupon code.")
            if coupon["discount_type"] == "percent":
                discount = int(amount * coupon["discount_value"] / 100)
            else:
                discount = min(coupon["discount_value"] * 100, amount)
            coupon_id = coupon["id"]
            amount = max(amount - discount, 0)

        order_id = f"SC_{user_id}_{uuid.uuid4().hex[:12].upper()}"

        # Save pending payment
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO payments (user_id, plan, amount, phonepe_order_id, status, coupon_id, discount_amount) "
                "VALUES (%s,%s,%s,%s,'pending',%s,%s)",
                (user_id, plan, amount, order_id, coupon_id, discount),
            )
            payment_id = cur.lastrowid

        # ── DUMMY MODE or 100% discount — activate immediately ──
        if use_dummy or amount == 0:
            with db.cursor() as cur:
                cur.execute("UPDATE payments SET status='success' WHERE id=%s", (payment_id,))
            if coupon_id:
                with db.cursor() as cur:
                    cur.execute("UPDATE coupons SET used_count=used_count+1 WHERE id=%s", (coupon_id,))
            _activate_plan(db, user_id, plan, payment_id)
            logger.info("Dummy payment activated user_id=%s plan=%s order=%s", user_id, plan, order_id)
            return json_resp(200, {"dummy": True, "order_id": order_id, "plan": plan})

    except Exception:
        logger.exception("initiate_payment DB error user_id=%s", user_id)
        return json_error(500, "Failed to create payment record.")
    finally:
        db.close()

    # ── Real PhonePe flow ──
    if not MERCHANT_ID or not MERCHANT_KEY:
        return json_error(500, "Payment gateway not configured. Use dummy=true for testing.")

    # Build PhonePe payload
    payload = {
        "merchantId":            MERCHANT_ID,
        "merchantTransactionId": order_id,
        "merchantUserId":        f"USER_{user_id}",
        "amount":                amount,
        "redirectUrl":           f"{REDIRECT_URL}?order_id={order_id}",
        "redirectMode":          "REDIRECT",
        "callbackUrl":           CALLBACK_URL,
        "mobileNumber":          "",
        "paymentInstrument":     {"type": "PAY_PAGE"},
    }
    payload_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    checksum    = _checksum(payload_b64, "/pg/v1/pay")

    try:
        resp = requests.post(
            f"{PHONEPE_HOST}/pg/v1/pay",
            json={"request": payload_b64},
            headers={"Content-Type": "application/json", "X-VERIFY": checksum},
            timeout=15,
        )
        data = resp.json()
        url  = data.get("data", {}).get("instrumentResponse", {}).get("redirectInfo", {}).get("url")
        if data.get("success") and url:
            return json_resp(200, {"redirect_url": url, "order_id": order_id})
        logger.error("PhonePe initiate failed: %s", data)
        return json_error(502, data.get("message", "Payment gateway error."))
    except Exception:
        logger.exception("PhonePe API call failed")
        return json_error(502, "Could not reach payment gateway.")


# ── POST /api/pay/callback  (PhonePe webhook — ONLY trusted source) ───────────
@payments_bp.route("/callback", methods=["POST"])
def payment_callback():
    body         = request.get_json(silent=True) or {}
    x_verify     = request.headers.get("X-VERIFY", "")
    response_b64 = body.get("response", "")

    if not response_b64:
        return json_error(400, "Missing response.")

    # ── Verify SHA256 checksum — reject anything that doesn't match ──
    expected = hashlib.sha256((response_b64 + MERCHANT_KEY).encode()).hexdigest() + "###" + str(MERCHANT_KEY_INDEX)
    if x_verify != expected:
        logger.warning("PhonePe callback checksum MISMATCH — possible fake webhook")
        return json_error(403, "Checksum mismatch.")

    try:
        decoded = json.loads(base64.b64decode(response_b64).decode())
    except Exception:
        return json_error(400, "Invalid response payload.")

    order_id    = decoded.get("data", {}).get("merchantTransactionId", "")
    phonepe_txn = decoded.get("data", {}).get("transactionId", "")
    success     = decoded.get("success", False)
    code        = decoded.get("code", "")

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM payments WHERE phonepe_order_id=%s", (order_id,))
            payment = cur.fetchone()

        if not payment:
            logger.warning("Callback for unknown order_id=%s", order_id)
            return json_error(404, "Payment record not found.")

        # Idempotency — ignore duplicate callbacks
        if payment["status"] == "success":
            return json_resp(200, {"message": "Already processed."})

        if success and code == "PAYMENT_SUCCESS":
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE payments SET status='success', phonepe_txn_id=%s WHERE phonepe_order_id=%s",
                    (phonepe_txn, order_id),
                )
            # Increment coupon usage
            if payment.get("coupon_id"):
                with db.cursor() as cur:
                    cur.execute("UPDATE coupons SET used_count=used_count+1 WHERE id=%s", (payment["coupon_id"],))

            _activate_plan(db, payment["user_id"], payment["plan"], payment["id"])
            logger.info("Webhook success order=%s user=%s plan=%s", order_id, payment["user_id"], payment["plan"])
        else:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE payments SET status='failed', phonepe_txn_id=%s WHERE phonepe_order_id=%s",
                    (phonepe_txn, order_id),
                )
            logger.warning("Webhook payment failed order=%s code=%s", order_id, code)

        return json_resp(200, {"message": "Callback processed."})
    except Exception:
        logger.exception("payment_callback failed order=%s", order_id)
        return json_error(500, "Callback processing failed.")
    finally:
        db.close()


# ── GET /api/pay/status?order_id=xxx ─────────────────────────────────────────
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
                "SELECT status, plan, amount, discount_amount, created_at FROM payments "
                "WHERE phonepe_order_id=%s AND user_id=%s",
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


# ── GET /api/pay/history ──────────────────────────────────────────────────────
@payments_bp.route("/history", methods=["GET"])
@require_auth
def payment_history(identity):
    user_id = int(identity["user_id"])
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT p.plan, p.amount, p.discount_amount, p.status, p.phonepe_order_id, "
                "p.created_at, s.end_date, s.status as sub_status "
                "FROM payments p LEFT JOIN subscriptions s ON s.payment_id=p.id "
                "WHERE p.user_id=%s ORDER BY p.created_at DESC LIMIT 20",
                (user_id,),
            )
            payments = cur.fetchall()
        return json_resp(200, {"payments": payments})
    except Exception:
        logger.exception("payment_history failed user_id=%s", user_id)
        return json_error(500, "Failed to fetch payment history.")
    finally:
        db.close()


# ── GET /api/pay/subscription ─────────────────────────────────────────────────
@payments_bp.route("/subscription", methods=["GET"])
@require_auth
def get_subscription(identity):
    user_id = int(identity["user_id"])
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT s.*, u.role, u.plan_expires_at FROM subscriptions s "
                "JOIN users u ON u.id=s.user_id "
                "WHERE s.user_id=%s AND s.status='active' ORDER BY s.created_at DESC LIMIT 1",
                (user_id,),
            )
            sub = cur.fetchone()
        return json_resp(200, {"subscription": sub})
    except Exception:
        logger.exception("get_subscription failed user_id=%s", user_id)
        return json_error(500, "Failed to fetch subscription.")
    finally:
        db.close()


# ── POST /api/pay/validate-coupon ─────────────────────────────────────────────
@payments_bp.route("/validate-coupon", methods=["POST"])
@require_auth
def validate_coupon(identity):
    body = request.get_json(silent=True) or {}
    code = body.get("code", "").strip().upper()
    plan = body.get("plan", "").lower()
    if not code or not plan:
        return json_error(400, "code and plan required.")
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT * FROM coupons WHERE code=%s AND is_active=1 "
                "AND (valid_until IS NULL OR valid_until > NOW()) "
                "AND (max_uses IS NULL OR used_count < max_uses) "
                "AND (applicable_plan='all' OR applicable_plan=%s)",
                (code, plan)
            )
            coupon = cur.fetchone()
        if not coupon:
            return json_error(400, "Invalid or expired coupon.")
        amount = PLANS.get(plan, {}).get("amount", 0)
        if coupon["discount_type"] == "percent":
            discount = int(amount * coupon["discount_value"] / 100)
        else:
            discount = min(coupon["discount_value"] * 100, amount)
        return json_resp(200, {
            "valid": True,
            "discount_type":  coupon["discount_type"],
            "discount_value": coupon["discount_value"],
            "discount_amount": discount,
            "final_amount":   max(amount - discount, 0),
        })
    except Exception:
        logger.exception("validate_coupon failed code=%s", code)
        return json_error(500, "Failed to validate coupon.")
    finally:
        db.close()


# ── POST /api/pay/expire-plans  (called by cron job) ─────────────────────────
@payments_bp.route("/expire-plans", methods=["POST"])
def expire_plans():
    """Cron endpoint — downgrade expired plans to basic. Secured by secret key."""
    secret = request.headers.get("X-Cron-Secret", "")
    if secret != os.getenv("CRON_SECRET", ""):
        return json_error(403, "Unauthorized.")
    db = get_db()
    try:
        # Find expired active subscriptions
        with db.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM subscriptions WHERE status='active' AND end_date < NOW()"
            )
            expired = cur.fetchall()

        count = 0
        for row in expired:
            uid = row["user_id"]
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE subscriptions SET status='expired' WHERE user_id=%s AND status='active' AND end_date < NOW()",
                    (uid,)
                )
                cur.execute(
                    "UPDATE users SET role='basic', plan_status=NULL, plan_expires_at=NULL WHERE id=%s",
                    (uid,)
                )
            try:
                create_notification(uid, "plan_expired", "Plan Expired",
                    "Your subscription has expired. Upgrade to continue using premium features.")
            except Exception:
                pass
            count += 1

        logger.info("expire_plans: downgraded %d users", count)
        return json_resp(200, {"expired": count})
    except Exception:
        logger.exception("expire_plans failed")
        return json_error(500, "Failed to expire plans.")
    finally:
        db.close()


# ── Admin: GET /api/pay/admin/transactions ────────────────────────────────────
@payments_bp.route("/admin/transactions", methods=["GET"])
@require_admin
def admin_transactions(identity):
    page   = max(1, int(request.args.get("page", 1)))
    limit  = 20
    offset = (page - 1) * limit
    status = request.args.get("status", "")
    plan   = request.args.get("plan", "")

    conditions = ["1=1"]
    params = []
    if status:
        conditions.append("p.status=%s"); params.append(status)
    if plan:
        conditions.append("p.plan=%s"); params.append(plan)
    where = " AND ".join(conditions)

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                f"SELECT p.*, u.name, u.email FROM payments p "
                f"JOIN users u ON u.id=p.user_id WHERE {where} "
                f"ORDER BY p.created_at DESC LIMIT %s OFFSET %s",
                params + [limit, offset]
            )
            txns = cur.fetchall()
            cur.execute(f"SELECT COUNT(*) as total FROM payments p WHERE {where}", params)
            total = cur.fetchone()["total"]
        return json_resp(200, {"transactions": txns, "total": total, "page": page})
    except Exception:
        logger.exception("admin_transactions failed")
        return json_error(500, "Failed to fetch transactions.")
    finally:
        db.close()


# ── Admin: PUT /api/pay/admin/override ───────────────────────────────────────
@payments_bp.route("/admin/override", methods=["PUT"])
@require_admin
def admin_override(identity):
    """Manually upgrade/downgrade/extend/cancel a user's plan."""
    admin_id = int(identity["user_id"])
    body     = request.get_json(silent=True) or {}
    user_id  = body.get("user_id")
    action   = body.get("action")  # upgrade | downgrade | extend | cancel
    plan     = body.get("plan", "basic")
    days     = int(body.get("days", 30))
    note     = body.get("note", "")

    if not user_id or not action:
        return json_error(400, "user_id and action required.")

    db = get_db()
    try:
        if action == "upgrade":
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE subscriptions SET status='cancelled' WHERE user_id=%s AND status='active'",
                    (user_id,)
                )
                cur.execute(
                    "INSERT INTO subscriptions (user_id, plan, status, start_date, end_date, admin_note) "
                    "VALUES (%s,%s,'active',NOW(),DATE_ADD(NOW(),INTERVAL %s DAY),%s)",
                    (user_id, plan, days, f"Admin override by {admin_id}: {note}")
                )
            role = PLANS.get(plan, {}).get("role", plan)
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE users SET role=%s, plan_status='active', "
                    "plan_expires_at=DATE_ADD(NOW(),INTERVAL %s DAY) WHERE id=%s",
                    (role, days, user_id)
                )

        elif action == "downgrade":
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE subscriptions SET status='cancelled', cancelled_at=NOW() "
                    "WHERE user_id=%s AND status='active'", (user_id,)
                )
                cur.execute(
                    "UPDATE users SET role='basic', plan_status=NULL, plan_expires_at=NULL WHERE id=%s",
                    (user_id,)
                )

        elif action == "extend":
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE subscriptions SET end_date=DATE_ADD(end_date, INTERVAL %s DAY) "
                    "WHERE user_id=%s AND status='active'", (days, user_id)
                )
                cur.execute(
                    "UPDATE users SET plan_expires_at=DATE_ADD(plan_expires_at, INTERVAL %s DAY) "
                    "WHERE id=%s", (days, user_id)
                )

        elif action == "cancel":
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE subscriptions SET status='cancelled', cancelled_at=NOW() "
                    "WHERE user_id=%s AND status='active'", (user_id,)
                )
                cur.execute(
                    "UPDATE users SET role='basic', plan_status=NULL, plan_expires_at=NULL WHERE id=%s",
                    (user_id,)
                )
        else:
            return json_error(400, "Invalid action.")

        try:
            create_notification(user_id, "admin_plan_change", "Plan Updated",
                f"Your plan has been updated by admin. {note}")
        except Exception:
            pass

        logger.info("admin_override admin=%s user=%s action=%s plan=%s", admin_id, user_id, action, plan)
        return json_resp(200, {"message": "Plan updated successfully."})
    except Exception:
        logger.exception("admin_override failed user=%s", user_id)
        return json_error(500, "Failed to update plan.")
    finally:
        db.close()
