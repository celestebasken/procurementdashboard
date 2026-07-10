import sqlite3

import pandas as pd
import pytest

from lib.db import init_db, migrate_schema
from lib.simap_classification import (
    _apply_flavor_confusion_override,
    _apply_fresh_bean_override,
    _apply_plant_based_override,
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


def test_keyword_match_handles_irregular_consonant_y_plural():
    # "Berry"/"Cherry" -> "Berries"/"Cherries", not "Berrys"/"Cherrys" --
    # the regular "s?" suffix alone misses this. Real products recovered:
    # "CHERRIES MARASCHINO...", "OG1 KAMUT BERRIES", "SNR BLACKBERRIES...".
    d = {"Berry": "Berries", "Cherry": "Fruits (misc.)"}
    assert keyword_match("OG1 KAMUT BERRIES", d) == "Berries"
    assert keyword_match("CHERRIES MARASCHINO WITH STEM", d) == "Fruits (misc.)"
    assert keyword_match("SINGLE BERRY PARFAIT", d) == "Berries"  # singular still matches


def test_keyword_match_does_not_pluralize_vowel_y_as_ies():
    # "Soy" ends in vowel+y -- must NOT become "Soies"; only the regular
    # optional "s" suffix applies here.
    d = {"Soy": "Soybeans/Tofu"}
    assert keyword_match("SOY SAUCE LOW SODIUM", d) == "Soybeans/Tofu"
    assert keyword_match("SOIES FABRIC SAMPLE", d) is None


def test_keyword_match_handles_o_ending_irregular_plural():
    # "Tomato"/"Potato" -> "Tomatoes"/"Potatoes", not "Tomatos"/"Potatos" --
    # no reliable rule distinguishes "+s" vs "+es" for o-ending words, so
    # both forms are accepted.
    d = {"Tomato": "Tomatoes", "Potato": "Potatoes"}
    assert keyword_match("SNR TOMATOES DICED 1/4 2/5LB", d) == "Tomatoes"
    assert keyword_match("POTATOES RED B 50 LBS", d) == "Potatoes"
    assert keyword_match("TOMATO PASTE FANCY CA", d) == "Tomatoes"  # singular still matches


# --------------------------------------------------------------------------
# _apply_plant_based_override
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,category,expected",
    [
        # "X milk"/"milk X" in either order is unambiguous regardless of
        # product form or which tier produced the buggy original category.
        ("MILK ALMOND BARISTA BLEND CALIFIA (SUS)", "Milk (cow's milk)", ("Almond milk", "plant_based_override")),
        ("MILK OAT BARISTA BLEND", "Milk (cow's milk)", ("Oat milk", "plant_based_override")),
        ("MILK SOY BULK", "Milk (cow's milk)", ("Soy milk", "plant_based_override")),
        ("NON-DAIRY MILK RICE ORIGINAL ORG", "Milk (cow's milk)", ("Rice milk", "plant_based_override")),
        ("MY MOCHI ICE CREAM SLTD CARAL OAT MILK 12 (6 CT)", "Ice cream", ("Oat milk", "plant_based_override")),
        # coconut/pea have no matching SIMAP category -- unclassified, not guessed.
        ("MILK COCONUT BARISTA (SUS)", "Milk (cow's milk)", (None, "unclassified")),
        ("MILK PEAMILK CHOC BULK UPROOT", "Milk (cow's milk)", (None, "unclassified")),
        # Named plant type + explicit non-dairy marker, no "milk" word at all.
        ("CREAMER SILK VANILLA SOY DAIRY FREE", "Cream", ("Soy milk", "plant_based_override")),
        ("VEGAN YOGURT OAT PLAIN", "Yogurt", ("Oat milk", "plant_based_override")),
        # Two different plant types named -- ambiguous, no defensible single answer.
        ("NON-DAIRY CREAMER COCONUT ALMOND CALIFA", "Cream", (None, "unclassified")),
        # Generic vegan/non-dairy marker, no named plant type: butter is the
        # one confirmed dominant-ingredient exception (oil-based).
        ("BUTTER SOLID UNSALTED VEGAN (SUS)", "Milk (cow's milk)", ("Vegetable oils", "plant_based_override")),
        ("BUTTER SOLID STICK VEGAN 18/16OZ", "Butter", ("Vegetable oils", "plant_based_override")),
        # Generic marker, no named type, not butter -- no defensible category.
        ("CREAM WHIPPING NON DAIRY (SUS)", "Cream", (None, "unclassified")),
        ("NON-DAIRY CHEESE CREAM ALT CHIVE", "Cheese", (None, "unclassified")),
    ],
)
def test_apply_plant_based_override_corrects_known_cases(name, category, expected):
    assert _apply_plant_based_override(name, category) == expected


