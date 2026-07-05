import sqlite3

import pytest

import pandas as pd

from lib.db import init_db
from lib.optimization import (
    InfeasibleScenarioError,
    build_category_baseline,
    identify_category_movers,
    solve_cost_target_then_maximize,
    solve_max_sustainable_keep_cost,
    solve_min_spend_keep_sustainability,
    solve_threshold_third_of_scenario1,
)

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
            (None, "Beans and pulses (dried)", 1.68),
            (None, "Apples", 0.36),
        ],
    )
    # Beef/Poultry/Beans all sit in one "Protein" food group (mirrors the
    # real reference/food_groups.csv), Apples in an unrelated "Fruits"
    # group -- lets tests verify the solver only reallocates weight within
    # a group (beef <-> chicken), never across groups (beef -> apples).
    conn.executemany(
        "INSERT INTO food_groups (simap_category, food_group) VALUES (?, ?)",
        [
            ("Beef & buffalo meat", "Protein"),
            ("Poultry (chicken, turkey)", "Protein"),
            ("Beans and pulses (dried)", "Protein"),
            ("Apples", "Fruits"),
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


def _make_purchase(conn, product_id, price, weight, weight_source="reported", campus=CAMPUS, fy=2025):
    conn.execute(
        "INSERT INTO purchases (campus, fiscal_year, product_id, total_price, total_weight_lbs, weight_source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (campus, fy, product_id, price, weight, weight_source),
    )
    conn.commit()


# --------------------------------------------------------------------------
# build_category_baseline
# --------------------------------------------------------------------------

def test_build_category_baseline_splits_sustainable_and_conventional(conn):
    beef_sus = _make_product(conn, "Grass-fed beef", "Beef & buffalo meat", "Y")
    beef_conv = _make_product(conn, "Feedlot beef", "Beef & buffalo meat", "N")
    _make_purchase(conn, beef_sus, price=700.0, weight=100.0)
    _make_purchase(conn, beef_conv, price=500.0, weight=100.0)

    df = build_category_baseline(conn, CAMPUS)
    row = df[df["simap_category"] == "Beef & buffalo meat"].iloc[0]

    assert row["baseline_spend"] == pytest.approx(1200.0)
    assert row["baseline_sustainable_spend"] == pytest.approx(700.0)
    assert row["baseline_conventional_spend"] == pytest.approx(500.0)
    assert row["sustainable_weight_lbs"] == pytest.approx(100.0)
    assert row["conventional_weight_lbs"] == pytest.approx(100.0)
    assert row["sustainable_price_per_lb"] == pytest.approx(7.0)
    assert row["conventional_price_per_lb"] == pytest.approx(5.0)
    assert row["ghg_factor_kg_per_kg"] == pytest.approx(41.35)
    assert row["weight_resolved_pct"] == pytest.approx(1.0)


def test_build_category_baseline_excludes_unresolved_weight_from_weight_math_not_spend(conn):
    resolved = _make_product(conn, "Chicken breast (weighed)", "Poultry (chicken, turkey)", "N")
    unresolved = _make_product(conn, "Chicken thigh (unweighed)", "Poultry (chicken, turkey)", "N")
    _make_purchase(conn, resolved, price=100.0, weight=50.0, weight_source="reported")
    _make_purchase(conn, unresolved, price=300.0, weight=None, weight_source="unresolved")

    df = build_category_baseline(conn, CAMPUS)
    row = df[df["simap_category"] == "Poultry (chicken, turkey)"].iloc[0]

    # Spend always includes both rows -- unresolved weight doesn't hide spend.
    assert row["baseline_spend"] == pytest.approx(400.0)
    # But weight/$-per-lb only reflect the resolved row.
    assert row["baseline_weight_lbs"] == pytest.approx(50.0)
    assert row["conventional_price_per_lb"] == pytest.approx(2.0)
    assert row["weight_resolved_pct"] == pytest.approx(100.0 / 400.0)


def test_build_category_baseline_tracks_unclassified_spend_separately(conn):
    classified = _make_product(conn, "Classified item", "Beef & buffalo meat", "N")
    unclassified = _make_product(conn, "Mystery item", None, "N")
    _make_purchase(conn, classified, price=100.0, weight=10.0)
    _make_purchase(conn, unclassified, price=250.0, weight=5.0)

    df = build_category_baseline(conn, CAMPUS)

    assert "Mystery item" not in df["simap_category"].values
    assert df.attrs["excluded_unclassified_spend"] == pytest.approx(250.0)


def test_build_category_baseline_handles_all_rows_unresolved(conn):
    # A category with zero resolved weight must not crash -- $/lb is NaN,
    # not a divide-by-zero error, and downstream solvers must exclude it
    # from weight-based optimization rather than guess.
    product = _make_product(conn, "Never weighed beans", "Beans and pulses (dried)", "N")
    _make_purchase(conn, product, price=500.0, weight=None, weight_source="unresolved")

    df = build_category_baseline(conn, CAMPUS)
    row = df[df["simap_category"] == "Beans and pulses (dried)"].iloc[0]

    assert row["baseline_weight_lbs"] == 0
    assert row["conventional_price_per_lb"] != row["conventional_price_per_lb"]  # NaN
    assert row["baseline_spend"] == pytest.approx(500.0)


def test_build_category_baseline_excludes_price_outlier_from_weight_math_not_spend(conn):
    # 12 ordinary Beef & buffalo meat purchases clustered around ~$7/lb --
    # enough rows to clear PRICE_OUTLIER_MIN_CATEGORY_N (10).
    normal_price_per_lb = [6.8, 6.9, 7.0, 7.1, 7.2, 6.85, 6.95, 7.05, 7.15, 6.75, 7.25, 6.65]
    for i, ppl in enumerate(normal_price_per_lb):
        pid = _make_product(conn, f"Normal beef {i}", "Beef & buffalo meat", "N")
        _make_purchase(conn, pid, price=ppl * 100.0, weight=100.0)

    # One corrupted row -- mirrors the real bug found this session: a
    # distributor-reported weight off by orders of magnitude (here,
    # 1,000,000 lbs for a $700 purchase -> $0.0007/lb, ~10,000x below the
    # category's own ~$7/lb median).
    corrupted = _make_product(conn, "Corrupted beef weight", "Beef & buffalo meat", "N")
    _make_purchase(conn, corrupted, price=700.0, weight=1_000_000.0, weight_source="reported")

    df = build_category_baseline(conn, CAMPUS)
    row = df[df["simap_category"] == "Beef & buffalo meat"].iloc[0]

    normal_total_spend = sum(ppl * 100.0 for ppl in normal_price_per_lb)
    normal_total_weight = 100.0 * len(normal_price_per_lb)

    # Spend always includes the corrupted row -- it's real money spent.
    assert row["baseline_spend"] == pytest.approx(normal_total_spend + 700.0)
    # But its 1,000,000 lb weight must NOT be counted -- otherwise it would
    # swamp the other 12 rows' combined ~1,200 lbs and crater the category's
    # $/lb toward zero, exactly the bug this guard exists to catch.
    assert row["baseline_weight_lbs"] == pytest.approx(normal_total_weight)
    assert df.attrs["price_outlier_count"] == 1
    assert df.attrs["price_outlier_spend"] == pytest.approx(700.0)


def test_build_category_baseline_does_not_flag_outliers_in_small_categories(conn):
    # Same corrupted row as above, but the category only has 3 total rows --
    # below PRICE_OUTLIER_MIN_CATEGORY_N (10), so there's not enough data to
    # safely judge what's "normal." Must NOT be flagged (avoids a false
    # positive on a small/sparse category).
    for i, ppl in enumerate([6.8, 7.2]):
        pid = _make_product(conn, f"Normal beef small {i}", "Beef & buffalo meat", "N")
        _make_purchase(conn, pid, price=ppl * 100.0, weight=100.0)
    corrupted = _make_product(conn, "Corrupted beef weight small", "Beef & buffalo meat", "N")
    _make_purchase(conn, corrupted, price=700.0, weight=1_000_000.0, weight_source="reported")

    df = build_category_baseline(conn, CAMPUS)
    row = df[df["simap_category"] == "Beef & buffalo meat"].iloc[0]

    assert df.attrs["price_outlier_count"] == 0
    # The corrupted row's weight IS counted here (not flagged), which is
    # exactly why the guard requires a minimum sample size -- a 3-row
    # category has no reliable basis for calling anything an outlier.
    assert row["baseline_weight_lbs"] == pytest.approx(100.0 + 100.0 + 1_000_000.0)


# --------------------------------------------------------------------------
# Solvers
# --------------------------------------------------------------------------

def _two_category_baseline(conn):
    """Beef: expensive+sustainable-heavy. Chicken: cheap+conventional-heavy.
    Gives the LP real room to shift spend between categories/sub-splits."""
    beef_sus = _make_product(conn, "Grass-fed beef", "Beef & buffalo meat", "Y")
    beef_conv = _make_product(conn, "Feedlot beef", "Beef & buffalo meat", "N")
    chix_sus = _make_product(conn, "Pasture chicken", "Poultry (chicken, turkey)", "Y")
    chix_conv = _make_product(conn, "Conventional chicken", "Poultry (chicken, turkey)", "N")
    _make_purchase(conn, beef_sus, price=700.0, weight=100.0)
    _make_purchase(conn, beef_conv, price=500.0, weight=100.0)
    _make_purchase(conn, chix_sus, price=250.0, weight=100.0)
    _make_purchase(conn, chix_conv, price=150.0, weight=100.0)
    return build_category_baseline(conn, CAMPUS)


def test_solve_min_spend_keep_sustainability_respects_floor_and_bounds(conn):
    baseline = _two_category_baseline(conn)
    result = solve_min_spend_keep_sustainability(baseline, category_lower_multiplier=0.5, category_upper_multiplier=1.5)

    assert result.totals["optimized_cost"] <= result.totals["baseline_cost"] + 1e-6
    assert result.totals["optimized_sustainable_spend"] >= result.totals["baseline_sustainable_spend"] - 1e-6
    # Real bug this exact assertion caught: with an unpopulated food_groups
    # table, every category falls back to its own singleton group, and a
    # singleton group's "fixed group weight" constraint collapses to an
    # exact per-category equality -- silently freezing the whole model at
    # baseline (a no-op "optimization" that still passed every other
    # assertion here, since <= baseline*1.5 is trivially true at baseline).
    assert not result.category_results["optimized_weight_lbs"].equals(result.category_results["baseline_weight_lbs"])
    for _, row in result.category_results.iterrows():
        assert row["optimized_weight_lbs"] <= row["baseline_weight_lbs"] * 1.5 + 1e-6
        assert row["optimized_weight_lbs"] >= row["baseline_weight_lbs"] * 0.5 - 1e-6


def test_category_results_includes_per_category_sustainable_pct(conn):
    baseline = _two_category_baseline(conn)
    result = solve_min_spend_keep_sustainability(baseline)

    beef_row = result.category_results[result.category_results["simap_category"] == "Beef & buffalo meat"].iloc[0]
    # Fixture: beef is $700 sustainable + $500 conventional = 700/1200 = 58.3%
    assert beef_row["baseline_sustainable_pct"] == pytest.approx(700 / 1200 * 100)
    assert beef_row["optimized_sustainable_pct"] == pytest.approx(
        beef_row["optimized_sustainable_spend"] / beef_row["optimized_spend"] * 100
    )


def test_solve_max_sustainable_keep_cost_respects_cap(conn):
    baseline = _two_category_baseline(conn)
    result = solve_max_sustainable_keep_cost(baseline, category_lower_multiplier=0.5, category_upper_multiplier=1.5)

    assert result.totals["optimized_cost"] <= result.totals["baseline_cost"] + 1e-6
    assert result.totals["optimized_sustainable_spend"] >= result.totals["baseline_sustainable_spend"] - 1e-6


def test_solve_cost_target_then_maximize_hits_target(conn):
    baseline = _two_category_baseline(conn)
    result = solve_cost_target_then_maximize(baseline, cost_reduction_target=0.1)

    assert result.totals["cost_pct_change"] == pytest.approx(-10.0, abs=0.05)


def test_solve_threshold_third_of_scenario1_derives_target_correctly(conn):
    baseline = _two_category_baseline(conn)
    scenario1 = solve_min_spend_keep_sustainability(baseline)
    scenario1_cut_pct = -scenario1.totals["cost_pct_change"]

    result = solve_threshold_third_of_scenario1(baseline)

    # Scenario 3's actual cost cut should be ~1/3 of Scenario 1's, per the
    # project owner's exact example (S1 finds 9% -> S3 locks in 3%).
    assert -result.totals["cost_pct_change"] == pytest.approx(scenario1_cut_pct / 3, abs=0.05)
    assert result.assumptions["derived_from_scenario1_cost_reduction_pct"] == pytest.approx(scenario1_cut_pct)


def test_solve_bounds_cross_group_reallocation_to_group_band(conn):
    # Food groups are a +/-15% BAND (not an exact freeze, per project owner
    # direction), with the grand total held exactly fixed as the outer
    # envelope -- so weight *can* move between groups (e.g. into cheap
    # apples out of Protein), but only up to each group's own band, never
    # unbounded.
    beef_sus = _make_product(conn, "Grass-fed beef", "Beef & buffalo meat", "Y")
    beef_conv = _make_product(conn, "Feedlot beef", "Beef & buffalo meat", "N")
    chix_sus = _make_product(conn, "Pasture chicken", "Poultry (chicken, turkey)", "Y")
    chix_conv = _make_product(conn, "Conventional chicken", "Poultry (chicken, turkey)", "N")
    apples = _make_product(conn, "Apples", "Apples", "N")
    _make_purchase(conn, beef_sus, price=700.0, weight=100.0)
    _make_purchase(conn, beef_conv, price=500.0, weight=100.0)
    _make_purchase(conn, chix_sus, price=250.0, weight=100.0)
    _make_purchase(conn, chix_conv, price=150.0, weight=100.0)
    _make_purchase(conn, apples, price=100.0, weight=100.0)  # cheapest $/lb of anything here

    baseline = build_category_baseline(conn, CAMPUS)
    result = solve_min_spend_keep_sustainability(baseline, category_lower_multiplier=0.5, category_upper_multiplier=1.5)

    # Global total is exactly fixed.
    assert result.category_results["optimized_weight_lbs"].sum() == pytest.approx(
        result.category_results["baseline_weight_lbs"].sum(), abs=1e-6
    )

    apples_row = result.category_results[result.category_results["simap_category"] == "Apples"].iloc[0]
    # Apples (its own singleton group) is the cheapest $/lb available, so a
    # cost-minimizer shifts weight into it -- but only up to its group's
    # +/-15% band (100 -> 115), not further, even though its own
    # category-level bound here (0.5x-1.5x) would allow much more.
    assert apples_row["optimized_weight_lbs"] == pytest.approx(115.0, abs=1e-6)

    protein_rows = result.category_results[
        result.category_results["simap_category"].isin(["Beef & buffalo meat", "Poultry (chicken, turkey)"])
    ]
    # Protein's total absorbs the corresponding decrease, itself bounded to
    # +/-15% of its own baseline (400 -> as low as 340) -- 385 here.
    assert protein_rows["optimized_weight_lbs"].sum() == pytest.approx(385.0, abs=1e-6)


def test_solve_raises_clear_error_on_infeasible_bounds(conn):
    baseline = _two_category_baseline(conn)
    # Locking every category's bounds to exactly its own baseline weight
    # while also demanding an above-baseline sustainability floor from a
    # cost-minimizing objective with zero room to shift is infeasible --
    # must raise, not silently return a wrong/empty result.
    with pytest.raises(InfeasibleScenarioError):
        solve_cost_target_then_maximize(
            baseline, cost_reduction_target=0.99, category_lower_multiplier=1.0, category_upper_multiplier=1.0
        )


def test_category_with_zero_baseline_weight_excluded_from_optimization_but_preserved(conn):
    product = _make_product(conn, "Never weighed beans", "Beans and pulses (dried)", "N")
    _make_purchase(conn, product, price=500.0, weight=None, weight_source="unresolved")
    beef_sus = _make_product(conn, "Grass-fed beef", "Beef & buffalo meat", "Y")
    beef_conv = _make_product(conn, "Feedlot beef", "Beef & buffalo meat", "N")
    _make_purchase(conn, beef_sus, price=700.0, weight=100.0)
    _make_purchase(conn, beef_conv, price=500.0, weight=100.0)

    baseline = build_category_baseline(conn, CAMPUS)
    result = solve_min_spend_keep_sustainability(baseline)

    beans_row = result.category_results[result.category_results["simap_category"] == "Beans and pulses (dried)"].iloc[0]
    assert not beans_row["in_optimization"]
    assert beans_row["optimized_spend"] == pytest.approx(beans_row["baseline_spend"])
    # Its baseline spend still shows up in the totals (not silently dropped).
    assert result.totals["optimized_cost"] >= beans_row["baseline_spend"]


# --------------------------------------------------------------------------
# identify_category_movers
# --------------------------------------------------------------------------

def _movers_fixture() -> pd.DataFrame:
    # Hand-built category_results -- exercises identify_category_movers in
    # isolation from the LP solver, since it's a pure post-processing step.
    return pd.DataFrame(
        [
            {
                # Real choice, sustainable is CHEAPER (ratio 0.5) -- shifts
                # way up. Should surface as a "cost-neutral-or-better" win.
                "simap_category": "Beef & buffalo meat",
                "food_group": "Protein",
                "baseline_spend": 1500.0,
                "baseline_sustainable_pct": 33.3,
                "optimized_sustainable_pct": 100.0,
                "in_optimization": True,
                "sustainable_price_per_lb": 5.0,
                "conventional_price_per_lb": 10.0,
            },
            {
                # Real choice, sustainable is a 2x PREMIUM -- if it still
                # shifts up (e.g. under a max-sustainable objective), that's
                # a premium-funded increase, not a free win.
                "simap_category": "Poultry (chicken, turkey)",
                "food_group": "Protein",
                "baseline_spend": 1500.0,
                "baseline_sustainable_pct": 20.0,
                "optimized_sustainable_pct": 80.0,
                "in_optimization": True,
                "sustainable_price_per_lb": 10.0,
                "conventional_price_per_lb": 5.0,
            },
            {
                # No real choice: baseline had zero conventional purchases,
                # so conventional_price_per_lb is NaN -- "100% sustainable"
                # here is a data artifact, not a finding. Must be excluded.
                "simap_category": "Beans and pulses (dried)",
                "food_group": "Protein",
                "baseline_spend": 5000.0,
                "baseline_sustainable_pct": 100.0,
                "optimized_sustainable_pct": 100.0,
                "in_optimization": True,
                "sustainable_price_per_lb": 3.0,
                "conventional_price_per_lb": float("nan"),
            },
            {
                # Real choice and a real shift, but the category's baseline
                # spend is below the materiality threshold -- must be
                # excluded regardless of how dramatic the % shift is.
                "simap_category": "Apples",
                "food_group": "Fruits",
                "baseline_spend": 10.0,
                "baseline_sustainable_pct": 0.0,
                "optimized_sustainable_pct": 100.0,
                "in_optimization": True,
                "sustainable_price_per_lb": 1.0,
                "conventional_price_per_lb": 2.0,
            },
            {
                # Excluded from the LP entirely -- never had a chance to move.
                "simap_category": "Citrus Fruit",
                "food_group": "Fruits",
                "baseline_spend": 2000.0,
                "baseline_sustainable_pct": 50.0,
                "optimized_sustainable_pct": 50.0,
                "in_optimization": False,
                "sustainable_price_per_lb": 4.0,
                "conventional_price_per_lb": 4.0,
            },
        ]
    )


def test_identify_category_movers_excludes_no_choice_categories():
    movers = identify_category_movers(_movers_fixture())
    assert "Beans and pulses (dried)" not in movers["simap_category"].values


def test_identify_category_movers_excludes_below_materiality_threshold():
    movers = identify_category_movers(_movers_fixture(), min_spend_fraction=0.01)
    assert "Apples" not in movers["simap_category"].values


def test_identify_category_movers_excludes_categories_not_in_optimization():
    movers = identify_category_movers(_movers_fixture())
    assert "Citrus Fruit" not in movers["simap_category"].values


def test_identify_category_movers_sorted_by_delta_descending():
    movers = identify_category_movers(_movers_fixture())
    assert list(movers["simap_category"]) == ["Beef & buffalo meat", "Poultry (chicken, turkey)"]
    assert movers.iloc[0]["delta_pts"] == pytest.approx(100.0 - 33.3)
    assert movers.iloc[1]["delta_pts"] == pytest.approx(80.0 - 20.0)


def test_identify_category_movers_flags_cost_neutral_vs_premium():
    movers = identify_category_movers(_movers_fixture())
    beef = movers[movers["simap_category"] == "Beef & buffalo meat"].iloc[0]
    chicken = movers[movers["simap_category"] == "Poultry (chicken, turkey)"].iloc[0]

    assert beef["price_ratio_sus_to_conv"] == pytest.approx(0.5)
    assert beef["cost_neutral_or_better"]

    assert chicken["price_ratio_sus_to_conv"] == pytest.approx(2.0)
    assert not chicken["cost_neutral_or_better"]
