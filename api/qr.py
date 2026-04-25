import io
import logging
import os
import re

from flask import Blueprint, request, send_file
import qrcode

from config.db import get_db
from utils import json_error

logger = logging.getLogger(__name__)
qr_bp = Blueprint("qr", __name__)

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://vcardfrontendnew.vercel.app")


def _make_qr(slug, target=None):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE slug = %s AND is_active = 1", (slug,))
            if not cur.fetchone():
                return json_error(404, "User not found.")
    except Exception:
        logger.exception("generate_qr DB error slug=%s", slug)
        return json_error(500, "Database error.")
    finally:
        db.close()

    if target:
        if not re.match(r"https?://", target):
            return json_error(422, "Invalid target URL.")
        card_url = target
    else:
        card_url = f"{FRONTEND_URL}/card/{slug}"

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(card_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    return send_file(
        buf,
        mimetype="image/png",
        as_attachment=False,
        download_name=f"qr-{slug}.png",
        max_age=86400,
    )


@qr_bp.route("", methods=["GET"])
def generate_qr():
    slug = request.args.get("slug", "").strip()
    if not slug:
        return json_error(400, "Slug is required.")
    return _make_qr(slug, request.args.get("target", "").strip() or None)


@qr_bp.route("/<slug>", methods=["GET"])
def generate_qr_path(slug):
    return _make_qr(slug.strip(), request.args.get("target", "").strip() or None)
