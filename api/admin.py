import json
import logging

from flask import Blueprint, request
from config.db import get_db
from config.admin import log_admin_action, create_notification, is_premium_user
from utils import json_resp, json_error, require_admin

logger = logging.getLogger(__name__)
admin_bp = Blueprint("admin", __name__)

# All routes use @require_admin which:
#   1. Verifies the JWT
#   2. Re-reads the role from DB on EVERY request
#      → revoked admins are blocked immediately, not after 7 days


# GET /api/admin — dashboard stats
@admin_bp.route("", methods=["GET"])
@require_admin
def dashboard(identity):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) as total FROM users")
            total_users = int(cur.fetchone()["total"])

            cur.execute("SELECT role, COUNT(*) as count FROM users GROUP BY role")
            by_role = {r["role"]: int(r["count"]) for r in cur.fetchall()}

            cur.execute("SELECT COUNT(*) as total FROM cards")
            total_cards = int(cur.fetchone()["total"])

            pending_requests = 0
            try:
                cur.execute("SELECT COUNT(*) as total FROM premium_requests WHERE status = 'pending'")
                pending_requests = int(cur.fetchone()["total"])
            except Exception:
                logger.warning("premium_requests table not found")

            recent_users = 0
            try:
                cur.execute(
                    "SELECT COUNT(*) as count FROM users WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)"
                )
                recent_users = int(cur.fetchone()["count"])
            except Exception:
                logger.warning("Could not fetch recent_users")

            recent_activity = []
            try:
                cur.execute(
                    """SELECT al.*, u.name as admin_name, tu.name as target_name
                       FROM admin_logs al
                       LEFT JOIN users u  ON al.admin_id = u.id
                       LEFT JOIN users tu ON al.target_user_id = tu.id
                       ORDER BY al.created_at DESC LIMIT 10"""
                )
                recent_activity = cur.fetchall()
            except Exception:
                logger.warning("Could not fetch recent_activity")

        return json_resp(200, {
            "stats": {
                "total_users":      total_users,
                "free_users":       int(by_role.get("free", by_role.get("user", 0))),
                "premium_users":    int(by_role.get("premium", 0)),
                "admin_users":      int(by_role.get("admin", 0)),
                "total_cards":      total_cards,
                "pending_requests": pending_requests,
                "recent_users":     recent_users,
            },
            "recent_activity": recent_activity,
        })
    except Exception:
        logger.exception("dashboard stats failed")
        return json_error(500, "Failed to fetch dashboard stats.")
    finally:
        db.close()


