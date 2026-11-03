"""Program-side judgments over stored metrics: conflicts, unclear basis, missing metrics.

Conflict = same company + same metric (and label, for "other") + same as-of date
           + same unit/currency + different normalized values.
Different metrics never conflict (registered customers are not paying customers).
A metric without an as-of date is "unclear basis": shown, but never compared.
"""

import sqlite3

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
    return [dict(r) for r in conn.execute(sql, params)]


def find_conflicts(conn: sqlite3.Connection, company_id: int) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for m in get_metrics(conn, company_id):
        if m["as_of_date"] is None or m["value"] is None:
            continue
        key = (m["metric"], (m["metric_label"] or "").lower(), m["as_of_date"], m["unit"], m["currency"])
        groups.setdefault(key, []).append(m)

    conflicts = []
    for (metric, label, as_of, unit, currency), items in groups.items():
        values = sorted({round(m["value"], 6) for m in items})
        if len(values) > 1:
            conflicts.append({
                "metric": metric,
                "metric_label": items[0]["metric_label"],
                "as_of_date": as_of,
                "unit": unit,
                "currency": currency,
                "values": values,
                "sources": [_source(m) for m in items],  # every value with its source; no winner picked
            })
    return conflicts


def unclear_basis(conn: sqlite3.Connection, company_id: int) -> list[dict]:
    out = []
    for m in get_metrics(conn, company_id):
        reasons = []
        if m["as_of_date"] is None:
            reasons.append("no as-of date stated")
        if m["value"] is None:
            reasons.append("value could not be read as a number")
        if reasons:
            out.append(_source(m) | {"metric": m["metric"], "metric_label": m["metric_label"],
                                     "reason": "; ".join(reasons)})
    return out


def missing_metrics(conn: sqlite3.Connection, company_id: int) -> list[str]:
    """Key metrics with no supported value at all in this company's materials."""
    present = {r["metric"] for r in conn.execute(
        "SELECT DISTINCT metric FROM metrics WHERE company_id = ?", (company_id,))}
    return [m for m in KEY_METRICS if m not in present]


def company_checks(conn: sqlite3.Connection, company_id: int) -> dict:
    return {
        "conflicts": find_conflicts(conn, company_id),
        "unclear_basis": unclear_basis(conn, company_id),
        "missing": missing_metrics(conn, company_id),
    }


def _source(m: dict) -> dict:
    return {"metric_id": m["id"], "value": m["value"], "value_raw": m["value_raw"], "unit": m["unit"],
            "currency": m["currency"], "as_of_date": m["as_of_date"], "quote": m["quote"],
            "chunk_id": m["chunk_id"], "loc_label": m["loc_label"], "filename": m["filename"],
            "doc_date": m["doc_date"]}
