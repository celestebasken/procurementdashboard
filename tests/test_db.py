import sqlite3

import pytest

from lib.db import init_db, migrate_schema


@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    yield conn
    conn.close()


def test_tables_created(conn):
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert {
        "campuses",
        "simap_taxonomy",
        "certification_types",
        "products",
        "product_aliases",
        "purchases",
    } <= tables


def test_purchases_rejects_bad_weight_source(conn):
    conn.execute(
        "INSERT INTO campuses (campus, primary_standard, campus_type, abbreviation) "
        "VALUES ('UC Test', 'AASHE STARS', 'Academic', 'UCT')"
    )
    conn.execute(
        "INSERT INTO products (canonical_name) VALUES ('Test Product')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO purchases (campus, fiscal_year, product_id, weight_source) "
            "VALUES ('UC Test', 2025, 1, 'made_up_value')"
        )


def test_purchases_accepts_valid_weight_source(conn):
    conn.execute(
        "INSERT INTO campuses (campus, primary_standard, campus_type, abbreviation) "
        "VALUES ('UC Test', 'AASHE STARS', 'Academic', 'UCT')"
    )
    conn.execute("INSERT INTO products (canonical_name) VALUES ('Test Product')")
    conn.execute(
        "INSERT INTO purchases (campus, fiscal_year, product_id, weight_source) "
        "VALUES ('UC Test', 2025, 1, 'unresolved')"
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 1


def test_migrate_schema_is_idempotent_and_preserves_existing_data():
    # Simulate a live db that already has products.simap_classification_source
    # missing (predates this migration) and has real data in it -- the
    # migration must not touch existing rows.
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE campuses (campus TEXT PRIMARY KEY, primary_standard TEXT, campus_type TEXT, abbreviation TEXT);
        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT NOT NULL,
            simap_category TEXT,
            sustainability_certifications TEXT,
            sustainable_yn TEXT NOT NULL DEFAULT 'NA',
            certification_validation_flag INTEGER NOT NULL DEFAULT 0,
            validated_sustainable_yn TEXT NOT NULL DEFAULT 'NA',
            first_seen_fy INTEGER,
            last_seen_fy INTEGER
        );
        """
    )
    conn.execute("INSERT INTO products (canonical_name) VALUES ('Pre-existing Product')")
    conn.commit()

    added = migrate_schema(conn)
    assert added == ["products.simap_classification_source"]

    row = conn.execute("SELECT canonical_name, simap_classification_source FROM products").fetchone()
    assert row == ("Pre-existing Product", None)  # untouched, new column just defaults to NULL

    # calling again should be a no-op, not error
    assert migrate_schema(conn) == []
    conn.close()
