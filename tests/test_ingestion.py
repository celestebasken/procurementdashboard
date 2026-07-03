import sqlite3
from io import StringIO

import pandas as pd
import pytest

from lib.db import init_db
from lib.ingestion import (
    aggregate_and_load,
    build_cert_lookup,
    load_ucb,
    load_ucd,
    load_ucd_h,
    load_ucla_h,
    load_ucr,
    load_ucsc,
    load_ucsd_h,
    split_non_food,
    validate_certification_text,
)
from lib.reference_loader import load_certification_types


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


# --------------------------------------------------------------------------
# UCB
# --------------------------------------------------------------------------

def test_load_ucb_parses_currency_units_and_certs(tmp_path):
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        'Acme Bread,1,Sour Bread,,,, $ 110.64 ,"12.000 LB",No,,,,,,,\n'
        'Acme Bread,2,Oz Item,,,, $ 5.00 ,16.000 oz,Yes,,,,,,Plant-based,\n'
        "Sysco,3,Cert Item,,,, $ 9.00 ,1.000 lb,No,,,,,,x,\"CH, GAP\"\n"
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    assert list(df["total_weight_lbs"]) == pytest.approx([12.0, 1.0, 1.0])
    assert list(df["weight_source"]) == ["reported", "reported", "reported"]
    assert list(df["total_price"]) == pytest.approx([110.64, 5.00, 9.00])
    assert list(df["sustainable_yn"]) == ["N", "Y", "N"]
    # bare 'x' checkbox in Certification is dropped in favor of Cert's real value
    assert df["sustainability_certifications"].iloc[2] == "CH, GAP"


# --------------------------------------------------------------------------
# UCD
# --------------------------------------------------------------------------

def test_load_ucd_weight_and_stars(tmp_path):
    cols = (
        "Name,Distributor Name (delivering entity),Manufacturer/Farm Name,Brand Name (if different from Column F),"
        "Pack Size (e.g.4),Item Weight (e.g.5),Item UOM (e.g.LB),Case Net Weight (e.g. 20),Product Category,"
        "Product Subcategory,Distributor Product Code (if available),Mfgr. Code(if available),GTIN,"
        "Total Quantity Purchased,Total Quantity Purchased UOM,Total Spend,STARS Certification,Plant Based,"
        "Local 250,Diet Factors,Sustainable Farming,Sustainable Agriculture,Institution - Affirmed Production,"
        "Humane Animal Care,Sustainable Seafood,Fair Trade / Labor,Paper Type,Other Attributes,Plastic Type\n"
    )
    row1 = "Case Item,Sysco,,,2,5,CS,5,Frozen,Side,1,,,3,CS,90,No,No,No,,Organic,,,,,,,\n"
    row2 = "Direct LB Item,Sysco,,,1,1,LB,1,Produce,Fresh,2,,,10,LB,50,STARS 2.2,No,No,,,,,,,,\n"
    df = load_ucd(_write(tmp_path, "ucd.csv", cols + row1 + row2))
    assert df["total_weight_lbs"].iloc[0] == pytest.approx(15.0)  # 5 * 3
    assert df["weight_source"].iloc[0] == "computed_tier2"
    assert df["total_weight_lbs"].iloc[1] == pytest.approx(10.0)  # 1 * 10
    assert df["weight_source"].iloc[1] == "reported"
    assert df["sustainable_yn"].iloc[0] == "N"
    assert df["sustainable_yn"].iloc[1] == "Y"
    assert df["sustainability_certifications"].iloc[0] == "Organic"


# --------------------------------------------------------------------------
# UCD_H
# --------------------------------------------------------------------------

def test_load_ucd_h_infers_sustainable_from_cert_presence(tmp_path):
    csv = (
        "weight_lb,qty, total_cost ,Brand,supplier,Name,env_sub_category,env_type_cert,local,uom\n"
        "10,1,20,,Barsotti,Certified Item,Fruit,Organic,Non Local,Pounds\n"
        "5,1,8,,Barsotti,Uncertified Item,Fruit, ,Non Local,Pounds\n"
    )
    df = load_ucd_h(_write(tmp_path, "ucd_h.csv", csv))
    assert list(df["sustainable_yn"]) == ["Y", "N"]
    assert df["total_weight_lbs"].iloc[0] == pytest.approx(10.0)
    assert df["weight_source"].iloc[0] == "reported"


