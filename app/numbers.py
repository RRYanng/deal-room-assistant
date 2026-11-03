"""Turn a number as written in a document ("$1.2M", "1,200,000", "sixty") into a float.

This is deliberately done by the program, not the model: it is a clear rule, and
conflict detection compares these normalized values.
"""

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
