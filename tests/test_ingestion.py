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


def test_load_ucb_sysco_pack_size_times_quantity(tmp_path):
    # Real Sysco pattern: Pack size is the weight of ONE CASE, Quantity
    # sold is the number of cases -- verified against real data with a
    # $/lb check (this exact row implies $4.42/lb, plausible for liquid
    # egg). "AVG"/"AV" qualifier and the "N/M unit" sub-pack-count shape
    # are both real too.
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        'Sysco,1,EGG LIQ WHL CAGE FREE W/CITRIC,,20 LB, $ 88.37 , $ 259179.30 ,2933,No,,,,,,,\n'
        "Sysco,2,BEEF GRND CHUB 80/20 HALAL,,10#AVG, $ 40.00 , $ 400.00 ,1,No,,,,,,,\n"
        "Sysco,3,SHRIMP CPND 31/40 T/OFF,,5 2 LB, $ 68.90 , $ 620.10 ,9,No,,,,,,,\n"
        "Sysco,4,SUGAR PACKET,,1GM, $ 34.62 , $ 1038.60 ,30,No,,,,,,,\n"
        "Sysco,5,PEETS COFFEE CASE,,12,\" $ 92.20 \",\" $ 13369.00 \",145,No,,,,,,,\n"
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    egg = df.iloc[0]
    assert egg["total_weight_lbs"] == pytest.approx(20 * 2933)
    assert egg["weight_source"] == "computed_tier2"
    avg = df.iloc[1]
    assert avg["total_weight_lbs"] == pytest.approx(10 * 1)  # "10#AVG" -> 10 lb
    assert avg["weight_source"] == "computed_tier2"
    subpack = df.iloc[2]
    assert subpack["total_weight_lbs"] == pytest.approx(5 * 2 * 9)  # 5 packs of 2 lb, 9 cases
    assert subpack["weight_source"] == "computed_tier2"
    # "1GM" is an individual packet weight, not a case weight -- multiplying
    # by case count would understate true weight by orders of magnitude
    # (real bug found this way); below the 1 lb plausibility floor, so it
    # must stay unresolved rather than commit a wrong number.
    packet = df.iloc[3]
    assert packet["weight_source"] == "unresolved"
    # bare Pack size "12" with no unit/# at all is a per-case ITEM COUNT
    # (Peets), not a weight -- must not be treated as 12 lb.
    peets = df.iloc[4]
    assert peets["weight_source"] == "unresolved"


def test_load_ucb_allen_brothers_and_cream_co_quantity_is_weight_directly(tmp_path):
    # Verified against real data: for these two distributors, "Quantity
    # sold" is already the total weight in lbs -- no Pack size
    # multiplication. Confirmed via $/lb plausibility (chicken/beef/lamb
    # in the $3-7/lb range), not Price*Quantity=Extended (neither
    # distributor populates a Price column in this export).
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        'Allen Brothers,1,CHICKEN BRST BNLS SKLS (H) 4-5 OZ MARYS,,4/10 LB CS,,\" $ 169792.00 \",24712,No,,,,,,,\n'
        "Cream Co,2,Beef Loin Tri Tip Halal,,#50,,\" $ 246550.28 \",33018.82,No,,,,,,,\n"
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    chicken = df.iloc[0]
    assert chicken["total_weight_lbs"] == pytest.approx(24712)
    assert chicken["weight_source"] == "computed_tier2"
    beef = df.iloc[1]
    assert beef["total_weight_lbs"] == pytest.approx(33018.82)
    assert beef["weight_source"] == "computed_tier2"


def test_load_ucb_daylight_foods_parses_case_weight_from_name(tmp_path):
    # Daylight Foods' Pack size column is blank in this export -- the
    # per-case weight is embedded in the raw_name instead. Verified
    # against real data ($3.60/lb and $1.90/lb respectively, plausible
    # for organic spring mix and broccoli).
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        'Daylight Foods,1,"SPRING MIX, SWEET ORG 3-LB",,,,\" $ 48298.00 \",4477,No,,,,,,,\n'
        'Daylight Foods,2,"BROCCOLI, FLORETS 4/3-LB",,,,\" $ 81645.48 \",3576,No,,,,,,,\n'
        'Daylight Foods,3,"CUCUMBER, 36-CT",,,,\" $ 20770.18 \",1140,No,,,,,,,\n'
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    spring_mix = df.iloc[0]
    assert spring_mix["total_weight_lbs"] == pytest.approx(3 * 4477)
    assert spring_mix["weight_source"] == "computed_tier2"
    broccoli = df.iloc[1]
    assert broccoli["total_weight_lbs"] == pytest.approx(4 * 3 * 3576)  # 4 sub-packs of 3 lb
    assert broccoli["weight_source"] == "computed_tier2"
    # "36-CT" is a count, not a weight -- no LB/OZ unit present at all, but
    # now resolved via the count_based_food_dictionary Tier 3 lookup (see
    # test_load_ucb_daylight_foods_count_based_produce_tier3 below).
    cucumber = df.iloc[2]
    assert cucumber["total_weight_lbs"] == pytest.approx(0.66 * 36 * 1140)
    assert cucumber["weight_source"] == "reference_table_tier3"


def test_load_ucb_peets_and_jfc_parse_name_embedded_case_weight(tmp_path):
    # Peets and JFC's "Pack size" is a bare count that duplicates a number
    # already in the name -- the actual size comes from the name, same
    # mechanism as Daylight Foods.
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        'Peets,1,507076 - PEETS YOSEMITE DOS SIERRAS ORGANIC COM 12/12OZ,,12,\" $ 92.20 \",\" $ 13369.00 \",145,No,,,,,,,\n'
        "JFC,2,N.S Shin Bowl Noodle 12/3.03 oz,,12,15.24,\" $ 1325.88 \",87,No,,,,,,,\n"
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    coffee = df.iloc[0]
    assert coffee["total_weight_lbs"] == pytest.approx((12 * 12 / 16.0) * 145)
    assert coffee["weight_source"] == "computed_tier2"
    noodle = df.iloc[1]
    assert noodle["total_weight_lbs"] == pytest.approx((12 * 3.03 / 16.0) * 87)
    assert noodle["weight_source"] == "computed_tier2"


def test_load_ucb_parses_three_level_case_pack_in_name(tmp_path):
    # Real bug: a first version of this regex only captured the last two
    # numbers before the unit, silently undercounting real 3-level packs
    # like "12/10/1.45 oz" (12 cases x 10 boxes x 1.45 oz/piece) by 12x --
    # caught via a $/lb sanity check (implied $231/lb for a snack food vs.
    # a plausible ~$19/lb once all three numbers are multiplied).
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        "JFC,1,Glico Pocky Almond Crush Eng 12/10/1.45 oz,,12,210.00,\" $ 840.00 \",4,No,,,,,,,\n"
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    row = df.iloc[0]
    expected_per_case_lb = (12 * 10 * 1.45) / 16.0
    assert row["total_weight_lbs"] == pytest.approx(expected_per_case_lb * 4)
    assert row["weight_source"] == "computed_tier2"


def test_load_ucb_bordenaves_tier3_lookup_with_pack_multiplier(tmp_path):
    # Bordenaves' "Pack size" is the unit Price/Quantity are priced in:
    # "ea" = Quantity is already an individual-item count, "dz" = Quantity
    # is counted in DOZENS (verified: Price $5.75/dz x Quantity 229 =
    # Extended $1316.75). Weight comes from the project-owner-confirmed
    # reference dictionary (title -> assumed per-item lb).
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        'Bordanaves,1,Sour 2# Vienna 3/4" Sliced,,ea,5.17,\" $ 6850.25 \",1325,No,,,,,,,\n'
        "Bordanaves,2,Sweet Small Round Roll Dz,,dz,5.75,\" $ 1316.75 \",229,No,,,,,,,\n"
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    loaf = df.iloc[0]
    assert loaf["total_weight_lbs"] == pytest.approx(2.0 * 1 * 1325)  # 2 lb/loaf, "ea"
    assert loaf["weight_source"] == "reference_table_tier3"
    roll = df.iloc[1]
    assert roll["total_weight_lbs"] == pytest.approx(0.09375 * 12 * 229)  # 1.5 oz/roll, "dz" = x12
    assert roll["weight_source"] == "reference_table_tier3"


def test_load_ucb_ben_and_jerrys_prefers_explicit_oz_over_tier3(tmp_path):
    # "Pack size" is always "N units" (items per case); Quantity sold is
    # the case count. An explicit per-item oz already stated in the name
    # (real data, not an assumption) is preferred over the Tier 3 pint
    # estimate when present.
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        "Ben & Jerry's,1,BJ Pt CHERRY GARCIA,,8 units ,36.64,\" $ 1978.56 \",54,No,,,,,,,\n"
        "Ben & Jerry's,2,BJ CHOC CHIP DOUGH CUP 4oz,,12 units,,\" $ 500.00 \",10,No,,,,,,,\n"
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    pint = df.iloc[0]
    assert pint["total_weight_lbs"] == pytest.approx(1.0 * 8 * 54)  # 16oz/pint Tier 3 estimate
    assert pint["weight_source"] == "reference_table_tier3"
    cup = df.iloc[1]
    assert cup["total_weight_lbs"] == pytest.approx((4.0 / 16.0) * 12 * 10)  # explicit 4oz, real data
    assert cup["weight_source"] == "computed_tier2"


