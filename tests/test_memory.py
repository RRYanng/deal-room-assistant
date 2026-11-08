"""Memory (E11, E12): what changed since the last deck, and nothing shared between companies."""

import pytest

from app.claims import create_run, latest_results, run_claim_check
from app.memo import build_memo, memo_to_markdown
from app.memory import changes
from tests.helpers import load_samples
from tests.helpers_v2 import DECK, claim_llm, load_clearclaim

SEP_DECK = "clearclaim_deck_2025-09.pdf"


def check(db, company_id, document_id, llm=None):
    run_claim_check(db, create_run(db, company_id, document_id), llm or claim_llm())
    return latest_results(db, company_id)


@pytest.fixture
def checked(db):
    company_id, docs = load_clearclaim(db)
    return company_id, docs, check(db, company_id, docs[DECK])


def by_key(changes_list):
    return {c["key"]: c for c in changes_list}


def test_e11_market_size_change_between_decks_is_flagged(checked):
    _, _, out = checked
    found = by_key(out["changes"])
    practices = found["market.customer_count"]
    assert practices["kind"] == "changed"
    assert (practices["old"]["value_raw"], practices["new"]["value_raw"]) == ("250,000", "400,000")
    assert practices["old"]["filename"] == SEP_DECK and practices["new"]["filename"] == DECK
    assert practices["note"] == f"250,000 ({SEP_DECK}, 2025-09-15) -> 400,000 ({DECK}, 2025-12-01)."
    assert found["market.size_usd"]["kind"] == "changed"  # $900M -> $12B


def test_figures_that_move_with_time_are_updates_not_flags(checked):
    found = by_key(checked[2]["changes"])
    assert found["traction.paying_customers"]["kind"] == "updated"  # 40 -> 85
    assert found["team.headcount"]["kind"] == "updated"  # 9 -> 14


def test_unchanged_figures_are_not_reported(checked):
    found = by_key(checked[2]["changes"])
    assert "market.price_usd" not in found  # $3,600 in both decks
    assert "market.industry_spend_usd" not in found  # $400 billion in both decks


def test_the_earlier_deck_is_read_once_and_the_run_says_so(db, checked):
    company_id, docs, out = checked
    assert any(f"Read the earlier deck {SEP_DECK} once" in n for n in out["run"]["notes"])
    llm = claim_llm()
    second = check(db, company_id, docs[DECK], llm)
    extract_calls = [c for c in llm.calls if "list the key claims" in c["system"]]
    assert len(extract_calls) == 1  # only the checked deck; the earlier one is already in memory
    assert not any("Read the earlier deck" in n for n in second["run"]["notes"])
    assert by_key(second["changes"])["market.customer_count"]["old"]["value_raw"] == "250,000"


def test_memory_survives_deleting_the_earlier_deck(db, checked):
    company_id, docs, _ = checked
    db.execute("DELETE FROM documents WHERE id = ?", (docs[SEP_DECK],))
    db.commit()
    found = by_key(changes(db, company_id, docs[DECK]))
    assert found["market.customer_count"]["old"]["filename"] == SEP_DECK


def test_changes_are_in_the_memo(db, checked):
    text = memo_to_markdown(build_memo(db, checked[0]))
    section = text.split("## Changed since the last deck")[1].split("##")[0]
    assert "**Changed** (market.customer_count): 250,000" in section
    assert "Updated (traction.paying_customers): 40" in section


def test_a_single_deck_has_nothing_to_compare(db):
    company_id, docs = load_clearclaim(db)
    db.execute("DELETE FROM documents WHERE id = ?", (docs[SEP_DECK],))
    db.commit()
    assert check(db, company_id, docs[DECK])["changes"] == []


# --- E12: three companies, nothing crosses ---------------------------------------------------------

def test_e12_memory_never_crosses_companies(db):
    ids = load_samples(db)
    clearclaim, docs = load_clearclaim(db)
    tidewell, kitebridge = ids["Tidewell Insurance"], ids["Kitebridge Finance"]
    # Another company holds an earlier deck with the same key and a different figure.
    tidewell_deck = db.execute("SELECT id FROM documents WHERE company_id = ? AND doc_type = 'deck'",
                               (tidewell,)).fetchone()["id"]
    db.execute("INSERT INTO memory_entries(company_id, document_id, filename, doc_date, key, value_raw, value_lo,"
               " value_hi, quote) VALUES (?, ?, 'tidewell_old_deck.pdf', '2025-01-01', 'market.customer_count',"
               " '999', 999, 999, 'There are 999 practices.')", (tidewell, tidewell_deck))
    db.commit()

    llm = claim_llm()
    out = check(db, clearclaim, docs[DECK], llm)
    practices = by_key(out["changes"])["market.customer_count"]
    assert practices["old"]["value_raw"] == "250,000"  # ClearClaim's own earlier deck, not Tidewell's 999
    assert all(c["old"]["filename"].startswith("clearclaim") for c in out["changes"])
    prompts = " ".join(c["prompt"] for c in llm.calls)
    assert "Tidewell" not in prompts and "Kitebridge" not in prompts

    for other in (tidewell, kitebridge):
        deck = db.execute("SELECT id FROM documents WHERE company_id = ? AND doc_type = 'deck'", (other,)).fetchone()
        result = check(db, other, deck["id"], claim_llm())
        assert all("clearclaim" not in c["old"]["filename"] for c in result["changes"])
        assert db.execute("SELECT COUNT(*) FROM memory_entries WHERE company_id = ? AND filename LIKE 'clearclaim%'",
                          (other,)).fetchone()[0] == 0
