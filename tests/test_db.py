import sqlite3

import pytest

from app.db import connect, init_db

EXPECTED_TABLES = {"companies", "documents", "chunks", "metrics", "crm_records", "agent_logs"}


def tables(conn):
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def seed(conn):
    """One company, one document, one chunk, one metric, one CRM row."""
    cid = conn.execute("INSERT INTO companies(name) VALUES ('Acme')").lastrowid
    did = conn.execute(
        "INSERT INTO documents(company_id, filename, doc_type) VALUES (?, 'deck.pdf', 'deck')", (cid,)
    ).lastrowid
    chid = conn.execute(
        "INSERT INTO chunks(document_id, company_id, chunk_index, text, page, para, loc_label)"
        " VALUES (?, ?, 0, '100 registered customers', 1, 1, 'deck.pdf p.1 ¶1')", (did, cid)
    ).lastrowid
    conn.execute(
        "INSERT INTO metrics(company_id, document_id, chunk_id, metric, value, value_raw, quote)"
        " VALUES (?, ?, ?, 'registered_customers', 100, '100', '100 registered customers')",
        (cid, did, chid),
    )
    conn.execute(
        "INSERT INTO crm_records(company_id, document_id, row_num, date) VALUES (?, ?, 1, '2026-01-01')",
        (cid, did),
    )
    return cid, did


def test_all_tables_created(db):
    assert EXPECTED_TABLES <= tables(db)


def test_init_db_is_idempotent(db):
    init_db(db)
    assert EXPECTED_TABLES <= tables(db)


def test_deleting_document_cascades(db):
    _, did = seed(db)
    db.execute("DELETE FROM documents WHERE id = ?", (did,))
    for t in ("chunks", "metrics", "crm_records"):
        assert db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t


def test_same_filename_per_company_is_unique(db):
    cid, _ = seed(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO documents(company_id, filename, doc_type) VALUES (?, 'deck.pdf', 'deck')", (cid,))


def test_invalid_metric_name_rejected(db):
    cid, did = seed(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO metrics(company_id, document_id, chunk_id, metric, value_raw, quote)"
            " VALUES (?, ?, 1, 'customers', '1', 'x')", (cid, did)
        )


def test_data_survives_reconnect(tmp_path):
    path = tmp_path / "test.db"
    conn = connect(path)
    init_db(conn)
    seed(conn)
    conn.commit()
    conn.close()

    conn = connect(path)
    init_db(conn)
    assert conn.execute("SELECT name FROM companies").fetchone()["name"] == "Acme"
    conn.close()