def test_load_ucb_kikka_sushi_tier3_lookup_by_roll_type(tmp_path):
    # No Pack size column and no case multiplier -- Price is already
    # per-roll, so Quantity sold is the individual-roll count directly.
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        "Kikka Sushi,1,CALIFORNIA ROLL,,,4.51,\" $ 34438.36 \",7636,No,,,,,,,\n"
        "Kikka Sushi,2,SHRIMP SPRING ROLL,,,4.77,\" $ 11810.52 \",2476,No,,,,,,,\n"
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    roll = df.iloc[0]
    assert roll["total_weight_lbs"] == pytest.approx(0.4375 * 7636)  # 7oz/roll
    assert roll["weight_source"] == "reference_table_tier3"
    spring_roll = df.iloc[1]
    assert spring_roll["total_weight_lbs"] == pytest.approx(0.1875 * 2476)  # 3oz/spring roll, matched first
    assert spring_roll["weight_source"] == "reference_table_tier3"


def test_load_ucb_corrects_known_kikka_price_typo(tmp_path):
    # Real bug in the raw file: Extended Price ($93,779.78) doesn't match
    # Price x Quantity ($5.62 x 1,669 = $9,379.78) -- confirmed with
    # project owner as a data-entry typo, corrected to the reconstructed
    # value for this one specific row.
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        "Kikka Sushi,1,GARDEN VEGETABLE SALAD ROLL,,,5.62,\" $ 93779.78 \",1669,No,,,,,,,\n"
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    assert df.iloc[0]["total_price"] == pytest.approx(9379.78)


def test_load_ucb_bimbo_bakery_explicit_oz_and_ct_tier3(tmp_path):
    # Bimbo Bakery mixes two shapes: some rows have an explicit-oz Pack
    # size ("16oz") reusing the Sysco-style parser directly; others have a
    # bare count ("6 Ct") needing an exact-title Tier 3 lookup (per-item
    # weight) x the parsed Ct-count x Quantity (case count).
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        'Bimbo Bakery,1,Bread Roll French 6 in,,16oz, $ 2.00 ,\" $ 200.00 \",100,No,,,,,,,\n'
        'Bimbo Bakery,2,Bagel Thomas Plain 6 Pk,,6 Ct, $ 4.00 ,\" $ 400.00 \",100,No,,,,,,,\n'
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    explicit_oz = df.iloc[0]
    assert explicit_oz["total_weight_lbs"] == pytest.approx((16.0 / 16.0) * 100)
    assert explicit_oz["weight_source"] == "computed_tier2"
    ct_tier3 = df.iloc[1]
    assert ct_tier3["total_weight_lbs"] == pytest.approx(0.20833333333333334 * 6 * 100)
    assert ct_tier3["weight_source"] == "reference_table_tier3"


def test_load_ucb_espostos_pattern_tier3_multiplier_always_one(tmp_path):
    # Espostos' "Pack size" is always "1" -- Quantity sold is already an
    # individual-item count. Weight comes from a pattern-matched Tier 3
    # lookup by item type (no case-size multiplication at all).
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        "Espostos,1,Apple Fritters - Large,,1,2.50,\" $ 250.00 \",100,No,,,,,,,\n"
        "Espostos,2,Turkey Panini,,1,6.00,\" $ 600.00 \",100,No,,,,,,,\n"
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    fritter = df.iloc[0]
    assert fritter["total_weight_lbs"] == pytest.approx(0.375 * 100)  # 6oz large fritter, project-owner-revised
    assert fritter["weight_source"] == "reference_table_tier3"
    panini = df.iloc[1]
    assert panini["total_weight_lbs"] == pytest.approx(0.4375 * 100)  # 7oz sandwich/wrap catch-all
    assert panini["weight_source"] == "reference_table_tier3"


def test_load_ucb_city_baking_pattern_tier3_with_int_multiplier(tmp_path):
    # City Baking's "Pack size" is a bare integer representing items per
    # case -- pattern-matched Tier 3 (per-item weight) x Pack-size-int x
    # Quantity (case count).
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        "City Baking,1,CHOCOLATE CHIP COOKIE,,24,12.00,\" $ 1200.00 \",100,No,,,,,,,\n"
        "City Baking,2,GINGERBREAD KID,,4,1.80,\" $ 180.00 \",100,No,,,,,,,\n"
        "City Baking,3,GINGER BREAD,,1,10.00,\" $ 1000.00 \",100,No,,,,,,,\n"
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    cookie = df.iloc[0]
    assert cookie["total_weight_lbs"] == pytest.approx(0.125 * 24 * 100)  # 2oz/cookie
    assert cookie["weight_source"] == "reference_table_tier3"
    # Real bug: "GINGERBREAD" contains "BREAD" as a substring -- an
    # unbounded pattern matched the 20oz whole-loaf proxy here instead of
    # the small gingerbread-cookie proxy, producing 500lb instead of the
    # correct ~37.5lb. Fixed with a dedicated GINGERBREAD pattern checked
    # before the word-bounded BREAD pattern.
    gingerbread_kid = df.iloc[1]
    assert gingerbread_kid["total_weight_lbs"] == pytest.approx(0.09375 * 4 * 100)
    assert gingerbread_kid["weight_source"] == "reference_table_tier3"
    # A genuine whole-loaf "GINGER BREAD" (space, no "KID") should still
    # hit the word-bounded BREAD pattern and use the loaf proxy.
    ginger_bread = df.iloc[2]
    assert ginger_bread["total_weight_lbs"] == pytest.approx(1.25 * 1 * 100)
    assert ginger_bread["weight_source"] == "reference_table_tier3"


def test_load_ucb_sysco_number10_can(tmp_path):
    # Sysco's "#10" Pack size is a well-established foodservice standard
    # can size, not a case-pack count -- Quantity sold is the number of
    # cans directly. Verified against real data: "SAUCE MARINARA CA"
    # implies $7.53/lb at the dictionary's ~6.5 lb/can estimate, a
    # plausible institutional canned-sauce price.
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        "Sysco,1,SAUCE MARINARA CA,,#10,,\" $ 52393.39 \",1071,No,,,,,,,\n"
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    row = df.iloc[0]
    assert row["total_weight_lbs"] == pytest.approx(6.5 * 1071)
    assert row["weight_source"] == "reference_table_tier3"


def test_load_ucb_sysco_gal_density_excludes_non_food(tmp_path):
    # Sysco's GAL-based Pack sizes get a density-per-product-type
    # conversion, except non-food items that happen to use "GAL" as a
    # capacity rating rather than a food-liquid volume (trash/compost
    # liners) -- these slipped past the general non-food filter and would
    # be nonsensical to convert via a food-liquid density.
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        "Sysco,1,MAYONNAISE REAL,,1 GAL,,\" $ 14801.60 \",176,No,,,,,,,\n"
        "Sysco,2,LINER TRASH 38X58 1.5 ML CLR,,60 GAL,,\" $ 32526.74 \",476,No,,,,,,,\n"
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    mayo = df[df["raw_name"] == "MAYONNAISE REAL"].iloc[0]
    assert mayo["total_weight_lbs"] == pytest.approx(9.0 * 1 * 176)  # condiment density 9.0 lb/gal
    assert mayo["weight_source"] == "reference_table_tier3"
    liner = df[df["raw_name"] == "LINER TRASH 38X58 1.5 ML CLR"].iloc[0]
    assert liner["weight_source"] == "unresolved"


def test_load_ucb_daylight_gal_quantity_shapes(tmp_path):
    # Daylight Foods embeds gallon quantities in the name in three shapes:
    # a bare number ("5 GAL"), a "/"-chain (sub-packs x gallons-each, e.g.
    # "4/1-GAL" = 4 one-gallon jugs), and a mixed number ("12 1/2
    # GALLONS" = 12.5 gal -- the space, not "/", separates the whole part
    # from the fraction, so it must NOT be parsed as a 12x(1/2) chain).
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        'Daylight Foods,1,"MILK, 2% DISP 5 GAL PRODUCERS",,,,\" $ 25337.81 \",1091,No,,,,,,,\n'
        'Daylight Foods,2,"JUICE, ORANGE 4/1-GAL",,,,\" $ 4860.53 \",107.75,No,,,,,,,\n'
        'Daylight Foods,3,"MILK, 2% 12 1/2 GALLONS",,,,\" $ 22741.48 \",792.92,No,,,,,,,\n'
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    bare = df[df["raw_name"] == "MILK, 2% DISP 5 GAL PRODUCERS"].iloc[0]
    assert bare["total_weight_lbs"] == pytest.approx(8.6 * 5 * 1091)
    assert bare["weight_source"] == "reference_table_tier3"
    chain = df[df["raw_name"] == "JUICE, ORANGE 4/1-GAL"].iloc[0]
    assert chain["total_weight_lbs"] == pytest.approx(8.3 * 4 * 107.75)  # catch-all water-like density
    assert chain["weight_source"] == "reference_table_tier3"
    mixed = df[df["raw_name"] == "MILK, 2% 12 1/2 GALLONS"].iloc[0]
    assert mixed["total_weight_lbs"] == pytest.approx(8.6 * 12.5 * 792.92)
    assert mixed["weight_source"] == "reference_table_tier3"


def test_load_ucb_sysco_ct_prefers_explicit_oz_over_dictionary(tmp_path):
    # Sysco's "N CT" Pack size items prefer a real explicit oz stated in
    # the name (computed_tier2, not a guess) over any dictionary
    # estimate, same "prefer the real number" precedent as Ben & Jerry's.
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        "Sysco,1,BUN HAMBURGER 4IN 1.75 OZ,,12CT,,\" $ 18652.86 \",903,No,,,,,,,\n"
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    row = df.iloc[0]
    assert row["total_weight_lbs"] == pytest.approx((1.75 / 16.0) * 12 * 903)
    assert row["weight_source"] == "computed_tier2"


def test_load_ucb_sysco_ct_carton_produce_ignores_count_grade(tmp_path):
    # Real bug fixed here: an earlier version divided the standard carton
    # weight by the count/size-grade code in the name, which is backwards
    # -- a standard produce carton weighs ~the same no matter what size
    # grade of fruit is inside. "formula=case" now applies the flat
    # carton weight regardless of the count grade.
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        "Sysco,1,APPLE FUJI FANCY FRESH,,88CT,,\" $ 2898.63 \",90,No,,,,,,,\n"
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    row = df.iloc[0]
    assert row["total_weight_lbs"] == pytest.approx(40.0 * 90)  # flat 40 lb carton x case count, NOT /88
    assert row["weight_source"] == "reference_table_tier3"


