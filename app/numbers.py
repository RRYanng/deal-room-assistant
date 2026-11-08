"""Turn a number as written in a document ("$1.2M", "1,200,000", "sixty") into a float.

This is deliberately done by the program, not the model: it is a clear rule, and
conflict detection compares these normalized values.
"""

import math
import re

SCALES = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "mn": 1e6, "million": 1e6,
          "b": 1e9, "bn": 1e9, "billion": 1e9}
NUMERIC_RE = re.compile(
    r"(-?\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion|mm|mn|bn|k|m|b)?(?![a-z])", re.IGNORECASE)

ONES = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
TENS = {w: 10 * i for i, w in enumerate(
    "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}


def normalize_number(raw: str) -> float | None:
    """Return the numeric value of `raw`, or None if it cannot be read unambiguously."""
    if not raw:
        return None
    text = raw.strip().lower()
    match = NUMERIC_RE.search(text)
    if match:
        number = float(match.group(1).replace(",", ""))
        scale = match.group(2)
        return number * SCALES[scale.lower()] if scale else number
    return _words_to_number(text)


def currency_of(raw: str) -> str | None:
    """Currency implied by a symbol or code in the raw text, e.g. "$1.2M" -> "USD"."""
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in raw:
            return code
    found = re.search(r"\b(usd|eur|gbp)\b", raw.lower())
    return found.group(1).upper() if found else None


def _words_to_number(text: str) -> float | None:
    """English number words: "sixty", "one hundred twenty-five", "two million"."""
    words = [w for w in re.split(r"[\s-]+", re.sub(r"[^a-z\s-]", " ", text)) if w and w not in ("and", "a")]
    if not words:
        return None
    total, current, seen = 0, 0, False
    for word in words:
        if word in ONES:
            current += ONES[word]
        elif word in TENS:
            current += TENS[word]
        elif word == "hundred":
            current = (current or 1) * 100
        elif word in ("thousand", "million", "billion"):
            total += (current or 1) * SCALES[word]
            current = 0
        else:
            return None  # unknown word: refuse to guess
        seen = True
    return float(total + current) if seen else None


# --- ranges (v2 claim checks) ---------------------------------------------------

_NUM = r"\$?\s*\d[\d,]*(?:\.\d+)?\s*(?:thousand|million|billion|mm|mn|bn|k|m|b)?(?![a-z])"
_RANGE_RE = re.compile(rf"({_NUM})\s*%?\s*(?:-|to)\s*({_NUM})", re.IGNORECASE)
_ABOVE_RE = re.compile(r"\b(?:above|over|more than|greater than|at least|in excess of|exceeds?)\b|>|\d\s*%?\s*\+")
_BELOW_RE = re.compile(r"\b(?:below|under|less than|up to|at most|no more than)\b|<")


def parse_range(raw: str) -> tuple[float, float] | None:
    """Read a stated number as a range: "67-82%" -> (67, 82), "above 90%" -> (90, inf),
    "under 5" -> (-inf, 5), "85" -> (85, 85). None if there is no readable number."""
    if not raw:
        return None
    text = raw.lower().replace("–", "-").replace("—", "-")
    pair = _RANGE_RE.search(text)
    if pair:
        lo, hi = normalize_number(pair.group(1).replace("$", "")), normalize_number(pair.group(2).replace("$", ""))
        if lo is not None and hi is not None:
            # "$1-2M": the scale written once applies to both ends.
            if hi > lo and lo < 1000 <= hi and not re.search(r"[kmb]", pair.group(1)):
                scale = hi / normalize_number(re.sub(r"[^\d.,]", "", pair.group(2)) or "1")
                lo *= scale
            return (min(lo, hi), max(lo, hi))
    value = normalize_number(text.replace("$", ""))
    if value is None:
        return None
    if _ABOVE_RE.search(text):
        return (value, math.inf)
    if _BELOW_RE.search(text):
        return (-math.inf, value)
    return (value, value)


def ranges_agree(a: tuple[float, float], b: tuple[float, float], tolerance: float = 0.10) -> bool:
    """True if the two ranges overlap. A single value gets +/- `tolerance` around it; a stated range
    or bound is used as written, because it already is the writer's own margin
    ("67-82%" and "above 90%" do not agree, "around 70%" and "67-82%" do)."""
    if a[0] == a[1] and b[0] == b[1]:  # two single values: within `tolerance` of the larger one
        return abs(a[0] - b[0]) <= tolerance * max(abs(a[0]), abs(b[0]))
    a_lo, a_hi = _widen(a, tolerance)
    b_lo, b_hi = _widen(b, tolerance)
    return a_lo <= b_hi and b_lo <= a_hi


def _widen(r: tuple[float, float], tolerance: float) -> tuple[float, float]:
    lo, hi = r
    if lo != hi:
        return r
    return (lo - abs(lo) * tolerance, hi + abs(hi) * tolerance)