@pytest.mark.parametrize(
    "name,category",
    [
        # Real dairy products where almond/coconut/rice are a FLAVOR, not a
        # substitute base -- no "milk" adjacency and no vegan/non-dairy
        # marker, so these must NOT be swept into a plant-milk category.
        ("ICE CREAM BAR HAAGEN-DAZS VANILLA ALMOND", "Ice cream"),
        ("CHOBANI LOW FAT COCONUT GREEK YOGURT", "Yogurt"),
        ("YOGHURT NOOSA COCONUT 8OZ", "Yogurt"),
        ("LUNDBERG WHITE CHEDDAR RICE CAKE MINIS", "Cheese"),
        ("AMY'S MAC & CHEESE, RICE GF", "Cheese"),
        ("BARNEY BUTTER SMOOTH ALMOND BUTTER 6 (10 OZ)", "Butter"),
        ("DESSERT BAR MANGO COCONUT CRUNCH", "Ice cream"),
        # A normal dairy product with no plant-based signal at all.
        ("Cheddar Cheese Block", "Cheese"),
    ],
)
def test_apply_plant_based_override_leaves_real_dairy_products_alone(name, category):
    assert _apply_plant_based_override(name, category) is None


@pytest.mark.parametrize(
    "name,category",
    [
        # "Butter" as a flavor compound ("cookie butter" is a spread, not
        # dairy butter) must not trigger the Vegetable oils override just
        # because a generic non-dairy marker is also present -- found via
        # real-data audit against the live db before this fix was applied.
        ("ICE CREAM, COOKIE BUTTER NON-DAIRY FROZEN CUP", "Ice cream"),
        # Same flavor-compound problem, but the current category is Cream
        # rather than Ice cream -- must stay unclassified either way, not
        # be swept into Vegetable oils just because "butter" appears.
        ("PEANUT BUTTER NON-DAIRY FROSTING", "Cream"),
    ],
)
def test_apply_plant_based_override_vegetable_oils_only_for_butter_and_milk_categories(name, category):
    # Still an override (unclassified, since there's a generic non-dairy
    # marker and no named plant type) -- just not Vegetable oils, since
    # these products were never really Butter/Milk to begin with.
    assert _apply_plant_based_override(name, category) == (None, "unclassified")


def test_apply_plant_based_override_ignores_categories_outside_the_dairy_set():
    # Even an explicit "vegan" marker shouldn't trigger an override for a
    # category this fix was never scoped to touch.
    assert _apply_plant_based_override("VEGAN BEEF STYLE CRUMBLES", "Beef & buffalo meat") is None
    assert _apply_plant_based_override("Mystery Item", None) is None


def test_classify_all_applies_plant_based_override_end_to_end(conn, tmp_path):
    # UC Davis's own Product Category column says "Milk" for this row (the
    # real confirmed bug) -- classify_all must still correct it to Oat milk.
    (tmp_path / "UCD_FY25.csv").write_text(
        "Name,Product Category,Product Subcategory\nMILK OAT BARISTA BLEND,Milk,\n"
    )
    for other in ["UCB_FY25.csv", "UCD_H_FY25.csv", "UCLA_H_FY25.csv", "UCR_FY25.csv", "UCSC_FY25.csv", "UCSD_H_FY25.csv"]:
        _write_stub(tmp_path, other)

    a = _add_product(conn, "MILK OAT BARISTA BLEND", "UC Berkeley", "MILK OAT BARISTA BLEND")
    conn.execute(
        "INSERT INTO campuses (campus, primary_standard, campus_type, abbreviation) "
        "VALUES ('UC Davis', 'AASHE STARS', 'Academic', 'UCD')"
    )
    conn.execute("UPDATE product_aliases SET campus = 'UC Davis' WHERE product_id = ?", (a,))
    conn.commit()

    counts = classify_all(conn, tmp_path)

    row = conn.execute(
        "SELECT simap_category, simap_classification_source FROM products WHERE product_id = ?", (a,)
    ).fetchone()
    assert row == ("Oat milk", "plant_based_override")
    assert counts["plant_based_override"] == 1


# --------------------------------------------------------------------------
# _apply_flavor_confusion_override
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,category",
    [
        # Real audit examples: a fruit-name keyword outlengths the actual
        # product-type word, misclassifying a beverage/candy/snack/dressing
        # as the fruit itself. All keyword_match-sourced.
        ("SODA DR PEPPER BLACKBERRY CAN 12OZ", "Berries"),
        ("GATORLYTE MIXED BERRY 20OZ", "Fruits (misc.)"),
        ("GTS SYNERGY KOMBUCHA RASPBERRY CHIA 12 (16 OZ)", "Berries"),
        ("CANDY SOUR PUNCH STRAWS BLUE RASPBERRY", "Berries"),
        ("BIB Brisk Tea Raspberry 5 GL", "Berries"),
        ("DRESSING ANNIES RASPBERRY VIN LF 8Z", "Berries"),
        ("BIB Dole Orange Juice 3 GL", "Citrus Fruit"),
        ("BANANA MUFFIN", "Fruits (misc.)"),
        ("YOGURT RASPBERRY GREEK NF", "Berries"),
    ],
)
def test_apply_flavor_confusion_override_downgrades_known_false_positives(name, category):
    dictionary = load_dictionary()
    assert _apply_flavor_confusion_override(name, category, "keyword_match", dictionary) == (None, "unclassified")


