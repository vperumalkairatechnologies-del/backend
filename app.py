import os
import sys
import logging
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

from flask import Flask, request, make_response, Response
from flask_jwt_extended import JWTManager
from flask_compress import Compress

from api.auth import auth_bp
from api.cards import cards_bp
from api.leads import leads_bp
from api.qr import qr_bp
from api.vcf import vcf_bp
from api.analytics import analytics_bp
from api.premium import premium_bp
from api.admin import admin_bp
from api.payments import payments_bp

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
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(days=7)

# ── Upload folder ─────────────────────────────────────────────────────────────
app.config["UPLOAD_FOLDER"] = os.path.join(
    os.path.dirname(__file__), os.getenv("UPLOAD_FOLDER", "uploads")
)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB

JWTManager(app)
Compress(app)
app.config['COMPRESS_MIMETYPES'] = ['application/json', 'text/plain']
app.config['COMPRESS_LEVEL'] = 6
app.config['COMPRESS_MIN_SIZE'] = 500

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
app.register_blueprint(payments_bp,   url_prefix="/api/pay")

# ── Auto-migrate: add max_cards column if missing ────────────────────────────
def _run_migrations():
    try:
        from config.db import get_db
        db = get_db()
        with db.cursor() as cur:
            # max_cards column
            cur.execute("""
                SELECT COUNT(*) as cnt FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'max_cards'
            """)
            if cur.fetchone()["cnt"] == 0:
                cur.execute("ALTER TABLE users ADD COLUMN max_cards INT DEFAULT NULL")
                logger.info("Migration: added max_cards column")
            # plan_expires_at column
            cur.execute("""
                SELECT COUNT(*) as cnt FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'plan_expires_at'
            """)
            if cur.fetchone()["cnt"] == 0:
                cur.execute("ALTER TABLE users ADD COLUMN plan_expires_at DATETIME DEFAULT NULL")
                logger.info("Migration: added plan_expires_at column")
            # payments table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                  id INT AUTO_INCREMENT PRIMARY KEY,
                  user_id INT NOT NULL,
                  plan ENUM('pro','advanced') NOT NULL,
                  amount INT NOT NULL,
                  currency VARCHAR(10) DEFAULT 'INR',
                  phonepe_order_id VARCHAR(100) DEFAULT NULL,
                  phonepe_txn_id VARCHAR(100) DEFAULT NULL,
                  status ENUM('pending','success','failed','refunded') DEFAULT 'pending',
                  discount_amount INT DEFAULT 0,
                  coupon_id INT DEFAULT NULL,
                  subscription_id INT DEFAULT NULL,
                  created_at DATETIME DEFAULT NOW(),
                  updated_at DATETIME DEFAULT NOW() ON UPDATE NOW(),
                  INDEX idx_user_id (user_id),
                  INDEX idx_phonepe_order (phonepe_order_id)
                )
            """)
            # subscriptions table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                  id INT AUTO_INCREMENT PRIMARY KEY,
                  user_id INT NOT NULL,
                  plan ENUM('basic','pro','advanced') NOT NULL DEFAULT 'basic',
                  status ENUM('active','expired','cancelled','pending') NOT NULL DEFAULT 'pending',
                  payment_id INT DEFAULT NULL,
                  start_date DATETIME DEFAULT NOW(),
                  end_date DATETIME DEFAULT NULL,
                  cancelled_at DATETIME DEFAULT NULL,
                  admin_note VARCHAR(255) DEFAULT NULL,
                  created_at DATETIME DEFAULT NOW(),
                  updated_at DATETIME DEFAULT NOW() ON UPDATE NOW(),
                  INDEX idx_user_id (user_id),
                  INDEX idx_end_date (end_date)
                )
            """)
            # coupons table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS coupons (
                  id INT AUTO_INCREMENT PRIMARY KEY,
                  code VARCHAR(50) NOT NULL UNIQUE,
                  discount_type ENUM('percent','fixed') NOT NULL DEFAULT 'percent',
                  discount_value INT NOT NULL,
                  max_uses INT DEFAULT NULL,
                  used_count INT DEFAULT 0,
                  valid_from DATETIME DEFAULT NOW(),
                  valid_until DATETIME DEFAULT NULL,
                  applicable_plan ENUM('pro','advanced','all') DEFAULT 'all',
                  is_active TINYINT(1) DEFAULT 1,
                  created_at DATETIME DEFAULT NOW(),
                  INDEX idx_code (code)
                )
            """)
            logger.info("Migration: billing tables ready")
            # Migrate old role values
            cur.execute("UPDATE users SET role='basic' WHERE role IN ('free','user')")
            cur.execute("UPDATE users SET role='pro'   WHERE role='premium'")
            logger.info("Migration: role values updated")
        db.close()
    except Exception as e:
        logger.warning("Migration failed (non-fatal): %s", e)

_run_migrations()

if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, port=8000)
