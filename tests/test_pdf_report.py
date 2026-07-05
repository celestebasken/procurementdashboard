import sqlite3

import pytest

from lib.db import init_db
from lib.optimization import build_category_baseline, solve_min_spend_keep_sustainability
from lib.pdf_report import generate_pdf_report

CAMPUS = "UC Test"


@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    conn.execute(
        "INSERT INTO campuses (campus, primary_standard, campus_type, abbreviation) "
        "VALUES (?, 'AASHE STARS', 'Academic', 'UCT')",
        (CAMPUS,),
    )
    conn.executemany(
        "INSERT INTO simap_taxonomy (meat_type, food_category, c_footprint_kg_per_kg_food) VALUES (?, ?, ?)",
        [
            ("Beef", "Beef & buffalo meat", 41.35),
            ("Chicken", "Poultry (chicken, turkey)", 4.40),
        ],
    )
    conn.commit()
    yield conn
    conn.close()


def _make_product(conn, name, simap_category, validated_sustainable_yn="NA"):
    cur = conn.execute(
        "INSERT INTO products (canonical_name, simap_category, sustainable_yn, validated_sustainable_yn) "
        "VALUES (?, ?, 'NA', ?)",
        (name, simap_category, validated_sustainable_yn),
    )
    return cur.lastrowid


def _make_purchase(conn, product_id, price, weight, weight_source="reported"):
    conn.execute(
        "INSERT INTO purchases (campus, fiscal_year, product_id, total_price, total_weight_lbs, weight_source) "
        "VALUES (?, 2025, ?, ?, ?, ?)",
        (CAMPUS, product_id, price, weight, weight_source),
    )
    conn.commit()


@pytest.fixture
def baseline(conn):
    beef_sus = _make_product(conn, "Grass-fed beef", "Beef & buffalo meat", "Y")
    beef_conv = _make_product(conn, "Feedlot beef", "Beef & buffalo meat", "N")
    chix_sus = _make_product(conn, "Pasture chicken", "Poultry (chicken, turkey)", "Y")
    chix_conv = _make_product(conn, "Conventional chicken", "Poultry (chicken, turkey)", "N")
    _make_purchase(conn, beef_sus, price=700.0, weight=100.0)
    _make_purchase(conn, beef_conv, price=500.0, weight=100.0)
    _make_purchase(conn, chix_sus, price=250.0, weight=100.0)
    _make_purchase(conn, chix_conv, price=150.0, weight=100.0)
    return build_category_baseline(conn, CAMPUS)


def test_generate_pdf_report_snapshot_only(baseline):
    pdf_bytes = generate_pdf_report(CAMPUS, baseline)
    assert pdf_bytes[:5] == b"%PDF-"
    assert len(pdf_bytes) > 1000


def test_generate_pdf_report_with_scenario(baseline):
    result = solve_min_spend_keep_sustainability(baseline)
    pdf_bytes = generate_pdf_report(CAMPUS, baseline, scenario_result=result)
    assert pdf_bytes[:5] == b"%PDF-"
    assert len(pdf_bytes) > 1000


def test_generate_pdf_report_handles_unclassified_and_unresolved_weight(conn):
    # A category with an unclassified product and an unresolved-weight row
    # must not crash the report -- these are exactly the "flag, don't
    # guess" edge cases the optimizer/report are supposed to surface.
    classified = _make_product(conn, "Classified item", "Beef & buffalo meat", "N")
    unclassified = _make_product(conn, "Mystery item", None, "N")
    unresolved = _make_product(conn, "Unweighed item", "Beef & buffalo meat", "N")
    _make_purchase(conn, classified, price=100.0, weight=10.0)
    _make_purchase(conn, unclassified, price=250.0, weight=5.0)
    _make_purchase(conn, unresolved, price=300.0, weight=None, weight_source="unresolved")

    baseline_df = build_category_baseline(conn, CAMPUS)
    pdf_bytes = generate_pdf_report(CAMPUS, baseline_df)
    assert pdf_bytes[:5] == b"%PDF-"
