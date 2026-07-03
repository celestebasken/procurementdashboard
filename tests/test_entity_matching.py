import sqlite3

import pytest

from lib.db import init_db
from lib.entity_matching import (
    _clean_for_matching,
    _origins_match,
    find_and_merge_cross_campus,
    find_and_merge_within_campus,
    merge_products,
    normalize_vendor,
)
from lib.ingestion import build_cert_lookup
from lib.reference_loader import load_certification_types


# --------------------------------------------------------------------------
# normalize_vendor
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "a,b",
    [
        ("Sysco", "SYSCO"),
        ("Sysco", "Sysco Corporation"),
        ("Sysco Corp", "Sysco Corporation"),
        ("United Natural Foods Inc.", "United Natural Foods, Inc"),
        ("  Sysco  ", "Sysco"),
    ],
)
def test_normalize_vendor_merges_spelling_variants(a, b):
    assert normalize_vendor(a) == normalize_vendor(b)


def test_normalize_vendor_keeps_different_vendors_distinct():
    assert normalize_vendor("Sysco") != normalize_vendor("US Foods")
    assert normalize_vendor("Coremark International") != normalize_vendor("TREPCO (COREMARK)")


def test_normalize_vendor_handles_blank():
    assert normalize_vendor(None) is None
    assert normalize_vendor("") is None


# --------------------------------------------------------------------------
# merge_products
# --------------------------------------------------------------------------

@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    load_certification_types(conn)
    conn.execute(
        "INSERT INTO campuses (campus, primary_standard, campus_type, abbreviation) "
        "VALUES ('UC Test', 'AASHE STARS', 'Academic', 'UCT')"
    )
    conn.commit()
    yield conn
    conn.close()


def _make_product(conn, name, certs=None, sustainable_yn="NA", flag=0, validated="NA", fy=2025, simap_category=None):
    cur = conn.execute(
        "INSERT INTO products (canonical_name, sustainability_certifications, sustainable_yn, "
        "certification_validation_flag, validated_sustainable_yn, first_seen_fy, last_seen_fy, simap_category) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, certs, sustainable_yn, flag, validated, fy, fy, simap_category),
    )
    return cur.lastrowid


def _make_alias(conn, product_id, campus, raw_name=None):
    conn.execute(
        "INSERT INTO product_aliases (raw_name, campus, product_id, match_confidence, human_confirmed) "
        "VALUES (?, ?, ?, 1.0, 0)",
        (raw_name or f"raw {product_id}", campus, product_id),
    )


def _make_purchase(
    conn, product_id, campus="UC Test", fy=2025, vendor="Sysco", brand=None, price=10.0, weight=1.0, source="unresolved"
):
    conn.execute(
        "INSERT INTO purchases (campus, fiscal_year, product_id, vendor, brand, total_price, total_weight_lbs, "
        "weight_source, n_transactions_aggregated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
        (campus, fy, product_id, vendor, brand, price, weight, source),
    )


def test_merge_products_sums_purchases_for_same_campus_and_fy(conn):
    a = _make_product(conn, "Widget A")
    b = _make_product(conn, "Widget B")
    _make_purchase(conn, a, price=10.0, weight=1.0, source="reported")
    _make_purchase(conn, b, price=20.0, weight=2.0, source="computed_tier2")
    conn.execute(
        "INSERT INTO product_aliases (raw_name, campus, product_id, match_confidence, human_confirmed) "
        "VALUES ('Widget A', 'UC Test', ?, 1.0, 0)", (a,)
    )
    conn.execute(
        "INSERT INTO product_aliases (raw_name, campus, product_id, match_confidence, human_confirmed) "
        "VALUES ('Widget B', 'UC Test', ?, 1.0, 0)", (b,)
    )
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    merge_products(conn, a, b, cert_lookup, "Academic")

    purchases = conn.execute("SELECT total_price, total_weight_lbs, weight_source, n_transactions_aggregated FROM purchases").fetchall()
    assert purchases == [(30.0, 3.0, "computed_tier2", 2)]  # weakest tier among contributing rows
    assert conn.execute("SELECT COUNT(*) FROM products WHERE product_id = ?", (b,)).fetchone()[0] == 0


