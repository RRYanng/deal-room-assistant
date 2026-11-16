"""Program-side judgments over stored metrics: the Data checks.

Each number's date is its own as-of date, else the date of its file, else "date unknown". Numbers are compared
within one metric: same metric (and label, for "other", after app/data_checks.json maps labels written
differently to one name), same unit and currency. Different metrics never conflict (registered customers are not
paying customers). Within a metric, two different values are:

- a conflict when they are in the same file, when the metric is a market fact (app/data_checks.json) and two
  files disagree, or when their dates are at most 31 days apart (the same period). One exception: a file that
  states each number's own date, further apart ("90 at March 31, 60 at June 30"), gives a change over time;
- "needs review" when either date is unknown: no way to tell growth from a conflict;
- a change over time when their dates are further apart: listed, oldest first, but not an issue.
Missing = a key metric with no value at all.
"""

import json
import re
import sqlite3
from datetime import date
from itertools import combinations
from pathlib import Path

KEY_METRICS = ("registered_customers", "paying_customers", "revenue", "funding_raised")


def get_metrics(conn: sqlite3.Connection, company_id: int, metric: str | None = None) -> list[dict]:
    sql = (
        "SELECT m.id, m.metric, m.metric_label, m.value, m.value_raw, m.unit, m.currency, m.as_of_date,"
        " m.quote, m.chunk_id, m.document_id, ch.loc_label, d.filename, d.doc_type, d.doc_date"
        " FROM metrics m JOIN chunks ch ON ch.id = m.chunk_id JOIN documents d ON d.id = m.document_id"
        " WHERE m.company_id = ?"
    )
    params: list = [company_id]
    if metric:
        sql += " AND m.metric = ?"
        params.append(metric)
    sql += " ORDER BY m.metric, m.as_of_date, d.doc_date, m.id"
    return [dict(r) | {"display_value": display_value(r)} for r in conn.execute(sql, params)]


def display_value(m) -> str:
    """The value as people read it, with its unit: "4 minutes", "60 customers", "USD 1,200,000"."""
    if m["value"] is None:
        number = m["value_raw"]
        if m["currency"]:
            return number
    else:
        number = f"{m['value']:,.2f}".rstrip("0").rstrip(".")
        if m["currency"]:
            return f"{m['currency']} {number}"
    unit = "customers" if m["metric"] in ("registered_customers", "paying_customers") else m["unit"]
    if not unit or unit == "count":
        return number
    if unit == "unknown currency":
        return f"{number} (currency not stated)"
    return f"{number}{unit}" if unit == "%" else f"{number} {unit}"


CONFIG_PATH = Path(__file__).with_name("data_checks.json")
REVIEW_NOTE = "Dates unknown, can't tell if this is growth or a conflict."
REASONS = {"same_file": "different values in the same file",
           "market_fact": "market fact: the files disagree",
           "same_period": "dates within {days} days: the same period"}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DOC_WORDS = {"deck": "deck", "notes": "notes", "crm": "CRM records"}


def load_config() -> dict:
    config = json.loads(CONFIG_PATH.read_text())
    return {"market_facts": {x.lower() for x in config["market_facts"]},
            "same_metric": {k.lower(): v.lower() for k, v in config["same_metric"].items()},
            "same_period_days": int(config["same_period_days"])}


def month_words(iso: str | None) -> str:
    """ "2025-09-15" -> "Sep 2025"."""
    return f"{_MONTHS[int(iso[5:7]) - 1]} {iso[:4]}" if iso else ""


def where(m: dict) -> str:
    """The source in words: "Sep 2025 deck, p.3", "Apr 2026 notes, para 2", "CRM records, row 3"."""
    place = m["loc_label"].replace(m["filename"], "").strip()
    page = place.split(" ¶")[0] if place.startswith("p.") else place.replace("¶", "para ")
    if m["doc_type"] == "crm":
        name = "CRM records"
    elif m["doc_date"]:
        name = f"{month_words(m['doc_date'])} {_DOC_WORDS.get(m['doc_type'], m['doc_type'])}"
    else:
        name = m["filename"]
    return f"{name}, {page}" if page else name


def _dated(m: dict) -> dict:
    """The number's date: its own as-of date, else its file's date, else unknown (None)."""
    if m["as_of_date"]:
        return m | {"date": m["as_of_date"], "date_source": "number"}
    if m["doc_date"]:
        return m | {"date": m["doc_date"], "date_source": "file"}
    return m | {"date": None, "date_source": None}


