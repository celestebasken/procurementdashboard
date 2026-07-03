import sqlite3

import pandas as pd
import pytest

from lib.db import init_db, migrate_schema
from lib.simap_classification import (
    build_campus_category_lookup,
    classify_all,
    keyword_match,
    load_dictionary,
)

# Covers every raw_name/category column any campus config might look up, so
# a stub file with this header (and no data rows) is a safe no-op placeholder
# for whichever campuses a given test isn't exercising.
_UNIVERSAL_STUB_HEADER = (
    "Name,notes,ProductName,Product Name or Description ,Product Category,"
    "Product Subcategory,env_sub_category,Product Description,Food Type (WRI),Food Type (RFC)\n"
)


def _write_stub(tmp_path, filename):
    # UCSC skips row 1 as junk before its real header -- needs an extra
    # leading line so the universal header still lands correctly.
    content = ("junk\n" + _UNIVERSAL_STUB_HEADER) if filename == "UCSC_FY25.csv" else _UNIVERSAL_STUB_HEADER
    (tmp_path / filename).write_text(content)


def test_load_dictionary_has_only_valid_simap_targets():
    dictionary = load_dictionary()
    assert len(dictionary) > 200
    simap_cats = set(pd.read_csv("reference/simap_categories.csv")["Food Category"].str.strip())
    assert all(v in simap_cats for v in dictionary.values())


def test_load_dictionary_known_entries():
    dictionary = load_dictionary()
    assert dictionary["Beef"] == "Beef & buffalo meat"
    assert dictionary["Chicken"] == "Poultry (chicken, turkey)"
    assert dictionary["Milk"] == "Milk (cow's milk)"


# --------------------------------------------------------------------------
# keyword_match
# --------------------------------------------------------------------------

def test_keyword_match_finds_category_in_product_name():
    d = {"Beef": "Beef & buffalo meat", "Onion": "Onions and Leeks"}
    assert keyword_match("BEEF, GROUND 80/20 BRICK FROZEN", d) == "Beef & buffalo meat"
    assert keyword_match("ONION RED JUMBO 5-LB", d) == "Onions and Leeks"


def test_keyword_match_breaks_length_ties_by_earliest_position():
    # "Chicken" and "Buffalo" are both 7 characters -- the primary
    # ingredient (mentioned first) should win over a later flavor/style
    # descriptor of the same keyword length.
    d = {"Chicken": "Poultry (chicken, turkey)", "Buffalo": "Stimulants & Spices (misc.)"}
    assert keyword_match("CHICKEN BREAST BUFFALO BITES", d) == "Poultry (chicken, turkey)"
    assert keyword_match("BUFFALO SAUCE WITH CHICKEN", d) == "Stimulants & Spices (misc.)"


def test_keyword_match_sugar_snap_protected_from_bare_sugar():
    # A longer, more specific compound ("Sugar Snap") must win over the
    # bare "Sugar" keyword it contains, or "PEA SUGAR SNAP FRESH" would
    # misclassify as a sweetener instead of Peas.
    dictionary = load_dictionary()
    assert keyword_match("PEA SUGAR SNAP FRESH", dictionary) == "Peas"
    assert keyword_match("SUGAR BROWN LIGHT 2LB", dictionary) == "Sugars and sweeteners"


def test_keyword_match_vegan_flags_plant_based_items():
    d = {"Vegan": "Soybeans/Tofu"}
    assert keyword_match("VEGAN PROTEIN SNACK BOWL", d) == "Soybeans/Tofu"


def test_keyword_match_hamburger_bun_is_bread_not_meat_substitute():
    # A vegan hamburger BUN is correctly bread, not a meat substitute --
    # "Hamburger" is the longer, more specific keyword vs. "Vegan".
    dictionary = load_dictionary()
    assert keyword_match("BUN HAMBURGER VEGAN GF", dictionary) == "Wheat/Rye (Bread, pasta, baked goods)"


def test_keyword_match_respects_word_boundaries():
    d = {"Corn": "Corn (Maize)"}
    # "Cornstarch" contains "Corn" as a substring but is not the word "Corn"
    assert keyword_match("CORNSTARCH THICKENER 5LB", d) is None
    assert keyword_match("CORN SWEET FROZEN 5LB", d) == "Corn (Maize)"


def test_keyword_match_prefers_longest_keyword_on_multiple_matches():
    d = {"Baked Goods": "Wheat/Rye (Bread, pasta, baked goods)", "Goods": "Vegetables (misc.)"}
    assert keyword_match("ASSORTED BAKED GOODS TRAY", d) == "Wheat/Rye (Bread, pasta, baked goods)"


def test_keyword_match_returns_none_when_nothing_matches():
    d = {"Beef": "Beef & buffalo meat"}
    assert keyword_match("NAPKIN DISPENSER WHITE", d) is None


# --------------------------------------------------------------------------
# build_campus_category_lookup
# --------------------------------------------------------------------------

def test_build_campus_category_lookup_prefers_finer_column(tmp_path):
    csv = tmp_path / "UCD_FY25.csv"
    csv.write_text(
        "Name,Product Category,Product Subcategory\n"
        "Cheddar Block,Dairy,Cheese\n"
        "Mystery Item,Dry Goods,\n"
    )
    lookup = build_campus_category_lookup("UCD", tmp_path)
    assert lookup["Cheddar Block"] == "Cheese"  # finer Subcategory preferred over coarser Category
    assert lookup["Mystery Item"] == "Dry Goods"  # falls back to Category when Subcategory blank