def test_merge_products_preserves_both_brand_values(conn):
    # vendor (distributor) is the matching gate and is expected identical;
    # brand is a different, often-blank concept that must NOT be dropped
    # when the two merged rows carry different values (confirmed with
    # project owner).
    a = _make_product(conn, "Chicken Breast")
    b = _make_product(conn, "Chicken Breast (SUS)")
    _make_purchase(conn, a, vendor="Sysco", brand="Marys")
    _make_purchase(conn, b, vendor="Sysco", brand="Diestel")
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    merge_products(conn, a, b, cert_lookup, "Academic")

    brand = conn.execute("SELECT brand FROM purchases").fetchone()[0]
    assert brand == "Diestel, Marys"


def test_merge_products_brand_blank_on_one_side_keeps_the_other(conn):
    a = _make_product(conn, "Chicken Breast")
    b = _make_product(conn, "Chicken Breast (SUS)")
    _make_purchase(conn, a, vendor="Sysco", brand=None)
    _make_purchase(conn, b, vendor="Sysco", brand="Diestel")
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    merge_products(conn, a, b, cert_lookup, "Academic")

    brand = conn.execute("SELECT brand FROM purchases").fetchone()[0]
    assert brand == "Diestel"
    aliases = conn.execute("SELECT product_id FROM product_aliases").fetchall()
    assert all(pid == (a,) for pid in aliases)


def test_merge_products_recomputes_flag_when_certs_text_changes(conn):
    a = _make_product(conn, "No Cert Item", certs=None, sustainable_yn="Y", flag=0, validated="Y")
    b = _make_product(conn, "Cert Item", certs="Totally Made Up Certification XYZ", sustainable_yn="Y", flag=1, validated="N")
    _make_purchase(conn, a)
    _make_purchase(conn, b)
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    merge_products(conn, a, b, cert_lookup, "Academic")

    row = conn.execute(
        "SELECT sustainability_certifications, certification_validation_flag, validated_sustainable_yn "
        "FROM products WHERE product_id = ?", (a,)
    ).fetchone()
    # keep_product_id (a) had no certs, so loser's cert text now applies to
    # it -- flag/validated_yn must be recomputed against that new text, not
    # left at keep's stale "no cert claimed" values.
    assert row[0] == "Totally Made Up Certification XYZ"
    assert row[1] == 1
    assert row[2] == "N"


def test_merge_products_self_merge_is_a_noop(conn):
    a = _make_product(conn, "Widget")
    _make_purchase(conn, a)
    conn.commit()
    cert_lookup = build_cert_lookup(conn)
    merge_products(conn, a, a, cert_lookup, "Academic")
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1


def test_merge_products_drops_self_referential_candidate(conn):
    a = _make_product(conn, "Widget A")
    b = _make_product(conn, "Widget B")
    c = _make_product(conn, "Widget C")
    for p in (a, b, c):
        _make_purchase(conn, p)
    # Two pending candidates that both involve b -- after merging b into a,
    # the (a, c) vs (b, c) pair collapses to referencing the same pair twice,
    # but a stale (a, b)-shaped candidate would become self-referential.
    conn.execute(
        "INSERT INTO product_match_candidates (campus, product_id_a, product_id_b, match_score, status) "
        "VALUES ('UC Test', ?, ?, 92.0, 'pending')", (a, b)
    )
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    merge_products(conn, a, b, cert_lookup, "Academic")

    remaining = conn.execute("SELECT product_id_a, product_id_b FROM product_match_candidates").fetchall()
    assert all(pid_a != pid_b for pid_a, pid_b in remaining)


