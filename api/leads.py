"""
api/leads.py

FIX: CREATE TABLE has been removed from this endpoint.
     Run the migration in database.sql / add-role-columns.sql before deploying.
     DDL in hot paths causes a table-level lock on every single request.
"""

import logging
import re

from flask import Blueprint, request
from config.db import get_db
from utils import json_resp, json_error

logger = logging.getLogger(__name__)
leads_bp = Blueprint("leads", __name__)


@leads_bp.route("", methods=["POST"])
def capture_lead():
    body = request.get_json(silent=True) or {}
    slug   = body.get("slug",  "").strip()
    name   = str(body.get("name",  "")).strip()[:120]
    email  = str(body.get("email", "")).strip()[:190]
    phone  = str(body.get("phone", "")).strip()[:30]
    note   = body.get("note", "").strip()

    if not slug:
        return json_error(400, "Slug is required.")
    if not name:
        return json_error(422, "Name is required.")
    if not email and not phone:
        return json_error(422, "Email or phone is required.")
    if email and not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        return json_error(422, "Invalid email format.")

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                """SELECT c.id FROM cards c
                   JOIN users u ON u.id = c.user_id
                   WHERE u.slug = %s AND c.is_active = 1
                   LIMIT 1""",
                (slug,),
            )
            card = cur.fetchone()
            if not card:
                return json_error(404, "Card not found.")

            cur.execute(
                """INSERT INTO card_leads
                       (card_id, lead_name, lead_email, lead_phone, lead_note, source)
                   VALUES (%s, %s, %s, %s, %s, 'public_card')""",
                (card["id"], name, email or None, phone or None, note or None),
            )
        return json_resp(201, {"message": "Lead captured successfully."})
    except Exception:
        logger.exception("capture_lead failed slug=%s", slug)
        return json_error(500, "Failed to capture lead.")
    finally:
        db.close()
