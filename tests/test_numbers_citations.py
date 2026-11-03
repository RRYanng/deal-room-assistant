import pytest

from app.citations import quote_in_text
from app.numbers import currency_of, normalize_number


@pytest.mark.parametrize("raw,expected", [
    ("100", 100), ("1,250", 1250), ("1,200,000", 1_200_000), ("$1.2M", 1_200_000),
    ("$8.5 million", 8_500_000), ("€300k", 300_000), ("2.5bn", 2_500_000_000), ("12.5%", 12.5),
    ("sixty", 60), ("Sixty-two", 62), ("one hundred twenty-five", 125), ("two million", 2_000_000),
    ("100 members", 100),
])
def test_normalize_number(raw, expected):
    assert normalize_number(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "several", "a few dozen"])
def test_normalize_number_refuses_to_guess(raw):
    assert normalize_number(raw) is None


def test_currency_of():
    assert currency_of("$1.2M") == "USD"
    assert currency_of("EUR 300k") == "EUR"
    assert currency_of("100") is None


def test_quote_must_be_verbatim_after_whitespace_normalization():
    text = "Traction\nAs of June 30, 2026, 100 small businesses\nhave registered."
    assert quote_in_text("100 small businesses have registered.", text)  # line break is fine
    assert not quote_in_text("100 small businesses registered.", text)  # word dropped
    assert not quote_in_text("100 Small Businesses have registered.", text)  # case differs
    assert not quote_in_text("   ", text)
