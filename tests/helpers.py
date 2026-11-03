"""Shared test helpers: a FakeLLM extraction reply that behaves like a careful model."""

import re

CHUNK_RE = re.compile(r"\[chunk_id=(\d+)\] \((.*?)\)\n(.*?)(?=\n\[chunk_id=|\Z)", re.DOTALL)


def chunks_in_prompt(prompt: str) -> list[tuple[int, str, str]]:
    return [(int(i), label, text) for i, label, text in CHUNK_RE.findall(prompt)]


def fake_extractor(rules: list[dict]):
    """Reply function for FakeLLM.complete_json. Each rule has a `quote`; if a chunk of the
    document in the prompt contains it, the rule is returned as a metric for that chunk."""
    def reply(call):
        out = []
        for chunk_id, _label, text in chunks_in_prompt(call["prompt"]):
            flat = " ".join(text.split())
            for rule in rules:
                if rule["quote"] in flat:
                    out.append({"chunk_id": chunk_id, "metric_label": None, "unit": None,
                                "currency": None, "as_of_date": None} | rule)
        return {"metrics": out}
    return reply


TIDEWELL_RULES = [
    {"metric": "registered_customers", "value_raw": "100", "as_of_date": "2026-06-30",
     "quote": "As of June 30, 2026, 100 small businesses have registered on the Tidewell platform."},
    {"metric": "paying_customers", "value_raw": "60", "as_of_date": "2026-06-30",
     "quote": "Dana said that as of June 30, 2026, Tidewell had 60 paying customers"},
    {"metric": "funding_raised", "value_raw": "$1.2M", "currency": "USD",
     "quote": "We have raised $1.2M in pre-seed funding to date."},
    {"metric": "paying_customers", "value_raw": "80", "as_of_date": "2026-06-30",
     "quote": "As of June 30, 2026, we had 80 paying customers on active policies."},
]

KITEBRIDGE_RULES = [
    {"metric": "registered_customers", "value_raw": "1,250", "as_of_date": "2026-03-31",
     "quote": "As of March 31, 2026, 1,250 businesses have registered on Kitebridge."},
    {"metric": "funding_raised", "value_raw": "$8.5M", "as_of_date": "2026-03-31",
     "quote": "As of March 31, 2026, Kitebridge has raised $8.5M in total funding."},
    {"metric": "paying_customers", "value_raw": "410", "as_of_date": "2026-03-31",
     "quote": "Lee said that as of March 31, 2026, Kitebridge had 410 paying customers"},
]


# --- agent helpers ------------------------------------------------------------------

def load_samples(conn, include_update=False):
    """Load sample files into `conn` with FakeLLM extraction. Returns {company name: id}."""
    import json
    from pathlib import Path

    from app.extract import extract_metrics
    from app.ingest import ingest_document
    from app.llm import FakeLLM

    samples = Path(__file__).resolve().parent.parent / "samples"
    extractor = FakeLLM(default=fake_extractor(TIDEWELL_RULES + KITEBRIDGE_RULES))
    ids = {}
    for m in json.loads((samples / "manifest.json").read_text()):
        if not m["initial"] and not include_update:
            continue
        if m["company"] not in ids:
            ids[m["company"]] = conn.execute("INSERT INTO companies(name) VALUES (?)", (m["company"],)).lastrowid
        path = samples / m["path"]
        doc = ingest_document(conn, ids[m["company"]], path.name, m["doc_type"], m["doc_date"], path.read_bytes())
        extract_metrics(conn, doc["document_id"], extractor)
    conn.commit()
    return ids


def last_tool_output(call):
    """Decoded output of the first tool result in the latest FakeLLM chat call."""
    import json
    return json.loads(call["messages"][-1]["tool_results"][0]["content"])


def cite_from_search(needle, claim, open_items=()):
    """FakeLLM final-answer reply: cite the searched chunk that contains `needle`."""
    import json

    from app.llm import FakeLLM

    def reply(call):
        hits = [r for r in last_tool_output(call) if needle in " ".join(r["text"].split())]
        claims = [{"text": claim, "citations": [{"chunk_id": hits[0]["chunk_id"], "quote": needle}]}] if hits else []
        return FakeLLM.text(json.dumps({"claims": claims, "open_items": list(open_items)}))
    return reply
