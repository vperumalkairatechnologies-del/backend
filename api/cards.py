import io
import logging
import os
import struct
import uuid

from flask import Blueprint, request, current_app
from config.db import get_db
from utils import json_resp, json_error, require_auth

logger = logging.getLogger(__name__)
cards_bp = Blueprint("cards", __name__)

ALLOWED_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MIME_TO_EXT = {
    "image/jpeg": "jpg",
    "image/png":  "png",
    "image/gif":  "gif",
    "image/webp": "webp",
}

# ── Magic-byte signatures ─────────────────────────────────────────────────────
# We read the first 12 bytes and match against known file headers.
# This prevents an attacker from uploading a PHP/HTML file with a forged
# Content-Type header.

def _detect_image_type(header: bytes) -> str | None:
    """Return MIME string if header matches a known image format, else None."""
    if header[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    # WebP: RIFF????WEBP
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_card_with_links(db, card_id: int):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM cards WHERE id = %s", (card_id,))
        card = cur.fetchone()
        if not card:
            return None
        cur.execute(
            "SELECT id, type, label, url, sort_order FROM card_links "
            "WHERE card_id = %s ORDER BY sort_order ASC",
            (card_id,),
        )
        card["links"] = cur.fetchall()
    return card


def _save_links(db, card_id: int, links: list):
    with db.cursor() as cur:
        cur.execute("DELETE FROM card_links WHERE card_id = %s", (card_id,))
        for i, link in enumerate(links):
            url = str(link.get("url", ""))[:500].strip()
            if url:
                cur.execute(
                    "INSERT INTO card_links (card_id, type, label, url, sort_order) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        card_id,
                        str(link.get("type", ""))[:30].strip(),
                        str(link.get("label", ""))[:100].strip(),
                        url,
                        i,
                    ),
                )


# ── Public: GET /api/cards/public/id/<card_id> ──────────────────────────────
@cards_bp.route("/public/id/<int:card_id>", methods=["GET"])
def public_card_by_id(card_id):
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                """SELECT u.name, u.email, u.slug, c.id, c.title, c.company,
                          c.bio, c.photo, c.theme
                   FROM users u
                   JOIN cards c ON c.user_id = u.id
                   WHERE c.id = %s AND c.is_active = 1""",
                (card_id,),
            )
            card = cur.fetchone()
        if not card:
            return json_error(404, "Card not found.")
        with db.cursor() as cur:
            cur.execute(
                "SELECT type, label, url FROM card_links "
                "WHERE card_id = %s ORDER BY sort_order ASC",
                (card["id"],),
            )
            card["links"] = cur.fetchall()
        return json_resp(200, {"card": card})
    except Exception:
        logger.exception("public_card_by_id failed for card_id=%s", card_id)
        return json_error(500, "Failed to fetch card.")
    finally:
        db.close()


# ── Public: GET /api/cards/public/<slug> ─────────────────────────────────────
@cards_bp.route("/public/<slug>", methods=["GET"])
def public_card(slug):
    slug = slug.strip()
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                """SELECT u.name, u.email, u.slug, c.id, c.title, c.company,
                          c.bio, c.photo, c.theme
                   FROM users u
                   JOIN cards c ON c.user_id = u.id
                   WHERE u.slug = %s AND c.is_active = 1
                   LIMIT 1""",
                (slug,),
            )
            card = cur.fetchone()
        if not card:
            return json_error(404, "Card not found.")
        with db.cursor() as cur:
            cur.execute(
                "SELECT type, label, url FROM card_links "
                "WHERE card_id = %s ORDER BY sort_order ASC",
                (card["id"],),
            )
            card["links"] = cur.fetchall()
        return json_resp(200, {"card": card})
    except Exception:
        logger.exception("public_card failed for slug=%s", slug)
        return json_error(500, "Failed to fetch card.")
    finally:
        db.close()


# ── POST /api/cards/upload ────────────────────────────────────────────────────
@cards_bp.route("/upload", methods=["POST"])
@require_auth
def upload_photo(identity):
    if "photo" not in request.files:
        return json_error(400, "No file uploaded.")

    file = request.files["photo"]

    # 1) Check declared MIME type
    mime = file.mimetype
    if mime not in ALLOWED_MIME:
        return json_error(422, "Only JPEG, PNG, GIF, and WebP images are allowed.")

    # 2) Size check
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > 5 * 1024 * 1024:
        return json_error(422, "File size must be under 5 MB.")

    # 3) Magic-byte check — prevents forged Content-Type attacks
    header = file.read(12)
    file.seek(0)
    actual_mime = _detect_image_type(header)
    if actual_mime is None:
        return json_error(422, "File content does not match a supported image format.")
    if actual_mime != mime:
        logger.warning(
            "MIME mismatch upload: declared=%s actual=%s user=%s",
            mime, actual_mime, identity.get("user_id"),
        )
        return json_error(422, "Declared content type does not match actual file content.")

    ext = MIME_TO_EXT[actual_mime]
    filename = f"photo_{uuid.uuid4().hex}.{ext}"
    upload_dir = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_dir, exist_ok=True)
    file.save(os.path.join(upload_dir, filename))

    return json_resp(200, {"filename": filename})


