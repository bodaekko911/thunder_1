"""Small fuzzy-matching helpers for the dashboard assistant (stdlib only)."""
from __future__ import annotations

import difflib
import re

# Arabic-Indic digits ٠١٢٣٤٥٦٧٨٩ → 0-9
_ARABIC_INDIC_TABLE = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation (keeps apostrophes/hyphens), collapse whitespace,
    and convert Arabic-Indic digits (٠-٩ → 0-9)."""
    text = text.translate(_ARABIC_INDIC_TABLE)
    text = text.lower()
    # keep word chars (\w includes Unicode letters), spaces, apostrophes, hyphens
    text = re.sub(r"[^\w\s'-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def token_overlap_score(text: str, keywords: list[str]) -> float:
    """Return fraction of keywords found as substrings in text (0.0–1.0)."""
    if not keywords:
        return 0.0
    found = sum(1 for kw in keywords if kw in text)
    return found / len(keywords)


def closest_matches(text: str, candidates: list[str], limit: int = 3) -> list[str]:
    """Return up to `limit` close matches for `text` from `candidates` (cutoff=0.5)."""
    return difflib.get_close_matches(text, candidates, n=limit, cutoff=0.5)
