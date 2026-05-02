import logging
from typing import Dict, Any, Optional

from config.db import get_db

logger = logging.getLogger(__name__)

# ── Plan definitions ──────────────────────────────────────────────────────────
PLAN_LEVELS = {'basic': 0, 'free': 0, 'user': 0, 'pro': 1, 'premium': 1, 'advanced': 2, 'admin': 99}

PLAN_LIMITS = {
    'basic': {
        'max_cards':        1,
        'max_social_links': 5,
        'analytics_days':   7,
        'cover_photo':      False,
        'company_logo':     False,
        'virtual_background': False,
        'custom_colors':    False,
        'lead_capture':     False,
        'csv_export':       False,
        'custom_slug':      False,
    },
    'pro': {
        'max_cards':        3,
        'max_social_links': -1,   # unlimited
        'analytics_days':   30,
        'cover_photo':      True,
        'company_logo':     True,
        'virtual_background': False,
        'custom_colors':    True,
        'lead_capture':     True,
        'csv_export':       False,
        'custom_slug':      False,
    },
    'advanced': {
        'max_cards':        -1,   # unlimited
        'max_social_links': -1,
        'analytics_days':   -1,   # full history
        'cover_photo':      True,
        'company_logo':     True,
        'virtual_background': True,
        'custom_colors':    True,
        'lead_capture':     True,
        'csv_export':       True,
        'custom_slug':      True,
    },
    'admin': {
        'max_cards':        -1,
        'max_social_links': -1,
        'analytics_days':   -1,
        'cover_photo':      True,
        'company_logo':     True,
        'virtual_background': True,
        'custom_colors':    True,
        'lead_capture':     True,
        'csv_export':       True,
        'custom_slug':      True,
    },
}


def get_plan_level(role: str) -> int:
    role_map = {'free': 'basic', 'user': 'basic', 'premium': 'pro'}
    normalized = role_map.get(role or 'basic', role or 'basic')
    return PLAN_LEVELS.get(normalized, 0)


def get_plan_limits(role: str) -> dict:
    """Return plan limits for a given role. Handles legacy role names."""
    # Normalize legacy role names
    role_map = {'free': 'basic', 'user': 'basic', 'premium': 'pro'}
    normalized = role_map.get(role or 'basic', role or 'basic')
    return PLAN_LIMITS.get(normalized, PLAN_LIMITS['basic'])


def is_premium_user(identity: Dict[str, Any]) -> bool:
    """Check if user has pro or above plan."""
    role = (identity.get('role') or '').lower()
    return get_plan_level(role) >= 1


def can_access_feature(role: str, feature: str) -> bool:
    """Check if a role can access a specific feature."""
    limits = get_plan_limits(role)
    val = limits.get(feature, False)
    if isinstance(val, bool):
        return val
    return val != 0


def get_user_feature_limits(identity: Dict[str, Any]) -> Dict[str, Any]:
    """Get feature limits for a user — used by /premium/features endpoint."""
    role = (identity.get('role') or 'basic').lower()
    limits = get_plan_limits(role)
    return {
        'max_cards':          limits['max_cards'],
        'max_social_links':   limits['max_social_links'],
        'analytics_days':     limits['analytics_days'],
        'cover_photo':        {'enabled': limits['cover_photo']},
        'company_logo':       {'enabled': limits['company_logo']},
        'virtual_background': {'enabled': limits['virtual_background']},
        'custom_colors':      {'enabled': limits['custom_colors']},
        'lead_capture':       {'enabled': limits['lead_capture']},
        'csv_export':         {'enabled': limits['csv_export']},
        'custom_slug':        {'enabled': limits['custom_slug']},
    }


def log_admin_action(admin_id: int, action: str, target_user_id: Optional[int] = None, notes: Optional[str] = None) -> None:
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO admin_logs (admin_id, action, target_user_id, details, created_at) "
                "VALUES (%s, %s, %s, %s, NOW())",
                (admin_id, action, target_user_id, notes),
            )
    except Exception:
        logger.exception("Failed to log admin action: admin_id=%s, action=%s", admin_id, action)
    finally:
        db.close()


def create_notification(user_id: int, type: str, title: str, message: str) -> None:
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO notifications (user_id, type, title, message, is_read, created_at) "
                "VALUES (%s, %s, %s, %s, 0, NOW())",
                (user_id, type, title, message),
            )
    except Exception:
        logger.exception("Failed to create notification: user_id=%s, type=%s", user_id, type)
    finally:
        db.close()