# ── GET /api/cards ────────────────────────────────────────────────────────────
@cards_bp.route("", methods=["GET"])
@require_auth
def get_cards(identity):
    user_id = int(identity["user_id"])
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, title, company, bio, photo, theme, is_active, created_at, updated_at "
                "FROM cards WHERE user_id = %s ORDER BY created_at DESC",
                (user_id,),
            )
            cards = cur.fetchall()
            
            # Get links for each card
            for card in cards:
                cur.execute(
                    "SELECT id, type, label, url, sort_order FROM card_links "
                    "WHERE card_id = %s ORDER BY sort_order ASC",
                    (card["id"],),
                )
                card["links"] = cur.fetchall()
                
        return json_resp(200, {"cards": cards})
    except Exception:
        logger.exception("get_cards failed for user_id=%s", user_id)
        return json_error(500, "Failed to fetch cards.")
    finally:
        db.close()


# ── GET /api/cards/<int:card_id> ────────────────────────────────────────────────────
@cards_bp.route("/<int:card_id>", methods=["GET"])
@require_auth
def get_single_card(identity, card_id):
    user_id = int(identity["user_id"])
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT id FROM cards WHERE id = %s AND user_id = %s", (card_id, user_id))
            row = cur.fetchone()
        if not row:
            return json_error(404, "Card not found.")
        card = _get_card_with_links(db, card_id)
        return json_resp(200, {"card": card})
    except Exception:
        logger.exception("get_single_card failed for card_id=%s, user_id=%s", card_id, user_id)
        return json_error(500, "Failed to fetch card.")
    finally:
        db.close()


# ── POST /api/cards ───────────────────────────────────────────────────────────
@cards_bp.route("", methods=["POST"])
@require_auth
def create_card(identity):
    user_id = int(identity["user_id"])
    body = request.get_json(silent=True) or {}
    db = get_db()
    try:
        # Check user's card limit based on their plan
        with db.cursor() as cur:
            cur.execute("SELECT role FROM users WHERE id = %s", (user_id,))
            user = cur.fetchone()
            
            cur.execute("SELECT COUNT(*) as count FROM cards WHERE user_id = %s", (user_id,))
            card_count = cur.fetchone()["count"]
            
            # Apply limits based on user role
            max_cards = 1  # Default for free users
            if user and user["role"] == "premium":
                max_cards = 10  # Premium users can have up to 10 cards
            elif user and user["role"] == "admin":
                max_cards = 50  # Admin users can have up to 50 cards
                
            if card_count >= max_cards:
                return json_error(429, f"Card limit reached. You can have maximum {max_cards} cards.")
        
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO cards (user_id, title, company, bio, photo, theme) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    user_id,
                    str(body.get("title", "My Business Card"))[:100].strip(),
                    str(body.get("company", ""))[:100].strip(),
                    body.get("bio", "").strip(),
                    body.get("photo", "").strip(),
                    body.get("theme", "default").strip(),
                ),
            )
            card_id = cur.lastrowid
        if body.get("links") and isinstance(body["links"], list):
            _save_links(db, card_id, body["links"])
        card = _get_card_with_links(db, card_id)
        return json_resp(201, {"card": card})
    except Exception:
        logger.exception("create_card failed for user_id=%s", user_id)
        return json_error(500, "Failed to create card.")
    finally:
        db.close()


# ── PUT /api/cards/<id> ───────────────────────────────────────────────────────
@cards_bp.route("/<int:card_id>", methods=["PUT"])
@require_auth
def update_card(identity, card_id: int):
    user_id = int(identity["user_id"])
    body = request.get_json(silent=True) or {}
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
                "UPDATE cards SET title=%s, company=%s, bio=%s, photo=%s, theme=%s "
                "WHERE id=%s AND user_id=%s",
                (
                    str(body.get("title", ""))[:100].strip(),
                    str(body.get("company", ""))[:100].strip(),
                    body.get("bio", "").strip(),
                    body.get("photo", "").strip(),
                    body.get("theme", "default").strip(),
                    card_id,
                    user_id,
                ),
            )
        if "links" in body and isinstance(body["links"], list):
            _save_links(db, card_id, body["links"])
        card = _get_card_with_links(db, card_id)
        return json_resp(200, {"card": card})
    except Exception:
        logger.exception("update_card failed card_id=%s user_id=%s", card_id, user_id)
        return json_error(500, "Failed to update card.")
    finally:
        db.close()


# ── DELETE /api/cards/<id> ────────────────────────────────────────────────────
@cards_bp.route("/<int:card_id>", methods=["DELETE"])
@require_auth
def delete_card(identity, card_id: int):
    user_id = int(identity["user_id"])
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id FROM cards WHERE id = %s AND user_id = %s",
                (card_id, user_id),
            )
            if not cur.fetchone():
                return json_error(403, "Card not found or access denied.")
            cur.execute("DELETE FROM card_links WHERE card_id = %s", (card_id,))
            cur.execute(
                "DELETE FROM cards WHERE id = %s AND user_id = %s",
                (card_id, user_id),
            )
        return json_resp(200, {"message": "Card deleted successfully."})
    except Exception:
        logger.exception("delete_card failed card_id=%s user_id=%s", card_id, user_id)
        return json_error(500, "Failed to delete card.")
    finally:
        db.close()
