"""Phase 10 - Company scoring.

A transparent lead score built from signals gathered in Phases 1-9. Each factor
contributes fixed points; the breakdown is stored alongside the total so a score
is always explainable.

Note: the spec lists factors that sum to 95 and a stated ceiling of 100, so the
total is clamped to [0, 100].
"""

from __future__ import annotations

# factor key -> (points, human label)
SCORE_FACTORS: dict[str, tuple[int, str]] = {
    "has_website": (10, "Website"),
    "has_email": (20, "Email"),
    "has_linkedin": (5, "LinkedIn"),
    "has_verified_email": (30, "Verified Email"),
    "https": (5, "HTTPS"),
    "active_website": (5, "Active Website"),
    "contact_page": (5, "Contact Page"),
    "has_description": (5, "Company Description"),
    "social_presence": (10, "Social Presence"),
}

MAX_SCORE = 100


def score_company(facts: dict) -> tuple[int, dict]:
    """Return (clamped_total, breakdown) from a dict of boolean signals."""
    breakdown: dict[str, int] = {}
    total = 0
    for key, (points, _label) in SCORE_FACTORS.items():
        if facts.get(key):
            breakdown[key] = points
            total += points
    return min(total, MAX_SCORE), breakdown