@pytest.mark.parametrize(
    "name,category",
    [
        # Genuine fruit/berry products -- no non-produce signal word present.
        ("BLACKBERRY FRESH", "Berries"),
        ("BLACKBERRY IQF", "Berries"),
        ("CRANBERRIES 24/12 OZ (FRESH)", "Berries"),
        ("BLUEBERRY, DMSTC WHL IQF FZN", "Berries"),
    ],
)
def test_apply_flavor_confusion_override_leaves_real_produce_alone(name, category):
    dictionary = load_dictionary()
    assert _apply_flavor_confusion_override(name, category, "keyword_match", dictionary) is None


def test_apply_flavor_confusion_override_does_not_touch_reliable_keywords():
    # "Chocolate"/"Cookie"/"Candy" are themselves reliable category-defining
    # keywords -- a real chocolate bar IS Cocoa, a real cookie IS
    # Wheat/Rye. This override must not touch categories those keywords
    # produce, even though "candy"/"cookie" are also in the exclusion word
    # list used to detect fruit-flavor false positives.
    dictionary = load_dictionary()
    assert _apply_flavor_confusion_override("CANDY BAR MILK CHOCOLATE", "Cocoa", "keyword_match", dictionary) is None
    assert (
        _apply_flavor_confusion_override(
            "COOKIE CHOCOLATE CHIP", "Wheat/Rye (Bread, pasta, baked goods)", "keyword_match", dictionary
        )
        is None
    )


def test_apply_flavor_confusion_override_ignores_categories_outside_scope():
    dictionary = load_dictionary()
    assert _apply_flavor_confusion_override("SODA GINGER ALE 12OZ", "Liquids", "keyword_match", dictionary) is None
    assert (
        _apply_flavor_confusion_override(
            "CHICKEN SALAD SANDWICH", "Poultry (chicken, turkey)", "keyword_match", dictionary
        )
        is None
    )


def test_apply_flavor_confusion_override_never_fires_for_campus_category_source():
    # The bug lives in keyword_match's own tie-break, not in campus-
    # reported categories -- confirmed via live-db audit that 48 real
    # campus_category-sourced products (genuine dominant-ingredient calls
    # the campus already made correctly, e.g. diced peach canned "in
    # juice", a grape "jam") would be wrongly downgraded if this override
    # applied regardless of source. "JUICE ORANGE 100% CRTN" is real,
    # correctly campus_category-classified pure orange juice -- must stay
    # untouched even though "Orange" + "Juice" would otherwise trigger the
    # override.
    dictionary = load_dictionary()
    assert _apply_flavor_confusion_override("JUICE ORANGE 100% CRTN", "Citrus Fruit", "campus_category", dictionary) is None
    assert (
        _apply_flavor_confusion_override(
            "PEACH, DICED IN JUICE SS PLASTIC CUP", "Fruits (misc.)", "campus_category", dictionary
        )
        is None
    )
    assert (
        _apply_flavor_confusion_override(
            "JAM, GRAPE SS CUP SHELF STABLE", "Sugars and sweeteners", "campus_category", dictionary
        )
        is None
    )


# --------------------------------------------------------------------------
# _apply_fresh_bean_override
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name",
    [
        "HARICOT BEANS FRENCH 10/1 LB (SUS)",  # the exact reported example
        "GREEN BEANS BLUE LAKE 25 LB",
        "TRIMMED GREEN BEANS *CALIFORNIA*",
        "TRIMMED YELLOW WAX BEANS 5#",
        "TRIMMED ROMANO BEANS 5#",
        "BLUELAKE BEANS POUND",
        "BEAN GREEN WHL HARICOT VERT",
    ],
)
def test_apply_fresh_bean_override_reroutes_string_bean_varieties(name):
    assert _apply_fresh_bean_override(name, "Beans and pulses (dried)") == ("Vegetables (misc.)", "keyword_match")


@pytest.mark.parametrize(
    "name",
    [
        "BEAN GARBANZO DRIED",
        "BEAN, PINTO WASHED DRIED SHELF STABLE BAG",
        "DRIED BLACK TURTLE BEANS 25# (ORGANIC)",
        "CANNED KIDNEY BEANS 6/#10",
        # Reversed word order ("BEAN, GREEN" not "GREEN BEAN") isn't
        # caught by this regex -- a known, narrow gap (same class of
        # limitation as the milk/oat reversed-order case elsewhere in this
        # file), not attempted here since it's a small minority of cases.
        "BEAN, GREEN CHOPPED FROZEN SS CUP",
    ],
)
def test_apply_fresh_bean_override_leaves_dried_pulse_varieties_alone(name):
    assert _apply_fresh_bean_override(name, "Beans and pulses (dried)") is None


def test_apply_fresh_bean_override_ignores_other_categories():
    assert _apply_fresh_bean_override("HARICOT BEANS FRENCH", "Vegetables (misc.)") is None


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
