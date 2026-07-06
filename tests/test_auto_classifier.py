import sqlite3

import pandas as pd
import pytest

from lib.auto_classifier import (
    CONFIDENT_MATCH,
    NEEDS_REVIEW,
    NO_MATCH,
    load_match_corpus,
    match_uploaded_products,
)
from lib.db import init_db

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
    conn.commit()
    yield conn
    conn.close()


def _make_product(conn, canonical_name, simap_category, certs, validated_sustainable_yn, aliases):
    cur = conn.execute(
        "INSERT INTO products (canonical_name, simap_category, sustainability_certifications, "
        "sustainable_yn, validated_sustainable_yn) VALUES (?, ?, ?, 'Y', ?)",
        (canonical_name, simap_category, certs, validated_sustainable_yn),
    )
    product_id = cur.lastrowid
    for alias in aliases:
        conn.execute(
            "INSERT INTO product_aliases (raw_name, campus, product_id, match_confidence, human_confirmed) "
            "VALUES (?, ?, ?, 100.0, 1)",
            (alias, CAMPUS, product_id),
        )
    conn.commit()
    return product_id


def _corpus_from_rows(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# load_match_corpus
# --------------------------------------------------------------------------

def test_load_match_corpus_joins_aliases_to_product_fields(conn):
    _make_product(
        conn,
        "Organic Whole Milk Gallon",
        "Milk (cow's milk)",
        "Organic",
        "Y",
        aliases=["ORGANIC WHOLE MILK GALLON", "ORG WHOLE MILK GAL"],
    )

    corpus = load_match_corpus(conn)

    assert len(corpus) == 2
    assert set(corpus["raw_name"]) == {"ORGANIC WHOLE MILK GALLON", "ORG WHOLE MILK GAL"}
    assert (corpus["simap_category"] == "Milk (cow's milk)").all()
    assert (corpus["sustainability_certifications"] == "Organic").all()


# --------------------------------------------------------------------------
# match_uploaded_products
# --------------------------------------------------------------------------

def test_match_uploaded_products_exact_match_is_confident():
    corpus = _corpus_from_rows(
        [
            {
                "raw_name": "ORGANIC BABY SPINACH BAG 5 LB",
                "product_id": 1,
                "canonical_name": "Organic Baby Spinach Bag 5 LB",
                "simap_category": "Vegetables (misc.)",
                "sustainability_certifications": "Organic",
                "validated_sustainable_yn": "Y",
            }
        ]
    )

    result = match_uploaded_products(["ORGANIC BABY SPINACH BAG 5 LB"], corpus)
    row = result.iloc[0]

    assert row["match_tier"] == CONFIDENT_MATCH
    assert row["matched_name"] == "Organic Baby Spinach Bag 5 LB"
    assert row["simap_category"] == "Vegetables (misc.)"
    assert row["sustainability_certifications"] == "Organic"
    assert row["validated_sustainable_yn"] == "Y"
    assert row["match_score"] == pytest.approx(100.0)


def test_match_uploaded_products_needs_review_tier_for_moderate_score():
    # Verified empirically: "ORGANIC WHOLE MILK GALLON" vs "...HALF GALLON"
    # scores 90.91 (>= REVIEW_THRESHOLD=90, < CONFIDENT_THRESHOLD=97) and
    # clears every hard gate -- a real "probably the same, but check" case.
    corpus = _corpus_from_rows(
        [
            {
                "raw_name": "ORGANIC WHOLE MILK HALF GALLON",
                "product_id": 2,
                "canonical_name": "Organic Whole Milk Half Gallon",
                "simap_category": "Milk (cow's milk)",
                "sustainability_certifications": "Organic",
                "validated_sustainable_yn": "Y",
            }
        ]
    )

    result = match_uploaded_products(["ORGANIC WHOLE MILK GALLON"], corpus)
    row = result.iloc[0]

    assert row["match_tier"] == NEEDS_REVIEW
    assert row["matched_name"] == "Organic Whole Milk Half Gallon"
    assert 90 <= row["match_score"] < 97


def test_match_uploaded_products_blocked_by_numbers_gate_despite_high_text_similarity():
    # Verified empirically: these score 97.56 (would clear CONFIDENT_THRESHOLD
    # on text alone) but differ in pack size (25-LB vs 5-LB) -- _all_gates_match
    # must block this regardless of how similar the text looks.
    corpus = _corpus_from_rows(
        [
            {
                "raw_name": "ONION RED JUMBO 5-LB",
                "product_id": 3,
                "canonical_name": "Onion Red Jumbo 5-LB",
                "simap_category": "Onions and Leeks",
                "sustainability_certifications": None,
                "validated_sustainable_yn": "N",
            }
        ]
    )

    result = match_uploaded_products(["ONION RED JUMBO 25-LB"], corpus)
    row = result.iloc[0]

    assert row["match_tier"] == NO_MATCH
    assert row["matched_name"] is None
    assert row["simap_category"] is None


def test_match_uploaded_products_unrelated_name_is_no_match():
    corpus = _corpus_from_rows(
        [
            {
                "raw_name": "ORGANIC BABY SPINACH BAG 5 LB",
                "product_id": 1,
                "canonical_name": "Organic Baby Spinach Bag 5 LB",
                "simap_category": "Vegetables (misc.)",
                "sustainability_certifications": "Organic",
                "validated_sustainable_yn": "Y",
            }
        ]
    )

    result = match_uploaded_products(["FROZEN CHICKEN WINGS BULK HALAL 40 LB"], corpus)
    row = result.iloc[0]

    assert row["match_tier"] == NO_MATCH
    assert row["matched_name"] is None
    assert row["match_score"] is None


def test_match_uploaded_products_empty_corpus_does_not_crash():
    result = match_uploaded_products(["ANYTHING AT ALL"], pd.DataFrame())
    row = result.iloc[0]

    assert row["match_tier"] == NO_MATCH
    assert row["matched_name"] is None


def test_match_uploaded_products_returns_one_row_per_upload_in_order():
    corpus = _corpus_from_rows(
        [
            {
                "raw_name": "ORGANIC BABY SPINACH BAG 5 LB",
                "product_id": 1,
                "canonical_name": "Organic Baby Spinach Bag 5 LB",
                "simap_category": "Vegetables (misc.)",
                "sustainability_certifications": "Organic",
                "validated_sustainable_yn": "Y",
            }
        ]
    )

    result = match_uploaded_products(
        ["ORGANIC BABY SPINACH BAG 5 LB", "SOMETHING ELSE ENTIRELY", "ORGANIC BABY SPINACH BAG 5 LB"], corpus
    )

    assert list(result["uploaded_name"]) == [
        "ORGANIC BABY SPINACH BAG 5 LB",
        "SOMETHING ELSE ENTIRELY",
        "ORGANIC BABY SPINACH BAG 5 LB",
    ]
    assert list(result["match_tier"]) == [CONFIDENT_MATCH, NO_MATCH, CONFIDENT_MATCH]