def test_load_ucb_sysco_ct_excludes_bare_one_count(tmp_path):
    # Real bug: "EGG HARD COOKED CAGE FREE" at Pack size "1 CT" implied
    # $137.75/lb when the dictionary's per-egg estimate was multiplied by
    # a case count of 1 -- every food item in these dictionaries is
    # bought in bulk, multi-count cases in this export, so a bare "1 CT"
    # is excluded rather than trusted.
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        "Sysco,1,EGG HARD COOKED CAGE FREE,,1 CT,,\" $ 3013.24 \",200,No,,,,,,,\n"
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    assert df.iloc[0]["weight_source"] == "unresolved"


def test_load_ucb_daylight_ct_count_extraction_shapes(tmp_path):
    # Daylight Foods embeds the count directly in the name in several
    # shapes: a chain ("12/3-CT" = 12 bags of 3 hearts = 36 hearts) and a
    # bare count ("18-CT").
    csv = (
        "Distributor,Item #,Product Name or Description ,Brand,Pack size, Price , Extended Price , Quantity sold ,AASHE?,Plant Based,Bipoc,Organic,Local,Woman Owned,Certification,Cert\n"
        'Daylight Foods,1,"ROMAINE, HEARTS 12/3-CT ANDY BOY",,,,\" $ 15947.30 \",600.58,No,,,,,,,\n'
        'Daylight Foods,2,"BROCCOLINI, 18-CT",,,,\" $ 29653.08 \",940,No,,,,,,,\n'
    )
    df = load_ucb(_write(tmp_path, "ucb.csv", csv))
    romaine = df[df["raw_name"] == "ROMAINE, HEARTS 12/3-CT ANDY BOY"].iloc[0]
    assert romaine["total_weight_lbs"] == pytest.approx(0.5 * (12 * 3) * 600.58)
    assert romaine["weight_source"] == "reference_table_tier3"
    broccolini = df[df["raw_name"] == "BROCCOLINI, 18-CT"].iloc[0]
    assert broccolini["total_weight_lbs"] == pytest.approx(0.5 * 18 * 940)
    assert broccolini["weight_source"] == "reference_table_tier3"


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
    # Real bug fixed this session: total_weight must be Item Weight (a
    # PER-PIECE weight) x Pack Size (pieces/case) x Qty for every UOM
    # except bare "LB" -- previously Pack Size was silently dropped
    # entirely, e.g. undercounting a real potsticker case by ~150x.
    row1 = "Case Item,Sysco,,,2,5,CS,5,Frozen,Side,1,,,3,CS,90,No,No,No,,Organic,,,,,,,\n"
    # Bare "LB": Total Quantity Purchased is ALREADY the total weight --
    # Item Weight (14, deliberately != Pack Size here) must be ignored
    # entirely, not multiplied in (real bug: multiplying gave beef brisket
    # an absurd $0.32/lb; ignoring it gives a plausible $4.50/lb).
    row2 = "Direct LB Item,Sysco,,,5,14,LB,14,Produce,Fresh,2,,,10,LB,50,STARS 2.2,No,No,,,,,,,,\n"
    # "5LB": looks like "LB" but is NOT the same exception -- Item Weight
    # (5, matching a real 5 lb bag) still needs Pack Size x Qty applied,
    # and is tagged 'computed_tier2' (a computation), not 'reported'.
    row3 = "5LB Bag Item,Sysco,,,1,5,5LB,5,Produce,Fresh,3,,,4,5LB,30,No,No,No,,,,,,,,,\n"
    df = load_ucd(_write(tmp_path, "ucd.csv", cols + row1 + row2 + row3))
    assert df["total_weight_lbs"].iloc[0] == pytest.approx(30.0)  # 5 * 2 * 3
    assert df["weight_source"].iloc[0] == "computed_tier2"
    assert df["total_weight_lbs"].iloc[1] == pytest.approx(10.0)  # Qty alone, Item Weight ignored
    assert df["weight_source"].iloc[1] == "reported"
    assert df["total_weight_lbs"].iloc[2] == pytest.approx(20.0)  # 5 * 1 * 4
    assert df["weight_source"].iloc[2] == "computed_tier2"
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


def test_load_ucd_h_weight_lb_zero_is_unresolved_not_reported(tmp_path):
    # Real bug: weight_lb == 0 is a missing-data placeholder in this
    # export (real products with real spend, e.g. syrups/sauces), not a
    # legitimate "weighs nothing" claim -- weight.notna() treated 0.0 as
    # valid, silently tagging these 'reported' with a weight of exactly
    # zero. Fixed to require weight > 0 for the 'reported' tier.
    csv = (
        "weight_lb,qty, total_cost ,Brand,supplier,Name,env_sub_category,env_type_cert,local,uom\n"
        "0,200,9197.22,,Barsotti,SAUCE WHITE CHOCOLATE 4CT,Chocolate, ,Non Local,Pounds\n"
        "10,1,20,,Barsotti,Real Weight Item,Fruit, ,Non Local,Pounds\n"
    )
    df = load_ucd_h(_write(tmp_path, "ucd_h.csv", csv))
    assert df["weight_source"].iloc[0] == "unresolved"
    assert pd.isna(df["total_weight_lbs"].iloc[0])
    assert df["weight_source"].iloc[1] == "reported"
    assert df["total_weight_lbs"].iloc[1] == pytest.approx(10.0)


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


def test_load_ucla_h_weight_zero_is_unresolved_not_reported(tmp_path):
    # Same bug as UCD_H/UCSD_H: weight == 0 is a missing-data placeholder
    # (real spend, no weight recorded), not a real "weighs nothing" claim.
    cols = (
        "account,entry_date,env_category,weight,qty,total_cost,env_defined_type,notes,current_month,supplier,"
        "brand,description,env_sub_category,env_type_cert,local,healthy_bev,uom,pack_size,total_quantity,"
        "packaging,beverage_type,stop_light,healthy_other\n"
    )
    row = (
        "1,9/30/24,Sauce,0,200,9197.22,Conventional,SAUCE WHITE CHOCOLATE,9/1/24,US Foods,,,Sauce,,"
        "Non Local,,Pounds,,,,,,\n"
    )
    df = load_ucla_h(_write(tmp_path, "ucla_h.csv", cols + row))
    assert df["weight_source"].iloc[0] == "unresolved"
    assert pd.isna(df["total_weight_lbs"].iloc[0])


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


def test_load_ucr_excludes_dolr_rollup_spelling_variants(tmp_path):
    # Real bug: the original filter only excluded the exact spelling
    # "DOLR" -- rollup rows spelled "DOLR*", "DOLAR", "DOLLR*", "DLR"
    # slipped through and were counted as real line items (verified: each
    # one's "Units" value sits within a few dollars of its own "Total
    # Spend", the signature of a dollar-denominated placeholder row, not
    # a physical unit count -- e.g. real data "POULTRY" Units=86,000.81
    # vs. Total Spend=$86,048.87).
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,POULTRY,DOLR*,"86000.81","86048.87",Not Found,,,,,\n'
        '2,SAFETY ITEMS,DOLAR,"14562.56","14562.56",Not Found,,,,,\n'
        '3,EDI CATCH ALL,DOLLR*,"460.5","9536.62",Not Found,,,,,\n'
        '4,WHOLE GRAINS/ NUTS,DLR,"67.03","67.03",Not Found,,,,,\n'
        '5,CHICKEN HALAL,20#CS,"5","301.00",Sysco,123,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    assert len(df) == 1
    assert set(df["raw_name"]) == {"CHICKEN HALAL"}


def test_load_ucr_parses_bare_lb_and_n_lb_purchase_units(tmp_path):
    # Bare "LB" means Units IS the total weight directly (verified against
    # real data with a $/lb sanity check -- e.g. "CHICKEN THIGH...5#AVG"
    # with Purchase Unit "LB" has Units=19,711.40 and Spend=$79,691.61,
    # implying $4.04/lb, a plausible per-pound price, not a per-unit
    # count). "NLB" (e.g. "5LB") is a per-unit-times-count size, same
    # pattern as "N#".
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,CHICKEN THIGH BULK,LB,"19711.40","79691.61",Sysco,123,,,,\n'
        '2,SBUX PIKE PLACE ROAST,5LB,"80","3907.40",Sysco,124,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    chicken = df[df["raw_name"] == "CHICKEN THIGH BULK"].iloc[0]
    assert chicken["total_weight_lbs"] == pytest.approx(19711.40)
    assert chicken["weight_source"] == "reported"
    coffee = df[df["raw_name"] == "SBUX PIKE PLACE ROAST"].iloc[0]
    assert coffee["total_weight_lbs"] == pytest.approx(400.0)  # 5 lb/unit * 80
    assert coffee["weight_source"] == "computed_tier2"


def test_load_ucr_sysco_name_embedded_chain_ignores_redundant_purchase_unit_multiplier(tmp_path):
    # When the product name already has a full "N/M unit" chain, that
    # chain alone is the per-case weight -- Purchase Unit's own leading
    # number ("4/CS"), when present, is redundant with the chain's first
    # token in the large majority of real cases and must NOT be
    # multiplied in again (that would 4x-inflate this example).
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,CHICKEN THIGH BONELESS SKINLESS VP 4/10#,4/CS,"304","27331.21",Sysco,123,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    row = df.iloc[0]
    assert row["total_weight_lbs"] == pytest.approx(4 * 10 * 304)  # NOT x4 again
    assert row["weight_source"] == "computed_tier2"


