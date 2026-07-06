import sqlite3

import pytest

from lib.brand_cleanup import (
    _dedup_key,
    apply_brand_mapping,
    build_brand_mapping,
    normalize_brand,
)

# --------------------------------------------------------------------------
# normalize_brand
# --------------------------------------------------------------------------


def test_normalize_brand_strips_leading_dash_artifact():
    assert normalize_brand("- Amy's") == "Amy's"
    assert normalize_brand("-Annies") == "Annies"
    assert normalize_brand("--Bobs red mill") == "Bobs red mill"


def test_normalize_brand_placeholder_becomes_none():
    assert normalize_brand("-") is None
    assert normalize_brand("- ") is None
    assert normalize_brand("") is None
    assert normalize_brand("   ") is None
    assert normalize_brand(None) is None


def test_normalize_brand_normalizes_curly_quotes_and_backtick():
    assert normalize_brand("AMY`S") == "AMY'S"
    assert normalize_brand("Amy’s") == "Amy's"


def test_normalize_brand_collapses_whitespace():
    assert normalize_brand("  Bob's   Red  Mill  ") == "Bob's Red Mill"


def test_normalize_brand_leaves_a_clean_value_alone():
    assert normalize_brand("Sysco") == "Sysco"


# --------------------------------------------------------------------------
# _dedup_key
# --------------------------------------------------------------------------


def test_dedup_key_groups_the_real_amys_example():
    variants = ["AMYS", "Amy's", "amys", "AMY'S"]
    keys = {_dedup_key(v) for v in variants}
    assert len(keys) == 1


def test_dedup_key_distinguishes_different_brands():
    assert _dedup_key("Sysco") != _dedup_key("Daisy")


# --------------------------------------------------------------------------
# build_brand_mapping / apply_brand_mapping
# --------------------------------------------------------------------------


@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE purchases (purchase_id INTEGER PRIMARY KEY, brand TEXT, total_price REAL)"
    )
    yield conn
    conn.close()


def _insert(conn, brand, n=1, price=10.0):
    for _ in range(n):
        conn.execute("INSERT INTO purchases (brand, total_price) VALUES (?, ?)", (brand, price))
    conn.commit()


def test_build_brand_mapping_collapses_amys_variants(conn):
    _insert(conn, "AMYS", n=1)
    _insert(conn, "AMY`S", n=1)
    _insert(conn, "Amy's", n=5)  # most common exact spelling -- should win as canonical
    _insert(conn, "- Amy's", n=1)
    _insert(conn, "amys", n=1)

    mapping_df, _ = build_brand_mapping(conn)
    canonicals = set(mapping_df["canonical_brand"])
    assert canonicals == {"Amy's"}
    assert set(mapping_df["merge_tier"]) == {"unchanged", "exact_normalize"}
    unchanged_row = mapping_df[mapping_df["raw_brand"] == "Amy's"].iloc[0]
    assert unchanged_row["merge_tier"] == "unchanged"


def test_build_brand_mapping_prefers_titlecase_over_allcaps_on_tie(conn):
    _insert(conn, "SYSCO", n=3)
    _insert(conn, "Sysco", n=3)  # tied row count -- mixed case should win the tie-break

    mapping_df, _ = build_brand_mapping(conn)
    assert set(mapping_df["canonical_brand"]) == {"Sysco"}


def test_build_brand_mapping_clears_dash_placeholder_to_none(conn):
    _insert(conn, "-", n=5)
    _insert(conn, "Real Brand", n=1)

    mapping_df, _ = build_brand_mapping(conn)
    placeholder_row = mapping_df[mapping_df["raw_brand"] == "-"].iloc[0]
    assert placeholder_row["canonical_brand"] is None
    assert placeholder_row["merge_tier"] == "clear_placeholder"


def test_build_brand_mapping_does_not_merge_unrelated_brands(conn):
    _insert(conn, "Daisy", n=2)
    _insert(conn, "Diestel", n=2)

    mapping_df, fuzzy_df = build_brand_mapping(conn)
    assert set(mapping_df["canonical_brand"]) == {"Daisy", "Diestel"}
    assert fuzzy_df.empty


def test_build_brand_mapping_fuzzy_tier_proposes_prefix_match_not_auto_applied(conn):
    _insert(conn, "Amy's", n=5)
    _insert(conn, "Amy's Organic Vegetable", n=1)

    mapping_df, fuzzy_df = build_brand_mapping(conn)
    # deterministic tier keeps them distinct -- the dedup key doesn't merge
    # "Amy's Organic Vegetable" into "Amy's" (extra words survive stripping).
    assert set(mapping_df["canonical_brand"]) == {"Amy's", "Amy's Organic Vegetable"}
    # ...but the fuzzy tier surfaces it as a candidate for manual review.
    assert len(fuzzy_df) == 1
    candidate = fuzzy_df.iloc[0]
    assert candidate["raw_brand"] == "Amy's Organic Vegetable"
    assert candidate["proposed_canonical"] == "Amy's"
    assert candidate["score"] >= 90.0


def test_apply_brand_mapping_updates_purchases_and_confirmed_fuzzy_pairs(conn):
    _insert(conn, "AMYS", n=2)
    _insert(conn, "Amy's", n=5)
    _insert(conn, "Amy's Organic Vegetable", n=1)
    _insert(conn, "-", n=3)

    mapping_df, fuzzy_df = build_brand_mapping(conn)
    updated = apply_brand_mapping(
        conn, mapping_df, confirmed_fuzzy_pairs=[("Amy's Organic Vegetable", "Amy's")]
    )
    assert updated > 0

    remaining_brands = {r[0] for r in conn.execute("SELECT DISTINCT brand FROM purchases").fetchall()}
    assert remaining_brands == {"Amy's", None}
    amys_count = conn.execute("SELECT COUNT(*) FROM purchases WHERE brand = \"Amy's\"").fetchone()[0]
    assert amys_count == 8  # 2 (AMYS) + 5 (Amy's) + 1 (Amy's Organic Vegetable, confirmed)
    null_count = conn.execute("SELECT COUNT(*) FROM purchases WHERE brand IS NULL").fetchone()[0]
    assert null_count == 3


def test_apply_brand_mapping_without_confirmed_fuzzy_pairs_leaves_them_unmerged(conn):
    _insert(conn, "Amy's", n=5)
    _insert(conn, "Amy's Organic Vegetable", n=1)

    mapping_df, _ = build_brand_mapping(conn)
    apply_brand_mapping(conn, mapping_df)  # no confirmed_fuzzy_pairs passed

    remaining_brands = {r[0] for r in conn.execute("SELECT DISTINCT brand FROM purchases").fetchall()}
    assert remaining_brands == {"Amy's", "Amy's Organic Vegetable"}
