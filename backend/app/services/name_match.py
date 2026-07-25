"""KYC name matching — compare a member's registered name with the official name returned by an
Aadhaar / PAN / Passport verification and produce a confidence score (0–100) plus a status.

The score is never shown to the operator; only the derived status (Verified / Manual Review
Required / Not Verified) appears, in the Verification History "Status" column.

Design goals (all met by a greedy token alignment — no I/O, no DB, well under 1 ms per call, so it
scales to thousands of comparisons):
  * Normalisation: lowercase, unicode-normalise + strip accents (José → Jose), drop honorifics
    (Mr./Ms./Dr.…), remove punctuation/dots/commas/hyphens, collapse whitespace.
  * Word order ignored:            "Sharma Rahul Kumar" vs "Rahul Kumar Sharma" → 100.
  * Initials recognised:           "R K Sharma"        vs "Rahul Kumar Sharma" → ~90–96.
  * Missing middle name lenient:   "Rahul Sharma"      vs "Rahul Kumar Sharma" → 90.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# Honorifics / titles stripped before comparison (matched after punctuation removal, so "Mr." →
# "mr"). Deliberately excludes ambiguous tokens (e.g. "md") that can be real name parts.
_TITLES = {
    "mr", "mrs", "ms", "miss", "mx", "dr", "prof", "master", "mstr",
    "shri", "sri", "smt", "kum", "kumari", "late",
}
# A few common spelling variants normalised to one canonical form.
_ABBREV = {"mohd": "mohammed", "mohammad": "mohammed", "muhammad": "mohammed"}

# Per-token match strengths.
_INITIAL_WEIGHT = 0.85   # a single letter matching the start of a full token (Kumar ~ K)
_FUZZY_MIN = 0.8         # minimum SequenceMatcher ratio to count a fuzzy (typo) match

VERIFIED = "VERIFIED"
MANUAL_REVIEW = "MANUAL_REVIEW"
NOT_VERIFIED = "NOT_VERIFIED"


def normalize_name(name: str | None) -> str:
    """Lowercase, strip accents/unicode, drop titles & punctuation, collapse whitespace.

    Example: "MR. Rahul   Kumar-Sharma" → "rahul kumar sharma".
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(c for c in text if not unicodedata.combining(c))   # remove accents
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)                          # punctuation/dots/hyphens → space
    tokens = [_ABBREV.get(t, t) for t in text.split() if t and t not in _TITLES]
    return " ".join(tokens)


def _pair_value(a: str, b: str) -> float:
    """Match strength between two tokens: 1.0 exact, 0.85 initial, else fuzzy ratio (≥0.8) or 0."""
    if a == b:
        return 1.0
    if (len(a) == 1 and b.startswith(a)) or (len(b) == 1 and a.startswith(b)):
        return _INITIAL_WEIGHT
    ratio = SequenceMatcher(None, a, b).ratio()
    return ratio if ratio >= _FUZZY_MIN else 0.0


def match_score(member_name: str | None, kyc_name: str | None) -> int:
    """Confidence (0–100) that member_name and kyc_name denote the same person.

    Greedy best-match alignment of the shorter name's tokens onto the longer name's (so order does
    not matter), initial-aware, and lenient on extra tokens in the longer name (a subset scores
    high rather than being treated as a mismatch).
    """
    small = normalize_name(member_name).split()
    large = normalize_name(kyc_name).split()
    if not small or not large:
        return 0
    if len(small) > len(large):
        small, large = large, small

    used = [False] * len(large)
    matched_sum = 0.0
    for tok in small:
        best_val, best_j = 0.0, -1
        for j, other in enumerate(large):
            if used[j]:
                continue
            val = _pair_value(tok, other)
            if val > best_val:
                best_val, best_j = val, j
                if val == 1.0:
                    break
        if best_j >= 0 and best_val > 0:
            used[best_j] = True
            matched_sum += best_val

    coverage = matched_sum / len(small)        # how fully the shorter name is found in the longer
    completeness = len(small) / len(large)     # gentle penalty for extra tokens (missing middle)
    return round(100 * coverage * (0.7 + 0.3 * completeness))


def match_status(score: int) -> str:
    """Map a confidence score to the status shown in Verification History.
    ≥85 Verified · 70–84 Manual Review Required · <70 Not Verified."""
    if score >= 85:
        return VERIFIED
    if score >= 70:
        return MANUAL_REVIEW
    return NOT_VERIFIED


def score_and_status(member_name: str | None, kyc_name: str | None) -> tuple[int, str]:
    """Convenience: (score, status) in one call."""
    s = match_score(member_name, kyc_name)
    return s, match_status(s)