def test_load_ucr_sysco_bare_name_weight_needs_purchase_unit_case_multiplier(tmp_path):
    # Real bug: when the name states only a bare per-ITEM weight ("1LB",
    # no chain), Purchase Unit's own leading case-pack count ("30/CS")
    # must be multiplied in -- treating the bare name number as if it
    # were already the full per-case weight (ignoring the real 30x
    # multiplier) understated weight by 30x and implied an absurd
    # $100/lb for butter; fixed to give a plausible $3.36/lb.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,BUTTER SOLID USDA AA UNSLTD 1LB,30/CS,"152","15329.49",Sysco,123,,,,\n'
        '2,PASTE CHILI KOREAN GOCHUJANG 2.2#,JAR,"410","4925.68",Sysco,124,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    butter = df[df["raw_name"] == "BUTTER SOLID USDA AA UNSLTD 1LB"].iloc[0]
    assert butter["total_weight_lbs"] == pytest.approx(1 * 30 * 152)
    assert butter["weight_source"] == "computed_tier2"
    # Bare unit-of-sale word ("JAR") with no leading number -- multiplier
    # of 1 (each Unit IS one jar).
    paste = df[df["raw_name"] == "PASTE CHILI KOREAN GOCHUJANG 2.2#"].iloc[0]
    assert paste["total_weight_lbs"] == pytest.approx(2.2 * 1 * 410)
    assert paste["weight_source"] == "computed_tier2"


def test_load_ucr_name_embedded_weight_applies_to_all_vendors(tmp_path):
    # The name-embedded chain/bare-weight + Purchase Unit case-multiplier
    # logic was originally verified against Sysco rows only, but
    # "Purchase Unit"/"Units" are a file-wide convention in this export
    # (one shared report format, not a per-distributor combined sheet
    # like UCB's) -- verified against real non-Sysco data (e.g. The Berry
    # Man, Sunrise Produce), so it now applies to every vendor.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,SNR POTATOES RED B QRTRD W/SKIN 10LB,10LB,"1187","23312.65","The Berry Man, Inc",123,,,,\n'
        '2,SNR CARROT MATCHSTICK 5LB,4/CS,"1133","12536.20",Sunrise Produce,124,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    potatoes = df[df["raw_name"] == "SNR POTATOES RED B QRTRD W/SKIN 10LB"].iloc[0]
    assert potatoes["total_weight_lbs"] == pytest.approx(10 * 1187)
    assert potatoes["weight_source"] == "computed_tier2"
    carrots = df[df["raw_name"] == "SNR CARROT MATCHSTICK 5LB"].iloc[0]
    assert carrots["total_weight_lbs"] == pytest.approx(5 * 4 * 1133)
    assert carrots["weight_source"] == "computed_tier2"


def test_load_ucr_explicit_oz_only_applies_to_each_not_case(tmp_path):
    # Real bug: a bare individual-item oz (no chain, e.g. "15.2oz") under
    # Purchase Unit "EACH" genuinely means one item per unit ("NAKED
    # MIGHTY MANGO 15.2oz" implies a plausible $2.58/lb), but under "CASE"
    # it does NOT -- small snack items are always bundled many-per-case,
    # an unstated multi-count this data gives no anchor for. Treating
    # "CASE" the same as "EACH" here implied a median ~$52-109/lb across
    # real data (e.g. small chip bags, sausage patties) before this fix
    # restricted the explicit-oz path to EACH/EACH* only.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,NAKED MIGHTY MANGO 15.2oz,EACH,"3204","7849.80",NAKED JUICE,123,,,,\n'
        '2,SW CHIPS 1.375 OZ JALAPENO MS VICKIES,CASE,"64","2763.85",SALADINOS SUBWAY,124,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    juice = df[df["raw_name"] == "NAKED MIGHTY MANGO 15.2oz"].iloc[0]
    assert juice["total_weight_lbs"] == pytest.approx((15.2 / 16.0) * 3204)
    assert juice["weight_source"] == "computed_tier2"
    chips = df[df["raw_name"] == "SW CHIPS 1.375 OZ JALAPENO MS VICKIES"].iloc[0]
    assert chips["weight_source"] == "unresolved"


def test_load_ucr_explicit_oz_extends_to_ncs_within_plausible_range(tmp_path):
    # "N/CS" (an explicit, unambiguous case-pack count from Purchase
    # Unit) combined with a bare individual-item oz in the name resolves
    # the same way "EACH" does -- e.g. "AQUAFINA ALUMINUM 16OZ" at
    # "24/CS" implies a plausible $1.13/lb for bulk bottled water.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,AQUAFINA ALUMINUM 16OZ,24/CS,"986.21","26671.67",PEPSI-COLA COMPANY,123,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    row = df.iloc[0]
    assert row["total_weight_lbs"] == pytest.approx((16.0 / 16.0) * 24 * 986.21)
    assert row["weight_source"] == "computed_tier2"


def test_load_ucr_explicit_oz_ncs_excludes_implausible_price(tmp_path):
    # Real bug: small individual-serving items frequently bundle far more
    # than "N" per case -- "WPD SEAWEED...0.35OZ" at "12/CS" implied
    # $87/lb, clearly wrong, since a case of seaweed snacks doesn't
    # genuinely contain only 12 individual 0.35 oz packets. A real,
    # data-driven gap exists in the $/lb distribution across all "N/CS" +
    # explicit-oz rows in this file (smooth up to ~$20/lb, then jumps to
    # $26+ with nothing between) -- capped at $20/lb; rows above it are
    # left unresolved rather than guessed.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,WPD SEA SALT ROASTED ORG SEAWEED 0.35OZ,12/CS,"4","91.68",West Pico Distributors,123,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    assert df.iloc[0]["weight_source"] == "unresolved"


def test_load_ucr_explicit_oz_ncs_skips_chain_shaped_names(tmp_path):
    # Real bug: a name with its own embedded chain ("6/1.59OZ") that gets
    # rejected by the per-case plausibility floor (6 x 1.59oz = 0.6 lb,
    # too small to be a real case) must NOT fall through to re-parsing
    # just the bare "1.59" and multiplying by Purchase Unit's case count
    # -- that ignores the name's own "6x" and understates weight ~6x,
    # implying an absurd $45/lb. Left unresolved (shape=='chain', not
    # 'none') rather than reinterpreted.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,KODIAK CAKES PORTAIN CRUNCH 6/1.59OZ,12/CS,"1","54.14",UNFI,123,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    assert df.iloc[0]["weight_source"] == "unresolved"


def test_load_ucr_gal_purchase_unit_density(tmp_path):
    # Purchase Unit "GAL" means Units already IS the gallon count
    # directly (mult=1); "3GAL" gives a per-case gallon size needing
    # x Units; both go through the shared gal_density_dictionary.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,SODA BIB PEPSI 5G,GAL,"1314","22968.60",PEPSI-COLA COMPANY,123,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    row = df.iloc[0]
    assert row["total_weight_lbs"] == pytest.approx(8.3 * 1 * 1314)  # catch-all water-like density
    assert row["weight_source"] == "reference_table_tier3"


def test_load_ucr_number10_can_embedded_in_name(tmp_path):
    # "#10" (a standard foodservice can size, ~6.5 lb) appears embedded
    # directly in the NAME for this campus rather than a separate Pack
    # size column. Case multiplier comes from a real Purchase Unit "N/CS"
    # count when present; else a "N/#10" chain in the name itself; else 1
    # for a bare "CASE". Verified: "TOMATO DICED CANNED NO SALT 6/#10"
    # (6/CS x 110) implies $0.95/lb, "SYSCO CLS FILLING PIE PEACH 6/#10"
    # (CASE, "6/" from the name x 15) implies $2.42/lb, both plausible.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,TOMATO DICED CANNED NO SALT 6/#10,6/CS,"110","4083.38",Sysco,123,,,,\n'
        '2,SYSCO CLS FILLING PIE PEACH 6/#10,CASE,"15","1416.63",Sysco,124,,,,\n'
        '3,SYS CLS PEA BLACKEYE #10,CASE,"1","66.38",Sysco,125,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    tomato = df[df["raw_name"] == "TOMATO DICED CANNED NO SALT 6/#10"].iloc[0]
    assert tomato["total_weight_lbs"] == pytest.approx(6.5 * 6 * 110)
    assert tomato["weight_source"] == "reference_table_tier3"
    pie = df[df["raw_name"] == "SYSCO CLS FILLING PIE PEACH 6/#10"].iloc[0]
    assert pie["total_weight_lbs"] == pytest.approx(6.5 * 6 * 15)  # "6/" parsed from the name itself
    assert pie["weight_source"] == "reference_table_tier3"
    pea = df[df["raw_name"] == "SYS CLS PEA BLACKEYE #10"].iloc[0]
    assert pea["total_weight_lbs"] == pytest.approx(6.5 * 1 * 1)  # bare "#10", no multiplier anywhere
    assert pea["weight_source"] == "reference_table_tier3"


def test_load_ucr_number10_excludes_non_can_size_codes(tmp_path):
    # Real bug: "#10" isn't always a can size -- "CONE CAKE #10 36/20CT"
    # and "MEAD ENVELOPES WHITE #10 50CT" use it as an unrelated product
    # size code (a cone size, an envelope size), which implied an absurd
    # $0.20-0.22/lb when treated as a 6.5 lb can. Excluded by name.
    # "CONE CAKE" is later separately resolved via the ucr_count_food_dictionary
    # wafer-cone entry (its own self-contained "36/20CT" chain, unrelated to
    # the "#10" size code) -- envelopes have no such dictionary entry and
    # stay unresolved.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,CONE CAKE #10 36/20CT,36/CS,"243","12761.37",Sysco,123,,,,\n'
        '2,MEAD ENVELOPES WHITE #10 50CT,EACH,"5","6.60",Sysco,124,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    cone = df[df["raw_name"] == "CONE CAKE #10 36/20CT"].iloc[0]
    assert cone["weight_source"] == "reference_table_tier3"
    envelope = df[df["raw_name"] == "MEAD ENVELOPES WHITE #10 50CT"].iloc[0]
    assert envelope["weight_source"] == "unresolved"


