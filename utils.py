"""
utils.py — Shared helpers for all API blueprints.
"""

import logging
from functools import wraps
from flask import jsonify, make_response
from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity, get_jwt
from config.db import get_db

logger = logging.getLogger(__name__)

PLAN_LEVELS = {'basic': 0, 'free': 0, 'user': 0, 'pro': 1, 'premium': 1, 'advanced': 2, 'admin': 99}

def plan_level(role):
    return PLAN_LEVELS.get(role or 'basic', 0)

def is_pro_or_above(role):
    return plan_level(role) >= 1

def is_advanced_or_above(role):
    return plan_level(role) >= 2

# ── Response helpers ──────────────────────────────────────────────────────────

def json_resp(status: int, data: dict):
    return make_response(jsonify(data), status)

def json_error(status: int, message: str):
    return json_resp(status, {"error": message})

# ── Auth decorators ───────────────────────────────────────────────────────────

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            verify_jwt_in_request()
            user_id = get_jwt_identity()
            claims  = get_jwt()
        except Exception as exc:
            logger.warning("Auth failed: %s", exc)
            return json_error(401, "Unauthorized. Valid token required.")
        identity = {
            "user_id":     int(user_id),
            "slug":        claims.get("slug"),
            "role":        claims.get("role", "basic"),
            "plan_status": claims.get("plan_status"),
        }
        return f(identity, *args, **kwargs)
    return wrapper


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            verify_jwt_in_request()
            user_id = get_jwt_identity()
            claims  = get_jwt()
        except Exception as exc:
            logger.warning("Admin auth failed: %s", exc)
            return json_error(401, "Unauthorized. Valid token required.")

        try:
            db = get_db()
            with db.cursor() as cur:
                cur.execute(
                    "SELECT role, is_active FROM users WHERE id = %s",
                    (int(user_id),),
                )
                row = cur.fetchone()
            db.close()
        except Exception as exc:
            logger.exception("DB error in require_admin: %s", exc)
            return json_error(500, "Internal error.")

        if not row or not row["is_active"]:
            return json_error(403, "Account not found or deactivated.")
        if row["role"] != "admin":
            return json_error(403, "Admin access required.")

        identity = {
            "user_id":     int(user_id),
            "slug":        claims.get("slug"),
            "role":        row["role"],
            "plan_status": claims.get("plan_status"),
        }
        return f(identity, *args, **kwargs)
    return wrapper