# --------------------------------------------------------------------------
# UCLA_H
# --------------------------------------------------------------------------

def test_load_ucla_h_uses_notes_not_description(tmp_path):
    cols = (
        "account,entry_date,env_category,weight,qty,total_cost,env_defined_type,notes,current_month,supplier,"
        "brand,description,env_sub_category,env_type_cert,local,healthy_bev,uom,pack_size,total_quantity,"
        "packaging,beverage_type,stop_light,healthy_other\n"
    )
    row = (
        "1,9/30/24,Eggs,141,6,502,Conventional,EGG SHELL LARGE,9/1/24,US Foods,GLENVIEW,,Egg,"
        "American Humane Certified,Non Local,,Pounds,,,,,,\n"
    )
    df = load_ucla_h(_write(tmp_path, "ucla_h.csv", cols + row))
    assert df["raw_name"].iloc[0] == "EGG SHELL LARGE"
    assert df["sustainable_yn"].iloc[0] == "N"
    assert df["total_weight_lbs"].iloc[0] == pytest.approx(141.0)
    assert df["weight_source"].iloc[0] == "reported"


# --------------------------------------------------------------------------
# UCR
# --------------------------------------------------------------------------

def test_load_ucr_excludes_dolr_rollups_and_parses_hash_weight(tmp_path):
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,PRODUCE,DOLR,"1,000","1,000.00",Not Found,,,,,\n'
        '2,CHICKEN HALAL,20#CS,"5","301.00",Sysco,123,,,,\n'
        '3,ORGANIC KALE,CASE,"10","50.00",UNFI,124,USDA Organic,,,Produce\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    assert len(df) == 2  # DOLR rollup row dropped
    assert set(df["raw_name"]) == {"CHICKEN HALAL", "ORGANIC KALE"}
    chicken = df[df["raw_name"] == "CHICKEN HALAL"].iloc[0]
    assert chicken["total_weight_lbs"] == pytest.approx(100.0)  # 20 lb/case * 5
    assert chicken["weight_source"] == "computed_tier2"
    assert chicken["sustainable_yn"] == "N"
    kale = df[df["raw_name"] == "ORGANIC KALE"].iloc[0]
    assert kale["weight_source"] == "unresolved"  # "CASE" has no embedded lb size
    assert kale["sustainable_yn"] == "Y"


# --------------------------------------------------------------------------
# UCSC
# --------------------------------------------------------------------------

def test_load_ucsc_skips_junk_header_row_and_parses_lb_oz(tmp_path):
    junk_row = "Real Food,,junk,junk\n"
    header = (
        'STARS 3.0 Cert,Real Food,Product Description,Month,Year,"Item #\n(Foodpro)","Product #\n(VON)",Cost,'
        'Total Item cost,"Units\nPurchased",Units,"Unit\nType",Total units ordered,"Total Weight \n(in lbs)",'
        'Label/Brand,Vendor,Food Type (WRI),Food Type (RFC),"Is this product plant-\nbased?",Notes\n'
    )
    row1 = '✅,✅,LB ITEM,11,2024,1,1,"$10.00","$10.00",1,50,LB,"50",-,Brand,Vendor,,,,\n'
    row2 = '❌,❌,OZ ITEM,11,2024,2,2,"$5.00","$5.00",1,32,OZ,"32",-,Brand,Vendor,,,,\n'
    df = load_ucsc(_write(tmp_path, "ucsc.csv", junk_row + header + row1 + row2))
    assert len(df) == 2
    lb_item = df[df["raw_name"] == "LB ITEM"].iloc[0]
    assert lb_item["total_weight_lbs"] == pytest.approx(50.0)
    assert lb_item["weight_source"] == "reported"
    assert lb_item["sustainable_yn"] == "Y"
    oz_item = df[df["raw_name"] == "OZ ITEM"].iloc[0]
    assert oz_item["total_weight_lbs"] == pytest.approx(2.0)  # 32 oz / 16
    assert oz_item["sustainable_yn"] == "N"