def test_load_ucr_try_tray_purchase_unit(tmp_path):
    # "TRY"/"TRAY" verified only against real bare-LB rows (e.g. "SNR
    # SALSA PICO DE GALLO FRESH 5LB TRAY" implies a plausible $3.44/lb).
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,SNR SALSA PICO DE GALLO FRESH 5LB TRAY,TRY,"91","1564.46",Sysco,123,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    row = df.iloc[0]
    assert row["total_weight_lbs"] == pytest.approx(5.0 * 1 * 91)
    assert row["weight_source"] == "computed_tier2"


def test_load_ucr_name_embedded_gal_chain_vs_bare(tmp_path):
    # Gallon quantities also show up embedded directly in the NAME rather
    # than the Purchase Unit column. A "chain"-shaped name ("6/1GAL" = 6
    # gal/case already) is self-contained and must NOT also be multiplied
    # by the Purchase Unit's case count -- verified "OIL CORN 6/1GAL" at
    # Purchase Unit "6/CS" implies a plausible $2.03/lb using the chain
    # alone (an implausible $0.34/lb results if the "6" from Purchase
    # Unit is applied on top of it too). A "bare"-shaped name ("0.5GAL")
    # needs the Purchase Unit's case multiplier -- "CREAM HEAVY
    # MANUFACTURING 40% 0.5GAL" (6/CS) implies a plausible $1.69/lb.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,OIL CORN 6/1GAL,6/CS,"529","48211.92",Sysco,123,,,,\n'
        '2,CREAM HEAVY MANUFACTURING 40% 0.5GAL,6/CS,"1371.33","59828.77",Sysco,124,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    oil = df[df["raw_name"] == "OIL CORN 6/1GAL"].iloc[0]
    assert oil["total_weight_lbs"] == pytest.approx(7.5 * (6 * 1) * 529)  # chain alone, NOT x6 again
    assert oil["weight_source"] == "reference_table_tier3"
    cream = df[df["raw_name"] == "CREAM HEAVY MANUFACTURING 40% 0.5GAL"].iloc[0]
    assert cream["total_weight_lbs"] == pytest.approx(8.6 * (0.5 * 6) * 1371.33)  # bare x Purchase Unit's 6
    assert cream["weight_source"] == "reference_table_tier3"


def test_load_ucr_name_embedded_gal_excludes_non_food(tmp_path):
    # Non-food items that use "GAL" as a capacity rating (trash liners)
    # rather than a food-liquid volume must not get a food density
    # applied, whether the GAL figure is in Purchase Unit or the name.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,"LINER TRASH 43\'\' X 48\'\' 16 MICRON 56GAL",CASE,"28","1531.04",Sysco,123,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    assert df.iloc[0]["weight_source"] == "unresolved"


def test_load_ucr_gal_regex_handles_no_leading_digit_decimal(tmp_path):
    # Real bug: the number pattern originally required a leading digit
    # before an optional decimal point, which failed to match a
    # bare-decimal like ".5" (no leading zero) -- "WHLFCLS MILK NON-FAT
    # 6/.5GAL" was silently parsed as "5GAL" (dropping the leading "."),
    # a 10x overcount that implied an absurd $0.057/lb instead of the
    # correct $0.57/lb.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,WHLFCLS MILK NON-FAT 6/.5GAL,6/CS,"18","266.44",Sysco,123,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    row = df.iloc[0]
    assert row["total_weight_lbs"] == pytest.approx(8.6 * (6 * 0.5) * 18)
    assert row["weight_source"] == "reference_table_tier3"


def test_load_ucr_case_formula_produce_gated_by_naming_prefix(tmp_path):
    # Keyword matching alone is too risky here -- "APPLE"/"ORANGE"/etc.
    # also match dozens of unrelated beverage/candy items in this file
    # (CELSIUS FUJI APPLE, GATORADE LEMON LIME, OCEAN SPRAY APPLE, etc.),
    # so every produce pattern is anchored to this export's own genuine
    # fresh-produce naming prefixes ("SNR "/"SW "). "Case"-formula entries
    # (avocado/orange/apple/pear) use Units directly, sidestepping the
    # need to parse a count at all -- verified: "SNR APPLE GALA XF 125CT"
    # (a bare "CASE" Purchase Unit) implies $0.637/lb, "SNR APPLES GRANNY
    # SMITH 88CT" (Purchase Unit "80/88CT", a size-grade RANGE, correctly
    # ignored) implies $0.71/lb, both plausible.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,SNR APPLE GALA XF 125CT,CASE,"40","1019.59",Sunrise Produce,123,,,,\n'
        '2,SNR APPLES GRANNY SMITH 88CT,80/88CT,"148","4205.09",Sunrise Produce,124,,,,\n'
        '3,CELSIUS FUJI APPLE 12OZ,CASE,"10","100.00",TREPCO (COREMARK),125,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    gala = df[df["raw_name"] == "SNR APPLE GALA XF 125CT"].iloc[0]
    assert gala["total_weight_lbs"] == pytest.approx(40.0 * 40)
    assert gala["weight_source"] == "reference_table_tier3"
    granny = df[df["raw_name"] == "SNR APPLES GRANNY SMITH 88CT"].iloc[0]
    assert granny["total_weight_lbs"] == pytest.approx(40.0 * 148)  # flat carton, NOT the 80/88 range
    assert granny["weight_source"] == "reference_table_tier3"
    celsius = df[df["raw_name"] == "CELSIUS FUJI APPLE 12OZ"].iloc[0]
    assert celsius["weight_source"] == "unresolved"


def test_load_ucr_multiply_formula_produce_uses_purchase_unit_or_name_count(tmp_path):
    # "Multiply"-formula entries need a real per-case count, taken from a
    # bare Purchase Unit "NCT"/"NCT-CS" when present, else a name-embedded
    # count. Verified: "SNR CUCUMBERS 36CT" (Purchase Unit "36CT*")
    # implies $0.80/lb; "NOODLE RAMEN IQF COOKED SUN 80CT" (count from the
    # name, Purchase Unit bare "CASE") implies $4.15/lb, both plausible.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,SNR CUCUMBERS 36CT,36CT*,"996","18998.95",Sunrise Produce,123,,,,\n'
        '2,NOODLE RAMEN IQF COOKED SUN 80CT,CASE,"599","31097.00",Sysco,124,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    cucumber = df[df["raw_name"] == "SNR CUCUMBERS 36CT"].iloc[0]
    assert cucumber["total_weight_lbs"] == pytest.approx(0.66 * 36 * 996)
    assert cucumber["weight_source"] == "reference_table_tier3"
    ramen = df[df["raw_name"] == "NOODLE RAMEN IQF COOKED SUN 80CT"].iloc[0]
    assert ramen["total_weight_lbs"] == pytest.approx(0.15625 * 80 * 599)
    assert ramen["weight_source"] == "reference_table_tier3"


def test_load_ucr_multiply_formula_applies_purchase_unit_multiplier_to_bare_count(tmp_path):
    # Real bug: "BUN BRIOCHE MINI SLICED TURANO 18CT" (a bare, non-chain
    # count in the name) at Purchase Unit "12/CS" was resolved using just
    # the bare 18-count, silently dropping Purchase Unit's real "12"
    # case-multiplier -- a 12x undercount that implied an absurd
    # $41.34/lb. Fixed to use 18 x 12 = 216, giving a plausible $3.44/lb.
    # "TORTILLA FLOUR 10\" 12/12CT" (a chain, self-contained) must NOT
    # also be multiplied by Purchase Unit "12/CS" (redundant with the
    # chain's own leading token) -- implies a plausible $2.12/lb using
    # the chain alone.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,BUN BRIOCHE MINI SLICED TURANO 18CT,12/CS,"734","34138.34",Sysco,123,,,,\n'
        '2,TORTILLA FLOUR 10" 12/12CT,12/CS,"398","15219.55",Sysco,124,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    bun = df[df["raw_name"] == "BUN BRIOCHE MINI SLICED TURANO 18CT"].iloc[0]
    assert bun["total_weight_lbs"] == pytest.approx(0.0625 * (18 * 12) * 734)
    assert bun["weight_source"] == "reference_table_tier3"
    tortilla = df[df["raw_name"] == 'TORTILLA FLOUR 10" 12/12CT'].iloc[0]
    assert tortilla["total_weight_lbs"] == pytest.approx(0.125 * (12 * 12) * 398)  # chain alone, NOT x12 again
    assert tortilla["weight_source"] == "reference_table_tier3"


def test_load_ucr_multiply_formula_falls_back_to_purchase_unit_only_count(tmp_path):
    # Some names carry no count of their own at all ("SNR CORN COB
    # YELLOW") -- the count lives only in Purchase Unit's bare "NCT"
    # ("48CT"), used as a last resort when the name gives nothing.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,SNR CORN COB YELLOW,48CT,"31","850.05",Sunrise Produce,123,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    row = df.iloc[0]
    assert row["total_weight_lbs"] == pytest.approx(0.15625 * 48 * 31)
    assert row["weight_source"] == "reference_table_tier3"


