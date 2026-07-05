import sqlite3

import pytest

from lib.db import init_db
from lib.entity_matching import (
    _clean_for_matching,
    _color_match,
    _container_match,
    _decaf_match,
    _dietary_claim_match,
    _diet_zero_match,
    _frozen_match,
    _fruit_confusion_match,
    _grade_letter_match,
    _halal_match,
    _numbers_match,
    _origins_match,
    _quality_match,
    _volume_match,
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


def test_find_and_merge_sus_suffix_variant_auto_merges_when_sustainable_yn_matches(conn):
    # Confirmed with project owner after manual review: a "(SUS)" suffix is
    # a campus-reporting tag, not a different product -- _clean_for_matching
    # strips it before scoring, so this pair scores 100 (identical once
    # cleaned). But the merge itself additionally requires sustainable_yn
    # to actually agree -- both 'Y' here, so it auto-merges.
    a = _make_product(conn, "APPLES GALA XF ORGANIC 100-110 CT", sustainable_yn="Y")
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


def test_find_and_merge_sus_suffix_variant_never_auto_merges_when_sustainable_yn_differs(conn):
    # UC Davis's real "SANDWICH FRESH ZESTY TURKEY WRAP" pair prompted this:
    # a "(SUS)" suffix alone is NOT enough -- if the underlying
    # sustainable_yn claims genuinely disagree ('NA' vs 'Y' here), these are
    # two different purchasing lines, not the same product re-tagged.
    a = _make_product(conn, "APPLES GALA XF ORGANIC 100-110 CT", sustainable_yn="NA")
    b = _make_product(conn, "APPLES GALA XF ORGANIC 100-110 CT (SUS)", sustainable_yn="Y")
    _make_purchase(conn, a, vendor="Sysco", price=10.0)
    _make_purchase(conn, b, vendor="Sysco", price=20.0)
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")

    assert stats["auto_merged"] == 0
    assert stats["candidates_created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2


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
    _make_purchase(conn, b, campus="UC Other", vendor="Sysco")
    _make_alias(conn, a, "UC Test")
    _make_alias(conn, b, "UC Other")
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_cross_campus(conn, cert_lookup)

    assert stats["auto_merged"] == 1
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 1


def test_find_and_merge_cross_campus_never_auto_merges_differing_vendor(two_campus_conn):
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

    assert stats["auto_merged"] == 0
    assert stats["candidates_created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2


def test_find_and_merge_cross_campus_never_auto_merges_differing_sustainable_yn(two_campus_conn):
    conn = two_campus_conn
    a = _make_product(conn, "APPLES GALA XF ORGANIC 100-110 CT", simap_category="Apples", sustainable_yn="Y")
    b = _make_product(conn, "apples gala xf organic 100-110 ct", simap_category="Apples", sustainable_yn="N")
    _make_purchase(conn, a, campus="UC Test", vendor="Sysco")
    _make_purchase(conn, b, campus="UC Other", vendor="Sysco")
    _make_alias(conn, a, "UC Test")
    _make_alias(conn, b, "UC Other")
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_cross_campus(conn, cert_lookup)

    assert stats["auto_merged"] == 0
    assert stats["candidates_created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2


def test_find_and_merge_cross_campus_auto_merges_when_both_sustainable(two_campus_conn):
    # A "(SUS)"-suffix pair (stripped for scoring by _clean_for_matching)
    # still merges fine when both sides are ACTUALLY sustainable_yn='Y'.
    conn = two_campus_conn
    a = _make_product(conn, "APPLES GALA XF ORGANIC 100-110 CT", simap_category="Apples", sustainable_yn="Y")
    b = _make_product(conn, "APPLES GALA XF ORGANIC 100-110 CT (SUS)", simap_category="Apples", sustainable_yn="Y")
    _make_purchase(conn, a, campus="UC Test", vendor="Sysco")
    _make_purchase(conn, b, campus="UC Other", vendor="Sysco")
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


# --------------------------------------------------------------------------
# _numbers_match -- fraction handling
# --------------------------------------------------------------------------

def test_numbers_match_blocks_differing_cut_size_fraction():
    # Real audit example: a 1/4" cut size vs a 1" cut size used to pass this
    # gate by accident -- flattening "1/4" into separate digits "1" and "4"
    # made both names' digit sets equal to {1,4,5} once combined with the
    # unrelated "4/5-LB" pack size elsewhere in the string.
    assert _numbers_match('BELL PEPPER, RED DICED 1/4" 4/5-LB', 'BELL PEPPER, RED DICED 1" 4/5-LB') is False


def test_numbers_match_treats_identical_fractions_as_equal():
    assert _numbers_match('BELL PEPPER, RED DICED 1/4" 4/5-LB', 'BELL PEPPER, RED DICED 1/4" 4/5-LB (SUS)') is True


def test_find_and_merge_never_auto_merges_differing_cut_size_fraction(conn):
    a = _make_product(conn, 'BELL PEPPER, RED DICED 1/4" 4/5-LB')
    b = _make_product(conn, 'BELL PEPPER, RED DICED 1" 4/5-LB')
    _make_purchase(conn, a, vendor="Sysco", price=10.0)
    _make_purchase(conn, b, vendor="Sysco", price=20.0)
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")

    assert stats["auto_merged"] == 0
    assert stats["candidates_created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2


# --------------------------------------------------------------------------
# _halal_match
# --------------------------------------------------------------------------

def test_halal_match_blocks_halal_vs_non_halal():
    assert _halal_match("CHICKEN BREAST BONELESS SKINLESS (H)", "CHICKEN BREAST BONELESS SKINLESS") is False


def test_halal_match_allows_same_halal_status():
    assert _halal_match("CHICKEN BREAST BONELESS SKINLESS (H)", "CHICKEN BREAST BONELESS SKINLESS (H)") is True
    assert _halal_match("CHICKEN BREAST BONELESS SKINLESS", "CHICKEN BREAST BONELESS SKINLESS") is True


def test_halal_match_is_case_insensitive():
    assert _halal_match("CHICKEN BREAST (h)", "CHICKEN BREAST (H)") is True


def test_halal_match_recognizes_spelled_out_word():
    # Real missed case: UC's supplier-spec-style naming spells out "Halal"
    # instead of using "(H)" -- e.g. "Beef Loin, Tri Tip, C 185C, Halal".
    assert _halal_match("Beef Chuck, Tail Flap Meat, Boneless, Halal", "Beef Chuck, Tail Flap Meat, Boneless") is False
    assert _halal_match("Beef Loin, Tri Tip, C 185C, Halal", "Beef Loin, Tri Tip, C 185C, Halal") is True


def test_find_and_merge_never_auto_merges_halal_mismatch(conn):
    a = _make_product(conn, "CHICKEN BREAST BONELESS SKINLESS (H)")
    b = _make_product(conn, "CHICKEN BREAST BONELESS SKINLESS")
    _make_purchase(conn, a, vendor="Sysco", price=10.0)
    _make_purchase(conn, b, vendor="Sysco", price=20.0)
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")

    assert stats["auto_merged"] == 0
    assert stats["candidates_created"] == 0  # excluded from review too, not just auto-merge
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2


# --------------------------------------------------------------------------
# _frozen_match
# --------------------------------------------------------------------------

def test_frozen_match_blocks_frozen_vs_non_frozen():
    assert _frozen_match("CHICKEN THIGH BNLS SKLS NAE FZ", "CHICKEN THIGH BNLS SKLS NAE") is False
    assert _frozen_match("BEEF CHUCK FLAP 116C FR", "BEEF CHUCK FLAP 116C") is False


def test_frozen_match_allows_same_frozen_status():
    assert _frozen_match("CHICKEN THIGH BNLS SKLS NAE FZ", "CHICKEN THIGH BNLS SKLS NAE FZ") is True
    assert _frozen_match("CHICKEN THIGH BNLS SKLS NAE", "CHICKEN THIGH BNLS SKLS NAE") is True


def test_frozen_match_does_not_false_positive_on_substrings():
    # "FRESH" contains "FR" but isn't a standalone token -- must not trigger.
    assert _frozen_match("STRAWBERRIES FRESH WHOLE", "STRAWBERRIES FRESH WHOLE DICED") is True


# --------------------------------------------------------------------------
# _grade_letter_match (potato A/B grade)
# --------------------------------------------------------------------------

def test_grade_letter_match_blocks_differing_potato_grade():
    assert _grade_letter_match("POTATO, RED A 50-LB", "POTATO, RED B 50-LB") is False


def test_grade_letter_match_allows_same_potato_grade():
    assert _grade_letter_match("POTATO, RED B 50-LB", "POTATO, RED B 50-LB (SUS)") is True


def test_grade_letter_match_ignores_non_potato_products():
    # "W"/"P" etc. are common unrelated single-letter tokens elsewhere in
    # this data (e.g. "W/SKIN", distributor item-code prefixes) -- the gate
    # only fires for potato products, so these must never block.
    assert _grade_letter_match("POTATO CHIP SKINON W/ SEA SALT", "POTATO CHIP SKINON") is True
    assert _grade_letter_match("P Potatoes Fries 1/4 Golden Fry GFR01 6 ct", "Potatoes Fries 1/4 Golden Fry") is True


def test_find_and_merge_never_auto_merges_differing_potato_grade(conn):
    a = _make_product(conn, "POTATO, RED A 50-LB")
    b = _make_product(conn, "POTATO, RED B 50-LB")
    _make_purchase(conn, a, vendor="Sysco", price=10.0)
    _make_purchase(conn, b, vendor="Sysco", price=20.0)
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")

    assert stats["auto_merged"] == 0
    assert stats["candidates_created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2


# --------------------------------------------------------------------------
# _volume_match (HGL vs GL/GAL)
# --------------------------------------------------------------------------

def test_volume_match_blocks_half_gallon_vs_gallon():
    assert _volume_match("CR HOMO HGL PL", "CR HOMO GL PL") is False
    assert _volume_match("CR HOMO HGL PL", "CR HOMO GAL PL") is False


def test_volume_match_allows_same_volume_marker():
    assert _volume_match("CR HOMO HGL PL", "CR HOMO HGL PPR") is True
    assert _volume_match("BIB Dr Pepper 5 GL", "BIB Dr Pepper 5 GAL") is True


def test_volume_match_ignores_products_without_volume_markers():
    assert _volume_match("CHICKEN BREAST BONELESS", "CHICKEN BREAST BONELESS SKINLESS") is True


# --------------------------------------------------------------------------
# _clean_for_matching -- leading brand possessive / LIQ abbreviation
# --------------------------------------------------------------------------

def test_clean_for_matching_rejoins_leading_brand_possessive():
    assert _clean_for_matching("BRENTLEY S CHICKEN") == "BRENTLEYS CHICKEN"
    assert _clean_for_matching("BRENTLEYS CHICKEN") == "BRENTLEYS CHICKEN"


def test_clean_for_matching_leading_brand_fix_is_scoped_to_first_word():
    # Must not touch a standalone "S" elsewhere in the name (e.g. a real
    # size code) -- only the leading brand-name position is a confirmed
    # apostrophe-dropped-to-space artifact.
    assert _clean_for_matching("CHICKEN BREAST S SIZE") == "CHICKEN BREAST S SIZE"


def test_clean_for_matching_normalizes_liq_abbreviation():
    assert _clean_for_matching("CREAMER, LIQ REF CTN NONDARY") == "CREAMER, LIQUID REF CTN NONDARY"
    assert _clean_for_matching("CREAMER, LIQUID REF CTN NONDARY") == "CREAMER, LIQUID REF CTN NONDARY"


def test_clean_for_matching_strips_parens_around_a_word():
    assert _clean_for_matching("STRAWBERRIES (FRESH) WHOLE") == "STRAWBERRIES FRESH WHOLE"
    assert _clean_for_matching("STRAWBERRIES FRESH WHOLE") == "STRAWBERRIES FRESH WHOLE"


def test_clean_for_matching_normalizes_word_synonyms():
    assert _clean_for_matching("MANGO PERUVIAN") == _clean_for_matching("MANGO PERU")
    assert _clean_for_matching("ORANGE, 88 CT") == _clean_for_matching("ORANGE, 88 COUNT")
    assert _clean_for_matching("SQUASH PEEL W/TOP") == _clean_for_matching("SQUASH PEELED W/TOP")
    assert _clean_for_matching("CANDY GUMMI BEAR") == _clean_for_matching("CANDY GUMMY BEAR")
    assert _clean_for_matching("BEAN PINTO FCY") == _clean_for_matching("BEAN PINTO FANCY")


# --------------------------------------------------------------------------
# _decaf_match
# --------------------------------------------------------------------------

def test_decaf_match_blocks_decaf_vs_regular():
    assert _decaf_match("COFFEE PEETS DECAF HOUSE BLEND", "COFFEE PEETS HOUSE BLEND") is False
    assert _decaf_match("COFFEE, GROUND DECAFFEINATED ARABICA BAG", "COFFEE, GROUND ARABICA BAG") is False


def test_decaf_match_allows_same_decaf_status():
    assert _decaf_match("COFFEE PEETS DECAF HOUSE BLEND", "COFFEE PEETS DECAF HOUSE BLEND (SUS)") is True
    assert _decaf_match("COFFEE PEETS HOUSE BLEND", "COFFEE PEETS HOUSE BLEND") is True
    # "decaf" and "decaffeinated" both count as the marker being present.
    assert _decaf_match("COFFEE PEETS DECAF HOUSE BLEND", "COFFEE, GROUND DECAFFEINATED HOUSE BLEND") is True


def test_find_and_merge_never_auto_merges_decaf_mismatch(conn):
    a = _make_product(conn, "COFFEE PEETS DECAF HOUSE BLEND")
    b = _make_product(conn, "COFFEE PEETS HOUSE BLEND")
    _make_purchase(conn, a, vendor="Sysco", price=10.0)
    _make_purchase(conn, b, vendor="Sysco", price=20.0)
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")

    assert stats["auto_merged"] == 0
    assert stats["candidates_created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2


# --------------------------------------------------------------------------
# _container_match (JUG vs SHAKER)
# --------------------------------------------------------------------------

def test_container_match_blocks_jug_vs_shaker():
    assert _container_match("SPICE, BASIL GROUND PLASTIC SHAKER", "SPICE, BASIL GROUND PLASTIC JUG") is False


def test_container_match_allows_same_container_or_neither():
    assert _container_match("SPICE, BASIL GROUND PLASTIC SHAKER", "SPICE, BASIL GROUND PLASTIC SHAKER (SUS)") is True
    assert _container_match("CHICKEN BREAST BONELESS", "CHICKEN BREAST BONELESS SKINLESS") is True


def test_container_match_blocks_jar_vs_bottle():
    assert _container_match("SEASONING CHINESE 5 SPICE 1 LB JAR", "SEASONING CHINESE 5 SPICE 1 LB BOTTLE") is False


def test_container_match_blocks_jug_vs_jar():
    assert _container_match("MUSTARD DIJON PLS JUG", "MUSTARD DIJON PLS JAR") is False


def test_container_match_blocks_cup_vs_can():
    assert _container_match("JUICE, APPLE 100% SS CUP SHELF STABLE", "JUICE, APPLE 100% SS CAN SHELF STABLE") is False


def test_container_match_blocks_can_vs_pouch():
    assert _container_match(
        "SAUCE, MARINARA TOMATO CHUNKY CAN SHELF STABLE", "SAUCE, MARINARA TOMATO CHUNKY POUCH SHELF STABLE"
    ) is False


# --------------------------------------------------------------------------
# _frozen_match -- FRZ addition
# --------------------------------------------------------------------------

def test_frozen_match_recognizes_frz():
    assert _frozen_match("ALPHA FOOD BURRITO PHILLY PB FRZ 12 (5 OZ)", "ALPHA FOOD BURRITO PHILLY PB 12 (5 OZ)") is False
    assert _frozen_match("AMYS BOWL BAKE KALE CHEESE FRZ 12 (8.5 OZ)", "AMYS BOWL BAKE KALE CHEESE FRZ 12 (8.5 OZ) ORG") is True


# --------------------------------------------------------------------------
# _diet_zero_match
# --------------------------------------------------------------------------

def test_diet_zero_match_blocks_diet_vs_regular():
    assert _diet_zero_match("SODA PEPSI DIET CAN 12OZ", "SODA PEPSI CAN 12OZ") is False


def test_diet_zero_match_blocks_zero_vs_regular():
    assert _diet_zero_match("Dr Pepper Zero 12 oz", "Dr Pepper 12 oz") is False


def test_diet_zero_match_blocks_diet_vs_zero():
    # Diet and zero-sugar are both distinct claims, not synonyms of each other.
    assert _diet_zero_match("SODA PEPSI DIET CAN 12OZ", "BIB Pepsi Zero 3 GL") is False


def test_diet_zero_match_allows_same_status():
    assert _diet_zero_match("SODA PEPSI DIET CAN 12OZ", "SODA PEPSI DIET CAN 12OZ (SUS)") is True
    assert _diet_zero_match("CHICKEN BREAST BONELESS", "CHICKEN BREAST BONELESS SKINLESS") is True


def test_find_and_merge_never_auto_merges_diet_mismatch(conn):
    a = _make_product(conn, "SODA PEPSI DIET CAN 12OZ")
    b = _make_product(conn, "SODA PEPSI CAN 12OZ")
    _make_purchase(conn, a, vendor="Sysco", price=10.0)
    _make_purchase(conn, b, vendor="Sysco", price=20.0)
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")

    assert stats["auto_merged"] == 0
    assert stats["candidates_created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2


# --------------------------------------------------------------------------
# _color_match
# --------------------------------------------------------------------------

def test_color_match_blocks_differing_colors():
    assert _color_match("ONION RED WHOLE PEEL 4/5-LB", "ONION YELLOW WHOLE PEEL 4/5-LB") is False
    assert _color_match("SPRINKLES BLUE", "SPRINKLES BLACK") is False


def test_color_match_recognizes_abbreviations():
    assert _color_match("GRN BANANA", "GREEN BANANA") is True


def test_color_match_allows_same_color_or_neither():
    assert _color_match("ONION RED WHOLE PEEL 4/5-LB", "ONION RED WHOLE PEEL 4/5-LB (SUS)") is True
    assert _color_match("CHICKEN BREAST BONELESS", "CHICKEN BREAST BONELESS SKINLESS") is True


def test_color_match_excludes_organic_abbreviation_collision():
    # "ORG" is "organic" in this data, not "orange" -- must not be treated
    # as a color token (verified against real data before excluding).
    assert _color_match("MLT AFRICAN NECTAR STITCH ORG", "MLT AFRICAN NECTAR STITCH") is True


def test_find_and_merge_never_auto_merges_differing_colors(conn):
    a = _make_product(conn, "ONION RED WHOLE PEEL 4/5-LB")
    b = _make_product(conn, "ONION YELLOW WHOLE PEEL 4/5-LB")
    _make_purchase(conn, a, vendor="Sysco", price=10.0)
    _make_purchase(conn, b, vendor="Sysco", price=20.0)
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")

    assert stats["auto_merged"] == 0
    assert stats["candidates_created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2


# --------------------------------------------------------------------------
# _fruit_confusion_match (pear vs peach)
# --------------------------------------------------------------------------

def test_fruit_confusion_match_blocks_pear_vs_peach():
    assert _fruit_confusion_match("PEACH, PUREE FROZEN SS CUP", "PEAR, PUREE FROZEN SS CUP") is False


def test_fruit_confusion_match_allows_same_fruit_or_neither():
    assert _fruit_confusion_match("PEACH SLI IQF", "PEACH SLICED IQF") is True
    assert _fruit_confusion_match("CHICKEN BREAST BONELESS", "CHICKEN BREAST BONELESS SKINLESS") is True


# --------------------------------------------------------------------------
# _quality_match (PREMIUM / CHOICE)
# --------------------------------------------------------------------------

def test_quality_match_blocks_premium_vs_plain():
    assert _quality_match("PORK BBQ SMKD PULLED PREMIUM", "PORK BBQ SMKD PULLED") is False


def test_quality_match_blocks_choice_vs_plain():
    assert _quality_match("BEEF SIRL TOP DENU CHOICE", "BEEF SIRL TOP DENU") is False


def test_quality_match_blocks_premium_vs_choice():
    # Not synonyms of each other -- distinct quality-tier claims.
    assert _quality_match("PEAR, BARTLETT PREMIUM 70-90 CT", "PEAR, BARTLETT CHOICE 70-90 CT") is False


def test_quality_match_allows_same_tier_or_neither():
    assert _quality_match("BEEF SIRL TOP DENU CHOICE", "BEEF SIRL TOP DENU CHOICE (SUS)") is True
    assert _quality_match("CHICKEN BREAST BONELESS", "CHICKEN BREAST BONELESS SKINLESS") is True


def test_quality_match_blocks_fancy_vs_plain():
    assert _quality_match("JUICE, TOMATO 100% FANCY CAN SHELF STABLE", "JUICE, TOMATO 100% CAN SHELF STABLE") is False


def test_find_and_merge_never_auto_merges_quality_mismatch(conn):
    a = _make_product(conn, "PORK BBQ SMKD PULLED PREMIUM")
    b = _make_product(conn, "PORK BBQ SMKD PULLED")
    _make_purchase(conn, a, vendor="Sysco", price=10.0)
    _make_purchase(conn, b, vendor="Sysco", price=20.0)
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")

    assert stats["auto_merged"] == 0
    assert stats["candidates_created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 2


# --------------------------------------------------------------------------
# _clean_for_matching -- "#" and WHL/GRTD synonyms
# --------------------------------------------------------------------------

def test_clean_for_matching_normalizes_pound_sign():
    assert _clean_for_matching("DICED RED BELL PEPPER 5#") == _clean_for_matching("DICED RED BELL PEPPER 5 LB")


def test_clean_for_matching_pound_sign_does_not_glue_to_following_letters():
    cleaned = _clean_for_matching("ONION YELLOW WHOLE PEELED 30-LB 5#BG")
    tokens = cleaned.split()
    assert "LBBG" not in tokens
    assert "BG" in tokens


def test_clean_for_matching_does_not_touch_letter_hash_number_codes():
    # "OR#"/"ITEM#" mean "number", not weight -- must not be rewritten.
    assert _clean_for_matching("ITEM# 892632 OR# 160") == "ITEM# 892632 OR# 160"


def test_clean_for_matching_normalizes_whl_and_grtd():
    assert _clean_for_matching("BEAN MUNG WHL") == _clean_for_matching("BEAN MUNG WHOLE")
    assert _clean_for_matching("CHEESE PARM GRTD PKT") == _clean_for_matching("CHEESE PARM GRATED PKT")


# --------------------------------------------------------------------------
# _dietary_claim_match (gluten-free / no MSG)
# --------------------------------------------------------------------------

def test_dietary_claim_match_blocks_gluten_free_vs_plain():
    assert _dietary_claim_match(
        "CHIP, POTATO KETTLE VINEGAR SEA SALT SS BAG SHELF STABLE",
        "CHIP, POTATO KETTLE VINEGAR SEA SALT GLUTEN-FREE SS BAG SHELF STABLE",
    ) is False


def test_dietary_claim_match_treats_hyphen_and_space_as_same():
    assert _dietary_claim_match("PASTA PENNE GLUTEN FREE", "PASTA PENNE GLUTEN-FREE") is True


def test_dietary_claim_match_blocks_no_msg_vs_plain():
    assert _dietary_claim_match(
        "DRESSING, VINAIGRETTE BALSAMIC PLASTIC JAR REF", "DRESSING, VINAIGRETTE BALSAMIC NO MSG PLASTIC JAR REF"
    ) is False


def test_dietary_claim_match_allows_same_claim_or_neither():
    assert _dietary_claim_match("PASTA PENNE GLUTEN FREE", "PASTA PENNE GLUTEN FREE (SUS)") is True
    assert _dietary_claim_match("CHICKEN BREAST BONELESS", "CHICKEN BREAST BONELESS SKINLESS") is True


# --------------------------------------------------------------------------
# re-run idempotency -- neither pass should insert duplicate candidate rows
# for a pair already known to product_match_candidates (in ANY status), since
# re-running either function is a normal, expected operation (e.g. after
# re-ingestion adds new products). Real bug: this inflated the live review
# queue twice in one session (707->1128 on a within-campus re-run, and +39 on
# a cross-campus run) before the fix.
# --------------------------------------------------------------------------

def test_find_and_merge_within_campus_rerun_creates_no_duplicate_candidates(conn):
    a = _make_product(conn, "Organic Roma Tomatoes 25lb Case")
    b = _make_product(conn, "Organic Roma Tomato 25lb Cs")
    _make_purchase(conn, a, vendor="Sysco", price=10.0)
    _make_purchase(conn, b, vendor="Sysco", price=20.0)
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats1 = find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")
    assert stats1["candidates_created"] == 1

    stats2 = find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")
    assert stats2["candidates_created"] == 0

    rows = conn.execute("SELECT COUNT(*) FROM product_match_candidates").fetchone()[0]
    assert rows == 1


def test_find_and_merge_cross_campus_rerun_creates_no_duplicate_candidates(two_campus_conn):
    conn = two_campus_conn
    a = _make_product(conn, "Organic Roma Tomatoes 25lb Case", simap_category="Tomatoes")
    b = _make_product(conn, "Organic Roma Tomato 25lb Cs", simap_category="Tomatoes")
    _make_purchase(conn, a, campus="UC Test", price=10.0)
    _make_purchase(conn, b, campus="UC Other", price=20.0)
    _make_alias(conn, a, "UC Test")
    _make_alias(conn, b, "UC Other")
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats1 = find_and_merge_cross_campus(conn, cert_lookup)
    assert stats1["candidates_created"] == 1

    stats2 = find_and_merge_cross_campus(conn, cert_lookup)
    assert stats2["candidates_created"] == 0

    rows = conn.execute("SELECT COUNT(*) FROM product_match_candidates").fetchone()[0]
    assert rows == 1


def test_find_and_merge_within_campus_does_not_requeue_reviewed_pair(conn):
    # A pair a human already approved or rejected must not get a fresh
    # 'pending' row inserted if find_and_merge_within_campus encounters it
    # again -- 'approved'/'rejected' both count as "already known", not just
    # 'pending'.
    a = _make_product(conn, "Organic Roma Tomatoes 25lb Case")
    b = _make_product(conn, "Organic Roma Tomato 25lb Cs")
    _make_purchase(conn, a, vendor="Sysco", price=10.0)
    _make_purchase(conn, b, vendor="Sysco", price=20.0)
    pid_a, pid_b = sorted((a, b))
    conn.execute(
        "INSERT INTO product_match_candidates (campus, campus_a, campus_b, product_id_a, product_id_b, "
        "match_score, status) VALUES ('UC Test', 'UC Test', 'UC Test', ?, ?, 95.0, 'rejected')",
        (pid_a, pid_b),
    )
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_within_campus(conn, "UC Test", cert_lookup, "Academic")

    assert stats["candidates_created"] == 0
    rows = conn.execute(
        "SELECT status FROM product_match_candidates WHERE product_id_a = ? AND product_id_b = ?",
        (pid_a, pid_b),
    ).fetchall()
    assert rows == [("rejected",)]  # still exactly one row, untouched


def test_find_and_merge_cross_campus_does_not_requeue_reviewed_pair(two_campus_conn):
    conn = two_campus_conn
    a = _make_product(conn, "Organic Roma Tomatoes 25lb Case", simap_category="Tomatoes")
    b = _make_product(conn, "Organic Roma Tomato 25lb Cs", simap_category="Tomatoes")
    _make_purchase(conn, a, campus="UC Test", price=10.0)
    _make_purchase(conn, b, campus="UC Other", price=20.0)
    _make_alias(conn, a, "UC Test")
    _make_alias(conn, b, "UC Other")
    pid_a, pid_b = sorted((a, b))
    conn.execute(
        "INSERT INTO product_match_candidates (campus, campus_a, campus_b, product_id_a, product_id_b, "
        "match_score, status) VALUES ('UC Test', 'UC Test', 'UC Other', ?, ?, 95.0, 'approved')",
        (pid_a, pid_b),
    )
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    stats = find_and_merge_cross_campus(conn, cert_lookup)

    assert stats["candidates_created"] == 0
    rows = conn.execute(
        "SELECT status FROM product_match_candidates WHERE product_id_a = ? AND product_id_b = ?",
        (pid_a, pid_b),
    ).fetchall()
    assert rows == [("approved",)]  # still exactly one row, untouched
