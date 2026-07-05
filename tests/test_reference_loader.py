import sqlite3

import pytest

from lib.db import init_db
from lib.reference_loader import (
    load_all,
    load_campuses,
    load_certification_types,
    load_food_groups,
    load_simap_taxonomy,
)


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


def test_load_food_groups(conn):
    n = load_food_groups(conn)
    assert n == 55
    row = conn.execute(
        "SELECT food_group FROM food_groups WHERE simap_category = 'Beef & buffalo meat'"
    ).fetchone()
    assert row[0] == "Protein (Meat/Poultry/Seafood/Plant)"
    # Every simap_taxonomy category must have a food_groups mapping -- an
    # unmapped category would silently fall back to a group-of-one in
    # lib.optimization, hiding a real gap in this reference table.
    load_simap_taxonomy(conn)
    unmapped = conn.execute(
        "SELECT DISTINCT TRIM(food_category) FROM simap_taxonomy "
        "WHERE TRIM(food_category) NOT IN (SELECT simap_category FROM food_groups)"
    ).fetchall()
    assert unmapped == []


def test_load_all_is_idempotent(conn):
    first = load_all(conn)
    second = load_all(conn)
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM campuses").fetchone()[0] == first["campuses"]


def test_load_simap_taxonomy_strips_food_category_whitespace(conn):
    # Real bug: "Citrus Fruit " / "Corn (Maize) " (trailing whitespace in the
    # source CSV) silently broke the join against products.simap_category
    # (clean, no whitespace) for every product in those categories.
    load_simap_taxonomy(conn)
    for category in ("Citrus Fruit", "Corn (Maize)"):
        row = conn.execute(
            "SELECT food_category FROM simap_taxonomy WHERE food_category = ?", (category,)
        ).fetchone()
        assert row is not None, f"{category!r} should match with no trailing whitespace"