def test_load_ucr_egg_dozen_count(tmp_path):
    # Eggs are sold by a dozen-count rather than a piece count ("15DZ" =
    # 15 dozen = 180 eggs; "15/1DZ" = a chain, 15 x 1 dozen = 180 eggs) --
    # a standard large egg runs ~2 oz. Verified: "EGG SHELL LARGE WHITE
    # CAGE FREE 15DZ" implies $3.44/lb, plausible for bulk eggs.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,EGG SHELL LARGE WHITE CAGE FREE 15DZ,CASE,"94","7265.99",Sysco,123,,,,\n'
        '2,SNR EGG-LARGE RETAIL 15/1DZ,CASE,"36","2494.52",Sunrise Produce,124,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    bare = df[df["raw_name"] == "EGG SHELL LARGE WHITE CAGE FREE 15DZ"].iloc[0]
    assert bare["total_weight_lbs"] == pytest.approx(0.125 * (15 * 12) * 94)
    assert bare["weight_source"] == "reference_table_tier3"
    chain = df[df["raw_name"] == "SNR EGG-LARGE RETAIL 15/1DZ"].iloc[0]
    assert chain["total_weight_lbs"] == pytest.approx(0.125 * (15 * 1 * 12) * 36)
    assert chain["weight_source"] == "reference_table_tier3"


def test_load_ucr_sysco_cross_campus_case_weight_lookup(tmp_path):
    # Sysco is a national distributor -- some SKUs recur across campuses.
    # "SAUSAGE TURKEY PTY CKD 1.6OZ" has a confirmed Pack size of "10LB"
    # in UC Berkeley's own Sysco data (a real value, not an estimate),
    # reused here on the assumption the same national SKU is packed the
    # same way regardless of which campus orders it. Verified: implies
    # $4.38/lb, plausible for turkey sausage patties.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,SAUSAGE TURKEY PTY CKD 1.6OZ,CASE,"651","28512.30",Sysco,123,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    row = df.iloc[0]
    assert row["total_weight_lbs"] == pytest.approx(10.0 * 651)
    assert row["weight_source"] == "reference_table_tier3"


def test_load_ucr_liter_and_ml_embedded_in_name(tmp_path):
    # Liter/mL quantities embedded in the name (converted to
    # gallon-equivalents and run through the shared density dictionary).
    # A "chain"-shaped name ("2/1.5LTR") is self-contained -- Purchase
    # Unit's separate "2/CS" (redundant with the chain's own leading "2")
    # must NOT also be multiplied in. Verified: "JUICE CONC FRZ ORG GV
    # PAS 3L" (bare, CASE) implies $0.82/lb, plausible for juice
    # concentrate; "CREAMER FRENCH VAN COFFEEMATE 2/1.5LTR" (chain)
    # implies $1.63/lb, plausible for coffee creamer.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,JUICE CONC FRZ ORG GV PAS 3L,CASE,"667","45563.44",Sysco,123,,,,\n'
        '2,CREAMER FRENCH VAN COFFEEMATE 2/1.5LTR,2/CS,"156","4478.76",Sysco,124,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    juice = df[df["raw_name"] == "JUICE CONC FRZ ORG GV PAS 3L"].iloc[0]
    expected_gal = 3.0 / 3.78541
    assert juice["total_weight_lbs"] == pytest.approx(8.3 * expected_gal * 667)  # water-like catch-all density
    assert juice["weight_source"] == "reference_table_tier3"
    creamer = df[df["raw_name"] == "CREAMER FRENCH VAN COFFEEMATE 2/1.5LTR"].iloc[0]
    expected_gal_chain = (2 * 1.5) / 3.78541
    assert creamer["total_weight_lbs"] == pytest.approx(8.6 * expected_gal_chain * 156)  # dairy creamer, chain alone
    assert creamer["weight_source"] == "reference_table_tier3"


def test_load_ucr_bib_soda_bare_g_means_gallons(tmp_path):
    # A bare "N G" in the name means GALLONS specifically for soda BIB
    # (bag-in-box syrup concentrate) items when Purchase Unit doesn't
    # already say "GAL" -- a real, well-known abbreviation in this data
    # (rows with Purchase Unit "GAL" spell out the identical convention
    # elsewhere, e.g. "SODA BIB PEPSI 5G" at Purchase Unit "GAL"). Requires
    # "BIB" in the name since a bare "G" is otherwise a real gram unit for
    # other products. Verified: implies $0.95/lb, plausible for soda syrup.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,SODA BIB PEPSI ZERO 3G,BIB*,"884.84","21007.02",PEPSI-COLA COMPANY,123,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    row = df.iloc[0]
    assert row["total_weight_lbs"] == pytest.approx(8.3 * 3.0 * 884.84)
    assert row["weight_source"] == "reference_table_tier3"


def test_load_ucr_gram_embedded_in_name(tmp_path):
    # Gram quantities embedded in the name -- same chain-vs-bare
    # distinction, restricted to a genuine numeric "N/CS" Purchase Unit
    # multiplier for bare-shaped names (not the broader bare "CASE"/"EACH"
    # set, since small individual-serving items are exactly the category
    # where that assumption broke down earlier for OZ-based items).
    # Verified: "SEASONING SHICHIMI TOGARASHI SB 300G" (EACH) implies
    # $2.90/lb, plausible for a spice.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,SEASONING SHICHIMI TOGARASHI SB 300G,EACH,"156","954.24",Sysco,123,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    row = df.iloc[0]
    assert row["total_weight_lbs"] == pytest.approx(300.0 * 0.00220462 * 156)
    assert row["weight_source"] == "computed_tier2"


def test_load_ucr_gal_and_liter_exclude_non_food_pump(tmp_path):
    # Real bug: "SBUX CBS PLAST PUMP 3.75 ML 3/IP" is a pump dispenser
    # DEVICE (confirmed by its own "Food Service Supplies" cost category
    # in the raw data), not a food liquid -- its "3.75 ML" describes the
    # pump mechanism's capacity, not a food volume. Excluded by name
    # (word-bounded so it doesn't also match "PUMPKIN").
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '1,SBUX CBS PLAST PUMP 3.75 ML 3/IP,EACH,"3","3.66",STARBUCKS COFFEE COMPANY,123,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    assert df.iloc[0]["weight_source"] == "unresolved"


def test_load_ucr_excludes_trailing_grand_total_summary_rows(tmp_path):
    # Real bug: a trailing summary block (blank Item/Purchase Unit/Vendor,
    # only Name + a dollar figure populated) was being ingested as if each
    # row were its own product -- "Total Spend" ($10.2M) and "Sustainable
    # Spend" ($652K) ended up counted as individual line items, materially
    # inflating both total and "sustainable" spend figures.
    csv = (
        "Item,Name,Purchase Unit,Units, Total Spend ,Vendor,VON,Sustainability,Sustainability Link,Plant-Based? Y/N,Cost Category\n"
        '2,CHICKEN HALAL,20#CS,"5","301.00",Sysco,123,,,,\n'
        ',,,,,,,,,,\n'
        ',Total Spend,,," $ 10,232,470.30 ",,,,,,\n'
        ',,,,,,,,,,\n'
        ',Total Food & Beverage Spend,,," $ 9,310,665.57 ",,,,,,\n'
        ',Sustainable Spend,,," $ 652,686.78 ",,,,,,\n'
        ',Sustainable %,,,7.01%,,,,,,\n'
    )
    df = load_ucr(_write(tmp_path, "ucr.csv", csv))
    assert len(df) == 1
    assert set(df["raw_name"]) == {"CHICKEN HALAL"}


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


def test_load_ucsc_uses_cost_not_repeated_total_item_cost(tmp_path):
    # Real bug: "Total Item cost" repeats the SAME aggregate value across
    # every row of a product that spans multiple rows (e.g. split across
    # delivery batches), rather than being that row's own cost -- verified
    # against real data (88 groups, 0 mismatches between the repeated
    # value and the sum of "Cost" across those rows). Using "Total Item
    # cost" directly double(or more)-counted split products -- most
    # visibly on real rows literally named "GENERIC FOOD" that all shared
    # one repeated $3,109.42 total regardless of each row's very different
    # actual cost. "Cost" is the real per-row amount and must be used
    # instead.
    header = (
        'STARS 3.0 Cert,Real Food,Product Description,Month,Year,"Item #\n(Foodpro)","Product #\n(VON)",Cost,'
        'Total Item cost,"Units\nPurchased",Units,"Unit\nType",Total units ordered,"Total Weight \n(in lbs)",'
        'Label/Brand,Vendor,Food Type (WRI),Food Type (RFC),"Is this product plant-\nbased?",Notes\n'
    )
    row1 = '❌,❌,GENERIC FOOD,11,2024,1,1,"$173.25","$3109.42",3,,LB,,-,Brand,Daylight,,,,\n'
    row2 = '❌,❌,GENERIC FOOD,12,2024,2,2,"$37.15","$3109.42",1,,LB,,-,Brand,Daylight,,,,\n'
    df = load_ucsc(_write(tmp_path, "ucsc.csv", "junk\n" + header + row1 + row2))
    assert len(df) == 2
    assert sorted(df["total_price"].tolist()) == pytest.approx(sorted([173.25, 37.15]))


def test_load_ucsc_parses_grams(tmp_path):
    header = (
        'STARS 3.0 Cert,Real Food,Product Description,Month,Year,"Item #\n(Foodpro)","Product #\n(VON)",Cost,'
        'Total Item cost,"Units\nPurchased",Units,"Unit\nType",Total units ordered,"Total Weight \n(in lbs)",'
        'Label/Brand,Vendor,Food Type (WRI),Food Type (RFC),"Is this product plant-\nbased?",Notes\n'
    )
    row = '✅,✅,SAUCE HOT PKT,11,2024,3,3,"$8.00","$8.00",1,32200,g,"32200",-,Brand,Vendor,,,,\n'
    df = load_ucsc(_write(tmp_path, "ucsc.csv", "junk\n" + header + row))
    g_item = df.iloc[0]
    assert g_item["total_weight_lbs"] == pytest.approx(32200 * 0.00220462)
    assert g_item["weight_source"] == "reported"