# GET /api/admin/users
@admin_bp.route("/users", methods=["GET"])
@require_admin
def list_users(identity):
    page   = max(1, int(request.args.get("page", 1)))
    limit  = 20
    offset = (page - 1) * limit
    search = request.args.get("search", "").strip()
    role   = request.args.get("role",   "").strip()
    plan   = request.args.get("plan",   "").strip()

    conditions = ["is_active = 1"]
    params: list = []
    if search:
        conditions.append("(name LIKE %s OR email LIKE %s OR slug LIKE %s)")
        term = f"%{search}%"
        params += [term, term, term]
    if role:
        conditions.append("role = %s")
        params.append(role)
    if plan:
        conditions.append("plan_status = %s")
        params.append(plan)

    where = " AND ".join(conditions)

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) as total FROM users WHERE {where}", params)
            total = int(cur.fetchone()["total"])

            cur.execute(
                f"""SELECT id, name, email, slug, role, plan_status,
                           premium_requested_at, premium_approved_at, created_at,
                           COALESCE(max_cards, CASE WHEN role='admin' THEN 50 WHEN role='premium' THEN 10 ELSE 1 END) as max_cards
                    FROM users WHERE {where}
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s""",
                params + [limit, offset],
            )
            users = cur.fetchall()

        return json_resp(200, {
            "users": users,
            "pagination": {
                "page":  page,
                "limit": limit,
                "total": total,
                "pages": -(-total // limit),
            },
        })
    except Exception:
        logger.exception("list_users failed")
        return json_error(500, "Failed to fetch users.")
    finally:
        db.close()


# GET /api/admin/requests?status=pending
@admin_bp.route("/requests", methods=["GET"])
@require_admin
def list_requests(identity):
    status = request.args.get("status", "pending").strip()
    db = get_db()
    try:
        with db.cursor() as cur:
            if status == 'all':
                cur.execute(
                    """SELECT pr.*, u.name, u.email, u.slug,
                              admin.name as processed_by_name
                       FROM premium_requests pr
                       JOIN users u ON pr.user_id = u.id
                       LEFT JOIN users admin ON pr.processed_by = admin.id
                       ORDER BY pr.requested_at DESC"""
                )
            else:
                cur.execute(
                    """SELECT pr.*, u.name, u.email, u.slug,
                              admin.name as processed_by_name
                       FROM premium_requests pr
                       JOIN users u ON pr.user_id = u.id
                       LEFT JOIN users admin ON pr.processed_by = admin.id
                       WHERE pr.status = %s
                       ORDER BY pr.requested_at DESC""",
                    (status,),
                )
            requests_list = cur.fetchall()
        return json_resp(200, {"requests": requests_list})
    except Exception:
        logger.exception("list_requests failed status=%s", status)
        return json_error(500, "Failed to fetch requests.")
    finally:
        db.close()


# POST /api/admin/requests/<id>/<action>
@admin_bp.route("/requests/<int:request_id>/<action>", methods=["POST"])
@require_admin
def process_request(identity, request_id: int, action: str):
    if action not in ("approve", "reject"):
        return json_error(400, "Action must be 'approve' or 'reject'.")

    admin_id   = int(identity["user_id"])
    body       = request.get_json(silent=True) or {}
    admin_note = body.get("admin_note", "").strip()
    new_status = "approved" if action == "approve" else "rejected"

    db = get_db()
    db.autocommit(False)
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT * FROM premium_requests WHERE id = %s AND status = 'pending'",
                (request_id,),
            )
            req = cur.fetchone()
            if not req:
                db.rollback()
                return json_error(404, "Request not found or already processed.")

            user_id = int(req["user_id"])

            cur.execute(
                "UPDATE premium_requests SET status=%s, processed_at=NOW(), "
                "processed_by=%s, admin_note=%s WHERE id=%s",
                (new_status, admin_id, admin_note, request_id),
            )

            if action == "approve":
                cur.execute(
                    "UPDATE users SET role='premium', plan_status='active', "
                    "premium_approved_at=NOW(), approved_by=%s WHERE id=%s",
                    (admin_id, user_id),
                )
                try:
                    create_notification(
                        user_id, "premium_approved",
                        "🎉 Premium Access Approved!",
                        "Congratulations! Your premium access request has been approved.",
                    )
                except Exception:
                    logger.exception("Notification failed for user_id=%s", user_id)
            else:
                cur.execute("UPDATE users SET plan_status=NULL WHERE id=%s", (user_id,))
                try:
                    create_notification(
                        user_id, "premium_rejected",
                        "Premium Request Update",
                        "Your premium request was reviewed. "
                        + (admin_note or "Please contact support for more information."),
                    )
                except Exception:
                    logger.exception("Notification failed for user_id=%s", user_id)

            try:
                log_admin_action(admin_id, f"premium_request_{action}", user_id, admin_note)
            except Exception:
                logger.exception("Admin log failed")

        db.commit()
        return json_resp(200, {"message": "Request processed successfully."})
    except Exception:
        db.rollback()
        logger.exception("process_request failed request_id=%s action=%s", request_id, action)
        return json_error(500, "Failed to process request.")
    finally:
        db.autocommit(True)
        db.close()


# PUT /api/admin/user?id=<id>
@admin_bp.route("/user", methods=["PUT"])
@require_admin
def update_user(identity):
    admin_id = int(identity["user_id"])
    user_id  = int(request.args.get("id", 0))
    if not user_id:
        return json_error(400, "User ID required.")

    body    = request.get_json(silent=True) or {}
    updates: list = []
    params:  list = []

    if "role" in body and body["role"] in ("free", "premium", "admin"):
        updates.append("role = %s")
        params.append(body["role"])
    if "plan_status" in body and body["plan_status"] in (None, "free", "active", "pending"):
        updates.append("plan_status = %s")
        params.append(body["plan_status"])
    if "is_active" in body:
        updates.append("is_active = %s")
        params.append(int(bool(body["is_active"])))
    if "max_cards" in body and isinstance(body["max_cards"], int) and body["max_cards"] >= 0:
        updates.append("max_cards = %s")
        params.append(body["max_cards"])

    if not updates:
        return json_error(400, "No valid fields to update.")

    params.append(user_id)
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = %s", params)
        log_admin_action(admin_id, "user_updated", user_id, json.dumps(body))
        return json_resp(200, {"message": "User updated successfully."})
    except Exception:
        logger.exception("update_user failed user_id=%s", user_id)
        return json_error(500, "Failed to update user.")
    finally:
        db.close()