def test_merge_products_preserves_approved_audit_row_even_when_self_referential(conn):
    # Mirrors the review-queue UI's approve flow: mark the candidate being
    # acted on 'approved' BEFORE calling merge_products, since the merge
    # makes that row's own product_id_a/b equal. A 'pending' row in the same
    # situation should still be dropped (covered by the test above); an
    # 'approved' one is an audit record and must survive.
    a = _make_product(conn, "Widget A")
    b = _make_product(conn, "Widget B")
    _make_purchase(conn, a)
    _make_purchase(conn, b)
    cur = conn.execute(
        "INSERT INTO product_match_candidates (campus, product_id_a, product_id_b, match_score, status) "
        "VALUES ('UC Test', ?, ?, 99.9, 'approved')", (a, b)
    )
    candidate_id = cur.lastrowid
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    merge_products(conn, a, b, cert_lookup, "Academic")

    row = conn.execute(
        "SELECT product_id_a, product_id_b, status FROM product_match_candidates WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchone()
    assert row is not None
    assert row == (a, a, "approved")


# --------------------------------------------------------------------------
# find_and_merge_within_campus
# --------------------------------------------------------------------------

def test_find_and_merge_auto_merges_near_identical_names_same_vendor(conn):
    # Case/whitespace-only differences -- token_sort_ratio scores these 100
    # after normalization, comfortably clearing AUTO_MERGE_THRESHOLD.
    a = _make_product(conn, "APPLES GALA XF ORGANIC 100-110 CT")
    b = _make_product(conn, "apples gala xf organic 100-110 ct")
    for p in (a, b):
        _make_purchase(conn, p, vendor="Sysco")
        conn.execute(
            "INSERT INTO product_aliases (raw_name, campus, product_id, match_confidence, human_confirmed) "
            "VALUES (?, 'UC Test', ?, 1.0, 0)", (f"raw {p}", p)
        )
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")

    assert stats["auto_merged"] == 1
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1


def test_find_and_merge_sus_suffix_variant_auto_merges(conn):
    # Confirmed with project owner after manual review: a "(SUS)" suffix is
    # a campus-reporting tag, not a different product -- _clean_for_matching
    # strips it before scoring, so this pair scores 100 (identical once
    # cleaned) and correctly auto-merges rather than sitting in review.
    a = _make_product(conn, "APPLES GALA XF ORGANIC 100-110 CT", sustainable_yn="NA")
    b = _make_product(conn, "APPLES GALA XF ORGANIC 100-110 CT (SUS)", sustainable_yn="Y")
    _make_purchase(conn, a, vendor="Sysco", price=10.0)
    _make_purchase(conn, b, vendor="Sysco", price=20.0)
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")

    assert stats["auto_merged"] == 1
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1
    price = conn.execute("SELECT total_price FROM purchases").fetchone()[0]
    assert price == 30.0  # summed like any other within-campus auto-merge
    # sustainable_yn reconciliation: keep's 'NA' (unknown) defers to the
    # merged-in row's definitive 'Y' rather than discarding it.
    assert conn.execute("SELECT sustainable_yn FROM products").fetchone()[0] == "Y"


def test_find_and_merge_never_auto_merges_differing_quantities(conn):
    # Real example caught via audit: token_sort_ratio scores this pair 97.6
    # (clears AUTO_MERGE_THRESHOLD) even though 25lb vs 5lb is a completely
    # different product. Confirmed with project owner after manual review:
    # differing numbers are "almost never" the same product, so this is
    # excluded from the review queue entirely now, not just auto-merge.
    a = _make_product(conn, "ONION, RED JUMBO 25-LB")
    b = _make_product(conn, "ONION, RED JUMBO 5-LB")
    _make_purchase(conn, a, vendor="Sysco", price=10.0)
    _make_purchase(conn, b, vendor="Sysco", price=20.0)
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")

    assert stats["auto_merged"] == 0
    assert stats["candidates_created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2
    prices = {r[0] for r in conn.execute("SELECT total_price FROM purchases").fetchall()}
    assert prices == {10.0, 20.0}


def test_find_and_merge_never_merges_across_different_vendors(conn):
    a = _make_product(conn, "Cheez-It Crackers")
    b = _make_product(conn, "Cheez-It Crackers")  # identical name, different vendor
    _make_purchase(conn, a, vendor="Sysco")
    _make_purchase(conn, b, vendor="US Foods")
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")

    assert stats["auto_merged"] == 0
    assert stats["candidates_created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2


def test_find_and_merge_queues_medium_confidence_without_touching_data(conn):
    # Similar but not near-identical -- should land in the review band, not
    # auto-merge, and must not touch products/purchases at all.
    a = _make_product(conn, "Organic Roma Tomatoes 25lb Case")
    b = _make_product(conn, "Organic Roma Tomato 25lb Cs")
    _make_purchase(conn, a, vendor="Sysco", price=10.0)
    _make_purchase(conn, b, vendor="Sysco", price=20.0)
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")

    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2
    prices = {r[0] for r in conn.execute("SELECT total_price FROM purchases").fetchall()}
    assert prices == {10.0, 20.0}  # untouched
    if stats["candidates_created"]:
        candidate = conn.execute(
            "SELECT product_id_a, product_id_b, status FROM product_match_candidates"
        ).fetchone()
        assert candidate[2] == "pending"
        assert {candidate[0], candidate[1]} == {a, b}


def test_find_and_merge_transitively_merges_chain(conn):
    # A~B and B~C both score high, but A~C might not directly -- union-find
    # should still merge all three into one component.
    a = _make_product(conn, "Whole Milk Gallon Organic Valley")
    b = _make_product(conn, "Whole Milk Gallon Organic Valley ")
    c = _make_product(conn, "Whole Milk Gallon Organic Valley  ")
    for p in (a, b, c):
        _make_purchase(conn, p, vendor="Sysco")
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")

    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1


# --------------------------------------------------------------------------
# find_and_merge_cross_campus
# --------------------------------------------------------------------------

@pytest.fixture
def two_campus_conn(conn):
    conn.execute(
        "INSERT INTO campuses (campus, primary_standard, campus_type, abbreviation) "
        "VALUES ('UC Other', 'AASHE STARS', 'Academic', 'UCO')"
    )
    conn.commit()
    return conn


def test_find_and_merge_cross_campus_auto_merges_identical_names(two_campus_conn):
    conn = two_campus_conn
    a = _make_product(conn, "APPLES GALA XF ORGANIC 100-110 CT", simap_category="Apples")
    b = _make_product(conn, "apples gala xf organic 100-110 ct", simap_category="Apples")
    _make_purchase(conn, a, campus="UC Test", vendor="Sysco")
    _make_purchase(conn, b, campus="UC Other", vendor="US Foods")
    _make_alias(conn, a, "UC Test")
    _make_alias(conn, b, "UC Other")
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_cross_campus(conn, cert_lookup)

    assert stats["auto_merged"] == 1
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1


def test_find_and_merge_cross_campus_does_not_sum_purchases(two_campus_conn):
    # Unlike within-campus merges, purchases from different campuses must
    # stay as separate rows -- campus is part of what makes them distinct
    # purchase records, not a duplicate to be collapsed.
    conn = two_campus_conn
    a = _make_product(conn, "Chicken Breast Boneless", simap_category="Chicken")
    b = _make_product(conn, "chicken breast boneless", simap_category="Chicken")
    _make_purchase(conn, a, campus="UC Test", price=100.0, weight=10.0)
    _make_purchase(conn, b, campus="UC Other", price=200.0, weight=20.0)
    _make_alias(conn, a, "UC Test")
    _make_alias(conn, b, "UC Other")
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    find_and_merge_cross_campus(conn, cert_lookup)

    purchases = conn.execute(
        "SELECT campus, total_price, total_weight_lbs FROM purchases ORDER BY campus"
    ).fetchall()
    assert purchases == [("UC Other", 200.0, 20.0), ("UC Test", 100.0, 10.0)]
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1


def test_find_and_merge_cross_campus_never_compares_within_same_campus(two_campus_conn):
    # Two near-identical products at the SAME campus must be left alone --
    # that's within-campus matching's job, not this function's.
    conn = two_campus_conn
    a = _make_product(conn, "Whole Milk Gallon", simap_category="Milk")
    b = _make_product(conn, "whole milk gallon", simap_category="Milk")
    _make_purchase(conn, a, campus="UC Test")
    _make_purchase(conn, b, campus="UC Test")
    _make_alias(conn, a, "UC Test")
    _make_alias(conn, b, "UC Test")
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_cross_campus(conn, cert_lookup)

    assert stats["auto_merged"] == 0
    assert stats["candidates_created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2


def test_find_and_merge_cross_campus_skips_unclassified_products(two_campus_conn):
    conn = two_campus_conn
    a = _make_product(conn, "Mystery Item", simap_category=None)
    b = _make_product(conn, "mystery item", simap_category=None)
    _make_purchase(conn, a, campus="UC Test")
    _make_purchase(conn, b, campus="UC Other")
    _make_alias(conn, a, "UC Test")
    _make_alias(conn, b, "UC Other")
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_cross_campus(conn, cert_lookup)

    assert stats["auto_merged"] == 0
    assert stats["candidates_created"] == 0


def test_find_and_merge_cross_campus_never_auto_merges_across_campus_types(conn):
    conn.execute(
        "INSERT INTO campuses (campus, primary_standard, campus_type, abbreviation) "
        "VALUES ('UC Health', 'Practice Greenhealth', 'Health', 'UCH')"
    )
    conn.commit()
    a = _make_product(conn, "Chicken Breast Boneless", simap_category="Chicken")
    b = _make_product(conn, "chicken breast boneless", simap_category="Chicken")
    _make_purchase(conn, a, campus="UC Test")
    _make_purchase(conn, b, campus="UC Health")
    _make_alias(conn, a, "UC Test")
    _make_alias(conn, b, "UC Health")
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_cross_campus(conn, cert_lookup)

    # Perfect name match, but Academic/Health crosses certification
    # frameworks -- must go to review, never auto-merge.
    assert stats["auto_merged"] == 0
    assert stats["candidates_created"] == 1
    row = conn.execute("SELECT campus_a, campus_b FROM product_match_candidates").fetchone()
    assert set(row) == {"UC Test", "UC Health"}


def test_find_and_merge_cross_campus_never_auto_merges_differing_quantities(two_campus_conn):
    conn = two_campus_conn
    a = _make_product(conn, "ONION, RED JUMBO 25-LB", simap_category="Onions")
    b = _make_product(conn, "ONION, RED JUMBO 5-LB", simap_category="Onions")
    _make_purchase(conn, a, campus="UC Test")
    _make_purchase(conn, b, campus="UC Other")
    _make_alias(conn, a, "UC Test")
    _make_alias(conn, b, "UC Other")
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_cross_campus(conn, cert_lookup)

    assert stats["auto_merged"] == 0
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2


# --------------------------------------------------------------------------
# _clean_for_matching / _origins_match
# --------------------------------------------------------------------------

def test_clean_for_matching_strips_sus_suffix():
    assert _clean_for_matching("SALAD MACARONI CLASSIC (SUS)") == "SALAD MACARONI CLASSIC"
    assert _clean_for_matching("SALAD MACARONI CLASSIC") == "SALAD MACARONI CLASSIC"


def test_clean_for_matching_normalizes_curly_quotes():
    assert _clean_for_matching("Briosh 4” Ham Plain Dz") == 'Briosh 4" Ham Plain Dz'
    assert _clean_for_matching("Jo’s Peppermint Creme Cookies") == "Jo's Peppermint Creme Cookies"


def test_origins_match_blocks_different_countries():
    assert _origins_match("GRAPES RED SEEDLESS (CHILE)", "GRAPES RED SEEDLESS (PERU)") is False
    assert _origins_match("ASPARAGUS 11# LARGE (CALIFORNIA)", "ASPARAGUS 11# LARGE (MEXICO)") is False


def test_origins_match_treats_synonyms_as_same_origin():
    # Abbreviated vs full form of the same country -- real audit examples.
    assert _origins_match("JALAPENO CHILE POUND *MEX*", "JALAPENO CHILE POUND *MEXICO*") is True
    assert _origins_match("GRAPES RED SEEDLESS (CHILE)", "GRAPES RED SEEDLESS (CHILEAN)") is True


def test_origins_match_excludes_food_style_words():
    # "Turkey" is the bird, not the country; "Texas Toast" is a bread
    # style, not origin -- verified against real data before excluding.
    assert _origins_match("SUPER TURKEY W/BACON", "TURKEY W/VEGGIS MULTIGR") is True
    assert _origins_match("BREAD TEXAS TOAST 17SLI", "BREAD TEXAS TOAST GARLIC") is True


def test_find_and_merge_never_auto_merges_different_origins(conn):
    a = _make_product(conn, "GRAPES RED SEEDLESS (CHILE)")
    b = _make_product(conn, "GRAPES RED SEEDLESS (PERU)")
    _make_purchase(conn, a, vendor="Sysco", price=10.0)
    _make_purchase(conn, b, vendor="Sysco", price=20.0)
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")

    assert stats["auto_merged"] == 0
    assert stats["candidates_created"] == 0  # excluded from review too, not just auto-merge
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2
