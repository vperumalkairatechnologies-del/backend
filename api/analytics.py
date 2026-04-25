import logging
from datetime import date, timedelta

from flask import Blueprint, request
from config.db import get_db
from utils import json_resp, json_error, require_auth

logger = logging.getLogger(__name__)
analytics_bp = Blueprint("analytics", __name__)


# POST /api/analytics/view — public, no auth required
@analytics_bp.route("/view", methods=["POST"])
def log_view():
    body    = request.get_json(silent=True) or {}
    card_id = int(body.get("card_id", 0))
    if not card_id:
        return json_error(400, "card_id is required.")

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT id FROM cards WHERE id = %s AND is_active = 1", (card_id,))
            if not cur.fetchone():
                return json_error(404, "Card not found.")

            ip         = request.headers.get("X-Forwarded-For", request.remote_addr or "")
            ip         = ip.split(",")[0].strip()[:45]
            user_agent = (request.user_agent.string or "")[:500]

            cur.execute(
                "INSERT INTO card_views (card_id, visitor_ip, user_agent) VALUES (%s, %s, %s)",
                (card_id, ip, user_agent),
            )
        return json_resp(201, {"message": "View logged."})
    except Exception:
        logger.exception("log_view failed card_id=%s", card_id)
        return json_error(500, "Failed to log view.")
    finally:
        db.close()


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
        with db.cursor() as cur:
            cur.execute(
                "SELECT id FROM cards WHERE id = %s AND user_id = %s",
                (card_id, user_id),
            )
            if not cur.fetchone():
                return json_error(403, "Card not found or access denied.")

            cur.execute(
                "SELECT COUNT(*) AS total FROM card_views WHERE card_id = %s",
                (card_id,),
            )
            total = int(cur.fetchone()["total"])

            cur.execute(
                """SELECT DATE(viewed_at) AS date, COUNT(*) AS views
                   FROM card_views
                   WHERE card_id = %s AND viewed_at >= CURDATE() - INTERVAL 6 DAY
                   GROUP BY DATE(viewed_at)
                   ORDER BY date ASC""",
                (card_id,),
            )
            rows = cur.fetchall()

        day_map = {str(r["date"]): int(r["views"]) for r in rows}
        days = [
            {"date": str(date.today() - timedelta(days=i)), "views": day_map.get(str(date.today() - timedelta(days=i)), 0)}
            for i in range(6, -1, -1)
        ]
        return json_resp(200, {"total_views": total, "last_7_days": days})
    except Exception:
        logger.exception("get_analytics failed card_id=%s user_id=%s", card_id, user_id)
        return json_error(500, "Failed to fetch analytics.")
    finally:
        db.close()
