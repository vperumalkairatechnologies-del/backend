import logging
from typing import Dict, Any, Optional

from config.db import get_db

logger = logging.getLogger(__name__)


def is_premium_user(identity: Dict[str, Any]) -> bool:
    """Check if a user has premium access based on their role."""
    try:
        role = identity.get("role", "").lower()
        return role == "premium"
    except Exception:
        logger.exception("Error checking premium status for identity: %s", identity)
        return False


def get_user_feature_limits(identity: Dict[str, Any]) -> Dict[str, Any]:
    """Get feature limits for a user based on their subscription plan."""
    try:
        if is_premium_user(identity):
            return {
                "max_cards": 50,
                "max_leads_per_card": 1000,
                "custom_themes": True,
                "analytics": True,
                "export_formats": ["pdf", "vcard", "csv"],
                "priority_support": True,
            }
        else:
            return {
                "max_cards": 3,
                "max_leads_per_card": 50,
                "custom_themes": False,
                "analytics": False,
                "export_formats": ["vcard"],
                "priority_support": False,
            }
    except Exception:
        logger.exception("Error getting feature limits for identity: %s", identity)
        return {
            "max_cards": 1,
            "max_leads_per_card": 10,
            "custom_themes": False,
            "analytics": False,
            "export_formats": ["vcard"],
            "priority_support": False,
        }


def log_admin_action(admin_id: int, action: str, target_user_id: Optional[int] = None, notes: Optional[str] = None) -> None:
    """Log an administrative action for audit purposes."""
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                """INSERT INTO admin_logs (admin_id, action, target_user_id, details, created_at)
                   VALUES (%s, %s, %s, %s, NOW())""",
                (admin_id, action, target_user_id, notes),
            )
    except Exception:
        logger.exception("Failed to log admin action: admin_id=%s, action=%s", admin_id, action)
    finally:
        db.close()


def create_notification(user_id: int, type: str, title: str, message: str) -> None:
    """Create a notification for a user."""
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                """INSERT INTO notifications (user_id, type, title, message, is_read, created_at)
                   VALUES (%s, %s, %s, %s, 0, NOW())""",
                (user_id, type, title, message),
            )
    except Exception:
        logger.exception("Failed to create notification: user_id=%s, type=%s", user_id, type)
    finally:
        db.close()