# --------------------------------------------------------------------------
# UCSD_H
# --------------------------------------------------------------------------

def test_load_ucsd_h_distributor_and_supplier_are_separate_fields(tmp_path):
    csv = (
        "account,entry_date,ProductName,env_category,env_sub_category,weight,qty,total_cost,Sustainable?,"
        "Certification,current_month,Distributor,Supplier,description,local,healthy_bev,uom,pack_size,"
        "total_quantity,packaging,beverage_type,stop_light,healthy_other\n"
        "1,7/22/24,Organic Carrots,Roots,Root,5,1,8.5,Sustainable,USDA Organic,7/1/24,Specialty Produce,Some Farm, ,Non Local, ,Pounds, , , , , \n"
        "2,7/22/24,Conventional Item,Roots,Root,3,1,4.0,Conventional,,7/1/24,Specialty Produce, , ,Non Local, ,Pounds, , , , , \n"
    )
    df = load_ucsd_h(_write(tmp_path, "ucsd_h.csv", csv))
    # Distributor is the entity-matching vendor gate; Supplier is a smaller-
    # grain, often-blank concept that must never substitute for it (confirmed
    # with project owner -- conflating the two was a real bug).
    assert df["vendor"].iloc[0] == "Specialty Produce"
    assert df["brand"].iloc[0] == "Some Farm"
    assert df["vendor"].iloc[1] == "Specialty Produce"
    assert pd.isna(df["brand"].iloc[1])
    assert df["sustainable_yn"].iloc[0] == "Y"
    assert df["sustainable_yn"].iloc[1] == "N"


# --------------------------------------------------------------------------
# Non-certification noise stripped from sustainability_certifications
# --------------------------------------------------------------------------

def test_load_ucla_h_strips_diet_tags_from_certifications(tmp_path):
    cols = (
        "account,entry_date,env_category,weight,qty,total_cost,env_defined_type,notes,current_month,supplier,"
        "brand,description,env_sub_category,env_type_cert,local,healthy_bev,uom,pack_size,total_quantity,"
        "packaging,beverage_type,stop_light,healthy_other\n"
    )
    real_cert_row = "1,9/30/24,Eggs,141,6,502,Sustainable,REAL CERT ITEM,9/1/24,US Foods,GLENVIEW,,Egg,American Humane Certified,Non Local,,Pounds,,,,,,\n"
    diet_tag_row = "2,9/30/24,Grocery,10,1,20,Sustainable,VEG ITEM,9/1/24,US Foods,GLENVIEW,,Grocery,VEGETARIAN,Non Local,,Pounds,,,,,,\n"
    df = load_ucla_h(_write(tmp_path, "ucla_h.csv", cols + real_cert_row + diet_tag_row))
    assert df["sustainability_certifications"].iloc[0] == "American Humane Certified"
    assert pd.isna(df["sustainability_certifications"].iloc[1])  # 'VEGETARIAN' stripped, not a certification


def test_load_ucb_strips_plant_based_tag_from_certifications(tmp_path):
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        "Sysco,1,Veg Item,,,, $ 5.00 ,1.000 lb,No,,,,,,Plant-based Alternative Proteins,\n"
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    assert pd.isna(df["sustainability_certifications"].iloc[0])


# --------------------------------------------------------------------------
# Non-food line removal
# --------------------------------------------------------------------------