def test_load_ucsc_parses_lb_variants_and_kg(tmp_path):
    # "LBS" and "LB AVG" are plain spelling variants of "LB" that check
    # out as direct weights against real data (e.g. "PORK CARNITAS MEAT
    # PRCK CAFE H" implies a plausible $3.98/lb). "KG" is a direct
    # conversion, not an estimate.
    header = (
        'STARS 3.0 Cert,Real Food,Product Description,Month,Year,"Item #\n(Foodpro)","Product #\n(VON)",Cost,'
        'Total Item cost,"Units\nPurchased",Units,"Unit\nType",Total units ordered,"Total Weight \n(in lbs)",'
        'Label/Brand,Vendor,Food Type (WRI),Food Type (RFC),"Is this product plant-\nbased?",Notes\n'
    )
    row1 = '✅,✅,APPLESAUCE FANCY UNSWEET,11,2024,1,1,"$833.10","$833.10",20,,LBS,"1200",-,Brand,Vendor,,,,\n'
    row2 = '✅,✅,PORK CARNITAS MEAT,11,2024,2,2,"$7585.96","$7585.96",63.5,,LB AVG,"1906",-,Brand,Vendor,,,,\n'
    row3 = '✅,✅,CHEESE MANCHEGO AGED,11,2024,3,3,"$298.87","$298.87",41.8,,KG,"250.8",-,Brand,Vendor,,,,\n'
    df = load_ucsc(_write(tmp_path, "ucsc.csv", "junk\n" + header + row1 + row2 + row3))
    lbs_item = df[df["raw_name"] == "APPLESAUCE FANCY UNSWEET"].iloc[0]
    assert lbs_item["total_weight_lbs"] == pytest.approx(1200.0)
    assert lbs_item["weight_source"] == "reported"
    lbavg_item = df[df["raw_name"] == "PORK CARNITAS MEAT"].iloc[0]
    assert lbavg_item["total_weight_lbs"] == pytest.approx(1906.0)
    assert lbavg_item["weight_source"] == "reported"
    kg_item = df[df["raw_name"] == "CHEESE MANCHEGO AGED"].iloc[0]
    assert kg_item["total_weight_lbs"] == pytest.approx(250.8 * 2.20462)
    assert kg_item["weight_source"] == "reported"


def test_load_ucsc_count_based_carton_produce_requires_prod_prefix(tmp_path):
    # Real bug: matching produce-carton keywords (APPLE, ORANGE, etc.)
    # without requiring UCSC's own "PROD " fresh-produce naming prefix
    # also matched non-produce items sharing a fruit name ("FRUIT CAN
    # PEAR SLI CHOICE IN JUICE", a canned good, not a fresh-produce
    # carton) -- 208 of 233 raw keyword matches in this file turned out to
    # be flavor descriptors/condiments/snacks. Restricting to "PROD "
    # correctly resolves the genuine produce line and leaves the
    # look-alike unresolved.
    header = (
        'STARS 3.0 Cert,Real Food,Product Description,Month,Year,"Item #\n(Foodpro)","Product #\n(VON)",Cost,'
        'Total Item cost,"Units\nPurchased",Units,"Unit\nType",Total units ordered,"Total Weight \n(in lbs)",'
        'Label/Brand,Vendor,Food Type (WRI),Food Type (RFC),"Is this product plant-\nbased?",Notes\n'
    )
    row1 = '✅,✅,PROD APPLE GALA,11,2024,1,1,"$1045.40","$1045.40",49,88,CT,"4312",-,Brand,Vendor,,,,\n'
    row2 = '✅,✅,FRUIT CAN PEAR SLI CHOICE IN JUICE,11,2024,2,2,"$786.61","$786.61",13,44,CN,"572",-,Brand,Vendor,,,,\n'
    df = load_ucsc(_write(tmp_path, "ucsc.csv", "junk\n" + header + row1 + row2))
    apple = df[df["raw_name"] == "PROD APPLE GALA"].iloc[0]
    assert apple["total_weight_lbs"] == pytest.approx(40.0 * 49)  # flat 40 lb carton x case count
    assert apple["weight_source"] == "reference_table_tier3"
    canned_pear = df[df["raw_name"] == "FRUIT CAN PEAR SLI CHOICE IN JUICE"].iloc[0]
    assert canned_pear["weight_source"] == "unresolved"


def test_load_ucsc_gal_left_unresolved(tmp_path):
    # GAL/volume units were tried and rejected for this campus: a real GAL
    # row ("MILK WHL HG CORRUGATE") had an internally inconsistent
    # multiplier (Units=1.0 but Total units ordered implies an effective
    # x30), pointing to a genuine data-quality problem in this column.
    # Left unresolved rather than guessed.
    header = (
        'STARS 3.0 Cert,Real Food,Product Description,Month,Year,"Item #\n(Foodpro)","Product #\n(VON)",Cost,'
        'Total Item cost,"Units\nPurchased",Units,"Unit\nType",Total units ordered,"Total Weight \n(in lbs)",'
        'Label/Brand,Vendor,Food Type (WRI),Food Type (RFC),"Is this product plant-\nbased?",Notes\n'
    )
    row = '✅,✅,MILK WHL HG CORRUGATE,11,2024,1,1,"$26117.26","$26117.26",1365,1,GAL,"40950",-,Brand,Vendor,,,,\n'
    df = load_ucsc(_write(tmp_path, "ucsc.csv", "junk\n" + header + row))
    milk = df.iloc[0]
    assert milk["weight_source"] == "unresolved"


def test_load_ucsc_multiply_formula_gated_by_units_threshold(tmp_path):
    # "Units" values for CT/EA/etc. fall into two populations with a wide
    # gap between them in real data: 1-163 (genuine case-pack counts) and
    # 612+ (some other Foodpro-internal unit bleeding into this column --
    # "PROD CELERY WHL" at Units=2,430 implied an absurd $0.0054/lb).
    # Gated at <=200. "PROD BROCCOLINI" (Units=18) is safely under the
    # threshold and resolves; "PROD CELERY WHL" (Units=2,430) does not.
    header = (
        'STARS 3.0 Cert,Real Food,Product Description,Month,Year,"Item #\n(Foodpro)","Product #\n(VON)",Cost,'
        'Total Item cost,"Units\nPurchased",Units,"Unit\nType",Total units ordered,"Total Weight \n(in lbs)",'
        'Label/Brand,Vendor,Food Type (WRI),Food Type (RFC),"Is this product plant-\nbased?",Notes\n'
    )
    row1 = '✅,✅,PROD BROCCOLINI,11,2024,1,1,"$3133.66","$3133.66",83,18,CT,"1494",-,Brand,Vendor,,,,\n'
    row2 = '✅,✅,PROD CELERY WHL,11,2024,2,2,"$435.25","$435.25",22,2430,CT,"53460",-,Brand,Vendor,,,,\n'
    df = load_ucsc(_write(tmp_path, "ucsc.csv", "junk\n" + header + row1 + row2))
    broccolini = df[df["raw_name"] == "PROD BROCCOLINI"].iloc[0]
    assert broccolini["total_weight_lbs"] == pytest.approx(0.5 * 1494)
    assert broccolini["weight_source"] == "reference_table_tier3"
    celery = df[df["raw_name"] == "PROD CELERY WHL"].iloc[0]
    assert celery["weight_source"] == "unresolved"


def test_load_ucsc_multiply_formula_excludes_approx_wt_items(tmp_path):
    # Real bug: "PROD MELON HONEYDEW APPROX WT" has Units=48, safely under
    # the 200 threshold, yet still implied an absurd $0.070/lb -- its own
    # name says "APPROX WT" (this specific line is an approximate-weight
    # estimate, not a real count), regardless of Unit Type or how small
    # Units is. Excluded by name rather than trusted.
    header = (
        'STARS 3.0 Cert,Real Food,Product Description,Month,Year,"Item #\n(Foodpro)","Product #\n(VON)",Cost,'
        'Total Item cost,"Units\nPurchased",Units,"Unit\nType",Total units ordered,"Total Weight \n(in lbs)",'
        'Label/Brand,Vendor,Food Type (WRI),Food Type (RFC),"Is this product plant-\nbased?",Notes\n'
    )
    row = '✅,✅,PROD MELON HONEYDEW APPROX WT,11,2024,1,1,"$13946.38","$13946.38",831,48,CT,"39888",-,Brand,Vendor,,,,\n'
    df = load_ucsc(_write(tmp_path, "ucsc.csv", "junk\n" + header + row))
    assert df.iloc[0]["weight_source"] == "unresolved"


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


def test_load_ucsd_h_weight_zero_is_unresolved_not_reported(tmp_path):
    # Same bug as UCD_H/UCLA_H: weight == 0 is a missing-data placeholder
    # (real spend, no weight recorded), not a real "weighs nothing" claim.
    csv = (
        "account,entry_date,ProductName,env_category,env_sub_category,weight,qty,total_cost,Sustainable?,"
        "Certification,current_month,Distributor,Supplier,description,local,healthy_bev,uom,pack_size,"
        "total_quantity,packaging,beverage_type,stop_light,healthy_other\n"
        "1,7/22/24,Sauce Base,Sauce,Sauce,0,200,500.0,Conventional,,7/1/24,US Foods, , ,Non Local, ,Pounds, , , , , \n"
    )
    df = load_ucsd_h(_write(tmp_path, "ucsd_h.csv", csv))
    assert df["weight_source"].iloc[0] == "unresolved"
    assert pd.isna(df["total_weight_lbs"].iloc[0])


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


