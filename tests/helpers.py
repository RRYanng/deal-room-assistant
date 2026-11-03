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
