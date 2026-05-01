import logging
import os
import re

from flask import Blueprint, request
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash, check_password_hash
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from config.db import get_db
from utils import json_resp, json_error

logger = logging.getLogger(__name__)
auth_bp = Blueprint("auth", __name__)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")


def _generate_slug(name: str, db) -> str:
    base = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip()).strip("-").lower()
    slug = base
    i = 1
    while True:
        with db.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE slug = %s", (slug,))
            if not cur.fetchone():
                break
        slug = f"{base}-{i}"
        i += 1
    return slug


@auth_bp.route("/me", methods=["GET"])
def me():
    from utils import require_auth
    @require_auth
    def _inner(identity):
        db = get_db()
        try:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT id, name, email, slug, role, plan_status "
                    "FROM users WHERE id = %s AND is_active = 1",
                    (identity["user_id"],),
                )
                user = cur.fetchone()
            if not user:
                return json_error(404, "User not found.")
            return json_resp(200, {"user": {
                "id":          user["id"],
                "name":        user["name"],
                "email":       user["email"],
                "slug":        user["slug"],
                "role":        user["role"],
                "plan_status": user["plan_status"],
            }})
        except Exception:
            logger.exception("me failed")
            return json_error(500, "Failed to fetch user.")
        finally:
            db.close()
    return _inner()


@auth_bp.route("/register", methods=["POST"])
def register():
    body  = request.get_json(silent=True) or {}
    name  = body.get("name",  "").strip()
    email = body.get("email", "").strip()
    password = body.get("password", "")

    if not name or not email or not password:
        return json_error(422, "Name, email, and password are required.")
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        return json_error(422, "Invalid email address.")
    if len(password) < 6:
        return json_error(422, "Password must be at least 6 characters.")

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                return json_error(409, "Email already registered.")

            slug   = _generate_slug(name, db)
            hashed = generate_password_hash(password, method="pbkdf2:sha256")

            cur.execute(
                "INSERT INTO users (name, email, password, slug, role, plan_status) "
                "VALUES (%s, %s, %s, %s, 'basic', NULL)",
                (name, email, hashed, slug),
            )
            user_id = cur.lastrowid

        token = create_access_token(
            identity=str(user_id),
            additional_claims={"slug": slug, "role": "basic", "plan_status": None}
        )
        return json_resp(201, {
            "token": token,
            "user": {
                "id": user_id, "name": name, "email": email,
                "slug": slug, "role": "basic", "plan_status": None,
            },
        })
    except Exception:
        logger.exception("register failed email=%s", email)
        return json_error(500, "Registration failed. Please try again.")
    finally:
        db.close()


@auth_bp.route("/login", methods=["POST"])
def login():
    body  = request.get_json(silent=True) or {}
    email = body.get("email", "").strip()
    password = body.get("password", "")

    if not email or not password:
        return json_error(422, "Email and password are required.")

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, name, email, password, slug, role, plan_status "
                "FROM users WHERE email = %s AND is_active = 1",
                (email,),
            )
            user = cur.fetchone()

        try:
            stored = user["password"] if user else ""
            if stored.startswith("$2y$") or stored.startswith("$2b$"):
                password_valid = check_password_hash(stored.replace("$2y$", "$2b$", 1), password)
            elif stored.startswith(("pbkdf2:", "scrypt:")):
                password_valid = check_password_hash(stored, password)
            else:
                password_valid = stored == password
        except ValueError:
            password_valid = False
        if not password_valid:
            return json_error(401, "Invalid email or password.")

        # Auto-upgrade plain text or PHP hash to pbkdf2 on login
        if user and not user["password"].startswith(("pbkdf2:", "scrypt:")):
            try:
                new_hash = generate_password_hash(password, method="pbkdf2:sha256")
                with db.cursor() as cur:
                    cur.execute("UPDATE users SET password = %s WHERE id = %s", (new_hash, user["id"]))
            except Exception:
                pass

        token = create_access_token(
            identity=str(user["id"]),
            additional_claims={
                "slug":        user["slug"],
                "role":        user.get("role", "basic"),
                "plan_status": user.get("plan_status"),
            }
        )
        return json_resp(200, {
            "token": token,
            "user": {
                "id":          user["id"],
                "name":        user["name"],
                "email":       user["email"],
                "slug":        user["slug"],
                "role":        user.get("role", "basic"),
                "plan_status": user.get("plan_status"),
            },
        })
    except Exception:
        logger.exception("login failed email=%s", email)
        return json_error(500, "Login failed. Please try again.")
    finally:
        db.close()


@auth_bp.route("/google", methods=["POST"])
def google_login():
    body     = request.get_json(silent=True) or {}
    token    = body.get("credential", "")
    userInfo = body.get("userInfo", {})

    if not token or not userInfo:
        return json_error(422, "Google credential is required.")

    email = userInfo.get("email", "").strip()
    name  = userInfo.get("name",  "").strip() or email.split("@")[0]

    if not email:
        return json_error(422, "Could not get email from Google account.")

    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, name, email, slug, role, plan_status FROM users WHERE email = %s AND is_active = 1",
                (email,),
            )
            user = cur.fetchone()

        if not user:
            slug   = _generate_slug(name, db)
            hashed = generate_password_hash(os.urandom(32).hex(), method="pbkdf2:sha256")
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (name, email, password, slug, role, plan_status) "
                    "VALUES (%s, %s, %s, %s, 'basic', NULL)",
                    (name, email, hashed, slug),
                )
                user_id = cur.lastrowid
            user = {"id": user_id, "name": name, "email": email,
                    "slug": slug, "role": "basic", "plan_status": None}

        token_jwt = create_access_token(
            identity=str(user["id"]),
            additional_claims={
                "slug":        user["slug"],
                "role":        user.get("role", "basic"),
                "plan_status": user.get("plan_status"),
            }
        )
        return json_resp(200, {
            "token": token_jwt,
            "user": {
                "id":          user["id"],
                "name":        user["name"],
                "email":       user["email"],
                "slug":        user["slug"],
                "role":        user.get("role", "basic"),
                "plan_status": user.get("plan_status"),
            },
        })
    except Exception:
        logger.exception("google_login failed email=%s", email)
        return json_error(500, "Google login failed. Please try again.")
    finally:
        db.close()
