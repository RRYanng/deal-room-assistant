"""Estimated cost of model calls, from token usage and list prices.

USD per million tokens, Anthropic first-party API list prices (Claude API reference, cached
2026-10-06). Cache writes are priced at 1.25x input (5-minute cache). A refusal served by a fallback
model is billed at that model's rates, which this estimate ignores; treat the result as an estimate.
"""

PRICES = {  # model: (input, output, cache write, cache read)
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
}
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def estimate_usd(model: str, usage: dict) -> float | None:
    """None when the model has no listed price."""
    price = PRICES.get(model)
    if price is None:
        return None
    return round(sum(usage.get(f, 0) * p for f, p in zip(USAGE_FIELDS, price)) / 1_000_000, 4)