def test_split_non_food_uses_confirmed_safe_keywords_only():
    df = pd.DataFrame(
        {
            "raw_name": [
                "NAPKIN DINNER 2PLY",
                "GLOVE NITRILE LARGE",
                "LID FLAT HOT CUP",
                "UNIFORM CHEF COAT",
                "APPLESAUCE NATURAL CUP TRAY PK",  # food-in-cup, not filtered (CUP excluded on purpose)
                "SKITTLES FRUIT ORIG PEG BAG",  # food-in-bag, not filtered (BAG excluded on purpose)
                "CHICKEN BREAST BONELESS",
            ]
        }
    )
    food, non_food = split_non_food(df)
    assert set(non_food["raw_name"]) == {"NAPKIN DINNER 2PLY", "GLOVE NITRILE LARGE", "LID FLAT HOT CUP", "UNIFORM CHEF COAT"}
    assert set(food["raw_name"]) == {"APPLESAUCE NATURAL CUP TRAY PK", "SKITTLES FRUIT ORIG PEG BAG", "CHICKEN BREAST BONELESS"}


def test_split_non_food_removes_plain_and_bubly_water_but_not_water_as_ingredient():
    df = pd.DataFrame(
        {
            "raw_name": [
                "BUBLY STRAWBERRY SUNSET SPARKLING 12OZ",
                "WATER SPARKLING BUBLY CHERRY 12OZ",
                "PROUD SPRING WATER PH BALANCED 750ML",
                "WATER BOTTLED",
                "AQUAFINA WATER 16.9oz",
                "Voss Sparkling Water",
                # water as an ingredient/packing medium in a real food item --
                # must NOT be removed (confirmed against real data: bare
                # "Water" alone has a huge false-positive rate).
                "STAR-KIST CHUNK LITE TUNA IN WATER 5oz",
                "HAM DICED 0.25\" HAM & WATER 2/5LB",
                "MIX, CAKE CHOCOLATE COMPLETE ADD WATER",
                # flavored water -- explicitly out of scope for now, must
                # stay classified as food (Liquids) rather than removed.
                "GINGER TANGERINE WATER",
                "DRINK COCONUT WATER C2O W/ PULP 17.5OZ",
            ]
        }
    )
    food, non_food = split_non_food(df)
    assert set(non_food["raw_name"]) == {
        "BUBLY STRAWBERRY SUNSET SPARKLING 12OZ",
        "WATER SPARKLING BUBLY CHERRY 12OZ",
        "PROUD SPRING WATER PH BALANCED 750ML",
        "WATER BOTTLED",
        "AQUAFINA WATER 16.9oz",
        "Voss Sparkling Water",
    }
    assert set(food["raw_name"]) == {
        "STAR-KIST CHUNK LITE TUNA IN WATER 5oz",
        "HAM DICED 0.25\" HAM & WATER 2/5LB",
        "MIX, CAKE CHOCOLATE COMPLETE ADD WATER",
        "GINGER TANGERINE WATER",
        "DRINK COCONUT WATER C2O W/ PULP 17.5OZ",
    }


# --------------------------------------------------------------------------
# Certification validation
# --------------------------------------------------------------------------

@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    load_certification_types(conn)
    yield conn
    conn.close()


def test_validate_certification_matches_known_cert(conn):
    cert_lookup = build_cert_lookup(conn)
    flag = validate_certification_text("Certified Humane Raised and Handled", "Academic", cert_lookup)
    assert flag == 0


def test_validate_certification_flags_unknown_cert(conn):
    cert_lookup = build_cert_lookup(conn)
    flag = validate_certification_text("Totally Made Up Certification XYZ", "Academic", cert_lookup)
    assert flag == 1


def test_validate_certification_blank_is_not_a_mismatch(conn):
    cert_lookup = build_cert_lookup(conn)
    assert validate_certification_text(None, "Academic", cert_lookup) == 0
    assert validate_certification_text("", "Health", cert_lookup) == 0


def test_validate_certification_matches_case_and_abbreviation_variants(conn):
    cert_lookup = build_cert_lookup(conn)
    assert validate_certification_text("CH", "Academic", cert_lookup) == 0  # abbreviation, exact
    assert validate_certification_text("CERTIFIED HUMANE", "Academic", cert_lookup) == 0  # all-caps + shortened
    # shortened, token-subset match; covers both frameworks so passes for Health too
    assert validate_certification_text("Rainforest Alliance", "Health", cert_lookup) == 0