# DELETE /api/admin/user?id=<id>  (soft delete)
@admin_bp.route("/user", methods=["DELETE"])
@require_admin
def delete_user(identity):
    admin_id = int(identity["user_id"])
    user_id  = int(request.args.get("id", 0))
    if not user_id:
        return json_error(400, "User ID required.")
    if user_id == admin_id:
        return json_error(400, "Cannot delete your own account.")

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("UPDATE users SET is_active = 0 WHERE id = %s", (user_id,))
        log_admin_action(admin_id, "user_deleted", user_id)
        return json_resp(200, {"message": "User deleted successfully."})
    except Exception:
        logger.exception("delete_user failed user_id=%s", user_id)
        return json_error(500, "Failed to delete user.")
    finally:
        db.close()


# GET /api/admin/analytics
@admin_bp.route("/analytics", methods=["GET"])
@require_admin
def platform_analytics(identity):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) as total FROM cards")
            total_cards = int(cur.fetchone()["total"])

            cur.execute("SELECT COUNT(*) as total FROM users")
            total_users = int(cur.fetchone()["total"])

            cur.execute("SELECT COUNT(*) as total FROM card_views")
            total_views = int(cur.fetchone()["total"])

            cur.execute("SELECT COUNT(*) as total FROM card_leads")
            total_leads = int(cur.fetchone()["total"])

            cur.execute(
                "SELECT COUNT(*) as count FROM users WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)"
            )
            new_users_7d = int(cur.fetchone()["count"])

            cur.execute(
                "SELECT COUNT(*) as count FROM card_views WHERE viewed_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)"
            )
            views_7d = int(cur.fetchone()["count"])

            try:
                cur.execute(
                    "SELECT COUNT(*) as count FROM cards WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)"
                )
                new_cards_7d = int(cur.fetchone()["count"])
            except Exception:
                new_cards_7d = 0

        return json_resp(200, {
            "total_cards":  total_cards,
            "total_users":  total_users,
            "total_views":  total_views,
            "total_leads":  total_leads,
            "new_users_7d": new_users_7d,
            "new_cards_7d": new_cards_7d,
            "views_7d":     views_7d,
        })
    except Exception:
        logger.exception("platform_analytics failed")
        return json_error(500, "Failed to fetch analytics.")
    finally:
        db.close()


# GET /api/admin/feature-limits
@admin_bp.route("/feature-limits", methods=["GET"])
@require_admin
def get_feature_limits(identity):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM feature_limits ORDER BY plan_type, feature_name")
            limits = cur.fetchall()
        return json_resp(200, {"limits": limits})
    except Exception:
        logger.exception("get_feature_limits failed")
        return json_error(500, "Failed to fetch feature limits.")
    finally:
        db.close()


# PUT /api/admin/feature-limits
@admin_bp.route("/feature-limits", methods=["PUT"])
@require_admin
def update_feature_limits(identity):
    admin_id = int(identity["user_id"])
    body = request.get_json(silent=True) or {}
    limits = body.get("limits", [])
    if not limits:
        return json_error(400, "No limits provided.")
    db = get_db()
    try:
        with db.cursor() as cur:
            for item in limits:
                cur.execute(
                    "UPDATE feature_limits SET limit_value=%s, is_enabled=%s WHERE plan_type=%s AND feature_name=%s",
                    (item.get("limit_value"), int(item.get("is_enabled", 1)), item["plan_type"], item["feature_name"])
                )
        log_admin_action(admin_id, "feature_limits_updated", None, json.dumps(limits))
        return json_resp(200, {"message": "Feature limits updated."})
    except Exception:
        logger.exception("update_feature_limits failed")
        return json_error(500, "Failed to update feature limits.")
    finally:
        db.close()
