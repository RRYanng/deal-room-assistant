import pytest

from app.db import connect, init_db


@pytest.fixture
def db():
    conn = connect(":memory:")
    init_db(conn)
    yield conn
    conn.close()