def test_validate_certification_bare_organic_aliases_to_usda_organic(conn):
    cert_lookup = build_cert_lookup(conn)
    assert validate_certification_text("Organic", "Academic", cert_lookup) == 0
    assert validate_certification_text("ORGANIC", "Health", cert_lookup) == 0


def test_validate_certification_antibiotic_free_aliases_to_nae(conn):
    cert_lookup = build_cert_lookup(conn)
    assert validate_certification_text("Antibiotic Free", "Health", cert_lookup) == 0
    # AASHE STARS doesn't have NAE listed as an applicable framework -- confirms
    # the alias is framework-checked like any other match, not a blanket pass.
    assert validate_certification_text("Antibiotic Free", "Academic", cert_lookup) == 1


def test_validate_certification_rbst_free_stays_unmatched(conn):
    # rBST/hormone claims have no corresponding entry in certification_types.csv
    # (distinct from antibiotics) -- confirmed with project owner not to alias.
    cert_lookup = build_cert_lookup(conn)
    assert validate_certification_text("rBST Free", "Health", cert_lookup) == 1
    # Also confirm the new Fair Trade / GAP aliases (both introduced alongside
    # this one) don't accidentally catch it via substring/fuzzy leakage.
    assert validate_certification_text("RBST FREE", "Health", cert_lookup) == 1


def test_validate_certification_fair_trade_routes_by_campus_type(conn):
    # certification_types.csv has two separate rows for this concept: "Fair
    # Trade Certifications" (AASHE STARS) and "Fairtrade International"
    # (Practice Greenhealth) -- bare "Fair Trade" must resolve to whichever
    # one covers the reporting campus's framework.
    cert_lookup = build_cert_lookup(conn)
    assert validate_certification_text("Fair Trade", "Academic", cert_lookup) == 0
    assert validate_certification_text("Fair Trade", "Health", cert_lookup) == 0


def test_validate_certification_monterey_aquarium_variant_aliases(conn):
    cert_lookup = build_cert_lookup(conn)
    assert validate_certification_text("MONTEREY AQUARIUM BEST CHOICE GREEN", "Academic", cert_lookup) == 0
    # Monterey Bay Aquarium Seafood Watch is AASHE STARS-only in
    # certification_types.csv -- no Practice Greenhealth equivalent exists,
    # so Health-campus mentions correctly still fail even after aliasing.
    assert validate_certification_text("MONTEREY AQUARIUM BEST CHOICE GREEN", "Health", cert_lookup) == 1


def test_validate_certification_gap_4_certified_aliases(conn):
    cert_lookup = build_cert_lookup(conn)
    assert validate_certification_text("GAP 4 Certified", "Academic", cert_lookup) == 0
    assert validate_certification_text("GAP 4 Certified", "Health", cert_lookup) == 0


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def test_aggregate_and_load_validated_sustainable_yn(conn):
    load_certification_types(conn)
    conn.execute(
        "INSERT INTO campuses (campus, primary_standard, campus_type, abbreviation) "
        "VALUES ('UC Test', 'AASHE STARS', 'Academic', 'UCT')"
    )
    conn.commit()
    df = pd.DataFrame(
        {
            "raw_name": ["Validated Y", "Unvalidated Y", "No Cert Y", "Claimed N"],
            "vendor": ["Sysco"] * 4,
            "brand": [None] * 4,
            "total_price": [10.0] * 4,
            "total_weight_lbs": [1.0] * 4,
            "weight_source": ["reported"] * 4,
            "sustainable_yn": ["Y", "Y", "Y", "N"],
            "sustainability_certifications": [
                "Certified Humane Raised and Handled",
                "Totally Made Up Certification XYZ",
                None,
                None,
            ],
            "purchase_type": ["purchasing"] * 4,
        }
    )
    cert_lookup = build_cert_lookup(conn)
    aggregate_and_load(df, "UC Test", "Academic", 2025, conn, "test.csv", cert_lookup)
    rows = dict(
        conn.execute("SELECT canonical_name, validated_sustainable_yn FROM products").fetchall()
    )
    assert rows["Validated Y"] == "Y"  # claimed cert actually matches known vocabulary
    assert rows["Unvalidated Y"] == "N"  # claimed cert doesn't match anything -> downgraded
    assert rows["No Cert Y"] == "Y"  # no cert claimed at all -> nothing to invalidate
    assert rows["Claimed N"] == "N"  # campus said N -> passes through unchanged