def test_split_non_food_removes_personal_care_and_otc_medicine():
    df = pd.DataFrame(
        {
            "raw_name": [
                "KRAZY GLUE ALL PURPOSE 0.07oz",
                "CHAPSTICK ORIGINAL 0.15OZ",
                "NEOSPORIN OINTMENT 0.5oz",
                "VISINE RED EYE COMFORT 0.5OZ",
                "TYLENOL SINUS SEVERE 24CT",
                "HALLS STRAWBERRY COUGH DROP STICK 9CT",
                # "GLUE" must not match "GLUETEN" (a real misspelling of
                # "gluten" found in this data) -- word-boundary check.
                "SOY SAUCE TAMARI GLUETEN FREE .5GAL",
                "CHICKEN BREAST BONELESS",
            ]
        }
    )
    food, non_food = split_non_food(df)
    assert set(non_food["raw_name"]) == {
        "KRAZY GLUE ALL PURPOSE 0.07oz",
        "CHAPSTICK ORIGINAL 0.15OZ",
        "NEOSPORIN OINTMENT 0.5oz",
        "VISINE RED EYE COMFORT 0.5OZ",
        "TYLENOL SINUS SEVERE 24CT",
        "HALLS STRAWBERRY COUGH DROP STICK 9CT",
    }
    assert set(food["raw_name"]) == {"SOY SAUCE TAMARI GLUETEN FREE .5GAL", "CHICKEN BREAST BONELESS"}


def test_split_non_food_removes_cleaning_chemicals_but_not_bleached_flour():
    df = pd.DataFrame(
        {
            "raw_name": [
                "CHEM 143 DEGREASER CLEANER 3L WAXIE",
                "CHEM ANTIBACTERIAL FOAM SOAP ADV 750ML",
                "SBUX CLEANING TABLET JAR CAFIZA 3G",
                "CLOROX DISENFECTANT WIPES LEMON35ct",
                "CHEM DELIMER LIME AWAY ECOLAB 1GAL",
                # real food items that must survive: "BLEACHED"/"UNBLEACHED"
                # and even bare "BLEACH" are legitimate flour descriptors in
                # this data -- bare "BLEACH" is deliberately NOT in the
                # keyword list because of this.
                "FLOUR HI-GLUTEN ALL TRUMP UNBLEACHED (SUS)",
                "FLOUR CAKE & PASTRY BLEACH",
                "CLEANER VEG FRUIT ANTIMICRO",  # non-food produce-wash chemical, not produce itself
            ]
        }
    )
    food, non_food = split_non_food(df)
    assert set(non_food["raw_name"]) == {
        "CHEM 143 DEGREASER CLEANER 3L WAXIE",
        "CHEM ANTIBACTERIAL FOAM SOAP ADV 750ML",
        "SBUX CLEANING TABLET JAR CAFIZA 3G",
        "CLOROX DISENFECTANT WIPES LEMON35ct",
        "CHEM DELIMER LIME AWAY ECOLAB 1GAL",
        "CLEANER VEG FRUIT ANTIMICRO",
    }
    assert set(food["raw_name"]) == {"FLOUR HI-GLUTEN ALL TRUMP UNBLEACHED (SUS)", "FLOUR CAKE & PASTRY BLEACH"}


def test_split_non_food_removes_janitorial_supplies_but_not_lined_produce_or_pad_thai():
    df = pd.DataFrame(
        {
            "raw_name": [
                "LINER TRASH 38X58 1.5 ML CLR",
                "LINER PAN QUILON",
                "SW CAN LINER 56GL 43X48 17MIC",
                "NATRACARE PANTY LINERS",
                "LINER ROLL COMPOST34X48 1ML",
                "TISSUE TOILET 2PLY ANGELSOFT",
                "SW THERMOMETER DIGITAL POCKET",
                "FORK WOODEN DISP COMPOSTABLE",
                "TOTE DEPOSIT",
                # real food items that must survive
                "LETTUCE, HONEY GEM 18 CT LINER",
                "AMYS ENTREE PAD THAI FRZ   12 (9.5 OZ)",
                "AVOCADO HASS FRSH HAND SCOOPED",
            ]
        }
    )
    food, non_food = split_non_food(df)
    assert set(non_food["raw_name"]) == {
        "LINER TRASH 38X58 1.5 ML CLR",
        "LINER PAN QUILON",
        "SW CAN LINER 56GL 43X48 17MIC",
        "NATRACARE PANTY LINERS",
        "LINER ROLL COMPOST34X48 1ML",
        "TISSUE TOILET 2PLY ANGELSOFT",
        "SW THERMOMETER DIGITAL POCKET",
        "FORK WOODEN DISP COMPOSTABLE",
        "TOTE DEPOSIT",
    }
    assert set(food["raw_name"]) == {
        "LETTUCE, HONEY GEM 18 CT LINER",
        "AMYS ENTREE PAD THAI FRZ   12 (9.5 OZ)",
        "AVOCADO HASS FRSH HAND SCOOPED",
    }


def test_split_non_food_removes_disposable_serviceware_but_not_food_using_the_same_words():
    df = pd.DataFrame(
        {
            "raw_name": [
                "KNIFE PLAS COMPST PLANTW MED6",
                "FORK WOOD 6.5IN",
                "SPOON PLAS CORN STARCH",
                "SW BOWL 32 OZ PROTEIN",
                "BOWL PAPER PULP WHITE 12OZ",
                "TRAY FOOD NAT #1",
                "HUHTAMAKI 1# FOOD TRAY -RED PLAID",
                "COVER PLAS BUN PAN RACK 15MC",
                "SW DUST PAN LOBBY W/LONG HANDLE",
                "CHOPSTICK BAMBOO IND WRAP 9",
                "GLAD CLING WRAP 16/100ft",
                "WRAP PAPER ECOCRAFT 12X12",
                # real food items using the exact same bare words -- must survive
                "ENGLISH MUFFIN, PLAIN 3 OZ FORK SPLIT BAKED TRAY FROZEN SANDWICH SIZE",
                "DOLE FRUIT BWL MIX FORK PNAPPL PCH PEAR 12 (7 OZ)",
                "Spinach Baby Spoon 4 lbs",
                "AMYS BOWL RAVIOLI FRZ   12 (9.5 OZ) ORG",
                "CHEESE, MONTEREY JACK SLICED .75 OZ TRAY REF",
                "BON APPETIT PAN DE QUESO 4OZ",
                "PAN'S MUSHROOM JERKY, ORIGINAL",
                "OIL, PAN COATING CANOLA AEROSOL SPRAY ALLERGEN FREE",
                "SANDWICH GRILLED CHICKEN CEASAR WRAP",
                "WRAP TORTILLA WHEAT 12 IN",
            ]
        }
    )
    food, non_food = split_non_food(df)
    assert set(non_food["raw_name"]) == {
        "KNIFE PLAS COMPST PLANTW MED6",
        "FORK WOOD 6.5IN",
        "SPOON PLAS CORN STARCH",
        "SW BOWL 32 OZ PROTEIN",
        "BOWL PAPER PULP WHITE 12OZ",
        "TRAY FOOD NAT #1",
        "HUHTAMAKI 1# FOOD TRAY -RED PLAID",
        "COVER PLAS BUN PAN RACK 15MC",
        "SW DUST PAN LOBBY W/LONG HANDLE",
        "CHOPSTICK BAMBOO IND WRAP 9",
        "GLAD CLING WRAP 16/100ft",
        "WRAP PAPER ECOCRAFT 12X12",
    }
    assert set(food["raw_name"]) == {
        "ENGLISH MUFFIN, PLAIN 3 OZ FORK SPLIT BAKED TRAY FROZEN SANDWICH SIZE",
        "DOLE FRUIT BWL MIX FORK PNAPPL PCH PEAR 12 (7 OZ)",
        "Spinach Baby Spoon 4 lbs",
        "AMYS BOWL RAVIOLI FRZ   12 (9.5 OZ) ORG",
        "CHEESE, MONTEREY JACK SLICED .75 OZ TRAY REF",
        "BON APPETIT PAN DE QUESO 4OZ",
        "PAN'S MUSHROOM JERKY, ORIGINAL",
        "OIL, PAN COATING CANOLA AEROSOL SPRAY ALLERGEN FREE",
        "SANDWICH GRILLED CHICKEN CEASAR WRAP",
        "WRAP TORTILLA WHEAT 12 IN",
    }


def test_split_non_food_removes_packaging_supplies_but_not_branded_or_wrapped_food():
    df = pd.DataFrame(
        {
            "raw_name": [
                "KEYSTON LABEL ROLL SHLF LFE DISS",
                "SW LABELS MONDAY DISSOLVE-A-WAY",
                "PAPER FILM PVC 18\" ROLL 2000FT",
                "BAG PAPER COOKIE WITH WINDOW 5X1 .5X7",
                "BAG PAPER BRN KRAFT GROC 10LB",
                "FOIL ALMN ROLL STD WGT 500FT",
                "FOIL ALUMINUM SHEET 12\"X10.75\" POP UP",
                # real food items that must survive
                "PORK BACON BLACK LABEL SLICED HORMEL 16OZ",
                "CHEESE, CREAM PLAIN LOAF BULK FOIL-WRAPPED REF",
                "SKITTLES FRUIT ORIG PEG BAG",
            ]
        }
    )
    food, non_food = split_non_food(df)
    assert set(non_food["raw_name"]) == {
        "KEYSTON LABEL ROLL SHLF LFE DISS",
        "SW LABELS MONDAY DISSOLVE-A-WAY",
        "PAPER FILM PVC 18\" ROLL 2000FT",
        "BAG PAPER COOKIE WITH WINDOW 5X1 .5X7",
        "BAG PAPER BRN KRAFT GROC 10LB",
        "FOIL ALMN ROLL STD WGT 500FT",
        "FOIL ALUMINUM SHEET 12\"X10.75\" POP UP",
    }
    assert set(food["raw_name"]) == {
        "PORK BACON BLACK LABEL SLICED HORMEL 16OZ",
        "CHEESE, CREAM PLAIN LOAF BULK FOIL-WRAPPED REF",
        "SKITTLES FRUIT ORIG PEG BAG",
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
