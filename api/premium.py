import logging

from flask import Blueprint, request
from config.db import get_db
from config.admin import is_premium_user, get_user_feature_limits
from utils import json_resp, json_error, require_auth

logger = logging.getLogger(__name__)
premium_bp = Blueprint("premium", __name__)


# POST /api/premium/request — request premium access
@premium_bp.route("/request", methods=["POST"])
@require_auth
def request_premium(identity):
    user_id = int(identity["user_id"])
    body    = request.get_json(silent=True) or {}
    message = body.get("message", "").strip()

    if is_premium_user(identity):
        return json_error(400, "You already have premium access.")

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id FROM premium_requests WHERE user_id = %s AND status = 'pending'",
                (user_id,),
            )
            if cur.fetchone():
                return json_error(400, "You already have a pending request.")

            cur.execute(
                "INSERT INTO premium_requests (user_id, message, status) VALUES (%s, %s, 'pending')",
                (user_id, message),
            )
            cur.execute(
                "UPDATE users SET plan_status = 'pending', premium_requested_at = NOW() WHERE id = %s",
                (user_id,),
            )
        return json_resp(201, {"message": "Premium request submitted successfully."})
    except Exception:
        logger.exception("request_premium failed user_id=%s", user_id)
        return json_error(500, "Failed to submit request.")
    finally:
        db.close()


# GET /api/premium/status
@premium_bp.route("/status", methods=["GET"])
@require_auth
def premium_status(identity):
    user_id = int(identity["user_id"])
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                """SELECT pr.*, admin.name as processed_by_name
                   FROM premium_requests pr
                   LEFT JOIN users admin ON pr.processed_by = admin.id
                   WHERE pr.user_id = %s
                   ORDER BY pr.requested_at DESC
                   LIMIT 1""",
                (user_id,),
            )
            req = cur.fetchone()
        return json_resp(200, {"request": req, "is_premium": is_premium_user(identity)})
    except Exception:
        logger.exception("premium_status failed user_id=%s", user_id)
        return json_error(500, "Failed to fetch status.")
    finally:
        db.close()


# GET /api/premium/features
@premium_bp.route("/features", methods=["GET"])
@require_auth
def premium_features(identity):
    db = get_db()
    try:
        features = get_user_feature_limits(identity)
        plan = "premium" if is_premium_user(identity) else "free"
        return json_resp(200, {
            "features":   features,
            "is_premium": is_premium_user(identity),
            "plan":       plan,
        })
    except Exception:
        logger.exception("premium_features failed user_id=%s", identity.get("user_id"))
        return json_error(500, "Failed to fetch features.")
    finally:
        db.close()


# GET /api/premium/notifications
@premium_bp.route("/notifications", methods=["GET"])
@require_auth
def get_notifications(identity):
    user_id = int(identity["user_id"])
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT * FROM notifications WHERE user_id = %s ORDER BY created_at DESC LIMIT 20",
                (user_id,),
            )
            notifications = cur.fetchall()
            cur.execute(
                "SELECT COUNT(*) as count FROM notifications WHERE user_id = %s AND is_read = 0",
                (user_id,),
            )
            unread = int(cur.fetchone()["count"])
        return json_resp(200, {"notifications": notifications, "unread_count": unread})
    except Exception:
        logger.exception("get_notifications failed user_id=%s", user_id)
        return json_error(500, "Failed to fetch notifications.")
    finally:
        db.close()


# PUT /api/premium/notification?id=<id>
@premium_bp.route("/notification", methods=["PUT"])
@require_auth
def mark_notification_read(identity):
    user_id  = int(identity["user_id"])
    notif_id = int(request.args.get("id", 0))
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE notifications SET is_read = 1 WHERE id = %s AND user_id = %s",
                (notif_id, user_id),
            )
        return json_resp(200, {"message": "Notification marked as read."})
    except Exception:
        logger.exception("mark_notification_read failed notif_id=%s user_id=%s", notif_id, user_id)
        return json_error(500, "Failed to update notification.")
    finally:
        db.close()
