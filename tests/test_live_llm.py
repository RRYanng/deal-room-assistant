"""Calls the real model. Run with: pytest -m live  (needs Anthropic credentials)."""

import pytest

from app.llm import get_llm

pytestmark = pytest.mark.live


def test_real_model_returns_json():
    llm = get_llm()
    out = llm.complete_json(
        "You extract numbers.",
        "SYNTHETIC SAMPLE: We had 60 paying customers.",
        {"type": "object", "properties": {"paying_customers": {"type": "integer"}},
         "required": ["paying_customers"], "additionalProperties": False},
    )
    assert out == {"paying_customers": 60}
