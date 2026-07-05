import sqlite3

import pytest

from lib.db import init_db
from lib.dining_dashboard import get_campus_vendors, load_certification_types, load_sustainable_products

CAMPUS_A = "UC Test A"
CAMPUS_B = "UC Test B"


@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    conn.executemany(
        "INSERT INTO campuses (campus, primary_standard, campus_type, abbreviation) VALUES (?, 'AASHE STARS', 'Academic', ?)",
        [(CAMPUS_A, "UCTA"), (CAMPUS_B, "UCTB")],
    )
    conn.execute(
        "INSERT INTO certification_types (certification_name, abbreviation, frameworks) "
        "VALUES ('Organic', 'OG', 'AASHE STARS; Practice Greenhealth')"
    )
    conn.commit()
    yield conn
    conn.close()


def _make_product(conn, name, simap_category, validated_sustainable_yn="NA", certs=None):
    cur = conn.execute(
        "INSERT INTO products (canonical_name, simap_category, sustainability_certifications, sustainable_yn, validated_sustainable_yn) "
        "VALUES (?, ?, ?, 'NA', ?)",
        (name, simap_category, certs, validated_sustainable_yn),
    )
    return cur.lastrowid


def _make_purchase(conn, product_id, campus, vendor, brand=None, weight_source="reported"):
    conn.execute(
        "INSERT INTO purchases (campus, fiscal_year, product_id, vendor, brand, total_price, total_weight_lbs, weight_source) "
        "VALUES (?, 2025, ?, ?, ?, 100.0, 10.0, ?)",
        (campus, product_id, vendor, brand, weight_source),
    )
    conn.commit()


def test_load_sustainable_products_excludes_non_sustainable(conn):
    sus = _make_product(conn, "Organic Apples", "Apples", "Y", "Organic")
    conv = _make_product(conn, "Conventional Apples", "Apples", "N")
    _make_purchase(conn, sus, CAMPUS_A, "Sysco")
    _make_purchase(conn, conv, CAMPUS_A, "Sysco")

    df = load_sustainable_products(conn)

    assert "Organic Apples" in df["canonical_name"].values
    assert "Conventional Apples" not in df["canonical_name"].values


def test_load_sustainable_products_aggregates_cross_campus(conn):
    pid = _make_product(conn, "Fair Trade Coffee", "Stimulants", "Y", "Fair Trade")
    _make_purchase(conn, pid, CAMPUS_A, "Peets", brand="Peet's")
    _make_purchase(conn, pid, CAMPUS_B, "Sysco", brand="Peet's")

    df = load_sustainable_products(conn)
    row = df[df["canonical_name"] == "Fair Trade Coffee"].iloc[0]

    assert sorted(row["campuses"]) == [CAMPUS_A, CAMPUS_B]
    assert sorted(row["vendors"]) == ["Peets", "Sysco"]
    assert row["brands"] == ["Peet's"]


def test_load_sustainable_products_splits_multi_certification_text(conn):
    pid = _make_product(conn, "Halal Certified Beef", "Beef & buffalo meat", "Y", "Certified Halal, Certified Humane, CH")
    _make_purchase(conn, pid, CAMPUS_A, "Sysco")

    df = load_sustainable_products(conn)
    row = df[df["canonical_name"] == "Halal Certified Beef"].iloc[0]

    assert row["cert_list"] == ["Certified Halal", "Certified Humane", "CH"]


def test_load_sustainable_products_fills_unclassified_simap_category(conn):
    pid = _make_product(conn, "Mystery Sustainable Item", None, "Y", "Organic")
    _make_purchase(conn, pid, CAMPUS_A, "Sysco")

    df = load_sustainable_products(conn)
    row = df[df["canonical_name"] == "Mystery Sustainable Item"].iloc[0]

    assert row["simap_category"] == "(Unclassified)"


def test_load_sustainable_products_handles_no_purchases_gracefully(conn):
    # A validated-sustainable product with zero purchases rows shouldn't
    # crash the aggregation (left join, not inner) -- should surface with
    # empty campus/vendor/brand lists rather than being dropped or erroring.
    _make_product(conn, "Never Purchased Organic Rice", "Rice", "Y", "Organic")

    df = load_sustainable_products(conn)
    row = df[df["canonical_name"] == "Never Purchased Organic Rice"].iloc[0]

    assert row["campuses"] == []
    assert row["vendors"] == []
    assert row["brands"] == []


def test_load_sustainable_products_empty_when_none_validated(conn):
    _make_product(conn, "Conventional Only Item", "Apples", "N")

    df = load_sustainable_products(conn)

    assert df.empty


def test_get_campus_vendors_returns_distinct_vendors_for_that_campus_only(conn):
    pid = _make_product(conn, "Organic Milk", "Milk (cow's milk)", "Y", "Organic")
    _make_purchase(conn, pid, CAMPUS_A, "Sysco")
    _make_purchase(conn, pid, CAMPUS_A, "Daylight Foods")
    _make_purchase(conn, pid, CAMPUS_B, "Peets")

    assert get_campus_vendors(conn, CAMPUS_A) == {"Sysco", "Daylight Foods"}
    assert get_campus_vendors(conn, CAMPUS_B) == {"Peets"}


def test_get_campus_vendors_empty_for_campus_with_no_purchases(conn):
    assert get_campus_vendors(conn, CAMPUS_A) == set()


def test_load_certification_types_returns_reference_table(conn):
    df = load_certification_types(conn)
    assert list(df["certification_name"]) == ["Organic"]
    assert df.iloc[0]["abbreviation"] == "OG"
