"""Find which existing companies a piece of text refers to, by name. No model involved.

A company is matched by the non-generic words of its name ("Tidewell" for "Tidewell Insurance"),
tolerating small typos, so "insurance" alone never matches. If two companies share those words,
only the full name matches.
"""

import re
import sqlite3
from collections import Counter
from difflib import SequenceMatcher

GENERIC_WORDS = {
    "ai", "and", "bank", "capital", "co", "company", "corp", "corporation", "finance", "financial",
    "group", "health", "holdings", "inc", "insurance", "labs", "limited", "llc", "ltd", "of",
    "partners", "software", "solutions", "systems", "tech", "technologies", "technology", "the",
    "ventures",
}
FUZZY_MIN_LENGTH = 5  # shorter names must match exactly: one typo in a short word is another word
FUZZY_RATIO = 0.85


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _similar(word: str, target: str) -> bool:
    if word == target:
        return True
    if min(len(word), len(target)) < FUZZY_MIN_LENGTH or abs(len(word) - len(target)) > 2:
        return False
    return SequenceMatcher(None, word, target).ratio() >= FUZZY_RATIO


def _count_name(words: list[str], name_words: list[str]) -> int:
    """How often the name occurs, written as separate words or run together ("tide well", "tidewell")."""
    target = "".join(name_words)
    count = 0
    for size in {1, len(name_words), len(name_words) + 1}:
        windows = Counter("".join(words[i:i + size]) for i in range(len(words) - size + 1))
        count += sum(n for window, n in windows.items() if _similar(window, target))
    return count


def _exact_phrase_count(words: list[str], name_words: list[str]) -> int:
    n = len(name_words)
    return sum(words[i:i + n] == name_words for i in range(len(words) - n + 1))


def find_mentions(text: str, companies: list[dict]) -> list[dict]:
    """Companies named in `text`, most mentioned first: [{id, name, count}]."""
    words = _words(text)
    keys = {c["id"]: [w for w in _words(c["name"]) if w not in GENERIC_WORDS] for c in companies}
    mentions = []
    for company in companies:
        key = keys[company["id"]]
        shared = sum(k == key for k in keys.values()) > 1
        if key and not shared:
            count = _count_name(words, key)
        else:
            count = _exact_phrase_count(words, _words(company["name"]))
        if count:
            mentions.append({"id": company["id"], "name": company["name"], "count": count})
    return sorted(mentions, key=lambda m: (-m["count"], m["name"]))


def all_companies(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT id, name FROM companies ORDER BY name")]


def resolve_question_company(conn: sqlite3.Connection, question: str, last_company_id: int | None) -> dict:
    """Decide which company a question is about.

    status "resolved" has `company` and `source` (mentioned / previous). "multiple" and "unknown"
    have `companies` for the user to choose from.
    """
    companies = all_companies(conn)
    mentioned = find_mentions(question, companies)
    if len(mentioned) > 1:
        return {"status": "multiple", "companies": [{"id": m["id"], "name": m["name"]} for m in mentioned]}
    if mentioned:
        return {"status": "resolved", "source": "mentioned",
                "company": {"id": mentioned[0]["id"], "name": mentioned[0]["name"]}}
    previous = next((c for c in companies if c["id"] == last_company_id), None)
    if previous:
        return {"status": "resolved", "source": "previous", "company": previous}
    return {"status": "unknown", "companies": companies}
