import logging
import os
import re

from flask import Blueprint, request, Response
from config.db import get_db
from utils import json_error

logger = logging.getLogger(__name__)
vcf_bp = Blueprint("vcf", __name__)


def _vcf_escape(value: str) -> str:
    return (
        value
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace(":", "\\:")
    )


@vcf_bp.route("", methods=["GET"])
def download_vcf():
    slug = request.args.get("slug", "").strip()
    if not slug:
        return json_error(400, "Slug is required.")

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                """SELECT u.name, u.email, u.slug,
                          c.id, c.title, c.company, c.bio, c.photo
                   FROM users u
                   JOIN cards c ON c.user_id = u.id
                   WHERE u.slug = %s AND c.is_active = 1
                   LIMIT 1""",
                (slug,),
            )
            card = cur.fetchone()
            if not card:
                return json_error(404, "Card not found.")

            cur.execute(
                "SELECT type, label, url FROM card_links WHERE card_id = %s ORDER BY sort_order ASC",
                (card["id"],),
            )
            links = cur.fetchall()
    except Exception:
        logger.exception("download_vcf DB error slug=%s", slug)
        return json_error(500, "Database error.")
    finally:
        db.close()

    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"FN:{_vcf_escape(card['name'])}",
        f"N:{_vcf_escape(card['name'])};\\;\\;\\;",
    ]

    if card.get("title") or card.get("company"):
        lines.append(f"ORG:{_vcf_escape(card.get('company') or '')}")
        lines.append(f"TITLE:{_vcf_escape(card.get('title') or '')}")

    if card.get("email"):
        lines.append(f"EMAIL;TYPE=INTERNET:{_vcf_escape(card['email'])}")

    if card.get("bio"):
        lines.append(f"NOTE:{_vcf_escape(card['bio'])}")

    for link in links:
        type_ = (link.get("type") or "").lower()
        url   = link.get("url", "")
        if type_ in ("phone", "whatsapp"):
            tel = re.sub(r"[^\d+]", "", url)
            if tel:
                lines.append(f"TEL;TYPE=CELL:{tel}")
        elif type_ == "email":
            lines.append(f"EMAIL;TYPE=INTERNET:{_vcf_escape(url)}")
        elif type_ == "website":
            lines.append(f"URL:{_vcf_escape(url)}")
        elif type_ in ("linkedin", "github", "twitter", "instagram"):
            lines.append(f"URL;TYPE={type_.upper()}:{_vcf_escape(url)}")
        elif re.match(r"https?://", url):
            lines.append(f"URL:{_vcf_escape(url)}")

    if card.get("photo"):
        scheme   = "https" if request.is_secure else "http"
        photo_url = f"{scheme}://{request.host}/smartcard/backend/uploads/{os.path.basename(card['photo'])}"
        lines.append(f"PHOTO;VALUE=URI:{photo_url}")

    lines.append("END:VCARD")
    vcf_content = "\r\n".join(lines) + "\r\n"

    return Response(
        vcf_content,
        status=200,
        headers={
            "Content-Type":        "text/vcard; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{slug}.vcf"',
            "Content-Length":      str(len(vcf_content.encode("utf-8"))),
            "Cache-Control":       "no-cache",
        },
    )