def test_aggregate_and_load_collapses_duplicate_raw_names(conn):
    load_certification_types(conn)
    conn.execute(
        "INSERT INTO campuses (campus, primary_standard, campus_type, abbreviation) "
        "VALUES ('UC Test', 'AASHE STARS', 'Academic', 'UCT')"
    )
    conn.commit()
    df = pd.DataFrame(
        {
            "raw_name": ["Widget", "Widget", "Other"],
            "vendor": ["Sysco", "Sysco", "UNFI"],
            "brand": [None, None, None],
            "total_price": [10.0, 20.0, 5.0],
            "total_weight_lbs": [1.0, 2.0, None],
            "weight_source": ["reported", "reported", "unresolved"],
            "sustainable_yn": ["Y", "Y", "N"],
            "sustainability_certifications": [None, None, None],
            "purchase_type": ["purchasing", "purchasing", "purchasing"],
        }
    )
    cert_lookup = build_cert_lookup(conn)
    stats = aggregate_and_load(df, "UC Test", "Academic", 2025, conn, "test.csv", cert_lookup)
    assert stats["products_created"] == 2
    widget = conn.execute(
        "SELECT total_price, total_weight_lbs, weight_source, n_transactions_aggregated FROM purchases p "
        "JOIN products pr ON p.product_id = pr.product_id WHERE pr.canonical_name = 'Widget'"
    ).fetchone()
    assert widget == (30.0, 3.0, "reported", 2)


def test_aggregate_and_load_mixed_weight_tiers_uses_weakest(conn):
    conn.execute(
        "INSERT INTO campuses (campus, primary_standard, campus_type, abbreviation) "
        "VALUES ('UC Test', 'AASHE STARS', 'Academic', 'UCT')"
    )
    conn.commit()
    df = pd.DataFrame(
        {
            "raw_name": ["Widget", "Widget"],
            "vendor": ["Sysco", "Sysco"],
            "brand": [None, None],
            "total_price": [10.0, 20.0],
            "total_weight_lbs": [1.0, 2.0],
            "weight_source": ["reported", "computed_tier2"],
            "sustainable_yn": ["Y", "Y"],
            "sustainability_certifications": [None, None],
            "purchase_type": ["purchasing", "purchasing"],
        }
    )
    cert_lookup = build_cert_lookup(conn)
    aggregate_and_load(df, "UC Test", "Academic", 2025, conn, "test.csv", cert_lookup)
    weight_source = conn.execute("SELECT weight_source FROM purchases").fetchone()[0]
    assert weight_source == "computed_tier2"


def test_aggregate_and_load_drops_blank_raw_name(conn):
    conn.execute(
        "INSERT INTO campuses (campus, primary_standard, campus_type, abbreviation) "
        "VALUES ('UC Test', 'AASHE STARS', 'Academic', 'UCT')"
    )
    conn.commit()
    df = pd.DataFrame(
        {
            "raw_name": ["Widget", None],
            "vendor": ["Sysco", "Sysco"],
            "brand": [None, None],
            "total_price": [10.0, 5.0],
            "total_weight_lbs": [1.0, None],
            "weight_source": ["reported", "unresolved"],
            "sustainable_yn": ["Y", "NA"],
            "sustainability_certifications": [None, None],
            "purchase_type": ["purchasing", "purchasing"],
        }
    )
    cert_lookup = build_cert_lookup(conn)
    stats = aggregate_and_load(df, "UC Test", "Academic", 2025, conn, "test.csv", cert_lookup)
    assert stats["products_created"] == 1