def _groups(conn: sqlite3.Connection, company_id: int, config: dict) -> dict[tuple, list[dict]]:
    """Comparable numbers, by metric. Values that could not be read as numbers are not compared."""
    groups: dict[tuple, list[dict]] = {}
    for m in get_metrics(conn, company_id):
        if m["value"] is None:
            continue
        label = (m["metric_label"] or "").strip().lower() if m["metric"] == "other" else ""
        label = config["same_metric"].get(label, label)
        key = (m["metric"], label, (m["unit"] or "").lower(), m["currency"])
        groups.setdefault(key, []).append(_dated(m))
    return groups


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _days(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def _own_dates_apart(a: dict, b: dict, days: int) -> bool:
    """Both numbers state their own dates, more than `days` apart: a series in one file, not a conflict."""
    return bool(a["as_of_date"] and b["as_of_date"] and _days(a["as_of_date"], b["as_of_date"]) > days)


def _value(m: dict) -> float:
    return round(m["value"], 6)


def _order(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda m: (m["date"] is None, m["date"] or "", m["id"]))


def _summary(items: list[dict], joiner: str = " vs ", dates: bool = False) -> str:
    """ "250,000 (Sep 2025 deck, p.3) vs 400,000 (Dec 2025 deck, p.3)"; the same value from several places once."""
    parts: dict[str, list[str]] = {}
    for m in items:
        label = m["value_raw"]
        place = f"{_date_words(m)}; {m['where']}" if dates else m["where"]
        parts.setdefault(label, [])
        if place not in parts[label]:
            parts[label].append(place)
    return joiner.join(f"{value} ({'; '.join(places)})" for value, places in parts.items())


def _date_words(m: dict) -> str:
    if m["date"] is None:
        return "date unknown"
    return month_words(m["date"]) + (", file date" if m["date_source"] == "file" else "")


def _row(key: tuple, items: list[dict], **extra) -> dict:
    metric, _label, unit, currency = key
    first = items[0]
    label = first["metric_label"] and _capital(first["metric_label"])  # "total addressable market" -> "Total …"
    return {"metric": metric, "metric_label": label, "unit": first["unit"], "currency": currency,
            "values": sorted({_value(m) for m in items}), "sources": [_source(m) for m in items]} | extra


def data_checks(conn: sqlite3.Connection, company_id: int) -> dict:
    """Conflicts, numbers that need review and changes over time, from the saved materials each time."""
    config = load_config()
    days = config["same_period_days"]
    conflicts, review, changed = [], [], []
    for key, items in _groups(conn, company_id, config).items():
        if len({_value(m) for m in items}) < 2:
            continue
        for m in items:
            m["where"] = where(m)
        market = key[1] in config["market_facts"]
        reasons: list[str] = []
        in_conflict: dict[int, dict] = {}
        in_review: dict[int, dict] = {}
        for a, b in combinations(items, 2):
            if _value(a) == _value(b):
                continue
            if a["document_id"] == b["document_id"] and not _own_dates_apart(a, b, days):
                reason = "same_file"
            elif market:
                reason = "market_fact"
            elif a["date"] is None or b["date"] is None:
                in_review |= {a["id"]: a, b["id"]: b}
                continue
            elif _days(a["date"], b["date"]) <= days:
                reason = "same_period"
            else:
                continue
            in_conflict |= {a["id"]: a, b["id"]: b}
            if reason not in reasons:
                reasons.append(reason)
        if in_conflict:
            rows = _order(list(in_conflict.values()))
            conflicts.append(_row(key, rows, reasons=reasons, summary=_summary(rows),
                                  reason=_capital("; ".join(REASONS[r].format(days=days) for r in reasons))))
        if in_review:
            rows = _order(list(in_review.values()))
            review.append(_row(key, rows, summary=_summary(rows, dates=True), note=REVIEW_NOTE))
        dated = [m for m in items if m["date"]]
        if not market and any(_value(a) != _value(b) and _days(a["date"], b["date"]) > days
                              for a, b in combinations(dated, 2)):
            rows = _order(dated)
            changed.append(_row(key, rows, summary=_timeline(rows)))
    return {"conflicts": conflicts, "needs_review": review, "changed": changed}


def _timeline(items: list[dict]) -> str:
    """ "40 (Aug 2025) → 85 (Nov 2025) → 120 (Apr 2026 file date)": one step per date, oldest first."""
    steps: dict[str, list[dict]] = {}
    for m in items:
        steps.setdefault(m["date"], []).append(m)
    out = []
    for ms in steps.values():
        values = list(dict.fromkeys(m["value_raw"] for m in ms))
        out.append(f"{' or '.join(values)} ({_date_words(ms[0])})")
    return " → ".join(out)


def find_conflicts(conn: sqlite3.Connection, company_id: int) -> list[dict]:
    return data_checks(conn, company_id)["conflicts"]


def missing_metrics(conn: sqlite3.Connection, company_id: int) -> list[str]:
    """Key metrics with no supported value at all in this company's materials."""
    present = {r["metric"] for r in conn.execute(
        "SELECT DISTINCT metric FROM metrics WHERE company_id = ?", (company_id,))}
    return [m for m in KEY_METRICS if m not in present]


def company_checks(conn: sqlite3.Connection, company_id: int) -> dict:
    return data_checks(conn, company_id) | {"missing": missing_metrics(conn, company_id)}


def issue_counts(conn: sqlite3.Connection, company_id: int) -> dict:
    """Open issues per group, computed from the saved materials each time it is called. Changes over time are
    counted, but they are not issues: the total leaves them out."""
    checks = company_checks(conn, company_id)
    counts = {group: len(items) for group, items in checks.items()}
    return counts | {"total": counts["conflicts"] + counts["needs_review"] + counts["missing"]}


def shown_value(s: dict) -> str:
    """A plain number with its unit ("80 customers", "USD 100"); a range or words as written ("67-82%", "above 90%")."""
    raw = (s.get("value_raw") or "").strip()
    if not raw or (s.get("display_value") and re.fullmatch(r"[$€£]?\s?[\d,.]+\s?%?", raw)):
        return s["display_value"]
    return raw


def _source(m: dict) -> dict:
    return {"metric_id": m["id"], "value": m["value"], "value_raw": m["value_raw"],
            "display_value": m["display_value"], "unit": m["unit"],
            "currency": m["currency"], "as_of_date": m["as_of_date"], "quote": m["quote"],
            "chunk_id": m["chunk_id"], "loc_label": m["loc_label"], "filename": m["filename"],
            "doc_date": m["doc_date"], "doc_type": m["doc_type"], "date": m.get("date"),
            "date_source": m.get("date_source"), "where": m.get("where") or where(m)}
