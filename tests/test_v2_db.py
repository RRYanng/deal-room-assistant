"""v2 tables: added next to the v1 tables, without changing them."""

import sqlite3

import pytest

from app.db import SCHEMA, connect, init_db

V2_TABLES = {"claim_runs", "claims", "web_sources", "evidence", "competitors", "reviews", "monitor_runs",
             "monitor_periods", "signals", "memory_entries"}


def tables(conn):
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def seed_run(conn, name="Acme"):
    cid = conn.execute("INSERT INTO companies(name) VALUES (?)", (name,)).lastrowid
    did = conn.execute("INSERT INTO documents(company_id, filename, doc_type) VALUES (?, 'deck.pdf', 'deck')",
                       (cid,)).lastrowid
    chid = conn.execute("INSERT INTO chunks(document_id, company_id, chunk_index, text, loc_label)"
                        " VALUES (?, ?, 0, 'Gross margin is 70%.', 'deck.pdf p.1 ¶1')", (did, cid)).lastrowid
    rid = conn.execute("INSERT INTO claim_runs(company_id, document_id, status) VALUES (?, ?, 'done')",
                       (cid, did)).lastrowid
    claim = conn.execute(
        "INSERT INTO claims(run_id, company_id, document_id, chunk_id, key, statement, quote, fingerprint, status)"
        " VALUES (?, ?, ?, ?, 'unit.gross_margin_pct', 's', 'Gross margin is 70%.', 'fp', 'unverified')",
        (rid, cid, did, chid)).lastrowid
    conn.execute("INSERT INTO evidence(claim_id, company_id, source, stance, note) VALUES (?, ?, 'calculation',"
                 " 'contradicts', 'n')", (claim, cid))
    return cid, did


def test_v2_tables_created(db):
    assert V2_TABLES <= tables(db)


def test_v2_tables_added_to_a_v1_database_without_touching_its_data(tmp_path):
    """The demo database was made by v1.1; starting v2 on a copy must only add the new tables."""
    v1_schema = SCHEMA.split("-- v2:")[0]
    path = tmp_path / "v1.db"
    conn = connect(path)
    conn.executescript(v1_schema)
    conn.execute("INSERT INTO companies(name) VALUES ('Tidewell Insurance')")
    conn.commit()
    assert not V2_TABLES & tables(conn)

    init_db(conn)
    assert V2_TABLES <= tables(conn)
    assert [r["name"] for r in conn.execute("SELECT name FROM companies")] == ["Tidewell Insurance"]


def test_deleting_the_deck_removes_its_claim_runs(db):
    _, did = seed_run(db)
    db.execute("DELETE FROM documents WHERE id = ?", (did,))
    assert db.execute("SELECT COUNT(*) FROM claim_runs").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


def test_review_needs_a_reason(db):
    cid, _ = seed_run(db)
    for reason in ("", "   "):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO reviews(company_id, item_type, fingerprint, decision, reason)"
                       " VALUES (?, 'claim', 'fp', 'dismissed', ?)", (cid, reason))


def test_unknown_status_and_stance_are_refused(db):
    cid, did = seed_run(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE claims SET status = 'true'")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE evidence SET stance = 'proves'")


def test_columns_added_later_reach_an_earlier_v2_database(tmp_path):
    """dev databases made before source_type and scope existed get the columns on startup."""
    path = tmp_path / "early.db"
    conn = connect(path)
    early = SCHEMA.replace(",\n    source_type  TEXT", "").replace(
        ",             -- written by the program\n    scope         TEXT NOT NULL DEFAULT 'company'", "")
    conn.executescript(early)
    have = lambda t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
    assert "source_type" not in have("web_sources") and "scope" not in have("evidence")
    init_db(conn)
    init_db(conn)  # idempotent
    assert "source_type" in have("web_sources") and "scope" in have("evidence")
