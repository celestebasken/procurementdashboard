import sqlite3

import pytest

from lib.db import init_db
from lib.reference_loader import load_all, load_campuses, load_certification_types, load_simap_taxonomy


@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    yield conn
    conn.close()


def test_load_campuses(conn):
    n = load_campuses(conn)
    assert n == 14
    row = conn.execute(
        "SELECT primary_standard, campus_type FROM campuses WHERE abbreviation = 'UCD_H'"
    ).fetchone()
    assert row == ("Practice Greenhealth", "Health")


def test_load_certification_types(conn):
    n = load_certification_types(conn)
    assert n == 53
    row = conn.execute(
        "SELECT frameworks, needs_review FROM certification_types "
        "WHERE certification_name = 'Certified Humane Raised and Handled'"
    ).fetchone()
    assert row[0] == "AASHE STARS; Practice Greenhealth"
    assert row[1] == 0


def test_load_simap_taxonomy(conn):
    n = load_simap_taxonomy(conn)
    assert n == 56
    row = conn.execute(
        "SELECT c_footprint_kg_per_kg_food FROM simap_taxonomy WHERE meat_type = 'Beef'"
    ).fetchone()
    assert row[0] == pytest.approx(41.34628634)


def test_load_all_is_idempotent(conn):
    first = load_all(conn)
    second = load_all(conn)
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM campuses").fetchone()[0] == first["campuses"]
