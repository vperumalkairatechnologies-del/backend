import logging
import threading
from datetime import date, timedelta

from flask import Blueprint, request
from config.db import get_db
from config.admin import get_plan_limits
from utils import json_resp, json_error, require_auth

logger = logging.getLogger(__name__)
analytics_bp = Blueprint("analytics", __name__)


def _write_view(card_id, ip, user_agent):
    try:
        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO card_views (card_id, visitor_ip, user_agent) VALUES (%s, %s, %s)",
                (card_id, ip, user_agent),
            )
        db.close()
    except Exception:
        logger.exception("background _write_view failed card_id=%s", card_id)


# POST /api/analytics/view — public
@analytics_bp.route("/view", methods=["POST"])
def log_view():
    body    = request.get_json(silent=True) or {}
    card_id = int(body.get("card_id", 0))
    if not card_id:
        return json_error(400, "card_id is required.")
    ip         = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()[:45]
    user_agent = (request.user_agent.string or "")[:500]
    threading.Thread(target=_write_view, args=(card_id, ip, user_agent), daemon=True).start()
    return json_resp(201, {"message": "View logged."})


# GET /api/analytics?card_id=<id>
@analytics_bp.route("", methods=["GET"])
@require_auth
def get_analytics(identity):
    user_id = int(identity["user_id"])
    card_id = int(request.args.get("card_id", 0))
    if not card_id:
        return json_error(400, "card_id is required.")

    db = get_db()
    try:
        # Verify card ownership
        with db.cursor() as cur:
            cur.execute("SELECT id FROM cards WHERE id=%s AND user_id=%s", (card_id, user_id))
            if not cur.fetchone():
                return json_error(403, "Card not found or access denied.")

        # Get user role for plan-based limits
        with db.cursor() as cur:
            cur.execute("SELECT role FROM users WHERE id=%s", (user_id,))
            user_row = cur.fetchone()
        role   = user_row["role"] if user_row else "basic"
        limits = get_plan_limits(role)
        days_limit = limits["analytics_days"]  # 7 | 30 | -1 (unlimited)

        with db.cursor() as cur:
            # Total views — always available
            cur.execute("SELECT COUNT(*) AS total FROM card_views WHERE card_id=%s", (card_id,))
            total = int(cur.fetchone()["total"])

            # Total leads
            try:
                cur.execute("SELECT COUNT(*) AS total FROM card_leads WHERE card_id=%s", (card_id,))
                total_leads = int(cur.fetchone()["total"])
            except Exception:
                total_leads = 0

            # Daily breakdown — limited by plan
            if days_limit == -1:
                # Advanced: full history (last 365 days max for performance)
                cur.execute(
                    """SELECT DATE(viewed_at) AS date, COUNT(*) AS views
                       FROM card_views WHERE card_id=%s
                       AND viewed_at >= CURDATE() - INTERVAL 364 DAY
                       GROUP BY DATE(viewed_at) ORDER BY date ASC""",
                    (card_id,)
                )
                interval_days = 365
            else:
                cur.execute(
                    """SELECT DATE(viewed_at) AS date, COUNT(*) AS views
                       FROM card_views WHERE card_id=%s
                       AND viewed_at >= CURDATE() - INTERVAL %s DAY
                       GROUP BY DATE(viewed_at) ORDER BY date ASC""",
                    (card_id, days_limit - 1)
                )
                interval_days = days_limit

            rows = cur.fetchall()

        day_map = {str(r["date"]): int(r["views"]) for r in rows}
        days_list = [
            {"date": str(date.today() - timedelta(days=i)), "views": day_map.get(str(date.today() - timedelta(days=i)), 0)}
            for i in range(min(interval_days, 365) - 1, -1, -1)
        ]

        return json_resp(200, {
            "total_views":   total,
            "total_leads":   total_leads,
            "last_7_days":   days_list[:7],        # always return 7-day slice for chart
            "full_history":  days_list,             # full allowed history
            "analytics_days": days_limit,           # tell frontend what plan allows
            "plan":          role,
        })
    except Exception:
        logger.exception("get_analytics failed card_id=%s user_id=%s", card_id, user_id)
        return json_error(500, "Failed to fetch analytics.")
    finally:
        db.close()