def test_build_campus_category_lookup_none_for_campuses_without_category_field(tmp_path):
    csv = tmp_path / "UCB_FY25.csv"
    csv.write_text("Product Name or Description ,Distributor\nWidget,Sysco\n")
    assert build_campus_category_lookup("UCB", tmp_path) == {}


# --------------------------------------------------------------------------
# classify_all
# --------------------------------------------------------------------------

@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    migrate_schema(conn)
    conn.execute(
        "INSERT INTO campuses (campus, primary_standard, campus_type, abbreviation) "
        "VALUES ('UC Davis Health', 'Practice Greenhealth', 'Health', 'UCD_H')"
    )
    conn.execute(
        "INSERT INTO campuses (campus, primary_standard, campus_type, abbreviation) "
        "VALUES ('UC Berkeley', 'AASHE STARS', 'Academic', 'UCB')"
    )
    conn.commit()
    yield conn
    conn.close()


def _add_product(conn, name, campus, raw_name):
    cur = conn.execute("INSERT INTO products (canonical_name) VALUES (?)", (name,))
    product_id = cur.lastrowid
    conn.execute(
        "INSERT INTO product_aliases (raw_name, campus, product_id, match_confidence, human_confirmed) "
        "VALUES (?, ?, ?, 1.0, 0)",
        (raw_name, campus, product_id),
    )
    conn.commit()
    return product_id


def test_classify_all_uses_campus_category_when_available(conn, tmp_path):
    (tmp_path / "UCD_H_FY25.csv").write_text("Name,env_sub_category\nCheddar Cheese Block,Cheese\n")
    (tmp_path / "UCB_FY25.csv").write_text("Product Name or Description ,x\n,\n")
    for other in ["UCD_FY25.csv", "UCLA_H_FY25.csv", "UCR_FY25.csv", "UCSC_FY25.csv", "UCSD_H_FY25.csv"]:
        _write_stub(tmp_path, other)

    a = _add_product(conn, "Cheddar Cheese Block", "UC Davis Health", "Cheddar Cheese Block")
    counts = classify_all(conn, tmp_path)

    row = conn.execute(
        "SELECT simap_category, simap_classification_source FROM products WHERE product_id = ?", (a,)
    ).fetchone()
    assert row == ("Cheese", "campus_category")
    assert counts["campus_category"] == 1


def test_classify_all_falls_back_to_keyword_match(conn, tmp_path):
    # UCB has no category column at all -- must fall back to the product name.
    (tmp_path / "UCB_FY25.csv").write_text(
        "Product Name or Description ,Distributor\nBeef Ground 80/20,Sysco\n"
    )
    for other in ["UCD_FY25.csv", "UCD_H_FY25.csv", "UCLA_H_FY25.csv", "UCR_FY25.csv", "UCSC_FY25.csv", "UCSD_H_FY25.csv"]:
        _write_stub(tmp_path, other)

    a = _add_product(conn, "Beef Ground 80/20", "UC Berkeley", "Beef Ground 80/20")
    counts = classify_all(conn, tmp_path)

    row = conn.execute(
        "SELECT simap_category, simap_classification_source FROM products WHERE product_id = ?", (a,)
    ).fetchone()
    assert row == ("Beef & buffalo meat", "keyword_match")
    assert counts["keyword_match"] == 1


def test_classify_all_marks_unclassified_when_nothing_matches(conn, tmp_path):
    (tmp_path / "UCB_FY25.csv").write_text(
        "Product Name or Description ,Distributor\nMystery Item XYZ,Sysco\n"
    )
    for other in ["UCD_FY25.csv", "UCD_H_FY25.csv", "UCLA_H_FY25.csv", "UCR_FY25.csv", "UCSC_FY25.csv", "UCSD_H_FY25.csv"]:
        _write_stub(tmp_path, other)

    a = _add_product(conn, "Mystery Item XYZ", "UC Berkeley", "Mystery Item XYZ")
    counts = classify_all(conn, tmp_path)

    row = conn.execute(
        "SELECT simap_category, simap_classification_source FROM products WHERE product_id = ?", (a,)
    ).fetchone()
    assert row == (None, "unclassified")
    assert counts["unclassified"] == 1


def test_classify_all_merged_product_checks_all_aliases(conn, tmp_path):
    # A product with two aliases (post entity-matching merge) where only the
    # second alias's raw_name has a matching campus category -- must still
    # find it rather than giving up after the first alias.
    (tmp_path / "UCD_H_FY25.csv").write_text(
        "Name,env_sub_category\nUnclassifiable Alias,\nCheddar Cheese Block,Cheese\n"
    )
    for other in ["UCB_FY25.csv", "UCD_FY25.csv", "UCLA_H_FY25.csv", "UCR_FY25.csv", "UCSC_FY25.csv", "UCSD_H_FY25.csv"]:
        _write_stub(tmp_path, other)

    cur = conn.execute("INSERT INTO products (canonical_name) VALUES ('Cheddar Cheese Block')")
    product_id = cur.lastrowid
    conn.execute(
        "INSERT INTO product_aliases (raw_name, campus, product_id, match_confidence, human_confirmed) "
        "VALUES ('Unclassifiable Alias', 'UC Davis Health', ?, 1.0, 0)",
        (product_id,),
    )
    conn.execute(
        "INSERT INTO product_aliases (raw_name, campus, product_id, match_confidence, human_confirmed) "
        "VALUES ('Cheddar Cheese Block', 'UC Davis Health', ?, 1.0, 0)",
        (product_id,),
    )
    conn.commit()

    classify_all(conn, tmp_path)
    row = conn.execute(
        "SELECT simap_category, simap_classification_source FROM products WHERE product_id = ?", (product_id,)
    ).fetchone()
    assert row == ("Cheese", "campus_category")
