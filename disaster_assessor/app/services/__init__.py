"""Domain services. Optional integrations degrade gracefully when not configured."""


def vision_review_status():
    try:
        from . import vision_review
    except ImportError:  # pragma: no cover
        return False
    try:
        return bool(vision_review.is_configured())
    except Exception:
        return False
