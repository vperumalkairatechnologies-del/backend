import os
import sys
import logging
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

from flask import Flask, request, make_response
from flask_jwt_extended import JWTManager

from api.auth import auth_bp
from api.cards import cards_bp
from api.leads import leads_bp
from api.qr import qr_bp
from api.vcf import vcf_bp
from api.analytics import analytics_bp
from api.premium import premium_bp
from api.admin import admin_bp

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── JWT config ────────────────────────────────────────────────────────────────
_jwt_secret = os.getenv("JWT_SECRET", "")
if not _jwt_secret:
    # Crash loudly in production — never fall back to a known default
    if os.getenv("FLASK_ENV") == "production":
        logger.critical("JWT_SECRET is not set. Refusing to start in production.")
        sys.exit(1)
    else:
        import secrets
        _jwt_secret = secrets.token_hex(48)
        logger.warning("JWT_SECRET not set — using a random key (dev only). Tokens won't survive restarts.")

app.config["JWT_SECRET_KEY"] = _jwt_secret
app.config["JWT_ALGORITHM"] = os.getenv("JWT_ALGORITHM", "HS256")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=1)   # ← was 7 days; use short-lived + refresh

# ── Upload folder ─────────────────────────────────────────────────────────────
app.config["UPLOAD_FOLDER"] = os.path.join(
    os.path.dirname(__file__), os.getenv("UPLOAD_FOLDER", "uploads")
)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB

JWTManager(app)

# ── CORS (strict allowlist) ───────────────────────────────────────────────────
_raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173")
ALLOWED_ORIGINS: set[str] = {o.strip().rstrip("/") for o in _raw_origins.split(",") if o.strip()}

def _cors_origin(request_origin: str | None) -> str:
    """Return the origin to echo back, or deny with empty string."""
    if not request_origin:
        return ""
    cleaned = request_origin.rstrip("/")
    return cleaned if cleaned in ALLOWED_ORIGINS else ""


@app.after_request
def add_cors_headers(response):
    origin = _cors_origin(request.headers.get("Origin"))
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Vary"] = "Origin"
    return response


@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        return make_response("", 200)


# ── Static uploads ────────────────────────────────────────────────────────────
from flask import send_from_directory

@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# ── Register blueprints ───────────────────────────────────────────────────────
app.register_blueprint(auth_bp,       url_prefix="/api/auth")
app.register_blueprint(cards_bp,      url_prefix="/api/cards")
app.register_blueprint(leads_bp,      url_prefix="/api/leads")
app.register_blueprint(qr_bp,         url_prefix="/api/qr")
app.register_blueprint(vcf_bp,        url_prefix="/api/vcf")
app.register_blueprint(analytics_bp,  url_prefix="/api/analytics")
app.register_blueprint(premium_bp,    url_prefix="/api/premium")
app.register_blueprint(admin_bp,      url_prefix="/api/admin")

if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, port=8000)
